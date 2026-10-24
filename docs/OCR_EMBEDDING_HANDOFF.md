# 固定 OCR 范围的向量交接

`scripts/ocr_embedding_handoff.py` 只在已有固定 OCR 范围排空后，准备一份新的不可变向量计划，并可启动既有 `complete_embeddings.py` 预检与监督器。它不创建队列、不修正文档状态、不批准法规效力，也不重新识别页面。现有 OCR、旧向量计划和原输出账本保持原位。

## 准备与执行

不带 `--apply` 时，脚本以 `mode=ro`、`query_only` 读取业务库，不构造 `Service`，不发 HTTP；只在私有 handoff 根写入计划、证明和 reservation。SQLite 对在线 WAL 的正常只读连接可能使用共享内存侧车；这里的只读保证是业务表、原件、正文和向量零写入。

带 `--apply` 会先运行既有无 `--apply` 向量入口。这个入口会构造 `Service`、做正常幂等初始化，并创建 baseline、metadata guard、成功缓存和 preflight 记录，**不是业务库只读命令**。该预检通过后才运行既有向量监督器。不要把准备授权理解为执行授权。

运维者先从经过检查的提交冻结完整 `backend/app`、`scripts`、`config` 源码，再用锁定 uv 环境运行下列入口。证明绑定整个源码树摘要；运行中不修改这个源码树。

```sh
uv run --project /absolute/project/backend python \
  /absolute/frozen-runtime/scripts/ocr_embedding_handoff.py \
  --ocr-snapshot /absolute/ocr/snapshot \
  --handoff-root /absolute/private/ocr-embedding-handoffs \
  --exclusions /absolute/private/exclusions.json \
  --models-env /absolute/project/.env \
  --admission-path /absolute/project/.dev/embedding-admission.sqlite3
```

以上命令准备并持久预留合格输入；复查 `latest-prepare.json`、批次 `selection-proof.json` 后，在同一命令末尾加 `--apply` 执行。不提供 `--output` 或 retry-unresolved 选项。重复启动必须使用原 handoff 根、排除证据、模型文件和固定源码；新目录不能替代遗失账本。

入口固定为共享 HTTP 上限 64、文档阈值 12、每文档请求并发 2、batch 2、timeout 60 秒、监督窗口 300 秒。64 准入库必须已经存在，且 provider/model scope 和容量吻合；脚本不创建新的独立配额。12 是同业务库全局活跃 embed 租约阈值，和旧 runner 共用，不是额外独占 12。既有监督器可降低文档阈值；不保证各 runner 的固定份额。查询预留和真实 HTTP 限流继续由已有准入器执行。

## 排除证据契约

`exclusions.json` 是运维核验的固定授权边界，路径均为绝对路径，SHA256 指文件原始字节。首次绑定后不可修改；跨 scope 自动发现和重新授权不在首版范围内。

```json
{
  "protocol": "ocr-embedding-exclusions-v1",
  "handoff_root": "/absolute/private/ocr-embedding-handoffs",
  "data_dir": "/absolute/project/restores/current-library",
  "owner": "aitea-system-legal-library",
  "fingerprint": "已验证的模型 fingerprint",
  "admission_path": "/absolute/project/.dev/embedding-admission.sqlite3",
  "admission_scope": "已验证的 provider/model scope",
  "embedding_plans": [
    {
      "kind": "legacy_fixed",
      "expected_documents": 621,
      "plan": {"path": "/absolute/old/selection.json", "sha256": "原字节 SHA256"},
      "inputs": {"path": "/absolute/old/all-inputs.json", "sha256": "原字节 SHA256"}
    },
    {
      "kind": "prior_handoff",
      "expected_documents": 15,
      "plan": {"path": "/absolute/prior/selection.json", "sha256": "原字节 SHA256"},
      "inputs": {"path": "/absolute/prior/all-inputs.json", "sha256": "原字节 SHA256"}
    }
  ],
  "ocr_holds": {"path": "/absolute/ocr/unknown-outcome-holds.json", "sha256": "原字节 SHA256"},
  "ocr_history": {"path": "/absolute/ocr/historical-paid-result-gap-audit.json", "sha256": "原字节 SHA256"},
  "expected_held_documents": 52
}
```

必须提供一个 `legacy_fixed` 和至少一个 `prior_handoff`。每份 plan 沿用 `fixed-embedding-continuation-v1`，文档附实际 `chunk_count`。输入清单协议为 `fixed-embedding-input-inventory-v1`，包含 `plan_sha256`、`plan_digest`、`documents`（ID 到完整 chunk 列表）及 `unique_inputs_sha256`。每个 chunk 至少有连续从 0 开始的 `ordinal` 和实际 `input_hash`；生成的交接清单还保留 `chunk_id`、retrieval text SHA、locator SHA。`plan_digest` 和输入集合摘要均用现有 `complete_embeddings.digest` 计算，输入集合先去重再排序。

旧计划输入清单必须是全部固定输入，不是当前缺失行、成功缓存或未知集合的子集。运维生成清单时须核对原计划文档、manifest/retrieval chunks 及 `app.contexts.knowledge.application.retrieval.input_hash`；脚本检查完整文档集合、逐文档行数/顺序、绑定摘要，缺证据就阻挡。现有未版本化的摘要报告不能直接冒充这份完整清单。

OCR holds/history 复用已有报告结构：合并 current_records、historical_records、historical_paid_missing_records、historical_paid_missing_document_ids、additional_document_holds，并核对预期唯一 ID 数。正文当前看似完整不能清除历史未知或缺失付费结果。所有旧计划文档、全部旧输入和 holds 都排除；单个输入冲突时整份候选 deferred。

## 完成门禁与证据

首版只接受整个固定 OCR scope 的 `completed`、`blocked_quality` 或 `blocked_inputs`；必须有可信终态窗口，且无活动 parse claim。部分 scope、blocked_unknown、缺失窗口均拒绝。根据 generation 核对所有窗口与原 launching 事件，逐窗检查 run-input/report/events 的范围、排空和请求/响应/checkpoint 计数。每份原证据的路径与 SHA 都进入证明，不能只保留最后一窗。

候选须同时满足系统 owner、role primary、明确的国家 primary 分类、`can_index`、draft、`text_complete`、完整且不脏的检索输入、已有 queued/embed 任务且无租约/冷却。只检查原 OCR ID，不扫描新文档；原件、metadata、原成功页和累积成功页必须保持。质量待审、二级资料、缺任务、失败任务和模型不匹配的现存向量都 deferred，不用交接器修复。

批次 ID 由 OCR binding、模型和选中文档的原件/正文/资格证据确定，不依赖启动时间。先写 reservation 再发布文件；发布中断导致证据不完整时保留原文件并阻挡，需人工核对，不自动删除重建。每批保留 `selection.json`、`selection-proof.json`、`input-inventory.json` 和唯一 `output/`。所有已预留输入，包括已完成批次，首版都继续排除；跨批成功输入转移明确 deferred，不复制另一个 plan 的成功缓存。

全局 handoff 锁在 spawn 前取得，通过经过路径/inode 核验且已持有的文件描述符传给 child，再传到真正的向量 runner。父进程或中间 supervisor 被 SIGKILL 后，存活的付费 runner 仍持锁；并发启动不会进入第二条新通道。没有从环境变量读取任意 FD，也不靠 PID 文件覆盖锁语义。

纯预检中断且尚无 paid launch intent 时，可在原 output 继续正常预检。付费启动意图持久化后，丢失 baseline、guard、cache 或请求日志均阻挡；缓存和旧未知请求只在原目录恢复。成功 HTTP 已保存、向量未提交时，既有缓存优先复用；不伪造请求，不新增旧未知请求授权。现有 supervisor 对无终态子进程的 60 秒冷却保持不变。

只有固定计划、最终报告、监督状态、缓存 scope、请求账本和实际数据库同时闭合，missing_rows=0、无活动 claim、无未知输入且三项完整性守卫为 true，才标记 completed。exit 0 或 OCR 文本完整不单独代表向量完成。

## 状态与停止

查看 handoff 根的 `handoff-state.json`，再看其唯一批次 output 的 `supervisor-state.json`、`progress.json`、`report.json` 和请求日志。先核对 PID 与记录的进程启动身份，再对本次外层进程发送 SIGTERM；它转发到自己创建的 child，并等待既有监督器排空。若父进程已丢失，用 handoff 状态中的 wrapper PID/start_identity 定位，勿按进程名批量终止。当前没有另加 STOP 文件语义；停止使用受归属校验的进程信号。

所有执行产物、成功缓存与原日志必须纳入相应数据备份证据；准入数据库仅协调活动进程，不能复制旧 PID/租约作为恢复数据。本功能未自动安装任务或启动生产模型。
