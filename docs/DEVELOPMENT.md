# 开发环境

启动入口与 Admin System 保持相同习惯：`make dev` 准备依赖并在后台启动服务，`make dev-stop` 停止当前工作树。这里只启动茶渡 API 与 DeerFlow 前端，不引入其他项目的数据服务。

## 一条命令启动

在仓库根目录执行：

```bash
make dev
```

前提是 Linux 环境已提供 uv、Python 3、Node.js 22+ 和 npm。Python 3 仅用于运行标准库开发管理脚本；后端和 Python 测试统一使用 uv 管理的 Python 3.12+ 环境。无需 `source activate`，也不使用全局 pip、Conda 或 Poetry。

启动器会检查端口、同步锁定依赖、启动后端热更新与 Next.js 开发服务，并等待 API 与页面就绪。安装期间会显示最新进度；暂无新日志时也会提示安装仍在进行及日志位置。健康检查完成后输出访问地址并返回终端。默认入口为 <http://127.0.0.1:3000/tea>，API 监听本机 8000 端口。

| 命令 | 行为 |
| --- | --- |
| `make dev` | 按需同步依赖，后台启动开发服务 |
| `make dev-foreground` | 前台运行，Ctrl+C 停止本次服务 |
| `make dev-status` | 查看当前工作树运行状态 |
| `make dev-list` | 只读查看本仓库各工作树的运行状态 |
| `make dev-stop` | 取消当前工作树的依赖准备/启动，或停止已运行的服务 |
| `make dev-clean` | 停止服务，清理运行状态和日志，保留业务数据、配置、虚拟环境及依赖缓存 |
| `make setup` | 显式同步前后端依赖，不启动服务 |
| `make dev-check` | 检查启动器和工作树工具 |
| `make check` | 后端测试与静态检查、前端类型检查和茶业务交互测试 |
| `make build` | 构建前端生产产物 |
| `make start` | 使用已有构建后台启动本地生产预览，后端不热更新 |

同一工作树不能同时运行开发服务和生产预览。切换前先执行 `make dev-stop`；生产构建也应在停止开发服务后执行，避免两种模式同时修改 `.next`。

生产构建会固化前端代理地址。使用自定义端口或链接工作树预览时，构建和启动要使用相同的 API 端口，例如 `make build API_PORT=9001` 后执行 `make start API_PORT=9001 WEB_PORT=4001`；地址不一致时启动器会提示重新构建。

## uv 与依赖缓存

- Python 依赖的唯一入口为 `backend/pyproject.toml` 和 `backend/uv.lock`；`uv sync --locked` 校验并安装锁定版本。
- 虚拟环境固定为当前工作树的 `backend/.venv`；实际服务通过 `uv run --project backend --locked --no-sync` 使用已同步环境。
- 缓存和自动下载的 Python 安装在当前工作树的 `.dev/` 下：`uv-cache`、`python`、`npm-cache`、`pnpm-store`。
- 前端使用锁定的 pnpm 10.26.2 和 `frontend/pnpm-lock.yaml`；不执行全局 pnpm 安装。
- `SYNC=auto` 是默认值，依赖输入不变且安装存在时跳过同步；`SYNC=force` 强制同步；`SYNC=skip` 仅在依赖已经安装时跳过同步，不会伪造安装成功。

```bash
make dev SYNC=force
make dev SYNC=skip WEB_PORT=3001 API_PORT=8001
```

新增后端依赖时用 uv 修改项目并同步提交锁文件，例如：

```bash
UV_CACHE_DIR="$PWD/.dev/uv-cache" UV_PROJECT_ENVIRONMENT="$PWD/backend/.venv" \
  UV_PYTHON_INSTALL_DIR="$PWD/.dev/python" \
  uv add --project backend package-name
```

直接执行后端命令也应显式设置项目内缓存与环境；通过 Makefile 执行时这些变量已统一设置。uv 的锁文件和同步行为以 [官方说明](https://docs.astral.sh/uv/concepts/projects/sync/) 为准。

## 工作树隔离

创建功能分支和独立工作树：

```bash
make worktree NAME=feature/example
```

创建器要求当前工作树干净，分支名有明确含义，目标目录在仓库内且被 Git 忽略。默认基于 `main`；初始化阶段没有 `main` 时使用当前 `HEAD`，也可指定 `BASE=<ref>`。输出中会给出实际工作树目录。

进入新工作树后仍然使用 `make dev`。每个工作树独立保存 `.dev/`、`backend/.venv`、`frontend/node_modules` 和 `data/`，不会复制主工作树的密钥、账号或业务数据。链接工作树默认使用按目录计算的端口；实际端口以启动输出为准，发生占用时通过 `API_PORT` 和 `WEB_PORT` 显式改端口。

`make dev-stop` 根据工作树归属和进程身份停止已登记的进程，不会按端口杀进程或使用全局 `pkill`。`make dev-list` 不会停止其他工作树。移除工作树前，先在其中停止服务，再核对路径、未提交改动和需保留的数据。

## 日志与排错

安装和启动阶段的归属记录保存在 `.dev/startup.json`，服务运行状态保存在 `.dev/dev.json`；不要手动编辑或复制到另一个工作树。准备依赖期间 `make dev-status` 显示 `preparing`，此时仍可使用 `make dev-stop` 安全取消，不需要等待安装完成，也不要手动删除锁文件。日志包括：

- `.dev/api.log`：后端及热更新日志。
- `.dev/web.log`：Next.js 编译与页面请求日志。
- `.dev/supervisor.log`：服务管理与退出原因。
- `.dev/setup-api.log`、`.dev/setup-web.log`：依赖同步日志。

启动失败会显示错误及相关日志位置，并回收本次已启动的服务。端口被其他服务占用时，启动器报错，不会接管或关闭那个服务。

如果后台某个服务意外退出，管理进程会停止同次启动的其他服务；先读日志，再重新 `make dev`。锁文件与依赖声明不一致时先确认依赖变更，再用 uv 更新并提交锁文件，不用删除锁文件绕过错误。

`.env` 仍为业务配置入口，密钥仅由后端读取。`data/` 保存 SQLite 数据库和原始资料；开发清理不会删除它。删除数据、迁移数据和正式部署应分别处理，不能混入日常启动或清理命令。
