# 原生与转换资料的映射盘点

`app.archive_inventory` 提供离线、只读的资料盘点。默认格式仍为版本 4；显式使用 `--include-source-mapping` 后采用版本 5，在已有账号 SHA-256 / 国别分区内增加 `source-mapping.jsonl`。此选项不运行解析器、图片模型、向量模型或索引，不改变资料、审核状态和结构版本。

```bash
uv run --project backend --locked python -m app.archive_inventory \
  --data-dir /absolute/path/to/data --owner ACCOUNT_ID --country ID \
  --include-text --include-source-mapping
```

`--include-text` 导出已有正文；`--include-source-mapping` 导出来源定位和必要的单元格原值。两者可分别使用。账号参数用于本地操作者限定范围，不是新增网络接口或鉴权令牌。账号和国别沿用原有查询过滤，各分区目录权限为 `0700`，文件权限为 `0600`。SQLite 记录共享一个只读事务；原件仅检查无符号链接的归档内路径、存在性和大小，不打开原件或重新计算其 SHA-256。

`scripts/collect_resources.py` 同样接受 `--include-source-mapping`，在本次采集结束后的盘点阶段显式输出版本 5 映射。它是导出选项，默认关闭，不改变采集任务冻结的范围、资料层级或模型设置；已有运行进程不会因该选项的新增而自动改变。

每个映射记录包含资料标识、不可变资料版本、数据库原件 SHA-256、国别、保存的法律层级、精确 manifest UTF-8 SHA-256 和 JSON 指针。普通关联附件（`role=secondary`）仅导出原件状态，不读取或输出其 manifest。作为独立法规资料保存的二级法律资料（`role=primary`、法律层级为 `secondary`）可以导出已有文本和映射，既有二级资料检索门禁继续由服务判断。

## 定位含义

| 记录 | 保留的定位 | 解释限制 |
| --- | --- | --- |
| DOCX | 原始包部件名称、部件 SHA-256、XML 路径、关系标识、段落/表格/单元格/合并范围、正文块的所有来源锚点 | XML 路径指向部件，不是物理页号；外部关系不会下载 |
| XLS | 工作表、行列/单元格、原始值、浮点十六进制、格式及日期模式、BIFF 流偏移/记录 SHA-256、公式缓存 SHA-256、合并范围、打印字段字符偏移 | 流偏移不是复合文件物理偏移；缓存不会重算，动态页号不会当作实际页号 |
| MDB | 表/列、原始行页号/行槽/文件字节偏移/行 SHA-256、字段位置、NULL 位、Memo 链和载荷 SHA-256 | 数据库存储页不是法规页；不运行查询、报表或宏 |
| DOC | 原始复合文件流标识/目录、子文档 CP 范围、WordDocument 逻辑流偏移及字节 SHA-256；转换 artifact 的 SHA-256、XPath 和声明分页 | 原始 CP 与转换 XPath 分别保留；未核验布局、字段可见性和对象时不能推定一一对应或全文完整 |
| WPF | 转换 HTML / 事件 artifact SHA-256、XPath、事件字节偏移、转换表格位置和有限合并属性、图片 SHA-256 | 转换位置不是原始 WPF 记录位置；保留原始对象映射未核验状态 |
| 图片 | 原件/部件或转换位置、图片 SHA-256/大小/尺寸、待识别状态、来源关系 | 导出不会识别图片；转换图片的原始对象对应关系与图片字节核验分开表示 |

关系目标保留标识、类型、状态和目标 SHA-256。仅无凭据、无查询参数、无片段、符合既有 HTTPS 和公共 IP 字面量检查的目标 URL 可输出，并标记为“仅语法检查，未查询 DNS”。其余目标只输出哈希和 `redacted`；完整目标仍保存在授权归档 manifest 内。

## 有界输出与内嵌字节

一次最多读取一个 40 MiB manifest，逐条输出映射；单条输出上限为 4 MiB。超过限制、重复 JSON 键、非有限数值、来源 SHA-256 不一致、非法偏移或内嵌字节校验失败会产生 `mapping_gap`，不会把错误记录当作成功映射。后续资料继续盘点。字段使用各格式的固定白名单；工具私有路径、请求凭据、任意未知字段和向量数组不进入映射输出。

DOC/WPF 原始对象、转换 artifact 和图片的内嵌 Base64 按块检查编码、实际大小和 SHA-256，不在 JSONL 中再次复制字节。输出的引用含 `storage=documents.manifest`、精确 manifest SHA-256、JSON 指针、内容 SHA-256 和大小。它是对本次数据库快照内容的引用，**不是磁盘 artifact 路径**。授权检查者读取对应资料 manifest 时必须先匹配其 SHA-256；资料后续处理改变 manifest 时，旧引用不能指向新的内容。

`document_end.projection_complete` 只表示白名单映射遍历成功；`text_complete` 复用应用的正文质量门禁。两者不表示法规有效、审核通过、向量齐全或清单采集完成。`mapping_record_indexed=false` 表示这批盘点记录本身不会被导出程序索引，不能用于判断原资料当前的 RAG 资格。

## 验证

专项测试覆盖账号/国别、不可变版本、独立二级法律资料与关联附件、归档路径、原生/转换定位、图片及 artifact 引用、Memo/NULL、前导零/浮点/公式缓存、打印偏移和损坏/超限输入。真实来源回放报告保存于任务的私有 `data/verification/native-source-mapping-20261013/report.json`；其范围是现有 manifest 的映射投影，不代表外部网站全量覆盖或法规全文审核。

BKPM 投资目录另有来源限定的 64 KiB 元数据出口：仅接受冻结的印尼 `4.1` 投资采集配方、同账号/来源/绑定/版本、官方 `/api/document-search` 的 `id_theme=1&limit=50&page=N` GET 请求和归档原件 SHA-256/账号路径绑定。原件仍只做无符号链接路径、存在性和大小检查，SHA-256 使用数据库已有值。逐条目录映射保留真实 `bkpm:id`、标题、官方 PDF URL、有限来源元数据及父目录页标识/SHA-256，并标记 `mapping_record_indexed=false`、效力 `not_reviewed`。此例外不改变通用目录的 16 KiB 限制，也不将目录记录当作正文或有效法规。

2026-10-13 的既有 BKPM 页记录报告 `returned=50`，实际已保存 `documents=30`；这次仅恢复这 30 个映射，并明确输出另 20 行缺少已保存文书元数据的差异和已有警告。不能由此推定该页全部法规原件或整个 BKPM 目录已覆盖。专项证明保存于私有 `data/verification/bkpm-inventory-mapping-20261013/report.json`，旧失败盘点保留。
