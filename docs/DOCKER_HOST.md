# wenbo 本机 Docker

[部署说明](DEPLOYMENT.md) · [实例管理](INSTANCE_MANAGEMENT.md)

本机没有系统管理员权限，也没有 `newuidmap` / `newgidmap`。当前采用官方支持的 [Docker-in-gVisor](https://gvisor.dev/docs/tutorials/docker-in-gvisor/) 运行方式：Docker Engine 在本机的 gVisor 中运行，外层进程始终属于 wenbo；没有 QEMU、独立虚拟机或远端资料副本。这与依赖 uidmap 的标准 Docker RootlessKit 安装不同。

## 运行边界

运行目录是 `/data/238_2/wenbo/Service/docker-host`，权限 `0700`，独立保存运行时、基础文件系统、Docker 状态和安装收据。Docker 29.9.2 与 gVisor release20261019.0 的二进制哈希记录在 `installation.json`，启动时逐一核对；基础文件系统来自官方 Python 3.13 slim-bookworm 镜像。许可和来源见该目录的 `SOURCES.md`。运行时不依赖 GPU 项目的文件。

用户服务模板为 [docker-host.service](../deploy/docker-host.service)，将 `@ROOT@`、`@RUNTIME@` 替换为实际路径后安装到用户服务目录。入口 [docker_host.py](../scripts/docker_host.py) 检查私有目录归属、固定二进制和挂载根；停止只针对记录的 gVisor 实例。

```bash
systemctl --user status wenbo-docker-host.service
python3 scripts/docker_host.py status --runtime-dir /data/238_2/wenbo/Service/docker-host
docker --context wenbo-host info
```

Docker context `wenbo-host` 使用运行目录内的 Unix socket，不提供 TCP 管理接口。Docker 使用 VFS 存储和 host 网络，关闭 Docker 网桥、iptables 与 IP 转发。gVisor 允许访问的宿主路径显式记录于安装收据；新增挂载根须核对用途后修改收据并重启服务。

整个引擎由用户服务限制为 8 GB 内存、禁止新增权限。内部 cgroup 由 gVisor 提供，不能把普通 Docker 的每容器内核限额当作已经验证。VFS 会复制镜像文件系统，创建容器明显慢于原生 overlayfs；这不代表请求也有相同延迟，应分别检查运行期的首字和接口延迟。

## AItea 参数

本安装只有单 UID/GID 映射。容器内 UID/GID 0 映射到宿主 wenbo，不具备宿主 root 权限；其他容器 UID 无法正常写入宿主转发的文件系统。因此使用专用配方，而不是改变通用 Docker 配方的非 root 默认值：

```bash
DOCKER_CONTEXT=wenbo-host make production-prepare PRODUCTION_ARGS='--replace --engine gvisor --network host --origin https://aerisun.top --base-path /aitea'
DOCKER_CONTEXT=wenbo-host make production-web-build
DOCKER_CONTEXT=wenbo-host make production-build PRODUCTION_ARGS='--prebuilt-web'
DOCKER_CONTEXT=wenbo-host make production-check
DOCKER_CONTEXT=wenbo-host make production-up
```

该专用配方要求 Compose 2.27.1+ 和 Buildx。构建通过同一组 Compose 文件交给 Bake，只明确允许 `network.host`，不授予 `security.insecure`。

`--engine gvisor` 默认选择命名空间内 UID/GID 0，并拒绝不兼容的 UID 或 bridge 网络。额外加载 `deploy/compose.gvisor.yaml`，使 web/gateway 的临时目录也使用该映射。只读根文件系统、全部移除 capabilities、禁止新增权限、回环监听和真实资料挂载校验继续生效。

网关的官方 Caddy 镜像携带 `security.capability` 扩展属性，当前单 UID 宿主转发文件系统不能保存它。专用配方使用本地 `aitea-caddy-base:2.11.4`：从已核验的官方 `caddy:2.11.4-alpine` 导出完整文件系统，仅删除 tar 中这一个文件 capabilities 扩展属性，然后保留原 ENTRYPOINT、CMD、ENV、WORKDIR 导入本地镜像。原始镜像 digest、两份 tar 的 SHA256 和导入镜像 ID 记录在 `.dev/host-docker/base-images/receipt.json`。该基础镜像持久保存在新引擎，运行和重建不依赖旧虚拟机；重新安装时须按相同来源与收据准备。通用配方仍使用官方镜像，并在构建中移除相同能力。

切换前停止同一工作树的开发服务。已有 RAGFlow 和独立采集服务继续运行在本机，Docker host 网络使容器仍能访问它们的回环入口。不要为部署重建资料库或批量重置外部请求。

已有用户权限 RAGFlow 的开机启动模板是 [aitea-ragflow.service](../deploy/aitea-ragflow.service)，替换 `@ROOT@` 和 `@UV@` 后启用。它复用现有进程身份、配置和数据，并等待依赖就绪；不复制资料，不重新生成凭据。生产中的启停用该用户服务管理，状态仍可用 `make ragflow-status` 检查；服务处于 active 不代表已经完成长期检索质量或崩溃恢复验收。

## 闲鱼、GPU 与旧虚拟机

闲鱼后台迁至本机 Python 服务 `xianyu-native.service`，使用其项目 `var/native/state/` 中的迁移数据。GPU 的正式后端是独立的本机 gVisor/JupyterHub，保留原有用户、存储和后台服务；它不经过本 Docker 引擎，也不要求给 AItea 容器开放 GPU。

旧 `wenbo-vm` 的停用以本次部署记录和用户服务实际状态为准。旧磁盘以及一致性导出的闲鱼数据、GPU 卷仅作离线恢复证据；不得重新启动两个后台共同消费同一闲鱼账号。禁止把历史部署记录当作当前服务状态。
