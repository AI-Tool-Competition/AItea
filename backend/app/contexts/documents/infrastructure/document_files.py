"""Local document operations. No macros, formulas, URLs or user code are executed.

Creation uses format libraries; edits patch only specified XML text/cells and
copy every other package member unchanged. All public operations run in the
existing bounded document worker, separate from ingestion/OCR and indexing.
"""

import base64
import csv
import io
import json
import math
import re
import zipfile
from html import escape

from lxml import etree as XML

from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded

MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 1_000_000
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
MIMES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
}


def operate(action, options, content=None, *, timeout=20):
    if content is not None and not 0 < len(content) <= MAX_BYTES:
        raise ValueError("文件不能为空或超过20 MB")
    return _run_bounded(_worker, (action, options, content), min(20, timeout))


def _worker(send, action, options, content):
    try:
        if action == "inspect":
            result = inspect(content, **options)
        elif action in {"create", "edit"}:
            data = create(**options) if action == "create" else edit(content, **options)
            # Reopen serialized bytes, not the original in-memory library object.
            checked = inspect(data, options["format"], limit=1)
            result = {
                "file_base64": base64.b64encode(data).decode(),
                "validation": {
                    "reopened": True,
                    "format": options["format"],
                    "total": checked["total"],
                    "structure": checked["structure"],
                    "visual_check": "not_performed",
                    "warnings": checked["warnings"],
                },
            }
        else:
            raise ValueError("未支持的文件操作")
        send.send_bytes(json.dumps({"result": result}, ensure_ascii=False).encode())
    except (ValueError, TypeError, KeyError) as exc:
        # Our bounded validation errors contain only fixed messages, never file content.
        message = str(exc) if isinstance(exc, ValueError) else "文件内容与工具格式不符"
        send.send_bytes(json.dumps({"error": message[:240]}, ensure_ascii=False).encode())
    except Exception:
        send.send_bytes(json.dumps({"error": "文档处理失败，请检查文件格式或指定的修改位置。"}).encode())
    finally:
        send.close()


def _xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("不处理包含外部实体的XML")
    return XML.fromstring(data, XML.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))


def _package(data, format):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("文件大小超出限制")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(e.file_size for e in entries) > 50 * 1024 * 1024:
            raise ValueError("Office文件展开大小超过限制")
        seen, parts = set(), {}
        for entry in entries:
            name = entry.filename
            if (
                name.casefold() in seen
                or name.startswith("/")
                or "\\" in name
                or any(p in {"..", "."} for p in name.split("/"))
                or entry.flag_bits & 1
                or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise ValueError("Office文件包含不支持的包记录")
            seen.add(name.casefold())
            parts[name] = archive.read(entry)
        required = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
        if required[format] not in parts:
            raise ValueError("Office文件类型与内容不符")
        return parts


def _texts(parts, format):
    ns = W if format == "docx" else A
    pattern = (
        r"word/(document|header\d+|footer\d+|footnotes|endnotes)\.xml"
        if format == "docx"
        else r"ppt/slides/slide\d+\.xml"
    )
    result = []
    for part, data in parts.items():
        if not re.fullmatch(pattern, part):
            continue
        root = _xml(data)
        for index, paragraph in enumerate(root.iter(f"{{{ns}}}p")):
            nodes = _paragraph_nodes(paragraph, ns)
            text = "".join(n.text or "" for n in nodes)
            result.append({"part": part, "index": index, "text": text})
    return result


def _paragraph_nodes(paragraph, ns):
    result = []
    for node in paragraph.iter(f"{{{ns}}}t"):
        ancestor = node.getparent()
        while ancestor is not None and ancestor.tag != f"{{{ns}}}p":
            ancestor = ancestor.getparent()
        if ancestor is paragraph:
            result.append(node)
    return result


def _sheets(parts):
    root = _xml(parts["xl/workbook.xml"])
    rels = _xml(parts["xl/_rels/workbook.xml.rels"])
    targets = {r.get("Id"): r.get("Target", "") for r in rels if r.get("TargetMode") != "External"}
    result = {}
    for sheet in root.findall(f"{{{S}}}sheets/{{{S}}}sheet"):
        target = targets.get(sheet.get(f"{{{R}}}id"), "")
        part = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if not re.fullmatch(r"xl/worksheets/[^/]+\.xml", part) or part not in parts:
            raise ValueError("工作表关系无法定位")
        result[sheet.get("name")] = part
    return result


def _shared(parts):
    if "xl/sharedStrings.xml" not in parts:
        return []
    return ["".join(t.text or "" for t in n.iter(f"{{{S}}}t")) for n in _xml(parts["xl/sharedStrings.xml"])]


def _cell_value(cell, strings):
    value = cell.find(f"{{{S}}}v")
    text = value.text if value is not None else None
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(n.text or "" for n in cell.iter(f"{{{S}}}t"))
    if kind == "s":
        return strings[int(text)] if text is not None else ""
    if text is None:
        return None
    if kind == "b":
        return text == "1"
    if kind in {"str", "e", "d"}:
        return text
    return float(text) if any(c in text.lower() for c in ".e") else int(text)


def inspect(data, format, offset=0, limit=20):
    warnings = ["已检查文件可重开及内容结构；未进行页面视觉检查。"]
    structure = {}
    if format in {"docx", "pptx", "xlsx"}:
        parts = _package(data, format)
        structure["package_parts"] = len(parts)
        if format == "xlsx":
            elements, strings = [], _shared(parts)
            sheets = _sheets(parts)
            structure["sheets"] = list(sheets)
            for sheet, part in sheets.items():
                root = _xml(parts[part])
                for cell in root.iter(f"{{{S}}}c"):
                    formula = cell.find(f"{{{S}}}f")
                    elements.append(
                        {
                            "sheet": sheet,
                            "cell": cell.get("r"),
                            "value": _cell_value(cell, strings),
                            "formula": formula.text if formula is not None else None,
                        }
                    )
            warnings.append("公式保留但未计算；合并单元格、图表与打印布局需在办公软件中核对。")
        else:
            elements = _texts(parts, format)
            if format == "docx":
                structure["tables"] = sum(
                    len(_xml(v).findall(f".//{{{W}}}tbl"))
                    for k, v in parts.items()
                    if k == "word/document.xml"
                )
            else:
                structure["slides"] = sum(bool(re.fullmatch(r"ppt/slides/slide\d+\.xml", k)) for k in parts)
            warnings.append("正文含有表格、页眉页脚或模板时按part定位。域、修订和复杂对象不作自动改写。")
    elif format == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 100:
            raise ValueError("不处理加密或超过100页的PDF")
        structure["pages"] = len(reader.pages)
        elements = [{"page": i + 1, "text": p.extract_text() or ""} for i, p in enumerate(reader.pages)]
        warnings.append("未执行OCR；图片中的文字可能无法读取。PDF原文修改和PDF转Word暂不支持。")
    elif format in {"md", "txt", "csv"}:
        text = data.decode("utf-8-sig")
        elements = [{"line": i + 1, "text": line} for i, line in enumerate(text.splitlines())]
        structure["encoding"] = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
        if format == "csv":
            structure["rows"] = sum(1 for _ in csv.reader(io.StringIO(text)))
    else:
        raise ValueError("暂不支持该文件格式")
    if len(elements) > 20000 or len(json.dumps(elements, ensure_ascii=False)) > MAX_TEXT:
        raise ValueError("文档内容超过处理限制")
    selected = elements[offset : offset + limit]
    selected = [
        {**e, **({"text": e["text"][:6000], "truncated": len(e["text"]) > 6000} if "text" in e else {})}
        for e in selected
    ]
    return {
        "format": format,
        "elements": selected,
        "total": len(elements),
        "offset": offset,
        "structure": structure,
        "warnings": warnings,
    }


def _blocks(content):
    """Small Markdown subset, deliberately no HTML, resource loading or extensions."""
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if i + 1 < len(lines) and "|" in line and re.fullmatch(r"[| :\-]+", lines[i + 1].strip()):
            rows = [[cell.strip() for cell in line.strip("|").split("|")]]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                rows.append([cell.strip() for cell in lines[i].strip().strip("|").split("|")])
                i += 1
            if not 1 <= len(rows[0]) <= 10 or any(len(row) != len(rows[0]) for row in rows):
                raise ValueError("表格列数需一致且不超过10列")
            yield "table", rows
            continue
        if line:
            heading = re.match(r"^(#{1,3})\s+(.+)$", line)
            yield ("heading" + str(len(heading[1])), heading[2]) if heading else ("paragraph", line)
        i += 1


def create(format, content):
    if not isinstance(content, str) or not 0 < len(content) <= 40000:
        raise ValueError("新文件正文需在1至40000字符内")
    buffer = io.BytesIO()
    if format == "docx":
        from docx import Document
        from docx.oxml.ns import qn
        from docx.shared import Mm, Pt

        document = Document()
        section = document.sections[0]
        section.page_width, section.page_height = Mm(210), Mm(297)
        for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
            style = document.styles[name]
            style.font.name = "宋体" if name == "Normal" else "黑体"
            style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), style.font.name)
        document.styles["Normal"].font.size = Pt(11)
        for kind, text in _blocks(content):
            if kind == "table":
                table = document.add_table(rows=0, cols=len(text[0]))
                table.style = "Table Grid"
                for row in text:
                    for cell, value in zip(table.add_row().cells, row, strict=True):
                        cell.text = value
            elif kind.startswith("heading"):
                document.add_heading(text, int(kind[-1]))
            else:
                document.add_paragraph(text)
        document.save(buffer)
    elif format == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        normal = ParagraphStyle(
            "cn", fontName="STSong-Light", fontSize=11, leading=17, wordWrap="CJK", alignment=TA_LEFT
        )
        story = []
        for kind, text in _blocks(content):
            if kind == "table":
                table = Table([[Paragraph(escape(c), normal) for c in row] for row in text], repeatRows=1)
                table.setStyle(
                    TableStyle(
                        [("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]
                    )
                )
                story.append(table)
            else:
                style = ParagraphStyle(
                    kind,
                    parent=normal,
                    fontSize=16 - int(kind[-1]) if kind.startswith("heading") else 11,
                    spaceAfter=8,
                )
                story.extend([Paragraph(escape(text), style), Spacer(1, 4)])
        SimpleDocTemplate(buffer, pagesize=(595.276, 841.89)).build(story)
    elif format == "xlsx":
        from openpyxl import Workbook, load_workbook

        payload = json.loads(content)
        sheets = payload.get("sheets", [])
        if not 1 <= len(sheets) <= 12:
            raise ValueError("Excel需要1至12张工作表")
        book = Workbook()
        book.remove(book.active)
        names = set()
        for sheet in sheets:
            name, rows = sheet["name"], sheet["rows"]
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 31
                or re.search(r"[\\/*?:\[\]]", name)
                or name.casefold() in names
            ):
                raise ValueError("工作表名称重复或无效")
            names.add(name.casefold())
            if not 1 <= len(rows) <= 1000 or any(not isinstance(row, list) or len(row) > 30 for row in rows):
                raise ValueError("每张表最多1000行30列")
            ws = book.create_sheet(name)
            for row in rows:
                for value in row:
                    if value is not None and type(value) not in (str, int, float, bool):
                        raise ValueError("单元格仅支持文字、数值、布尔或空值")
                    if isinstance(value, str) and len(value) > 4000:
                        raise ValueError("单元格文字过长")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise ValueError("单元格数值必须有限")
                ws.append(row)
                for cell in ws[ws.max_row]:
                    # User-supplied formulas/URLs are literal text, never executable data.
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
            ws.freeze_panes = "A2"
        book.save(buffer)
        reopened = load_workbook(io.BytesIO(buffer.getvalue()), data_only=False)
        if reopened.sheetnames != [s["name"] for s in sheets]:
            raise ValueError("保存后的工作表核对失败")
        reopened.close()
    elif format == "pptx":
        from pptx import Presentation
        from pptx.util import Pt

        slides = json.loads(content).get("slides", [])
        if not 1 <= len(slides) <= 30:
            raise ValueError("PPT需要1至30页")
        deck = Presentation()
        for item in slides:
            title, bullets = item["title"], item.get("bullets", [])
            if (
                not isinstance(title, str)
                or len(title) > 100
                or not isinstance(bullets, list)
                or len(bullets) > 8
                or any(not isinstance(b, str) or len(b) > 250 for b in bullets)
            ):
                raise ValueError("每页标题最多100字、正文最多8项且每项250字")
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = title
            frame = slide.placeholders[1].text_frame
            for i, text in enumerate(bullets):
                paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
                paragraph.text = text
                paragraph.font.size = Pt(20)
        deck.save(buffer)
    elif format in {"md", "txt", "csv"}:
        buffer.write(content.encode("utf-8-sig" if format == "csv" else "utf-8"))
    else:
        raise ValueError("暂不支持该文件格式")
    data = buffer.getvalue()
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("生成文件大小超出限制")
    return data


def _replace_runs(root, ns, change):
    found = []
    for paragraph in root.iter(f"{{{ns}}}p"):
        nodes = _paragraph_nodes(paragraph, ns)
        text = "".join(node.text or "" for node in nodes)
        count = text.count(change["old_text"])
        if count:
            if ns == W and any(
                n.tag in {f"{{{W}}}{tag}" for tag in ("fldChar", "instrText", "ins", "del", "tab", "br")}
                for n in paragraph.iter()
            ):
                raise ValueError("目标段落含域、修订或换行，请在办公软件中修改该段")
            found.append((nodes, text, count))
    if sum(n for _, _, n in found) != change["expected_count"]:
        raise ValueError("原文命中次数不符，未保存修改；请重新读取并缩小修改范围")
    for nodes, text, _ in found:
        starts, pos = [], 0
        for node in nodes:
            starts.append(pos)
            pos += len(node.text or "")
        matches = list(re.finditer(re.escape(change["old_text"]), text))
        for match in reversed(matches):
            start, end = match.span()
            first = next(i for i, n in enumerate(nodes) if starts[i] <= start < starts[i] + len(n.text or ""))
            for i in range(len(nodes) - 1, first - 1, -1):
                value = nodes[i].text or ""
                left, right = max(start - starts[i], 0), min(end - starts[i], len(value))
                if left < right:
                    nodes[i].text = value[:left] + (change["new_text"] if i == first else "") + value[right:]
                    nodes[i].set(XML_SPACE, "preserve")


def edit(data, format, replacements=None, cells=None):
    replacements, cells = replacements or [], cells or []
    if not replacements and not cells:
        raise ValueError("请提供明确的文字修改或单元格修改")
    if format in {"md", "txt", "csv"}:
        if cells or any(c.get("part") for c in replacements):
            raise ValueError("文本文件仅支持精确文字替换")
        bom = data.startswith(b"\xef\xbb\xbf")
        text = data.decode("utf-8-sig")
        for change in replacements:
            if text.count(change["old_text"]) != change["expected_count"]:
                raise ValueError("原文命中次数不符，未保存修改")
            text = text.replace(change["old_text"], change["new_text"])
        result = text.encode("utf-8-sig" if bom else "utf-8")
    elif format in {"docx", "pptx", "xlsx"}:
        parts = _package(data, format)
        if any(k.startswith("_xmlsignatures/") for k in parts):
            raise ValueError("带数字签名的Office文件需要在签署软件中修改")
        touched = {}
        if format in {"docx", "pptx"}:
            if cells:
                raise ValueError("Word和PPT仅支持指定part的文字修改")
            allowed = {e["part"] for e in _texts(parts, format)}
            for change in replacements:
                part = change["part"]
                if any(c in change["new_text"] or c in change["old_text"] for c in "\r\n\t"):
                    raise ValueError("Word和PPT定点改字暂不支持新增换行或制表符")
                if part not in allowed:
                    raise ValueError("修改位置不在已读取的正文部件中")
                if part not in touched:
                    touched[part] = _xml(parts[part])
                _replace_runs(touched[part], W if format == "docx" else A, change)
        else:
            if replacements:
                raise ValueError("Excel仅支持按工作表和单元格修改")
            sheets, strings = _sheets(parts), _shared(parts)
            for change in cells:
                part = sheets.get(change["sheet"])
                if part is None:
                    raise ValueError("未找到指定工作表")
                if part not in touched:
                    touched[part] = _xml(parts[part])
                from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

                row, col = coordinate_to_tuple(change["cell"])
                for merge in touched[part].iter(f"{{{S}}}mergeCell"):
                    left, top, right, bottom = range_boundaries(merge.get("ref"))
                    if left <= col <= right and top <= row <= bottom and (col, row) != (left, top):
                        raise ValueError("合并单元格只能修改左上角锚点")
                cell = touched[part].find(f".//{{{S}}}c[@r='{change['cell']}']")
                if cell is None or cell.find(f"{{{S}}}f") is not None:
                    raise ValueError("仅可修改已存在的非公式单元格")
                if _cell_value(cell, strings) != change["expected_value"]:
                    raise ValueError("单元格原值不一致，未保存修改")
                for child in list(cell):
                    if child.tag in {f"{{{S}}}v", f"{{{S}}}is"}:
                        cell.remove(child)
                value = change["value"]
                if isinstance(value, str):
                    cell.set("t", "inlineStr")
                    node = XML.SubElement(XML.SubElement(cell, f"{{{S}}}is"), f"{{{S}}}t")
                    node.text = value
                    node.set(XML_SPACE, "preserve")
                elif value is None:
                    cell.attrib.pop("t", None)
                else:
                    cell.set("t", "b" if isinstance(value, bool) else "n")
                    XML.SubElement(cell, f"{{{S}}}v").text = (
                        str(int(value)) if isinstance(value, bool) else str(value)
                    )
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(output, "w") as edited:
            edited.comment = original.comment
            for entry in original.infolist():
                content = (
                    XML.tostring(
                        touched[entry.filename], xml_declaration=True, encoding="UTF-8", standalone=True
                    )
                    if entry.filename in touched
                    else parts[entry.filename]
                )
                edited.writestr(entry, content)
        result = output.getvalue()
        after = _package(result, format)
        if any(after[k] != v for k, v in parts.items() if k not in touched):
            raise ValueError("非目标部件核对失败，未保存修改")
    else:
        raise ValueError("PDF暂不支持原文修改，请提供可编辑源文件")
    if len(result) > MAX_BYTES:
        raise ValueError("修改后的文件大小超过限制")
    return result
