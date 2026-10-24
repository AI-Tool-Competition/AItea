# 部署与生产预览

[文档中心](README.md) · [实例管理](INSTANCE_MANAGEMENT.md) · [MinerU 与 RAGFlow](MINERU_RAGFLOW.md)

当前应用按单机实例管理 SQLite、原件及后台队列。MinerU 和 embedding 使用 API，本机 RAGFlow 的用户权限运行方式另见接入文档。开发启动、前端生产预览和容器演示是不同入口；选择与实际用途对应的方式。

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

## 容器演示配方

已准备 Docker Engine 与 Compose 的环境可以使用仓库现有配方：

```bash
docker compose up --build
```

根 `compose.yaml` 启动 API 和前端，入口默认绑定宿主机 `127.0.0.1:3000`，API 不单独暴露宿主端口；数据位于 Compose 管理的 `tea_data` volume。镜像使用锁文件和非 root 用户。

这份配方是独立的演示实例：不会自动挂载宿主项目的 `data/`，也没有部署完整 RAGFlow 服务或自动传入 `application.env` 的全部参数。现有运行实例不能直接改用它作为迁移方式。使用前需在目标环境验证镜像构建、数据持久化及前后端连通性；历史检查范围见 [验收记录](VERIFICATION.md)，本机测试通过不能代替容器验收。

## 正式接入的验收范围

- 配置 HTTPS、允许的站点来源与 Secure cookie；初始管理员及账号开放机制见 [配置后台](ADMIN_STUDIO.md)，不能直接使用演示账号策略开放公网服务。
- 核验账号和业务隔离、私有原件访问、法规审核及适用期。采集完成、解析完成和可以作为法律依据是不同状态。
- 对真实来源、MinerU、RAGFlow 和模型供应商分别验证认证、超时、限流、断点恢复与实际检索质量，不用模拟测试代替供应商实测。
- 备份配置、数据库、原件、版本证据和检索数据，并在新目录验证恢复。保留代码提交号及锁文件，迁移工具与限制见 [实例管理](INSTANCE_MANAGEMENT.md)。

`make check`、`make build` 和 CI 结果用于证明相应代码与构建检查；生产容量、外部服务可用性和法规有效性需要独立验收。发布说明只记录本次实际验证的范围。
