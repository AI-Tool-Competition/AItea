# 扫描页范围误判的有界恢复

`legal-page-transcription-v3` 将完整性范围明确为当前物理页可见的全部内容。正常跨页的句子、条款、表格或断词不构成缺页；必须保留末尾实际可见的文字，不能补写下一页。真实裁切、难以识别或尚未转录的可见内容仍返回不完整。提示版本、提示摘要和请求 recipe 同时进入复用指纹，因此旧请求不能冒充新策略的结果。

旧 `complete=false` 不会自动转成成功。`app.contexts.documents.infrastructure.recovery.ocr_page_retry.prepare_physical_page_retry` 仅对显式选择的 1 至 20 个物理页准备一次新策略：旧提示必须是 v2、存在 `model_reported_incomplete`、原件摘要和整份物理页映射一致。已有重试历史、已成功页、其他错误和未知来源均拒绝。完整旧页保存在单层 `ocr_retry_history`，保留旧恢复冲突，不允许嵌套历史反复付费重试。单页旧记录限 2 MiB，准备前后整份 manifest 限 40 MiB。

先通过 `scripts/import_collected_resources.py --document-id …` 把目标资料及其关系依赖导入隔离目录。恢复入口拒绝工作树及上级项目默认的 `data/`，不更新正式总库、法律审核状态或来源版本。数据放在项目目录；只有存在明确的额外磁盘授权时，才使用已登记的临时溢出目录，并由最终导入流程把成果归回项目。

从隔离库读取当前文档 `version`、`content_hash` 和 `manifest` 的原始字符串。`--expected-manifest-sha256` 是该原始字符串 UTF-8 字节的 SHA-256，不能先重新格式化 JSON。准备操作始终只读，不初始化数据库，不调用模型：

```bash
uv run --project backend --locked python scripts/recover_page_scope.py prepare \
  --data-dir .dev/recovery/isolated --owner <归属ID> --document-id <文档ID> \
  --source-hash <PDF摘要> --version 1 --expected-manifest-sha256 <当前manifest摘要> \
  --page 22
```

输出含有逐页的新请求检查点。获准的调度器需在每次调用前持久保存请求意图和发送的 PNG，使用现有受限渲染、视觉调用和数值/结构验证，不再请求已成功页。跨进程的 OCR 并发与限流状态不会自动共享；大批恢复应由一个协调器分批调度，避免各进程分别达到并发上限。该 CLI 的 `prepare` 不启动后台任务，也不开放新的自动重试循环。

已保存的成功响应可先预检，再追加 `--apply` 回写隔离副本：

```bash
uv run --project backend --locked python scripts/recover_page_scope.py apply-checkpoint \
  --data-dir .dev/recovery/isolated --owner <归属ID> --document-id <文档ID> \
  --source-hash <PDF摘要> --version 1 --expected-manifest-sha256 <当前manifest摘要> \
  --checkpoint .dev/recovery/request/page-after.json \
  --checkpoint-dir .dev/recovery/request --checkpoint-owner <识别图片归属ID>
```

回写前会核验归属、草稿状态、版本和原 manifest 摘要，拒绝活动处理租约；检查旧页快照与当前保存页一致、响应为新策略、无未确认区段、数值及结构验证通过，再从该 PDF 原件重新渲染，要求 PNG 摘要与请求存档完全相同。仅账号 CAS 路径可在迁移后变化，旧页证据摘要将重新绑定并保留来源摘要。其他物理页检查点不得变化。派生正文和分块重新构建，导入记录、目录和详情页证据仍保留。

预检没有数据库写入和模型调用。`--apply` 也不调用 OCR，只写入验证过的页面、更新索引并排入后续处理；全文完整且配置了真实 embedding 时，从 `embed` 阶段继续。需要加载已有后端模型配置时可加 `--models-env <私有配置路径>`，密钥不会写入检查点。后续处理必须限制到本次归属及文档，仍由索引规则检查完整性与一级用途。每次成功回写后 manifest 摘要会变化，旧命令重放被拒绝。失败响应不能覆盖旧页。

2026-10-19 的真实对照仅覆盖两页：印尼 BI PADG 17/2025 第 22 页、PP 28/2025 工业附录第 6 页。两页新旧请求图像摘要相同，数字序列不变；归一化排版后的字母和数字一致。新提示恢复了这两页的物理页完整性判定。该结果支持进一步有界验证，不能代替其他待核页识别、全量覆盖验收或法律审核。资料进入正式 RAG 仍须通过现有审核、国家、固定版本和正文完整性门禁。

## 单进程批量执行与验收

`scripts/execute_page_scope_recovery.py` 把明确列出的物理页分成每批最多 20 页，同份 PDF 串行写回，最多 4 份资料并行。全进程共用最多 64 个自适应 OCR 槽；当前配置、可用槽和失败情况出现在结果报告中。只等待本地容量时可有界重新领取；已经保存外部请求意图后不自动再发请求。每批成功结果只重建一次正文并在一个事务中写回。

计划文件格式如下。每份文档必须绑定隔离库当前的原 manifest 摘要；最多 32 份资料、2,000 个明确页码，不能借计划自动发现或扩大来源。

```json
{
  "protocol": "physical-page-recovery-execution-v1",
  "owner_id": "<归属ID>",
  "documents": [{
    "document_id": "<隔离库文档ID>",
    "source_hash": "<PDF摘要>",
    "version": 1,
    "expected_manifest_sha256": "<当前manifest摘要>",
    "physical_pages": [23, 26, 35, 36, 48]
  }]
}
```

```bash
# 默认仅预检，不创建输出目录或可写服务。
uv run --project backend --locked python scripts/execute_page_scope_recovery.py \
  --data-dir .dev/recovery/isolated --plan .dev/recovery/plan.json

# 只有已授权真实模型调用时，才同时提供以下执行选项。
uv run --project backend --locked python scripts/execute_page_scope_recovery.py \
  --data-dir .dev/recovery/isolated --plan .dev/recovery/plan.json \
  --output .dev/recovery/results --concurrency 64 \
  --models-env .env --live-models --apply
```

执行器在隔离数据目录的 `.page-scope-v3/` 持久保存请求意图、原页、新响应、PNG、逐字差异和进度。同一数据目录有排他执行锁；切换报告输出目录不能绕过已有请求记录。已经保存成功响应的页面复用响应；只有意图没有响应的页面记为中断待查，不自动重发。失败和待复核页保存全部结果，保留原页。迁移、备份或接续未完成任务时须一起保存该目录，否则丢失的失败请求记录无法约束重复付费。成功写回的页面另在数据库保留完整旧页和重试来源证明。

HTTP 成功但返回无效、空或截断内容时，普通采集与范围恢复复用同一错误证据存储。仅保留最多 256 KiB 原始响应前缀，记录是否截断、响应摘要及请求摘要；先核对已保存的原件、物理页、请求标识、尝试次数、像素、模型和提示版本，再存入所属账号 CAS。范围恢复另独立保存 `error-response.json` 引用，即使后续 `page-after.json` 写入中断，也可查到失败响应，且已有意图仍禁止自动重发。错误字节不进入正文、检索或普通日志；非成功 HTTP 响应及含凭据回显的内容不保存为该证据。

除现有原件、像素和数值校验外，范围恢复还要求字母数字及其顺序完全一致、逐表逐行逐单元格一致，并保留语义标点和词间边界。仅允许换行、`<br>`、成对强调标记、弯引号样式和保留原字符的连字符／斜线周围空白归一化；不能删除连字符、移动空单元格、合并表格行或用高相似度放行改词。非矩形表格也进入待审。执行器与单页检查点回写共用 `physical-page-scope-content-and-cells-v2` 门禁，直接回写不能绕过该规则。该比较证明新策略没有改变已有映射，不代表法律审核或原页每个字符已经人工核准。

字符相似度仅作诊断，不参与放行。相同文本直接记为 `1.0`；不同文本超过 1,000,000 字符对计算预算时，该值为 `null`，并标记 `normalized_letters_digits_similarity_basis=omitted_work_limit`。长页仍执行精确内容与表格门禁，不能为了一个诊断百分比耗尽 OCR 工作线程。

早期批次使用的 97% 字符相似度不足以核验表格结构，其报告保留为历史执行记录。已写入的变化页可用 `quarantine` 入口降为待审，每批最多 20 页，同时绑定原件、版本、原 manifest 和各页完整检查点摘要：

```bash
uv run --project backend --locked python scripts/recover_page_scope.py quarantine \
  --data-dir .dev/recovery/isolated --owner <归属ID> --document-id <文档ID> \
  --source-hash <PDF摘要> --version 1 --expected-manifest-sha256 <当前manifest摘要> \
  --expected-page-sha256 <物理页码>:<完整页检查点摘要>
```

默认只预检，追加 `--apply` 才写入隔离库。完整页检查点摘要按与重试历史相同的排序 JSON 规则计算。降级保留原图、新旧正文、旧检查点与模型原始完整性标记，只增加服务端待审理由和修改页状态；全文和索引完整性变为假，处理任务暂停在失败状态，模型调用为零。原 manifest 或任一页已变更、存在处理租约或资料不属于草稿时拒绝操作。迁回总库前须用最新门禁核查所有历史已写入页，不能只采信旧批次的成功计数。

报告中的 `new_request_intents` 是本次持久保存的请求意图数量，不等于供应商确认计费次数；`applied_pages` 是该次运行按当时规则写回的页数，需结合比较策略版本和后续待审记录解读。正文未全部完整时仍保持不完整状态。

全部选中页写回成功时报告 `selected_pages_complete`；存在未调用、失败或待复核页时报告 `needs_review`，命令退出码为 3。后者仍保留成功检查点和全部请求记录。入口校验拒绝的退出码为 2。选中页完成不代表未选中页、整份法规或整套采集清单完成。

一次执行后原 manifest 摘要会变化。继续剩余页时需重新读取当前摘要并生成显式新计划；旧计划被拒绝，已有页面响应不会因此再次请求。若数据库已提交但进度文件尚未写完即中断，先检查已保存历史与当前正文，不凭旧报告重复执行。该执行器只排入后续处理，不启动 embedding 后台进程；完整正文的向量由限定归属和文档的统一队列继续生成。
