# 超大 PDF 原生页证据分片

`scripts/extract_archive_page_shards.py` 独立保存超大 PDF 的原生页证据。原件仍是一个不可变文档版本；每片最多包含 64 个绝对物理页，继续使用现有的受限解析进程、原件大小/SHA-256 校验、页内容流限制和可见性检查。每个窗口保留 4,000,000 字符及 40 MiB 进程传输限制；分片落盘另外限制为 20 MiB。分片超限时停止该窗口，操作员可降低 `--window-pages` 后续跑。

该入口没有 OCR、模型或向量调用。它不生成 `elements`、`chunks`，根清单始终保持 `text_complete=false`、`derived_status=not_generated`、`index_status=not_generated`。`native_mapping_complete=true` 仅表示所有物理页均有原生提取检查点；待识别、待核对状态原样保留。不得将每片局部组装后交给现有完整文档索引：条文上下文可能跨页延续，而旧组装器的分块序号会在各片重号。

## 执行与续跑

从项目根目录执行，下例参数应替换为实际原件和已授权存储位置：

```bash
uv run --project backend python scripts/extract_archive_page_shards.py \
  --source-db /absolute/project/data/tea.sqlite3 \
  --document-id IMMUTABLE_DOCUMENT_ID --total-pages 5964 \
  --checkpoint /absolute/recovery/manifest.json \
  --output /absolute/storage/recovery/law-native-pages \
  --storage-root /absolute/storage --workers 4
```

首次可加 `--max-windows 1` 验证一次提交，再运行同一命令恢复余下窗口。既有已提交页不会再次提取；并发支持 1 至 8，调度不重叠的缺失窗口。每次启动先在受限工作进程中核对实际 PDF 页数，不提取正文；即使没有新窗口，也不能靠截短的旧检查点声明物理页齐全。该脚本在只读连接中获取文档 ID、版本、账号、原件 SHA 和大小，既不修改来源数据库，也不改变处理任务或法律审核状态。项目 `.dev/archive-page-shards/` 自动保留位置指针。

## 持久化与证据

- `root.json` 绑定来源账号、文档 ID、不可变版本、原件 SHA、大小和物理页总数，记录有序分片、逐页哈希、缺失页和状态计数。
- `files/blobs/` 使用现有账号隔离的内容寻址存储，保存完整原件、旧检查点原始字节、原样页对象和页内引用的已有证据。旧检查点的 OCR 历史、图片与错误响应引用不被正文替代或丢弃。
- 旧页中的路径字符串保持不变；`legacy_dependencies` 按物理页、JSON pointer、原件 SHA 和实际内容哈希映射到已复制的相对 CAS。跨目录迁移后必须通过此表解析旧路径，不能直接打开旧绝对路径。仅接受来源账号 CAS 内的路径；未知账号或损坏对象拒绝导入。
- `last-run.json` 记录实际新增页、窗口错误和由子进程起止时间测得的并发峰值。该运行报告不会将原生提取完成描述为法规正文或向量已完成。

协调器持有输出目录的独占锁，分片先持久化并同步目录，再核验旧根哈希并原子替换根清单。中断可能留下未引用 CAS，续跑只认可已提交引用。恢复开始核验全部已提交分片、逐页映射、原件、旧检查点和依赖；文件缺失、哈希错误、重叠页、身份/版本改变或旧页丢失都会停止，不能自动以重新提取覆盖证据。

该目录可以整体搬迁，原件和旧检查点在其中有独立副本；文件系统备份时仍应在协调器停止后复制，或使用有一致性保证的快照。它尚未接入应用的 `data_backup`、跨账号导入、资料详情或 RAG 主链，不能把本格式伪装成现有应用 manifest。后续接线必须补齐流式条文派生、稳定分块标识、完整性门禁和有界预览。
