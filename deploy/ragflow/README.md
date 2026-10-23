# AItea 的 RAGFlow 服务

RAGFlow 固定为官方 `v1.0.0-rc2`。使用 Go API、Infinity、MySQL、MinIO、Kvrocks 和 NATS；不启动本地 OCR、MinerU、embedding 或 ingestion worker。AItea 通过 MinerU 云端精准解析获取页级结构，再通过 RAGFlow 文档与 chunk API 建立索引。RAGFlow 负责调用已配置的远端 embedding、混合召回和可选重排。

运行 `make ragflow-prepare` 生成当前 checkout 的私有配置，再运行 `make ragflow-up`。配置位于忽略目录 `.dev/ragflow/`，业务数据位于 `data/ragflow/`；端口默认只绑定本机 `18080`（管理界面）和 `19380`（API）。`make ragflow-stop` 停止本项目服务并保留数据。

没有 sudo 或 Docker 时，`--runtime user` 使用安装在 `/home/wenbo/.local/opt/aitea/` 的 Apptainer 和 slirp4netns。独立用户及网络命名空间内运行各组件，只转发本机界面/API 端口，不依赖 uidmap。已有 Docker Engine 与 Compose v2 的机器可选择 `--runtime docker`。脚本不修改系统配置、shell 配置或全局 Docker context，不会删除现存数据库和容器。配置云端模型需继续运行 `make ragflow-bootstrap`；完整说明见 [部署和数据管线](../../docs/MINERU_RAGFLOW.md)。

`infinity.toml`、`kvrocks-entrypoint.sh` 和 `init.sql` 来自 [InfiniFlow RAGFlow v1.0.0-rc2](https://github.com/infiniflow/ragflow/tree/v1.0.0-rc2/docker)，沿用 Apache-2.0 许可（见 `UPSTREAM-LICENSE`）。Infinity 的缓存预算调整为 2 GB，memindex 预算调整为 256 MB。Compose 根据该版本官方配置裁剪，所有依赖均固定镜像版本。
