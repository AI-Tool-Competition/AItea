"""Recognize explicit file delivery requests, without inspecting model reasoning."""

import re

TEXT_FORMATS = frozenset({"docx", "pdf", "md", "txt"})
FORMAT_NAMES = {
    "docx": r"(?<![a-z0-9])(?:word|docx?)(?![a-z0-9])",
    "pdf": r"(?<![a-z0-9])pdf(?![a-z0-9])",
    "xlsx": r"(?<![a-z0-9])(?:excel|xlsx?)(?![a-z0-9])",
    "pptx": r"(?<![a-z0-9])(?:powerpoint|pptx?)(?![a-z0-9])|演示文稿",
    "md": r"(?<![a-z0-9])(?:markdown|md)(?![a-z0-9])",
    "txt": r"(?<![a-z0-9])txt(?![a-z0-9])|纯文本文件",
    "csv": r"(?<![a-z0-9])csv(?![a-z0-9])",
}
ACTION = r"生成|制作|创建|新建|起草|撰写|编写|整理|导出|另存|保存|转成|转为|转换|下载|提供|给|要|做|写|create|generate|export|save"
EDIT = r"修改|编辑|修订|替换|填入|更新|改为|改成|改到|更改|edit|replace"


def file_delivery_request(question):
    """Return requested formats and whether an original must be edited.

    A generic request for a document defaults to DOCX. Instructions to only
    answer in chat, negated formats and questions about using files do not
    request a new file. This controls delivery only, not content quality.
    """
    formats, generic, editing = [], False, False
    for clause in re.split(r"[，,。；;！？!?\n]", question):
        if re.search(
            r"不要|不用|无需|不需要|暂不|先不|别|不(?:生成|导出|创建|新建|下载|保存)|只(?:要|给|在).{0,12}(?:正文|聊天|对话)",
            clause,
        ):
            continue
        if re.search(r"(?:生成|导出|下载).{0,8}(?:失败|报错|出错|不了|不成功)", clause):
            continue
        if re.search(
            r"(?:怎么|如何|为什么|为何).{0,15}(?:生成|导出|下载|编辑|修改)|(?:需要|要)哪些.{0,12}(?:信息|资料|材料)",
            clause,
        ):
            continue
        found = [name for name, pattern in FORMAT_NAMES.items() if re.search(pattern, clause, re.I)]
        artifact = bool(found or re.search(r"文档|文件", clause))
        if artifact and re.search(ACTION + "|" + EDIT, clause, re.I):
            formats.extend(name for name in found if name not in formats)
            generic = True
        editing |= bool(re.search(EDIT, re.sub(r"可编辑|可修改", "", clause), re.I))
        editing |= bool(re.search(r"(?:其他|其余).{0,12}(?:不变|保留)", clause))
    return tuple(formats or (["docx"] if generic else [])), editing


def export_filename(content, format):
    heading = re.search(r"(?m)^#{1,6}\s+([^\n]{1,70})", content)
    title = heading[1].strip(" *#`") if heading else "茶渡文档"
    return f"{title}.{format}"
