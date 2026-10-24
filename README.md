# AItea · 茶渡

面向六堡茶出口印度尼西亚、新加坡的资料与办理工作台。把办理流程、官方资料采集、结构化解析、法律检索和文稿版本放在同一个工作区，帮助商家准备材料，让法务人员维护规则并核验依据。

[文档中心](docs/README.md) · [开发环境](docs/DEVELOPMENT.md) · [工程规范](AGENTS.md) · [CI 检查](https://github.com/AI-Tool-Competition/AItea/actions/workflows/check.yml)

## 主要能力

- **办理流程**：提供印尼／新加坡、线上／线下四条路线；按阶段组织任务、对话、事实和材料，保留业务创建时的配置快照。
- **资料库**：区分共享法规与账号私有资料，保存原件、不可变版本、法规关系、审核状态及适用日期。
- **持续采集**：从统一来源清单发现与下载资料，支持代理、周期检查、断点续跑和内容去重，来源变化保留历史证据。
- **解析与检索**：使用 MinerU 云端精准解析，保留页码、表格、图片与原文定位；HTML 使用结构化解析；RAGFlow 负责索引与检索。
- **分析与文稿**：围绕已确认事实和合格资料生成建议，保留引用及人工编辑；资料变化触发复核，过期编辑不会覆盖新版本。

模型建议不会直接完成办理任务。法规是否能作为依据，仍须通过账号、国家、版本、审核和适用期检查。

## 技术组成

| 部分   | 实现                                                     |
| ------ | -------------------------------------------------------- |
| 前端   | Next.js、React、TypeScript，复用 DeerFlow 界面与会话组件 |
| 后端   | FastAPI、Pydantic，按业务上下文组织的 DDD 分层单体       |
| 持久化 | SQLite WAL、显式事务、内容寻址原件与版本化结构           |
| 解析   | MinerU 精准解析 API；HTML DOM 校验与结构化提取           |
| 检索   | 本机 RAGFlow，调用外部 embedding 服务                    |
| 工具链 | uv、锁定版本的 pnpm、根目录 Makefile                     |

模块职责见 [后端目录](backend/README.md)，系统协作与权限边界见 [架构说明](docs/ARCHITECTURE.md)。

## 快速开始

当前开发启动器支持 **Linux**。准备 Git、Make、Python 3、uv、Node.js 22+ 和 npm；后端 Python 3.12+ 环境及依赖由 uv 管理，前端使用 pnpm 10.26.2，无需全局安装 pnpm 或手动激活虚拟环境。

```bash
git clone https://github.com/AI-Tool-Competition/AItea.git
cd AItea
make dev
```

首次运行会按锁文件下载依赖，创建本工作树的环境和数据目录，并等待前后端就绪；后续启动按依赖指纹复用环境。代码修改支持热更新。

| 入口         | 默认地址                               |
| ------------ | -------------------------------------- |
| 茶业务工作台 | <http://127.0.0.1:3000/tea>            |
| 管理后台     | <http://127.0.0.1:3000/tea/admin>      |
| API 健康检查 | <http://127.0.0.1:8000/api/tea/health> |

可从“进入演示工作台”开始体验；每位访客都有独立会话与业务记录。新注册账号需管理员开放登录。初始管理员、权限管理和操作步骤见 [配置后台](docs/ADMIN_STUDIO.md)；体验材料见 [演示指南](docs/DEMO_GUIDE.md)。

未配置生成接口时显示演示助手，未配置 RAGFlow 时显示检索未就绪。体验界面不代表真实法规已采集、审核或入索引。

## 配置真实服务

新实例将 [.env.example](.env.example) 复制为 `data/config/application.env`，目录权限设为 `700`、文件权限设为 `600`。已有实例先按 [实例管理](docs/INSTANCE_MANAGEMENT.md) 归集旧配置，保留原凭据和资料。修改环境参数后重启所属服务。

| 配置                                                             | 用途                                        |
| ---------------------------------------------------------------- | ------------------------------------------- |
| `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`                       | 后端生成接口                                |
| `MINERU_API_TOKEN`                                               | MinerU 云端精准解析，本机不部署 MinerU 模型 |
| `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`     | 由 RAGFlow 调用的云端向量接口               |
| `RAGFLOW_BASE_URL`、`RAGFLOW_API_KEY`、`RAGFLOW_EMBEDDING_MODEL` | 项目与 RAGFlow 的接线，由配置引导命令填写   |

RAGFlow 的普通用户部署、模型注册和并发规则见 [MinerU 与 RAGFlow](docs/MINERU_RAGFLOW.md)。密钥仅进入后端私有配置，不放入 `NEXT_PUBLIC_*` 或 Git。

来源和流程默认配置位于 [`config/`](config/README.md)。运行 `make instance-paths` 查看实际生效目录；恢复实例会携带原配置快照。数据库、原件、私有配置、采集状态和 RAGFlow 数据由同一实例目录管理，可以成套备份和恢复。

## 常用命令

| 命令                                | 用途                                         |
| ----------------------------------- | -------------------------------------------- |
| `make dev`                          | 同步依赖并启动热更新开发环境                 |
| `make dev-status` / `make dev-stop` | 查看／停止当前工作树的服务，保留业务数据     |
| `make check`                        | 后端测试和静态检查、前端类型及茶业务交互检查 |
| `make build` / `make start`         | 构建／启动生产预览，先停止当前开发服务       |
| `make instance-paths`               | 查看数据库、私有配置与来源清单的位置         |
| `make collection-status`            | 查看已配置的官方采集服务状态                 |

端口占用时可用 `make dev WEB_PORT=3001 API_PORT=8001` 启动。日志位于 `.dev/`。完整开发命令、独立工作树及排错方法见 [开发环境](docs/DEVELOPMENT.md)；实例备份必须遵循 [停服和校验步骤](docs/INSTANCE_MANAGEMENT.md)。

## 仓库结构

```text
backend/       后端服务、业务上下文与测试
frontend/      DeerFlow 前端及 /tea 工作区
config/        可版本化的流程、来源与采集配置
scripts/       开发管理、采集和运维入口
deploy/        部署配方
docs/          使用、设计、运维与历史验证文档
samples/       体验材料
data/          当前实例的持久数据与私有配置（不提交）
.dev/          当前工作树的运行状态、日志和工具缓存（不提交）
```

## 进一步阅读

- **使用工作台**：[六堡茶流程](docs/LIUBAO_WORKFLOWS.md)、[配置后台](docs/ADMIN_STUDIO.md)、[法律分析](docs/LEGAL_AGENT.md)。
- **维护资料库**：[持续采集](docs/UNATTENDED_COLLECTION.md)、[解析与检索](docs/MINERU_RAGFLOW.md)、[资料目录](docs/COLLECTION_MATERIALS.md)。
- **部署和搬迁**：[部署说明](docs/DEPLOYMENT.md)、[实例管理](docs/INSTANCE_MANAGEMENT.md)、[备份协议](docs/DATA_BACKUP.md)。
- **参与开发**：[工程规范](AGENTS.md)、[接口契约](docs/API_CONTRACT.md)、[测试范围](docs/TESTING.md)。

全部专题与历史记录见 [文档中心](docs/README.md)。前端沿用 DeerFlow 上游说明；解析器提取代码保留来源与许可记录。`tmp/` 中的参考项目不参与运行。
