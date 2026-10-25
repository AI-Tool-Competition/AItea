# 新加坡官方资料采集

## 全量范围与当日目录

业务方案 v7 有 15 项活动任务；v5、v6 快照分别保留原始 16 项范围和因访问受限而移除目录项时的状态。2026-10-24 的 SSO 403 是历史观测；2026-10-25 使用项目代理重新取得 Current Acts 与 Current Subsidiary Legislation 两个官方目录，v7 恢复 1.1、1.2、1.3。1.1 是 Current Acts 的 Constitutional 筛选，不是独立来源。目录计数和动态监控不代表全部目录页或正文已下载、解析、向量化或通过法律审核。SG-LegalCite 的 2.1 仍因来源页与官方 API 超时而缺失。监管操作资料 3.1–3.7 的目标为合计 30–45 份、总上限 45 份，不能按每组 45 份计算。所有资料均须保留原件、不可变版本和来源定位；目录完成、文本完整、向量完整、法务审核分别核验。

2026-10-19 v5 历史快照以每页 300 条记录过 Current Acts 300/526、Current Subsidiary Legislation 300/5,872 和 3/3 个宪制文件；后续旧探测遇到 AWS WAF 或 CloudFront 拦截。2026-10-25 的正式目录探测改用每页 500 条，分别对账为 Acts 526 条、2 页，Subsidiary Legislation 5,872 条、12 页。后者的全量目录分页仍由无人值守采集器按来源额度逐步完成。目录行数不是已取得原件或已建立可检索正文的数量；“Current”仅是来源栏目状态。未生效、废止、失效法规登记、修订历史和附属法规授权关系仍有独立缺口。

`scripts/collect_sso_catalog.py` 固定方案快照、原始页面压缩字节及 SHA-256，逐页核对总数、身份重复和明确下一页；导航链接不会占用原件候选额度。恢复运行仅继续缺页，不替换旧证据。正式来源池也使用同一 SSO 目录解析逻辑；目录页与最多 10,000 条缓存元数据保留为可恢复队列，受 128 条活动队列限制时分批继续处理。尚未取得的原件不计为可检索资料。

## eGazette 公开目录与历史原件

[eGazette 官方公开前端](https://www.egazette.gov.sg/egazette-browse/)提供 Legislative Supplements 的只读检索配置。适配器只读取已核查页面中的字面数据，限定应用、索引、主机及 `/1/indexes/prod_ogp_egazettes_index/query` 端点；请求方式遵循 [Algolia 官方单索引检索协议](https://www.algolia.com/doc/rest-api/search/search-single-index)。公开前端检索凭据仅用于该只读端点，禁止重定向传递或附加到下载域；原件仍经过 HTTPS、公网地址、TLS、体积、时间和每域最多 2 个连接的安全链。

来源返回 `Retry-After` 时，主机冷却时间保存在 `network-state.json`；冷却期间和恢复运行后均不向该域发新请求。未给等待时间的 HTTP 429 至少冷却 60 秒，尚未发送的原件标为 `cooldown_pending`，不把它们记为已经尝试下载的永久失败。

连续三次暂时连接失败也会使该域冷却，等待从 60 秒指数增加、最长 1 小时；计数保存于 `network-failure-streak.json`，重启不会清除，成功响应才复位。原件恢复须显式传入 `--retry-incomplete`；默认最多两次总尝试，可用 `--max-original-attempts 8` 提高到八次。每件的 `retry_not_before` 与整站冷却共同约束下一次请求，报告给出 `retry_next_at` 和次数耗尽数量，不能另开同站进程绕开等待。

每次补试前把旧收据原字节写入 CAS，校验前缀尝试链、官方 URL、标题和目录定位，再持久预留新的尝试号。重启中断只允许经过退避后创建后续尝试，不重复使用或清零原尝试号。所有历史收据继续由 `campaign.json.download_attempts` 显式映射，正式导入逐一保留其哈希和原字节。已成功原件只校验后复用，403/404、错误内容与超限文件不自动重试；达到明确次数上限仍保持缺口。

`scripts/collect_egazette.py` 按四种法律出版类别与年份分页，查询窗口不完整时按出版时间有界分区；`exhaustiveNbHits=false` 明确保留为计数未确认。1998–2026 的实际目录发现 27,124 个不同官方 PDF URL，116 个年份／类别分区中 110 个计数确认、6 个仍受来源计数或窗口限制，不能据此宣称完整收齐。原始响应、请求参数、JSON pointer、原件 URL、内容哈希及下载收据分别保留，可续跑并复用已验证旧原件；失败收据不盲目重试。运行阶段的实际下载数量以该运行目录 `originals/` 收据及最终 `report.json` 为准。

原件单次文件上限 32 MiB，总下载预算上限 10 GiB，并在发起请求前按在途预留量保留至少 40 GiB 磁盘空闲。低于余量时记为 `storage_pending`，释放空间后可以续跑。`--stage-originals` 只把已验证 PDF、目录响应和下载收据写入独立源库的 CAS 与持久化解析队列，不启动网络或模型 worker；`campaign.json` 保留可随总库迁移的证据关系。重复入队复用同 URL、原件哈希和版本；恢复旧原件时必须显式提供其来源根目录。后续处理仍须遵守已保存的正文和向量复用规则。

eGazette 文件是特定日期公布或修订的版本，统一标记 `gazette_published_or_revised_version_not_current_consolidated`。其正文不能替代独立的 SSO 现行合并文本、修订链或生效核验。目录和下载不调用模型，后续解析与真实向量化通过持久化任务进行，草稿不得自动进入正式业务法规 RAG。

```bash
uv run --project backend --locked python scripts/collect_egazette.py \
  --output data/campaigns/sg-egazette-1998-2026 \
  --year-from 1998 --year-through 2026 --originals 1000 --stage-originals \
  --original-byte-budget 1073741824 --time-budget 1800
# 同一输出目录续跑须显式增加 --resume；不能改变冻结的年份、类别或分页大小。
```

## 中新税收协定的两份 OECD 原件身份

总库 SG 准入只增加下列两个完整 URL，不开放整个 OECD 域或目录；查询串、片段、路径编码、邻近文件、其他主机及 ID 国家范围均不适用。原 URL、原件哈希和草稿审核状态不改写：

| 官方原件 | SHA-256 | 已保存字节 |
| --- | --- | ---: |
| [中国 MLI 交存立场](https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/beps-mli/beps-mli-position-china-instrument-deposit.pdf) | `b2d79dd37746899f7f18d5496617f55f57f63e38fadab3efdd7abab934c1def8` | 2,111,395 |
| [新加坡 MLI 交存立场](https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/beps-mli/beps-mli-position-singapore-instrument-deposit.pdf) | `0a7b10250c3940935f8a421a239a66f917a5d2f7a2fb014c367a69daa76b6e2b` | 1,392,061 |

[IRAS 官方 MLI 说明页](https://www.iras.gov.sg/taxes/international-tax/international-tax-agreements-concluded-by-singapore/multilateral-convention-to-implement-tax-treaty-related-measures-to-prevent-base-erosion-and-profit-shifting)保存的 HTML SHA-256 为 `f417e46a3fc8dde944411a8c99aee0f1fb5fc589a3a05f97bde5cefb2ef2bfa0`。其中 UTF-8 字节 254682–254878 的链接指向 OECD 旧版新加坡交存立场 URL，片段 SHA 为 `7f26c17642ee951a254f251c7169b5d6ab072e010a9437b85bbd2b05a8697421`；字节 239630–239873 指向 OECD MLI 总入口，片段 SHA 为 `48414f9ae05b5dc352fa9aec343b453d76b0864aa3e3628884e26d7d8a36e7c3`。IRAS 保存页没有直链上述中国 PDF，不捏造这一关联；中国 PDF 来源证据为 OECD 自身发布的原件，其中列有中新协定及相关议定书。

历史证据位于归档 `source-data/verification/sg-iras-ipos-official-gap-recovery-20261014/`，包含 IRAS 原始 HTML、响应收据和两份 OECD 原件；恢复时重新核对哈希。当前 OECD 总入口的受限下载返回 403，保留失败证据；该失败不改变已取得原件的来源身份，也不证明其法律效力。两份立场文件与 DTA、议定书及适用 MLI 的实质关系仍须审核。

## 联合国登记的独立协定原件

[联合国条约集第 8206 号登记页](https://treaties.un.org/pages/showDetails.aspx?objid=080000028012b8be)直接链接 [Volume 563 的英文原件](https://treaties.un.org/doc/Publication/UNTS/Volume%20563/volume-563-I-8206-English.pdf)。2026-10-19 经安全下载链取得的登记页为 23,064 字节，SHA-256 为 `765931abce6c62093669393e78b44094af453b1e11d8e152eb16ee220a2571ae`；PDF 链接起始标签位于 UTF-8 字节 16803–16901，片段 SHA 为 `2eed6f7bac8583291fb9a6cc95296421137b3bbcf12af1a3f8fc47408aa0a51b`。已有 PDF 原件为 135,037 字节，SHA 为 `ce4c66535ce615dd15d3bec48d790687a9bd7648c3c3080c74935316ae78e28f`，已从来源账号 CAS 重新校验。

SG 总库仅准入上述含 `Volume%20563` 的完整原件 URL。相邻登记号、法文版本、双重编码、查询串、其他主机和 ID 国家范围仍拒绝，不授予整个联合国域或目录。登记页标明 1965 年签署及 1966 年登记，作为历史正式出版证据保存；原件、8 页来源映射和已有向量可复用，不能代替 SSO 当前版本或法务审核，导入仍为草稿。

## 来源与版本

`singapore-demo-20261009` 版本 4 仅补充 SG 3.1、3.6 已实取的官方入口与文件路径。16 项原要求、条目编号、排除范围和原 DOCX 的 SHA-256 保持不变。版本 3 的原字节保存在 `config/collection-plans/versions/singapore-demo-20261009.v3.json`；旧绑定、来源计划、任务和选取文件继续使用冻结规则。版本 4 另有同字节快照，不在恢复旧运行时隐式升级。

PDPC 当前公开站点在 HTML 内保存 Next Flight 数据。直接搜索到的旧 Key Concepts 和 Selected Topics 地址实取返回 404；当前正式栏目位于 `/organisations/regulations-decisions/regulatory-guidance/`。实际文件链接为同站 `/assets/<UUID>`，链接没有 PDF 扩展名。新适配只对明确启用 `limits.sg_official_2026=true` 的 SG 3.6 生效，读取四个指定指南栏目与 Managing Data Intermediaries 页面，不扩采决定库、咨询稿、金融或医疗专项指南。

适配只解码完整字面 `self.__next_f.push([1, "…"])`，不执行脚本、动态表达式或组件。JSON 正文中的 `data.content` 与已验证的 UTF-8 字节长度文本记录用于提取链接；模块导入、资源预加载、普通字符串和图像地址不能冒充正式 PDF。网页、数据流、单记录、块数、记录数、JSON 深度、对象数与链接数分别有上限。截断、未知协议、异常长度、验证或错误页均保留失败。定位包含原 HTML SHA、脚本位置及哈希、解码数据流哈希、记录字节范围和必要 JSON pointer；解码范围不冒充原始 HTML 字节范围。

原理核对采用 [Next.js 官方 Server/Client Components 说明](https://nextjs.org/docs/app/getting-started/server-and-client-components)与 [React 官方 `emitTextChunk` 源码](https://raw.githubusercontent.com/react/react/main/packages/react-server/src/ReactFlightServer.js)：客户端渲染的数据载荷与预渲染 HTML 分开，文本记录带十六进制字节长度。该实现仅识别实际保存的 PDPC 公共载荷格式，遇到新增协议先留下适配缺口。

Customs 新入口采用 `/doing-business/import-operations/`、`/doing-business/valuation-duties-and-fees/` 等路径。仅八个确切文章路径被登记为 HTML 正文候选，涵盖进口主体、许可、控制货物、GST、估价、留档及归类裁定；目录和其他商品或行业文章不因此变成正文。实际转介文件仅来自 `isomer-user-content.by.gov.sg` 的 `174/<UUID>/<文件名>.pdf`。该文件域不继承 Customs 文章或目录路径，其他 bucket、错误 UUID、额外查询参数均拒绝。

所有下载继续经过现有 HTTPS、公网地址、TLS、重定向、字节数、截止时间及并发限制。新链接、原生文本和标题均不证明法律效力或业务适用性；正文仍须检查图片、表格及解析完整性。

## 实际验证与已知缺口

服务器私有证明位于 `data/verification/sg-official-gap-20261013/`。本次有界选取 19 件：PDPC 九份 PDF、Customs 八份 HTML 和两份 PDF，共 14,915,596 字节。19 件原件哈希与受限解析源哈希均核验；PDF 共 357 个物理页，其中 22 页待识别，已保存 22 张实际受限渲染页图及原件、物理页、图像哈希与尺寸映射。HTML 的 11 处 literal 图像引用包括重复站标和三个业务图表，仍需按正文范围核对，不将替代文字当作图中完整文字。

PDPC 当前文件的原生封面显示 Key Concepts 修订为 29 April 2026、Selected Topics 为 May 2024；DNC、营销同意及委托处理材料另保留各自的原文件名和封面。年份和原生读取结果不能代替版本、生效或适用期审核，旧搜索摘要不能覆盖当前原件。

用途分级与法规法阶分开。本次工程选取以完整指南作为一级，Key Concepts 的 Annex A/B/C、委托处理 primer 和 Customs 两份配套说明作为二级；二者的文件角色均可解析。关联类型为 `related`，两端原件 SHA 和官方页面 SHA 固定保存，不将官网链接推定为法定授权、修订或已核引文。二级向量为零。普通上传附件的 `role=secondary` 继续只存原件。

独立 CLI 发现证明位于 `data/campaigns/sg-pdpc-official-v4-20261013/`：七页实际来源发现九个候选，零失败和零未处理入口。另一独立选取运行 `data/campaigns/sg-pdpc-official-v4-selected-20261013/` 取得九件原件、一级五件、二级四件及四条已保存版本关联；其模型关闭的初始快照中仅 primer 一件通过文本完整性，向量为零；后续真实模型处理另写 `model-windows/`，不改该快照或冻结的方案选项。两个账号及数据根分别盘点，不累加为法规覆盖数量；“发现范围完成”不代表 SG 3.6 已满足所有验收要求。

PDPA、Customs Act、GST 法规及 RIEA 的正式正文、适用修改和法律审核仍待补；SSO 1.1/1.2/1.3 已恢复目录监控，但各页、法规原件和索引状态仍须以正式运行账本核验。SG-LegalCite 2.1 仍有访问缺口。会员登记、营销、退订、跨境访问的实际处理事实仍须核实。茶的配料、加工、包装与当地完整 HS 归类尚未确认，不照抄中国 HS 尾码，也不把个人包裹规则当作商业进口豁免。上述有界选取不能代替新加坡 15 项活动任务的验收，也不能消除原始范围中的案例数据缺口。

## 操作与复核

先使用新的私有数据根检查新版本来源；不将旧任务的选取模式、版本或来源快照改成新运行：

```bash
uv run --project backend --locked python scripts/collect_resources.py \
  --plan-id singapore-demo-20261009 \
  --output data/campaigns/sg-pdpc-discovery \
  --items 3.6 --discovery-batches 1 --originals-per-item 0 \
  --time-budget 80 --include-source-mapping
```

选取文件按实际保存的官方页面列明 URL、一级／二级用途、`primary_keys` 和 locator，绑定原清单 SHA。从抽样模式切换为显式选取须创建新数据根，既有恢复规则不允许替换冻结选取。模型处理使用同一原件及检查点，须明确启用真实 OCR／embedding 配置；完整一级才建立真实向量，二级不建立向量，草稿和未经审核的版本继续受业务 RAG 门禁限制。

工程验收包括真实 Next 记录回放、协议与资源界限、200 错误页、分域路径、版本冻结及既有 WTO 参数兼容。采集运行的零失败不能代替图片识别、语义校验或法务复核。
