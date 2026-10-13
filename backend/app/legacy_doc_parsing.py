"""Offline legacy text, with separate source and conversion-artifact locators.

Called inside parse_bounded. Word CPs address characters in WordDocument, not
physical pages. Converter XPath/event positions address the immutable derived
artifact; no unproved one-to-one mapping to source CPs is asserted. Layout,
field visibility and unresolved objects keep the whole source out of indexing.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from xlrd.compdoc import CompDoc, CompDocError

from .vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement

PROTOCOL = "legacy-source-artifact-v1"
MAX_BYTES = 20 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
MAX_ARTIFACTS = 12 * 1024 * 1024
MAX_OBJECTS = 8 * 1024 * 1024
MAX_TEXT = 4_000_000
MAX_ELEMENTS = 20_000
MAX_DEPTH = 128
MAX_IMAGES = 100
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 6_000_000
_OLE = bytes.fromhex("d0cf11e0a1b11ae1")
_COMPRESSED = {
    0x82: 0x201A,
    0x83: 0x0192,
    0x84: 0x201E,
    0x85: 0x2026,
    0x86: 0x2020,
    0x87: 0x2021,
    0x88: 0x02C6,
    0x89: 0x2030,
    0x8A: 0x0160,
    0x8B: 0x2039,
    0x8C: 0x0152,
    0x91: 0x2018,
    0x92: 0x2019,
    0x93: 0x201C,
    0x94: 0x201D,
    0x95: 0x2022,
    0x96: 0x2013,
    0x97: 0x2014,
    0x98: 0x02DC,
    0x99: 0x2122,
    0x9A: 0x0161,
    0x9B: 0x203A,
    0x9C: 0x0153,
    0x9F: 0x0178,
}
_SUBDOCUMENTS = (
    ("main", 3),
    ("footnotes", 4),
    ("headers_footers", 5),
    ("comments", 7),
    ("endnotes", 8),
    ("textboxes", 9),
    ("header_textboxes", 10),
)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _trusted_file(value, *, executable=False, limit=16 * 1024 * 1024):
    if not value or not Path(value).is_absolute():
        raise ValueError("旧格式解析需要配置可信工具及运行资料的绝对路径。")
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("旧格式解析工具或运行资料不可用。") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or info.st_uid not in {0, os.getuid()}
        or executable
        and not info.st_mode & stat.S_IXUSR
        or not 0 < info.st_size <= limit
    ):
        raise ValueError("旧格式解析工具及运行资料必须是可信的普通文件。")
    return path, {"name": path.name, "size": info.st_size, "sha256": _digest(path.read_bytes())}


def _trusted_directory(value):
    if not value or not Path(value).is_absolute():
        raise ValueError("旧格式解析需要配置可信运行目录的绝对路径。")
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("旧格式运行目录不可用。") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or info.st_uid not in {0, os.getuid()}
    ):
        raise ValueError("旧格式运行目录必须由服务器管理员管理。")
    return path


def _configuration(kind):
    launcher, launcher_proof = _trusted_file(os.getenv("TEA_LEGACY_CONVERTER_PATH"), executable=True)
    if kind == "doc":
        converter, converter_proof = _trusted_file(os.getenv("TEA_ANTIWORD_PATH"), executable=True)
        support = _trusted_directory(os.getenv("TEA_ANTIWORD_HOME"))
        files = []
        for name in ("UTF-8.txt", "fontnames"):
            _, proof = _trusted_file(support / name, limit=1024 * 1024)
            files.append(proof)
        return (
            launcher,
            [("docbook", converter, support)],
            {
                "launcher": launcher_proof,
                "converters": [converter_proof],
                "runtime_files": files,
            },
        )
    support = _trusted_directory(os.getenv("TEA_WPD_LIBRARY_DIR"))
    converters, proofs = [], []
    for mode, variable in (("wpf-html", "TEA_WPD_HTML_PATH"), ("wpf-raw", "TEA_WPD_RAW_PATH")):
        converter, proof = _trusted_file(os.getenv(variable), executable=True)
        converters.append((mode, converter, support))
        proofs.append(proof)
    files = []
    for path in sorted(support.glob("*.so*")):
        # Package symlinks must remain within this reviewed runtime directory.
        resolved = path.resolve(strict=True)
        if resolved.parent != support.resolve():
            raise ValueError("旧格式共享库不能指向运行目录之外。")
        _, proof = _trusted_file(resolved)
        if proof not in files:
            files.append(proof)
    if not files or len(files) > 32:
        raise ValueError("旧格式共享库缺失或超过运行限制。")
    return (
        launcher,
        converters,
        {
            "launcher": launcher_proof,
            "converters": proofs,
            "runtime_files": files,
            "system_abi": "operator_review_required",
        },
    )


def _run_converter(content, launcher, converter, support, mode, *, timeout=18):
    """Bound pipes and reap the owned group; native launcher protects parent death."""
    if mode not in {"docbook", "wpf-html", "wpf-raw"} or not 0 < timeout <= 20:
        raise ValueError("旧格式转换方式或超时设置无效。")
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    with tempfile.TemporaryDirectory(prefix="aitea-legacy-doc-") as directory:
        original = Path(directory) / "original"
        original.write_bytes(content)
        original.chmod(0o400)
        process = subprocess.Popen(
            [str(launcher), str(os.getpid()), mode, str(converter), str(support), str(original)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + timeout
        try:
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("旧格式解析超时，原件已保留。")
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        outputs[key.data].extend(chunk)
                        if len(outputs[key.data]) > (MAX_OUTPUT if key.data == "stdout" else 65536):
                            raise ValueError("旧格式转换输出超过限制。")
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code or outputs["stderr"]:
                raise ValueError("旧格式转换不完整或离线隔离不可用，原件已保留。")
            return bytes(outputs["stdout"])
        except subprocess.TimeoutExpired as exc:
            raise ValueError("旧格式转换退出超时，原件已保留。") from exc
        finally:
            selector.close()
            # This is a newly owned process group. Native launcher disallows
            # fork/clone; cleanup also covers a misconfigured operator helper.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


def _artifact(raw, kind):
    if not raw or len(raw) > MAX_OUTPUT:
        raise ValueError("旧格式转换没有输出或超过限制。")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValueError("旧格式转换未完整解码 UTF-8。") from exc
    if "\ufffd" in text or "\x00" in text:
        raise ValueError("旧格式转换含不完整字符，原件已保留。")
    return text, {
        "kind": kind,
        "sha256": _digest(raw),
        "size": len(raw),
        "encoding": "utf-8",
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "source_mapping": "unverified",
    }


def _fib_piece_map(word, table, *, source_hash, table_name):
    """MS-DOC Fib97/CLX/Pcd source ranges; no layout/visibility inference."""
    if len(word) < 154 or struct.unpack_from("<HH", word) != (0xA5EC, 0xC1):
        raise ValueError("仅支持已核验的 Word 97 FIB 结构。")
    flags = struct.unpack_from("<H", word, 10)[0]
    if flags & (0x100 | 0x8000):
        raise ValueError("不支持加密或混淆 DOC，原件已保留。")
    if struct.unpack_from("<H", word, 32)[0] != 14 or struct.unpack_from("<H", word, 62)[0] != 22:
        raise ValueError("DOC FIB 固定结构损坏。")
    cbmac = struct.unpack_from("<I", word, 64)[0]
    if not 154 <= cbmac <= len(word):
        raise ValueError("DOC WordDocument 有效字节边界无效。")
    pair_count = struct.unpack_from("<H", word, 152)[0]
    if not 93 <= pair_count <= 183 or 154 + pair_count * 8 > cbmac:
        raise ValueError("DOC FIB 流位置表不完整。")
    counts = struct.unpack_from("<22i", word, 64)
    sections, cp = [], 0
    for name, index in _SUBDOCUMENTS:
        count = counts[index]
        if not 0 <= count <= MAX_TEXT:
            raise ValueError("DOC 子文档字符数无效或超过限制。")
        sections.append({"kind": name, "cp_start": cp, "cp_end": cp + count, "cp_count": count})
        cp += count
    if not 0 < cp <= MAX_TEXT or counts[6] != 0:
        raise ValueError("DOC 子文档计数无效。")
    total_cp = cp + (1 if any(counts[index] for _, index in _SUBDOCUMENTS[1:]) else 0)
    if total_cp > cp:
        sections.append({"kind": "terminal", "cp_start": cp, "cp_end": total_cp, "cp_count": 1})
    clx_offset, clx_length = struct.unpack_from("<II", word, 154 + 33 * 8)
    if not 0 < clx_length <= len(table) or clx_offset + clx_length > len(table):
        raise ValueError("DOC CLX 越过所选 Table 流。")
    clx = table[clx_offset : clx_offset + clx_length]
    offset, prcs = 0, []
    while offset < len(clx) and clx[offset] == 1:
        if offset + 3 > len(clx):
            raise ValueError("DOC CLX 属性记录不完整。")
        size = struct.unpack_from("<H", clx, offset + 1)[0]
        if offset + 3 + size > len(clx):
            raise ValueError("DOC CLX 属性记录越界。")
        prcs.append(
            {
                "stream_offset": clx_offset + offset,
                "length": 3 + size,
                "sha256": _digest(clx[offset : offset + 3 + size]),
            }
        )
        offset += 3 + size
    if offset + 5 > len(clx) or clx[offset] != 2:
        raise ValueError("DOC 缺少完整 Pcdt 字符位置表。")
    size = struct.unpack_from("<I", clx, offset + 1)[0]
    if size < 16 or (size - 4) % 12 or offset + 5 + size != len(clx):
        raise ValueError("DOC PlcPcd 大小或结束边界无效。")
    plc = clx[offset + 5 :]
    n = (size - 4) // 12
    if n > MAX_ELEMENTS:
        raise ValueError("DOC 字符片段数超过限制。")
    cps = struct.unpack_from(f"<{n + 1}I", plc)
    if cps[0] != 0 or cps[-1] != total_cp or any(a >= b for a, b in zip(cps, cps[1:])):
        raise ValueError("DOC 字符位置范围重复、不完整或与 FIB 不一致。")
    pieces, controls = [], []
    for index, (first, last) in enumerate(zip(cps, cps[1:])):
        record_offset = (n + 1) * 4 + index * 8
        pcd_flags, fc, prm = struct.unpack_from("<HIH", plc, record_offset)
        compressed, location = bool(fc & 0x40000000), fc & 0x3FFFFFFF
        if fc & 0x80000000 or pcd_flags & 4 or compressed and location % 2:
            raise ValueError("DOC Pcd 字符位置或保留位无效。")
        location = location // 2 if compressed else location
        length = (last - first) * (1 if compressed else 2)
        if not 0 <= location <= location + length <= cbmac:
            raise ValueError("DOC 字符片段越过 WordDocument 有效边界。")
        raw = word[location : location + length]
        try:
            text = (
                "".join(chr(_COMPRESSED.get(byte, byte)) for byte in raw)
                if compressed
                else raw.decode("utf-16-le", "strict")
            )
            decode_state = "complete" if "\ufffd" not in text else "replacement_pending"
        except UnicodeError:
            text, decode_state = None, "strict_decode_pending"
        ref = {
            "kind": "compound_stream",
            "source_hash": source_hash,
            "stream": "WordDocument",
            "cp_start": first,
            "cp_end": last,
            "stream_offset": location,
            "length": length,
            "sha256": _digest(raw),
            "encoding": "ms-doc-compressed" if compressed else "utf-16-le",
        }
        pieces.append(
            {
                **ref,
                "text": text,
                "decode_state": decode_state,
                "prm": prm,
                "pcd_reference": {
                    "stream": table_name,
                    "stream_offset": clx_offset + offset + 5 + record_offset,
                    "length": 8,
                    "sha256": _digest(plc[record_offset : record_offset + 8]),
                },
            }
        )
        # Inspect original code units so CP offsets remain correct for astral
        # characters. These control markers are not claimed to be image bytes.
        units = raw if compressed else struct.unpack(f"<{len(raw) // 2}H", raw)
        for i, unit in enumerate(units):
            if unit in {1, 2, 5, 8, 12, 19, 20, 21}:
                controls.append(
                    {
                        "cp": first + i,
                        "code_unit": unit,
                        "stream_offset": location + i * (1 if compressed else 2),
                        "meaning": "unresolved_control_or_field",
                        "source_hash": source_hash,
                    }
                )
                if len(controls) > MAX_ELEMENTS:
                    raise ValueError("DOC 字段或对象标记数超过限制。")
    return {
        "nFib": 193,
        "fHasPic": bool(flags & 8),
        "table_stream": table_name,
        "cp_count": total_cp,
        "subdocuments": sections,
        "pieces": pieces,
        "control_markers": controls,
        "property_records": prcs,
        "layout_visibility_mapping": "unverified",
        "physical_pages": False,
    }


def _doc_source(content):
    if not content.startswith(_OLE):
        raise ValueError("DOC 不是完整的 OLE 文件。")
    log = io.StringIO()
    try:
        container = CompDoc(content, logfile=log)
        if not 1 <= len(container.dirlist) <= 2000:
            raise ValueError("DOC OLE 目录数超过限制。")
        streams, mappings, objects = {}, [], []
        object_bytes = 0
        source_hash = _digest(content)
        for node in container.dirlist:
            if not node.name or node.etype == 5:
                continue
            path, active = [node.name], node.parent
            visited = {node.DID}
            while active > 0:
                if active in visited or len(path) > MAX_DEPTH or active >= len(container.dirlist):
                    raise ValueError("DOC OLE 目录循环或越界。")
                visited.add(active)
                parent = container.dirlist[active]
                path.insert(0, parent.name)
                active = parent.parent
            ref = {
                "kind": "compound_stream",
                "source_hash": source_hash,
                "directory_id": node.DID,
                "parent_directory_id": node.parent,
                "path": path,
                "entry_type": node.etype,
            }
            if any("/" in item or "\ufffd" in item for item in path):
                raise ValueError("DOC OLE 流名称不能准确定位。")
            if node.etype == 2:
                if not 0 <= node.tot_size <= MAX_BYTES:
                    raise ValueError("DOC OLE 流大小无效。")
                if active < 0:
                    mappings.append({**ref, "declared_size": node.tot_size, "state": "orphan_unmapped"})
                    continue
                key = "/".join(path)
                if key.casefold() in {value.casefold() for value in streams}:
                    raise ValueError("DOC OLE 流名称重复。")
                raw = container.get_named_stream(key)
                if raw is None or len(raw) != node.tot_size:
                    raise ValueError("DOC OLE 流未完整提取。")
                streams[key] = raw
                ref = {**ref, "stream_offset": 0, "length": len(raw), "sha256": _digest(raw)}
                if key not in {"WordDocument", "0Table", "1Table"}:
                    object_bytes += len(raw)
                    if object_bytes > MAX_OBJECTS:
                        raise ValueError("DOC 待核验对象字节超过限制。")
                    objects.append(
                        {
                            **ref,
                            "storage_base64": base64.b64encode(raw).decode("ascii"),
                            "mime_type": "application/octet-stream",
                            "state": "object_mapping_pending",
                            "image_bytes_verified": False,
                        }
                    )
            mappings.append(ref)
        word = streams.get("WordDocument")
        if word is None or len(word) < 12:
            raise ValueError("DOC 缺少主 WordDocument 流。")
        table_name = "1Table" if struct.unpack_from("<H", word, 10)[0] & 0x200 else "0Table"
        if table_name not in streams:
            raise ValueError("DOC 缺少 FIB 指定的 Table 流。")
        source = _fib_piece_map(word, streams[table_name], source_hash=source_hash, table_name=table_name)
        return {**source, "streams": mappings, "objects": objects, "container_warning": bool(log.getvalue())}
    except (CompDocError, IndexError, struct.error, UnicodeError, RecursionError) as exc:
        raise ValueError("DOC OLE/FIB 结构损坏，原件已保留。") from exc


def _locator(source_hash, artifact, path):
    return {
        "kind": "conversion_artifact",
        "source_hash": source_hash,
        "artifact_sha256": artifact["sha256"],
        "artifact_kind": artifact["kind"],
        "xpath": path,
        "source_mapping": "unverified",
        "physical_pages": False,
    }


def _artifact_text_coverage(elements, expected):
    # This checks coverage of one immutable conversion artifact. It is not
    # independent character evidence about the original DOC/WPF source.
    expected_inventory = Counter(
        character for text in expected for character in text if not character.isspace()
    )
    actual_inventory = Counter(
        character for element in elements for character in element.content_text if not character.isspace()
    )
    if actual_inventory != expected_inventory:
        raise ValueError("旧格式转换资料存在未完整映射或重复的正文字符。")
    return {
        "artifact_non_whitespace_characters": sum(expected_inventory.values()),
        "artifact_text_coverage": "complete",
        "independent_source_character_evidence": False,
    }


def _docbook_elements(raw, source_hash):
    text, artifact = _artifact(raw, "antiword-docbook-xml")
    # ElementTree does not fetch the converter's external DocBook DTD. Reject
    # internal subsets/entities before parsing, including small expansion bombs.
    if re.search(r"<!ENTITY|<!DOCTYPE[^>]*\[", text, re.I):
        raise ValueError("DOC 转换 XML 包含不支持的实体定义。")
    try:
        tree = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError("DOC 转换 XML 未完整结束。") from exc
    if tree.tag != "book":
        raise ValueError("DOC 转换 XML 根结构无效。")
    elements, nodes, breaks, tables, images = [], [], [], [], []
    text_count = 0

    def emit(node, path, kind, body):
        nonlocal text_count
        if not body.strip():
            return
        text_count += len(body)
        if text_count > MAX_TEXT or len(elements) >= MAX_ELEMENTS:
            raise ValueError("DOC 转换正文超过限制。")
        elements.append(
            ParsedElement(
                f"legacy-{len(elements)}",
                kind,
                len(elements),
                body,
                _locator(source_hash, artifact, path),
                [],
                metadata_json={"artifact_tag": node.tag, "source_cp_mapping": "unverified"},
            )
        )

    def visit(node, path, depth, consumed=False, metadata=False):
        if depth > MAX_DEPTH or len(nodes) >= MAX_ELEMENTS:
            raise ValueError("DOC 转换 XML 深度或元素数超过限制。")
        nodes.append({"xpath": path, "tag": node.tag, "attributes": dict(node.attrib)})
        metadata = metadata or node.tag == "bookinfo"
        if node.tag == "beginpage":
            breaks.append(
                {
                    "kind": "declared_break",
                    "locator": _locator(source_hash, artifact, path),
                    "physical_page_verified": False,
                }
            )
        if node.tag in {"imagedata", "graphic"}:
            # A derived reference is not proof of an extracted original image.
            images.append(
                _inline_image(node.attrib.get("fileref") or "", _locator(source_hash, artifact, path))
            )
            if len(images) > MAX_IMAGES or sum(item.get("size", 0) for item in images) > MAX_OBJECTS:
                raise ValueError("DOC 图片数量或总字节超过限制。")
        if node.tag in {"informaltable", "table"} and not metadata:
            rows = []
            for row_index, row in enumerate(node.iter("row")):
                cells = []
                for col_index, cell in enumerate(row.findall("entry")):
                    cells.append(
                        {
                            "row": row_index + 1,
                            "column": col_index + 1,
                            "text": "".join(cell.itertext()),
                            "attributes": dict(cell.attrib),
                        }
                    )
                rows.append(cells)
            tables.append(
                {
                    "xpath": path,
                    "rows": rows,
                    "structure_source": "conversion_artifact",
                    "original_merge_geometry_verified": False,
                    "artifact_text": "".join(node.itertext()),
                    "text_in_ancestor_element": consumed,
                }
            )
            # Real antiword emits informaltable inside para. In that case the
            # ancestor already retains all text in source order; map structure
            # independently without duplicating it into another body element.
            if not consumed:
                body = "\n".join("\t".join(cell["text"] for cell in row) for row in rows)

                def outside_cells(part):
                    if part.tag == "entry":
                        return ""
                    return (part.text or "") + "".join(
                        outside_cells(child) + (child.tail or "") for child in part
                    )

                caption_text = outside_cells(node).strip()
                if caption_text:
                    body = caption_text + "\n" + body
                emit(node, path, "table", body or "".join(node.itertext()))
            consumed = True
        elif not consumed and not metadata:
            if node.tag in {"para", "simpara", "literallayout", "programlisting", "title"}:
                emit(node, path, "heading" if node.tag == "title" else "paragraph", "".join(node.itertext()))
                consumed = True
            elif node.text and node.text.strip():
                emit(node, path, "paragraph", node.text)
        sibling_counts = Counter()
        for child in node:
            sibling_counts[child.tag] += 1
            child_path = f"{path}/{child.tag}[{sibling_counts[child.tag]}]"
            visit(child, child_path, depth + 1, consumed=consumed, metadata=metadata)
            if not consumed and not metadata and child.tail and child.tail.strip():
                emit(node, path, "paragraph", child.tail)

    visit(tree, "/book[1]", 0)

    def body_text(node):
        if node.tag == "bookinfo":
            return []
        return [node.text or ""] + [text for child in node for text in (*body_text(child), child.tail or "")]

    coverage = _artifact_text_coverage(elements, body_text(tree))
    return elements, {
        "artifact": artifact,
        "nodes": nodes,
        "declared_breaks": breaks,
        "tables": tables,
        "images": images,
        **coverage,
    }


@dataclass
class _Node:
    tag: str
    attributes: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


class _HTMLTree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > MAX_ELEMENTS or len(self.stack) > MAX_DEPTH:
            raise ValueError("WPF 转换 HTML 深度或节点数超过限制。")
        if len(attrs) > 128 or len(dict(attrs)) != len(attrs):
            raise ValueError("WPF 转换 HTML 属性无效。")
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if len(self.stack) <= 1 or self.stack[-1].tag != tag:
            raise ValueError("WPF 转换 HTML 结构未完整配对。")
        self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _html_text(node):
    return "".join(
        child if isinstance(child, str) else "\n" if child.tag == "br" else _html_text(child)
        for child in node.children
    )


def _html_table(node, path, consumed):
    def table_rows(part):
        if part is not node and part.tag == "table":
            return []
        rows = [part] if part.tag == "tr" else []
        return rows + [
            row for child in part.children if isinstance(child, _Node) for row in table_rows(child)
        ]

    rows = []
    for row_index, row in enumerate(table_rows(node), 1):
        rows.append(
            [
                {
                    "row": row_index,
                    "column": column_index,
                    "text": _html_text(cell),
                    "attributes": cell.attributes,
                }
                for column_index, cell in enumerate(
                    (
                        child
                        for child in row.children
                        if isinstance(child, _Node) and child.tag in {"td", "th"}
                    ),
                    1,
                )
            ]
        )
    return {
        "xpath": path,
        "rows": rows,
        "artifact_text": _html_text(node),
        "original_merge_geometry_verified": False,
        "text_in_ancestor_element": consumed,
    }


def _inline_image(uri, locator):
    match = re.fullmatch(r"data:(image/(?:png|jpeg|gif));base64,([A-Za-z0-9+/]*={0,2})", uri)
    if not match or len(match[2]) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        return {
            "locator": locator,
            "state": "image_reference_pending",
            "image_bytes_verified": False,
            "reference_sha256": _digest(uri.encode("utf-8")),
            "reference": uri[:2048],
        }
    try:
        raw = base64.b64decode(match[2], validate=True)
    except ValueError as exc:
        raise ValueError("WPF 内嵌图片 Base64 无效。") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES or base64.b64encode(raw).decode("ascii") != match[2]:
        raise ValueError("WPF 内嵌图片字节不完整或超过限制。")
    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/gif": b"GIF8"}
    if not raw.startswith(signatures[match[1]]):
        raise ValueError("WPF 图片签名与类型不一致。")
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if not 0 < width <= 10000 or not 0 < height <= 10000 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("WPF 图片像素超过限制。")
            if image.format != {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF"}[match[1]]:
                raise ValueError("WPF 图片解码格式不一致。")
            frames = getattr(image, "n_frames", 1)
            if not 1 <= frames <= 100 or width * height * frames > MAX_IMAGE_PIXELS:
                raise ValueError("WPF 图片帧数或总像素超过限制。")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            for frame in range(frames):
                image.seek(frame)
                image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("WPF 图片解码失败。") from exc
    return {
        "locator": locator,
        "state": "vision_pending",
        "image_bytes_verified": True,
        "mime_type": match[1],
        "image_sha256": _digest(raw),
        "content_base64": match[2],
        "size": len(raw),
        "width": width,
        "height": height,
        "frame_count": frames,
        "original_object_mapping": "unverified",
    }


def _wpf_artifacts(html_raw, events_raw, source_hash):
    html_text, html_artifact = _artifact(html_raw, "libwpd-html")
    events_text, events_artifact = _artifact(events_raw, "libwpd-events")
    if len(html_raw) + len(events_raw) > MAX_ARTIFACTS:
        raise ValueError("WPF 转换资料总大小超过限制。")
    parser = _HTMLTree()
    parser.feed(html_text)
    parser.close()
    if len(parser.stack) != 1:
        raise ValueError("WPF 转换 HTML 未完整结束。")
    elements, nodes, images, image_bytes, tables = [], [], [], 0, []

    def visit(node, path, depth, body=False, consumed=False):
        nonlocal image_bytes
        if depth > MAX_DEPTH:
            raise ValueError("WPF 转换 HTML 深度超过限制。")
        body = body or node.tag == "body"
        nodes.append({"xpath": path, "tag": node.tag, "attributes": node.attributes})
        locator = _locator(source_hash, html_artifact, path)
        if body and node.tag == "img":
            image = _inline_image(node.attributes.get("src") or "", locator)
            image_bytes += image.get("size", 0)
            images.append(image)
            if len(images) > MAX_IMAGES or image_bytes > MAX_OBJECTS:
                raise ValueError("WPF 图片数量或总字节超过限制。")
        if body and node.tag == "table":
            tables.append(_html_table(node, path, consumed))
        if (
            body
            and not consumed
            and node.tag
            in {"p", "table", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "header", "footer"}
        ):
            text = _html_text(node)
            if text.strip():
                kind = (
                    "table"
                    if node.tag == "table"
                    else "heading"
                    if node.tag.startswith("h") and node.tag[1:].isdigit()
                    else "paragraph"
                )
                elements.append(
                    ParsedElement(
                        f"legacy-{len(elements)}",
                        kind,
                        len(elements),
                        text,
                        locator,
                        [],
                        metadata_json={"artifact_tag": node.tag, "source_record_mapping": "unverified"},
                    )
                )
            consumed = True
        elif body and not consumed:
            direct_text = "".join(child for child in node.children if isinstance(child, str))
            if direct_text.strip():
                elements.append(
                    ParsedElement(
                        f"legacy-{len(elements)}",
                        "paragraph",
                        len(elements),
                        direct_text,
                        locator,
                        [],
                        metadata_json={"artifact_tag": node.tag},
                    )
                )
        counts = Counter()
        for child in node.children:
            if isinstance(child, _Node):
                counts[child.tag] += 1
                visit(child, f"{path}/{child.tag}[{counts[child.tag]}]", depth + 1, body, consumed)

    visit(parser.root, "/document[1]", 0)

    def body_nodes(node):
        if node.tag == "body":
            return [_html_text(node)]
        return [text for child in node.children if isinstance(child, _Node) for text in body_nodes(child)]

    coverage = _artifact_text_coverage(elements, body_nodes(parser.root))
    lines, offset = [], 0
    for ordinal, line in enumerate(events_raw.splitlines(keepends=True), 1):
        if ordinal > MAX_ELEMENTS or len(line) > 65536:
            raise ValueError("WPF 转换事件数量或单行大小超过限制。")
        value = line.decode("utf-8", "strict").rstrip("\r\n")
        lines.append(
            {
                "ordinal": ordinal,
                "artifact_byte_offset": offset,
                "length": len(line),
                "sha256": _digest(line),
                "event": value,
            }
        )
        offset += len(line)
    if not events_text.strip().startswith("startDocument()") or not events_text.strip().endswith(
        "endDocument()"
    ):
        raise ValueError("WPF 转换事件没有完整起止标记。")
    if not elements or sum(len(element.content_text) for element in elements) > MAX_TEXT:
        raise ValueError("WPF 没有可提取正文或正文超过限制。")
    return elements, {
        "artifacts": [html_artifact, events_artifact],
        "nodes": nodes,
        "events": lines,
        "images": images,
        "tables": tables,
        "event_source_record_mapping": "unverified",
        "physical_pages": False,
        **coverage,
    }


def _result(elements, *, source_hash, filename, format_name, tool_proof, mapping):
    from .ingestion import _parsed_result

    review = [
        {"reason": "source_artifact_layout_mapping_unverified", "source_hash": source_hash},
        {"reason": "field_visibility_and_object_completeness_unverified", "source_hash": source_hash},
    ]
    if (
        not elements
        or len(elements) > MAX_ELEMENTS
        or sum(len(element.content_text) for element in elements) > MAX_TEXT
    ):
        raise ValueError("旧格式没有可提取正文或正文超过限制。")
    result = _parsed_result(
        ParsedDocument(
            f"legacy-{format_name}", "1", PROTOCOL, elements, [], [row["reason"] for row in review]
        ),
        source_hash,
        Path(filename).stem,
    )
    result["quality"] = {
        "format": format_name,
        "complete": False,
        "pending_review": review,
        "physical_pages": False,
        "text_extracted": True,
    }
    result["legacy"] = {
        "source_hash": source_hash,
        "format": format_name,
        "tools": tool_proof,
        "macros_executed": False,
        "external_links_fetched": False,
        "independent_character_evidence": False,
        **mapping,
    }
    return result


def parse_legacy_doc(content: bytes, *, filename: str):
    if not content or len(content) > MAX_BYTES:
        raise ValueError("DOC 不能为空且不能超过 20 MB。")
    mapping = _doc_source(content)
    launcher, converters, proof = _configuration("doc")
    mode, converter, support = converters[0]
    raw = _run_converter(content, launcher, converter, support, mode)
    if _configuration("doc")[2] != proof:
        raise ValueError("DOC 转换工具或运行资料在处理中发生变化。")
    source_hash = _digest(content)
    elements, conversion = _docbook_elements(raw, source_hash)
    return _result(
        elements,
        source_hash=source_hash,
        filename=filename,
        format_name="doc",
        tool_proof=proof,
        mapping={"source_storage": mapping, "conversion": conversion},
    )


def parse_wpf(content: bytes, *, filename: str):
    if not content or len(content) > MAX_BYTES or not content.startswith(b"\xffWPC"):
        raise ValueError("WPF 签名无效、为空或超过 20 MB。")
    launcher, converters, proof = _configuration("wpf")
    outputs = [
        _run_converter(content, launcher, converter, support, mode) for mode, converter, support in converters
    ]
    if _configuration("wpf")[2] != proof:
        raise ValueError("WPF 转换工具或共享库在处理中发生变化。")
    source_hash = _digest(content)
    elements, mapping = _wpf_artifacts(*outputs, source_hash)
    return _result(
        elements,
        source_hash=source_hash,
        filename=filename,
        format_name="wpf",
        tool_proof=proof,
        mapping={
            "source_storage": {
                "kind": "source_bytes",
                "source_hash": source_hash,
                "offset": 0,
                "length": len(content),
                "record_mapping": "unverified",
            },
            "conversion": mapping,
        },
    )
