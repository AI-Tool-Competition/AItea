# 无人值守官方来源池

配置为 [`config/collection-sources/id-sg-unattended.json`](../config/collection-sources/id-sg-unattended.json)，共 17 个稳定来源节点：印度尼西亚 10 个（6 个活动、4 个归档），新加坡 7 个（全部活动）。`priority` 数值越大越优先，范围 1–100；默认复查间隔为 24 小时；明确为空的 BPJPH 新目录以 `empty_catalogue=true` 标记，7 天低频复查，不作为业务采集成功。每条固定国家、主题、资料性质、精确 HTTPS 主机和发现关键词，运行时根据来源健康状态选择下一个有业务价值的入口。抓取不得跳出这些来源所允许的主机，也不得把新闻、验证码、错误页面或商业整理作为法规正文。

配置采用 `aitea-official-source-pool-v2`：`groups` 是国家根节点及主题子节点，子节点列出来源 ID 和对应核验清单项；来源自身保留正式入口、访问证据、主题、发现策略、优先级、轮询间隔和 `active` 状态。活动源须留有官方身份、业务用途、清单范围、近期开页回执与正文 SHA-256；空目录单独标记并低频复查。`source_relations` 是有方向的关系边，区分 `complements`（互补）、`cross_checks`（交叉核验）、`implements`（实施规则）与 `publishes_original`（正式原件发布），并说明适用清单项和关系理由。所有活动源都明确使用 `transport: proxy`；watchdog 将其精确允许主机并入代理白名单。归档用 `status: archived` 移出运行队列，原节点、关系和所有历史账本均保留；新增来源必须经有界代理抓取取得有效正文后才能登记。

本池服务于两份既有法律清单：印度尼西亚 29 项的正式法规、部门补强与国际协定，以及新加坡六堡茶进口经营、食品、税务、公司、个人资料、知识产权及贸易协定事项。食品和清真来源用于补足茶叶食品经营的监管法源，不表示扩采全部药品、化妆品、政务操作或所有行业材料。全部原件、状态和版本关系仍须通过既有采集、解析、审核和入库规则。

## 优先启动的互补来源

| 优先级 | 来源 | 用途 |
| --- | --- | --- |
| 100 | [ID 贸易部法规目录](https://jdih.kemendag.go.id/peraturan) | 进口、贸易规则、原产地及消费者保护 |
| 99 | [SG SFA 食品标签要求](https://www.sfa.gov.sg/regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines/labelling-requirements-for-food) | 茶叶食品标签、进口商责任与声称规则 |
| 98 | [ID BPOM 官方法规目录](https://jdih.pom.go.id/rule) | 食品注册、标签、包装与食品安全 |
| 97 | [SG Customs 进口概览](https://www.customs.gov.sg/doing-business/import-operations/import-procedures/import-procedures-overview/) | 商业进口、许可、估价、GST 与海关责任 |
| 82 | [ID 海关总署法规目录](https://peraturan.beacukai.go.id/) | 印尼进出口、关税、海关及消费税规范 |

这五个入口均已在当前服务器取得真实业务内容，适合首批并行运行。印尼海关目录于 2026-10-24 经项目代理成功获取，配置回执记录页面正文和来源事件 SHA；目录自述仍在更新，不能据此宣称覆盖完整。贸易部入口在另一次网页浏览中出现过拒绝请求，而服务器此次有界直连成功；不同访问结果分别保留，不能因此认定其网站已过期。

## 完整来源与边界

首轮核验日期为 2026 年 10 月 20 日（北京时间）；后续新增与失效复核均按各来源 `verification.checked_at` 记录。每条来源性质、法律清单对应项、原始内容摘要校验和、网络回执及限制保存在配置中。下列链接同时作为主管机关身份与业务内容的官方证据。

| ID | 主管机关/入口 | 国家 | 用途和本次服务器结果 |
| --- | --- | --- | --- |
| `id-kemendag-regulations` | [JDIH Kementerian Perdagangan](https://jdih.kemendag.go.id/peraturan) | ID | 法规目录可达，含近期规范及状态标签；只按贸易法律问题收录。 |
| `id-bpom-regulations` | [JDIH BPOM](https://jdih.pom.go.id/rule) | ID | 法规目录可达，含食品包装等相关规则；排除无关药品/化妆品材料。 |
| `id-bpjph-nonhalal-regulation` | [BPJPH 非清真标识规则](https://bpjph.halal.go.id/read/keterangan-tidak-halal) | ID | 有业务内容，可定位正式规则原件入口；具体茶产品适用性待原文审核。 |
| `id-bkpm-regulations` | [投资与下游产业部/BKPM JDIH](https://jdih.bkpm.go.id/id) | ID | 可达，补投资、经营准入、KBLI 及许可法源。 |
| `id-beacukai-regulations` | [海关总署法规目录](https://peraturan.beacukai.go.id/) | ID | 项目代理返回 HTTP 200；补海关、进出口、关税和消费税规范，与贸易部交叉核对、与新加坡海关互补。目录自述仍在持续补齐，不能代表全国法规全量。 |
| `id-kemenkeu-www-regulations`（归档） | [财政部 JDIH](https://www.jdih.kemenkeu.go.id/)，[官方海关估价规范实例](https://www.jdih.kemenkeu.go.id/dok/144-pmk-04-2022/summary) | ID | 旧入口跨日 10 次代理请求均失败，最近为 TLS 证书验证失败；保留归档和旧回执。海关总署目录仅补海关规范，不替代财政部税务、估价等完整目录。 |
| `id-pajak-regulations`（归档） | [税务总局法规目录](https://pajak.go.id/id/peraturan) | ID | 旧入口 9 次连续返回无法验证的页面；`/index-peraturan/` 和 `/katalog-peraturan` 两个官方备选入口也只返回无法验证内容，因此不把挑战页当法规正文，不再放在活动抓取队列。印尼税务覆盖仍有缺口。 |
| `id-ditjenpp-regulations`（归档） | [法律法规总局数据库](https://peraturan.go.id/) | ID | 旧入口跨日 10 次代理请求均超时；没有可用的全国法规目录替代项。全国法律覆盖缺口仍显式保留。 |
| `id-bpjph-regulations` | [BPJPH 新版法律资料库](https://jdih.halal.go.id/)，[发布机关官方转介](https://bpjph.halal.go.id/berita/) | ID | 首页可达但明确显示尚无法规、无分类；空目录仅记录，不能计入正文采集。 |
| `id-setneg-perpu` | [国家秘书处 PERPU 目录](https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0) | ID | 保留旧任务的国家立法来源；历史取得 33 条目录、23 份记录的 24 份原件。本轮原入口和20条分页入口均404，记为待复查，不能认定域名过期。仅此固定范围启用既有Setneg适配器及受限JSON解压。 |
| `sg-sfa-labelling` | [Singapore Food Agency](https://www.sfa.gov.sg/regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines/labelling-requirements-for-food) | SG | 可达，食品标签和相应法规/官方指南转介。 |
| `sg-customs-import` | [Singapore Customs](https://www.customs.gov.sg/doing-business/import-operations/import-procedures/import-procedures-overview/) | SG | 可达，商业进口主体、许可和税费义务。 |
| `sg-iras-imports` | [Inland Revenue Authority of Singapore](https://www.iras.gov.sg/taxes/goods-services-tax-%28gst%29/claiming-gst-%28input-tax%29/importing-of-goods/) | SG | 可达，进口 GST、抵扣及 e-Tax Guides。 |
| `sg-enterprisesg-rcep` | [Enterprise Singapore RCEP](https://www.enterprisesg.gov.sg/grow-your-business/go-global/international-agreements/free-trade-agreements/find-an-fta/rcep) | SG | 可达，RCEP 与原产地/关税附件发现；外链不得自动跨域抓取。 |
| `sg-acra-compliance` | [Accounting and Corporate Regulatory Authority](https://www.acra.gov.sg/regulations/compliance-requirements/) | SG | 可达，经营主体和持续合规义务；排除无关金融主体/审计行业材料。 |
| `sg-pdpc-marketing` | [Personal Data Protection Commission](https://www.pdpc.gov.sg/organisations/regulations-decisions/regulatory-guidance/advisory-guidelines-on-the-do-not-call-provisions) | SG | 可达，社群、电话/短信营销的个人资料与 DNC 官方解释。 |
| `sg-ipos-trademark-guides` | [Intellectual Property Office of Singapore](https://www.ipos.gov.sg/about-ip/trade-marks/tm-guides/) | SG | 可达，商标指南、审查手册及正式法规转介。 |

## 法规来源覆盖差距（2026-10-24）

根据 `data/tea.sqlite3` 中现有采集项按来源和结果状态汇总：BPK 返回 403 的条目涉及 TAP MPR（6 项）、UU／Perppu／PP／Perpres（各 2 项）、Perda（4 项）、中央部委和机构法规（2 项）及地方首长规章（2 项）。这些是采集项数量，不代表不同法规数量或目录覆盖率。

UU、Perppu、PP 和 Perpres 已同时配置 Setneg 官方 JDIH 目录。当前采集记录分别有 78/68/78/78 项候选发现、40/34/37/46 项原件已存、4/0/0/0 项失败；同样是采集项累计状态，不能据此声称全国目录完整，也不能把原件已存等同于已解析、通过审核或已进入检索。BPK 仍作为独立核验来源保留。

目前未形成可用全国替代的类别：

- **TAP MPR**：MPR 官方 JDIH 提供按“Ketetapan MPR”过滤的列表、分页、详情与附件入口；详情包含制定日期、发布机关和附件记录。[官方过滤目录](https://jdih.mpr.go.id/dokumen/index2?id=KETETAPAN+MPR)、[TAP MPR 详情示例](https://jdih.mpr.go.id/dokumen/view?id=215)。2026-10-24 外部检索到的该分类页面显示 96 条 TAP MPR 记录；同日服务器受限 GET 返回 HTTP 403，外部页面打开也返回 403。尚未取得本机原始 HTML 和 PDF 回执，因此仍只记为主管机关确认的候选，不进入自动采集池。96 是目录显示数，不代表本项目应收数量或法规现行数。
- **Perda**：Bandung JDIH 仅覆盖 Bandung；本次对应 2 个失败采集项，不能代表全国省、市、县法规覆盖。[全国 DITJEN PP 数据库](https://peraturan.go.id/)是官方候选，但本机 HTTPS 握手超时，现有来源回执亦为不可达。
- **中央部委和机构法规**：当前 BPK 入口 2 项失败；来源池中的行业 JDIH 只能补对应机关和业务范围，不能代替全国中央机关清单。全国数据库候选仍受上述连接问题限制。
- **地方首长规章**：BPK 有 2 项失败，Bandung 有 2 项失败；Bandung 是单一城市来源，尚无可验证的全国级 Pergub／Perbup／Perwali 替代目录。

JDIHN 是成员 JDIH 文书的聚合目录，文件来源仍须回到发布机关核对。2026-10-24 官方检索索引中的一个详情记录展示了成员 JDIH、文号、年份、状态、原件名称及成员站点详情链接：[官方详情示例](https://jdihn.go.id/pencarian/detail/2203416)。但同日服务器侧对该详情执行受限 GET 后连接超时；聚合记录不能替代原始法规，也不能证明成员目录同步完整。暂只作为跨库发现候选，未加入无人值守来源池。

### 2026-10-24 服务器复核补充

本次从失败项逐条核对原始 URL，并对 DITJEN PP、JDIHN 与 MPR 官方候选各执行一次最多 2 MiB、15 秒截止的项目受限 HTTPS GET；没有写业务数据库。服务器结果分别为连接超时、连接超时、HTTP 403。浏览器/官方检索能看到页面，只能证明页面在该检索通道中有公开记录，不代表采集服务器能够合法、稳定地抓取；403 不通过换身份、伪造浏览器状态或代理绕过。三个候选均未被标记为已接入来源。

同日另对 [BPK 法规数据库](https://peraturan.bpk.go.id/)的[法规类型目录](https://peraturan.bpk.go.id/Jenis)执行一次隔离探测。官方目录按中央、地方及部门/机构类别提供法规发现入口；BPK 页面说明其记录来自相关政府机关官网，因此适合作为补充发现与交叉核验候选，不能替代制定机关原件或据聚合条目确认法律效力。服务器使用 `unattended_collection.py` 项目受限 HTTPS 传输、单来源/单请求、单主机并发 1、4 MiB 响应上限及独立忽略目录 `.dev/aitea-source-validation-20261024-bpk/`，返回 HTTP 403（`http_403`），未取得页面内容，未写正式来源状态或业务数据库。该次仅证明此精确入口当前受限，不推断整个 BPK 域名所有路径均不可达；候选暂不加入 16 项正式来源池，也不尝试绕过访问限制。

另发现业务相关度更高的[印尼检疫局 JDIH](https://jdih.karantinaindonesia.go.id/document/peraturan?bentuk_peraturan=PERBAN)：官方检索列有 Perbarantin 1/2024 和其 5/2025 修订；官方库中的植物检疫 HS 表包含茶类 0902，后续海关文件也引用这两项规则并列出茶叶单元及所需检疫文件。[修订原件](https://jdih.karantinaindonesia.go.id/storage/common/dokumen/Perba5.pdf)、[官方 HS 表与文件要求](https://jdih.karantinaindonesia.go.id/document/311/download)。这补出“茶类进口的植物检疫/许可”这一具体来源缺口；HS 子目、加工状态、包装与实际检疫义务仍须逐项核原文和适用范围，目录元数据不能代替法律审核。2026-10-24 对准确的 PERBAN 目录入口执行一次单请求隔离探测（20 秒、4 MiB 上限、正式有界 HTTPS 下载器、独立来源池和数据根目录 `.dev/aitea-source-validation-20261024-barantin/`），结果为连接超时（`network_timeout`），未取得页面或原件，也未写正式来源状态或业务库。候选暂不加入 16 项正式来源池、不绕过访问限制；待同一受限方式冷却后复测可达性，再决定是否纳入并限定检疫与茶类关键词。

### 2026-10-24 新加坡食品法规候选复核

为补足 SG-3.2（食品进口标签及官方转介的食品声称资料）中直接法规原文入口，核对了 [Singapore Statutes Online《Food Regulations》](https://sso.agc.gov.sg/SL/SFA1973-RG1?DocDate=20240520)。官方目录可见一般标签要求（第 5 条）、误导性标签陈述（第 9 条）、进口食品登记（第 14 条）以及茶、茶末/筛下物、速溶茶、冲泡茶（第 153–156 条）；官方检索结果可作为候选发现证据，不能替代具体版本正文、修订记录和生效状态核验。

服务器于 2026-10-24 使用 `unattended_collection.py` 的正式有界 HTTPS 获取逻辑对该准确入口执行 1 次隔离探测，返回 HTTP 403。回执、状态和事件保存在忽略目录 `.dev/aitea-source-validation-20261024-sso/`；该探测使用单独来源池和独立数据根目录，未改正式 16 来源配置、正式采集状态或业务数据库。该候选当前标记为“官方目录已发现、采集服务器受限、未取得原文”，不纳入正式来源池、不计作法规原件或覆盖完成，也不尝试绕过访问限制。后续仅在冷却期后按相同边界低频复查；若持续受限，继续保留 SFA 官方指引作为补充，并明确直接法规正文仍未验证。

### 2026-10-24 印尼农业部检疫法规来源候选

为验证茶类植物检疫来源缺口，核对了[印尼农业部 JDIH 的农业检疫法规目录](https://jdih.pertanian.go.id/peraturan?tematik=karantina-pertanian)。官方目录说明按“karantina-pertanian”主题列出法规；例如《农业部条例 1/2021》详情标为有效，主题为农业检疫，并列出检疫文件及其替代关系。该材料证明农业部 JDIH 有可供发现的检疫法规，但其具体茶类适用性仍须核对原文；检疫局目录与农业部历史职权也不能相互替代。

2026-10-24 01:34 UTC，服务器用项目 `_download` 有界 HTTPS 获取器对准确主题目录执行 1 次隔离 GET，超时 15 秒、响应上限 2 MiB，返回 HTTP 403，未取得内容或原件。隔离记录位于忽略目录 `.dev/source-validation-20261024-jdih-pertanian-probe/`；没有修改正式 16 来源配置、正式采集状态或业务数据库，也没有尝试绕过访问限制。该 URL 暂列官方候选，不进入无人值守池；冷却后仅按相同边界低频复测，若仍不可达则保留未覆盖缺口。

当前正式库的 113 个失败采集项应按故障阶段拆分：55 个 HTTP 403、25 个连接失败/超时、4 个重定向异常、4 个 HTTP 500、2 个不在清单路径/筛选范围、2 个原件额度异常、1 个 PDF 类型/签名不符，以及 20 个向量请求结果未知。最后 20 项是模型处理回执问题，不是来源站点失败。`peraturan.bpk.go.id` 的 22 个失败请求项包含 TAP MPR（6）、UU／Perppu／PP／Perpres（各 2）、Perda（4）、中央部委/机构（2）和地方首长规章（2）；这些仍是请求项数，不能用作独立法规计数。

## 本次探测证据

实际探测记录位于项目忽略目录 `.dev/unattended-collection-20261020/source-pool-audit/`。本次共 16 次 GET，最多 4 个并发，每个主机最多 1 个并发，每次 20 秒、2 MiB 上限，复用项目既有的 HTTPS、公网地址、重定向和体积限制。未使用模型、未写业务数据库、未绕过验证码或访问限制。

16 次探测中 12 次取得 HTML：其中 11 个入口有对应业务内容，1 个为 BPJPH 空目录；另 4 次失败为财政部 www/非 www 两个入口的 DNS 超时、税务总局及法律法规总局的连接失败。池只保留财政部一个正式入口，因此首批 15 个来源中为 11 个业务内容入口、1 个空目录和 3 个暂不可达入口。

`server-probe-summary.json` 保存前 14 次结果，`server-probe-extra-summary.json` 保存最后 2 次结果。每个来源另有 JSON 回执，成功内容有原始 HTML 和 SHA-256。探测的 `reachable_content` 是关键词与页面长度的初步校验；BPJPH 首页经人工复核被配置纠正为 `reachable_empty_catalogue`。真实原件完整性、法律效力及覆盖率不可从网络成功率推导。

## 失效与替换规则

连接失败、超时、403、人机校验及临时空目录分别记录并进入冷却；达到失败阈值后选取池内其他国家/主题相关来源，原有来源和未补原文缺口继续保留。404/410、长期 DNS 不存在或发布机关明确迁站可以作为归档候选，但单次连接异常和法规年代较早均不能证明网站或法规过期。

来源迁移应保存旧 URL、新 URL、官方迁站证据、核验时间和历史回执。只有来自主管机关或已经核实的官方资料库的候选才能补入正式来源池；候选网页和模型返回均为不可信输入，不能获得执行权限。来源切换不应删除旧原件、重置付费模型尝试或把未经审核的新材料默认为现行法律。

本池不承担对互联网任意域名的自动批准。新站点需要来源发现与官方身份核对，核对后纳入可审计配置；无人值守运行可在已批准的互补来源之间持续轮换。明确无文档的空目录不能持续作为成功业务任务消耗采集窗口。

## 保留历史有效的 Setneg 来源

切换服务前另核验国家秘书处原 PERPU 目录及缩小分页入口，共 2 次有界 GET，均返回 404。第 16 来源保留原 50 条分页入口和原 PERPU 范围，单页候选上限为 51（50 条加下一页），不扩大到其他法规类型。原 15 来源、请求账本、国家与额度均保持，配置升级清单保存旧/新摘要和唯一新增来源 ID。该来源历史成功与当前失败分别留证；其冷却不阻塞其他官方来源。

新增核验回执和旧33条目录的离线解析复核位于 `.dev/unattended-collection-20261020/source-pool-audit/setneg-catalogue-check/`。两次探测未取得新原件，不计入有效正文；全部16个来源由11个已取业务内容入口、1个空目录和4个暂不可达/待复查入口组成。

## 2026-10-24 来源池复核与扩展

通过项目现有 HTTPS 代理对 [DJBC 官方法规目录](https://peraturan.beacukai.go.id/)执行隔离轮询：入口返回 HTTP 200；采集回执记录 128 条来源引用、4 个内容版本事件和 3 个原件映射资源。正文 SHA-256 与 `source_observation` 回执 SHA-256 已写入 `id-beacukai-regulations`；回执和隔离原件保留在 `.dev/aitea-source-probe-beacukai-20261024/`。该入口加入印尼贸易主题，与贸易部法规目录交叉核对，并与新加坡海关进口来源建立跨境互补关系。网站说明其目录数据库仍在补充，因此只作为海关法规专题来源。

首轮自动轮询把 24 个页面保存在原始仓，但全部是 HTML 目录/查询页，按入库边界正确跳过，不能计为已抓取法规或已索引正文。代码现已针对该门户将 `jenis` 分类页、`detail/jenis` 文书页和官方文件子域的 `/referensi/YYYY/MM/<摘要>-<文件名>.pdf` 分开识别，并排除站点操作手册；旧目录页会从已校验缓存重新发现，无需重复 GET。来源当日 24 次请求额度已用完，原件抓取将在额度恢复后继续；首次实际法规 PDF 成功保存与入库情况以之后的 `collection-catalog` 状态为准。

财政部 JDIH、税务总局旧入口和国家法规总目录在既有官方身份核验后仍持续无法取得可用内容：财政部入口出现 10 次 TLS 验证失败，国家法规总目录 10 次网络超时，税务总局旧入口 9 次返回不可验证内容。三者均改为 `archived`，保留原 ID、关系、额度、请求回执、原件和健康记录。税务总局 `/index-peraturan/` 与 `/katalog-peraturan` 两个官方页面虽返回 HTTP 200，但项目采集器将其内容判为无法验证页面，故未加入正式清单。海关来源不能替代印尼税务目录，国家级法规总目录及税务来源的覆盖缺口仍然存在；后续仅在可验证官方内容恢复或找到可达的正式替代入口后再纳入。

更新后清单共 17 个来源节点：印尼 6 个活动、4 个归档；新加坡 7 个活动。新增来源通过项目代理回执核验；已归档来源不会被删除，也不会作为当前适用法规的自动替代品。
