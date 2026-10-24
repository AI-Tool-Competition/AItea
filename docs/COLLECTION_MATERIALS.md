# 持续采集与资料目录

日常从 `make collection-status` 查看双国采集任务，再运行 `make collection-catalog` 导出共享总库的可读目录。两者分别回答采集是否继续、已存资料在哪里及还有什么未完成，不把进程存活当成资料全部采齐。

```bash
make collection-status
make collection-catalog
make collection-catalog COUNTRY=SG
```

目录命令沿用 `TEA_DATA_DIR`；未配置时使用当前项目 `data/`，打印新目录位置。它不启动业务服务或模型，不改队列、审核或数据库，不导出个人上传。原件不重命名、不复制，仍以不可变内容哈希存储。文件权限为 `0600`、目录为 `0700`，供服务器维护人员查看，不能把整个目录公开托管。

## 查找路径

每次生成两个新的不可变快照：原有 inventory 保存数据库同一事务中的结构化证据；`catalogs/materials-<快照标识>/README.md` 为人类阅读入口，指向前者。生成失败不发布半成品，已发布的原有 inventory 保留。不会覆盖旧快照或改写历史证据。

| 文件 | 使用方法 |
| --- | --- |
| `documents.csv` | 筛选 `country`、`legal_level=primary/secondary/attachment`，用 `family_id` 和 `version` 查历史版本 |
| `relations.csv` | 查固定两端版本的一二级映射和法规引用；查看 `anomalies` 的缺口 |
| inventory 中的 `texts.jsonl` | 按文档与版本查正文、切片及定位 |
| inventory 中的 `ocr-pages.jsonl`、`ocr-regions.jsonl` | 用页码、区域和源哈希定位扫描件与识别依据 |
| inventory 中的 `source-mapping.jsonl` | 查格式与结构定位；保留不支持格式的显式缺口 |
| `sources.csv` | 查来源健康、失败原因、下次复查 UTC 时间；额度等待是可恢复状态 |
| `raw-latest-versions.csv` | 原始仓最新缓存的 URL、摘要和路径；原件被采集不等于解析或审核完成 |
| inventory 内的 `official-source-imports.jsonl` | 每个新内容版本被自动排入解析、跳过或阻止的原因与正式资料版本 ID，同一数据库快照读取 |
| `summary.json`、`COMPLETE.json` | 观察时间、统计、缺口、索引队列阶段，以及本目录文件的大小和 SHA-256 |

`original_path` 相对于数据根，不能直接移走原件。CSV 使用 UTF-8 BOM，危险公式前缀转义，可用电子表格筛选；原始字段保留在 JSONL。正文、网址及文件名始终作为数据处理。

## 完成度与边界

`text_complete` 表示解析完整性，未知和未完成保持可见。独立二级法规文本化，普通附件只保留原件。自动采集的新法规和指引会先建立带哈希的系统资料版本并排入解析；来源采集回执、解析队列与法律审核状态分别核对。一级资料的 `indexed_chunk_count`、`source_hash_matches`、`index_complete` 和 `matching_vector_count` 描述索引现状；`fingerprint_counts` 列明模型指纹。匹配向量只表示与当前切片输入哈希一致，不证明它通过当前模型、有效版本、适用期及审核。正式 RAG 准入仍由 [共享总库规则](SYSTEM_LIBRARY.md) 判断。

共享总库目录的 `summary.json.index_queue` 聚合已记录的 RAGFlow 代次、分块、解析任务、未知提交数和超过 15 分钟未更新的待处理代次，不含文档标识、正文或错误详情。多个旧模型/资料版本代次也会计入，因此该计数用于观察积压；每份资料的 `index_status`、切片哈希和向量数仍是判断当前版本的依据。`paid_result_unknown` 只通过远端回执只读核账，不自动再次提交。

`gaps` 中 `legal_review_not_published` 是审核待办，不等于文件损坏。`text_incomplete_or_unknown`、`index_incomplete_or_stale`、`vectors_incomplete_or_unknown` 分别指向文本、索引、向量待办。原件异常见 inventory 的 `anomalies`。统计只覆盖本库现有记录，不证明外部法规清单全部采齐。

原始状态在数据库事务之后逐国读取，各有观察时间与 SHA，不是跨进程原子快照。CSV 只列当前缓存；历史、归档缓存、旧版本与请求在 `official-updates/{ID,SG}/{requests,events,files}/`。候选保留 `unreviewed`，跨国税表还须经过业务相关度判断，官方来源本身不代表适用于本业务。

## 无人值守与交接

开机启动、停止、强制中断恢复、STOP 暂停和失效处理见 [UNATTENDED_COLLECTION.md](UNATTENDED_COLLECTION.md)。确认撤下或多次跨日 404 留证后降低复查频率，其他同国同主题官方来源继续采集；403、DNS 与超时保留独立原因。网页内容不能自行扩大来源权限。

目录不重算所有原件摘要；完整性核验和实例备份使用 [DATA_BACKUP.md](DATA_BACKUP.md)。备份应包含数据库、原件、原始仓及历史，配置与密钥另行私有交接，目录不能代替备份。升级保留旧证据，在隔离目录演练后切换；各层位置与权限见 [DATA_PLATFORM.md](DATA_PLATFORM.md)。
