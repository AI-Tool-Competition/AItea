# 茶渡 · 茶叶出口工作台

面向中国到印度尼西亚茶叶出口的配置驱动工作台。商家在对话中办理业务，右侧查看串行阶段和并行任务；法务人员维护任务、核验要求、助手步骤与资料来源。

前端基于用户提供的 **DeerFlow 完整前端源码**，新增茶业务工作区 `/tea`，实际复用它的 UI 和会话组件。原文解析器由 GEO-System / Admin-System 提取。参考项目保留在 `tmp/`，不参与运行。

## 本地运行

开发启动器支持 Linux，需要 Node.js 22+、npm、Python 3 和 uv。后端 Python 3.12+ 环境由 **uv** 在 `backend/.venv` 中管理；不需要手动激活环境或使用全局 pip。在本项目目录运行：

```bash
make dev
```

打开 <http://127.0.0.1:3000/tea>，选择“进入演示工作台”，或注册独立账户。演示入口也有服务端会话与持久化业务记录，不会把不同访客放进同一个共享账号。

`make dev` 自动同步锁定依赖、后台启动前后端并等待健康检查，成功后返回终端。前后端代码修改均支持热更新。首次安装需要下载 DeerFlow 的完整依赖，后续根据依赖指纹跳过重复安装。

```bash
make dev-status       # 当前工作树状态
make dev-list         # 本仓库所有工作树状态
make dev-stop         # 取消安装/启动或停止服务，保留业务数据
make dev-foreground   # 需要前台运行时使用，Ctrl+C 停止
```

日志保存在 `.dev/api.log`、`.dev/web.log` 和 `.dev/supervisor.log`。端口已占用时不会终止其他项目，可显式指定：

```bash
make dev WEB_PORT=3001 API_PORT=8001
```

业务数据库和原始文件位于 `data/`，不要提交到 Git。刷新、重启及 `make dev-clean` 都会保留业务数据。完整命令、工作树隔离和排错方式见 [开发环境说明](docs/DEVELOPMENT.md)；工程约定见 [AGENTS.md](AGENTS.md)。

## 连接模型与真实资料

复制 `.env.example` 为 `.env`，设置后端模型接口、密钥和模型名称，然后重启。所有模型凭据只在后端使用，不能放进 `NEXT_PUBLIC_*`。

- `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`：OpenAI-compatible 生成接口。
- `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`：向量接口。
- 未配置生成接口时，页面标为演示助手；未配置向量接口时，使用词项检索。
- 在资料库上传文件，或在资料来源页面配置经过确认的官方 HTTPS 来源。
- 采集资料默认待审核；在资料预览中设置类别、法域、生效日期并审核后，才作为回答依据。用户业务材料与法规资料明确区分。
- 演示业务内容、模式 A/B 和内部资料说明仅用于演练，不是已审核的法务办理清单。实际国家规则和两种模式由后续配置替换。

## 核心行为

- 一次业务对应一个文件夹，各阶段有独立对话与共享业务事实。
- 阶段串行、任务并行；必需条件和当前版本证据满足后才能完成。
- 配置发布保留历史版本，已有业务继续使用创建时的快照。
- 材料更改、信息冲突、版本过期会触发补充或复核，不自动覆盖历史判断。
- 文稿有独立版本、编辑与确认；过期编辑请求会被拒绝，防止覆盖新内容。
- 主文件解析、结构化和索引；关联二级文件保留原件但不解析、不进入正文检索。
- 来源更新由后台周期检查；手动抓取与定时抓取使用相同受限下载路径。

默认清单位于 `config/workflows/china-indonesia.json`，服务启动自动读取并校验。也可在页面导入、编辑、调试、发布；文件与账号内发布版本的关系见 `config/README.md`。

## 验证与构建

```bash
make check
make dev-stop
make build
make start
```

`make check` 运行茶业务后端测试与静态检查、整个前端的类型检查，以及新增前端交互测试；`make build` 构建 DeerFlow 前端。具体浏览器、容器与功能验证结果见 [验收记录](docs/VERIFICATION.md)。本次新增工作区的检查不等于完整上游 DeerFlow Gateway 功能检查。

## 容器运行

```bash
docker compose up --build
```

容器入口默认只绑定本机 3000 端口，数据存入独立 volume，API 不直接暴露宿主端口。镜像使用非 root 用户与依赖锁文件。默认是单机演示配置；正式公网部署需要 HTTPS、明确允许的站点来源及 Secure cookie，并验证真实规则、检索质量和容量。Compose 配方的实际构建验证状态见验收记录。

## 设计资料

- `docs/ARCHITECTURE.md`：模块边界、DeerFlow 接法与运行边界。
- `docs/API_CONTRACT.md`：前后端接口与配置定义。
- `docs/INGESTION.md`：解析器来源、格式支持、检索与采集限制。
- `docs/plans/2026-10-11-tea-export-workbench.md`：已批准设计对应的实施与验收清单。

本地文档与文件内容不作为系统指令执行。依法务配置检索到的内容只作为证据输入，任务状态始终由业务服务判断。

初次演示可按 `docs/DEMO_GUIDE.md` 操作，示例业务材料位于 `samples/product-profile.md`。`make check` 也会运行新增前端交互测试及后端静态检查；CI 配方在 `.github/workflows/check.yml`。
