# 向量并发、检查点与续跑

向量任务按账号、原件版本、模型标识和实际输入校验。只有正文完整且具备一级检索资格的资料进入向量任务；生成向量不改变法规的审核、国家或有效期门禁。共享法律总库使用系统主体，用户上传资料继续按账号隔离。

## 并发参数

| 参数 | 默认 | 范围与作用 |
| --- | --- | --- |
| `TEA_PROCESS_WORKERS` | 2 | 1–32；同库解析与向量任务的总领取额度 |
| `TEA_EMBED_WORKERS` | 2 | 1–32；向量文档任务额度，不超过总领取额度 |
| `EMBEDDING_HTTP_WORKERS` | 2 | 1–64；进程内实际向量 HTTP 槽，首次使用后变更须重启 |
| `EMBEDDING_REQUEST_CONCURRENCY` | 1 | 1 或 2；同文档一次处理中的请求并行数 |
| `EMBEDDING_BATCH_SIZE` | 64 | 1–64；每次请求的文本段数 |
| `EMBEDDING_HTTP_TIMEOUT_SECONDS` | 30 | 1–60 秒；HTTP 超时，整次调用另有 300 秒总边界 |
| `EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS` | true | 正式续跑脚本固定 false；响应不明时先留证，不自动重发 |
| `EMBEDDING_INPUT_CACHE` | false | 同账号、同模型、实际输入完全相等时复用已有向量 |

提高并发应比较成功输入字符数、耗时、实际请求峰值和错误率。多个进程分别拥有 HTTP 槽，不应以多开进程绕过额度。共用数据库的 OCR 租约也会占用总领取额度，配置 64 不代表实际同时发出 64 个请求。

2026-10-19 的真实服务探测中，32 路、每请求 2 段的一个持续窗口达到实际峰值 32，56 次请求全部成功；提高到目标 64 路、每请求 1 段后，实际峰值 59，最终 473 次请求中 464 次 HTTP 200、9 次 429，另保留一个响应处理被取消、结果未确认的输入记录。可比观察窗口的成功字符吞吐没有显示 64 路更快，因此当次续跑回到 32 路、每请求 2 段、60 秒超时。文本长度和窗口不同，这不是受控性能基准，也不保证后续不会限流。

## 固定范围执行

正式入口为 `scripts/complete_embeddings.py`。先冻结计划，再使用同一计划续跑；计划包含 `data_dir`、`owner`、`fingerprint` 和 `documents`，每份文档保存 `id`、`version`、`content_hash`、`source_url`、`size`、`storage_path`。预检核对原件字节、正文、分块输入、一级资格及模型标识；不发现或追加新文档，不下载、解析、OCR 或批准法规。

以下路径应替换为本次已冻结的计划、私有工作目录和模型配置文件。首次不加 `--apply` 执行预检；确认预检成功后，在同一命令末尾追加 `--apply` 执行：

```sh
uv run --project backend python scripts/complete_embeddings.py \
  --plan /absolute/private/selection.json \
  --output /absolute/private/embedding-run \
  --models-env /absolute/private/models.env \
  --http-workers 32 --request-concurrency 1 --batch-size 2 --http-timeout 60
```

密钥文件仅后端可读，不提交版本库。运行目录以文件锁避免重复执行；收到 429 后停止新增领取、保存 Retry-After，排空已有请求。启用逐请求持久回调时，已发出的兄弟请求仍在原超时边界内读取并保存有效成功结果；失败不会删除已保存批次。取消和失败后的状态可能是 `stopped`，只有缺失向量为零才是 `complete`。

`requests.jsonl` 记录真实 HTTP 请求、输入摘要和结果，`successful-inputs.sqlite3` 保存逐请求校验成功的向量。应用索引提交前再次核对租约、账号、原件、分类和完整输入。已有成功结果不会仅因同批其他请求失败而重新收费请求。对于已发出但结果不明的输入，默认暂缓；`--retry-unresolved` 是显式重试入口，可能再次计费，须保留旧请求记录。正常应用的同输入缓存不能消除两个同时发生的冷请求。

## 备份和迁移

运行中备份应用数据库和独立的 `successful-inputs.sqlite3` 时，都使用 SQLite 在线备份接口，不能只复制主数据库文件而遗漏 WAL。保留不可变计划、原始基线、已有元数据行摘要、请求日志和模型标识，并与原件 CAS、配置及应用数据库的备份清单关联。密钥单独按私有配置交接。

应用资料迁移沿用 [存储与备份](STORAGE.md) 的正式流程。计划绑定执行时的数据根和原件路径；迁移后须建立并验证新计划，保留原计划作为审计证据，不覆盖旧计划冒充原地续跑。缓存复用始终核验账号、模型和实际输入，不能跨用户或跨模型直接加载。
