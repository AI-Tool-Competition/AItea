# 无人值守官方来源池

配置为 [`config/collection-sources/id-sg-unattended.json`](../config/collection-sources/id-sg-unattended.json)，当前共 27 个稳定来源节点：印度尼西亚 16 个（11 个活动、5 个归档），新加坡 11 个（全部活动）。`priority` 数值越大越优先，范围 1–100；默认复查间隔为 24 小时。BPJPH 历史首页为空并已归档，当前 `/cari` 法规目录处于活动状态。每条固定国家、主题、资料性质、精确 HTTPS 主机和发现关键词，运行时根据来源健康状态选择下一个有业务价值的入口。抓取不得跳出这些来源所允许的主机，也不得把新闻、验证码、错误页面或商业整理作为法规正文。

配置采用 `aitea-official-source-pool-v2`：`groups` 是国家根节点及主题子节点，子节点列出来源 ID 和对应核验清单项；来源自身保留正式入口、访问证据、主题、发现策略、优先级、轮询间隔和 `active` 状态。活动源须留有官方身份、业务用途、清单范围、近期开页回执与正文 SHA-256；空目录单独标记并低频复查。`source_relations` 是有方向的关系边，区分 `complements`（互补）、`cross_checks`（交叉核验）、`implements`（实施规则）与 `publishes_original`（正式原件发布），并说明适用清单项和关系理由。所有活动源都明确使用 `transport: proxy`；watchdog 将其精确允许主机并入代理白名单。归档用 `status: archived` 移出运行队列，原节点、关系和所有历史账本均保留；新增来源必须经有界代理抓取取得有效正文后才能登记。

本池服务于两份既有法律清单：印度尼西亚当前 v18 的 17 项配置任务（v17 的 16 项、v16 升额前配置、v14 的 17 项、v13 的 19 项、v12 与原 29 项 v11 范围分别保留历史快照），以及新加坡六堡茶进口经营、食品、税务、公司、个人资料、知识产权及贸易协定事项。v18 新增 BPJPH 清真目录适配器，按页读取公开数据并从对应详情核验原件；代理观察总数 172 条、18 页，已验证前两页结构。BPOM 仍代理轮询但未绑定正式任务，待其正式分页和原件路径解析通过验证。新加坡当前 v7 有 15 项活动任务；v6 的 12 项状态和 v5 的 16 项定义均保留不可变快照。项目代理于 2026-10-25 重新取得 SSO Current Acts 与 Current Subsidiary Legislation 目录，现行主法、附属法例和宪制文件任务 1.1–1.3 已恢复；SG-LegalCite 页面与 API 仍超时，案例数据 2.1 保留为覆盖缺口。政府采购 JDIH 与 RCEP 官方入口连续超时或无法解析，已从活动清单移除并保留历史配置。食品和清真来源用于补足茶叶食品经营的监管法源，不表示扩采全部药品、化妆品、政务操作或所有行业材料。全部原件、状态和版本关系仍须通过既有采集、解析、审核和入库规则。

## 优先启动的互补来源

| 优先级 | 来源 | 用途 |
| --- | --- | --- |
| 100 | [ID 贸易部法规目录](https://jdih.kemendag.go.id/peraturan) | 进口、贸易规则、原产地及消费者保护 |
| 99 | [SG SFA 食品标签要求](https://www.sfa.gov.sg/regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines/labelling-requirements-for-food) | 茶叶食品标签、进口商责任与声称规则 |
| 98 | [SG SFA 商业食品进口要求](https://www.sfa.gov.sg/food-import-export/commercial-imports/what-you-need-to-know-for-import-of-food) | 食品进口登记、许可及类别要求 |
| 98 | [ID BPOM 官方法规目录](https://jdih.pom.go.id/rule) | 食品注册、标签、包装与食品安全 |
| 97 | [SG Customs 进口概览](https://www.customs.gov.sg/doing-business/import-operations/import-procedures/import-procedures-overview/) | 商业进口、许可、估价、GST 与海关责任 |
| 89 | [SG CCS 公平交易指引](https://www.ccs.gov.sg/consumer-protection/fair-trading-practices/) | 销售宣传、误导性陈述、价格透明和消费者公平交易 |
| 82 | [ID 海关总署法规目录](https://peraturan.beacukai.go.id/) | 印尼进出口、关税、海关及消费税规范 |

这七个入口均已在当前服务器取得真实业务内容，适合按优先级和额度轮询。CCS 规范地址于 2026-10-24 经项目代理返回 HTTP 200，正文通过现有 HTML 结构发现器校验；首轮缓存了 4 个同站页面并留下 8 个待检链接。后续轮询已发现公平交易、暗黑模式、价格透明及行业指引等关联页面；配置以精确路径列出 7 篇已核验的 HTML 指引，作为正式资料入库，其他目录、FAQ 和公开登记页仍只保留发现记录。印尼海关目录于同日经项目代理成功获取，配置回执记录页面正文和来源事件 SHA；目录自述仍在更新，不能据此宣称覆盖完整。贸易部入口在另一次网页浏览中出现过拒绝请求，而服务器此次有界请求成功；不同访问结果分别保留，不能因此认定其网站已过期。

运行账本曾观察到 BPOM 的食品包装关键词同时命中药品撤回规则，以及新加坡海关进口入口发现个人随身物品、贵金属和出口流程。来源清单现在用 `discovery_exclude_keywords` 明确排除这些非目标主题；正向食品/商业进口关键词仍保留。变更策略会用已校验的缓存根页无网络重放，并在来源事件中记录受排除候选，既不重置日额度也不删除原始回执。

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
| `id-bpk-regulations`（归档） | [印尼审计委员会法规数据库](https://peraturan.bpk.go.id/) | ID | 受限入口返回 HTTP 403；保留全国目录覆盖缺口和旧回执，不绕过访问限制。 |
| `id-bpjph-regulations`（归档） | [BPJPH 新版法律资料库](https://jdih.halal.go.id/)，[发布机关官方转介](https://bpjph.halal.go.id/berita/) | ID | 首页可达但明确显示尚无法规、无分类；空目录仅保留低频复查历史，不计入正文采集。 |
| `id-bpjph-jdih-cari` | [BPJPH 法规检索目录](https://jdih.halal.go.id/cari) | ID | 经有界请求取得可解析目录和文书候选；只收清真及食品标签相关规则，不代替正式原件和适用性审核。 |
| `id-setneg-uu` | [国家秘书处 UU 目录](https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=UU&length=50&start=0) | ID | 2026-10-25 经项目代理 POST 返回 HTTP 200；目录总数 901，首批 50 条通过类型、总数和唯一 ID 校验，抽样详情发现 1 个官方原件入口；对应 ID-1.3。 |
| `id-setneg-perpu` | [国家秘书处 PERPU 目录](https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0) | ID | 2026-10-25 经项目代理 POST 返回 HTTP 200，目录总数 33，33 条全部通过校验，抽样详情发现官方原件入口；恢复原稳定 ID 并保留此前 404、历史成功和原件账本；对应 ID-1.4。 |
| `id-setneg-pp` | [国家秘书处 PP 目录](https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PP&length=50&start=0) | ID | 2026-10-25 经项目代理 POST 返回 HTTP 200；目录总数 2,610，首批 50 条通过校验，抽样详情发现 1 个官方原件入口；对应 ID-1.5。 |
| `id-setneg-perpres` | [国家秘书处 Perpres 目录](https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPRES&length=50&start=0) | ID | 2026-10-25 经项目代理 POST 返回 HTTP 200；目录总数 2,453，首批 50 条通过校验，抽样详情发现 1 个官方原件入口；对应 ID-1.6。 |
| `sg-sfa-labelling` | [Singapore Food Agency](https://www.sfa.gov.sg/regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines/labelling-requirements-for-food) | SG | 可达，食品标签和相应法规/官方指南转介。 |
| `sg-sfa-commercial-import` | [Singapore Food Agency：商业食品进口要求](https://www.sfa.gov.sg/food-import-export/commercial-imports/what-you-need-to-know-for-import-of-food) | SG | 2026-10-26 经项目代理返回 HTTP 200；补进口商登记、食品类别和进口许可证流程，对应 SG-3.1/SG-3.2，并与 SFA 标签及新加坡海关来源互补。 |
| `sg-sso-acts-current` | [Singapore Statutes Online：Current Acts](https://sso.agc.gov.sg/Browse/Act/Current/All) | SG | 2026-10-25 经项目代理返回 HTTP 200；按页500条发现现行主法，并承载 1.1 的 Constitutional 子类过滤。目录行不等于正文入库或有效性审核。 |
| `sg-sso-statutes` | [Singapore Statutes Online：Current Subsidiary Legislation](https://sso.agc.gov.sg/Browse/SL/Current/All) | SG | 2026-10-25 经项目代理返回 HTTP 200；按页500条发现现行附属法例。目录行不等于正文入库或有效性审核。 |
| `sg-ccs-fair-trading` | [Competition and Consumer Commission of Singapore](https://www.ccs.gov.sg/consumer-protection/fair-trading-practices/) | SG | 新加坡政府官方公平交易页面；补充误导宣传、价格透明及销售行为指引，对应 SG-3.5，与 SFA 标签规则互补。页面正文标注内容更新于 2025-09-26，仍按日轮询并保留后续版本。 |
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

### 2026-10-26 印尼检疫来源冷却后复测

复测仍使用项目有界 HTTPS 获取器和已配置 CONNECT 代理，单主机并发 1、只发 GET、独立忽略目录 `.dev/aitea-source-recheck-20261026-042025/`，未接触正式来源账本、业务数据库或模型。农业部主题目录仍返回 HTTP 403，未取得页面。

检疫局 JDIH 的 PERBAN 目录返回 HTTP 200，取得 113,876 字节 HTML（SHA-256 `a94827d177d289c635d64b19ff576b0158dbd9fe50e196dd14cd3e8bed250a6f`）。对保存的原页使用项目 `link_discovery` 离线解析，可复核 10 个法规详情链接，其中包括 Perbarantin 1/2024 与其 5/2025 修订；两条详情页也分别返回 HTTP 200。原页和详情页解析仅证明官方目录与记录页面可读取，不代表原件已经入库。

从两条详情页发现的原件端点 `/document/315/download` 和 `/document/300/download` 均返回可重试 HTTP 502，未取得原件字节。两次失败记录及详情页 SHA、解析结果保存在上述隔离目录。由于当前未能取得并校验法规原件，该候选仍不进入正式来源池；不把摘要 PDF 当作法规正文，也不绕过 502。后续按退避间隔复测原件端点，只有至少一份正式原件成功取得并通过类型、签名和解析校验后，才评估来源配置与茶类适用范围。

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

截至 2026-10-24 的清单版本共 20 个来源节点：印尼 6 个活动、6 个归档；新加坡 8 个活动。来源活动状态只代表能从官方入口取得并校验内容，不代表清单覆盖完整、正文已入库或法律已审核发布。已归档来源保留历史节点和回执，不会作为当前适用法规的自动替代品。

### 2026-10-24 印尼官方备选目录代理复核

为补查劳动、最高法院及财政税务来源缺口，使用项目同一有界代理和独立采集根目录各探测一次官方入口：财政部 JDIH `https://jdih.kemenkeu.go.id/direktori` 与人力部 JDIH `https://jdih.kemnaker.go.id/beranda` 均返回 HTTP 500；最高法院 JDIH `https://jdih.mahkamahagung.go.id/dokumen?page=1` 返回 HTTP 403。原始隔离状态保存在 `.dev/aitea-source-validation-20261024-official-expansion-1792857342471071947/ID/`。三者均未取得可验证正文，不进入正式来源池；财政部税务、劳动及司法文件覆盖仍有缺口。此结果仅记录本次精确入口和代理状态，不推断整个机关网站不可用。

### 2026-10-24 新加坡公平交易来源迁站复核

旧入口 `www.cccs.gov.sg` 已重定向到 CCS 现用的 `www.ccs.gov.sg` 域名；旧入口因目标主机未在该来源的精确白名单而被正确拒绝。对主管机关的规范页面 `https://www.ccs.gov.sg/consumer-protection/fair-trading-practices/` 重新使用项目代理取得 HTTP 200。正式源只允许精确主机 `www.ccs.gov.sg`，并通过 `html_document_paths` 显式收录 7 篇已核验页面：公平交易基础、暗黑模式、CPFTA 指引、价格透明、化妆品行业、装修行业和 CPFTA 概览。它们从已缓存的 CCS 页面发现，无需扩大文档主机白名单；父页面、目录页、FAQ 和公开登记页不会因为同站链接而自动进入检索。正文图片另以 `html_asset_hosts` 精确批准政府 Isomer 静态资源主机；迁移须证明图片网址确实出现在已缓存、带不可变版本事件的 CCS 原文中。入库后只下载经结构映射确认的独立 `img/src`，经项目代理绑定原件 hash，再进入现有视觉 OCR 队列；响应式、脚本生成及未批准主机资源继续排除。此前被记为 `catalogue_page_not_document` 的 7 篇配置页面会由导入器按新清单自动复查，已有图片不完整的页面也会在保留 OCR 检查点的条件下自动补齐，其余未批准路径保持跳过。隔离回执及原件位于 `.dev/aitea-source-validation-20261024-ccs-1792857419275869076/SG/`；来源事件 SHA-256 为 `f9d5adcca1f65110ab817e80988205a0bca631bf8efab2c2b39537e3fba83d42`，正文 SHA-256 为 `afaee45d26e577e05bd1b0b5f6fafbdb43a150647cd9aa251852a4a9470c3a4f`。该节点归入 SG-3.5，并与 SFA 标签来源建立互补关系。CCS 页面是主管机关指引而非法规正文，仍须回到现行法规核对；已抓取或后续变化的内容均保持待审核状态。

### 2026-10-25 新加坡 SSO 现行法目录恢复

2026-10-24 的 SSO 403 保留为历史观测。2026-10-25 通过项目 HTTPS 代理重新取得官方 Current Acts 与 Current Subsidiary Legislation 目录，均为 HTTP 200：

- [Current Acts](https://sso.agc.gov.sg/Browse/Act/Current/All)：526 条、每页 500 条、共 2 页；正文 SHA-256 `6511278f4396493a0fc70972a517981109d24b4b12a98fd66be245b44a5e1e7a`。
- [Current Subsidiary Legislation](https://sso.agc.gov.sg/Browse/SL/Current/All)：5,872 条、每页 500 条、共 12 页；正文 SHA-256 `e4cd754fc669de8c2b86440522f145d93532053e34a957dbdc8f6ca26a11beaf`。

经有界目录解析、稳定法规身份、来源行定位和来源池迁移后，两项已加入活动来源池；清单 v7 恢复 1.2 与 1.3，并将 1.1 宪制文件作为 Current Acts 下的 Constitutional 子类过滤。三项任务分别覆盖宪制文件、现行主法与现行附属法例，置于“现行主法与附属法例”层级；其中主法与附属法例来源和清单关联互补，食品标签、海关进口来源用于业务交叉核验。代理白名单没有放宽，浏览器请求头仅对该精确官方主机启用。目录页按页面完整校验，超过 128 条活动候选容量的行会从保留的目录快照分批补入；单来源每日额度与主机冷却仍生效。

本次证明的是官方目录入口和目录响应可达，不是 526 或 5,872 份法规正文均已下载、解析或建向量。目录记录 `Current` 也不是适用性或法律效力结论。两份隔离 `source_observation` 回执与原始页面保存在 `.dev/aitea-sso-registration-97f2eb8161d8/SG/`；正式运行后的分页、原件下载和索引进度以 `data/official-updates/SG/` 事件账本及 `make collection-catalog` 为准。

### 2026-10-25 Setneg 四类目录恢复

2026-10-20 的 Setneg PERPU HTTP 404 作为历史观测保留。2026-10-25 通过同一项目代理按官网当前 JSON POST 结构重新核验 UU、PERPU、PP、Perpres 四个固定目录；每类首批目录均由现有解析器校验类型、稳定 ID、总数与分页，且各抽样一条详情通过匹配并发现官方原件入口。首页观测总数分别为 901、33、2,610、2,453；这只是目录快照，不代表所有原件均已取得或进入索引。

常驻采集器现按来源的 `setneg_type` 生成只读目录/详情 POST 请求体，原件仍通过原有受限 GET 下载。Perppu 继续使用原稳定 source ID，保留旧失败计数、缓存与历史原件；UU、PP、Perpres 分别新增独立来源节点。四类分别对应 ID-1.3 至 ID-1.6，并在层级清单中互相标为不同文种的互补目录，不据此推断法规范位阶。探测正文和 `source_observation` 回执位于 `.dev/aitea-setneg-20261025/`。

`scripts/migrate_unattended_source_pool.py` 为印尼与新加坡保留了原账本并迁移清单摘要，随后恢复 `aitea-collection.service`。采集器按每日来源额度、每轮请求上限、主机并发和代理白名单继续轮询；4 个类型的首屏发现不等于完整收齐，目录后续分页、详情、原件、解析和索引状态分别记录。法律效力、修订关联和适用期间仍须人工/业务复核。

### 2026-10-25 印尼劳动法规目录接入

[人力部 JDIH `/peraturan`](https://jdih.kemnaker.go.id/peraturan) 经项目代理返回 HTTP 200，服务器渲染的目录包含分页、法规详情和 `/asset/data_puu/` 官方原件链接。此前 `/beranda` 的 HTTP 500 仅代表旧首页异常，不等于当前法规目录不可用。来源绑定清单 ID-4.4，归入“劳动、雇佣与劳动条件”，并与国家层级 UU、PP 目录互补；来源关系用于回查，不据此判断法律位阶或适用性。

采集器只识别 `/peraturan` 与有界 `?hal=N` 分页、符合精确 ID/slug 形状的详情页，以及 `/asset/data_puu/` 下的 PDF/DOC 原件；新闻、词汇表、培训、地方规定、裁判文书和其他无关页面排除。原件单件上限 128 MiB、单日来源额度 256 MiB、单次超时 900 秒；普通目录页仍使用全局 8 MiB 响应上限。入口、回执和正文 SHA-256 已写入来源清单。此项验证证明入口和结构可抓取，不代表劳动法规已经完整收集、解析或完成适用性审核。

### 2026-10-26 新加坡 SFA 商业食品进口指南

[SFA 商业食品进口要求](https://www.sfa.gov.sg/food-import-export/commercial-imports/what-you-need-to-know-for-import-of-food) 使用项目代理取得 HTTP 200、21,821 字节官方 HTML；隔离探测正文 SHA-256 为 `90e532651767342c2c390c997fdf4cd1edd9aae340f443f459212a49f0ab8c32`。受限离线解析提取 5 条食品进口、许可与申请流程链接，排除了两个无关的机构代码表；正式 SG 采集器随后也成功取得页面并留下版本缓存，之后进入来源每日额度冷却。

来源 `sg-sfa-commercial-import` 纳入 `sg-food-trade`，对应 SG-3.1/SG-3.2，并分别与 SFA 标签指南、Singapore Customs 建立互补关系：SFA 指引负责食品类别、登记和许可证要求，海关来源覆盖进口申报；不能相互替代。来源池摘要迁移保留了原轮询历史，SG 采集任务现已按新摘要恢复。该页是行政指引，不是产品准入结论，也不代替法规原文、HS 分类及产品配方的适用性复核。

### 2026-10-26 当前来源池清单快照

仓库中的 `aitea-official-source-pool-v2` 配置现有 27 个来源节点：印尼 11 个活动、5 个归档；新加坡 11 个活动；两国分属 2 个主题组，共记录 22 条来源关系。归档节点保留历史与关系，不参与当前轮询。该计数描述清单配置，不表示各来源此刻都可访问、已取得法规原件、解析完成或法律审核通过；尚未取得原件的 Barantin 候选不在这 27 个节点内。
