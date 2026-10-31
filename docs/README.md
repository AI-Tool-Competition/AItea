# 文档中心

[返回项目首页](../README.md) · [工程规范](../AGENTS.md)

从操作目标选择文档。当前解析与检索入口以 **MinerU API + RAGFlow** 为准；历史 OCR、向量恢复工具和带日期的验收记录用于追溯，不代表当前在线配置、数据规模或服务状态。

## 首次使用与开发

| 文档                             | 解决的问题                                  |
| -------------------------------- | ------------------------------------------- |
| [演示指南](DEMO_GUIDE.md)        | 体验工作台与示例业务材料                    |
| [开发环境](DEVELOPMENT.md)       | uv/pnpm、启动停止、热更新、独立工作树及排错 |
| [部署说明](DEPLOYMENT.md)        | 本机生产预览、容器配方及实际部署边界        |
| [本机 Docker](DOCKER_HOST.md) | 无管理员权限的 Docker-in-gVisor、单 UID 映射与运行边界 |
| [2026-10-30 Docker 部署验证](DOCKER_VALIDATION_20261030.md) | 分支集成、已有资料校验、容器流式实测及挂载阻塞 |
| [测试范围](TESTING.md)           | 日常检查、测试隔离与保留的风险覆盖          |
| [后端目录](../backend/README.md) | 按业务定位实现文件及 DDD 分层职责           |

## 办理与法务配置

[Agent 调度与记忆](AGENT_RUNTIME.md)：单一主 Agent、法律取证、业务事务、按需技能、后台进程与执行预算。

[2026-10-30 跨场景稳定性记录](AGENT_STABILITY_20261030.md)：流停滞恢复、文件交付、引用关联、真实模型失败与代码检查。

[咨询与业务卡联动记录](CONSULTATION_CARDS_20261029.md)：咨询设定、主动填写、来源与事务、真实模型结果及未通过项。

[法律检索研究及复测](LEGAL_RETRIEVAL_RESEARCH_20261029.md)：论文、作者项目、下载版本与许可，条款选择故障、原文上下文及独立解释测试。

| 文档                              | 解决的问题                                   |
| --------------------------------- | -------------------------------------------- |
| [六堡茶流程](LIUBAO_WORKFLOWS.md) | 四条路线、任务编排和材料要求                 |
| [配置后台](ADMIN_STUDIO.md)       | 初始管理账号、账号开放、流程编辑、调试与发布 |
| [节点工作区](NODE_WORKSPACE.md)   | 业务、节点、对话与共享事实如何协作           |
| [法律研究与历史报告](LEGAL_AGENT.md) | 保留的人工研究、事实管理及报告修订入口       |
| [国别法律资料库](COUNTRY_LEGAL_LIBRARY.md) | 中国、印尼、新加坡资料包、原文检索及导入恢复 |
| [2026-10-29 中国法技能接入](CHINESE_LAW_INTEGRATION_20261029.md) | 精确法条工具、分析方法、接口退回及简单咨询实测 |
| [2026-10-28 四题实测](LEGAL_USER_TEST_20261028.md) | 独立会话的完整问答、评分及法律分析优化记录 |
| [2026-10-29 Agent 运行实测](AGENT_RUNTIME_TEST_20261029.md) | 任务识别、专业结果整合、真实模型答复及未通过项 |
| [2026-10-29 固定四题回答原文](AGENT_RUNTIME_FOUR_ANSWERS_20261029.md) | 最后一轮第 3、4、5、8 题的用户问题、耗时和完整回答 |
| [2026-10-29 按需能力与模型切换验收](DEMO_ACCEPTANCE_20261029.md) | Skills、工具搜索、MCP实测、Nemotron切换及未通过项 |
| [2026-10-29 业务验收失败原因](AGENT_ACCEPTANCE_FAILURES_20261029.md) | 逐题调用证据、工具发现复现、超时与交付入口缺陷及接口诊断 |
| [2026-10-29 执行控制改造](AGENT_EXECUTION_CONTROL_20261029.md) | 计划校验、确定性工具调用、交付状态、真实六题失败及独立引擎耗时 |
| [2026-10-29 单Agent与证据服务重构](AGENT_EVIDENCE_REFACTOR_20261029.md) | 执行器精简、24题三轮记录、协议修复与未通过的法律验收 |
| [2026-10-29 五题与商业题完整回答](DEMO_ANSWERS_20261029.md) | 当前Nemotron及前一配置的实际用户正文，保留失败结果 |
| [Agent 工具](AGENT_TOOLS.md)      | 实际工具调用、流式过程、失败预算与取消       |
| [配置目录](../config/README.md)   | 流程文件、来源清单及发布快照的关系           |

## 架构、接口与数据

| 文档                                   | 解决的问题                                 |
| -------------------------------------- | ------------------------------------------ |
| [系统架构](ARCHITECTURE.md)            | 前后端装配、状态边界和外部能力接入         |
| [2026-10-29 最新代码重建](SYSTEM_REBUILD_20261029.md) | 远端基线、旧代码归档与删除、新实例迁移及测试结果 |
| [接口契约](API_CONTRACT.md)            | 请求、响应、分页、修订冲突及事件约定       |
| [数据体系](DATA_PLATFORM.md)           | 共享法规、私有资料、版本和权限范围         |
| [系统法规总库](SYSTEM_LIBRARY.md)      | 系统资料归属、导入权限和审核门禁           |
| [实例管理](INSTANCE_MANAGEMENT.md)     | 配置归集、统一路径、成套备份及跨目录恢复   |
| [备份协议](DATA_BACKUP.md)             | SQLite 快照、原件引用校验和恢复边界        |
| [资料存储](STORAGE.md)                 | 原件位置与可读资料目录的导出               |
| [资料目录](COLLECTION_MATERIALS.md)    | 原件、文本、关系、索引状态和采集缺口的查找 |
| [总库迁移](SYSTEM_CORPUS_MIGRATION.md) | 向已有实例合入系统资料及中断续接           |

## 当前采集、解析与检索

先看 [持续采集](UNATTENDED_COLLECTION.md) 与 [MinerU/RAGFlow](MINERU_RAGFLOW.md)，再按来源或故障查看专题。来源配置默认位于 `config/collection-sources/`；恢复实例的活动路径以 `make instance-paths` 输出为准。

| 文档                                            | 解决的问题                                   |
| ----------------------------------------------- | -------------------------------------------- |
| [持续采集](UNATTENDED_COLLECTION.md)            | 初次采集、周期更新、版本留存和服务恢复       |
| [MinerU 与 RAGFlow](MINERU_RAGFLOW.md)          | 精准解析、结构化证据、本机检索服务和云端模型 |
| [解析与采集边界](INGESTION.md)                  | HTML、文件角色、资源隔离与历史解析兼容边界   |
| [官方来源池](OFFICIAL_SOURCE_POOL.md)           | 来源覆盖、关联关系与访问约束                 |
| [有界资源采集](RESOURCE_COLLECTION.md)          | 指定范围的服务器采集与续跑                   |
| [周期官方采集](PERIODIC_OFFICIAL_COLLECTION.md) | 固定来源的周期检查、停止和重启               |
| [印尼来源](INDONESIA_OFFICIAL_COLLECTION.md)    | 官方目录、原件发现和续采                     |
| [新加坡来源](SINGAPORE_COLLECTION.md)           | 官方目录、eGazette 和历史原件                |
| [ACFTA 来源准入](ACFTA_SOURCE_ADMISSION.md)     | 精确原件定位、国家和版本边界                 |
| [采集成果入库](COLLECTION_IMPORT.md)            | 将服务器上的采集成果导入应用                 |
| [采集持久恢复](COLLECTION_RECOVERY.md)          | 下载、处理状态与重试边界                     |
| [官方来源中断恢复](OFFICIAL_SOURCE_RECOVERY.md) | 有界采集窗口的恢复条件与启动                 |
| [来源响应凭证](SOURCE_RECEIPTS.md)              | 响应复用、封装原件和来源映射                 |
| [采集质量](INGESTION_QUALITY.md)                | 质量检查入口与验收依据                       |

## 历史证据与专用恢复工具

以下文档对应历史解析、固定计划或特定证据修复。使用前核对文档中的数据根、输入指纹、授权范围和恢复条件，不将其启动命令套用到新实例或当前自动采集队列。

| 文档                                             | 用途                               |
| ------------------------------------------------ | ---------------------------------- |
| [原生来源映射](NATIVE_SOURCE_MAPPING.md)         | 原生与转换资料的定位含义和映射盘点 |
| [分页证据恢复](ARCHIVE_PAGE_RECOVERY.md)         | 已有归档的分页原生证据恢复         |
| [超大 PDF 分片](ARCHIVE_PAGE_SHARDS.md)          | 超大文档原生页证据的分片与续跑     |
| [物理页 OCR 恢复](PHYSICAL_PAGE_OCR_RECOVERY.md) | 扫描页范围误判后的有界修复         |
| [固定范围识别](OCR_OPERATIONS.md)                | 固定 OCR 计划与历史识别结果恢复    |
| [向量恢复与并发](EMBEDDING_OPERATIONS.md)        | 旧向量计划、共享额度及断点证据     |
| [OCR 向量交接](OCR_EMBEDDING_HANDOFF.md)         | 已完成 OCR 固定范围的受控向量交接  |
| [早期资料接入设计](INGESTION_RESEARCH.md)        | 资料结构与一级／二级定义的设计背景 |

## 历史验证与设计记录

这些记录只证明所列日期、代码、来源和输入范围下的结果。确认当前版本是否通过检查，应查看对应提交的 [GitHub Actions](https://github.com/AI-Tool-Competition/AItea/actions/workflows/check.yml) 及本次本地记录。

- [工作台验收](VERIFICATION.md)
- [采集可用性与并发验证](COLLECTION_VALIDATION.md)
- [两国扩展采集与恢复记录](COMPLETE_INGESTION_VALIDATION.md)
- [无人值守采集验收](UNATTENDED_COLLECTION_VALIDATION.md)
- [周期采集验证](PERIODIC_OFFICIAL_COLLECTION_VALIDATION.md)
- [固定 OCR 续窗验证](FROZEN_OCR_SUPERVISION_VALIDATION.md)
- [OCR 与向量交接验证](OCR_EMBEDDING_HANDOFF_VALIDATION.md)
- [早期工作台实施记录](plans/2026-10-11-tea-export-workbench.md)

维护文档时，将当前操作更新在对应专题；历史记录保留其时间和事实，不覆盖成最新状态。新增专题同步本索引，临时任务计划按 [工程规范](../AGENTS.md) 放在专用 `/tmp` 目录。
