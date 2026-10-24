# MinerU 云端解析与本机 RAGFlow

PDF、Office 与图片原件使用：保存并校验原件 → MinerU 精准解析 API → 校验结构与页码 → 保存正文切片。静态 HTML 使用锁定的 Selectolax/Lexbor HTML5 树校验，再由项目结构化提取器按原件定位标题、正文、表格与对象。两条路径都进入同一 RAGFlow 云端 embedding 和受权限、法规版本及效力约束的检索。本机不运行 MinerU、OCR、视觉模型或 embedding 模型。

## 解析与原文证据

`MINERU_API_TOKEN` 只放在后端 `data/config/application.env`。PDF 使用 v4 精准解析、`model_version=vlm`，开启表格、公式和 OCR。上传前核验原件 SHA-256、大小和账号归属；超过 200 页机械分段，每段保留原件物理页范围。本地分段不提取文字或执行 OCR。

`document_parse_tasks`（结构版本 20）持久保存原件、解析配置、分段、批次、进度、上传回执和结果对象。上传结果不确定时先查询已有批次；不会创建新批次来重放已上传的文件。断点续跑保留已完成的分段。

原始 ZIP 与其中的 Markdown、布局、内容列表、模型输出、表格、图片及供应商导出的 PDF 都保存到账号隔离的内容存储。供应商导出的 PDF 与上传原件分别保留，不以重写后的 PDF 替换原件。下载与解包有时间、大小、成员数量和路径限制；本地整理在受限子进程执行。

正文块保留物理页、坐标、章节、表格 HTML、公式 LaTeX、图片及图注、脚注和原始 JSON 块引用。MinerU 跨页合并表格时，优先用同页、同坐标的模型输出恢复该物理页的表格文字；不把完整跨页表格都归到首页。无法确认的页保留问题并阻止整份正文进入检索。空的辅助文件记录空内容哈希，原始字节仍保留在 ZIP 中。

MinerU 将图表识别为 `chart`、但没有返回可读内容时，原始图像按结构块、物理页、坐标和 CAS 哈希建立独立清单，并调用现有视觉 API 作图表专用转写。标题、轴、单位、图例、类别、表格和已标注数据值进入独立 `figure_transcription` 结构块；不得按图形位置估算数值。未标值数据、空图、截断、图像限制或模型疑点继续阻止该页完整及整份文档入库。只有页面内全部缺失图表均有完整转写后，才解除 `chart_content_missing`；重解析仅复用结构定位与原始图像哈希完全一致的检查点。

单份组装结果最多 800 万正文字符、20,000 个内容块，传输给持久化层的结构化结果不超过 40 MiB。超过边界会保留原件与 MinerU 回执并说明触发的具体上限；不能将未组装结果标为完整。正文字符上限依据大体量多页表格资料复核，支持 337 页、约 533 万字符的来源；最终 40 MiB 清单界限仍保护数据库与备份读取。

## 并发探测

`.env.example` 提供以下起始配置：

```dotenv
MINERU_MAX_ACTIVE_TASKS=8
MINERU_AUTO_CONCURRENCY=true
MINERU_CONCURRENCY_CEILING=128
MINERU_CONCURRENCY_WINDOW_SECONDS=180
MINERU_REQUEST_INTERVAL_SECONDS=1
MINERU_POLL_SECONDS=15
```

`MINERU_MAX_ACTIVE_TASKS` 是手动上限，开启自动探测后作为起始值。自动档按约 1.5 倍增加，例如 8 → 12 → 18 → 27 → 41；允许配置范围 1–256。这里限制的是申请、上传、等待和解析中的在途分段数，供应商实际同时执行多少由其服务端调度。

每档至少观察一个窗口，累计跑满时间达到窗口的一半，并完成至少 `max(8, ceil(该档并发 / 2))` 个真实分段，且该窗口没有传输错误，才记为已验证并升档。队列不足时保持当前档，不把空闲时间算作稳定负载。不会为探测重复提交已经完成的资料。

429 或 `-60009`（提交队列已满）立即退档；连续三次传输失败，或一分钟内至少 20 个请求中错误率超过 5%，也退档。单次偶发传输失败重启观察窗口。退回已验证档后，继续在稳定档和不稳定档之间缩小区间；相邻后延长至三个窗口验证，再保留该档。已经在途的任务继续完成，不因降档丢弃或重新解析。

每档实际峰值、满载时间、完成分段及页数、请求数、错误数和升降档原因保存在数据目录 `.runtime/mineru-api.lock`，多个进程共用。独立续跑工具把相同证据写入进度 JSON。进程重启保留证据；同一实例的 worker 必须使用一致配置，配置不一致会拒绝启动探测，不能覆盖已验证证据。主动改变探测配置时先停止所属 worker，归档原有观察记录，再开始新一轮。监控应只读该状态，不以另一套默认配置初始化控制器。

请求频率独立于在途上限，并遵守服务端 Retry-After。每日任务额度与 HTML 额度不足分别记录为 `-60018`、`-60019`，只延后新提交，仍允许查询和下载已创建任务；它们不算并发上限。错误码依据 [MinerU 官方接口文档](https://mineru.net/apiManage/docs)。

“已验证最高档”“配置保护上限”和“首次不稳定档”应分别报告。队列耗尽或达到保护上限时，不能宣称已经测出服务的绝对最高并发。观察结果也只代表该批资料和当时服务状况。

## 本机服务

RAGFlow 固定为 `v1.0.0-rc2`，使用 Go API 和外部 embedding。普通用户运行方式使用 Apptainer、独立用户/网络命名空间和 slirp4netns，不需要 sudo、uidmap 或系统 Docker。程序位于 `/home/wenbo/.local/opt/aitea/`；私有凭据、密钥和服务配置在 `data/config/ragflow/`，进程身份和启动日志在 `.dev/ragflow/`，数据库和索引在 `data/ragflow/`；统一位置与迁移入口见 [实例管理](INSTANCE_MANAGEMENT.md)。Infinity 运行日志写入 `data/ragflow/infinity/log/`，按 100 MiB、保留 10 份轮转；禁用重复写入未轮转的标准输出日志。该方式要求 Linux 允许无特权用户及网络命名空间。

```bash
uv run --project backend --locked python scripts/ragflow.py prepare --runtime user
uv run --project backend --locked python scripts/ragflow.py up --runtime user
uv run --project backend --locked python scripts/ragflow.py bootstrap
uv run --project backend --locked python scripts/ragflow.py status --runtime user
```

默认管理界面为 `http://127.0.0.1:18080`，API 为 `http://127.0.0.1:19380`，其他组件端口只在私有网络内可达。启动等待真实健康检查；停止核验已记录的进程身份，只停止本项目进程并保留数据。已有 Docker Engine/Compose 的机器也可使用 `--runtime docker`；当前本机实际验证的是普通用户方式。

bootstrap 通过原有 `EMBEDDING_BASE_URL/API_KEY/MODEL` 注册 RAGFlow 模型提供商和实例，保存真实模型 ID、集成密钥到忽略配置；不下载 embedding 权重。私有密钥和内部账号密码不要提交或展示。

## 索引与检索

AItea 在 RAGFlow 中登记资料身份，再通过 chunk API 导入已校验的结构化切片，不触发 RAGFlow 内置解析或本地 ingestion worker。原件不超过 128 MiB 时登记原件；超过上限时创建空白虚拟文档，避免重复上传 MinerU 已处理的原件。`RAGFLOW_CHUNK_CONCURRENCY` 控制每份文档一次写入的块数，默认 8；它与 MinerU 在途数是独立配置。

结构版本 21 增加数据集、文档代次和切片回执表。代次绑定账号、原件、不可变版本、检索配置与本地索引修订。写入前保存意图，成功后保存远端 ID。响应丢失时先分页核对远端精确正文；RAGFlow 分块列表用 `content_with_weight` 返回正文、用 `chunk_id` 返回标识（兼容 `content`/`id` 形式）。常规任务可确认时直接归账；已进入待核验的未知结果由后台限量只读复核，只有所有未知切片都精确匹配才恢复剩余未发送任务，未匹配部分继续阻止重发。复核分页、请求超时和重查间隔均有上限。源文件或权限变化会使旧写入失效。

RAGFlow 负责向量与混合召回，AItea 继续负责账号、业务、国家、主/二级分类、法规审核与适用期。请求前只发送当前可用的远端文档 ID，返回后再次核验资格，并从本地可信切片生成引用。空的允许集合不调用 RAGFlow 的全库检索。解析完成或向量完成都不会自动发布法规。

缺少 RAGFlow 配置或调用失败时明确显示检索未就绪，不生成假向量，也不回退到旧本地检索。历史 `lexical`/`hybrid` 分析记录仍可阅读，当前执行模式为 `ragflow` 或 `unavailable`。

## 保留与恢复

原件、旧版解析检查点、已支付的历史 OCR 回执均保留。当前执行使用 MinerU，历史证据仅用于追溯，不作为启用旧识别流程的开关。

备份需要区分 AItea SQLite/CAS 与 RAGFlow MySQL、Infinity、对象存储等持久状态。`data_backup` 校验 AItea 的原件和解析产物，不等于已取得一致的 RAGFlow 在线快照。备份整个 RAGFlow 时先停止所属服务，保存 `data/ragflow/` 及对应私有配置和密钥，再启动并验证恢复；不能把运行中的数据库文件直接复制后称作一致备份。
