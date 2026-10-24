# 来源响应复用与封装原件

采集器保存原始响应、请求时间、原件字节和不可变哈希。复用与格式恢复均不授予法律效力，也不代表网页当前内容与历史响应相同。

## Setneg 已下载凭证复用

`scripts/collect_indonesia_official.py` 支持重复指定 `--reuse-receipts-from /absolute/old-campaign`，最多八个显式目录。先读取目标目录已经发现的任务，按清单条目、完整 URL 和 detail/document 类型匹配历史成功记录；未发现的文书不会因缓存存在而加入任务。

每份记录都重新核验字节大小、SHA-256、CAS 路径及当前冻结方案的来源规则。历史方案可以不同，但当前规则不接受的旧响应不能复用。详情先复用并重新发现原件链接，再匹配原件；目标已成功任务保持不变。原件和历史响应记录原始 JSON 都复制到目标 CAS，原始请求时间保留，额外标识 `historical_response_not_refetched`。

复用不会发出网络请求、增加实际请求计数或解除来源冷却。续跑同时核验原件与历史响应凭证的哈希。准备资料库时，目录、详情、详情下载凭证与原件下载凭证一并进入 `campaign.json` 的 `source_evidence`，供正式导入和备份迁移。

## BKPM 被错误标注为 PDF 的表单响应

BKPM 的个别公开文件实际返回完整的 multipart/form-data 上传实体，HTTP 类型却是 application/pdf。来源验证器和正式采集器只对已通过当前方案 URL 校验的 BKPM 官方原件启用适配，保留完整 HTTP 实体，再提取唯一 PDF 部件。采集器将提取后的 PDF 作为法规原件送入解析和索引；原始响应的 CAS 路径、哈希、大小、类型及提取证明随文档的 `source_response` 一并保留。由于缓存正文已转换为 PDF，采集器不复用该响应的 ETag/Last-Modified，后续轮询会重新验证原始响应。

适配按 [RFC 2046 的 multipart 边界规则](https://www.rfc-editor.org/rfc/rfc2046#section-5.1) 验证起止边界、部件头及内容边界；当前仅支持实际观察到的 WebKit 边界、一个 `file` 和可选 `customPath` 字段。响应最多 4 MiB、每个头最多 8 KiB，拒绝截断、重复文件、未知字段、编码变换和不明尾部。普通前缀后出现 `%PDF` 不构成恢复依据。

报告分别保存原 HTTP 实体和 PDF 的 CAS 与哈希，并按部件记录字节偏移、长度和哈希。PDF 仍须经过受限解析、正文完整性检查、清单适用范围及法律审核；成功拆包只说明取得了可验证的文件表示。原件摘要也保留 `source_response`，下游导入必须携带该证明，不能只复制提取后的 PDF。
