# 只读 MDB 解析工具

`mdb-reader.c` 使用 libmdb 1.0.0 的公开只读 API，按原始 JET4 表行导出数据，不执行 SQL 查询、Access 宏、报表脚本或外部连接。Python 接线位于 `backend/app/contexts/documents/infrastructure/parsers/mdb_parsing.py`，必须由现有 `parse_bounded` 工作进程调用；普通 secondary 附件仍只保存原件。

服务器通过 `TEA_MDB_READER_PATH` 指定已审核的绝对辅助程序路径。未配置、无法创建 Linux user/network namespace、工具版本不符、原件或行/Memo 链不完整时明确失败，不使用有网络的替代命令。工具启动时绑定实际父进程 PID，设置 parent-death `SIGKILL`；namespace 用户映射后再次设置，避免 Linux 清除该设置。Python 创建独立进程组，执行时间最多 25 秒，边读边限制输出为 32 MiB。C 同时限制内存、CPU 和墙钟时间。

解析保留原列类型/属性、NULL 与空串、嵌入换行、HS 前导零、原始 IEEE 数值字节、不可变原件 SHA、表名、行序号与真实 MDB 内部 storage page/row slot。内部存储页不表示打印页；Access 非表对象的 catalog ID 也不当作文件偏移。Python 对原行、普通字段与完整 Memo 链的字节 SHA 再次从原件核验。Jet4 压缩/非压缩 UTF-16 严格解码，不替换错误字符。

报表、查询、表单、链接表、未知对象和文档属性保留映射并阻挡整份资料的 `quality.complete`。OLE、Binary、复杂列和未知类型保留 source/record 定位及原字段字节，明确待核验；OLE 长对象当前未展开，不能认为已获得图片。表格映射完成与整份原件文本完成分别记录。数值按原存储值导出，不擅自应用 Access 显示格式、重算公式或猜测法定单位、有效性、资料层级。

## 项目内构建

可选系统工具只放当前 worktree 已忽略的 `.dev/legacy-tools/mdb/`，不需要全局安装软件包或修改 APT 配置。本机 Ubuntu 22.04 amd64 使用已安装的 `libglib2.0-0 2.72.1-1` 对应开发包构建；其他服务器须按其运行库版本核验 ABI、官方包与 namespace 支持。

```bash
mkdir -p .dev/legacy-tools/mdb/packages .dev/legacy-tools/mdb/runtime
cd .dev/legacy-tools/mdb/packages
apt-get download mdbtools=1.0.0+dfsg-1 libmdb3=1.0.0+dfsg-1 libmdbsql3=1.0.0+dfsg-1 mdbtools-dev=1.0.0+dfsg-1 libglib2.0-dev=2.72.1-1
cd ../../../..
for package in .dev/legacy-tools/mdb/packages/*.deb; do
  dpkg-deb --extract "$package" .dev/legacy-tools/mdb/runtime
done
cc -Wall -Wextra -Werror -O2 \
  -I .dev/legacy-tools/mdb/runtime/usr/include \
  -I .dev/legacy-tools/mdb/runtime/usr/include/glib-2.0 \
  -I .dev/legacy-tools/mdb/runtime/usr/lib/x86_64-linux-gnu/glib-2.0/include \
  scripts/tools/mdb/mdb-reader.c \
  -L .dev/legacy-tools/mdb/runtime/usr/lib/x86_64-linux-gnu \
  -Wl,-rpath,"$PWD/.dev/legacy-tools/mdb/runtime/usr/lib/x86_64-linux-gnu" \
  -lmdb -lglib-2.0 -ldl -o .dev/legacy-tools/mdb/mdb-reader
export TEA_MDB_READER_PATH="$PWD/.dev/legacy-tools/mdb/mdb-reader"
```

服务配置将该绝对路径写入集中配置 `data/config/application.env` 的 `TEA_MDB_READER_PATH`，然后重启当前项目服务以加载配置。当前仅主资料 `.mdb` 进入该 reader；secondary 附件仍只保存原件，`.accdb` 和非 Jet4 文件不会降级猜读。解析器版本更新或首次配置 reader 后，正式来源池会对系统主资料中因未支持格式而失败的 MDB 自动安排一次重解析；正文仍需通过完整性门禁才能进入检索。

下载后先逐包核对 `apt-cache show <package>=<version>` 的官方 SHA256，保存包 URL、版本、字节数和实际 SHA，再解包。此例只解包构建所需头文件与库，不运行包安装脚本；服务器已有的 glib 和 libc 也属于运行依赖。程序 manifest 记录辅助程序 SHA、协议版本、libmdb 头文件版本与实际加载的 libmdb 二进制 SHA。构建程序与项目配置仅由服务器管理员管理，用户上传文件不能选择可执行路径或命令。

## 格式来源和许可

格式依据为 MDB Tools `v1.0.0` 的公开头文件及 [data.c](https://github.com/mdbtools/mdbtools/blob/v1.0.0/src/libmdb/data.c)、[iconv.c](https://github.com/mdbtools/mdbtools/blob/v1.0.0/src/libmdb/iconv.c)，包含行字段 NULL 位、BOOL 位、Memo flags/chains 和 Jet4 Unicode 压缩切换规则。libmdb 与头文件为 LGPL-2.0-or-later，见上游 [COPYING.LIB](https://github.com/mdbtools/mdbtools/blob/v1.0.0/COPYING.LIB)；mdbtools 命令行工具为 GPL-2.0-or-later，见 [COPYING](https://github.com/mdbtools/mdbtools/blob/v1.0.0/COPYING)。项目不提交上游二进制或包，也不复制 `mdb-export` / `mdb-json` 的固定缓冲导出实现。`mdb-reader.c` 的 Memo 格式读取是依据上述实现重新编写的适配，按 LGPL-2.0-or-later 提供，并随源码保留 [COPYING.LIB](COPYING.LIB) 与来源说明。

上游 1.0.0 CLI 会用固定 200000 字节绑定缓冲，且以导出长度零同时省略 NULL 和空串。这里直接读取 storage bytes，并从原件复核，避免将 CLI 成功当作完整正文证据。其他工具/版本没有经过本次验证。
