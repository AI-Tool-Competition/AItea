"""Bounded OOXML source mapping; no field evaluation or external resource access.

Word's body is only one package part. Headers, notes, drawings and relationships
remain separate source objects, without inventing physical pages. Unresolved
render-time content blocks indexing even when its cached text is available.
See Microsoft's WordprocessingML structure and table documentation:
https://learn.microsoft.com/en-us/office/open-xml/word/structure-of-a-wordprocessingml-document
https://learn.microsoft.com/en-us/office/open-xml/word/working-with-wordprocessingml-tables
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import struct
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement, build_manifest

MAX_BYTES = 20 * 1024 * 1024
MAX_EXPANDED = 50 * 1024 * 1024
MAX_XML = 16 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_ELEMENTS = 20_000
MAX_XML_NODES = 200_000
MAX_TEXT = 4_000_000
MAX_RESULT = 40 * 1024 * 1024
MAX_OBJECTS = 2000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W, "r": R}
_NATIVE_ROOTS = {"document", "hdr", "ftr", "footnotes", "endnotes", "comments"}
_METADATA_ROOTS = {
    *(f"{{{W}}}{name}" for name in ("styles", "settings", "webSettings", "fonts", "numbering")),
    "{http://schemas.openxmlformats.org/drawingml/2006/main}theme",
    "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties",
    "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Properties",
    "{http://schemas.openxmlformats.org/officeDocument/2006/custom-properties}Properties",
    "{http://schemas.openxmlformats.org/package/2006/content-types}Types",
    f"{{{PR}}}Relationships",
}
_METADATA_RELATIONS = {
    "styles",
    "settings",
    "webSettings",
    "fontTable",
    "numbering",
    "theme",
    "metadata/core-properties",
    "extended-properties",
    "custom-properties",
}
_CONTENT_RELATIONS = {"officeDocument", "header", "footer", "footnotes", "endnotes", "comments"}
_CONTENT_ROOTS = {
    "officeDocument": "document",
    "header": "hdr",
    "footer": "ftr",
    "footnotes": "footnotes",
    "endnotes": "endnotes",
    "comments": "comments",
}
_OBJECT_TAGS = {
    "drawing",
    "pict",
    "object",
    "altChunk",
    "oMath",
    "oMathPara",
    "AlternateContent",
    "subDoc",
    "contentPart",
}
_REVISION_TAGS = {"ins", "del", "moveFrom", "moveTo", "conflictIns", "conflictDel"}
_RENDER_PROPERTIES = {"numPr", "vanish", "webHidden", "rtl", "bidi", "textDirection", "framePr"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


def _xml(data: bytes, name: str):
    if len(data) > MAX_XML:
        raise ValueError("DOCX 正文超过处理限制。")
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        elif data.startswith((b"\x00<", b"<\x00")):
            text = data.decode("utf-16be" if data.startswith(b"\x00<") else "utf-16le")
        else:
            text = data.decode("utf-8-sig")
        declaration = re.match(r"\s*<\?xml\b[^?]*\?>", text)
        if declaration:
            encoding = re.search(r"encoding\s*=\s*['\"]([^'\"]+)['\"]", declaration[0], re.I)
            if encoding and encoding[1].casefold() not in {"utf-8", "utf-16", "utf-16le", "utf-16be"}:
                raise ValueError("DOCX XML 声明了不支持的文本编码。")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
            raise ValueError("DOCX 不允许 DOCTYPE 或 ENTITY 声明。")
        root = ET.fromstring(data)
    except (UnicodeError, ET.ParseError) as exc:
        raise ValueError("DOCX XML 编码或结构损坏。") from exc
    paths, stack, nodes = {}, [(root, f"/{root.tag}[1]", 1)], 0
    # Clark notation paths identify namespaces without relying on mutable prefixes.
    while stack:
        node, path, depth = stack.pop()
        nodes += 1
        if depth > 128 or nodes > MAX_XML_NODES:
            raise ValueError("DOCX XML 嵌套深度或节点数量超过限制。")
        paths[node] = path
        counts, children = Counter(), []
        for child in node:
            counts[child.tag] += 1
            children.append((child, f"{path}/{child.tag}[{counts[child.tag]}]", depth + 1))
        stack.extend(reversed(children))
    return root, paths, nodes


def _package(content: bytes, *, max_bytes: int = MAX_BYTES):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise ValueError("DOCX 原件处理上限无效。")
    if not isinstance(content, bytes) or not content or len(content) > max_bytes:
        raise ValueError(f"DOCX 原件不能为空且不能超过 {max_bytes // (1024 * 1024)} MB。")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(x.file_size for x in entries) > MAX_EXPANDED:
                raise ValueError("DOCX 解压大小或文件数量超过限制。")
            seen, intervals, parts = set(), [], {}
            for entry in entries:
                name = entry.filename
                path = name[:-1] if entry.is_dir() else name
                if (
                    not path
                    or len(name) > 1024
                    or "\\" in name
                    or "\x00" in entry.orig_filename
                    or path.startswith("/")
                    or ":" in path
                    or any(segment in {"", ".", ".."} for segment in path.split("/"))
                    or unquote(path) != path
                    or path.casefold() in seen
                ):
                    raise ValueError("DOCX 包含重复或不安全的 ZIP 路径。")
                seen.add(path.casefold())
                if entry.flag_bits & 1:
                    raise ValueError("DOCX 不允许加密 ZIP 成员。")
                if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError("DOCX ZIP 压缩格式不受支持。")
                offset = entry.header_offset
                if offset < 0 or offset + 30 > len(content) or content[offset : offset + 4] != b"PK\x03\x04":
                    raise ValueError("DOCX ZIP 本地记录损坏。")
                fields = struct.unpack_from("<IHHHHHIIIHH", content, offset)
                if fields[2] & 1:
                    raise ValueError("DOCX 不允许加密 ZIP 成员。")
                if fields[2] != entry.flag_bits or fields[3] != entry.compress_type:
                    raise ValueError("DOCX ZIP 本地记录与目录不符。")
                start = offset + 30 + fields[-2] + fields[-1]
                end = start + entry.compress_size
                if start > len(content) or end > archive.start_dir:
                    raise ValueError("DOCX ZIP 成员与目录发生重叠。")
                local_name = content[offset + 30 : offset + 30 + fields[-2]].decode(
                    "utf-8" if fields[2] & 0x800 else "cp437", errors="strict"
                )
                if local_name != entry.orig_filename:
                    raise ValueError("DOCX ZIP 本地文件名与目录不符。")
                interval_end = end
                if entry.flag_bits & 8:
                    descriptor_start = end + (4 if content[end : end + 4] == b"PK\x07\x08" else 0)
                    if descriptor_start + 12 > archive.start_dir or struct.unpack_from(
                        "<III", content, descriptor_start
                    ) != (entry.CRC, entry.compress_size, entry.file_size):
                        raise ValueError("DOCX ZIP 数据描述符损坏。")
                    interval_end = descriptor_start + 12
                elif fields[6:9] != (entry.CRC, entry.compress_size, entry.file_size):
                    raise ValueError("DOCX ZIP 成员大小或校验记录不符。")
                intervals.append((offset, interval_end))
                if name.lower().endswith((".xml", ".rels")) and entry.file_size > MAX_XML:
                    raise ValueError("DOCX 正文超过处理限制。")
                compressed = content[start:end]
                if entry.compress_type == zipfile.ZIP_DEFLATED:
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    data = decoder.decompress(compressed, entry.file_size + 1)
                    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                        raise ValueError("DOCX ZIP 解压流不完整或包含额外数据。")
                else:
                    data = compressed
                if len(data) != entry.file_size or zlib.crc32(data) != entry.CRC:
                    raise ValueError("DOCX ZIP 成员大小或校验失败。")
                if not entry.is_dir():
                    parts[name] = data
            intervals.sort()
            if any(end > following[0] for (_, end), following in zip(intervals, intervals[1:])):
                raise ValueError("DOCX ZIP 成员发生重叠。")
            if "word/document.xml" not in parts:
                raise ValueError("DOCX 文件缺少正文。")
            return parts
    except (
        zipfile.BadZipFile,
        KeyError,
        RuntimeError,
        EOFError,
        OSError,
        struct.error,
        UnicodeError,
        zlib.error,
    ) as exc:
        raise ValueError("DOCX 文件损坏或缺少正文。") from exc


def _relationship_owner(name: str) -> str | None:
    if name == "_rels/.rels":
        return ""
    folder, leaf = posixpath.split(name)
    if posixpath.basename(folder) != "_rels" or not leaf.endswith(".rels"):
        return None
    return posixpath.join(posixpath.dirname(folder), leaf[:-5])


def _target(owner: str, target: str) -> str | None:
    try:
        split = urlsplit(target)
        path = unquote(split.path, errors="strict")
    except (ValueError, UnicodeError):
        return None
    if split.scheme or split.netloc or split.query or "\\" in path or "\x00" in path or ":" in path:
        return None
    if not path and split.fragment:
        return owner or None
    resolved = posixpath.normpath(
        path.lstrip("/") if path.startswith("/") else posixpath.join(posixpath.dirname(owner), path)
    )
    if resolved in {"", ".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        return None
    return resolved


def _image(data: bytes):
    """Check existing raster bytes only; no rendering or active format decoding."""
    if len(data) > MAX_IMAGE_BYTES:
        return {"status": "pending_vision", "reason": "image_byte_limit"}
    mime, width, height = None, None, None
    if len(data) >= 33 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", data, 16)
        mime = "image/png"
    elif len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width, height = struct.unpack_from("<HH", data, 6)
        mime = "image/gif"
    elif data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD9, 0xDA}:
                break
            if marker in {0x01, *range(0xD0, 0xD8)}:
                continue
            if offset + 2 > len(data):
                break
            size = struct.unpack_from(">H", data, offset)[0]
            if size < 2 or offset + size > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2} and size >= 8:
                height, width = struct.unpack_from(">HH", data, offset + 3)
                mime = "image/jpeg"
                break
            offset += size
    if mime is None:
        return {"status": "pending_vision", "reason": "unsupported_or_corrupt_image"}
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        return {"status": "pending_vision", "reason": "image_dimension_limit", "mime_type": mime}
    return {
        "status": "pending_vision",
        "mime_type": mime,
        "width": width,
        "height": height,
        "dimensions_from": "raster_signature",
        "pixels_verified": False,
    }


class _Mapping:
    def __init__(self, parts: dict, source_hash: str):
        self.parts, self.source_hash = parts, source_hash
        self.trees, self.part_hashes, self.relationships, self.relation_index = {}, {}, [], {}
        self.elements, self.paragraphs, self.tables, self.objects, self.assets = [], [], [], [], []
        self.references, self.consumed_text = [], set()
        self.styles, self.default_styles = {}, {}
        self.review, self.review_keys, self.headings = [], set(), []
        self.text_size, self.paragraph_count, self.cell_count, self.node_count, xml_text = 0, 0, 0, 0, 0
        for name, data in parts.items():
            self.part_hashes[name] = hashlib.sha256(data).hexdigest()
            if name.lower().endswith((".xml", ".rels")):
                tree = _xml(data, name)
                self.node_count += tree[2]
                if self.node_count > MAX_XML_NODES:
                    raise ValueError("DOCX XML 总节点数量超过限制。")
                xml_text += sum(len(node.text or "") + len(node.tail or "") for node in tree[0].iter())
                if xml_text > MAX_TEXT:
                    raise ValueError("DOCX XML 文本总量超过处理限制。")
                self.trees[name] = tree

    def locator(self, part, node, **values):
        return {
            "kind": "docx",
            "source_format": "docx",
            "source_hash": self.source_hash,
            "package_part": part,
            "part_hash": self.part_hashes[part],
            "xml_path": self.trees[part][1][node],
            **values,
        }

    def pending(self, reason, locator=None, **values):
        identity = (
            reason,
            json.dumps(locator, sort_keys=True) if locator else "",
            json.dumps(values, sort_keys=True),
        )
        if identity in self.review_keys:
            return
        self.review_keys.add(identity)
        self.review.append({"reason": reason, **({"locator": locator} if locator else {}), **values})
        if len(self.review) > MAX_OBJECTS:
            raise ValueError("DOCX 待核验对象超过处理限制。")

    def read_relationships(self):
        for name, (root, _, _) in self.trees.items():
            if not name.endswith(".rels"):
                continue
            owner = _relationship_owner(name)
            if owner is None or root.tag != f"{{{PR}}}Relationships" or (owner and owner not in self.parts):
                self.pending("unmapped_relationship_part", self.locator(name, root))
                continue
            for node in root:
                rid, kind, target = node.get("Id", ""), node.get("Type", ""), node.get("Target", "")
                mode = node.get("TargetMode", "Internal")
                if (
                    node.tag != f"{{{PR}}}Relationship"
                    or not rid
                    or not kind
                    or not target
                    or max(map(len, (rid, kind, target))) > 4096
                ):
                    raise ValueError("DOCX 关系记录损坏或超过限制。")
                key = (owner, rid)
                if key in self.relation_index:
                    raise ValueError("DOCX 关系标识重复。")
                resolved = _target(owner, target) if mode == "Internal" else None
                state = "internal" if resolved in self.parts else "missing_target"
                if mode == "External":
                    state = "external_not_fetched"
                elif mode != "Internal" or resolved is None:
                    state = "unsafe_target"
                record = {
                    "id": rid,
                    "source_part": owner,
                    "type": kind,
                    "target": target,
                    "target_mode": mode,
                    "target_part": resolved,
                    "status": state,
                    "locator": self.locator(name, node),
                }
                if resolved in self.part_hashes:
                    record["target_hash"] = self.part_hashes[resolved]
                self.relationships.append(record)
                self.relation_index[key] = record
                if len(self.relationships) > MAX_OBJECTS:
                    raise ValueError("DOCX 关系数量超过处理限制。")
                suffix = kind.removeprefix(R + "/")
                metadata = suffix in _METADATA_RELATIONS or kind.endswith("/metadata/core-properties")
                if state in {"missing_target", "unsafe_target"}:
                    self.pending(
                        "unresolved_relationship", record["locator"], relationship_id=rid, status=state
                    )
                elif state == "external_not_fetched" and suffix != "hyperlink":
                    self.pending("external_content_not_fetched", record["locator"], relationship_id=rid)
                elif not metadata and suffix not in _CONTENT_RELATIONS | {"hyperlink", "image"}:
                    self.pending(
                        "unmapped_relationship_type", record["locator"], relationship_id=rid, type=kind
                    )
                if state == "internal" and (metadata or suffix in _CONTENT_RELATIONS):
                    target_tree = self.trees.get(resolved)
                    if target_tree is None or (metadata and target_tree[0].tag not in _METADATA_ROOTS):
                        self.pending(
                            "unmapped_typed_relationship_target", record["locator"], relationship_id=rid
                        )
                    elif suffix in _CONTENT_RELATIONS and target_tree[0].tag != _w(_CONTENT_ROOTS[suffix]):
                        self.pending(
                            "unmapped_typed_relationship_target", record["locator"], relationship_id=rid
                        )

    def emit(self, key, kind, text, locator, metadata=None):
        if len(self.elements) >= MAX_ELEMENTS:
            raise ValueError("DOCX 元素数量超过限制。")
        self.text_size += len(text)
        if self.text_size > MAX_TEXT:
            raise ValueError("DOCX 正文超过处理限制。")
        self.elements.append(
            ParsedElement(
                key,
                kind,
                len(self.elements),
                text,
                locator,
                list(self.headings),
                metadata_json=metadata or {},
            )
        )

    def style(self, identifier, locator, *, default_kind=None):
        """Map applied style ancestry without executing numbering or fields."""
        identifier = identifier or self.default_styles.get(default_kind, "")
        level, visited = None, set()
        while identifier:
            if identifier in visited or len(visited) > 64:
                self.pending("cyclic_or_excessive_style_ancestry", locator, style_id=identifier)
                break
            visited.add(identifier)
            definition = self.styles.get(identifier)
            if definition is None:
                if not re.fullmatch(r"(?:heading|标题)\s*[1-9]", identifier, re.I):
                    self.pending("unresolved_style_reference", locator, style_id=identifier)
                break
            part, node = definition
            if level is None:
                outline = node.find("w:pPr/w:outlineLvl", NS)
                if outline is not None:
                    try:
                        value = int(outline.get(_w("val"), ""))
                        level = value + 1 if 0 <= value <= 8 else 0 if value == 9 else None
                    except ValueError:
                        self.pending("invalid_outline_level", self.locator(part, outline))
                name = node.find("w:name", NS)
                heading = re.fullmatch(
                    r"(?:heading|标题)\s*([1-9])",
                    name.get(_w("val"), "") if name is not None else identifier,
                    re.I,
                )
                if level is None and heading:
                    level = int(heading[1])
            for property_node in node.iter():
                property_name = _local(property_node.tag)
                active = property_node.get(_w("val"), "").casefold() not in {"0", "false", "off"}
                symbolic_font = property_node.tag == _w("rFonts") and any(
                    re.search(r"symbol|wingdings|webdings", value, re.I)
                    for value in property_node.attrib.values()
                )
                if (
                    property_node.tag.startswith(f"{{{W}}}")
                    and property_name in _RENDER_PROPERTIES
                    and active
                ) or symbolic_font:
                    self.pending(
                        "style_requires_rendering",
                        locator,
                        style_id=identifier,
                        style_property=property_name,
                        source_style_locator=self.locator(part, property_node),
                    )
            based = node.find("w:basedOn", NS)
            identifier = based.get(_w("val"), "") if based is not None else ""
        return level

    def inline(self, paragraph, part):
        pieces, stack = [], [paragraph]
        while stack:
            node = stack.pop()
            if node is not paragraph and node.tag == _w("p"):
                continue  # A textbox owns its own paragraph; do not duplicate it.
            local = _local(node.tag)
            if node.tag in {_w("t"), _w("delText")}:
                text = node.text or ""
                self.consumed_text.add(node)
                pieces.append(text)
                if "\ufffd" in text:
                    self.pending("native_decode_replacement", self.locator(part, node))
            elif node.tag == _w("tab"):
                pieces.append("\t")
            elif node.tag == _w("ptab"):
                pieces.append("\t")
                self.pending(
                    "rendering_dependent_tab", self.locator(part, node), attributes=dict(node.attrib)
                )
            elif node.tag in {_w("br"), _w("cr")}:
                pieces.append("\n")
            elif node.tag in {_w("noBreakHyphen"), _w("softHyphen")}:
                pieces.append("\u2011" if local == "noBreakHyphen" else "\u00ad")
            elif node.tag == _w("sym"):
                self.pending(
                    "font_symbol_requires_rendering", self.locator(part, node), attributes=dict(node.attrib)
                )
            elif node.tag in {_w("instrText"), _w("delInstrText"), _w("fldChar"), _w("fldSimple")}:
                self.consumed_text.add(node)
                self.pending(
                    "unevaluated_field",
                    self.locator(part, node),
                    instruction=node.text or node.get(_w("instr"), ""),
                    attributes=dict(node.attrib),
                )
            elif local in _REVISION_TAGS and node.tag.startswith(f"{{{W}}}"):
                self.pending("unresolved_tracked_revision", self.locator(part, node), revision_kind=local)
            elif local in _RENDER_PROPERTIES and node.tag.startswith(f"{{{W}}}"):
                disabled = local in {"vanish", "webHidden", "rtl", "bidi"} and node.get(
                    _w("val"), ""
                ).casefold() in {"0", "false", "off"}
                if not disabled:
                    self.pending("rendering_dependent_text", self.locator(part, node), property=local)
            elif node.tag == _w("rFonts") and any(
                re.search(r"symbol|wingdings|webdings", value, re.I) for value in node.attrib.values()
            ):
                self.pending(
                    "font_symbol_requires_rendering", self.locator(part, node), attributes=dict(node.attrib)
                )
            elif node.tag == _w("rStyle"):
                self.style(node.get(_w("val"), ""), self.locator(part, node), default_kind="character")
            stack.extend(reversed(list(node)))
        return "".join(pieces)

    def paragraph(self, node, part, context):
        self.paragraph_count += 1
        if self.paragraph_count > MAX_ELEMENTS:
            raise ValueError("DOCX 段落数量超过限制。")
        index = self.paragraph_count
        text = self.inline(node, part)
        style = node.find("w:pPr/w:pStyle", NS)
        style_id = style.get(_w("val"), "") if style is not None else ""
        outline = node.find("w:pPr/w:outlineLvl", NS)
        heading = re.fullmatch(r"(?:heading|标题)\s*([1-9])", style_id, re.I)
        level = int(heading[1]) if heading else None
        inherited_level = self.style(style_id, self.locator(part, node), default_kind="paragraph")
        self.style("", self.locator(part, node), default_kind="character")
        if inherited_level is not None:
            level = inherited_level
        if outline is not None:
            try:
                value = int(outline.get(_w("val"), ""))
                level = value + 1 if 0 <= value <= 8 else None
            except ValueError:
                self.pending("invalid_outline_level", self.locator(part, outline))
        kind = "table" if "table" in context else "paragraph"
        if level and "table" not in context and text.strip():
            self.headings = self.headings[: level - 1] + [text.strip()]
            kind = "heading"
        locator = self.locator(
            part,
            node,
            paragraph_start=index,
            paragraph_end=index,
            section_path=list(self.headings),
            **context,
        )
        metadata = {"style_id": style_id, "native_text": text}
        self.paragraphs.append({"paragraph": index, "text": text, "locator": locator})
        # Empty native paragraphs are real structural evidence, never fake body text.
        self.emit(f"paragraph-{index:06}", kind, text, locator, metadata)

    def object(self, node, part, context):
        if len(self.objects) >= MAX_OBJECTS:
            raise ValueError("DOCX 对象数量超过处理限制。")
        index = len(self.objects) + 1
        locator = self.locator(part, node, **context)
        refs = []
        for child in node.iter():
            for attr, rid in child.attrib.items():
                if attr in {f"{{{R}}}id", f"{{{R}}}embed", f"{{{R}}}link"}:
                    relationship = self.relation_index.get((part, rid))
                    ref = {
                        "relationship_id": rid,
                        "attribute": attr,
                        "xml_path": self.trees[part][1][child],
                        "status": relationship["status"] if relationship else "missing_relationship",
                    }
                    if relationship:
                        ref.update(
                            {
                                key: relationship[key]
                                for key in ("target", "target_part", "target_mode", "type")
                            }
                        )
                        if "target_hash" in relationship:
                            ref["target_hash"] = relationship["target_hash"]
                        target = relationship["target_part"]
                        if relationship["type"] == R + "/image" and target in self.parts:
                            if len(self.assets) >= MAX_OBJECTS:
                                raise ValueError("DOCX 图像映射数量超过处理限制。")
                            asset = {
                                "asset_key": f"docx-image-{len(self.assets) + 1:06}",
                                "package_part": target,
                                "image_hash": self.part_hashes[target],
                                "size": len(self.parts[target]),
                                "relationship_id": rid,
                                "source_part": part,
                                "locator": locator,
                                **_image(self.parts[target]),
                            }
                            self.assets.append(asset)
                            ref["asset_key"] = asset["asset_key"]
                    refs.append(ref)
                    if len(refs) > MAX_OBJECTS:
                        raise ValueError("DOCX 对象关系数量超过处理限制。")
        record = {
            "object_key": f"docx-object-{index:06}",
            "object_type": _local(node.tag),
            "locator": locator,
            "relationships": refs,
            "xml_hash": hashlib.sha256(ET.tostring(node, encoding="utf-8")).hexdigest(),
            "xml_hash_basis": "ElementTree serialization; exact original part is identified separately",
            "status": "pending_review",
        }
        self.objects.append(record)
        self.pending(
            "unresolved_document_object",
            locator,
            object_key=record["object_key"],
            object_type=record["object_type"],
        )
        self.emit(
            record["object_key"],
            "figure",
            "",
            {
                **locator,
                "paragraph_start": self.paragraph_count or 1,
                "paragraph_end": self.paragraph_count or 1,
                "section_path": list(self.headings),
            },
            record,
        )

    def table(self, node, part, context):
        number = len(self.tables) + 1
        if number > MAX_OBJECTS:
            raise ValueError("DOCX 表格数量超过处理限制。")
        grid = node.find("w:tblGrid", NS)
        columns = len(grid.findall("w:gridCol", NS)) if grid is not None else 0
        rows = node.findall("w:tr", NS)
        locator = self.locator(part, node, **context)
        record = {
            "table": number,
            "locator": locator,
            "column_count": columns,
            "row_count": len(rows),
            "cells": [],
        }
        self.tables.append(record)
        if not columns or columns > 1024 or len(rows) > MAX_ELEMENTS:
            self.pending("unverified_table_grid", locator)
        if "table" in context:
            self.pending("nested_table", locator)
        active = {}
        for row_number, row in enumerate(rows, start=1):
            before = row.find("w:trPr/w:gridBefore", NS)
            after = row.find("w:trPr/w:gridAfter", NS)
            column, continuation = 1, {}
            if before is not None or after is not None:
                self.pending("omitted_table_cells", self.locator(part, row))
                if before is not None:
                    try:
                        column += max(0, int(before.get(_w("val"), "0")))
                    except ValueError:
                        self.pending("invalid_table_span", self.locator(part, before))
            direct_cells = row.findall("w:tc", NS)
            if any(child.tag not in {_w("tc"), _w("trPr")} for child in row):
                self.pending("wrapped_table_cells", self.locator(part, row))
            for cell in direct_cells:
                self.cell_count += 1
                if self.cell_count > MAX_ELEMENTS:
                    raise ValueError("DOCX 表格单元格数量超过处理限制。")
                span = cell.find("w:tcPr/w:gridSpan", NS)
                try:
                    width = int(span.get(_w("val"), "1")) if span is not None else 1
                    if not 1 <= width <= 1024:
                        raise ValueError
                except ValueError:
                    width = 1
                    self.pending("invalid_table_span", self.locator(part, cell))
                merge = cell.find("w:tcPr/w:vMerge", NS)
                merge_kind = merge.get(_w("val"), "continue") if merge is not None else None
                cell_context = {
                    **context,
                    "table": number,
                    "row": row_number,
                    "column_start": column,
                    "column_end": column + width - 1,
                    "column_span": width,
                }
                cell_record = {
                    **cell_context,
                    "locator": self.locator(part, cell, **cell_context),
                    "vertical_merge": merge_kind,
                    "row_span": 1,
                    "paragraphs": [],
                    "text": "",
                }
                if merge_kind == "restart":
                    continuation[(column, width)] = cell_record
                elif merge_kind == "continue":
                    origin = active.get((column, width))
                    if origin is None:
                        self.pending("unmatched_vertical_merge", cell_record["locator"])
                    else:
                        origin["row_span"] += 1
                        cell_record["merged_origin"] = {
                            "row": origin["row"],
                            "column": origin["column_start"],
                        }
                        continuation[(column, width)] = origin
                elif merge_kind is not None:
                    self.pending("invalid_vertical_merge", cell_record["locator"])
                if cell.find("w:tcPr/w:hMerge", NS) is not None:
                    self.pending("legacy_horizontal_merge", cell_record["locator"])
                if (
                    cell.find("w:tcPr/w:tcBorders/w:tl2br", NS) is not None
                    or cell.find("w:tcPr/w:tcBorders/w:tr2bl", NS) is not None
                ):
                    self.pending("diagonal_table_border", cell_record["locator"])
                start = len(self.paragraphs)
                for child in cell:
                    if child.tag != _w("tcPr"):
                        self.walk(child, part, cell_context)
                cell_record["paragraphs"] = [p["paragraph"] for p in self.paragraphs[start:]]
                cell_record["text"] = "\n".join(p["text"] for p in self.paragraphs[start:])
                if merge_kind == "continue" and cell_record["text"].strip():
                    self.pending("vertical_merge_continuation_has_text", cell_record["locator"])
                record["cells"].append(cell_record)
                column += width
            for child in row:
                if child.tag not in {_w("tc"), _w("trPr")}:
                    self.walk(
                        child,
                        part,
                        {
                            **context,
                            "table": number,
                            "row": row_number,
                            "column_mapping": "unresolved_wrapped_cell",
                        },
                    )
            if columns and column - 1 != columns:
                self.pending(
                    "nonrectangular_table",
                    self.locator(part, row),
                    occupied_columns=column - 1,
                    grid_columns=columns,
                )
            active = continuation
        for child in node:
            if child.tag not in {_w("tblPr"), _w("tblGrid"), _w("tr")}:
                self.pending("wrapped_table_rows", self.locator(part, child))
                self.walk(child, part, context)

    def walk(self, node, part, context):
        local = _local(node.tag)
        if node.tag == _w("p"):
            self.paragraph(node, part, context)
            # Inline drawing/textboxes remain separately mapped source objects.
            for child in node:
                self.walk_objects(child, part, context)
            return
        if node.tag == _w("tbl"):
            self.table(node, part, context)
            return
        if local in _OBJECT_TAGS and (
            node.tag.startswith(f"{{{W}}}") or local in {"oMath", "oMathPara", "AlternateContent"}
        ):
            self.object(node, part, context)
        if local in _REVISION_TAGS and node.tag.startswith(f"{{{W}}}"):
            self.pending("unresolved_tracked_revision", self.locator(part, node), revision_kind=local)
        if node.tag == _w("sdt"):
            self.pending("content_control_binding_or_state", self.locator(part, node))
        if node.tag in {_w("footnote"), _w("endnote"), _w("comment")}:
            context = {**context, "note_id": node.get(_w("id")), "note_type": node.get(_w("type"), "normal")}
        for child in node:
            self.walk(child, part, context)

    def walk_objects(self, node, part, context):
        local = _local(node.tag)
        if local in _OBJECT_TAGS:
            self.object(node, part, context)
            # Alternative branches are not chosen; each native textbox stays mapped.
        if node.tag == _w("txbxContent"):
            self.pending("textbox_render_order", self.locator(part, node))
            for child in node:
                self.walk(child, part, {**context, "source_area": "textbox"})
            return
        for child in node:
            self.walk_objects(child, part, context)

    def extract(self):
        self.read_relationships()
        for name, (root, _, _) in self.trees.items():
            if root.tag != _w("styles"):
                continue
            for node in root.findall("w:style", NS):
                identifier = node.get(_w("styleId"))
                if not identifier or identifier in self.styles:
                    self.pending(
                        "duplicate_or_missing_style_identifier", self.locator(name, node), style_id=identifier
                    )
                    continue
                self.styles[identifier] = (name, node)
                if node.get(_w("default"), "").casefold() in {"1", "true", "on"}:
                    style_type = node.get(_w("type"))
                    if style_type in self.default_styles:
                        self.pending(
                            "ambiguous_default_style", self.locator(name, node), style_type=style_type
                        )
                    self.default_styles[style_type] = identifier
        main = self.trees.get("word/document.xml")
        if main is None or main[0].tag != _w("document") or len(main[0].findall("w:body", NS)) != 1:
            raise ValueError("DOCX 正文 XML 缺少 WordprocessingML 文档结构。")
        incoming = {r["target_part"] for r in self.relationships if r["status"] == "internal"}
        native = [
            name
            for name, (root, _, _) in self.trees.items()
            if root.tag.startswith(f"{{{W}}}") and _local(root.tag) in _NATIVE_ROOTS
        ]
        native.sort(key=lambda name: (name != "word/document.xml", name))
        for name in native:
            root = self.trees[name][0]
            area = _local(root.tag)
            self.headings = []
            if name != "word/document.xml" and name not in incoming:
                self.pending("orphan_native_part", self.locator(name, root))
            if area == "comments":
                self.pending("comment_annotations_require_review", self.locator(name, root))
            self.walk(root, name, {"source_area": area})
        note_ids = {kind: set() for kind in ("footnote", "endnote", "comment")}
        for name in native:
            for node in self.trees[name][0].iter():
                if node.tag in {_w(kind) for kind in note_ids}:
                    ids = note_ids[_local(node.tag)]
                    identifier = node.get(_w("id"))
                    if identifier is None or identifier in ids:
                        self.pending(
                            "duplicate_or_missing_note_identifier",
                            self.locator(name, node),
                            note_id=identifier,
                        )
                    ids.add(identifier)
        for name in native:
            for node in self.trees[name][0].iter():
                if node not in self.consumed_text and node.text and node.text.strip():
                    self.pending("unmapped_native_text", self.locator(name, node), native_text=node.text)
                if node.tag in {_w("subDoc"), _w("contentPart")}:
                    self.pending(
                        "unresolved_document_object", self.locator(name, node), object_type=_local(node.tag)
                    )
                for attribute, rid in node.attrib.items():
                    if attribute not in {f"{{{R}}}id", f"{{{R}}}embed", f"{{{R}}}link"}:
                        continue
                    relation = self.relation_index.get((name, rid))
                    ref = {
                        "reference_kind": _local(node.tag),
                        "attribute": attribute,
                        "relationship_id": rid,
                        "locator": self.locator(name, node),
                        "status": relation["status"] if relation else "missing_relationship",
                    }
                    if relation:
                        ref["target_part"] = relation["target_part"]
                    else:
                        self.pending("missing_relationship_reference", ref["locator"], relationship_id=rid)
                    self.references.append(ref)
                kind = {
                    "footnoteReference": "footnote",
                    "endnoteReference": "endnote",
                    "commentReference": "comment",
                }.get(_local(node.tag))
                if kind and node.tag == _w(_local(node.tag)):
                    identifier = node.get(_w("id"))
                    ref = {
                        "reference_kind": kind,
                        "note_id": identifier,
                        "locator": self.locator(name, node),
                        "status": "native_mapped" if identifier in note_ids[kind] else "missing_note",
                    }
                    self.references.append(ref)
                    if ref["status"] == "missing_note":
                        self.pending("unresolved_note_reference", ref["locator"], note_id=identifier)
                if len(self.references) > MAX_OBJECTS:
                    raise ValueError("DOCX 源引用数量超过处理限制。")
        for name, (root, _, _) in self.trees.items():
            if name in native or root.tag in _METADATA_ROOTS or name.endswith(".rels"):
                continue
            self.pending("unmapped_package_xml", self.locator(name, root))
        targets = {r["target_part"] for r in self.relationships if r["status"] == "internal"}
        for name in self.parts:
            if name not in self.trees and name not in targets:
                self.pending("unmapped_binary_part", package_part=name, part_hash=self.part_hashes[name])
        # Image relations need a mapping even when no drawing consumes the rId.
        used_images = {(a["source_part"], a["relationship_id"]) for a in self.assets}
        for relation in self.relationships:
            if (
                relation["type"] == R + "/image"
                and (relation["source_part"], relation["id"]) not in used_images
            ):
                self.pending(
                    "unmapped_image_relationship", relation["locator"], relationship_id=relation["id"]
                )
        return native


def parse_docx(content: bytes, *, filename: str = "document.docx", max_bytes: int = MAX_BYTES) -> dict:
    """Return the existing manifest shape with explicit text-completeness gates."""
    source_hash = hashlib.sha256(content).hexdigest() if isinstance(content, bytes) else ""
    mapping = _Mapping(_package(content, max_bytes=max_bytes), source_hash)
    native_parts = mapping.extract()
    document = ParsedDocument("docx-basic", "2", "docx-package-mapping-v2", mapping.elements, [], [])
    # build_manifest already excludes empty figures and heading-only content. This
    # path preserves source mapping for image-only packages without fake chunks.
    manifest, _ = build_manifest(document, source_hash=source_hash, document_title=Path(filename).stem)
    by_key = {element.element_key: element for element in document.elements}
    chunks = []
    for chunk in manifest["chunks"]:
        members = [by_key[key] for key in chunk["element_keys"] if key in by_key]
        locator = dict(chunk["locator"])
        anchors = [member.locator for member in members]
        locator["source_anchors"] = anchors
        # A merged chunk cannot attribute every span to its first package part.
        if len({member.locator["package_part"] for member in members}) > 1:
            for key in (
                "package_part",
                "part_hash",
                "xml_path",
                "source_area",
                "table",
                "row",
                "column_start",
                "column_end",
                "column_span",
                "note_id",
                "note_type",
            ):
                locator.pop(key, None)
        chunks.append(
            {
                "id": chunk["chunk_key"],
                "text": chunk["content_text"],
                "section_path": members[0].section_path if members else [],
                "locator": locator,
                "retrieval_text": chunk["retrieval_text"],
                "content_hash": chunk["content_hash"],
            }
        )
    if not chunks:
        mapping.pending("no_searchable_native_text")
    if mapping.assets:
        mapping.pending("images_pending_vision", image_count=len(mapping.assets))
    reasons = sorted({item["reason"] for item in mapping.review})
    warnings = [f"DOCX 内容待核验：{reason}" for reason in reasons]
    result = {
        "parser": document.parser_name,
        "parser_version": document.parser_version,
        "pipeline_version": manifest["pipeline_version"],
        "source_hash": source_hash,
        "title": next(
            (e.content_text for e in document.elements if e.kind in {"title", "heading"}), Path(filename).stem
        ),
        "elements": manifest["elements"],
        "chunks": chunks,
        "warnings": warnings,
        "retrieval_mode": "lexical",
        "quality": {
            "complete": not mapping.review and bool(chunks),
            "reason": reasons[0] if reasons else "verified_native_package",
            "review_reasons": reasons,
            "source_format": "docx",
            "physical_pages_known": False,
            "native_parts": len(native_parts),
            "paragraph_count": mapping.paragraph_count,
            "table_count": len(mapping.tables),
            "image_count": len(mapping.assets),
            "object_count": len(mapping.objects),
        },
        "package_parts": [
            {
                "package_part": name,
                "size": len(data),
                "sha256": mapping.part_hashes[name],
                "status": "native_mapped" if name in native_parts else "source_preserved",
            }
            for name, data in mapping.parts.items()
        ],
        "relationships": mapping.relationships,
        "references": mapping.references,
        "tables": mapping.tables,
        "objects": mapping.objects,
        "assets_pending": mapping.assets,
        "review": mapping.review,
    }
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESULT:
        raise ValueError("DOCX 解析结果超过处理限制。")
    return result
