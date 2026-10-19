# 官方来源有界窗口的中断恢复

`scripts/official_source_supervisor.py` 将既有 Setneg/eGazette 双来源监督流程纳入项目代码，使用已经校验的 `fixed-official-source-supervisor-v2` 运行包作为输入。新增 `scripts/official_source_supervisor_recovery.py` 处理采集进程消失、结束报告未写出、原件已保存但未登记的情况。

该入口不会自动安装系统服务，不替换已运行的监督器，也不改变已有包。切换须先让原监督器排空，再由持久的运行会话启动新入口。既有运行包中的 `operator_control.py start` 仍启动包内旧 `run.py`；要使用本次恢复能力，须显式调用项目新入口，或在新的独立封存包中将入口接到该脚本。旧包必须保留。

## 输入和启动

运行包保留 `runtime.json`、`manifest.json`、`repository.json`、固定 `plan.json`、两份 SG 模板及窗口模板。`runtime.json` 是路径重定位配置，不在包内容清单内；其项目、数据目录和统一控制目录仍由现有包预检校验。每次启动核对包清单、正式 collector SHA、已冻结选择和源库原件路径；每次窗口核对固定代码和范围。该版本支持已有的两国来源目录布局，不接受任意来源脚本。

```bash
uv run --project backend python scripts/official_source_supervisor.py \
  --bundle-dir /absolute/path/to/verified-runtime-bundle --inspect

# 原监督器已经排空、来源范围及存储门禁均确认后，在持久会话中运行：
uv run --project backend python scripts/official_source_supervisor.py \
  --bundle-dir /absolute/path/to/verified-runtime-bundle
```

`--inspect` 只读，不启动 worker。运行模式始终复用 `operations/official-source-supervisor/supervisor.lock`，不会同时启动第二个监督器。包内停止命令创建的持久 `STOP` 继续生效；SIGTERM/SIGINT 只请求本监督器排空已拥有的有界窗口。不会按进程名称杀进程，也不会停止只被观察到的其他 worker。重新启动不清除 `STOP`。

恢复不扩展固定目录、单站并发、字节预算、尝试上限或退避时间。来源抓取只发生于既有 collector 子进程。监督器和离线恢复路径禁止网络；模型配置在启动前移除，原生模板仍显式禁用 OCR 和 embedding。

## 进程与窗口一致性

每个窗口在临时目录准备完整计划、前一份报告与脚本后原子发布。启动子进程前创建独立的 `collector-process.lock` 或 `native-process.lock`，通过继承文件描述符让子进程持锁。监督器即使在 `Popen` 后、保存 PID 前中断，继承锁仍会阻止重复启动。

`*-process.json` 保存 PID、Linux 启动时钟、启动参数与命令摘要。PID 重用不代表原进程仍存在；无法读取身份、锁仍在使用或实际进程尚存时，只观察和等待，绝不因观察超时启动替代进程。结束报告已写出但原生进程尚未退出时，也不会开启下一窗口。

发现未闭合窗口时，先恢复该窗口，再判断是否开启新窗口：

1. 核对原窗口计划、前一报告 SHA、固定目录选择、正式 collector 和模板摘要。多个未闭合窗口、范围改变或证据冲突进入 `needs_attention`，不重置历史。
2. 确认 collector 已退出并取得其来源锁。保存首份恢复基线、旧报告和源映射。恢复本身再次中断时继续使用同一份基线。
3. SG 校验冻结目录文件、全部当前/历史下载收据及原件 SHA，再调用既有 `GazetteCollector.stage_originals` 离线登记。Setneg 只读核对 journal/CAS，调用已有 `export_state` 和 `prepare_library`；旧独占临时报告先按内容摘要留存，再进行正常原子导出。
4. 核对旧文档身份与原件、元数据与版本历史、关系、向量 BLOB、来源证据映射和已有成功物理页。只允许新原件登记与原生页正常增长。不会将已审核来源重置为草稿，也不会修改法律效力。
5. 写入明确标识 `interrupted_reconciled` 的来源报告，然后写窗口 `final-report.json`。两步间再次中断可重放，新增文档计数始终相对首份持久基线，避免把累计数误称本次新增。

恢复先于原生排空检查，因为旧原生 worker 可能正等待缺失的来源结束报告。只有 PID/启动身份、实际命令、窗口模板及 `/proc` 中原生来源锁归属都能证明是同窗 worker 时，才允许离线登记与它并行。其他原生锁持有者只会被观察。报告发布后，同窗原生 worker 自然排空，无需终止。

SG 的 `elapsed_seconds` 表示已有进度证据给出的运行下界；`elapsed_seconds_total` 使用原窗口完整预算与已观察耗时的较大值，明确标记为保守预算记账。无法证明未消耗的时间不退回下一窗口。这不是正常完成或实际运行时长的证明。Setneg 恢复报告也不直接宣称范围完整；后续正常 collector 窗口按原规则核对范围，不重置耗尽尝试。

## 恢复证据和限制

窗口内 `interrupted-recovery/` 保存首份基线、旧报告、已保存原件和失败收据摘要、登记后保护核对及恢复结果。原报告、journal 尝试记录和源文件不会因恢复而清空。目录与 JSON 有大小/数量上限，原件逐文件计算摘要，登记继续采用现有逐文档事务。短暂数据库锁复用 `retry_local_database`，最多三次本地尝试；不把下载或模型请求放进该重试函数。

现有源库须已经显式升级到当前 schema，恢复入口不负责结构迁移。来源需有完整 CAS、收据及计划；缺失原文、摘要不符、已审核或不同来源版本均停止等待人工核对。未知身份的原生进程、损坏解析任务和永久来源拒绝不被伪装成完成。这次没有扩大原生解析的过期租约恢复范围，也没有增加系统开机自启动。

## 验证

新增 `backend/tests/test_official_source_supervisor_recovery.py` 使用真实小型 SQLite/CAS 和实际子进程退出，验证下载后未登记的四份原件无需网络即可恢复、恢复再次中断后无重复版本、失败收据保留、活进程/继承锁保护、同窗原生自然排空、旧成功页和元数据保全、SG 预算不返还及 Setneg journal/临时报告恢复。另运行既有 eGazette 和 Setneg collector 回归。

这组验证不等于全量法规采集完成，也不表示新入口已替换线上运行包。真实新包的排空、重新封存、启动和停止演练由交接执行者单独记录。
