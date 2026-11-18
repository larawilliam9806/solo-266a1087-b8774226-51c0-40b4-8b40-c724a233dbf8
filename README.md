# DICOM 影像批处理服务（匿名化 / 重标识）

影像数据管理员将导出的 DICOM 整理为供研究数据湖关联使用的副本。本服务提供网页批量上传
未压缩 DICOM、统一换号处理、并下载整批处理结果（zip）的能力。

## 处理规则

对每个上传的 DICOM 文件：

| 项目 | 处理方式 |
| --- | --- |
| `PatientName`（含嵌套 PN） | **清空**（保留元素，值置空） |
| `PatientID`（含嵌套） | **替换**为确定性派生的 12 位数字新 ID（`P` + 12 位） |
| `StudyInstanceUID` / `SeriesInstanceUID` / `SOPInstanceUID` | **统一换号** |
| 上述三类 UID 的**全部嵌套引用**（序列内、文件元信息内） | 同步换号 |
| 文件元信息 `MediaStorageSOPInstanceUID` | 与新 `SOPInstanceUID` 同步 |
| SOP Class UID、Transfer Syntax UID、Implementation Class UID | **保持原样** |
| `FrameOfReferenceUID` 等其他非身份 UID | **保持原样** |
| 像素数据（PixelData） | **逐字节保持原样** |
| 传输语法 | 仅接受未压缩（Implicit/Explicit VR LE、Explicit VR BE），压缩语法整批拒绝 |

### UID 换号如何保证一致

- 新 UID 结构为 `<机构前缀>.0.<31 位十进制>`，满足 DICOM UID 规范（仅数字与点、
  每段无前导零、总长 ≤ 64）。首次出现的标识由 `(角色, 原 UID)` 通过 SHA-256
  **确定性派生**，同一进程/同一前缀下结果稳定。
- **已登记映射是唯一权威**：SQLite 中的 `uid_mappings` / `patient_mappings`
  持久化了每个原标识对应的新号。批次处理时先查询已登记值：已登记的
  PatientID 与 Study/Series/SOP UID 一律沿用原新号；只有从未出现过的新标识
  才使用**当前** `DICOM_UID_PREFIX` 派生。
- 因此机构更换前缀并重启后，继续上传同一 Study 的影像仍与已发布影像及历史
  清单保持一致；新出现的标识才使用新前缀。再次重启或重传都不会改号。
- 并发 worker 首次遇到同一原标识时，在写事务（IMMEDIATE）内以首个提交者的
  新号为准并回读权威值，影像、嵌套引用与清单使用同一份已提交映射。
- 若已登记映射与已发布实例的新号（或角色）无法调和，批次无法自洽完成，
  整批标记 `failed`、不产出下载包；既有批次下载不受影响。
- 身份 UID 的角色优先级高于标签提示：例如 `ReferencedStudySequence` 中承载 Study UID 的
  `ReferencedSOPInstanceUID` 会按 study 角色而非 sop 角色换号（见合成样例 `sr_001.dcm`）。

## 数据完整性与可靠性保证

1. **同字节复用**：同一 `SOPInstanceUID` 且原文件 SHA-256 相同，直接复用既有处理结果。
2. **字节冲突整批拒绝**：同一 `SOPInstanceUID` 但字节不同，整个批次标记 `failed`，
   不产生任何可下载产物。并发首次提交的竞态由 IMMEDIATE 事务内的哈希比对兜底。
3. **无半批可下载**：批次只有在全部文件处理、入库、zip 打包并原子 rename 成功后才置为 `ready`；
   zip 先写临时文件再 `os.replace`，失败/中断的批次下载返回 409。
4. **重启恢复**：进程启动时将遗留的 `processing` 批次标记 `failed`；已成功入库的实例与映射
   **保留不丢**，重新上传即可复用且标识不变。
5. **重试不变号**：映射表只插入不更新，已登记的新号被后续批次沿用（即使机构前缀已改变）；
   并发首发同一原标识时以首个提交的映射为准。
6. **先落盘后入库**：对象文件 `fsync` + 原子 rename 后才写数据库记录。
7. **不可调和即拒绝**：已登记映射与已发布实例的新号/角色冲突（或同一原标识存在多套
   已登记新号）时，本批无法与历史影像和清单自洽，整批 `failed` 且无可下载产物；
   既有批次不受影响。

## 目录结构

```
app/
  config.py        环境变量配置（数据目录、UID 前缀、上传大小、SQLite 超时）
  db.py            SQLite 连接、WAL、建表、中断恢复
  identifiers.py   确定性 UID / PatientID 派生
  dicom_proc.py    DICOM 校验、标识收集、映射、匿名化重标识、对象落盘
  services.py      批次编排、zip 原子打包、CSV 清单、状态机
  web.py           Flask 路由与应用工厂
  wsgi.py          gunicorn 入口
  templates/       上传页、批次详情页
samples/
  generate_samples.py  合成样例生成器（含跨文件引用）
  data/                生成的 4 个样例 DICOM
tests/             pytest 测试（含多进程并发与中断恢复）
Dockerfile
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

健康检查：`GET /healthz`。

数据持久化在命名卷 `dicom_data`（容器内 `/data`），包含：

- `dicom.db`（SQLite，WAL 模式）
- `objects/`（内容寻址的处理后 DICOM）
- `tmp/`（上传暂存，按批次隔离，结束即清理）
- `batches/`（最终 zip，原子写入）

## 本地开发（无 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 生成含跨文件引用的合成样例
python samples/generate_samples.py

# 运行测试（含多进程并发、前缀变更重启场景）
pytest -q

# 本地启动
DICOM_DATA_DIR=./data python -m flask --app app.wsgi:app run --debug
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DICOM_DATA_DIR` | `/data` | 数据根目录 |
| `DICOM_UID_PREFIX` | `1.2.826.0.1.3680043.10.9999`（IANA 测试弧） | 派生 UID 的组织前缀，**生产必须替换**为机构合法前缀，≤30 字符 |
| `MAX_CONTENT_LENGTH` | `2147483648`（2GB） | 单请求上传上限（字节） |
| `SQLITE_BUSY_TIMEOUT_MS` | `30000` | SQLite 写锁等待 |
| `FLASK_SECRET_KEY` | 开发占位 | 会话密钥，生产必须替换 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 上传表单与批次列表 |
| POST | `/upload` | multipart 多文件上传并同步处理，重定向到批次详情 |
| GET | `/batches/<id>` | 批次状态、错误信息、文件清单 |
| GET | `/batches/<id>/download` | 下载整批 zip（仅 `ready` 可用，否则 409） |
| GET | `/healthz` | 健康检查 |

下载的 zip 内含处理后的 DICOM（以新 SOP UID 命名）与 `manifest.csv`，
清单列出每个文件的原/新 PatientID、三级 UID 映射及原文件 SHA-256，供数据湖关联。

## 合成样例说明

`samples/generate_samples.py` 生成 4 个未压缩 Explicit VR Little Endian 文件：

- `ct_001.dcm` / `ct_002.dcm`：两张 64×64 带像素的 CT，共享 Study/Series；
- `rs_001.dcm`：RT Structure Set，经 `ReferencedFrameOfReferenceSequence`
  → `RTReferencedStudySequence` → `ContourImageSequence` 跨文件引用两张 CT 的 SOP UID；
- `sr_001.dcm`：Enhanced SR，含 `ReferencedStudySequence`、`ReferencedSeriesSequence`、
  `CurrentRequestedProcedureEvidenceSequence` 三级嵌套引用。
