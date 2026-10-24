# 后端目录与职责

HTTP 请求由 `app/main.py` 进入，`interfaces/http/factory.py` 装配路由和后台任务，`bootstrap.py` 装配业务服务。配置和存储路径统一经过 `configuration/`，不从业务模块层级推算仓库位置。

```text
backend/
├── app/
│   ├── main.py                     # ASGI 入口
│   ├── bootstrap.py                # 服务装配、跨上下文协作门面
│   ├── configuration/
│   │   ├── paths.py                # 唯一实例路径定义
│   │   └── environment.py          # 私有环境配置读取、覆盖优先级
│   ├── contexts/                   # 按业务职责划分的限界上下文
│   │   ├── accounts/               # 账号、会话、管理员权限
│   │   ├── workflows/              # 办理流程、配置、业务任务、调试
│   │   ├── conversations/          # 对话、节点记忆、工具执行
│   │   ├── documents/              # 原件版本、解析、审核、检索
│   │   ├── ingestion/              # 来源清单、发现、采集、导入
│   │   ├── legal_analysis/         # 法律事实、分析、报告修订
│   │   └── drafting/               # 文稿及版本历史
│   ├── interfaces/http/            # 输入校验、鉴权、响应和路由
│   └── shared/
│       ├── domain/                 # 纯类型、错误、通用值规则
│       ├── application/            # 公共契约导出和流式事件
│       └── infrastructure/
│           ├── persistence/        # SQLite 结构升级、事务、CAS 原件
│           ├── http/               # 受限下载与公开搜索
│           ├── operations/         # 调度、备份、恢复及显式数据迁移
│           └── vendor/             # 有来源和许可记录的第三方实现
├── tests/                          # 行为、权限、持久化及恢复验证
├── pyproject.toml                  # Python 依赖与检查规则
└── uv.lock                         # 锁定依赖
```

每个上下文只创建实际需要的层：`domain/` 放纯规则和类型，`application/` 组织用例、权限和事务，`infrastructure/` 实现存储或外部系统适配。领域层不导入数据库、HTTP、配置环境或模型服务；路由不直接执行 SQL。这些边界由 `test_architecture.py` 检查。

| 需要修改的行为 | 入口 |
| --- | --- |
| 数据库、原件或实例配置位置 | `configuration/paths.py` |
| 账号与会话 | `contexts/accounts/application/auth.py` |
| 流程发布与业务任务 | `contexts/workflows/application/configurations.py`、`workspaces.py` |
| 流程草稿与调试 | `contexts/workflows/application/workflow_studio.py`、`workflow_debugger.py` |
| 文档版本与检索 | `contexts/documents/application/archive.py`、`retrieval.py` |
| 文件格式解析 | `contexts/documents/infrastructure/parsers/`，受限入口 `document_parser.py` |
| MinerU、RAGFlow、embedding | `contexts/documents/infrastructure/providers/` |
| 历史解析证据恢复 | `contexts/documents/infrastructure/recovery/` |
| 来源清单、采集队列、导入 | `contexts/ingestion/application/source_catalog.py`、`collection_jobs.py`、`official_source_import.py` |
| 官方站点结构适配 | `contexts/ingestion/infrastructure/source_adapters/`，通用发现入口 `link_discovery.py` |
| 法律分析与人工修订 | `contexts/legal_analysis/application/analysis_engine.py`、`analysis_service.py` |
| 备份与迁移 | `shared/infrastructure/operations/instance_storage.py`、`data_backup.py` |

接口契约仍在各上下文的 `application/contracts.py` 定义，经 `shared/application/models.py` 显式汇总。现有查询与事务属于各应用服务；不另造空 Repository 或 ORM 层。对外 API、数据库表名、历史文档标识不随 Python 包名变化。

完整业务说明见 [架构](../docs/ARCHITECTURE.md)，实例目录与迁移命令见 [实例管理](../docs/INSTANCE_MANAGEMENT.md)。
