# 周期官方原件采集

`scripts/periodic_official_collection.py` 在独立目录中周期读取 Setneg 或 eGazette 官方目录，把新增资料、目录变化和有界原件复核冻结成新的采集轮次。原件下载继续使用现有官方下载器、来源规则、CAS、大小和连接限制。它不创建应用 Service，不运行解析/OCR/embedding，不导入或发布法律，不领取正式库的任何 processing_jobs。

这补充了 `official_source_supervisor.py` 的固定范围续采能力，没有改写旧监督器、旧密封计划或 621 文档范围。eGazette 出版原件也不等于 SSO 当前合并文本。此入口不包含 SSO、其他政府站点、清单全部来源或自动法律审核。

## 建立固定配置

从对应代码工作树执行，Python 使用项目锁定的 uv 环境。每个来源使用一个独立 schedule。目录必须是项目 `.dev` 或显式已有 `--storage-root` 的新子目录，拒绝符号链接和覆盖已有 schedule。

```bash
mkdir -p .dev/official-periodic
uv run --project backend --locked python scripts/periodic_official_collection.py init \
  --schedule-dir "$PWD/.dev/official-periodic/sg-acts-2026" \
  --source egazette --categories 'Acts Supplement' \
  --year-from 2026 --year-through 2026 \
  --interval-seconds 86400 --window-seconds 600 \
  --original-byte-budget 134217728

uv run --project backend --locked python scripts/periodic_official_collection.py init \
  --schedule-dir "$PWD/.dev/official-periodic/id-perpu" \
  --source setneg --types PERPU \
  --interval-seconds 86400 --window-seconds 600 \
  --original-byte-budget 134217728
```

默认剩余空间门槛是 **40 GiB**，开窗口前还保留 128 MiB；项目只有约 13 GiB 可用时返回 `storage_wait` 是正确行为。若操作者已根据其他在途任务明确核算并授权更小预算，可在 **init 时显式**设置 `--min-free-bytes`，最小 256 MiB。此值不会由程序自动降低。

`--original-byte-budget` 限制每轮，`--total-original-byte-budget` 限制整个 schedule（默认 10 GiB，最大 64 GiB）。每次原件 GET **发送前**在独立 `transfers.sqlite3` 中以短事务预占两种预算并记录尝试；并发窗口不能分别花掉同一额度。成功后只计实际字节。失败或崩溃时未能证明实际传输量的请求保留整个预占额，故计数是保守上界，不等于实际网络字节。达到总预算显示 `budget_wait`，不会在新周期清零。目录证据不计入原件字节预算，仍受响应大小、窗口期限和磁盘门槛约束。

配置保存来源类型、分类/年份或法规类型、清单文件 SHA、当前采集实现 SHA、时间间隔、窗口时长、重试上限、磁盘门槛及原件预算。年份不会跨年自动扩大。修改配置、代码绑定、路径或来源清单后拒绝继续；升级前先排空，保留原 schedule，再建立经过审阅的新配置。本入口不自动迁移运行状态。

**首次周期没有历史基线，会尝试采集所选目录范围中的原件**。先选明确的小范围和字节预算，不能默认它会识别正式库已有的 3890 份资料。程序不扫描正式库，也不解压历史备份寻找可复用结果。

现有 Setneg `--reuse-receipts-from` 和 eGazette `--reuse-campaign` / `--reuse-source-dir` 是各自专用采集器的显式历史复用入口；它们不是本周期入口的自动 seed 开关。Setneg 复用要求任务已被当前目录发现。要把历史来源纳入新 schedule 的基线，还需要单独核验目录身份、SHA 与完整回执的初始化流程，本次不把未经绑定的历史目录直接作为成功基线。

## 运行、停止与重启

```bash
uv run --project backend --locked python scripts/periodic_official_collection.py run \
  --schedule-dir "$PWD/.dev/official-periodic/sg-acts-2026"

uv run --project backend --locked python scripts/periodic_official_collection.py status \
  --schedule-dir "$PWD/.dev/official-periodic/sg-acts-2026"
```

`run --once` 只处理一个到期的有界窗口，适合一次性验收或外部定时器；它不表示整个采集轮次已完成。持续 `run` 在窗口之间等待到期时间并自动继续。这里只提供可运行入口，不替用户安装 systemd、cron 或修改系统配置。

在 schedule 根目录建立 `STOP` 文件可停止后续领取。正在执行的有界窗口继续保存回执并排空，监督器随后退出。SIGTERM/SIGINT 发给已核验归属的监督器 PID 也采用相同行为；不要向未知进程或整个进程组发信号。恢复前核对 `state.json` 和窗口日志，明确移除该 schedule 的 `STOP`，再执行同一 `run` 命令。

每个 schedule 使用 `supervisor.lock` 防止重复监督器。每个窗口复用现有 `spawn_owned` 的继承 flock：即使父进程在 Popen 后、PID 记录前被杀，仍在执行的子进程持锁，重启不会额外启动下载。PID 校验同时使用 Linux `/proc` 启动身份；同号新进程不能冒充旧 worker。该协议限同一 Linux 主机，不用于网络文件系统或跨主机共享调度。

缺最终报告时先确认旧进程及继承锁已退出，再由新窗口核验已有回执续跑；成功原件不重下。原件已发送但没有完整回执也消耗尝试额度，不能通过崩溃逃过上限；只收到字节而没有合格回执的记录不冒充成功。最后一个允许窗口意外退出时，离线读取持久回执形成交接结果，不凭进程退出码宣称成功；离线对账自身也支持中断重放。异常和旧尝试均保留。

## 周期、退避和原件复核

一轮先重新读取官方目录，验证完整性后保存 `catalogue.json` 和 `selection.json`。后者冻结新增、变化、目录消失、保留原件引用与复核选择。只有冻结选择及官方详情中通过现有规则的原件链接可以下载；不追踪任意跨域链接，不绕过 403。

正常重启复用本轮目录和原件回执。新周期使用新的目录、cycle ID 和 window ID，不覆盖旧目录页、选择、日志或原文。复核时点固定为轮次创建时间，因此在重启时跨越复核间隔也不会改变已冻结的选择。前轮 handoff 的 SHA 绑定在新轮计划中。

瞬时失败采用持久指数退避，等待时间不短于供应商 Retry-After；上游冷却时间跨窗口和周期保留。即使最后一个窗口达到次数上限，也会把最大冷却截止时间写入 handoff，下一周期的目录和原件请求均等到该时间后才能开始。默认每阶段最多 16 个窗口、每原件最多 8 次未成功确认的尝试。403、404 等永久失败与耗尽结果保留为缺口；同 URL 即使标题等目录元数据改变也不重置终止回执或未成功尝试。新周期仍能发现其他新增资料。失败页不能靠反复启动 `run` 清零。

目录元数据不变时，成功原件通常只核验本地 SHA 并引用原轮证据。为发现同 URL 静默替换，默认 `--recheck-after-seconds 604800 --recheck-limit 10`，按上次成功复核时间从旧到新选择少量原件。`recheck-limit=0` 显式关闭这一复核。当前专用采集器没有可直接复用的 ETag/Last-Modified 接线，因此本入口使用有上限的完整重取，不声称进行了条件请求。

- 字节 SHA 相同：记录 `same_bytes`，不创建新的法定版本。
- 字节不同：两份 CAS 和回执均保留，记录 `changed_bytes_candidate_version` 及旧、新原件引用，等待审核。
- 复核失败：保留之前成功的历史原件，显示 `recheck_gaps`；达到终态的本次复核不在下一目录周期被静默重置。
- 404 或目录消失：只记录观察，不能推断法规废止或删除历史文件。

复核量有上限，因此任意时刻不能声称所有同 URL 原件都已是最新。过大的新目录会受每轮预算、窗口次数和磁盘水位约束，以明确缺口结束。

## 证据与交接

```text
schedule/
  config.json                 固定配置与源码/清单绑定
  state.json                  下次时间、当前周期、进程身份、状态
  transfers.sqlite3           全调度原件发送意图、次数、保守字节预算
  STOP / supervisor.lock
  cycles/cycle-000001-<id>/
    plan.json                 本轮范围及前轮 handoff SHA
    catalogue.json            完整官方目录快照
    selection.json            不可变差异与明确原件选择
    source/                   官方目录、CAS 原件、原始回执/尝试历史
    windows/window-000001/    每窗口计划、日志、进程记录、结果、对账记录
    handoff.json              可审查原件、未完成项、版本候选和历史引用
```

`scheduled` 只表示下一周期已安排，`ready_for_review` 只表示本轮原件准备完成。`ready_for_review_with_gaps`、`catalogue_incomplete`、`storage_wait`、`budget_wait` 不能解释为全量完成；交接记录始终 `legal_review=unreviewed`、`model_calls=0`、`automatically_imported=false`。

应完整保留 schedule 树：新轮可能引用旧轮 CAS，不能只复制最新 handoff 或单独删除旧周期。当前输出是原件及证据包，尚不是有 `tea.sqlite3` 的可直接导入应用库。审核后可用现有 `collect_indonesia_official.prepare_library` / `GazetteCollector.stage_originals` 离线准备明确选择，再对 `import_collected_resources.py` 执行显式 ID dry-run、apply；解析、OCR、向量和正式导入都需各自独立的冻结计划。本周期入口不会自动开启这些后续步骤，也不会扩大已有任务范围。

新增回归使用真实 collector/journal/CAS 与本地 HTTP 桩，覆盖两个周期发现、成功不重复、限流冷却、永久失败、轮转复核、SHA 变化、并发进程锁、父进程崩溃、断点恢复、STOP 和证据篡改。没有在测试中访问真实官方站点或付费模型；外部来源可用性须由操作者另做小范围实际验收。
