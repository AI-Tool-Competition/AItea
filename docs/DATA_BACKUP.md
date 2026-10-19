# 完整实例备份、校验与恢复

`app.data_backup` 与 `scripts/backup_data.py` 为现有单机 SQLite 部署提供可独立恢复的数据包。备份覆盖整个 `data/`，包括系统法规总库、每个用户的私有上传、账号、会话、业务、模板快照、事实、消息、审核与版本历史、关系、采集检查点、原件、图片、证明文件和真实向量。工具不按账号筛选，也不会把备份当作跨账号查询接口；只有能够读取服务器数据目录的运维身份应使用它。

完整恢复保留所有者、文档 ID、family、不可变版本和原权限边界。系统法规仍归属于不可登录的系统身份，所有登录用户共享读取；用户上传仍属于原用户和原业务。恢复不会把待核法规变成正式法规，不会将二级附件加入向量检索。

## 创建与校验

默认将数据、备份及恢复放在项目内不同子目录：业务数据为 `data/`，完整备份为 `backups/`，恢复为 `restores/`，后两者已排除 Git。部署方授权使用其他磁盘时，可以为下面命令指定受控外部绝对路径，并在项目内保留位置清单。不要把备份放进源 `data/` 导致递归增长，也不要用普通用户上传目录保存全账号备份。从仓库根目录执行：

```bash
mkdir -p backups restores
chmod 700 backups restores

uv run --project backend --locked python scripts/backup_data.py create \
  --source-dir "$PWD/data" \
  --backup-dir "$PWD/backups/2026-10-16"

uv run --project backend --locked python scripts/backup_data.py verify \
  --backup-dir "$PWD/backups/2026-10-16"
```

目标父目录必须已经存在，目标目录必须是新目录且在源数据目录之外；工具不会覆盖旧备份，也不会把自己递归装进备份。已有历史备份和采集归档作为源树的内容保留，不自动清理。 冷归档 `ingestion-archive/` 中的 CAS 形状文件按原字节证据保存；历史故意损坏的测试负例以实际文件 SHA 校验，并在清单 `cold_archive_anomalies` 中逐条记录原地址摘要、实际摘要和路径。它们不能成为当前运行库的原件引用，根库和活动 campaign 的 CAS 地址、原件与 manifest 校验保持严格。应将历史备份放在 `data/` 之外，避免后续备份体积重复增长。

SQLite 根库先通过官方 online backup API 固定一致读快照，包含已提交 WAL 内容，不能用复制运行中 `tea.sqlite3` 的方式代替。根库和 `campaigns/**/tea.sqlite3` 均走 online backup。每个数据库的在线复制有独立的超时，默认 300 秒，可用 `--timeout` 设置为最多 3600 秒；不同 campaign 是各自的一致快照，不声称多个独立数据库具有同一事务时间点。原件采用先发布不可变文件、再提交数据库引用的既有规则；数据库快照取得后新产生的无引用文件可以随目录一起保存。

`files/` 中的原件始终保留原始字节，即使其内容恰好是 SQLite；`verification/`、`ingestion-archive/` 等冻结目录中的 SQLite 也原字节复制，避免破坏旧 receipt、清单和证明绑定的 SHA。这些冻结数据库若存在非空 WAL，工具拒绝生成成功包，要求先确认其归属与冻结状态；不会悄悄 checkpoint、转换文件或遗漏尚未合并的内容。清单以 online backup 完成后的独立快照摘要为证据；不对运行中物理主文件预先计算摘要，因为 WAL checkpoint 可以合法地同时改写主文件，物理主文件也不代表包含 WAL 的逻辑快照。冻结来源记录保留历史地址，不作为恢复后可直接启动的应用根目录。

备份包包含：

- `data/`：完整数据目录和空目录；原件、文本、证明、向量均在包内，不依赖旧 worktree。
- `manifest.json`：每个文件的相对路径、SHA-256、大小、复制策略、数据库结构版本、核心表数量以及明确排除的运行文件。
- `COMPLETE`：协议版本及清单 SHA-256。缺失此文件的临时目录不是可用备份。

已知运行临时目录 `files/.staging/` 被排除并记录；SQLite 的 WAL、SHM 和 rollback journal 由对应快照处理并记录，不作为另一套独立文件恢复。已发布 CAS 原件、分段下载检查点、冷归档和辅助证据不因扩展名被删除。符号链接、特殊文件、逃出源根的活动数据库文件路径、缺失原件、错误的内容哈希、跨账号 CAS 引用和未知的根库结构版本均导致拒绝。当前根库兼容结构版本 1–18，清单记录实际结构版本。

所有目录为 `0700`，数据包文件为 `0600`；恢复的 CAS 保持只读 `0400`。文件和目录都执行持久化同步，完整校验后在同一文件系统以 Linux `renameat2(RENAME_NOREPLACE)` 原子发布最终目录；不支持此能力的平台会拒绝发布。遇到空间不足、复制中断或校验失败，最终目录不会出现，普通异常会清理本次临时目录。进程被强制终止留下的 `.aitea-backup-*`、`.aitea-restore-*` 目录属于未完成操作，不能据此宣告成功。

SHA 清单用于检测误损坏和非一致修改，不是由外部密钥签名的防篡改凭证。应将备份存入另一个故障域，使用存储层加密、访问控制、保留期及不可变保留能力；定期执行恢复演练并将清单摘要留在独立受控位置。工具不会自行上传、配置云服务或删除旧备份。项目根目录的 `.env` 和外部模型配置不在 `data/`，需通过独立秘密配置渠道安全保管，不能提交 Git。

## 恢复到新目录

```bash
uv run --project backend --locked python scripts/backup_data.py restore \
  --backup-dir "$PWD/backups/2026-10-16" \
  --target-dir "$PWD/restores/2026-10-16-data"
```

`restores/` 必须已存在，`2026-10-16-data` 必须不存在。工具先完整验证 `COMPLETE`、清单和全部文件 SHA，检查 SQLite integrity、foreign keys、兼容的结构版本，以及根库和活动 campaign 的原件/图片/证明引用；恢复时再次核对复制摘要。已有目标即使为空也拒绝覆盖，备份和原实例不修改。

恢复仅对实际运行库的已知 SQL 文件句柄重定位：`documents.storage_path`、`checklist_pages.storage_path`、`collection_transfers.storage_path`、`collection_transfer_parts.storage_path` 中的旧数据根前缀替换为新数据根。历史 `manifest` JSON、typed registry、OCR 复用证明、原件、检索 chunk、FTS 和 float32 向量 BLOB 不做全局替换、重算或归一化。可执行 manifest 中的 CAS 路径必须为同账号的相对路径；旧式绝对 CAS 路径需要独立、有审核记录的数据整理，工具不会为让恢复通过而改写证明。

运行中进程的所有权不能迁移：`processing_jobs.processing` 和 `collection_items.fetching` 转回 `queued`，清除租约 token 与期限，删除瞬时网络租约。保留 phase、attempts、重试时间、错误、失败与取消状态、全部页级 OCR 和 embedding 检查点；已完成页和真实向量不重新处理。若原外部请求尚未产生持久化结果，恢复不能凭空补出结果，也不承诺跨进程的外部调用恰好执行一次。首次启动恢复实例前，先确认旧实例不会与新实例同时领取业务任务，再按现有 worker 退避和人工复核规则继续。

首次恢复写入 `RESTORE.json`；对已恢复实例再次备份和恢复时，保留所有历史回执字节，若该文件已存在则新写 `RESTORE-<唯一标识>.json`。命令返回值与新回执中的 `receipt_file` 给出实际文件名，并记录来源清单摘要、结构版本、重定位数量和释放的 claim 数。工具不实例化业务服务，不升级结构，不调用网络、模型、OCR 或索引重建。通过对应代码版本的验证后，再将新实例的数据目录指向此目录，由已有、独立验证过的升级链执行后续结构升级；不得用它覆盖仍在运行的 WAL 库。会话也被保留，因此备份应按认证秘密级别保护，迁移后的会话策略由实际部署决定。

上游依据：[SQLite Online Backup API](https://www.sqlite.org/backup.html)、[SQLite WAL 与备份边界](https://www.sqlite.org/wal.html)、[SQLite immutable 读取参数](https://www.sqlite.org/uri.html#uriimmutable)。
