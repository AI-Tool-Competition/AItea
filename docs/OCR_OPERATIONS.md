# 固定范围的识别与恢复

`scripts/process_frozen_parse.py` 只处理冻结清单中仍位于 `parse` 阶段的文档。
完成正文后停在 `embed` 队列；该入口禁止下载和调用 embedding。
共享库允许另一个工作进程同时处理不同 ID 的向量任务。

输入快照包含 `initial-report.json`、`initial-parse-ids.json` 和
`initial-parse-documents.json`。报告绑定两个清单的 SHA256、账号和数据库目录；
文档守卫绑定不可变版本、原件 hash、来源、metadata 和已成功页。
快照由操作者在首次领取前用只读事务保存，续跑使用同一快照，不能重新扫描扩大授权范围。

```bash
uv run --project backend python scripts/process_frozen_parse.py \
  --data-dir /path/to/library \
  --snapshot /path/to/frozen-parse-snapshot \
  --models-env /path/to/private-models.env \
  --run-id continuation-001 \
  --ocr-workers 4 --ocr-start-workers 1 \
  --document-workers 4 --claim-limit 60 --seconds 600
```

每次使用新的 `run-id`。`--document-ids` 可进一步限定最多 20 个冻结 ID。
同一快照有进程锁；报告保存领取范围、实际 HTTP 状态、并发峰值、页完成数和未通过质量门禁的页数。
`metric_schema_version: 2` 的报告以本次运行每份文档首次领取前的状态为增量基线，
保存在原子写入的 `run-baseline.json`，同时绑定版本、原件与 manifest SHA256。
同一次运行重复领取不会重置基线；正文和识别页的增量不会重复计算以前运行的成果。
`new_request_pages_not_accepted` 只统计本次调用模型所涉及、当前仍失败或待核对的物理页，
同页多次局部请求只算一页；`request_started` 事件记录调用范围，旧失败无新请求时不计入该字段。
缺少该版本字段的旧报告使用初始冻结快照作为页统计基线，不能直接当作本轮增量相加。
轮转计数持久化，长文不会因短文优先被永久排除。运行中收到 SIGTERM/SIGINT 后停止新领取，
等待已有请求和受限解析结束，继续维护租约并保存检查点。
领取和心跳使用共用的本地事务重试：只重试已回滚的 SQLite 忙锁，最多 3 次，
各次沿用数据库 15 秒等待上限并记录等待。超过上限停止新领取并排空在途工作；
模型调用不在数据库重试范围内。
硬中断后，入口只在原冻结 ID 内发现已过期的 `parse` 租约，再由既有任务服务回收；
有效租约、其他账号、向量任务和失败任务不因此重置。未保存结果的付费请求仍停止自动重发。

429/5xx 的 `Retry-After` 使用秒数或 HTTP 日期；页/区域检查点及运行目录同时保留等待时间。
并发可以降到 1；续跑用 `--ocr-start-workers` 延续观察到的容量。
文档领取数量随实际识别容量收缩，避免等待容量的任务反复空转。

已成功页不会重新请求。完整页和局部裁剪在外部调用前保存原件、物理页、像素 hash 和 request ID；
错误响应使用账号 CAS 保存有界原始字节，重试追加旧检查点，保留旧失败和裁剪历史。
连接建立失败可以有界重试。读/写/协议异常以及中断后只剩请求意图的状态可能已经计费，
会停止自动重发并标记结果未知。它们需要核验后另行显式恢复。
正式批量入口对 408/5xx 也采用结果未知的保守边界，保留响应及等待时间并停止该页自动重发；
429 仍按明确拒绝和 `Retry-After` 有界重试。通用识别服务的默认重试行为保持兼容。
旧 408/5xx 待重试页追加暂停检查点，原响应 CAS、请求号、尝试数与完整旧页保留，其他页继续处理。

`app.ocr_response_reparse.apply_saved_response_reparse` 可离线重解析已完整保存的错误响应。
仅接受当前提示版本、完整未截断正文以及仍匹配账号、原件、版本、manifest、请求和像素的证据。
合法格式包装只允许外层恰好为 `type: "json_object"` 和对象类型 `content`；
内层仍执行严格字段、类型、数字、表格和完整性检查。
函数默认 dry run，apply 保留原 request ID、attempts、原始错误 CAS 和完整旧失败页，
不创建新的模型请求；旧 manifest 重放会冲突。未通过质量门禁的内容继续待审。

这套恢复只改变派生文本与处理队列，不审核法律效力，不改原件、来源版本或法律关系。
HTML 中尚未绑定的外部图片仍需要独立采集与映射，不能用页面截图代替缺失原件。
