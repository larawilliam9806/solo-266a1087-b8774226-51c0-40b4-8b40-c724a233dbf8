"""DICOM 批处理核心：校验、标识收集、映射、匿名化重标识、落盘。

不变量：
- 仅清空 PatientName、替换 PatientID；
- 仅对 Study/Series/SOP 三类 UID 及其嵌套引用换号；
- SOP Class UID、Transfer Syntax UID、FrameOfReferenceUID 等其他 UID 不动；
- 像素数据与传输语法原样保留（只接受未压缩传输语法）；
- 同字节实例复用既有结果，字节冲突整批拒绝。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.tag import Tag

from . import config, identifiers
from .db import get_db, immediate_tx

# 未压缩传输语法白名单（deflated 1.2.1.99 属于压缩，拒绝）
UNCOMPRESSED_TS = {
    "1.2.840.10008.1.2",      # Implicit VR Little Endian
    "1.2.840.10008.1.2.1",    # Explicit VR Little Endian
    "1.2.840.10008.1.2.2",    # Explicit VR Big Endian
}

# 三类身份标识标签
TAG_STUDY_UID = Tag(0x0020, 0x000D)
TAG_SERIES_UID = Tag(0x0020, 0x000E)
TAG_SOP_UID = Tag(0x0008, 0x0018)
TAG_PATIENT_ID = Tag(0x0010, 0x0020)
TAG_PATIENT_NAME = Tag(0x0010, 0x0010)

# 非身份 UI 标签的角色提示（仅当值不在本批身份集合中时作为兜底）
ROLE_HINT_TAGS: dict[Tag, str] = {
    Tag(0x0002, 0x0003): "sop",   # Media Storage SOP Instance UID（文件元信息同步）
    Tag(0x0008, 0x1155): "sop",   # Referenced SOP Instance UID
    Tag(0x0020, 0x000D): "study",  # 嵌套序列中的 Study 引用
    Tag(0x0020, 0x000E): "series",  # 嵌套序列中的 Series 引用
}

# 明确不换号的 UI 标签：类 UID、传输语法、帧参考 UID 等
EXCLUDED_UI_TAGS = {
    Tag(0x0002, 0x0002),  # Media Storage SOP Class UID
    Tag(0x0002, 0x0010),  # Transfer Syntax UID
    Tag(0x0002, 0x0012),  # Implementation Class UID
    Tag(0x0008, 0x0016),  # SOP Class UID
    Tag(0x0008, 0x1150),  # Referenced SOP Class UID
    Tag(0x0020, 0x0052),  # Frame of Reference UID
    Tag(0x0020, 0x9162),  # Synchronization Frame of Reference UID
    Tag(0x0020, 0x9165),  # Concatenation Frame of Reference UID
}

ROLES = ("study", "series", "sop")


class BatchRejected(Exception):
    """整批拒绝类错误（任何实例失败都不产出可下载结果）。"""


@dataclass
class PlannedFile:
    tmp_path: Path
    orig_name: str
    sha256: str
    study_uid: str
    series_uid: str
    sop_uid: str
    patient_id: str
    ds: pydicom.Dataset
    reused: bool = False          # 命中既有实例，直接复用对象存储文件
    object_relpath: str = ""


@dataclass
class BatchPlan:
    files: list[PlannedFile] = field(default_factory=list)
    # 全局值 -> 角色（身份优先，标签提示兜底），跨整个批次统一
    value_roles: dict[str, str] = field(default_factory=dict)
    # 全局值 -> 新 UID
    uid_remap: dict[str, str] = field(default_factory=dict)
    patient_remap: dict[str, str] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_ui_elements(ds: pydicom.Dataset):
    """遍历数据集与文件元信息中的全部 UI 元素（含序列嵌套）。"""
    seen: set[int] = set()

    def callback(_ds, elem):
        if elem.VR == "UI":
            key = id(elem)
            if key not in seen:
                seen.add(key)
                yield_elems.append(elem)

    yield_elems: list = []
    ds.walk(callback, recursive=True)
    file_meta = getattr(ds, "file_meta", None)
    if file_meta:
        file_meta.walk(callback, recursive=True)
    return yield_elems


def _ui_values(elem) -> list[str]:
    """取 UI 元素的全部值（兼容单值与多值 VM）。"""
    value = elem.value
    if value is None:
        return []
    if isinstance(value, (list, pydicom.multival.MultiValue)):
        return [v for v in value if v]
    return [value]


def _read_and_validate(path: Path) -> pydicom.Dataset:
    try:
        ds = pydicom.dcmread(str(path), force=False)
    except Exception as exc:  # noqa: BLE001 - 统一转成批次拒绝
        raise BatchRejected(f"无法解析为 DICOM 文件 {path.name}: {exc}") from exc

    if not ds.file_meta or "TransferSyntaxUID" not in ds.file_meta:
        raise BatchRejected(f"{path.name} 缺少文件元信息或 TransferSyntaxUID")
    ts = str(ds.file_meta.TransferSyntaxUID)
    if ts not in UNCOMPRESSED_TS:
        raise BatchRejected(
            f"{path.name} 使用压缩传输语法 {ts}，本服务仅接受未压缩 DICOM"
        )
    for tag, name in (
        (TAG_STUDY_UID, "StudyInstanceUID"),
        (TAG_SERIES_UID, "SeriesInstanceUID"),
        (TAG_SOP_UID, "SOPInstanceUID"),
    ):
        if tag not in ds or not str(ds[tag].value):
            raise BatchRejected(f"{path.name} 缺少必需标识 {name}")
    return ds


def _object_relpath(new_sop_uid: str) -> str:
    # 以新 SOP UID 做两级分片，字符仅含数字与点，天然文件名安全
    return str(Path(new_sop_uid[:2]) / new_sop_uid[2:4] / f"{new_sop_uid}.dcm")


def plan_batch(tmp_files: list[tuple[str, Path]]) -> BatchPlan:
    """读取并校验整批文件，构建全局映射计划；任何冲突直接抛 BatchRejected。"""
    if not tmp_files:
        raise BatchRejected("批次为空")

    plan = BatchPlan()
    seen_sop: dict[str, str] = {}       # 原 SOP UID -> sha256
    instance_new_sops: dict[str, str] = {}  # 原 SOP UID -> 已发布实例的新 SOP UID
    conn = get_db()
    try:
        for orig_name, tmp_path in tmp_files:
            ds = _read_and_validate(tmp_path)
            digest = sha256_file(tmp_path)
            study_uid = str(ds[TAG_STUDY_UID].value)
            series_uid = str(ds[TAG_SERIES_UID].value)
            sop_uid = str(ds[TAG_SOP_UID].value)
            patient_id = str(ds[TAG_PATIENT_ID].value) if TAG_PATIENT_ID in ds else ""

            # 批内同 SOP 去重/冲突
            if sop_uid in seen_sop:
                if seen_sop[sop_uid] != digest:
                    raise BatchRejected(
                        f"批次内 SOPInstanceUID {sop_uid} 对应文件字节不一致，整批拒绝"
                    )
                continue  # 完全重复文件，只处理一次

            # 与库内既有实例比对：同 UID 同字节复用，字节不同整批拒绝
            row = conn.execute(
                "SELECT sha256, object_path, new_sop_uid FROM instances WHERE sop_uid=?",
                (sop_uid,),
            ).fetchone()
            reused = False
            object_relpath = ""
            if row is not None:
                if row["sha256"] != digest:
                    raise BatchRejected(
                        f"SOPInstanceUID {sop_uid} 已存在但文件字节不同，整批拒绝"
                    )
                reused = True
                object_relpath = row["object_path"]
                instance_new_sops[sop_uid] = row["new_sop_uid"]

            pf = PlannedFile(
                tmp_path=tmp_path,
                orig_name=orig_name,
                sha256=digest,
                study_uid=study_uid,
                series_uid=series_uid,
                sop_uid=sop_uid,
                patient_id=patient_id,
                ds=ds,
                reused=reused,
                object_relpath=object_relpath,
            )
            plan.files.append(pf)
            seen_sop[sop_uid] = digest

            # 1) 身份 UID 优先登记角色
            for uid, role in (
                (study_uid, "study"),
                (series_uid, "series"),
                (sop_uid, "sop"),
            ):
                existing = plan.value_roles.get(uid)
                if existing is not None and existing != role:
                    raise BatchRejected(
                        f"UID {uid} 在批次中同时被当作 {existing} 与 {role}，整批拒绝"
                    )
                plan.value_roles[uid] = role

            # 2) 其余 UI 值按标签提示登记角色，冲突拒绝
            for elem in _iter_ui_elements(ds):
                if elem.tag in EXCLUDED_UI_TAGS:
                    continue
                hint = ROLE_HINT_TAGS.get(elem.tag)
                for value in _ui_values(elem):
                    if value in plan.value_roles:
                        continue
                    if hint is not None:
                        plan.value_roles[value] = hint
                    # 无提示且非身份的 UI（如 StorageMediaFileSetUID）保持原值

        # 3) 与库内历史映射核对角色，防止跨批次角色漂移；
        #    已登记的映射是新号的唯一权威来源（即使当前前缀已变化）。
        committed_uids: dict[str, str] = {}
        if plan.value_roles:
            placeholders = ",".join("?" * len(plan.value_roles))
            rows = conn.execute(
                f"SELECT role, original_uid, new_uid FROM uid_mappings "
                f"WHERE original_uid IN ({placeholders})",
                tuple(plan.value_roles),
            ).fetchall()
            registered: dict[str, set[str]] = {}
            for row in rows:
                registered.setdefault(row["original_uid"], set()).add(row["role"])
                committed_uids.setdefault(row["original_uid"], row["new_uid"])
            for original_uid, roles in registered.items():
                if len(roles) > 1:
                    raise BatchRejected(
                        f"UID {original_uid} 历史上被登记为多种角色 "
                        f"{'/'.join(sorted(roles))}，无法一致完成，整批拒绝"
                    )
                wanted = plan.value_roles.get(original_uid)
                if wanted is not None and wanted not in roles:
                    raise BatchRejected(
                        f"UID {original_uid} 历史角色为 {next(iter(roles))}，"
                        f"本批为 {wanted}，整批拒绝"
                    )
                plan.value_roles[original_uid] = next(iter(roles))

        # 4) 解析新 UID：已登记沿用原新号；未登记才用当前前缀派生。
        #    未登记值此时只是候选，persist_mappings 会在写事务内以首个提交值为准
        #    回填 plan.uid_remap，保证并发 worker 最终使用同一份映射。
        for original_uid, role in plan.value_roles.items():
            plan.uid_remap[original_uid] = committed_uids.get(
                original_uid
            ) or identifiers.derive_uid(role, original_uid)

        # 已发布实例的新 SOP UID 同样必须原样沿用（对象文件已按该号落盘）。
        # 仅当与“已提交映射”冲突时才拒绝（说明库内状态本身无法自洽）；
        # 若映射行缺失，则以已发布实例为准，让 persist_mappings 补登记该值。
        for orig_sop, published_new_sop in instance_new_sops.items():
            committed = committed_uids.get(orig_sop)
            if committed is not None and committed != published_new_sop:
                raise BatchRejected(
                    f"SOPInstanceUID {orig_sop} 的已发布新号 {published_new_sop} "
                    f"与映射表 {committed} 不一致，整批拒绝"
                )
            plan.uid_remap[orig_sop] = published_new_sop

        # PatientID 同样优先沿用已登记值（派生本身不含前缀，仍保留兜底）
        patient_ids = {f.patient_id for f in plan.files}
        if patient_ids:
            placeholders = ",".join("?" * len(patient_ids))
            rows = conn.execute(
                f"SELECT original_patient_id, new_patient_id FROM patient_mappings "
                f"WHERE original_patient_id IN ({placeholders})",
                tuple(patient_ids),
            ).fetchall()
            for row in rows:
                plan.patient_remap[row["original_patient_id"]] = row["new_patient_id"]
        for pid in patient_ids:
            plan.patient_remap.setdefault(pid, identifiers.derive_patient_id(pid))
        return plan
    finally:
        conn.close()


def _transform_dataset(ds: pydicom.Dataset, plan: BatchPlan) -> None:
    """按计划原地改写数据集（含文件元信息），像素与传输语法不触碰。"""

    def callback(_ds, elem):
        tag = elem.tag
        if tag == TAG_PATIENT_NAME:
            elem.value = ""  # 仅清空，保留元素与 VR
            return
        if tag == TAG_PATIENT_ID:
            # 对任何遇到的 PatientID（含计划外的嵌套值）都确定性派生，
            # 避免未登记的患者 ID 原样泄露
            elem.value = plan.patient_remap.get(
                str(elem.value),
                identifiers.derive_patient_id(str(elem.value)),
            )
            return
        if elem.VR != "UI" or tag in EXCLUDED_UI_TAGS:
            return
        raw = elem.value
        if raw is None:
            return
        if isinstance(raw, (list, pydicom.multival.MultiValue)):
            elem.value = [plan.uid_remap.get(v, v) if v else v for v in raw]
        else:
            elem.value = plan.uid_remap.get(raw, raw)

    ds.walk(callback, recursive=True)
    file_meta = getattr(ds, "file_meta", None)
    if file_meta:
        file_meta.walk(callback, recursive=True)


def _atomic_write_object(ds: pydicom.Dataset, object_relpath: str) -> Path:
    objects_dir = config.get_objects_dir()
    final_path = objects_dir / object_relpath
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        # 并发下另一 worker 已写出同一确定性结果，直接复用
        return final_path
    tmp_path = objects_dir / f".{os.getpid()}-{id(ds)}.dcm.tmp"
    # write_like_original=False 保证带前导与文件元信息（含换号后的
    # Media Storage SOP Instance UID）按标准 DICOM 文件格式落盘
    ds.save_as(str(tmp_path), write_like_original=False)
    with open(tmp_path, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp_path, final_path)
    return final_path


def persist_mappings(plan: BatchPlan) -> None:
    """持久化 UID/患者映射，并把权威映射回填到 plan。

    写事务（IMMEDIATE）内先插入候选值、再回读已提交值：并发 worker 对同一
    原标识只有第一个提交者的新号生效，后到者必须改用这份已提交映射，从而
    保证影像、嵌套引用与 CSV 清单引用同一套号码。若同一原标识已被登记为
    不同角色，则无法一致完成，整批拒绝。
    """
    conn = get_db()
    try:
        with immediate_tx(conn):
            for original_uid, role in plan.value_roles.items():
                candidate = plan.uid_remap[original_uid]
                rows = conn.execute(
                    "SELECT role, new_uid FROM uid_mappings WHERE original_uid=?",
                    (original_uid,),
                ).fetchall()
                if not rows:
                    conn.execute(
                        "INSERT INTO uid_mappings(role, original_uid, new_uid) "
                        "VALUES(?,?,?)",
                        (role, original_uid, candidate),
                    )
                    committed = candidate
                else:
                    if any(row["role"] != role for row in rows):
                        raise BatchRejected(
                            f"UID {original_uid} 已被并发批次登记为 "
                            f"{'/'.join(sorted({r['role'] for r in rows}))}，"
                            f"本批为 {role}，无法一致完成，整批拒绝"
                        )
                    values = {row["new_uid"] for row in rows}
                    if len(values) > 1:
                        raise BatchRejected(
                            f"UID {original_uid} 存在多套已登记新号，无法一致完成，整批拒绝"
                        )
                    committed = values.pop()
                plan.uid_remap[original_uid] = committed

            for orig_pid in plan.patient_remap:
                candidate = plan.patient_remap[orig_pid]
                conn.execute(
                    "INSERT OR IGNORE INTO patient_mappings"
                    "(original_patient_id, new_patient_id) VALUES(?,?)",
                    (orig_pid, candidate),
                )
                row = conn.execute(
                    "SELECT new_patient_id FROM patient_mappings "
                    "WHERE original_patient_id=?",
                    (orig_pid,),
                ).fetchone()
                if row is None:
                    raise BatchRejected(
                        f"患者 {orig_pid} 的派生新 ID 与既有映射冲突，整批拒绝"
                    )
                if row["new_patient_id"] != candidate:
                    raise BatchRejected(
                        f"患者 {orig_pid} 的已登记新 ID {row['new_patient_id']} "
                        f"与本批 {candidate} 不一致，无法一致完成，整批拒绝"
                    )
                plan.patient_remap[orig_pid] = row["new_patient_id"]
    finally:
        conn.close()


def process_plan(plan: BatchPlan, batch_id: str) -> None:
    """执行计划：写对象存储、登记 instances/batch_files。先落盘后入库。

    实例登记在 IMMEDIATE 事务内做“插入或哈希比对”，堵住两个进程同时首次
    提交同一 SOP 不同字节时 plan 阶段都未见历史记录的竞态窗口。
    """
    conn = get_db()
    try:
        for pf in plan.files:
            if not pf.reused:
                _transform_dataset(pf.ds, plan)
                new_sop = plan.uid_remap[pf.sop_uid]
                pf.object_relpath = _object_relpath(new_sop)
                _atomic_write_object(pf.ds, pf.object_relpath)
            with immediate_tx(conn):
                existing = conn.execute(
                    "SELECT sha256, new_sop_uid FROM instances WHERE sop_uid=?",
                    (pf.sop_uid,),
                ).fetchone()
                new_sop = plan.uid_remap[pf.sop_uid]
                if existing is None:
                    conn.execute(
                        "INSERT INTO instances"
                        "(sop_uid, sha256, new_sop_uid, object_path) VALUES(?,?,?,?)",
                        (
                            pf.sop_uid,
                            pf.sha256,
                            new_sop,
                            pf.object_relpath,
                        ),
                    )
                elif existing["sha256"] != pf.sha256:
                    # 并发竞态兜底：另一批次已提交同 SOP 但字节不同
                    raise BatchRejected(
                        f"SOPInstanceUID {pf.sop_uid} 并发提交了不同字节的文件，整批拒绝"
                    )
                elif existing["new_sop_uid"] != new_sop:
                    # 并发竞态兜底：另一批次已发布该实例且新号不同，
                    # 本批影像与清单将无法自洽，整批拒绝
                    raise BatchRejected(
                        f"SOPInstanceUID {pf.sop_uid} 已发布为不同新号，"
                        f"无法一致完成，整批拒绝"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO batch_files(batch_id, sop_uid, orig_name, "
                    "original_patient_id, original_study_uid, original_series_uid) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, pf.sop_uid, pf.orig_name,
                     pf.patient_id, pf.study_uid, pf.series_uid),
                )
    finally:
        conn.close()
