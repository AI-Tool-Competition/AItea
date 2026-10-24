# DOC / WPF 离线文本与映射

`backend/app/contexts/knowledge/infrastructure/parsing/legacy_doc_parsing.py` 由现有 `parse_bounded` 工作进程调用。工具是可选的服务器配置，不由上传文件选择，也不自动下载。普通 secondary 附件继续只保存原件。

DOC 同时保存两组不同的定位：原始 OLE 目录 ID、流名、流内字节范围与 SHA，以及 Word 97 FIB / CLX / PlcPcd 的 CP→WordDocument 流范围；转换 DocBook XML 的 SHA、完整字节、XPath、表格行列/空单元格/合并属性、footnote 与声明断页也完整保留。CP 是 UTF-16 字符位置；OLE 流偏移不是容器文件偏移。`beginpage` 只是转换器声明的断页，不能当作核验过的打印页。真实 antiword 输出的表格可以嵌在 `para` 内，结构映射独立于正文消费，避免漏表或重复正文。

WPF 保存完整 HTML 和 librevenge raw events，事件的字节范围与 SHA 仅定位转换 artifact。页跨度、表格、页眉页脚、字段及对象事件保留原样，不猜测其原始 WPF 记录或打印页。图片外链不会下载；内嵌 PNG/JPEG/GIF 校验 Base64、签名、解码、尺寸、帧数、单张和总字节后保留图片 SHA、字节与 artifact 元素定位，状态为 `vision_pending`。只有对象引用而没有图像字节时，明确为 `image_reference_pending`。DOC 的 Data / ObjectPool 等原流字节保留为待核验对象，不假称已经得到图片。

转换器之间或同一转换器不同输出之间不被当作独立字符证据。XML 正文字符清单检查只说明一个转换 artifact 的文字没有在接线中遗漏或重复。XPath 与原始 CP 的对应、原文可见性、修订、字段值、布局及对象是否完整尚未核验，所以 DOC/WPF 全部保持 `quality.complete=false`，不能进入向量或正式法规检索。源 SHA、转换 artifact SHA 和工具/运行资料 SHA 共同保留，原件始终可追溯。

## 服务器内配置

下面是本次实际验证的 Ubuntu 22.04 amd64 运行资料。使用其他版本、ABI 或服务器时须重新核验，不能把这些 SHA 当作其他包的证明。下载后先对照官方 APT 元数据核验包 URL、版本、字节数和 SHA256；仅解包到当前 worktree 已忽略的 `.dev/legacy-tools/`，不运行包安装脚本、不全局安装。

| 包 | 版本 | 已核验包 SHA256 |
| --- | --- | --- |
| antiword | 0.37-16 | `b62117f65ccca2ececa9be365ab1276d94df37fa0635217c3ec068927cf17c83` |
| libwpd-tools | 0.10.3-2build1 | `4b27af0cb2778f1a25cd10bb0c71e30c67f6187989fe128e5131ca4f048430e4` |
| libwpd-0.10-10 | 0.10.3-2build1 | `d69973163c4514904c62af0be8fda73967d5fd31947b9f8f08c4bbe43db10cde` |
| librevenge-0.0-0 | 0.0.4-6ubuntu7 | `4531f5bb0f100fcc4d6930d7eb8d72b1daa4984d7c078548a4fdf5b3055947aa` |

新 launcher 只使用系统 C / Linux API，不新增 Python 依赖：

```bash
mkdir -p .dev/legacy-tools/document
cc -Wall -Wextra -Werror -O2 \
  scripts/tools/legacy-doc/offline-converter.c \
  -o .dev/legacy-tools/document/offline-converter
chmod 0555 .dev/legacy-tools/document/offline-converter
export TEA_LEGACY_CONVERTER_PATH="$PWD/.dev/legacy-tools/document/offline-converter"
export TEA_ANTIWORD_PATH="$PWD/.dev/legacy-tools/doc/runtime/usr/bin/antiword"
export TEA_ANTIWORD_HOME="$PWD/.dev/legacy-tools/doc/runtime/usr/share/antiword"
export TEA_WPD_HTML_PATH="$PWD/.dev/legacy-tools/wpf/runtime/usr/bin/wpd2html"
export TEA_WPD_RAW_PATH="$PWD/.dev/legacy-tools/wpf/runtime/usr/bin/wpd2raw"
export TEA_WPD_LIBRARY_DIR="$PWD/.dev/legacy-tools/wpf/runtime/usr/lib/x86_64-linux-gnu"
```

所有配置都要求绝对路径、当前管理员或 root 所有的普通文件/目录、无组或其他用户写权限。工具不能是 symlink；共享库包内 symlink 必须解析到同一运行目录。配置与文件哈希在转换前后复核。构建默认权限可能是 0775，必须执行上面的权限收紧，不能为通过检查放宽解析器门禁。实际加载所需的系统 libc/libstdc++ 等 ABI 仍需服务器管理员核验；manifest 对此如实记录。

launcher 绑定实际父 PID，设置 parent-death `SIGKILL`，创建 user/network namespace，用户映射后再次设置 parent-death，启用 no-new-privileges。它使用 exec 代替自身，不创建另一个 converter 子进程；seccomp 禁止 socket/socketpair/connect（包括 Unix socket）及 fork/clone/vfork，并拒绝不同 syscall ABI。不会执行 shell、宏或外部字段。原件 20 MiB、转换单输出 8 MiB、总转换 artifact 12 MiB、对象字节总量 8 MiB；工作进程仍受现有内存、CPU、结果体积与墙钟上限约束，单次 native converter 最多 18 秒。Python 边读边限制输出并清理本次创建的进程组；父工作进程被终止时，converter 由内核终止。namespace 不可用时明确失败，不能回退为联网转换。

## 格式依据与许可

原 DOC 片段映射依据 Microsoft [FibRgLw97](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/37713d3c-a0c8-40f5-821f-bc9622c7de48)、[FibRgFcLcb97](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/0c9df81f-98d0-454e-ad84-b612cd05b1a4)、[PlcPcd](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/1caae71f-35c4-49d7-adf0-af5fc766331c)、[FcCompressed](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/aa2e55a2-f4f2-4795-bab5-6d9d7a0ed249) 和 [Retrieving Text](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/01d5d8c4-cf9c-4ef9-80fd-439e763cfe01)。当前只接受本批原件实测的 `nFib=0xC1`，不猜测其他结构或修复损坏的字符表。

OLE 读取复用仓库已锁定的 xlrd 2.0.2 `compdoc`（BSD 许可）。antiword 为 GPL，libwpd 为 MPL-2.0 / LGPL-2.1-or-later，librevenge 许可见各官方包的 copyright；[libwpd 上游](https://sourceforge.net/projects/libwpd/files/libwpd/)提供版本及源码。此仓库仅提供调用与定位适配、新写的 MIT launcher，不提交上游二进制、包或复制转换器源码。
