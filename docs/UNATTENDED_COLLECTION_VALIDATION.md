# 无人值守采集验收与运行交接

验收日期：2026-10-20。功能已合入本地 `main`，集成提交 `7e48094`；运行说明见 [UNATTENDED_COLLECTION.md](UNATTENDED_COLLECTION.md)，来源范围见 [OFFICIAL_SOURCE_POOL.md](OFFICIAL_SOURCE_POOL.md)。

## 正式运行

`aitea-collection.service` 已启用，systemd 状态为 `active/running`、`enabled`，账号 `Linger=yes`。两个任务 `official-id`、`official-sg` 均存活；正式服务初始 MainPID 为 2998041。服务和 fleet 位于项目 `.dev/collection-service/`，无需保持当前聊天在线。

原件、请求账本、来源健康与映射保存在 `data/official-updates/{ID,SG}/`。当前 `data` 解析到本项目 `restores/id-sg-recovered-20261019/`。16 个来源为 ID 9 个、SG 7 个。国家秘书处 PERPU 目录本次返回 404，已按 HTTP 故障留证，不能从单次 404 认定域名过期；其失败不影响其他来源。

截至 07:44 UTC（北京时间 15:44），正式进程沿用旧 70 次请求的账本并继续抓取，ID 43 次、SG 79 次请求，其中 95 次成功，3 次仍在途。此为运行中的时间截面，不代表全部清单原文或法律审核完成。法规资料仍须经过既有分级、受限解析、审核与入库规则。新采集器不调用模型，不修改个人资料或正式法规审核状态。

旧 `id-perpu` 与 `sg-acts-2026` 周期进程已通过各自 STOP 标记正常排空，停止前核对了 PID、启动时刻和完整命令。原始文件、配置和失败历史保留。现有 OCR、向量任务及应用进程未被停止；应用 `/api/tea/health` 和 `/tea` 均实际返回 200，现有 `.env` 与此前配置备份的摘要一致。

## 实际验证

- 新增功能最终 101 项测试通过，覆盖持久冷却、跨天额度、缓存 304、停止/恢复、坏源隔离、并发上限、队列轮换、多父版本映射和 Setneg 的精确范围与分页。单主机峰值 2 和两个国家来源实际并行均有测试覆盖。
- 首次全仓检查为 3453 项通过、2 项失败。其中新停启测试读取状态早于守护器落盘，已改为同时等待进程启动与状态持久化；另一项旧测试要求临时目录位于项目外，原检查把临时目录指定在项目内，改为独立 RAM 临时目录。最终 `PYTEST_ADDOPTS=--lf make check` 两项复跑通过，后端全量 Ruff、前端类型及 208 项交互检查通过；最终新增功能 101 项亦独立复跑通过。未宣称在最后修正后再次从头运行全部用例。
- `make build` 成功，生成 164 个页面。构建使用已有独立 RAM 构建目录以兼容 Turbopack 对依赖链接的限制；1318 个前端及 Makefile 构建输入逐文件 SHA 与集成代码一致。
- 真实临时 systemd 服务验证了两国并行、全部首批 15 个来源轮换、正常 TERM 返回 75 后再启动自动续跑。强杀 MainPID 后 systemd 的 NRestarts 从 0 增为 1，新主进程和两国任务重新运行，并继续完成 4 次 GET；原 228 个证据文件逐一摘要保持。该次硬杀发生在无在途请求时，未将其表述为在途 HTTP 故障注入。
- 正式 16 来源服务也已实际启动，新增 Setneg 的 404 已写入正式来源观察与请求账本。Python、依赖、uv 和数据均在持久磁盘。已核验 enable/linger 和异常退出恢复，未重启整台服务器。

以上原始日志和 JSON 回执位于 `.dev/unattended-collection-20261020/validation/`：`production-activation.json`、`production-latest.json`、`legacy-retirement.json`、`systemd-validation/report.json`、`unattended-check.log`、`unattended-final-check.log`、`unattended-targeted-final.log` 与构建输入清单。

## 数据迁移与备份

初始实采 70 次请求及其原件从验证工作树迁入正式数据目录，逐文件验证后只变更路径绑定；随后以明确的范围升级加入历史已核验的 PERPU 入口。原 15 个来源、国家、额度、全部旧请求及原件未重置。旧、新来源池保存在 `data/official-updates/pools/`，迁移及唯一新增来源的清单分别在验收目录 `data-relocation.json`、`source-pool-upgrade.json`。

启用正式服务前已生成并逐文件验证项目内组件备份 `backups/unattended-collection-20261020/`，包含 685 个文件、16,906,266 字节，含初始原件/账本、来源池及正式 fleet/unit。`COMPLETE.json` 的 SHA-256 为 `308b53b677752a4a11f9a7e5c90dafb6276a71491f11f0d508dfc2ab58be6435`。该组件备份有明确时间截面，不包含之后持续取得的新文件，也不替代既有正式数据库全量备份；后续完整数据备份会包含 `data/official-updates/`。跨机器恢复必须保留全部账本并按运行说明审核路径绑定。

持续运行不等于所有外站最终可达。超时、限流、失效、空目录和存储不足分别进入可审计等待，符合范围的其他官方来源继续推进，待补原文和法律效力审核不会自动冒充成功。
