# 文档解析、检索与资料采集

## 接入边界

`backend/app/ingestion.py` 是无数据库、无用户状态的同步库。应用服务必须先检查账户和业务归属，再将耗时工作交给受限工作线程或进程。外部上传应通过 `app.parsing.parse_bounded` 进行资源隔离，不应在 API 事件循环或主服务进程内直接调用 `parse_file`。原始文件保存、不可变版本、内容 hash 去重、来源更新时间和引用访问控制由业务服务负责。

- `parse_file(filename, content, mime_type='', role='primary', parser='basic')` 返回结构元素、章节切片、原文 locator、解析版本、SHA-256 和警告。明确拒绝 `secondary`，避免把关联附件意外纳入索引。
- `lexical_search(query, chunks, limit=5)` 使用 BM25 风格词项评分；中文按连续双字匹配，保留传入 chunk 的文档、版本、locator 等元数据。没有重合词项就返回空结果。它不是语义向量检索，也不替代法律适用性判断。
- `embed_texts(texts)` 调用实际配置的 OpenAI 兼容 embedding 服务。没有配置时返回 `None`；部分配置、服务错误、空向量、非有限数值、重复索引或维度不一致会明确失败。不会生成假向量。服务层负责持久化模型、维度、向量与配置指纹，查询时按同一配置向量化，配置变化需要重建索引。
- `fetch_source(url, max_attachments=5)` 返回主文件原始 bytes、同源直接附件 bytes、实际最终 URL、采集时间和附件失败警告。它不解析附件，也不递归抓取。

## 基础解析

| 格式 | 提取与定位 | 已知边界 |
|---|---|---|
| TXT / Markdown | 复用原解析器；原始行号、字符位置、标题路径 | UTF-8；正文不能为空 |
| HTML | 标准库解析；标题路径、原始 HTML 行号、元素序号、存在时保留 id | 不执行脚本，不加载外部资源，不伪造提取文本在 HTML 中的字符位置 |
| DOCX | ZIP/XML 提取段落；标题样式、原始段落序号 | 基础模式不恢复复杂表格和图片；拒绝 DTD/实体声明 |
| PDF | pypdf 提取文字；保留原始页码 | 不进行 OCR；全扫描文档明确要求 OCR，部分空页记录警告 |

单文件 20 MB，提取文本最多 400 万字符，结构元素最多 20,000，PDF 最多 300 页。DOCX 解压总量最多 50 MB、正文 XML 最多 16 MB、ZIP 条目最多 2,000。PDF 解压后的页面内容流限制 16 MB；由于解压发生在检查之前，生产入口必须使用进程资源隔离。纯库的大小检查本身不是恶意文件沙箱。

切片直接复用 GEO 的章节与 token 预算逻辑，保持正文、检索上下文与原文定位分离。引用必须同时携带文档 ID、不可变文档版本及 locator；标题或路径本身不能作为引用唯一标识。

## 可选 Docling

显式选择 `parser='docling'`，安装可选依赖，并配置本地 `DOCLING_MODELS_PATH`。基础安装不会隐式拉取模型或载入 Docling。原解析器关闭远程服务与外部插件。尚未进行视觉核验的图片资产不纳入索引，返回警告；原件保持完整。

原代码来自用户提供的 `tmp/GEO-System/services/agent-service/app/`，完整抽取至 `backend/app/vendor/geo_parser/`。本次只对抽取副本作相对导入和 HTML locator 合并兼容修改；图片过滤发生在 facade。没有修改参考项目。

原始源码 SHA-256：

- `document_ingestion.py`: `a00321d72a42f65941b61309d4700b015fa8ffbdfeff1651df2eb907bb52e69c`
- `bounded_concurrency.py`: `c31e45c1bc317b95a193f7b599ad82f856cc2db24dc59327d0c8f2eeae0a6331`

## Embedding 配置

同时设置 `EMBEDDING_BASE_URL`（例如服务的 `/v1` 根地址）、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`。密钥仅来自服务环境。远程服务必须 HTTPS，本地开发只允许 localhost/回环地址使用 HTTP。不跟随重定向，不读取代理环境。每批最多 64 段，单次响应读取上限 16 MB，连接超时 10 秒、读写超时 30 秒。

单次 `/search` 响应中 `mode=hybrid` 的前提是当前文档已经具备与查询一致配置的实际向量，并且确实执行了向量与词项的融合检索。索引失败不能仅因为存在环境变量就将该次检索报告为 hybrid。`bootstrap` / `health` 的 `capabilities.retrieval_mode` 只表示接口配置具备的能力；具体检索是否成功使用向量，以 `/search` 的执行结果为准。

## 抓取约束

- 仅标准端口 HTTPS，无 URL 凭据或控制字符。
- DNS 全部解析结果必须为公网地址；连接固定到已检查 IP，同时使用原域名作 TLS 验证，防止 DNS 重新绑定。
- 最多 3 次重定向，每一跳重复 URL 和公网地址检查。
- 每个文件 20 MB，socket 超时 15 秒、整次采集截止时间 90 秒；超时关闭活动 socket。DNS 等待最多 10 秒、最多 8 个同时存活的守护解析线程，系统 DNS 异常不会无限占用应用工作线程。
- 不使用环境代理，不发送 cookie 或登录凭据；拒绝压缩 HTTP 传输，按实际读取字节校验大小。
- 从主 HTML 中只发现同源、具有受支持文件扩展名的直接链接，最多尝试 5 个；附件重定向也必须同源。不会把 HTML 页面链接当作递归入口。超过 4 MB 的 HTML 不进行附件发现。
- 次级附件只返回原件，失败记录在 warnings 中；不得静默展示“所有附件已抓取”。

运维需要限制可配置来源的权限、并发任务和保留策略，核对来源使用条件，针对数据规模配置队列、进程隔离及重试。此模块不包含 JavaScript 网站浏览器、验证码绕过、OCR 服务或全网爬虫。

## 验证

`PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s backend/tests -p test_ingestion.py`

用例覆盖中文检索与引用元数据、章节定位、PDF 实际文本与无文本失败、DOCX 解压/实体边界、HTML 脚本排除、次级拒绝解析、embedding 未配置与维度错误、SSRF 与重定向、同源直接附件和 DNS 截止时间。真实外部 embedding 凭据与各目标法规网站需在部署配置后做连接验证。
