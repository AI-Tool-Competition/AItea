"""Bounded content and declared text encoding rules."""

import codecs
import re

MAX_TEXT = 4_000_000


def _decode(content: bytes) -> str:
    # Honor a declared HTML charset before trying a Chinese legacy encoding:
    # Latin-1 can otherwise decode successfully as mojibake under GB18030.
    if not content.startswith(b"\xef\xbb\xbf"):
        for tag in re.findall(rb"<meta\b[^>]{0,1024}>", content[:8192], re.I):
            match = re.search(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]{1,40})", tag, re.I)
            if match:
                try:
                    encoding = codecs.lookup(match[1].decode("ascii")).name
                    if encoding not in {"utf-8", "iso8859-1", "cp1252", "gb18030", "gbk", "gb2312", "big5"}:
                        raise ValueError("HTML 声明了不支持的文本编码。")
                    return content.decode(encoding)
                except (LookupError, UnicodeDecodeError) as exc:
                    raise ValueError("HTML 声明的文本编码无法读取。") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("文档文本编码无法识别，请转换为 UTF-8。")
