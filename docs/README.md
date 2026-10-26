# 文档中心

[返回项目首页](../README.md) · [工程规范](../AGENTS.md)

从操作目标选择文档。当前解析与检索入口以 **MinerU API + RAGFlow** 为准；历史 OCR、向量恢复工具和带日期的验收记录用于追溯，不代表当前在线配置、数据规模或服务状态。

## 首次使用与开发

| 文档                             | 解决的问题                                  |
| -------------------------------- | ------------------------------------------- |
| [演示指南](DEMO_GUIDE.md)        | 体验工作台与示例业务材料                    |
| [开发环境](DEVELOPMENT.md)       | uv/pnpm、启动停止、热更新、独立工作树及排错 |
| [部署说明](DEPLOYMENT.md)        | 本机生产预览、容器配方及实际部署边界        |
| [测试范围](TESTING.md)           | 日常检查、测试隔离与保留的风险覆盖          |
| [后端目录](../backend/README.md) | 按业务定位实现文件及 DDD 分层职责           |

## 办理与法务配置

[Agent 调度与记忆](AGENT_RUNTIME.md)：模型自主工具选择、能力组合、图片与联网、记忆管理及运行边界。

| 文档                              | 解决的问题                                   |
| --------------------------------- | -------------------------------------------- |
| [六堡茶流程](LIUBAO_WORKFLOWS.md) | 四条路线、任务编排和材料要求                 |
| [配置后台](ADMIN_STUDIO.md)       | 初始管理账号、账号开放、流程编辑、调试与发布 |
| [节点工作区](NODE_WORKSPACE.md)   | 业务、节点、对话与共享事实如何协作           |
| [法律分析](LEGAL_AGENT.md)        | 事实、争点、引用、报告修订与人工复核         |
| [Agent 工具](AGENT_TOOLS.md)      | 实际工具调用、流式过程、失败预算与取消       |
| [配置目录](../config/README.md)   | 流程文件、来源清单及发布快照的关系           |

## 架构、接口与数据

| 文档                                   | 解决的问题                                 |
| -------------------------------------- | ------------------------------------------ |
| [系统架构](ARCHITECTURE.md)            | 前后端装配、状态边界和外部能力接入         |
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
