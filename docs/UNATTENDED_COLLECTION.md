# 官方资料持续采集

已存资料的原件、正文、一二级映射、向量状态及待补缺口，可用 `make collection-catalog` 生成统一入口，见 [COLLECTION_MATERIALS.md](COLLECTION_MATERIALS.md)。

`make collection-prepare` 在当前项目 `.dev/collection-service/` 生成绑定绝对路径及校验和的双国任务配置和 systemd 用户服务。使用既有 uv 环境，不启动模型调用，也不修改既有 OCR、向量任务或业务账号。来源池与核验记录见 [OFFICIAL_SOURCE_POOL.md](OFFICIAL_SOURCE_POOL.md)。

来源池是“已核验来源种子与抓取策略”配置，不是逐份法规 URL 的静态清单：每条定义官方入口、国家和业务主题、允许主机、发现关键词与复查间隔。采集器从入口解析目录和链接，在允许主机内持续发现并排队符合主题的目录页、详情页及原件；候选、父页、摘要、版本关系和每次请求回执均持久保存。单轮请求数、链接队列、响应大小、主机并发和日额度有界，因此不必人工枚举每个文书地址，同时也不会因网页链接自动扩大到未审核的域名或业务范围。新发现的域名、主题或资料类型先作为待核候选，完成官方身份与业务范围核验后再修改配置。

## 启动和恢复

先执行 `make setup` 安装锁定依赖，并确保 `data/` 所在卷有充足空间。准备后执行：

```bash
make collection-prepare
systemctl --user link "$PWD/.dev/collection-service/aitea-collection.service"
systemctl --user daemon-reload
systemctl --user enable --now aitea-collection.service
systemctl --user status aitea-collection.service
make collection-status
```

无人登录和服务器重启后运行需要管理员为此账号开启 linger：`loginctl show-user "$USER" -p Linger` 应输出 `Linger=yes`。无桌面会话的终端还需设置当前账号的 `XDG_RUNTIME_DIR=/run/user/$(id -u)` 及 `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus`。不要覆盖其他账号的用户服务或修改系统网络配置。

服务与两个国家采集器分别维护进程身份、锁及持久状态。子进程异常退出后自动退避重启；watchdog 异常退出后 systemd 自动重启。在 systemd 的 `KillMode=mixed` 下，主进程异常退出会清理原服务进程组，再从请求账本恢复，不保证保留原 PID。脱离 systemd 的 watchdog 重启可接管身份、启动令牌和继承锁相符的幸存子进程。正常停止最多等待当前有界请求结束；此服务只包含单请求 20 秒的新采集器，不接收旧的长窗口周期任务。

`systemctl --user stop aitea-collection.service` 正常停止服务；重新启动继续现有状态。全局紧急停止可在 `.dev/collection-service/state/STOP` 写入标记；单国停止为 `data/official-updates/ID/STOP` 或 `SG/STOP`。这些标记持续有效，重启不能绕过。单国自行退出 75 后，任务持久暂停；恢复时先停止整个服务、移除对应单国 STOP，再执行下列命令并重新启动服务。全局 STOP 仅需停止服务、移除标记再启动。不得通过删除状态、锁、请求账本或更换输出目录来解除暂停。

```bash
uv run --project backend --locked python scripts/collection_watchdog.py resume-task \
  --fleet "$PWD/.dev/collection-service/fleet.json" \
  --state-dir "$PWD/.dev/collection-service/state" --task official-id
```

## 数据与访问边界

两个国家在 `data/official-updates/{ID,SG}/` 分开整理：

| 路径 | 含义 |
| --- | --- |
| `state.json` | 来源健康、复查时间、有界发现队列、已知 URL 与内容摘要 |
| `host-cooldowns.json` | 跨来源、跨重启仍生效的主机等待截止时间 |
| `requests/YYYY-MM-DD/*.json` | 请求前落盘意图、额度预留、响应结果及原始字节位置 |
| `files/` | 复用项目 BlobStore 的不可变原始字节，以内容摘要校验 |
| `events/YYYY-MM-DD/*.json` | 版本、父页引用、原件映射、失败观察、换源与队列归档记录 |

所有数据留在项目目录中；项目 `data` 若为经确认的挂载或链接，实际存储随该卷。使用现有共享总库与个人上传隔离规则，采集器不建立个人账号，不修改任何用户的私有资料。备份需包含整个上述目录、来源池以及 `.dev/collection-service/` 配置；恢复时停止服务，原样保留请求账本、映射与健康状态，按新项目绝对路径审核迁移配置，不能把非空目录当新任务初始化。日志轮转仅影响诊断输出，原始证据与失败历史不会轮转删除。

每个引用保留父页 URL 和内容 SHA-256；同一原件被多个父页或父页新版本引用时，追加引用事件。目标原件真正读取或校验 304 后再记录目标摘要。仅从旧缓存得知的摘要明确标记为 `last_known_target_hash`，不能冒充本次取得。队列或缓存元数据到上限后归档旧元数据以容纳新发现，原件及映射历史保留。

API 服务每 30 秒读取 ID/SG 原始仓中新写入的 `content_version` 事件。首轮从最近 24 小时开始，之后用数据库游标续读；服务中断不会越过未完成事件。来源 ID、国家、HTTPS 主机、原件路径和 SHA-256 全部重新核验。已批准的指导来源正文，以及法规目录中明确标为文书的 PDF、DOC、DOCX 或 HTML，会以 `SYSTEM` 资料建立不可变版本并排入现有有界解析队列：HTML 使用项目 HTML5 结构解析，PDF/Office 使用 MinerU API。目录页、附件之外的普通文件、不支持格式或校验不一致的材料不会进入正文解析，原始采集账本仍保留其证据。

自动建立的资料保持 `draft` 法规审核状态，并保留国家、来源 ID、原始 URL、来源事件时间、内容哈希、前版哈希、版本和解析定位；导入收据还区分声明 MIME 与文件签名确认的 MIME，记录来源类型、发现用途、父页面 URL/哈希和标题，便于逐条核对来源链。无扩展名详情地址只有原件签名确认属于 PDF/Office 才进入解析；符合条件的旧跳过收据会重新验件并恢复导入。首次启动会从保留的版本事件完整回放，之后按游标增量处理；临时故障不推进游标，重试不会重复建档。自动解析不代表法规已生效、已审核或可用于 RAG。解析失败沿用原件和现有恢复队列；付费远端调用结果未知时只核对回执，不盲目重复提交。导入记录保存在 `official_source_imports`，处理队列与法规审核状态分别展示。

## 失效处理和资源额度

HTTP 410 记录资源已撤下；404 需至少 3 次、间隔至少 1 小时且跨越至少 24 小时才列为陈旧候选。403/401 为访问受限，不认定网站过期。DNS、超时和 5xx 连续跨天失败记为长期不可达，不能据此声称域名注册已到期。失败类型、时间与原 URL 保留，之后低频复查；其他相同国家、相关主题的已核验官方来源持续获得采集机会，换源不代表法律文本等价或缺失原文已补齐。池外新站点需核实主管机关身份后以配置升级纳入，不允许网页内容自动扩展执行权限。

429 或 Retry-After 会持久阻止该主机继续请求；等待时间可长于默认退避上限。已发现原件和根目录复查独立调度，避免根页的 24 小时间隔阻塞原件队列；每日额度恢复按 UTC 日期，不重置过去记录。空法规目录以配置间隔低频复查，不计作有效业务原文。服务断电后未完成的 GET 保留额度预留，后续按幂等请求退避重试。

请求回执与健康观察只保存枚举化 `failure_code`（如 `dns_resolution_failed`、`network_timeout`、`tls_certificate_verification_failed`、`redirect_scope_rejected` 或 `http_403`），不保存底层异常文本，避免把凭据或动态请求内容写入监控状态。非重试的来源范围拒绝归类为内容/策略无效，不伪报为网络故障。

默认两国合计最多 8 个来源任务并行，单主机 2 个连接，每次响应最多 8 MiB、20 秒，每个来源每轮最多 4 个请求。每国每天最多 256 次、512 MiB，每来源每天最多 24 次、32 MiB；请求前保守预留最大响应额度。成功按实际传输字节结算；若确认连接在收到响应前失败，则按 0 字节结算；响应体传输中断、字节数不明或进程在请求中退出时继续保留全额预留。磁盘可用空间低于 8 GiB 加预留时暂停取件并自动复查。网络、磁盘或上游限流无法保证最终成功；这些情况保留明确等待/失败状态，其他合格来源继续工作。

来源配置和额度与已有状态绑定，直接改动会拒绝继续，避免偷偷扩大范围或重置额度。升级时先停止服务、备份现有数据和配置，保留历史来源 ID 及账本，再审核迁移；`make collection-prepare` 对相同配置幂等，对不同旧配置拒绝覆盖。

仅移动项目路径且国家、来源池 SHA 和额度均不变时：取得旧采集目录锁，复制到新卷的临时目录，逐文件核对 SHA；只变更 `state.json` 的 `binding.root` 为新输出目录的真实绝对路径，核对其他字段与所有请求/原件未变，再原子发布新目录。保存旧状态摘要、新状态摘要、逐文件清单和迁移时间，旧副本保留。随后在新项目重新准备 fleet/unit，停用旧服务后启用新服务。来源池或额度变更不能套用这一单字段路径迁移，也不能删除历史账本重建。

实现依据：[systemd 服务重启与停止语义](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[systemd 进程组终止语义](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)、[HTTP 410](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.11) 与 [Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)。
