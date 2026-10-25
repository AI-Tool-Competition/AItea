# 印尼官方目录与原件续采

当前活动配置为 [`indonesia-20261003.json`](../config/collection-plans/indonesia-20261003.json) v18，共 17 项；v17 历史快照保留在 [`versions/indonesia-20261003.v17.json`](../config/collection-plans/versions/indonesia-20261003.v17.json)。本版本新增 4.8 BPJPH 清真产品法规目录：`https://jdih.halal.go.id/cari`。官方页面在 2026-10-24 的代理观察中报告 172 条、18 页；后续请求须逐页校验当前页、每页 10 条、UUID 唯一性和总数一致性。已通过实时代理检查前两页结构，动态分页遍历不构成同一时刻的原子全量快照。

BPJPH 专用适配器读取页面内嵌的公开 Next.js Flight JSON 数据，不执行页面脚本；按标题和文种筛选清真产品保证、认证、食品、进口产品及标签规则，并排除草案、内部管理和无关专题。候选详情必须匹配目录 UUID；PDF 只能来自该详情页公开的同源 `/api/jdih/pdf?path=jdih/dokumen/...pdf` iframe，且响应 MIME 和 `%PDF-` 签名均须有效。目录值作为未审核来源数据保留，法律效力、茶产品适用性和版本关系仍须依据原文审核。

BPOM 仍在代理来源池中轮询，当前没有绑定正式任务。根目录可访问还不足以证明正式法规目录分页及原件路径可可靠采集；验证完成前不把它计入正式清单覆盖。

`scripts/collect_indonesia_official.py` 按当前印尼清单的 1.3–1.6 项遍历 [Setneg 官方法规库](https://jdih.setneg.go.id/produk-hukum/instrumen/peraturan)。该机关的目录是这四项的一份官方来源，不能替代 BPK、地方与其他机关的范围，也不能据目录记录或原件到手推断法律有效、正文完整或已进入 RAG。

常驻来源轮询由 `scripts/unattended_collection.py` 执行，来源节点在 `config/collection-sources/id-sg-unattended.json`。Setneg 的 UU、PERPU、PP、PERPRES 使用各自固定的只读 JSON POST 目录和详情请求，按 50 条分页；原件仍通过校验过的官方 URL 下载。各类型的页面、详情、原件与正文索引分别计数，不能把目录总数当成已完成入库数。

2026-10-19 的实网遍历取得 UU 901、PERPU 33、PP 2,610、PERPRES 2,453 条，共 5,997 条。123 页的起始偏移、返回数、总数和唯一标识均已对账。原站不提供原子快照；完整标记仅对应这次观察到的同源目录。PERPU 的 33 条登记中，32 条提供共 34 份 PDF，79,828,724 字节，已逐份保存并核对哈希；两条 2005 年文件各另有一份附件。PERPU 2/2009（P14900）详情的文件数组为空，仍有原文缺口，不能用 34 份文件推断 33 项齐全。2/2022 原件首轮超时、一次有界重试后成功。正文解析、版本适用及法规审核仍单独验收。

P14900 后续通过 [BPK 官方公开 PDF](https://peraturan.bpk.go.id/Download/42613/Perpu%202%20Tahun%202009.pdf) 补得，保存于独立恢复源库，未改写 Setneg 的缺文件证据。45,854 字节的原件经受限原生解析核对文号、标题、LN 110 和 TLN 5036；6 页中第 4 页尚需图像识别。跨两份来源的原件覆盖 33 个已枚举 PERPU 项、共 35 份文件，正文完整性仍有独立门禁。

## 运行与扩量

默认数据目录位于当前工作树的 `data/` 或 `.dev/`。用户明确允许其他磁盘时，先创建任务专属真实目录，再用 `--storage-root` 显式传入。路径不能是符号链接；不会自动把数据写到系统临时目录。

```bash
uv run --project backend --no-sync python scripts/collect_indonesia_official.py \
  --output data/campaigns/indonesia-setneg \
  --detail-types PERPU --original-byte-budget 134217728 \
  --workers 4 --per-host 2 --timeout 90 --time-budget 1200 \
  --prepare-library
```

预算或时间结束后以同一目录加 `--resume` 继续。成功响应先验证原始字节和 SHA-256 再复用；不重新下载已登记成功的原件。要主动复测失败请求，加 `--retry-failed`。`Retry-After` 和 429 冷却持久保存，进程重启不会解除冷却。连续三次可重试的来源失败也会触发 60 秒起、最长 900 秒的退避；成功响应清除连续失败计数，已有服务端等待时间保留。每次请求沿用 HTTPS、公网 DNS、重定向、体积和超时检查。

自动恢复原件使用 `--resume --retry-transient`，不要反复传入宽泛的 `--retry-failed`；两个选项互斥。恢复器只选已登记、仍符合官方 PDF 路径和参数规则的 GET 原件，并要求上次结果明确为暂时错误。每件最多 8 次总尝试，逐件等待从 60 秒按指数增加、最长 1 小时，并与服务端 `Retry-After`、整个来源的冷却时间取较晚值。旧错误和 `attempts` 表不删除，旧记录缺少 `retry_not_before` 时由已保存的请求开始时间、耗时和次数推导。永久 HTTP 错误、体积/内容拒绝、身份不匹配、详情 POST 和无有效重试时间的记录不自动重试。

`report.json.transient_original_recovery` 保留本窗口选择的 URL、前次结果 SHA、尝试次数及下一次允许时间，并列出剩余待时、可重试和次数耗尽条目。调度器应等待 `next_at`，使用同一输出目录及已有排他锁启动下一个有界窗口；每次启动不会重新计数或扩大目录。8 次仍未成功的条目保持明确缺口，不能用不断清空历史或新建同源进程来冒充自动完成。

扩大为四类详情与原件可使用 `--detail-types UU,PERPU,PP,PERPRES --original-byte-budget 34359738368`，显式允许累计 32 GiB。可配置范围为 0–64 GiB；默认 0 不下载原件。预算包含当前 journal 中已保存成功的原件，`--resume` 不会重置，也不是完整性分母。每份原件仍最多 128 MiB；超过上限的文件留下缺口。外盘运行同时设置 `--min-free-bytes 42949672960`，保留 40 GiB 可用空间。

每批开始前按剩余预算、实时可用磁盘和每份原件最大响应计算并发槽位；例如保留线以上只余 256 MiB，即使 `--per-host 4` 也最多同时发起两份 128 MiB 原件。目录与详情同样按每份 4 MiB 响应限额预留。空间不足时原件保持 queued，报告 `requested_scope.storage_backpressure=true`；释放空间后以同目录 `--resume` 继续，不重新下载成功原件。这是本采集器每批的准入检查，不能为其他进程预留磁盘。目录之后按类型轮转详情和原件，及时排入现有处理队列，不等待某一类型全站下载完成；实际同站连接数仍受 `--per-host` 约束。

## 记录和处理边界

- `journal.sqlite3` 保存目录、详情和原件任务以及每次请求结果；每个成功响应先持久保存 CAS，再提交状态。相同目录只允许一个采集协调器。
- `ledger.jsonl` 包含 `source_key`、原始 URL、国别、清单编号、文种、官方 ID、标题、状态、版本／观察时间、文件、关系和发现页证据。原件获取状态、正文处理和法律审核分开记录。
- `report.json` 检查连续分页、末页、稳定总数、重复 ID 和原件缺口；每类分别计数有原件的法规项、下载文件和已取得详情但无文件的项目。`full_29_item_checklist_complete` 始终为 `false`，因为本工具只承担四类官方补源。退出码 0 只表示明确请求的这次范围完成。
- `--prepare-library` 复用 `Service`、`archive.store_original` 和持久处理队列，在同目录创建 `tea.sqlite3`。采集原件与目录证据已属于同一账号 CAS，准备阶段直接复用，不另写一份原件；历史其他账号的证据需要复制时，先复用已校验的目标 CAS，否则按实际新增字节检查保留线，空间不足则停止准备并保留断点。文档为 `country:ID` 一级法规草稿；此步骤不会调用模型或授予法律审核资格。正式扫描识别和向量化由既有工作进程消费。
- `campaign.json` 把原件版本和哈希绑定到同账号 CAS 中的目录／详情证据。`mengubah`、`dicabut` 等源站字段按原值与 JSON 位置保存为待核的版本关系断言，未凭字段自动更改法规效力，也未当作已审核的修订关系写入业务关联表。
- 同一官方 ID 下只有一份未标注附件的正文时，将文件名明确含 `Lampiran` 的文件关联为 `related`。关联位置包含目录／详情哈希及原始 `/file/N/realName`；两份文档的角色、分级、版本、解析结果与审核状态保持原值。存在多份可能正文时不猜对应关系；该关联只证明官方文件分组，不证明法律效力。

数据目录连同 `tea.sqlite3`、`files/`、`campaign.json` 和采集 journal 一起备份。数据库文件在线复制须使用项目备份工具；任务运行中不要直接复制 WAL 数据库。导入总库使用现有审核过的资源导入工具，保留来源证明，不能把该工程采集账号变成商家私有上传账号。

## 相关验证

`backend/tests/test_indonesia_official_collection.py` 覆盖缺页、重复、源站总数变化、末页、断点续跑、原件证据被改动、冻结配置变化、真实 SQLite 原件保存幂等、准备阶段禁止模型调用、32/64 GiB 显式预算及越界拒绝、磁盘限额下收缩并发与断点恢复、同账号原件不重复落盘，以及重启后继续遵守限流等待。外部站点仍可能变化，测试通过不构成“全部网站始终无故障”的保证。
