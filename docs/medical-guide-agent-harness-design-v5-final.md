# 医疗智能导诊 Agent · Harness 架构设计（最终方案 · Python 主栈）

> 设计台账：2026-10-20 · medical-guide-agent · **v5-final** · 关键决策 24 项 · 阻塞项 2 项
>
> **本版定位：可开工的最终方案。** 与 v4 的差别不是又改一轮架构，而是**收敛**——把认证砍到两个入口（OA 单点降为下期、只留契约）、把未知区从未决项收敛为"2 项外部依赖 + 其余已定"，并新增第 15 章「施工顺序、交付范围与验收」。
>
> **版本脉络**：v1（Node 主栈）→ v3（改 Python 主栈 + 头像模块）→ v4（加认证三入口）→ **v5-final（OA 延后，定稿）**。v1/v3/v4 仅作决策演进记录，编码一律以本文件为准。
>
> **六层设计自 v1 起未变**：E/T/C/S/L/V 职责划分、数据来源四态策略、md 六道门禁、grounding 硬门禁全程保留。这是本方案最可信的一点——三次大改（换语言、加头像、改认证）都没动摇骨架。
>
> 状态口径：`已确认` = 用户明确拍板；`已定` = 架构师定稿，施工期可微调但不影响架构；`下期` = 明确不在本期范围。
>
> **可编码三门槛自查**：① **类型完整** —— 第 7、8 节每个类型均给字段定义（Pydantic `BaseModel` / `dataclass` / `Protocol`），无只出现名字的类型；② **API 真实** —— 只用真实存在且未虚构参数的 API：`fastapi.FastAPI / UploadFile / Depends / HTTPException`、`pydantic.BaseModel / ConfigDict`、`PIL.Image / ImageOps.exif_transpose / ImageOps.fit / Image.Resampling.LANCZOS`、`sqlite3.connect`、`os.replace`、`hmac.compare_digest`、`jwt.PyJWKClient`；③ **标准可达** —— 第 1 节 P95 ≤ 3.5s 已拆为逐环节预算，非口号。

---

## 1. 需求规格

| 维度 | 内容 | 状态 |
|---|---|---|
| 目标与用途 | 院内导诊系统。本期四件事：① 语音维护「科室地址」md；② 主界面展示当天出诊科室；③ 语音多轮问答科室位置/地址/服务范围与医生职称/专长/坐诊时间；④ 医生照片上传与展示（管理端代传 + 医生本人经签名直链自传）。 | 已确认 |
| 成功标准 | 语音问答端到端 **P95 ≤ 3.5s**；回答**事实准确率 100%**（grounding 不通过不出答案）；今日排班与权威源一致率 100%；md 语音修改**误写率 0**；头像链路**零破图**。 | 已定 |
| 验收来源（oracle） | ① 120 条人工标注问句回归集；② 排班快照比对；③ md 写入后重读比对 `content_hash`；④ 语音修改的金标准是「确认弹窗前展示的 diff」；⑤ 头像 `pytest` 覆盖 8 类畸形输入；⑥ 直链 `pytest` 覆盖伪造签名/过期/撤销/重复使用。 | 已定 |
| 任务结构 | **固定管线为主**（意图分类→结构化查询→模板渲染），仅"指代消解/澄清重问"为有限多轮。非开放式探索。 | 已确认 |
| 数据/资源边界 | 读：HIS 排班接口、医生信息接口、科室 md、手动排班 md、头像文件。写：**仅本地 md、媒体目录、SQLite**。不写 HIS。 | 已定 |
| 硬约束 | 院内单机/内网；数据不出院；语音链路可离线；禁止输出诊断结论、用药建议、挂号承诺。 | 已确认 |
| 用户与场景 | 患者（自助机 kiosk / 移动端 H5）；导医与管理员（PC 管理端，账号密码登录）；**医生本人（头像自传，经管理端签发的签名直链进入）**。 | 已确认 |
| 边界（不做什么） | 不做挂号/缴费/取号；不做症状诊断（症状→科室仅走可维护规则表）；不替代人工导医；不跨患者读信息。**本期不做：OA 单点、报告解读、就诊数据分析**——三者均只留 `Protocol`，返回 501。 | 已确认 |
| 技术栈约束 | **Python 3.12 + FastAPI**；embeddable Python 免安装部署；前端 vanilla JS 无构建。 | 已确认 |

> **P95 ≤ 3.5s 的逐环节预算**：ASR ≤ 800ms（浏览器内置识别）／规则直答 ≤ 300ms／LLM 槽位抽取 ≤ 1200ms／工具查询 ≤ 600ms／TTS 首字 ≤ 600ms。每段都有独立超时与降级出口。

---

## 2. 架构范式 P

| 子维度 | 决策 | 理由 |
|---|---|---|
| 扩展方式 | **插件化（数据源 + 能力 + 认证）+ 嵌入式（工具与管线）** | 数据源每家医院都不同，必须插件；`analysis/`（报告解读）与 `auth/providers/`（OA 单点）本期只留契约不实现，下期加实现文件即可，主服务不动。工具与导诊管线越稳越好，嵌入式。 |
| 配置方式 | **声明式（策略层）+ 内容层独立** | 数据源优先级、超时阈值走 JSON（`pydantic-settings`）；科室地址与排班内容走 md。 |
| 部署拓扑 | **单机单进程**（可平移到院内多实例） | 隐私与内网约束下的最省解。LLM 允许指向远端，可关闭。 |
| 编排模式 | **中心化单 Agent** | 导诊低自由度；多 Agent 拆不出独立子任务（见第 5 节决策卡）。 |

**Python 带来的范式增量**：同一进程内用 `asyncio.to_thread` 隔离 CPU 密集任务（Pillow 编码、未来的 whisper 推理），不堵塞事件循环——这是 Python 异步栈的必答题。

---

## 3. 技术栈选型

### 3.1 选型表

| 项 | 选型 | 理由 |
|---|---|---|
| 语言 | **Python 3.12**（不用 3.13） | 3.12 是 `pillow` / 未来 `paddleocr` / `faster-whisper` 三方都稳的版本 |
| Web 框架 | **FastAPI 0.115.x + uvicorn 0.34.x** | 需要 `UploadFile`、`Depends`（认证/授权分离）、Pydantic 入参校验；自动 OpenAPI 便于院方 IT 对接 |
| 数据校验/配置 | **Pydantic 2.10.x + pydantic-settings 2.7.x** | 工具入参、LLM 抽取结果、配置文件三处共用一套模型 |
| 认证 | **PyJWT 2.10.x（HS256）+ `hmac`/`hashlib`/`secrets`（标准库）+ argon2-cffi** | 本期只需直链与密码，标准库够用；不引 `authlib`。`SsoProvider` 契约保留，OA 接入时再加 `defusedxml`（CAS XML 有 XXE 风险，本期用不到） |
| 图像处理 | **Pillow 11.1.x**（服务端） | 已是主栈依赖；图像处理收回服务端：不依赖浏览器能力、可防 decompression bomb |
| 数据库 | **`sqlite3`（标准库）+ WAL** | 无需额外驱动；`check_same_thread=False` + 单写锁 |
| 文件上传 | **`python-multipart` + `fastapi.UploadFile`** | `UploadFile` 走 SpooledTemporaryFile，10MB 以下不落盘 |
| LLM | OpenAI 兼容 `/v1/chat/completions`，`httpx.AsyncClient` | 只做槽位抽取与句式润色，可整体关闭 |
| ASR | 浏览器内置识别（默认）；`faster-whisper` 为下期可选 | 零成本起步；实测不达标再上本地模型 |
| TTS | 浏览器 `SpeechSynthesis`（默认）/ `edge-tts`（下期可选） | 无需授权，离线可用 |
| 前端 | vanilla JS + CSS Grid，无构建 | kiosk 不需要 SPA 框架 |
| 测试 | **`pytest` + `pytest-asyncio` + `httpx.ASGITransport`** | 不启真实端口即可测全链路 |
| 运行环境 | Windows 院内机 / Linux 自助机；**embeddable Python 免安装**（见 §3.3） | 现场装系统级 Python 是部署最大摩擦点 |

**本期依赖清单（`requirements.txt`，逐条都必须真的用到）**

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
pydantic-settings==2.7.0
PyJWT==2.10.1
argon2-cffi==23.1.0
pillow==11.1.0
python-multipart==0.0.20
httpx==0.28.1
```

**下期依赖（本期不装，装了就说明越界）**

```
# OA 单点：defusedxml==0.7.1
# 报告解读：paddleocr==2.9.1 · pdfplumber==0.11.4
# 数据分析：pandas==2.2.3
# 本地 ASR：faster-whisper==1.1.0
# TTS 替代：edge-tts==7.0.0
```

**安装命令（本机 pypi 直连被代理拦，必须走镜像）**

```bash
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 3.2 语言选型：为什么定 Python（决策记录，已定稿）

| 能力 | 权重 | Node | Python | 结论 |
|---|---|---|---|---|
| HTTP / 编排 / md 读写 / SQLite | 高 | 强 | 强 | 平手 |
| 医生头像处理 | 中 | 需 `sharp`（原生二进制，院内机常装失败） | **Pillow 纯 wheel** | Python 胜 |
| 检验报告 OCR / PDF（**下期**） | 高 | 弱 | 强（paddleocr） | Python 胜（决定项） |
| 就诊数据分析（**下期**） | 高 | 弱 | 强（pandas） | Python 胜（决定项） |
| 本地 ASR（下期可选） | 中 | whisper.cpp 需自封装 | faster-whisper 成熟 | Python 胜 |
| 院内机部署摩擦 | 高 | 单文件 Node（胜） | 需运行时 | **用 §3.3 抵消** |

**判据**：本地 ASR / 报告 OCR / 数据分析任一项确定落地 → 现在换 Python（迁移成本随时间指数上升）。用户已确认两者在路线图上 → 定 Python。

### 3.3 部署形态：embeddable Python（已确认）

| 方案 | 做法 | 状态 |
|---|---|---|
| **A. embeddable 免安装** | `python-3.12.x-embed-amd64.zip` 解压到 `runtime/python/`，依赖预装在 `runtime/python/Lib/site-packages`，`start.bat` 直接调 `runtime\python\python.exe -m uvicorn app.main:app` | ✅ **采用** |
| B. 在线安装 | `setup.bat` 跑 pip（镜像源） | 仅开发机 |
| C. PyInstaller 冻结 | — | 不采用（Pillow 隐藏依赖维护成本高） |

**方案 A 的三个必踩坑（照做，否则起不来）**

1. embeddable 自带 `python312._pth` **默认不含 site-packages** → 必须显式追加 `Lib\site-packages` 与 `..`（项目根），否则 `import fastapi` 直接失败。
2. embeddable **不含 pip** → 依赖必须在打包机上 `pip install --target runtime/python/Lib/site-packages -r requirements.txt -i <镜像>` 预装，现场不跑 pip。
3. **wheel 不跨平台** → Windows 包与 Linux 包必须分开打，不能一份两用。

**`start.bat` 硬约束**：纯 ASCII + CRLF 行尾；只做 `cd /d %~dp0`、设 `PYTHONPATH`、启服务、写 PID；**不含中文注释**（UTF-8 中文在中文 Windows 下会被 cmd 按 GBK 误解析，表现为"双击没反应"）。

### 3.4 下期计划（本期只留契约，不写实现）

| 插件域 | 下期实现 | **本期交付什么** | 红线 |
|---|---|---|---|
| `auth/providers/` | OA 单点：CAS / OAuth2 / OIDC 三实现 | `SsoProvider` Protocol + `NotConfiguredProvider`（返回 501） | 契约必须留，否则接 OA 要动 `principal.py` / `deps.py` |
| `analysis/report_ocr.py` | paddleocr 抽检验项 + 参考区间 | Protocol + 路由占位（501） | **不得输出结论性判断**，只解释术语与参考区间，强制"以医生解读为准" |
| `analysis/stats.py` | pandas 就诊量/科室分布 | Protocol | 不得对个体患者生成预测 |
| `voice/asr.py::FasterWhisperASR` | 本地转写 | 接口已定义，实现留 TODO | — |

> **纪律（硬约束）**：本期不把占位写成"半成品实现"。留 `Protocol` 是设计，写一半是负债——半成品会被误认为能力已就绪，验收时把"未完成"当"有 bug"。

---

## 4. 六层设计

### 4.1 E 执行循环

**设计决策**

- **执行模式：分类与执行（classify-then-execute），双通道**。不是自由 ReAct。
  - **通道 A · 规则直答（确定性）**：命中科室别名表 + 意图模板 → **不进 LLM**，直接查表渲染。目标覆盖 ≥ 60% 流量，延迟 < 300ms。
  - **通道 B · 模型抽取**：规则未命中 → LLM 只做「意图 + 槽位」结构化抽取（`response_format={"type":"json_object"}` + Pydantic 校验），再走工具查询 + 模板渲染。
  - 两通道**共用同一套工具与渲染器**，防止出现两套事实来源。
- **终止条件由 V 层裁定**：`grounding_check()` 不通过则丢弃模型文本、改用模板原文。模型说"答完了"不算数。
- **防跑飞三道保险**：单轮工具调用 ≤ 3；单轮 LLM token 预算 ≤ 3000；单轮总超时 6s（`asyncio.timeout(6.0)`）。
- **失败回喂与分类恢复**（错误作为观察回上下文，禁吞异常）：

| 类别 | 判据 | 恢复动作 |
|---|---|---|
| 瞬时 | HIS 5xx / 超时 / ASR 空结果 | 指数退避重试 1 次（200ms 抖动），仍失败转下一类 |
| 模型 | 抽取 JSON 不合法 / Pydantic `ValidationError` | **带纠正提示重 prompt 1 次**（附错误字段与合法枚举），仍失败转规则兜底 |
| 永久 | `status=not_found` | **不重试**，返回相似候选 top3 供点选 + 语音播报 |
| 资源 | `status=unavailable` 或熔断打开 | 按 §7.1 数据源策略降级，并向用户**显式声明数据来源** |

- **打转检测**：指纹 = `hash(intent + 归一化槽位)`，同一指纹连续 2 次同结果/同失败 → 打断，输出兜底 + `loop_guard` 事件。

**落地规格**

- `app/core/loop.py::handle_turn(input_: TurnInput, ctx: TurnContext) -> TurnOutput`：先 `try_fast_path()`，未命中走 `run_model_path()`，两条都汇入 `render_answer()` → `evaluate.grounding_check()` → `hooks.run_chain("post_invocation")`。
- **异常不外抛**：`handle_turn` 顶层 `except Exception` 统一转 `TurnOutput` 兜底（患者端不能白屏、不能露英文堆栈），并 `logger.exception` 记录。
- **CPU 密集隔离**：Pillow 编码、指纹哈希的批量计算走 `await asyncio.to_thread(...)`，不阻塞事件循环。
- 超时统一用 `asyncio.timeout()`（3.11+ 内置），逐工具与 LLM 调用各自套一层。

### 4.2 T 工具注册

**设计决策**

- **接入方式**：手写 async 函数 + Pydantic 入参模型，注册进 `ToolRegistry`。不引 MCP、不做动态加载。工具共 8 个（v3 新增 1 个头像相关只读工具）。
- **注册 ≠ 披露 ≠ 授权，三者物理分离**：

| 环节 | 位置 | 规则 |
|---|---|---|
| 注册 | `registry.register(ToolSpec(...))` | 全部注册，运行时持全量 |
| 披露 | `registry.disclose(channel)` | `patient_voice` 只披露 6 个只读工具；`admin_web` / `doctor_web` 额外披露写工具。披露清单是白名单常量，不从注册表推导 |
| 授权 | `hooks.pre_tool_use` | 校验 `(channel, tool, args_fingerprint)`；写 md 需 `confirm_token` 且绑定 `patch_id + after_hash`；写头像需 `PhotoWriteGrant` |

- **执行环境与影响半径**：全部在本进程内执行，无沙箱（单机可信环境）。写半径由 `assert_writable_path()` 硬限定为四个位置：`data/departments.md`、`data/schedule/manual.md`、`data/guide.db`、`data/media/doctors/{hash}-{size}.webp`（后者用正则卡文件名，拒绝任何路径分隔符）。
- **凭证不进执行环境**：HIS `app_secret` 只存在于 `datasource/schedule_api.py` 的闭包与 `settings`，不进工具入参、不进上下文、不进日志。`observe/logger.py` 的 `Redactor` 对键名 `secret|token|password|key|authorization` 一律打码。
- **工具清单**：

| # | 工具 | 入参模型 | 出参 | 通道 |
|---|---|---|---|---|
| ① | `list_on_duty` | `ListOnDutyArgs{date?}` | `ResolvedSchedule` | patient |
| ② | `get_department` | `GetDepartmentArgs{name_or_id}` | `DeptDetail \| CandidateList` | patient |
| ③ | `search_departments` | `SearchArgs{query, limit=5}` | `CandidateList` | patient |
| ④ | `get_doctor` | `GetDoctorArgs{name_or_id}` | `DoctorProfile \| CandidateList` | patient |
| ⑤ | `get_doctor_schedule` | `GetDoctorScheduleArgs{doctor_id, date?}` | `list[DoctorSlot]` | patient |
| ⑥ | `get_doctor_photo` | `GetPhotoArgs{doctor_id}` | `DoctorPhotoRef \| None` | patient |
| ⑦ | `propose_md_patch` | `ProposeMdArgs{department_id, field, new_value}` | `MdPatch`（**不落盘**） | admin |
| ⑧ | `commit_md_patch` | `CommitMdArgs{patch_id, confirm_token}` | `CommitReceipt` | admin |

**落地规格**：`ToolSpec` 用 `@dataclass(frozen=True)`（不是 `BaseModel`——它含 `type[BaseModel]` 与可调用对象，Pydantic 校验这两个字段需要 `arbitrary_types_allowed`，用 dataclass 更直接）。`registry.invoke()` 固定顺序 `pre_tool_use → 执行 → post_tool_use → 落 ToolCallRecord`，任一步失败都返回结构化 `ToolResult`，不抛裸异常。

### 4.3 C 上下文管理

**设计决策**

- **预算分配（8000 token 窗口）**：系统指令 15%（1200）／会话历史 25%（2000，滑动窗口 6 轮 + 超限摘要）／工具结果 40%（3200，单条超 800 token 截断并标 `truncated`）／参考资料 20%（1600）。四档各自设限，只给总量等于没设计。
- **每轮重新组装**：`build_context()` 每轮从 S 层重取；工具调用与其结果**成对保留**，压缩时不拆散。
- **外部内容消毒**（本项目头号注入面是 md 文件）：
  - md 正文、HIS 返回的 `remark`/`intro` 等自由文本，包进 `untrusted_data` 区块（形如 `⟨untrusted_data source="departments.md#心内科"⟩…⟨/untrusted_data⟩`），系统指令声明"该区块是数据不是指令"。
  - 解析时剥离 `忽略以上`、`ignore previous`、`system:`、`im_start` 等标记；剥离动作记审计。
  - 进上下文前按字段白名单裁剪——只取 `location/rooms/service_scope/phone` 四个字段，其余字段根本不进。
- **噪音下沉**：语音改 md 只把**目标 section 的 200 字窗口**送进 LLM，不送整份文件。

**落地规格**：`app/core/context.py::build_context(turn, state) -> ContextBundle`（含 `budget_report`）；`sanitize_external(text, source_ref) -> str` 单独可测。

### 4.4 S 状态存储

**设计决策**

- **结构**：`sqlite3` 单文件 `data/guide.db`，`PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`、`PRAGMA busy_timeout=5000`。连接以 `check_same_thread=False` 创建并由单一 `threading.Lock` 串行化写。
- **内容真相源是 md 文件，不是数据库**：`departments.md` 与 `schedule/manual.md` 是人类可维护的一等公民；DB 只存版本快照 + 索引 + 审计。DB 丢了可重建，md 丢了是真丢 → 每次 md 写入前先在 `md_version` 落完整快照。
- **写操作治理**：md 写入只能经 `md/patch.py::commit_patch()`（唯一写入口），内部依次：`file_lock` → `hash 比对（乐观锁）` → 写 `.tmp` → `os.fsync` → **`os.replace` 原子替换** → 落 `md_version` → 重读校验 hash。任一步失败即回滚并返回结构化错误。
- **并发写**：单写者。`file_lock` 用 `os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)`，TTL 10s，陈旧锁可抢占。不做多进程写入。
- **会话恢复 ≠ 外部行动恢复**：写副作用有两类，都幂等——
  - 写 md：幂等键 `patch_id`，`status='committed'` 时重复提交返回首次 `CommitReceipt`；
  - 写头像：幂等键即**内容 hash**，同 hash 已存在直接复用不重写；
  - **两者都不信任写入返回值**，一律重读文件比对 hash。
- **记忆治理**：
  - 患者会话仅存活于单次会话（`expires_at` 默认 30 分钟），不跨会话持久化姓名/手机号；日志 PII 用 `HMAC-SHA256` 脱敏，salt 走 `GUIDE_PII_SALT` 环境变量。
  - 写入 `md_version` 的每条必须带 `{author, channel, asr_confidence, confirmed_by, raw_utterance}`；**未确认的语音转写永不入库**。
  - 头像记录带 `consent_source`（`doctor_self` / `admin_proxy`），把"肖像授权"变成可追溯字段而非一句提示。
- **证据版本绑定**：`answer_record.source_version` 记回答时 md 的 `content_hash` + 排班快照 id。md 一变更，指向旧 hash 的"准确性结论"自动失效（回归集重跑才恢复有效）。

**落地规格**：`app/core/state.py` 用 `contextlib.contextmanager` 管事务；`md/version.py::rollback(version_id)`。

### 4.5 L 生命周期钩子

| 钩子 | 挂载点 | 职责 |
|---|---|---|
| `pre_invocation` | `handle_turn` 入口 / FastAPI 依赖 | 鉴权（患者端匿名+设备指纹；admin 账号密码；doctor 走签名直链，见 §7.5）、限流（单设备 20 次/分）、敏感词前置 |
| `pre_tool_use` | `registry.invoke` 内 | 通道-工具授权、入参 Pydantic 校验、路径越界检查、写 md 的 `confirm_token` 校验、写头像的 `PhotoWriteGrant` 校验 |
| `post_tool_use` | 工具返回后 | 结果脱敏、`ToolCallRecord` 落库、数据源健康度上报 |
| `post_invocation` | 回答输出前 | grounding 校验、医疗红线过滤、输出长度与格式约束 |

- **拦截点不可绕过**：钩子链在 `registry.invoke` 与 `handle_turn` 内**硬编码调用**，不是可选中间件；LLM 无法通过参数跳过（工具列表里没有 `skip_hooks`）。
- **鉴权写协议（两入口收敛为一个 `Principal`）**：所有接口只认内部 `Principal`，不认外部凭证形态。`Authorization: Bearer {jwt}`，`alg=HS256`，`exp` 30 分钟，`claims={sub, role, dept_scope[], entry}`；`role ∈ {admin, doctor}`，`entry ∈ {magic_link, password}`。验签 `jwt.decode(..., algorithms=["HS256"])` 后用 `hmac.compare_digest` 比对摘要防时序攻击。**两个入口只负责把外部凭证换成 `Principal`**，业务代码不知道医生是从链接来的还是管理员用了密码。OA 单点下期接入时，`entry` 增加 `oa_sso` 一项、`SsoProvider` 加一个实现文件，其余不动。详见 §7.5。
- **人在回路四档**：

| 档位 | 覆盖动作 | 实现 |
|---|---|---|
| 自动执行 | ①–⑥ 全部只读查询 | 无确认 |
| 执行后告知 | 数据源降级、兜底回答、badcase 记录 | 界面黄条 + 语音声明 + 管理端通知 |
| **执行前确认** | **md 文件写入（⑦→⑧）**、手动排班覆盖接口数据→ | **弹确认框展示 diff**，确认后签发 `confirm_token`（TTL 300s，绑定 `patch_id + after_hash`） |
| 直接阻断 | 索要诊断结论/处方、"帮我挂个号"、越权查他人信息、越权改他人头像、md 中出现注入模式 | 固定话术 + `blocked_event` |

> **头像上传为何不设确认弹窗**：上传动作**可见且可逆**（换一张即可），且 `doctor` 自传场景下弹确认毫无意义。每个动作都弹确认会让用户学会无脑点"确定"，**反而削弱 md 写入那个确认的严肃性**。头像的安全靠"校验 + 审计 + 可回滚"，不靠确认。

- **纵深防御**：医疗红线双路——① 规则/关键词前置拦截（独立于模型）；② 输出后独立分类器复核。任一路判定即阻断。**不依赖主 LLM 自己判断"这话能不能说"**。

### 4.6 V 评估接口

- **三层验证**：
  1. **确定性 grounding check（同步、必过）**：从回答抽实体（楼层、门牌、时间、职称、姓名）逐项比对工具结果。全部命中 → pass；任一不命中 → **丢弃模型文本，改用模板原文**。硬门禁，不是打分。
  2. **数据一致性 check（异步、每 5 分钟）**：重放 `list_on_duty`，与界面渲染快照比对，不一致写 `data_conflict`。
  3. **回归集评估（离线、CI）**：120 条标注问句跑全链路，产出准确率/兜底率/延迟分布。
- **生成者与评估者分离**：评估器只接收 `(问句, 工具结果, 回答文本)` 三元组，**看不到思维链**；4 维 0–2 分显式标准（事实正确 / 溯源完整 / 兜底恰当 / 无红线）；迭代预算 2 轮，超限转人工。
- **工具回执 ≠ 业务事实**：`commit_md_patch` 返回 `ok=True` 不算完成——重读文件比对 `content_hash == patch.after_hash` 才在 `md_version` 标 `verified=1`；头像同理，重读落盘文件算 SHA-256 与声明值比对。
- **头像专项验收**（可复现清单）：`pytest tests/test_media_pipeline.py` 覆盖 8 类输入——正常 JPEG、EXIF 旋转 270°、PNG 带透明通道、超大 8000×8000、伪造扩展名的 SVG、1×1 像素、非方形 16:9、重复上传同图（须 `deduped=True`）。
- **防"绕过任务"**：回归集含 20 条负样本（问诊断、问不存在的科室、问院外地址），"答得多"不得分，"答得对 + 该拒则拒"才得分。

---

## 5. 多 Agent 编排

**结论：不适用（单 Agent）。**

| 步 | 结论 |
|---|---|
| 是否真的需要 | **否**。三个功能域是同一进程三条入口，不构成可并行子任务 |
| 拓扑 | N/A（若未来拆：串行流水线 `ASR → 抽取 → 查询 → 渲染`，仍未到多 Agent） |
| 委派契约 / 隔离边界 | N/A |
| 超时与熔断 | 熔断落在**数据源**（breaker）与**LLM 调用**，不落在 Agent 之间 |
| 反模式自查 | 无共享可变状态（md 单写者）、无无界扇出、无超时缺失 |

**未来例外说明**：若报告解读上线，它是**并行能力插件**（`analysis/`）而非子 Agent——因为它的输入输出是确定的（图像/PDF → 结构化检验项），不需要自主决策。

---

## 6. 模块 / 目录结构

```
guide-agent/
├── app/
│   ├── main.py                  # FastAPI 实例、路由注册、lifespan（启动校验 md/front-matter、关库）
│   ├── settings.py              # pydantic-settings：config/*.json + 环境变量
│   ├── deps.py                  # FastAPI 依赖：require_patient / require_admin / require_doctor / require_photo_writer
│   ├── api/
│   │   ├── auth.py              # ★ POST /auth/exchange（直链换 Principal）· POST /auth/password · POST /auth/logout
│   │   ├── voice.py             # POST /api/voice/turn
│   │   ├── schedule.py          # GET  /api/schedule/today
│   │   ├── dept.py              # GET  /api/departments/{dept_id}
│   │   ├── media.py             # GET  /api/media/doctors/{filename}
│   │   ├── photo.py             # ★ POST|DELETE /api/doctors/{doctor_id}/photo（admin + doctor 双角色）
│   │   ├── admin_md.py          # POST /api/admin/md/propose|commit|rollback
│   │   ├── admin_link.py        # ★ POST /api/admin/magic-links（签发/撤销医生直链）
│   │   └── admin_src.py         # GET  /api/admin/source-health
│   ├── auth/                    # ★ 认证域（本期两入口；OA 单点留契约）
│   │   ├── principal.py         # Principal 模型 + JWT 签发/校验（唯一签发点）
│   │   ├── provider.py          # SsoProvider Protocol（下期用）
│   │   ├── providers/
│   │   │   └── not_configured.py # ★ 本期唯一实现：任何调用返回 501
│   │   ├── magic_link.py        # ★ 签名直链：签发 / 校验 / 一次性消费
│   │   ├── password.py          # ★ 管理端密码：argon2id 校验 + 失败锁定
│   │   └── staff.py             # 账号/工号 → 角色映射，映射不到即拒绝
│   ├── core/
│   │   ├── loop.py              # E：双通道、超时、打转检测、失败分类恢复
│   │   ├── registry.py          # T：注册/披露/授权三分离 + invoke 管线
│   │   ├── context.py           # C：预算装配、压缩、外部内容消毒
│   │   ├── state.py             # S：sqlite3 + WAL + 事务上下文
│   │   ├── hooks.py             # L：四类钩子链 + confirm_token / PhotoWriteGrant
│   │   ├── evaluate.py          # V：grounding、一致性、指标聚合
│   │   └── errors.py            # 结构化错误码（E_*），统一映射 HTTP 状态
│   ├── schemas/                 # ★ Pydantic 模型集中处（工具入参、API 出入参、内部结构）
│   │   ├── source.py            # SourceMode / SourcePolicy / SourceMeta / ResolvedSchedule
│   │   ├── domain.py            # ScheduleRow / DeptDetail / DoctorProfile / DoctorSlot / Candidate
│   │   ├── media.py             # DoctorPhotoRef / DoctorPhotoRecord / PhotoWriteGrant
│   │   ├── md.py                # MdPatch / ConfirmToken / CommitReceipt / MdField
│   │   ├── turn.py              # TurnInput / TurnContext / TurnOutput / IntentKind / Slots
│   │   └── tool.py              # ToolResult / ToolCallRecord / GroundingReport / HookResult
│   ├── datasource/
│   │   ├── adapter.py           # DataSourceAdapter Protocol + 注册表 + CircuitBreaker
│   │   ├── schedule_api.py      # HIS 排班（读 secret，不外泄）
│   │   ├── schedule_manual.py   # manual.md 解析
│   │   ├── dept_md.py           # departments.md 解析（front-matter + H2 分节）
│   │   └── doctor_api.py        # 医生职称/专长/坐诊时间
│   ├── policy/
│   │   └── resolver.py          # ★ 数据来源切换决策（§7.1）
│   ├── voice/
│   │   ├── asr.py               # base Protocol + BrowserPassThrough | FasterWhisper | Cloud
│   │   ├── tts.py
│   │   └── intent.py            # 规则直答表 + LLM 槽位抽取（Pydantic 校验 + 纠正重 prompt）
│   ├── md/
│   │   ├── parser.py            # front-matter / 分节 / 表格
│   │   ├── patch.py             # unified diff、原子写、乐观锁
│   │   └── version.py           # 快照与回滚
│   ├── media/
│   │   ├── validate.py          # ★ 魔数嗅探、格式白名单、拒 SVG、MAX_IMAGE_PIXELS
│   │   ├── process.py           # ★ exif_transpose → fit(480/192) → WebP q86
│   │   ├── store.py             # ★ content-hash 命名、原子写、去重、旧图 superseded
│   │   └── serve.py             # FileResponse + immutable 缓存 + 只按 hash 精确匹配
│   ├── analysis/                # ★ v3 只留 Protocol 与 501 占位
│   │   ├── report_ocr.py        # 报告解读（未来）
│   │   └── stats.py             # 就诊数据分析（未来）
│   └── observe/
│       ├── logger.py            # JSON 结构化日志 + Redactor
│       └── metrics.py           # P50/P95、兜底率、grounding 通过率
├── web/
│   ├── kiosk/                   # 患者端（自助机/大屏/H5 同一套）
│   │   ├── index.html           # 界面结构见 §6.1
│   │   ├── app.js               # 今日排班网格 + 状态条
│   │   ├── voice.js             # 语音按钮、转写编辑、对话抽屉、候选点选
│   │   └── styles.css
│   └── admin/
│       ├── index.html
│       ├── md-editor.js         # md 双栏 + 语音录入 + diff 确认浮层
│       ├── schedule-editor.js   # 手动排班维护（含"覆盖接口"显式开关）
│       ├── health.js            # 数据源健康 + 冲突裁决
│       └── photo-panel.js       # ★ 头像上传（FormData + 前端预压缩 + 预览）
├── web/doctor/                  # ★ 医生自助轻量页（本期唯一入口：签名直链）
│   ├── index.html               # 入口页：有 OA 会话则直进，否则显示"发送链接"说明
│   └── upload.js                # 读取 URL fragment 中的 token → Authorization → 上传
├── data/
│   ├── departments.md           # ★ 科室地址真相源（单文件，H2 分节）
│   ├── schedule/manual.md       # 手动排班（md 表格）
│   ├── media/doctors/           # ★ 头像（文件名 = hash + 尺寸，不可变）
│   │   └── ab/abcdef123456-480.webp
│   └── guide.db                 # SQLite（快照/审计/日志）
├── config/
│   ├── app.json                 # 端口/LLM/ASR/限流/超时
│   └── sources.json             # ★ 数据源策略（source_policy）
├── tests/
│   ├── conftest.py              # 内存 DB + 临时 md + ASGITransport 客户端
│   ├── test_policy_resolver.py  # ★ 四态 × 熔断 × 冲突 12 组用例
│   ├── test_md_patch.py         # ★ 六道门禁逐条
│   ├── test_media_pipeline.py   # ★ 8 类畸形输入
│   ├── test_auth.py             # ★ v4：直链签名/过期/撤销/消费 + SSO state 校验 + 工号映射拒绝
│   └── regression/run.py        # 120 条回归集
├── requirements.txt
├── runtime/python/              # embeddable Python（方案 A，随包分发）
└── scripts/
    ├── start.bat / stop.bat     # 纯 ASCII + CRLF
    └── setup.bat                # 方案 B 用：镜像源装依赖
```

### 6.1 整体界面结构

**A. 患者端主屏（kiosk / H5）**

```
┌──────────────────────────────────────────────────────────┐
│  ◉ 数据正常 · HIS 实时 · 更新于 14:02          [院区切换] │ ← 状态条（降级时变黄）
├──────────────────────────────────────────────────────────┤
│  今日出诊科室（2026-10-20 周二）  共 18 个科室            │
│                                                          │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐            │
│  │ 心血管内科  │ │ 呼吸内科    │ │ 骨科        │           │
│  │ 门诊楼3F·东 │ │ 门诊楼3F·西 │ │ 门诊楼2F·南 │           │
│  │ 4位医生出诊 │ │ 3位医生出诊 │ │ 2位医生出诊 │           │
│  │ HIS·14:02  │ │ HIS·14:02  │ │ 手动·今日   │ ← 来源徽标 │
│  └────────────┘ └────────────┘ └────────────┘            │
│  [ 灰色卡片 = 今日停诊，置底，不可点 ]                     │
├──────────────────────────────────────────────────────────┤
│              ╭────────────────────╮                      │
│              │   🎤 按住说话       │  ← 主 CTA（右下悬浮）│
│              ╰────────────────────╯                      │
└──────────────────────────────────────────────────────────┘
```

**B. 语音对话抽屉**

```
┌──────────────────────────────────────────────────────────┐
│  你说：「心内科在几楼」              [重说] [打字]   ✕     │ ← 转写可编辑（置信<0.85 高亮）
├──────────────────────────────────────────────────────────┤
│  🤖 心血管内科在门诊楼 3 楼东侧，3F-12 至 3F-18 诊室。      │
│     服务范围：高血压、冠心病、心律失常…                     │
│     ┌──────────────────────────────────────────┐         │
│     │ 心血管内科  门诊楼3F东  [带我去] [看医生]  │         │
│     └──────────────────────────────────────────┘         │
│     ⓘ 数据来源：科室信息手册（10-18 更新）                 │ ← 溯源徽标
│                                                          │
│  你说：「那有哪些医生」                                    │ ← 指代继承上文科室
│  🤖 今日出诊 4 位：                                      │
│     ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐         │
│     │ ┌────┐ │ │ ┌────┐ │ │ ┌────┐ │ │ ┌────┐ │         │
│     │ │头像│ │ │ │头像│ │ │ │ 张 │ │ │ │头像│ │         │ ← 无图/失败→首字占位
│     │ └────┘ │ │ └────┘ │ │ └────┘ │ │ └────┘ │         │
│     │ 张明   │ │ 李红   │ │ 王强   │ │ 赵敏   │         │
│     │ 主任医师│ │ 副主任 │ │ 主治   │ │ 副主任 │         │
│     │ 上午   │ │ 下午   │ │ 上午   │ │ 全天   │         │
│     └────────┘ └────────┘ └────────┘ └────────┘         │
│     点卡片 → 医生详情（头像大图 / 职称 / 专长 / 坐诊时间）  │
├──────────────────────────────────────────────────────────┤
│  ⚠️ 本系统仅提供就诊指引，不提供诊断与用药建议              │
└──────────────────────────────────────────────────────────┘
```

> 头像三条展示铁律：① 卡片位固定 72×72（1:1），`object-fit: cover`，**不因无图塌陷布局**；② 无照片或加载失败 → 姓名首字 + 科室主题色占位块（`onerror` 兜底，禁止破图图标）；③ 头像上**不叠任何文字**，防止与职称/专长文字混淆。

**C. 管理端（PC，三栏）**

```
┌────────────┬───────────────────────────────┬─────────────┐
│ 左：导航   │ 中：编辑区                     │ 右：操作栏   │
│            │                               │             │
│ · 科室地址 │  ## 心血管内科                │ 🎤 语音录入 │
│ · 医生信息 │  - 位置：门诊楼 3F 东侧        │ ─────────── │
│ · 手动排班 │  - 诊室：3F-12 ~ 3F-18        │ 数据源健康  │
│ · 数据源   │  - 服务范围：高血压、冠心病…   │ HIS ● 正常  │
│ · 审计日志 │                               │ 手动 ● 正常 │
│            │  [源码] [预览] [差异]          │ 冲突 2 条❗ │
└────────────┴───────────────────────────────┴─────────────┘

医生信息页（右侧操作栏切为「头像」面板）：
┌─────────────────────────────────────────────┐
│  ┌──────────┐  张明 · dept-cardio           │
│  │  头像     │  主任医师 · 冠心病介入          │
│  │  72×72   │  ─────────────────────────    │
│  └──────────┘  [ 选择图片 ]  [ 拍一张 ]      │ ← 支持相机（平板/一体机）
│  服务端已处理：480×480 + 192×192 · WebP      │ ← 回显服务端真实结果
│  ☑ 已取得该医生肖像使用授权（代传必勾）        │ ← admin_proxy 必填
│  [ 移除头像 ]                                │
└─────────────────────────────────────────────┘

医生自助页（手机打开，本期唯一入口=签名直链）：
┌─────────────────────────────┐
│  张明 医生 · 心血管内科       │  ← 身份来自 Principal，不由前端传
│  ┌───────────────────────┐  │
│  │   [ 当前头像预览 ]     │  │
│  └───────────────────────┘  │
│  [ 上传/更换我的照片 ]       │
│  ⓘ 本人上传即视为同意在院内   │ ← consent_source=doctor_self
│    导诊系统展示该照片         │
└─────────────────────────────┘

进入方式（本期）：
  管理端「医生信息」→ [ 生成照片链接 ] → 复制 / 短信 / 企业微信发给该医生
  https://host/doctor#t=<token>          ← token 放 fragment，不进日志与 Referer
        │
        │ 前端读 location.hash → POST /auth/exchange {token}
        ▼
  校验 HMAC 签名 + 未过期 + 未撤销 + 未消费 → Principal(role=doctor, sub=doctor_id)
        │
        ▼
  同一张上传页 · 同一套接口

（下期）OA 单点接入后，医生也可从 OA 工作台直接进入；入口变多，但
       落到同一个 Principal 与同一张上传页，业务代码不改。
```

语音录入后右侧抽屉弹出**确认浮层**（§7.2 详述）。

### 6.2 医生头像上传与展示模块（v3 重写）

**为什么 v3 把图像处理从浏览器挪回服务端**（一致性质问：上一版说前端处理更好，为什么改？）

上一版的结论建立在"主栈是 Node，引 `sharp` 有原生二进制风险"这个前提上。主栈换 Python 后，**Pillow 是必须装的依赖**，图像处理的边际成本降为 0；同时服务端处理还多拿三样东西：不依赖浏览器 `OffscreenCanvas` 支持（老版 kiosk Chromium 可能没有）、可保留源图存档、可用 `MAX_IMAGE_PIXELS` 防 decompression bomb。前提变了，结论就该变——这不是打脸，是前提驱动。

**上传处理链路**

| 环节 | 执行方 | 具体规格 | 失败处理 |
|---|---|---|---|
| 选择/拍摄 | 前端 | `<input type="file" accept="image/jpeg,image/png,image/webp" capture="environment">`，单文件 | — |
| 前端预压缩（可选） | 前端 | `createImageBitmap` + `OffscreenCanvas` 压到长边 ≤1600、WebP q0.85，**只为减体积** | 不支持则原图直传，不影响正确性 |
| 提交 | 前端 | `POST /api/doctors/{doctor_id}/photo`，`multipart/form-data`（`UploadFile`） | 失败保留本地 blob，允许重试 |
| 传输上限 | FastAPI | 先读 `Content-Length`，> 10MB 直接 `413`；`UploadFile` 走 SpooledTemporaryFile | `E_TOO_LARGE` |
| **格式嗅探** | `media/validate.py` | **读前 16 字节魔数**判真实类型（JPEG `FF D8 FF`／PNG `89 50 4E 47`／WebP `RIFF....WEBP`）；**SVG 直接拒**（可内嵌脚本） | `E_BAD_MAGIC` / `E_SVG_REJECTED` |
| **Pillow 校验** | `media/validate.py` | `Image.MAX_IMAGE_PIXELS = 40_000_000`；`Image.open(BytesIO).verify()` 验完整性；`im.format ∈ {JPEG,PNG,WEBP}`；宽高 ≥ 240 | `E_BAD_DIMENSION` / `E_DECODE_ERROR` |
| **处理** | `media/process.py` | 重新 `Image.open`（**`verify()` 后必须重开，Pillow 的硬性要求**）→ `ImageOps.exif_transpose()` 纠正方向 → `convert("RGB")` → `ImageOps.fit((480,480), LANCZOS, centering=(0.5,0.35))`（人像中心偏上）→ `save(format="WEBP", quality=86, method=6)`；同源再出 `192×192` | 处理异常 → `422`，不落盘 |
| **落盘** | `media/store.py` | 文件名 `{sha256[:24]}-480.webp` / `-192.webp`；路径 `data/media/doctors/{hash[:2]}/`；同 hash 已存在则**复用不重写**（天然去重 + 幂等）；写 `.tmp` → `os.fsync` → `os.replace` | `E_WRITE_FAILED` |
| **登记** | `state.py` | 写 `doctor_photo`：`doctor_id, content_hash, ext, width, height, bytes, source_sha256, uploaded_by, uploader_role, consent_source, uploaded_at, status`；同医生旧记录置 `superseded`（**不物理删**） | DB 失败 → 删除孤儿文件（best-effort） |
| **回读校验** | `media/store.py` | 重读落盘文件算 SHA-256，必须等于返回给前端的 `content_hash` 源 | 不符 → 回滚记录，`E_VERIFY_FAILED` |

**双角色上传口子（v3 新增，直接回应"提供个上传的口子"）**

| 角色 | 入口 | 授权 | `consent_source` | 限制 |
|---|---|---|---|---|
| `admin` | 管理端医生信息页 →「头像」面板 | JWT `role=admin` + `dept_scope` 含该医生科室 | `admin_proxy`（**必须勾选授权确认**，否则 422） | 可管本科室全部医生 |
| `doctor` | 医生自助轻量页（手机扫码/链接进入） | JWT `role=doctor` + `sub == doctor_id` | `doctor_self`（本人上传即授权） | **只能改自己**，越权返回 403 且不提示目标是否存在 |

**展示链路**

| 场景 | URL | 缓存 | 兜底 |
|---|---|---|---|
| 患者端医生卡（72×72 展示） | `GET /api/media/doctors/{hash}-192.webp` | `Cache-Control: public, max-age=31536000, immutable`（文件名即内容 hash，永不失效） | `onerror` → 首字占位块 |
| 医生详情（240 展示） | `GET /api/media/doctors/{hash}-480.webp` | 同上 | 同上 |
| 管理端列表（96 展示） | `-192.webp` + CSS 缩放 | 同上 | 同上 |
| 医生自助页预览 | `-480.webp` | 同上 | "暂无照片" |

**关键决策与理由**

| 决策 | 选择 | 理由 | 不这样会怎样 |
|---|---|---|---|
| 谁做图像处理 | **服务端 Pillow** | 已是主栈依赖，边际成本 0；不依赖浏览器能力；可防解压炸弹 | 前端处理 → 老 kiosk 不支持 `OffscreenCanvas` 时静默降级为原图直传，服务端仍得兜底 |
| 文件命名 | **内容 hash + 尺寸** | 天然去重、可永久缓存、可校验完整性；`-480/-192` 让响应式取图不需要额外参数 | 用 `{doctor_id}.jpg` 覆盖写 → 浏览器缓存不刷新，改完头像患者仍看旧图 |
| 旧照片 | 置 `superseded` 不删 | 可回滚、可审计；孤儿文件由每周任务按引用计数清 | 直接删 → 误删不可恢复，正在展示的页面立刻破图 |
| 是否要确认弹窗 | **不要** | 动作可见且可逆；滥弹确认会削弱 md 确认的严肃性 | 每步都弹 → 用户学会无脑点"确定"，md 那个确认就形同虚设 |
| 是否存进 md | **不存** | md 是文本真相源，二进制混进去会毁掉 diff 可读性（语音改 md 的核心体验） | base64 塞 md → diff 预览彻底不可读，语音修改功能失去意义 |
| 绑定关系 | `doctor_photo.doctor_id` 主键 | "这照片是谁"由 DB 主键决定，**不由文件名或上传者口述决定** | 靠文件名猜 → 张冠李戴（给患者看错人脸，医疗场景最不能犯的错） |
| 无照片时 | 首字占位，**绝不用通用医生剪影** | 剪影会被误认为真人照片 | 用剪影 → 患者以为那就是该医生本人 |
| 双尺寸 | 480 + 192 | kiosk 网格一屏 18 张卡，192 能显著降带宽；详情用 480 | 单张 480 全场景用 → kiosk 首屏加载变慢 |

**隐私与合规**

1. **肖像权落成字段**：`consent_source` 区分 `doctor_self`（本人上传）与 `admin_proxy`（代传，需勾选授权确认）。`uploaded_by` / `uploaded_at` / `uploader_role` 入 `photo_audit`，构成可追溯证据链。
2. **不可用于身份核验**：头像仅作视觉识别，界面不得出现"确认这位就是要找的医生"之类措辞。
3. **不采集患者图像**：本模块只存医生照片；患者端不上传任何图片。
4. **防批量抓取**：`/api/media/doctors/*` 允许患者端匿名读（导诊需要），但**只按 hash+尺寸精确匹配**，不提供目录列表接口；文件名正则 `^[0-9a-f]{24}-(480|192)\.webp$`，不匹配直接 404。
5. **离职处理**：医生停用即头像停用（`status='suspended'`），**不物理删**——留证需要。

---

## 7. 关键接口签名与交互流程

### 7.1 数据来源切换逻辑（核心，逻辑未变）

**四态策略**：

| mode | 语义 | 何时用 |
|---|---|---|
| `api_only` | 只用接口；不可用则**明确显示"排班暂不可用"** | 接口覆盖全且可靠的核心科室。**不做静默降级**——宁可说"暂无"，不给过期数据 |
| `manual_only` | 只用本地手动数据 | 接口未覆盖科室（体检中心、特需门诊） |
| `api_then_manual` | 接口优先，失败降级手动（带 `max_stale_ms`） | 默认策略 |
| `manual_override` | 手动数据强制置顶（带 `reason` 与 `expires_at`） | 临时调整、接口错误的人工裁决 |

**决策函数（`app/policy/resolver.py`）**

```python
async def resolve_schedule(date: str, scope: str) -> ResolvedSchedule:
    policy = settings.policy_for(scope)          # 科室级 > 全局 "*"
    api, manual = adapters["schedule_api"], adapters["schedule_manual"]

    if policy.mode is SourceMode.API_ONLY:
        r = await api.fetch(SourceQuery(date=date))
        if r.ok:
            return ResolvedSchedule.ok(r.rows, source="api", fetched_at=r.fetched_at)
        return ResolvedSchedule.empty("API_UNAVAILABLE", degraded=True)   # 不降级

    if policy.mode is SourceMode.MANUAL_ONLY:
        return _ok_from_manual(await manual.fetch(SourceQuery(date=date, scope=scope)))

    if policy.mode is SourceMode.MANUAL_OVERRIDE:
        if policy.expires_at and now_ms() > policy.expires_at:            # 过期自动失效
            log.warning("override_expired", extra={"scope": scope})
            return await resolve_schedule(date, "*")
        m = await manual.fetch(SourceQuery(date=date, scope=scope))
        if m.rows:
            return _ok_from_manual(m, overridden=True)
        r = await api.fetch(SourceQuery(date=date))                       # 手动无数据 → 回落
        return (ResolvedSchedule.ok(r.rows, source="api", fetched_at=r.fetched_at,
                                    degraded=True, reason="OVERRIDE_EMPTY")
                if r.ok else ResolvedSchedule.empty("OVERRIDE_EMPTY_AND_API_DOWN", degraded=True))

    # API_THEN_MANUAL（默认）
    if breaker.state is BreakerState.OPEN:                                # 熔断 → 直接手动
        return _ok_from_manual(await manual.fetch(SourceQuery(date=date)),
                               degraded=True, reason="BREAKER_OPEN")
    r = await api.fetch(SourceQuery(date=date, timeout_ms=policy.timeout_ms))
    if r.ok:
        breaker.record_success()
        return ResolvedSchedule.ok(r.rows, source="api", fetched_at=r.fetched_at)
    breaker.record_failure()                                              # 连续 3 次 → open 60s
    return _ok_from_manual(await manual.fetch(SourceQuery(date=date)),
                           degraded=True, reason=r.error_code or "API_FAILED")
```

**冲突处理（防"两套数据打架"）**

1. 两源都命中同一科室 → 按 policy 胜出方展示；
2. 差异写 `data_conflict`（双方 `value` / `fetched_at` / `winner`），管理端红点告警；
3. 人工裁决后写 `manual_override`（带 `reason` + `expires_at`），**冲突不自动 resolve**；
4. 手动数据支持 `valid_from/valid_to`，过期行自动失效而非一直显示。

**呈现层三条铁律**：① 每行必带 `source / fetched_at / stale`；② `degraded=True` 时界面黄条 + 语音回答**必须**声明"以下为人工维护数据，可能不是最新"；③ 来源徽标不可隐藏、不可配置关闭。

**接口签名**

```python
class SourceMode(StrEnum):
    API_ONLY = "api_only"
    MANUAL_ONLY = "manual_only"
    API_THEN_MANUAL = "api_then_manual"
    MANUAL_OVERRIDE = "manual_override"

class SourcePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    scope: str                      # department_id | "*"
    mode: SourceMode
    timeout_ms: int = 2000
    max_stale_ms: int = 21_600_000
    reason: str | None = None       # manual_override 必填
    expires_at: int | None = None   # manual_override 必填，epoch ms

class SourceQuery(BaseModel):
    date: str
    scope: str | None = None
    timeout_ms: int | None = None

class SourceResult(BaseModel):
    ok: bool
    rows: list[Any]
    fetched_at: int
    error_code: Literal["TIMEOUT","HTTP_5XX","PARSE_ERROR","NOT_CONFIGURED"] | None = None

class SourceMeta(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: Literal["api","manual"]
    fetched_at: int
    stale: bool
    overridden: bool = False

class ResolvedSchedule(BaseModel):
    rows: list[ScheduleRow]
    meta: SourceMeta
    degraded: bool = False
    reason: str | None = None
    conflicts: list[str] = Field(default_factory=list)   # 指向 data_conflict.id

    @classmethod
    def ok(cls, rows, source, fetched_at, degraded=False, reason=None, overridden=False) -> "ResolvedSchedule": ...
    @classmethod
    def empty(cls, reason: str, degraded: bool = True) -> "ResolvedSchedule": ...

class DataSourceAdapter(Protocol):
    id: Literal["schedule-api","schedule-manual","dept-md","doctor-api"]
    kind: Literal["schedule","department","doctor"]
    async def health(self) -> SourceHealth: ...
    async def fetch(self, q: SourceQuery) -> SourceResult: ...
```

### 7.2 语音修改 md 的三段式流程（核心，逻辑未变）

**为什么不能"说完就写"**：ASR 对"3F-12"这类门牌、"心内二病区"这类科室名的识别错误是常态。直写会污染全院导诊，而且**没人会发现**——系统会一本正经播报错误地址。所以必须把"识别"与"写入"拆开，中间插一段**人类可读的 diff**。

```
① 录音/转写        ② 提案（不落盘）              ③ 预览 + 确认           ④ 原子写 + 校验
──────────────────────────────────────────────────────────────────────────────────────
🎤 "心内科改到       LLM 只输出结构化提案：        ┌─ 确认浮层 ─────────┐   file_lock
 门诊楼四楼东侧"     {department_id:"dept-cardio", │ 语音原文：…         │   hash 乐观锁
                     field:"location",             │ 置信度：0.91 ✓      │   写 .tmp → fsync
                     new_value:"门诊楼 4F 东侧",   │ 原值：门诊楼3F东侧  │   os.replace 原子替换
ASR → 文本           confidence:0.91}              │ 新值：门诊楼4F东侧  │   md_version 快照
置信度 < 0.85        → 定位 section → 生成 diff    │ - 3F  + 4F         │   重读比对 hash
→ 强制人工编辑          → 返回 patch_id + diff      │ [取消] [确认修改]   │   verified=1
                                                   └────────────────────┘
```

**六道门禁**（`hooks.pre_tool_use` + `md/patch.py`）

| 门 | 判据 | 行为 |
|---|---|---|
| 置信度门 | `asr_confidence < 0.85` | 不生成提案，转写文本塞进输入框让人改 |
| 字段门 | `field not in FIELD_WHITELIST` | 拒绝（防语音改科室名/ID 破坏主键） |
| 定位门 | section 匹配到 ≠1 处 | 拒绝，提示"请手动指定科室" |
| 乐观锁门 | 当前 `content_hash != patch.base_hash` | 拒绝，提示"文件已被他人修改，请重新生成提案" |
| 确认门 | 无 `confirm_token` | `commit_md_patch` 直接拒绝 |
| 校验门 | 写入后重读 `content_hash != patch.after_hash` | 回滚 `md_version` 快照，`E_VERIFY_FAILED` |

**关键点：确认弹窗展示的是 diff，不是"是否保存"**。人要在弹窗里看到"3F → 4F"这一处差异，才有能力发现识别错误。只问"确认保存吗"等于没确认。

**接口签名**

```python
MdField = Literal["location", "rooms", "serviceScope", "phone"]
FIELD_WHITELIST: frozenset[str] = frozenset({"location", "rooms", "serviceScope", "phone"})

class MdPatchSource(BaseModel):
    channel: Literal["voice", "manual"]
    raw_utterance: str
    asr_confidence: float

class MdPatch(BaseModel):
    patch_id: str                    # uuid4().hex
    department_id: str               # 来自 front-matter，不由语音决定
    field: MdField
    before_value: str
    after_value: str
    diff: str                        # unified diff 文本，直接渲染
    base_hash: str                   # 提案时文件 content_hash
    after_hash: str                  # 应用后预期 hash
    source: MdPatchSource
    created_at: int
    status: Literal["proposed","committed","rejected","expired"] = "proposed"

class ConfirmToken(BaseModel):
    token: str                       # base64url(hmac_sha256(key, f"{patch_id}|{after_hash}|{exp}"))
    patch_id: str
    exp: int                         # now_ms + 300_000

class CommitReceipt(BaseModel):
    ok: bool
    patch_id: str
    version_id: str
    content_hash: str                # 重读后的实际 hash
    verified: bool                   # content_hash == patch.after_hash
    error_code: Literal["E_LOCK_TIMEOUT","E_STALE_BASE","E_VERIFY_FAILED","E_PATH_DENIED"] | None = None

async def propose_md_patch(args: ProposeMdArgs, ctx: AdminCtx) -> MdPatch: ...
def sign_confirm_token(patch: MdPatch) -> ConfirmToken: ...
async def commit_md_patch(args: CommitMdArgs, ctx: AdminCtx) -> CommitReceipt: ...
async def rollback(version_id: str, ctx: AdminCtx) -> CommitReceipt: ...
```

### 7.3 语音问答单轮流程

```
POST /api/voice/turn  {session_id, text?, audio_ref?, device_id}
  ↓
pre_invocation（限流 / 设备指纹）
  ↓
try_fast_path: 科室别名表 + 意图模板命中？──是──→ 直接查工具 → 渲染（不进 LLM，<300ms）
  │否
  ↓
intent.classify: LLM 槽位抽取 → Pydantic 校验（失败则带纠正重 prompt 1 次）
  ↓
槽位补全：指代继承（最近 3 轮内的 department_id/doctor_id）+ 歧义澄清（候选 top3 可点选）
  ↓
registry.invoke ×1..3（只读工具，3 次上限 + 打转指纹）
  ↓
render_answer：模板优先；LLM 仅句式润色（可关闭，润色后仍需过 grounding）
  ↓
evaluate.grounding_check ──不通过──→ 丢弃模型文本，用模板原文
  ↓
post_invocation（红线双路过滤）→ answer_record 落库 → 返回 {text, cards[], meta, disclaimer}
```

```python
class IntentKind(StrEnum):
    ASK_DEPT_LOCATION = "ASK_DEPT_LOCATION"
    ASK_DEPT_SERVICE = "ASK_DEPT_SERVICE"
    ASK_DEPT_CONTACT = "ASK_DEPT_CONTACT"
    ASK_DOCTOR_PROFILE = "ASK_DOCTOR_PROFILE"
    ASK_DOCTOR_SCHEDULE = "ASK_DOCTOR_SCHEDULE"
    ASK_TODAY_DUTY = "ASK_TODAY_DUTY"
    NAVIGATE = "NAVIGATE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    MEDICAL_REDLINE = "MEDICAL_REDLINE"

class Slots(BaseModel):
    department_id: str | None = None
    doctor_id: str | None = None
    date: str | None = None
    field: MdField | None = None
    raw_entities: list[RawEntity] = Field(default_factory=list)

class RawEntity(BaseModel):
    type: Literal["dept","doctor","date"]
    text: str
    confidence: float

class TurnInput(BaseModel):
    session_id: str
    text: str | None = None
    audio_ref: str | None = None
    device_id: str

class TurnContext(BaseModel):
    channel: Literal["patient_voice"] = "patient_voice"
    device_id: str
    now: int
    budget: Budget

class Budget(BaseModel):
    max_tool_calls: int = 3
    max_tokens: int = 3000
    deadline_ms: int                 # epoch ms

class AnswerCard(BaseModel):
    kind: Literal["dept","doctor"]
    title: str
    subtitle: str
    photo_url: str | None = None     # 医生卡才有
    actions: list[str] = Field(default_factory=list)   # ["带我去","看医生"]

class TurnOutput(BaseModel):
    answer_id: str
    text: str                        # 给 TTS
    display_text: str                # 给屏幕
    cards: list[AnswerCard] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    source_meta: SourceMeta | None = None
    degraded: bool = False
    disclaimer: str
    latency: LatencyBreakdown

class LatencyBreakdown(BaseModel):
    asr_ms: int = 0
    infer_ms: int = 0
    tool_ms: int = 0
    total_ms: int = 0

# 入口
async def handle_turn(input_: TurnInput, ctx: TurnContext) -> TurnOutput: ...
```

### 7.4 医生头像接口签名（v3 重写）

```python
# POST /api/doctors/{doctor_id}/photo      multipart/form-data, field="file"
#   Header: Authorization: Bearer {jwt}
#   依赖:   require_photo_writer → PhotoWriteGrant
async def upload_doctor_photo(
    doctor_id: str,
    file: UploadFile = File(..., description="JPEG/PNG/WebP，原始文件 ≤10MB"),
    consent_ack: bool = Form(False, description="admin 代传时必为 True"),
    ctx: AdminCtx | DoctorCtx = Depends(require_photo_writer),
) -> UploadResult: ...

class PhotoWriteGrant(BaseModel):
    subject: str                     # admin 的 sub 或 doctor_id
    role: Literal["admin", "doctor"]
    target_doctor_id: str
    consent_source: Literal["doctor_self", "admin_proxy"]

class UploadResult(BaseModel):
    ok: bool
    photo: DoctorPhotoRef | None = None
    deduped: bool = False            # True = 同 hash 已存在，未重写
    error_code: Literal[
        "E_TOO_LARGE","E_BAD_MAGIC","E_SVG_REJECTED","E_BAD_DIMENSION",
        "E_DECODE_ERROR","E_CONSENT_REQUIRED","E_FORBIDDEN","E_VERIFY_FAILED","E_WRITE_FAILED"
    ] | None = None

class DoctorPhotoRef(BaseModel):
    url_480: str                     # "/api/media/doctors/{hash}-480.webp"
    url_192: str
    width: int
    height: int
    bytes_480: int
    updated_at: int

class DoctorPhotoRecord(BaseModel):
    doctor_id: str
    content_hash: str
    ext: Literal["webp"] = "webp"
    width: int
    height: int
    bytes: int
    source_sha256: str
    uploaded_by: str
    uploader_role: Literal["admin", "doctor"]
    consent_source: Literal["doctor_self", "admin_proxy"]
    uploaded_at: int
    status: Literal["active", "superseded", "suspended"]

# 校验与处理（media/validate.py · media/process.py · media/store.py）
def sniff_image_type(head: bytes) -> Literal["jpeg","png","webp","svg","unknown"]: ...
def assert_photo_policy(raw: bytes, declared_len: int) -> ValidationReport: ...
def process_photo(raw: bytes) -> ProcessedPhoto: ...     # 同步、CPU 密集 → 调用方套 asyncio.to_thread
async def store_photo(doctor_id: str, proc: ProcessedPhoto, grant: PhotoWriteGrant) -> DoctorPhotoRecord: ...
async def remove_photo(doctor_id: str, ctx: AdminCtx | DoctorCtx) -> UploadResult: ...

class ValidationReport(BaseModel):
    pass_: bool = Field(alias="pass")
    type: Literal["jpeg","png","webp"]
    width: int
    height: int
    bytes: int
    errors: list[str] = Field(default_factory=list)

class ProcessedPhoto(BaseModel):
    big: bytes                       # 480×480 WebP
    small: bytes                     # 192×192 WebP
    content_hash: str                # sha256(big) 前 24 位
    width: int = 480
    height: int = 480
```

**出错行为（可编码规定）**：`E_BAD_MAGIC` / `E_SVG_REJECTED` / `E_BAD_DIMENSION` / `E_DECODE_ERROR` → `422`，**不落盘、不动 DB**；`E_CONSENT_REQUIRED` → `422` 且提示勾选授权；`E_FORBIDDEN` → `403` 且**不提示目标医生是否存在**（防探测）；`E_VERIFY_FAILED` → 删除孤儿文件 + 回滚记录；`E_TOO_LARGE` → `413`。

### 7.5 认证与入口设计（定稿：两入口；OA 单点只留契约）

**核心原则：入口可增，身份只有一个。** 本期只有两个入口（签名直链、管理端密码），但**业务代码只认 `Principal`**；`photo.py` 不知道医生是从链接来的还是管理员用了密码。OA 单点下期接入时，只多一个入口，主服务不动。

```
 ① 医生：短信/企业微信收到链接            ② 管理端：账号密码
    https://host/doctor#t=<token>            POST /auth/password
      → 前端读 location.hash                   → argon2id 校验
      → POST /auth/exchange {token}            → 失败 5 次锁 5 分钟
      → 校验 HMAC + jti + 未过期/未撤销/未消费      │
           │                                      │
           └───────────────┬──────────────────────┘
                           ▼
                  staff 表查身份 → role / dept_scope
                           ▼
                  Principal（内部 JWT，HS256，30min）
                           ▼
                  所有业务接口只验这一个

（下期插入 OA 单点：GET /auth/oa/start → 回调 → SsoProvider.exchange → 同一个 Principal）
```

**入口一：签名直链（`auth/magic_link.py`）** — 本期医生侧唯一入口

设计目标：**不要求医生登录，但链接受控**。

| 设计点 | 选择 | 理由 |
|---|---|---|
| token 放哪 | **URL fragment（`#t=...`）** | fragment 不随请求发到服务端 → **不进 Nginx access log、不进 Referer**；前端读出后放 `Authorization` 头 |
| 签名算法 | `HMAC-SHA256(magic_link_secret, f"{doctor_id}\|{jti}\|{exp}")` → base64url | 只验真伪、不加密；payload 不含敏感信息，无需加密 |
| `jti` | 32 字节 `secrets.token_urlsafe(32)` | 防猜、可撤销、可审计 |
| 有效期 | **默认 72 小时** | 医生不一定当天看手机；太短会逼管理员反复重发 |
| 一次性？ | **不是一次性，但"上传成功后即失效"** | 纯一次性会让"打开看看再决定"变死路；上传成功即 `consumed_at`，防"链接被转发给别人代传" |
| 防转发 | 有效期 + 上传后失效 + `auth_audit` 记 IP 段与 UA 哈希 | 绑设备指纹会误伤换手机/换网络的正常场景，不做 |
| 撤销 | `magic_link.revoked_at`，管理端可按医生批量撤销 | 离职、误发时必须能立刻止血 |

```python
class MagicLinkClaims(BaseModel):
    model_config = ConfigDict(frozen=True)
    doctor_id: str
    jti: str
    exp: int                                          # epoch s

def issue_magic_link(doctor_id: str, issued_by: str, ttl_hours: int = 72) -> MagicLinkToken: ...
async def verify_magic_link(token: str) -> Principal: ...     # 签名 + 未过期 + 未撤销 + 未消费
async def consume_magic_link(jti: str) -> None: ...           # 上传成功后调用，写 consumed_at
async def revoke_magic_links(doctor_id: str, actor: str) -> int: ...
```

**入口二：管理端账号密码（`auth/password.py`）** — 只给导医台与管理员

密码哈希 **`argon2id`**（`argon2-cffi` 的 `PasswordHasher`，参数取默认：`time_cost=3, memory_cost=65536, parallelism=4`）；校验用 `PasswordHasher.verify()`，它内部已是常数时间比较。初始口令由 `setup.bat` 交互式设置，**不写进任何配置文件**。登录失败 5 次锁 5 分钟，锁状态写 `auth_audit`。

**身份映射纪律**：`staff` 表查 `employee_no`（或 admin 账号）→ 拿 `role` 与 `dept_scope`。**查不到就拒绝**（403 + "请联系管理员开通"），**绝不自动创建账号**。

**下期扩展位：OA 单点**（`auth/provider.py` + `providers/not_configured.py`）

本期只留契约，任何调用返回 501：

```python
class SsoProvider(Protocol):
    name: str
    def build_authorize_url(self, state: str, nonce: str) -> str: ...
    async def exchange(self, code: str, state: str) -> SsoIdentity: ...

class SsoIdentity(BaseModel):
    employee_no: str                 # 工号，唯一映射键
    display_name: str
    raw: dict[str, Any] = Field(default_factory=dict)   # 原始断言，仅审计留痕，不进业务

# 本期唯一实现
class NotConfiguredProvider:
    name = "none"
    def build_authorize_url(self, state: str, nonce: str) -> str:
        raise HTTPException(501, "OA 单点未配置（下期接入）")
    async def exchange(self, code: str, state: str) -> SsoIdentity:
        raise HTTPException(501, "OA 单点未配置（下期接入）")
```

> **为什么留契约而不是彻底删掉**：删了以后接 OA 要动 `principal.py`（加 `entry` 枚举）、`deps.py`（加路由）、`staff.py`（改映射键），三处都要改且要重测。留一个 Protocol + 一个 501 实现，下期只加一个文件。**成本差 10 倍，而留契约的成本只是一个 20 行的类。**

**为什么不请求中携带 session cookie 而用 Bearer**：kiosk 与医生手机页可能跨域嵌入，cookie 需要 CSRF 防护与 SameSite 调优；Bearer 在 `Authorization` 头里，天然免疫 CSRF。代价是要防止 XSS 偷取——所以医生页不渲染任何用户输入内容。

**限流**：`/auth/exchange` 30 次/小时/IP；`/auth/password` 5 次/5 分钟/账号；`/api/admin/magic-links` 60 次/小时/管理员。

---

## 8. 核心数据结构

### 8.1 状态结构（SQLite 表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `session` | `id TEXT PK, device_id TEXT, created_at INT, expires_at INT, last_intent TEXT, last_dept_id TEXT, last_doctor_id TEXT` | 30 分钟过期；只存指代用实体 id，不存 PII |
| `message` | `id TEXT PK, session_id TEXT, role TEXT, content TEXT, created_at INT` | 滑动窗口来源 |
| `query_log` | `id TEXT PK, session_id TEXT, raw_text TEXT, intent TEXT, slots_json TEXT, path TEXT, latency_json TEXT, budget_json TEXT` | 调参与延迟分析 |
| `answer_record` | `id TEXT PK, query_id TEXT, answer_text TEXT, tool_calls_json TEXT, grounded_fields_json TEXT, grounding_pass INT, source_version TEXT, model_version TEXT, prompt_version TEXT` | V 层证据；`source_version` = md content_hash |
| `md_version` | `id TEXT PK, file TEXT, content_hash TEXT, content TEXT, author TEXT, channel TEXT, asr_confidence REAL, confirmed_by TEXT, raw_utterance TEXT, created_at INT, verified INT` | 每次写入完整快照，可回滚 |
| `md_patch` | `patch_id TEXT PK, department_id TEXT, field TEXT, before_value TEXT, after_value TEXT, diff TEXT, base_hash TEXT, after_hash TEXT, asr_confidence REAL, raw_utterance TEXT, status TEXT, created_at INT, committed_at INT` | 提案与幂等键来源 |
| `source_health` | `source_id TEXT, checked_at INT, ok INT, latency_ms INT, error_code TEXT, PRIMARY KEY(source_id, checked_at)` | 健康度时序 |
| `data_conflict` | `id TEXT PK, date TEXT, department_id TEXT, api_value TEXT, manual_value TEXT, winner TEXT, resolved_by TEXT, resolved_at INT` | 冲突不自动 resolve |
| `badcase` | `id TEXT PK, query_id TEXT, reason TEXT, expected TEXT, actual TEXT, created_at INT, fixed INT` | 回归集回流来源 |
| `doctor_photo` | `doctor_id TEXT, content_hash TEXT, ext TEXT, width INT, height INT, bytes INT, source_sha256 TEXT, uploaded_by TEXT, uploader_role TEXT, consent_source TEXT, uploaded_at INT, status TEXT('active'\|'superseded'\|'suspended'), PRIMARY KEY(doctor_id, content_hash)` | ★ 头像元数据；旧图置 `superseded` 不物理删 |
| `photo_audit` | `id TEXT PK, doctor_id TEXT, action TEXT('upload'\|'replace'\|'remove'), content_hash TEXT, actor TEXT, actor_role TEXT, consent_source TEXT, ip_hash TEXT, created_at INT` | ★ 肖像授权证据链，只追加不修改 |
| `staff` | `employee_no TEXT PK, role TEXT('admin'\|'doctor'), doctor_id TEXT NULL, dept_scope_json TEXT, display_name TEXT, active INT, password_hash TEXT NULL, created_at INT` | 账号/工号 → 角色映射。**查不到即拒绝，绝不自动建号**；`password_hash` 仅 admin 行有值（argon2id） |
| `magic_link` | `jti TEXT PK, doctor_id TEXT, issued_by TEXT, created_at INT, expires_at INT, consumed_at INT NULL, revoked_at INT NULL, first_ip_hash TEXT` | 签名直链。上传成功后写 `consumed_at`；撤销写 `revoked_at` |
| `auth_audit` | `id TEXT PK, event TEXT('login_ok'\|'login_fail'\|'link_issue'\|'link_use'\|'link_revoke'\|'lock'), principal_sub TEXT, entry TEXT, ip_hash TEXT, ua_hash TEXT, created_at INT` | 只追加；登录锁定状态也记于此。`event='sso_ok'/'sso_fail'` 留到下期 |
| `sso_state` | `state TEXT PK, nonce TEXT, provider TEXT, redirect_uri TEXT, created_at INT, expires_at INT` | **下期启用**（OA 单点的 CSRF/重放防护）。本期建表不写，避免下期改 schema |

**索引（必须建，否则 kiosk 首屏与登录会慢）**：`idx_message_session(session_id, created_at)`、`idx_answer_query(query_id)`、`idx_photo_doctor_status(doctor_id, status)`、`idx_conflict_date(date)`、`idx_magic_link_doctor(doctor_id, expires_at)`、`idx_auth_audit_event(event, created_at)`。

### 8.2 消息 / 任务结构

```python
class ScheduleRow(BaseModel):
    department_id: str
    department_name: str
    campus_id: str
    location: str                                    # "门诊楼3F 东侧"
    doctor_count: int
    doctors_preview: list[DoctorBrief] = Field(default_factory=list)   # 最多 3 位
    status: Literal["on_duty", "suspended"]
    source: Literal["api", "manual"]
    fetched_at: int
    stale: bool

class DoctorBrief(BaseModel):
    doctor_id: str
    name: str
    title: DoctorTitle
    photo_url: str | None = None

DoctorTitle = Literal["主任医师", "副主任医师", "主治医师", "住院医师"]

class DeptDetail(BaseModel):
    department_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    location: str
    rooms: str
    service_scope: str
    phone: str | None = None
    content_hash: str
    updated_at: int

class DoctorProfile(BaseModel):
    doctor_id: str
    name: str
    title: DoctorTitle
    specialty: str
    department_id: str
    photo: DoctorPhotoRef | None = None              # None = 无照片，前端走首字占位
    schedule: list[DoctorSlot] = Field(default_factory=list)

class DoctorSlot(BaseModel):
    date: str
    period: Literal["上午", "下午", "夜间"]
    room: str
    quota_left: int | None = None

class Candidate(BaseModel):
    id: str
    name: str
    score: float
    kind: Literal["dept", "doctor"]

class ToolResult(BaseModel):
    status: Literal["ok", "not_found", "unavailable", "denied"]
    data: Any | None = None
    source: SourceMeta | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    reason: str | None = None
    degraded_to: Literal["manual"] | None = None

class ToolCallRecord(BaseModel):
    tool_name: str
    args_hash: str
    status: str
    started_at: int
    duration_ms: int
    error_code: str | None = None

class GroundingReport(BaseModel):
    pass_: bool = Field(alias="pass")
    checked_fields: list[FieldCheck] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

class FieldCheck(BaseModel):
    field: str
    claimed: str
    actual: str
    match: bool

class HookResult(BaseModel):
    allow: bool
    reason: str | None = None
    mutation: dict[str, Any] | None = None            # 允许钩子改写 payload（如脱敏）

AuthEntry = Literal["magic_link", "password"]           # 下期增加 "oa_sso"

class Principal(BaseModel):
    """内部唯一身份。业务代码只认它，不认外部凭证形态。"""
    model_config = ConfigDict(frozen=True)
    sub: str                                          # admin 账号 id 或 doctor_id
    role: Literal["admin", "doctor"]
    dept_scope: list[str] = Field(default_factory=list)   # admin 可管科室；doctor 为自己的科室
    display_name: str | None = None
    entry: AuthEntry                                  # 从哪个入口进来的，仅审计与降级用
    issued_at: int
    exp: int                                          # epoch s，30 分钟

class MagicLinkToken(BaseModel):
    model_config = ConfigDict(frozen=True)
    token: str                                        # base64url(hmac_sha256)
    claims: MagicLinkClaims
    url: str                                          # "https://host/doctor#t={token}"
```

### 8.3 配置结构

```python
class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GUIDE_", env_file=".env")
    port: int = 8787
    llm_enabled: bool = True
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "qwen2.5:7b"
    llm_timeout_ms: int = 4000
    llm_key: SecretStr = SecretStr("")               # 只从环境变量读，不进日志
    asr_provider: Literal["webspeech", "faster_whisper", "cloud"] = "webspeech"
    asr_min_confidence: float = 0.85
    tts_provider: Literal["webspeech", "edge_tts"] = "webspeech"
    limit_per_device_per_min: int = 20
    turn_timeout_ms: int = 6000
    max_tool_calls: int = 3
    photo_max_bytes: int = 10 * 1024 * 1024
    photo_size: int = 480
    photo_thumb: int = 192
    pii_salt: SecretStr = SecretStr("")
    departments_md: Path = Path("data/departments.md")
    manual_schedule_md: Path = Path("data/schedule/manual.md")
    db_file: Path = Path("data/guide.db")
    media_dir: Path = Path("data/media/doctors")
    disclaimer_text: str = "本系统仅提供就诊指引，不提供诊断与用药建议。"
    # ── 认证（本期两入口） ──
    jwt_secret: SecretStr = SecretStr("")             # HS256 签名密钥，必填
    magic_link_secret: SecretStr = SecretStr("")      # 直链 HMAC 密钥，必填，且与 jwt_secret 不同
    magic_link_ttl_hours: int = 72
    public_base_url: str = "http://127.0.0.1:8787"    # 生成直链用
    admin_max_fail: int = 5
    admin_lock_minutes: int = 5
    # ── 下期（OA 单点）：sso_provider / sso_base_url / sso_client_id / sso_client_secret / sso_redirect_uri ──
```

---

## 9. 配置文件格式

### 9.1 `config/sources.json`

```json
{
  "default": { "mode": "api_then_manual", "timeout_ms": 2000, "max_stale_ms": 21600000 },
  "breaker": { "failure_threshold": 3, "open_ms": 60000 },
  "overrides": [
    { "scope": "dept-cardio", "mode": "api_only" },
    { "scope": "dept-checkup", "mode": "manual_only" },
    {
      "scope": "dept-ortho",
      "mode": "manual_override",
      "reason": "HIS 10/20 排班导入错误，人工更正",
      "expires_at": 1761062400000
    }
  ],
  "endpoints": {
    "schedule_api": { "base_url": "http://his.internal/api/v1/schedule", "auth_env": "GUIDE_HIS_APP_SECRET" },
    "doctor_api":   { "base_url": "http://his.internal/api/v1/doctor",   "auth_env": "GUIDE_HIS_APP_SECRET" }
  }
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `default.mode` | `SourceMode` | 全局默认策略（科室级 `overrides` 优先） |
| `default.timeout_ms` | int | 接口超时，超时即判定失败并进入降级 |
| `default.max_stale_ms` | int | 超过即标 `stale`，界面变灰提示 |
| `breaker.failure_threshold` | int | 连续失败几次打开熔断 |
| `breaker.open_ms` | int | 熔断持续时长，期间直接走手动源 |
| `overrides[].scope` | str | `department_id` 或 `"*"` |
| `overrides[].reason` | str | `manual_override` 必填，进审计 |
| `overrides[].expires_at` | int | `manual_override` 必填，epoch ms，过期自动失效 |
| `endpoints.*.auth_env` | str | **只写环境变量名，禁止写明文密钥** |

### 9.2 `data/departments.md`（内容真相源）

```markdown
---
schema: dept-address/v1
version: 7
updated_at: 2026-10-20T14:02:00+08:00
campus: 本部
---

## 心血管内科
<!-- id: dept-cardio; aliases: 心内科,心内 -->
- 位置：门诊楼 3F 东侧
- 诊室：3F-12 ~ 3F-18
- 服务范围：高血压、冠心病、心律失常、心力衰竭
- 电话：8801

## 骨科
<!-- id: dept-ortho; aliases: 骨外科 -->
- 位置：门诊楼 2F 南侧
```

| 约定 | 说明 |
|---|---|
| `<!-- id: ... -->` | **科室主键，语音永不修改**；语音提案只能改四个字段的值 |
| 四个可改字段 | `位置 / 诊室 / 服务范围 / 电话`（常量 `FIELD_WHITELIST`，`MdField` 为唯一枚举） |
| front-matter `version` | 每次提交 +1，与 `md_version` 交叉校验 |
| 单文件 | 人工维护直观、diff 可读；并发靠 `file_lock` + `content_hash` 乐观锁 |

### 9.3 `config/auth.json`（本期：两入口）

```json
{
  "magic_link": { "ttl_hours": 72, "secret_env": "GUIDE_MAGIC_LINK_SECRET" },
  "jwt": { "secret_env": "GUIDE_JWT_SECRET", "ttl_minutes": 30 },
  "password": { "max_fail": 5, "lock_minutes": 5 },
  "public_base_url": "https://guide.hospital.internal",
  "sso": { "provider": "none" }
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `magic_link.ttl_hours` | int | 直链有效期，默认 72 |
| `magic_link.secret_env` / `jwt.secret_env` | str | **只写环境变量名，禁止写明文密钥**；且两者必须是**不同**的环境变量 |
| `jwt.ttl_minutes` | int | Principal 有效期，默认 30 |
| `password.max_fail` / `lock_minutes` | int | 登录失败锁定阈值与时长 |
| `public_base_url` | str | 生成直链用；必须是对医生手机可达的院内地址 |
| `sso.provider` | `"none"` | **本期固定 `none`**，加载 `NotConfiguredProvider`；下期改 `cas`/`oauth2`/`oidc` 并补对应配置块 |

**密钥纪律（三条，缺一即视为违规）**：① `jwt_secret` 与 `magic_link_secret` **必须是两个不同密钥**（否则拿到直链密钥就能伪造管理员 JWT）；② 只从环境变量读，`SecretStr` 保证 `print(settings)` 不泄露；③ 日志 `Redactor` 对 `secret|token|key|password|authorization|ticket` 一律打码。

---

## 10. 层间交叉检查

| 关系 | 说明 |
|---|---|
| E ↔ V | 循环终止由 `grounding_check` 决定，不由模型自评；V 不通过时 E 降级到模板而非重试（重试只会产生另一个幻觉） |
| T ↔ L | 工具授权完全由 `pre_tool_use` 承担；`registry` 自身**不含**权限判断，避免两处授权判定不一致 |
| T ↔ S | 每次 `invoke` 落 `ToolCallRecord`；写工具额外落 `md_patch` / `photo_audit`；日志与审计同源 |
| C ↔ S | 上下文每轮从 S 组装，S 是唯一真相；`last_dept_id` 由 S 提供，C 不缓存 |
| C ↔ L | 外部内容消毒在 C 层（技术手段），红线段在 L 层（策略手段）；不可合并，否则改一处破坏另一处 |
| S ↔ L | md 写入幂等键由 S 生成（`patch_id`），执行许可由 L 签发（`confirm_token`）；**S 不能自己给自己发许可**。头像同理：`content_hash` 由 S/`store` 算，`PhotoWriteGrant` 由 L 发 |
| V ↔ C | 评估器用独立上下文，禁止复用生成侧 messages（否则等于模型自己判卷） |
| P ↔ E | 数据源与 `analysis/` 插件化要求 E 层只依赖 `Protocol`，禁止在 `loop.py` 里出现具体数据源名 |
| **异步 ↔ CPU** | Pillow 编码、`faster_whisper` 推理必须走 `asyncio.to_thread`，否则阻塞事件循环（Python 栈特有条目，Node 版无此项） |
| **L ↔ auth** | 认证在 L 层**之前**完成（`Depends` 阶段就产出 `Principal`），但**授权在 L 层**（`pre_tool_use` 判通道+工具+参数）。认证说"你是谁"，授权说"你能做这件事"，两者不可混在一处判断 |
| **auth ↔ S** | `sso_state` / `magic_link` / `auth_audit` 归 S 存，但**签发与校验归 auth 模块**；S 只提供读写，不参与密码学判断（防止"数据层顺便管签名"导致审计与实现耦合） |
| **auth ↔ T** | `PhotoWriteGrant` 由 `Principal` 派生（`role` + `sub` + `consent_source`），工具层不自己判断"这个医生是不是本人" |

---

## 11. 六层决策表

| 层 | 决策 | 依据 | 状态 |
|---|---|---|---|
| E 执行循环 | 分类与执行 + 双通道；3 次工具上限、6s 超时、指纹打转检测；CPU 密集走 `to_thread` | 导诊低自由度；`async` 栈必须隔离阻塞调用 | 默认假设 |
| T 工具注册 | 8 个手写工具 + Pydantic 入参；注册/披露/授权三分离；写工具仅 admin/doctor 通道 | 患者通道绝不能碰写能力 | 默认假设 |
| C 上下文 | 8000 窗口四档预算（15/25/40/20）；每轮重组；`untrusted_data` 包裹 + 剥离注入标记 | md 是唯一可被运营改写的注入面 | 默认假设 |
| S 状态存储 | sqlite3 单写者 WAL；md 为真相源、DB 存快照与审计；`patch_id` 幂等 + 重读校验；头像按内容 hash 落盘 | 会话恢复不能证明写入成功 | 默认假设 |
| L 生命周期钩子 | 4 类钩子硬编码调用；HITL 四档；红线双路（规则 + 独立分类器）；**两入口收敛为 `Principal`**（签名直链 / 密码），OA 单点留 `SsoProvider` 契约 | 写 md 必须「执行前确认」；头像靠校验+审计而非确认；认证≠授权必须分离 | 已定 |
| V 评估接口 | grounding 硬门禁 + 5 分钟一致性校验 + 120 条回归集 + 头像 8 类畸形输入单测；评估器独立上下文、4 维评分、2 轮预算 | 医疗场景不允许"大概对" | 默认假设 |

---

## 12. 生产级必答项

| # | 必答项 | 结论 |
|---|---|---|
| 1 | 执行环境：工具在哪跑、凭证是否进执行环境 | 全在本进程内跑，无沙箱（单机内网）。写路径由 `assert_writable_path()` 硬限定为四处：`data/departments.md`、`data/schedule/manual.md`、`data/guide.db`、`data/media/doctors/{hash}-{480\|192}.webp`（后者文件名用正则卡，拒绝任何路径分隔符）。**凭证（HIS `app_secret`、`jwt_secret`、`magic_link_secret`）只存在于 `settings` 与各适配器闭包，不进工具入参、不进上下文、不进日志**；`SecretStr` 保证 `print(settings)` 不泄露。`Redactor` 对 `secret\|token\|key\|password\|authorization\|ticket` 打码。 |
| 2 | 错误回喂：如何作为观察回上下文、如何分类恢复 | 四类（瞬时/模型/永久/资源）分级（见 §4.1 表）。错误一律转结构化 `ToolResult`，**禁止在循环里吞异常**；`handle_turn` 顶层兜底转 `TurnOutput`，患者端不露堆栈。 |
| 3 | 打转检测与熔断 | 指纹 = `hash(intent + 归一化槽位)`；连续 2 次同结果/同失败 → 打断转兜底 + `loop_guard` 事件。数据源熔断 3 次/60s；LLM 单次 4s 超时、0 次自动重试（重试放大时延，改为直接降级模板）。 |
| 4 | 记忆治理：可信度、归属、失效条件 | 患者记忆仅会话内（30 分钟），不存 PII，日志 HMAC 脱敏。md 修改必带 `author/channel/asr_confidence/confirmed_by/raw_utterance`，**未确认的语音转写永不入库**。头像必带 `consent_source` + `photo_audit`。md 变更后指向旧 `content_hash` 的答案准确性结论自动失效。 |
| 5 | 外部副作用幂等 | 两类都幂等。**写 md**：`patch_id` + `status='committed'` 短路 + 重读比对 `content_hash`。**写头像**：内容 hash 即幂等键，同 hash 复用不重写 + 重读算 SHA-256 比对。两者都**不信任写入返回值**。 |
| 6 | 证据版本绑定 | `answer_record.source_version` 存 md `content_hash` + 排班快照 id；回归报告带 `{model_version, prompt_version, git_commit}`。任一变更 → 旧结论标 `stale` 并进重跑队列。 |
| 7 | 生成者与评估者分离 | 评估器只接收 `(问句, 工具结果, 回答文本)`，看不到思维链；4 维 0–2 分显式标准；迭代预算 2 轮，超限转人工。grounding 是程序化确定性校验，天然与生成分离。 |

---

## 13. 参考架构图

```
                        ┌─────────────────────────── 患者端 kiosk / H5 ───────────────────────────┐
                        │  状态条(来源+时间)  今日排班网格(带来源徽标)  语音按钮  对话抽屉   │
                        └───────────────────────┬─────────────────────────────────────────────────┘
                                                │ POST /api/voice/turn · GET /api/schedule/today
┌───────────────────────────────────┐           │
│ 认证入口（本期两入口，一身份）      │           │
│  ① 签名直链 /auth/exchange         │           │
│     HMAC + jti + 72h + 上传后失效   │           │
│  ② 管理端 /auth/password argon2id  │           │
│  staff 查身份 → role/dept_scope    │           │
│  查不到即拒绝，绝不自动建号         │           │
│  ─ 下期：OA 单点 → 只加一个实现文件 │           │
└───────────────┬───────────────────┘           │
                │ Principal                     │
                └───────────────┬───────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                  app/main.py (FastAPI + uvicorn)                                  │
│  ┌──────────────────────────── L 生命周期钩子 ────────────────────────────┐                      │
│  │ pre_invocation(限流/鉴权)  pre_tool_use(授权/token)  post_tool_use(脱敏)  post_invocation(红线)│
│  └──────────────────────────────────┬──────────────────────────────────────┘                      │
│  ┌──────────── E 执行循环 ───────────▼─────────────────────────────────────┐                      │
│  │  try_fast_path(规则直答 <300ms)  ──命中──┐                              │                      │
│  │        │未命中                          ├──→ render_answer(模板优先) ─┐  │                      │
│  │  intent 槽位抽取(LLM) → 指代补全 ───────┘                            │  │                      │
│  │  打转指纹 / 3 次工具上限 / asyncio.timeout(6s) / to_thread 隔离 CPU      │                      │
│  └──────────────────────────────────┬──────────────── V grounding 硬门禁 ──┘                      │
│                                     │                                                             │
│  ┌──── T 工具注册(注册/披露/授权) ────▼────┐   ┌──── C 上下文(四档预算/消毒) ────┐                │
│  │ ①list_on_duty ②get_department          │   │ build_context() 每轮重组         │                │
│  │ ③search_departments ④get_doctor        │◄──┤ sanitize_external(untrusted)     │                │
│  │ ⑤get_doctor_schedule ⑥get_doctor_photo │   └──────────────┬──────────────────┘                │
│  │ ⑦propose_md_patch ⑧commit_md_patch     │                  │                                   │
│  └──────────────┬─────────────────────────┘                  │                                   │
│                 │                                             │                                   │
│  ┌──────────────▼── policy/resolver 数据来源切换 ──┐  ┌───────▼──── S 状态存储 ────────────┐    │
│  │ api_only │ manual_only │ api_then_manual │     │  │ guide.db: session/message/          │    │
│  │ manual_override   + CircuitBreaker + 冲突记录    │  │ query_log/answer_record/md_version/ │    │
│  └────┬──────────────────────┬────────────────────┘  │ md_patch/source_health/             │    │
│       │                      │                        │ data_conflict/badcase/doctor_photo/  │    │
│  ┌────▼─────────┐    ┌───────▼──────────┐            │ photo_audit                         │    │
│  │ schedule_api │    │ schedule_manual  │            └─────────────────────────────────────┘    │
│  │ (HIS HTTP)   │    │ (manual.md)      │                                                        │
│  │ breaker+缓存 │    │ 表格解析         │      ┌──────────── V 评估 ────────────┐                │
│  └──────────────┘    └──────────────────┘      │ grounding_check / consistency  │                │
│  ┌──────────────┐    ┌──────────────────┐      │ 回归集 120 条 / 独立上下文      │                │
│  │ dept_md      │    │ doctor_api       │      └────────────────────────────────┘                │
│  │ departments.md│   │ (HIS HTTP)       │                                                        │
│  └──────┬───────┘    └──────────────────┘      ┌──── 管理端 admin_web (JWT) ────┐                │
│         │                                      │ md 双栏 + 语音录入 + diff 确认  │                │
│         │                                      │ 手动排班 + 来源健康 + 冲突裁决  │                │
│         │                                      │ 医生照片上传(admin_proxy)       │                │
│         │                                      └──────────┬─────────────────────┘                │
│         │                                      ┌──────────┴──────────┐                          │
│         │                                      │ 医生自助页 (doctor)  │                          │
│         │                                      │ 仅头像上传          │                          │
│         │                                      │ doctor_self         │                          │
│         │                                      └──────────┬──────────┘                          │
│         │                                                 │ POST /api/doctors/{id}/photo        │
│         │                                      ┌──────────▼──── media 头像 ─────────┐           │
│         │                                      │ validate 魔数/MAX_IMAGE_PIXELS/拒SVG │           │
│         │                                      │ process exif_transpose+fit 480/192   │           │
│         │                                      │ store   sha256 命名·原子写·去重      │           │
│         ▼                                      │ serve   immutable·仅 hash 精确匹配   │           │
│  data/departments.md ◄── md/patch 原子写 ──►   └──────────┬─────────────────────────┘           │
│  （file_lock + hash 乐观锁 + 快照回滚）                     ▼                                     │
│                                            data/media/doctors/{hash}-{480,192}.webp             │
│                                            doctor_photo 存引用 + photo_audit 存授权证据          │
│                                                                                                  │
│  ┌──── 路线图插件（v3 仅 Protocol 占位，返回 501）────┐                                          │
│  │ analysis/report_ocr.py   报告解读（红线：不出结论） │                                          │
│  │ analysis/stats.py        就诊数据分析               │                                          │
│  └────────────────────────────────────────────────────┘                                          │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 14. 遗留问题与未知区

> 本版把 v4 的 9 项未知收敛为 **2 项阻塞 + 6 项已定稿**。原则：**能由架构师定的一律定掉**，只把真正需要外部输入的留成未知——否则"未知区"会变成拖延的借口。

**仅 2 项阻塞（需要外部输入，架构师无法自决）**

| # | 阻塞项 | 影响 | 未解决时怎么开工 |
|---|---|---|---|
| 1 | **HIS 排班/医生接口契约**（字段名、鉴权方式、是否含实时停诊） | 决定 `schedule_api.py` / `doctor_api.py` 的字段映射 | **不阻塞开工**：按本文 `ScheduleRow` / `DoctorProfile` 定义写适配器，接口侧用 `tests/fixtures/` 回放；拿到文档后只改这两个文件 |
| 2 | **患者隐私与合规口径**（录音是否留存、日志保留期、是否需授权告知） | 决定 L 层留存策略与免责话术 | **不阻塞设计但阻塞上线**：按默认「音频不落盘 / 日志 30 天 / 常驻免责声明」施工，上线前须院方合规确认 |

**已定稿（不再列为未知，施工期如需微调按"改一处配置"处理）**

| # | 事项 | 定稿结论 |
|---|---|---|
| 3 | ASR 方案 | 本期用浏览器内置识别；上线前实测 200 条真实录音，`asr_min_confidence` 初值 0.85，不达标再上 `faster_whisper`（下期） |
| 4 | 自助机确认方式 | **触屏确认**（防误触误说话）；语音二次确认作为无障碍备用 |
| 5 | 院区 | **单院区**；`campus_id` 字段已预留，多院区时改 `departments/{campus}.md` + `source_policy.scope` 加 `campus_id` 层级 |
| 6 | 医生照片来源与授权 | 双角色上传 + `consent_source` 字段 + `photo_audit` 证据链；离职 `status='suspended'` 不物理删 |
| 7 | OA 单点 | **下期**，本期固定 `sso.provider="none"`，`NotConfiguredProvider` 返回 501 |
| 8 | 报告解读 / 数据分析 | **下期**，本期只留 Protocol |

**框架未覆盖、需人做原创判断的部分（不可能被设计解决）**：
- 「症状 → 科室」映射表内容属临床知识，本设计只提供可维护规则表结构（`data/symptom-rules.md`）与红线拦截，不提供医学判断逻辑。
- 排班"停诊"的实时性语义（HIS 停诊后多久生效）属业务约定。
- 「哪些医生必须配照片」属业务规范（能否无照片上线），框架不设默认值；技术上只保证无照片不破版。
- **报告解读的"什么算解读清楚"没有 ground truth**，只能靠临床人员抽检，不假装能自动评分。

---

## 15. 施工顺序、交付范围与验收

### 15.1 交付范围

| 本期交付 | 本期不交付（明确划线） |
|---|---|
| 患者端 kiosk/H5：今日排班网格、语音问答、医生卡与详情 | 挂号、缴费、取号 |
| 管理端：md 双栏+语音改 md（六道门禁）、手动排班、数据源健康与冲突裁决、医生照片上传 | 症状分诊（仅保留规则表结构与扩展位） |
| 医生自助页：签名直链 + 头像自传 | OA 单点（只留契约） |
| 数据来源四态 + 熔断 + 冲突记录 | 报告解读（只留 Protocol 501） |
| grounding 硬门禁 + 120 条回归集 | 就诊数据分析（只留 Protocol） |

### 15.2 施工顺序（五个里程碑，M1 即可演示）

| 里程碑 | 内容 | 完成判据（可验证） | 依赖 |
|---|---|---|---|
| **M0 骨架** | embeddable Python + `start.bat` 能起；`app/main.py` 起 FastAPI；SQLite 建表；`assert_writable_path` 生效 | 双击 `start.bat`，`GET /healthz` 返回 200；`data/guide.db` 生成且表齐全 | 无 |
| **M1 数据与展示** | `policy/resolver` 四态 + `schedule_manual` + `schedule_api`（fixture 回放）+ 患者端排班网格 | 12 组 resolver 单测全绿；界面每卡带来源徽标；断开接口后黄条出现且语音不撒谎 | M0 |
| **M2 md 与语音改** | `md/parser` + `md/patch` 六道门禁 + 管理端双栏 + ASR 接入 | 6 条门禁单测全绿；语音改一次地址，确认弹窗能看到 `3F → 4F` 的 diff；写入后重读 hash 一致 | M1 |
| **M3 语音问答** | 规则直答表 + LLM 槽位抽取 + `registry` 三分离 + `grounding_check` | 120 条回归集事实准确率 100%、兜底恰当；grounding 不通过时自动降级模板 | M2 |
| **M4 头像与认证** | `media/*` + 直链签发/校验 + 双角色上传 + 患者端医生卡头像 | 8 类畸形输入单测全绿；同图二次上传 `deduped=true`；伪造签名/过期/撤销/重复使用均被拒 | M3 |
| **M5 硬化** | 观测（JSON 日志+指标）、限流、回归门禁、`stop.bat`、部署包与验收文档 | 打包成 zip 在干净 Windows 机解压即可跑；P95 实测达标 | M4 |

**排序理由**：M1 先跑通"看得到"的部分（可给院方演示）；M2/M3 是核心价值但依赖数据；**M4 放最后不是因为它不重要，而是因为它依赖 `Principal` 与工具授权链，前面没稳时做认证等于在流沙上盖楼**。

### 15.3 每个里程碑的验收证据（不接受口头"做完了"）

| 证据类型 | 要求 |
|---|---|
| 单测 | `pytest -q` 全绿，且新增用例覆盖该里程碑的失败路径（不是只测成功路径） |
| 真人验证 | UI 入口真实点击驱动，截图 + 步骤；HTTP 入口给 Status Code + Body |
| 数据一致性 | md 写入后重读比对 `content_hash`；头像落盘后重算 SHA-256 |
| 回归基线 | 跑完 120 条问句，与上一里程碑结果比对，无新增失败 |

### 15.4 已知的风险（会让工期偏离，提前说）

| 风险 | 触发概率 | 缓解 |
|---|---|---|
| HIS 接口文档迟迟拿不到 | 高 | M1 已用 fixture 回放，接口只影响最后接线；但要**尽早拿到字段名**，否则 `ScheduleRow` 映射要返工 |
| 自助机 Chromium 过老，无 `Web Speech` | 中 | 立刻切 `faster_whisper`（下期依赖提前启用）+ 提供服务端 `/api/asr` 代理；这是唯一可能让 M2 延期项 |
| 院方要求 OA 单点提前到本期 | 中 | `SsoProvider` 契约已在，加一个实现约 1 人日 + 联调；但要额外 3-5 天联调与安全评审 |
| Pillow 在 embeddable 下缺 DLL | 低 | 打包机与目标机同为 x64；zip 内含 `_imaging.cp312-win_amd64.pyd` 与 VC 运行时；M0 就验证一次头像处理 |

---

## 附：默认假设清单（可逐条推翻）

| # | 假设 | 若推翻，影响 |
|---|---|---|
| A1 | 主栈 **Python 3.12 + FastAPI**，embeddable 免安装部署 | 换回 Node 只需重写 `api/` 与 `main.py`；`policy/md/media` 逻辑可平移（六层契约不变） |
| A2 | ASR 默认浏览器内置识别，`faster_whisper` 为下期可选 | 换 `voice/asr.py` 实现，`asr_min_confidence` 门禁不变 |
| A3 | `api_then_manual` 为默认数据源策略 | 改 `sources.json`，resolver 逻辑不变 |
| A4 | md 采用单文件 + front-matter | 改 `md/parser.py` 与文件布局，patch/version 机制不变 |
| A5 | 患者端匿名 + 设备指纹限流 | 改 `hooks.pre_invocation`，工具授权不变 |
| A6 | 不做症状分诊（仅保留规则表扩展位） | 新增一工具 + 一界面区块，红线拦截需加强 |
| A7 | 头像服务端处理，出 480 + 192 两档 WebP | 若只保留一档，`process_photo` 与 `serve` 改尺寸表，`doctor_photo` 结构不变 |
| A8 | 头像上传双角色（admin 代传需勾授权 / doctor 本人自传） | 若只留 admin，删 `require_photo_writer` 的 doctor 分支，`consent_source` 固定 `admin_proxy` |
| A9 | 报告解读与数据分析排期在下期，本期只留 Protocol | 若本期就做，需追加 `analysis/` 完整设计与临床验收口径，工期另计 |
| A10 | OA 单点下期接入，本期固定 `sso.provider="none"` | 若提前，加一个 `SsoProvider` 实现文件 + 补 `sso_state` 逻辑，`Principal` 与下游不动 |
| A11 | 医生直链**不是一次性**，而是"72h 有效 + 上传成功后失效" | 若要求严格一次性，`verify_magic_link` 加 `consumed_at IS NULL` 硬条件，代价是"打开看看"变死路 |
| A12 | 直链 token 放 **URL fragment**，前端读后转 `Authorization` 头 | 若改 query 参数，token 会进 Nginx access log 与 Referer，需额外做日志脱敏 |
