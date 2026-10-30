# 外部技能检查与茶渡版本

检查日期：2026-10-29。运行时只读取本目录，不依赖用户下载目录。来源名称均为调用别名；已有 legal、legal_lookup、contract_review、draft 标识保留，新增 litigation_analysis 和 long_image_to_pdf。

china-lawyer-analyst：来源为用户提供的同名目录，作者陈石律师，原入口声明 MIT，许可文本见 [MIT](licenses/MIT.txt)。保留已有来源说明，吸收法律解释、请求及事实对应方法；不复制案例数据库、全领域模块或其中的示例法条。方法文字不能证明示例条号现行或准确。

chinese-law-verifier：来源为用户提供的同名技能，公益接口提供者李伯阳律师。沿用现有精确条号工具，保留第三方身份、日期与版本缺项，不将接口成功解释为官方效力核验。

contract-review：保留现有 1.0.0 改编版本。只按实际交易与法域使用风险审查方法，外国法或比例示例不作为通用法律依据。

legal-writing：采用用户在对话中提供的允许完整起草和改写版本。磁盘副本为教学用途且限制代写，没有用其教学限制覆盖用户已经明确的功能要求，也未复制学生配置和学习记录逻辑。

litigation-analysis：检查的原包署名杨卫薪律师（微信ywxlaw），许可标为 CC-BY-NC-SA-4.0，并限制商业使用；包内许可正文还存在与共享改编相关的表述差异。原包文本、模板、领域参考和许可文件均未复制进运行目录。茶渡的同名入口为自行编写的材料读取与争议分析说明，复用已有事实、原文与文稿工具；来源目录的别名仅用于用户调用，不表示获得原包商业授权或作者背书。

long-image-to-pdf：原入口署名 ByronLeeeee、版本 1.0、声明 MIT，许可文本见 [MIT](licenses/MIT.txt)。原脚本的命令行与第三方排版依赖未复制；茶渡自行实现账号绑定、受限进程中的切片与 A4 排版，复用已安装的 Pillow、pypdf。保留重叠切片和行列排版用途，不使用原脚本的自由文件位置、允许截断图像或只凭 stdout 报成功的做法。

document-operations：根据用户提供的 `/Users/erics/Downloads/SKILL.md` 整理为应用内 `document_operations`。保留先读原件、定点修改、另存副本、重开检查与真实交付原则；格式说明按需读取，不复制固定执行步骤。新增真实文件适配工具，未实现的 PDF 原文编辑、扫描件转 Word、复杂版式转换和视觉验收均明确限制，来源文件未修改。

## 2026-10-30 开源法律方法融合

以下为固定提交的源码检查记录。运行时使用本目录内的茶渡改编文档，不加载上游仓库、用户下载目录或外部脚本。原有14个技能标识保留，新方法合并到现有入口；全文与参考资料通过 load_skill 按需读取。

| 项目 | 核对提交 | 许可及采用范围 |
| --- | --- | --- |
| [code-lawyer/Legal-Agent-Skills](https://github.com/code-lawyer/Legal-Agent-Skills) | [891684160a6bb972ccadfed11af490de6b5c6567](https://github.com/code-lawyer/Legal-Agent-Skills/tree/891684160a6bb972ccadfed11af490de6b5c6567) | MIT，Copyright (c) 2026 Tsinglaw Partners；合同风险、研究来源对应、模板填写状态的方法改编，许可见 [原文](licenses/legal-agent-skills-MIT.txt) |
| [LegalQuants/lq-skills](https://github.com/LegalQuants/lq-skills) | [884cf402949981ca2a4afb8ff88f19337005eecf](https://github.com/LegalQuants/lq-skills/tree/884cf402949981ca2a4afb8ff88f19337005eecf) | 项目 Apache-2.0，部分技能保留 MIT；按下表分别保留归属，项目许可见 [原文](licenses/legalquants-Apache-2.0.txt) |
| [anthropics/claude-for-legal](https://github.com/anthropics/claude-for-legal) | [4a6c651889c97cc9140580363c73e0eb17379c2b](https://github.com/anthropics/claude-for-legal/tree/4a6c651889c97cc9140580363c73e0eb17379c2b) | Apache-2.0；参考 commercial-legal 的 Practice Profile、采购/销售立场、修订追踪及业务摘要设计，自行编写适配说明；许可见 [原文](licenses/claude-for-legal-Apache-2.0.txt) |

LegalQuants 各方法的归属、依赖和融合位置：

| 上游技能与作者 | 许可 | 茶渡位置与采用方法 | 原包依赖与本轮边界 |
| --- | --- | --- | --- |
| foreign-law-research，Chong Liu / imchongliu | [MIT](licenses/foreign-law-research-MIT.txt) | legal / references/foreign-law.md；法源身份、适用时点、原语言材料 | 方法与 resources.md 资料导航；不附带外国法数据库，不照搬强制访谈或固定有效期 |
| sgcite，Yu Chou Teo | 项目 Apache-2.0 | legal_lookup / references/singapore.md；案件、当事人、段落与引文分开核对 | 入口提及外部 sgcite CLI；所检查目录仅 SKILL.md，无核验程序；未安装 CLI，不宣称已有 eLitigation 正文读取能力 |
| bart-statutory-reference-checker，Kevan Wee | 项目 Apache-2.0 | 同上；法名、条款、修订、废止与适用版本 | 目录仅 SKILL.md，描述私有 BART 后端、ChromaDB、Legal-BERT/GEMMA 与 Word 插件；均未引入 |
| proposition-checking，AnonLQ | [Apache-2.0](licenses/legalquants-proposition-checking-Apache-2.0.txt) | legal / references/research.md；区分来源存在、部分支持、矛盾与未能核对 | 保留缺原文时不得凭记忆评判的原则；不自动改写法律含义或增加审核服务 |
| adversarial-qc，Alexios van der Slikke-Kirillov | [MIT](licenses/adversarial-qc-MIT.txt) | 同上；在当前分析中检查不利材料、反例与条件遗漏 | 上游要求两个核验 Agent；只采用反证方法，不启动子 Agent、额外模型或复核证书 |
| coquill，Ang Hou Fu | [MIT](licenses/coquill-MIT.txt) | draft / references/template-fill.md；变量识别、复用已有信息、留存缺项 | 上游有 analyzer/renderer/transcriber 脚本，依赖 docxtpl、Jinja2、WeasyPrint、markdown，部分 PDF 转换依赖 Word/LibreOffice；未复制或执行，复用茶渡文档工具 |
| contract-qa，LegalQuants | 项目 Apache-2.0 | contract_review；具体问题直接回答，引用原条款与关联条件 | 无需全文审查；不照搬固定校准页脚与针对律师的交付限制 |
| comms-improver，LegalQuants | 项目 Apache-2.0 | guidance；保留主体、条件和例外的通俗表达 | 复用现有主 Agent，不增加文字润色模型 |
| legal-translation，Arjun Singh Chouhan | [MIT](licenses/legal-translation-MIT.txt) | draft / references/translation.md；术语一致、原意与法域差异 | 不附带翻译认证、外部服务或官方译本；不照搬“未指定双语复核者就不翻译”的前置限制 |
| redlines，Hou Fu Ang | 项目 Apache-2.0 | contract_review / references/revisions.md；修改定位、完整替换与对照交付 | 目录仅 SKILL.md，依赖另行安装的 @houfu/redlines；未安装。现有编辑不生成 Word 原生修订痕迹 |

Legal-Agent-Skills 的三个入口及其 references 已核查。contract-review 含中国/美国规则包与 Word 修订脚本，legal-research 含法规/案例 MCP 前置要求与固定终检，document-fill 含填写计划校验和 DOCX 渲染脚本。茶渡只改编研究、风险与溯源方法，未复制规则卡中的法条、数值、模板、脚本或完整报告。原生修订脚本会重建段落并影响字符格式，不替换现有定点编辑。保留 extracted、inferred、user_confirmed、ambiguous、gap、pending_drafting 的含义，但只在需要时用于本次上下文，不新增长期业务记录或强制 JSON 计划。

Claude for Legal 仅作设计参考：共享已有业务与谈判规则、区分采购/销售立场、追踪主合同与补充协议、按读者解释业务后果。未复制其公司档案、默认美国法、示例数值、审批矩阵、后台 Agent、MCP 配置或强制审核输出。

法律数据优先检索茶渡内部库，缺少相关原文、适用版本或材料时才补查外部官方来源；用户提供完整原文或明确要求核对外部来源时按本次要求处理。技能方法不授予新工具权限，也不证明已核验法律结论。当前法规工具支持 CN/ID/SG 且受业务范围约束，其他法域用已开放的公开资料查询说明实际取得范围。eLitigation 的非 gov.sg 域名、私有 BART 服务和原生 Word 修订均保留明确限制。

上游验证材料也已核对：Legal-Agent-Skills 带研究和填写结构验证、DOCX/修订脚本测试；LegalQuants 的 proposition-checking 带 evals.yaml，contract-qa 与 comms-improver 带 test-plan.md。未运行上游测试，未引入其依赖；茶渡使用自己的加载、权限、资源边界、模拟模板与真实模型测试。测试结果的范围与运行记录见 [Agent 运行机制](../../docs/AGENT_RUNTIME.md)。
