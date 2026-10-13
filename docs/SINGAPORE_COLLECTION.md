# 新加坡 PDPC 与 Customs 定向采集

## 来源与版本

`singapore-demo-20261009` 版本 4 仅补充 SG 3.1、3.6 已实取的官方入口与文件路径。16 项原要求、条目编号、排除范围和原 DOCX 的 SHA-256 保持不变。版本 3 的原字节保存在 `config/collection-plans/versions/singapore-demo-20261009.v3.json`；旧绑定、来源计划、任务和选取文件继续使用冻结规则。版本 4 另有同字节快照，不在恢复旧运行时隐式升级。

PDPC 当前公开站点在 HTML 内保存 Next Flight 数据。直接搜索到的旧 Key Concepts 和 Selected Topics 地址实取返回 404；当前正式栏目位于 `/organisations/regulations-decisions/regulatory-guidance/`。实际文件链接为同站 `/assets/<UUID>`，链接没有 PDF 扩展名。新适配只对明确启用 `limits.sg_official_2026=true` 的 SG 3.6 生效，读取四个指定指南栏目与 Managing Data Intermediaries 页面，不扩采决定库、咨询稿、金融或医疗专项指南。

适配只解码完整字面 `self.__next_f.push([1, "…"])`，不执行脚本、动态表达式或组件。JSON 正文中的 `data.content` 与已验证的 UTF-8 字节长度文本记录用于提取链接；模块导入、资源预加载、普通字符串和图像地址不能冒充正式 PDF。网页、数据流、单记录、块数、记录数、JSON 深度、对象数与链接数分别有上限。截断、未知协议、异常长度、验证或错误页均保留失败。定位包含原 HTML SHA、脚本位置及哈希、解码数据流哈希、记录字节范围和必要 JSON pointer；解码范围不冒充原始 HTML 字节范围。

Customs 新入口采用 `/doing-business/import-operations/`、`/doing-business/valuation-duties-and-fees/` 等路径。仅八个确切文章路径被登记为 HTML 正文候选，涵盖进口主体、许可、控制货物、GST、估价、留档及归类裁定；目录和其他商品或行业文章不因此变成正文。实际转介文件仅来自 `isomer-user-content.by.gov.sg` 的 `174/<UUID>/<文件名>.pdf`。该文件域不继承 Customs 文章或目录路径，其他 bucket、错误 UUID、额外查询参数均拒绝。

所有下载继续经过现有 HTTPS、公网地址、TLS、重定向、字节数、截止时间及并发限制。新链接、原生文本和标题均不证明法律效力或业务适用性；正文仍须检查图片、表格及解析完整性。

## 实际验证与已知缺口

服务器私有证明位于 `data/verification/sg-official-gap-20261013/`。本次有界选取 19 件：PDPC 九份 PDF、Customs 八份 HTML 和两份 PDF，共 14,915,596 字节。19 件原件哈希与受限解析源哈希均核验；PDF 共 357 个物理页，其中 22 页待识别，已保存 22 张实际受限渲染页图及原件、物理页、图像哈希与尺寸映射。HTML 的 11 处 literal 图像引用包括重复站标和三个业务图表，仍需按正文范围核对，不将替代文字当作图中完整文字。

PDPC 当前文件的原生封面显示 Key Concepts 修订为 29 April 2026、Selected Topics 为 May 2024；DNC、营销同意及委托处理材料另保留各自的原文件名和封面。年份和原生读取结果不能代替版本、生效或适用期审核，旧搜索摘要不能覆盖当前原件。

用途分级与法规法阶分开。本次工程选取以完整指南作为一级，Key Concepts 的 Annex A/B/C、委托处理 primer 和 Customs 两份配套说明作为二级；二者的文件角色均可解析。关联类型为 `related`，两端原件 SHA 和官方页面 SHA 固定保存，不将官网链接推定为法定授权、修订或已核引文。二级向量为零。普通上传附件的 `role=secondary` 继续只存原件。

独立 CLI 发现证明位于 `data/campaigns/sg-pdpc-official-v4-20261013/`：七页实际来源发现九个候选，零失败和零未处理入口。另一独立选取运行 `data/campaigns/sg-pdpc-official-v4-selected-20261013/` 取得九件原件、一级五件、二级四件及四条已保存版本关联；其模型关闭的初始快照中仅 primer 一件通过文本完整性，向量为零；后续真实模型处理另写 `model-windows/`，不改该快照或冻结的方案选项。两个账号及数据根分别盘点，不累加为法规覆盖数量；“发现范围完成”不代表 SG 3.6 已满足所有验收要求。

PDPA、Customs Act、GST 法规及 RIEA 的正式正文、适用修改和法律审核仍待补；会员登记、营销、退订、跨境访问的实际处理事实仍须核实。茶的配料、加工、包装与当地完整 HS 归类尚未确认，不照抄中国 HS 尾码，也不把个人包裹规则当作商业进口豁免。上述有界选取不能代替两项全部必要内容或整份 45 项清单的验收。

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
