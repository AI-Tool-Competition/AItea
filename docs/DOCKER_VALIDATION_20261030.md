# 2026-10-30 分支集成与 Docker 部署验证

[文档中心](README.md) · [部署操作](DEPLOYMENT.md)

本记录只覆盖当日代码、实例与检查，不代表后续在线状态、真实模型时延或法律内容验收。

## 分支集成与代码检查

`main` 从 `40e1070` 集成远端 `refactor/evidence-first-runtime` 的 `e164c18`，使用双亲提交 `ae2902f` 保留功能边界；没有文件冲突，集成树与远端功能分支一致。该提交已推送，[对应 CI](https://github.com/AI-Tool-Competition/AItea/actions/runs/41009771719) 成功。

- 集成前 `make check`：后端 3000 通过、5 跳过；前端 299 通过；生产构建成功。
- Docker 部署实现的 `make check`：后端 3031 通过、5 跳过、2 条警告；前端 309 通过，类型和静态检查通过。
- 部署专项 31 项通过；改动范围的 Ruff、Python 格式、前端 Prettier/ESLint 通过；根路径与 `/aitea` 的原生生产构建均成功。

分支原有的法律解释、业务卡完整性未通过项继续以 [跨场景记录](AGENT_STABILITY_20261030.md)、[法律检索复测](LEGAL_RETRIEVAL_RESEARCH_20261029.md) 为准；集成与部署检查不将这些未通过项改为已验收。

## 已有资料与数据库

活动数据为 `/data/238_2/wenbo/Project/AItea/data`，约 173 GB；活动清单仍为同项目的 `config/`，没有改绑私有配置或重建检索库。

停前后端后生成 SQLite 独立快照 `.dev/production/backups/20261030-pre-evidence-52d908f4/tea.sqlite3`，`quick_check=ok`，SHA-256 和计数收据保存在同目录。这只是数据库快照，原件与运行中的 RAGFlow 未停服，不能视作整套实例的一致性备份。

副本先验证结构 27 → 28，再恢复本地服务完成实际升级。前后以下记录数一致：用户 3、业务 2、资料 2041、消息 44、任务状态 84、RAGFlow 文档回执 2051、分块回执 214263。检查的 1903 个不同原件路径均存在；回执数量不等于已审核有效法规数量。

实际 RAGFlow 认证列表请求成功；恢复后 API 健康检查为 `ok`，助手为 `live`、检索为 `ragflow`。本次未重跑未知付费请求，未执行真实模型容量测试。

## 容器与转发范围

使用 wenbo 当前的 `wenbo-vm` Docker context。API、前端、网关镜像已实际构建；虚拟机采用 CPU 模拟且只有 4 GB 内存，本次取消了耗时的容器内前端源码编译，改为本机 Node.js 22.23.0 构建 standalone，再通过相同 Dockerfile 的 `prebuilt` 阶段封装。产物核验源码指纹、路由前缀、API 代理、Node 版本和包内链接。默认源码容器构建由新增的 push/PR Docker CI 独立执行。

隔离容器测试只使用专用合成资料卷和本地模型替身，未读取私有资料或访问付费模型。验证 `/aitea` 跳转、带指纹资源、外部 Host 转发、登录退出、Cookie 范围和至少两段不同时间到达的 SSE。实测四段持续输出，约 0.73 秒跨度；这是人为设置每段 250 ms 的模拟模型，不能用于推断真实上游速度。

测试中发现并修正 Caddy 文件 capability 与移除权限冲突，以及回环地址隐含 TLS/Host 匹配问题；同时将隔离检查改为网页所用的节点会话接口，并设置有效的有界模型预算。

Playwright 实际打开 `/aitea/tea`、进入演示业务、发送节点会话并显示模拟模型完整正文；1440 × 1000 与 390 × 844 视口均完成，无 JavaScript 页面错误或横向溢出。浏览器环境缺少中文字体，截图只用于结构核对，不作为中文排版的完整视觉验收。

`rowan@tcyun` 的 Serino Caddy 已实际校验独立 `/aitea` 片段，未安装或热加载路由。一次临时 SSH 链路测试收到四段输出，首段 2.27 秒、末段 3.03 秒；该次仅为隔离实例，未包含公网 HTTPS 或真实资料检索。Docker 虚拟机禁用 TCP 转发，浏览器临时检查通过 SSH 命令的标准输入输出连接合成实例，未修改其 SSH 配置；正式部署不依赖这个临时测试入口。

## 尚未完成的生产切换

wenbo 的 Docker 守护进程位于 `wenbo-xianyu-docker` 虚拟机，该机器不存在活动数据的绝对路径。本机不存在可用 Docker socket，rootless 所需 `newuidmap/newgidmap` 未安装，当前用户没有免密管理员权限。资料挂载检查按预期失败并清除探测标识，没有创建替代空库。

生产容器与 `https://aerisun.top/aitea` 尚未启用，需要可访问原始资料盘的本机 wenbo Docker，或由管理员准备相应运行环境。后续按 [部署说明](DEPLOYMENT.md) 重新生成参数、核验真实挂载、启动健康容器，再建立正式转发并验证原件与账号。不能用隔离容器通过代替这一条件。

原有本地开发工作台已恢复到 `http://127.0.0.1:3000/tea`，API 为 `127.0.0.1:8000`；API 内批量 worker 保持 `external`，独立采集监督器及 RAGFlow 未因本次部署停止。日志在 `.dev/api.log`、`.dev/web.log`，数据库快照继续保留。
