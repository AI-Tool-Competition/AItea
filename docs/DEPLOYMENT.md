# 部署与生产预览

[文档中心](README.md) · [实例管理](INSTANCE_MANAGEMENT.md) · [MinerU 与 RAGFlow](MINERU_RAGFLOW.md)

当前应用按单机实例管理 SQLite、原件及后台队列。MinerU 和 embedding 使用 API，本机 RAGFlow 的用户权限运行方式另见接入文档。开发启动、本机生产预览和容器生产部署是不同入口；选择与实际用途对应的方式。

## 本机生产预览

在仓库根目录执行，先停止同一工作树的开发服务，避免同时写入前端构建目录：

```bash
make dev-stop
make build
make start
make dev-status
```

默认打开 <http://127.0.0.1:3000/tea>。生产预览使用已构建前端，后端关闭自动重载；启动器仍校验 API、页面和前端 API 转发是否就绪。停止使用 `make dev-stop`。端口配置和日志位置见 [开发环境](DEVELOPMENT.md)。

修改前端构建所使用的 API 地址或端口后，应重新构建再启动；不能复用指向另一个后端地址的构建产物。构建成功不等于真实来源可访问、外部模型已配置或全部法规已审核。

## 实例配置与外部服务

先用 `make instance-paths` 确认活动数据、私有环境文件和配置清单。私有配置默认位于 `data/config/application.env`，RAGFlow 凭据位于 `data/config/ragflow/`；运行状态与日志位于 `.dev/`。已有实例按 [归集步骤](INSTANCE_MANAGEMENT.md) 迁移，不能重新生成凭据替代原配置。

RAGFlow 运行环境准备完成后，使用 `make ragflow-up` 启动服务、`make ragflow-bootstrap` 注册已配置的云端向量模型。普通用户部署的工具准备、实际参数、认证和已知边界以 [MinerU 与 RAGFlow](MINERU_RAGFLOW.md) 为准。API 重新启动不会代替 RAGFlow 部署。

`make dev-stop` 管理工作台前后端；独立的官方采集服务与 RAGFlow 有各自的生命周期。完整实例备份前须按 [实例管理](INSTANCE_MANAGEMENT.md) 停止全部所属写入者，再进行备份和校验。

## Docker 生产部署

使用调用者的 Docker Engine 与 Compose 2.24.4+，不使用 sudo；部署脚本不修改默认 context 或全局 Docker 配置。先用 `docker context show` 和 `docker info` 核实实际守护进程；SSH context 的挂载路径属于远端主机，不能用本机目录存在推定挂载成立。容器不会创建新空库替代已有资料。

根 `compose.yaml` 包含 API、Next.js standalone 前端和 Caddy 流式网关。默认桥接网络只发布宿主机 `127.0.0.1:3080`；服务使用非 root 用户、只读镜像、受限临时目录、日志轮转和健康检查。整个实例以相同绝对路径挂载，保持历史原件、检查点和版本证据中的绝对路径有效；原件不会复制进镜像。旧配方的 `tea_data` volume 不会自动删除或迁移。

`deploy/compose.host.yaml` 用于 Linux 上已有 RAGFlow、下载代理绑定本机回环地址的实例。三个服务均只监听回环地址，不另建 RAGFlow、不改变已有检索数据。`production-prepare` 默认选择此模式；独立桥接部署传 `--network bridge`，并确保 `application.env` 中的外部服务地址在容器内真实可达。

先停止同工作树的开发服务，确认所需备份与数据库升级副本检查，再执行：

```bash
make instance-paths
make dev-stop
make production-prepare PRODUCTION_ARGS='--origin https://example.com --base-path /aitea'
make production-build
make production-check
make production-up
make production-status
```

`prepare` 读取现有实例路径，只生成 `.dev/production/deploy.env` 的非密钥部署参数，权限为 `0600`。`--data-dir` 可显式选择已有目录，必须包含有效的 `tea.sqlite3`、`files/` 和 `config/application.env`；不会创建缺失数据目录。已有参数需核对后显式 `--replace`。端口分别用 `--api-port`、`--web-port`、`--gateway-port` 设置。

活动来源和流程目录也保持原绝对路径并只读挂载，默认由 `InstancePaths` 选择，可用 `--catalogs` 显式指定。这样宿主采集器与容器不会各维护一份生效清单；恢复实例的 `data/config/catalogs/` 继续优先。`--replace` 保留已生成产物的位置，封装时仍重新核对源码与参数，不能绕过过期检查。

容器直接读取同一份 `application.env`，不使用空环境变量覆盖 Agent、MinerU、识图、Embedding 或 RAGFlow 配置，后台 API 配置仍可原子保存。UID/GID 默认取当前用户；普通 Docker 可用 `--uid`、`--gid` 与现有目录所有者匹配，不能递归改属主来绕过权限。Rootless Docker 的容器 UID 0 对应宿主普通用户，需准备好系统的 uidmap 与 subordinate UID/GID，再用 `--uid 0 --gid 0`。详见 [Docker Rootless](https://docs.docker.com/engine/security/rootless/)。

Docker 主机内存不足或使用 CPU 模拟时，可将前端编译放在拥有项目 Node.js 22 工具链的本机，再由同一个 Docker 封装镜像：

```bash
make production-web-build
make production-build PRODUCTION_ARGS='--prebuilt-web'
```

两条路径共用 `Dockerfile.web` 的生产基础层，Node.js 固定为 22.23.0，与项目工具链一致。产物保存在 `.dev/production/web-artifacts/`，必须通过源码指纹、前缀、API 代理、Node 主版本及包内链接检查；前端源码或部署参数改变后拒绝封装旧产物。有独立前端环境文件时拒绝本机封装，避免将私有配置混入。生产运行仍全部使用 Docker；CI 使用默认的容器内源码构建路径。

`production-check` 和 `production-up` 在资料根生成一次性只读校验标识，要求容器能读到同一标识、SQLite 文件及私有配置；无论成功、超时或拒绝都会清理标识。远端空目录、错误副本或无权限均阻止启动。`up` 等待三个服务健康，不以容器进程已创建代替就绪；`production-stop` 保留所有数据、配置和容器卷。

生产配方显式关闭匿名演示，入口直接展示登录表单；访客可自助创建普通账号，注册成功即建立会话，无需管理员审批。公网开放前须更换仍使用的初始管理员弱口令，不能把源码中的默认密码当作生产凭据。隔离容器冒烟显式开启演示，仅使用本地模型替身。

API 默认不启动解析、采集和索引 worker，使聊天与批量处理分离。需要启动这组现有持久队列时，明确执行 `make production-up PRODUCTION_ARGS='--with-workers'`，确认没有其他实例处理同一队列，并核对付费调用预算。原独立官方采集器和 RAGFlow 的生命周期保持各自管理；旧未知付费结果及超预算任务不会因部署被重置。

本机缺少 uidmap 且无法获得管理员协助时，已验证的 Docker-in-gVisor 配置见 [wenbo 本机 Docker](DOCKER_HOST.md)。该模式需要显式使用 `--engine gvisor`，不能直接套用标准 RootlessKit 的准备步骤。

## `/aitea` 与流式转发

生产默认入口为 `/aitea`，直接展示登录页；注册、登录和退出后都保留此地址，旧入口 `/aitea/tea` 兼容跳转到 `/aitea`。管理后台保留 `/aitea/tea/admin`。前端构建时固化 `NEXT_PUBLIC_TEA_BASE_PATH`，接口、文件下载和映射图片均使用同一前缀。改变前缀或前端直连的 API 地址后重新构建，不能仅修改运行时变量。未设置部署前缀时，工作台入口为 `/`，`/tea` 兼容跳转；原客户端 API 协议不变。

Cookie 使用 `Secure`、`HttpOnly`、`SameSite=Lax`，范围限定到部署前缀，退出按同一范围删除。`TEA_PUBLIC_ORIGIN` 只允许 HTTPS 来源，本机隔离测试例外。来源只填站点，例如 `https://example.com`，不含 `/aitea`。

网关将 `/aitea/api/tea/*` 直接转发到 API，剥离部署前缀，避免正文流经过 Next.js 代理；模型和工具流保持 `no-store, no-transform` 与 `X-Accel-Buffering: no`。API 代理即时刷新，不压缩 SSE；前端静态资源由 Next.js 生成内容指纹并缓存。配置依据 [Next.js basePath](https://nextjs.org/docs/pages/api-reference/config/next-config-js/basePath) 与 [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

Serino 的独立扩展片段见 [serino-aitea.caddy](../deploy/serino-aitea.caddy)。先验证本机生产入口、资料挂载、账号与流式，再从实际 Docker 宿主机建立到腾讯云的专用 SSH 反向转发：

```bash
systemd-run --user --unit=aitea-production-tunnel \
  --property=Restart=always --property=RestartSec=3 \
  /usr/bin/ssh -NT -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o Compression=no \
  -o ControlMaster=no -R 127.0.0.1:18088:127.0.0.1:3080 rowan@tcyun
```

该用户级临时服务在连接中断后重连；宿主机重启后需重新启动。需要持久运行时，使用 [aitea-production-tunnel.service](../deploy/aitea-production-tunnel.service)：将 `@ROOT@` 替换为实际仓库路径，安装到用户服务目录并启用；它从已有部署参数读取本机网关端口。端口已占用时拒绝接管，不停止其他转发。Serino 只加入 `/aitea` 的扩展文件，保留原站点路由；片段不剥离前缀。验证完整 Caddy 配置后热加载，再检查 `https://aerisun.top/aitea`。Docker 守护进程在其他主机时，SSH 转发也必须指向真正的容器入口。

## 容器隔离验收

构建镜像后执行：

```bash
uv run --project backend --locked python scripts/container_smoke.py --tag <镜像标签>
```

单 UID 的本机 Docker-in-gVisor 增加 `--engine gvisor`。该模式使用命名空间内 UID/GID 0 初始化和运行隔离实例，仍不挂载业务资料或调用真实模型。

脚本只创建带随机标识的专用 Docker 数据卷和本地模型替身，测试真实镜像的页面跳转、静态资源、登录、Cookie 范围、退出失效和逐段 SSE。模型替身每 250 ms 发送一段，要求至少两段在不同时间到达，防止把最终一次性返回误判为流式。普通检查不读取业务配置，不访问真实模型；结束清理所属容器和卷。`--keep` 可保留合成实例供浏览器检查，完成后按输出的 `state.json` 使用 `--cleanup` 清理。CI 对每次 push/PR 构建三个镜像并执行同一检查。

真实账号、现有原件、RAGFlow 检索和公网转发需另行验收。隔离镜像冒烟通过不能证明某个远端 Docker 已挂载本机资料，也不代表上游模型、专业法律内容或并发容量已通过验收。

## 正式接入的验收范围

- 配置 HTTPS、允许的站点来源与 Secure cookie；初始管理员及账号开放机制见 [配置后台](ADMIN_STUDIO.md)，不能直接使用演示账号策略开放公网服务。
- 核验账号和业务隔离、私有原件访问、法规审核及适用期。采集完成、解析完成和可以作为法律依据是不同状态。
- 对真实来源、MinerU、RAGFlow 和模型供应商分别验证认证、超时、限流、断点恢复与实际检索质量，不用模拟测试代替供应商实测。
- 备份配置、数据库、原件、版本证据和检索数据，并在新目录验证恢复。保留代码提交号及锁文件，迁移工具与限制见 [实例管理](INSTANCE_MANAGEMENT.md)。

`make check`、`make build` 和 CI 结果用于证明相应代码与构建检查；生产容量、外部服务可用性和法规有效性需要独立验收。发布说明只记录本次实际验证的范围。
