# 无人值守官方来源池

配置为 [`config/collection-sources/id-sg-unattended.json`](../config/collection-sources/id-sg-unattended.json)，包含印度尼西亚 9 个、新加坡 7 个来源。`priority` 数值越大越优先，范围 1–100；默认复查间隔为 24 小时；明确为空的 BPJPH 新目录以 `empty_catalogue=true` 标记，7 天低频复查，不作为业务采集成功。每条固定国家、主题、资料性质、精确 HTTPS 主机和发现关键词，运行时根据来源健康状态选择下一个有业务价值的入口。抓取不得跳出这些来源所允许的主机，也不得把新闻、验证码、错误页面或商业整理作为法规正文。

本池服务于两份既有法律清单：印度尼西亚 29 项的正式法规、部门补强与国际协定，以及新加坡六堡茶进口经营、食品、税务、公司、个人资料、知识产权及贸易协定事项。食品和清真来源用于补足茶叶食品经营的监管法源，不表示扩采全部药品、化妆品、政务操作或所有行业材料。全部原件、状态和版本关系仍须通过既有采集、解析、审核和入库规则。

## 优先启动的互补来源

| 优先级 | 来源 | 用途 |
| --- | --- | --- |
| 100 | [ID 贸易部法规目录](https://jdih.kemendag.go.id/peraturan) | 进口、贸易规则、原产地及消费者保护 |
| 99 | [SG SFA 食品标签要求](https://www.sfa.gov.sg/regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines/labelling-requirements-for-food) | 茶叶食品标签、进口商责任与声称规则 |
| 98 | [ID BPOM 官方法规目录](https://jdih.pom.go.id/rule) | 食品注册、标签、包装与食品安全 |
| 97 | [SG Customs 进口概览](https://www.customs.gov.sg/doing-business/import-operations/import-procedures/import-procedures-overview/) | 商业进口、许可、估价、GST 与海关责任 |

这四个入口均已在当前服务器取得真实业务内容，适合首批并行运行。贸易部入口在另一次网页浏览中出现过拒绝请求，而服务器此次有界直连成功；不同访问结果分别保留，不能因此认定其网站已过期。

## 完整来源与边界

核验日期为 2026 年 10 月 20 日（北京时间）。每条精确抓取时间、来源性质、法律清单对应项、原始内容摘要校验和、网络回执及限制保存在配置的 `verification` 字段。下列链接同时作为主管机关身份与业务内容的官方证据。

| ID | 主管机关/入口 | 国家 | 用途和本次服务器结果 |
| --- | --- | --- | --- |
| `id-kemendag-regulations` | [JDIH Kementerian Perdagangan](https://jdih.kemendag.go.id/peraturan) | ID | 法规目录可达，含近期规范及状态标签；只按贸易法律问题收录。 |
| `id-bpom-regulations` | [JDIH BPOM](https://jdih.pom.go.id/rule) | ID | 法规目录可达，含食品包装等相关规则；排除无关药品/化妆品材料。 |
| `id-bpjph-nonhalal-regulation` | [BPJPH 非清真标识规则](https://bpjph.halal.go.id/read/keterangan-tidak-halal) | ID | 有业务内容，可定位正式规则原件入口；具体茶产品适用性待原文审核。 |
| `id-bkpm-regulations` | [投资与下游产业部/BKPM JDIH](https://jdih.bkpm.go.id/id) | ID | 可达，补投资、经营准入、KBLI 及许可法源。 |
| `id-kemenkeu-www-regulations` | [财政部 JDIH](https://www.jdih.kemenkeu.go.id/)，[官方海关估价规范实例](https://www.jdih.kemenkeu.go.id/dok/144-pmk-04-2022/summary) | ID | 本机 DNS 超时；官方当前页面仍可检索核到，属于暂不可达。 |
| `id-pajak-regulations` | [税务总局法规目录](https://pajak.go.id/id/peraturan) | ID | 本机连接失败；检索有近期税务文件，保留低优先级复查。 |
| `id-ditjenpp-regulations` | [法律法规总局数据库](https://peraturan.go.id/) | ID | 本机连接失败；作为正式法规备选核对入口。 |
| `id-bpjph-regulations` | [BPJPH 新版法律资料库](https://jdih.halal.go.id/)，[发布机关官方转介](https://bpjph.halal.go.id/berita/) | ID | 首页可达但明确显示尚无法规、无分类；空目录仅记录，不能计入正文采集。 |
| `id-setneg-perpu` | [国家秘书处 PERPU 目录](https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0) | ID | 保留旧任务的国家立法来源；历史取得 33 条目录、23 份记录的 24 份原件。本轮原入口和20条分页入口均404，记为待复查，不能认定域名过期。仅此固定范围启用既有Setneg适配器及受限JSON解压。 |
| `sg-sfa-labelling` | [Singapore Food Agency](https://www.sfa.gov.sg/regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines/labelling-requirements-for-food) | SG | 可达，食品标签和相应法规/官方指南转介。 |
| `sg-customs-import` | [Singapore Customs](https://www.customs.gov.sg/doing-business/import-operations/import-procedures/import-procedures-overview/) | SG | 可达，商业进口主体、许可和税费义务。 |
| `sg-iras-imports` | [Inland Revenue Authority of Singapore](https://www.iras.gov.sg/taxes/goods-services-tax-%28gst%29/claiming-gst-%28input-tax%29/importing-of-goods/) | SG | 可达，进口 GST、抵扣及 e-Tax Guides。 |
| `sg-enterprisesg-rcep` | [Enterprise Singapore RCEP](https://www.enterprisesg.gov.sg/grow-your-business/go-global/international-agreements/free-trade-agreements/find-an-fta/rcep) | SG | 可达，RCEP 与原产地/关税附件发现；外链不得自动跨域抓取。 |
| `sg-acra-compliance` | [Accounting and Corporate Regulatory Authority](https://www.acra.gov.sg/regulations/compliance-requirements/) | SG | 可达，经营主体和持续合规义务；排除无关金融主体/审计行业材料。 |
| `sg-pdpc-marketing` | [Personal Data Protection Commission](https://www.pdpc.gov.sg/organisations/regulations-decisions/regulatory-guidance/advisory-guidelines-on-the-do-not-call-provisions) | SG | 可达，社群、电话/短信营销的个人资料与 DNC 官方解释。 |
| `sg-ipos-trademark-guides` | [Intellectual Property Office of Singapore](https://www.ipos.gov.sg/about-ip/trade-marks/tm-guides/) | SG | 可达，商标指南、审查手册及正式法规转介。 |

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
