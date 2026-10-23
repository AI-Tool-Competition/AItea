"""Bounded mechanical PDF splitting and MinerU result import; no local OCR."""

from __future__ import annotations

from collections import defaultdict
import hashlib
from html.parser import HTMLParser
import io
import json
import mimetypes
from pathlib import Path, PurePosixPath
import resource
import stat
import zipfile

from .blob_store import BlobStore, _verify_existing

VERSION = "mineru-precise-v1"
CHART_ASSET_INVENTORY_VERSION = "mineru-chart-assets-v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ASSEMBLED_TEXT_CHARACTERS = 8_000_000
MAX_ASSEMBLED_ELEMENTS = 20_000
MAX_ASSEMBLED_RESULT_BYTES = 40 * 1024 * 1024


def reference(blob):
    return {
        "storage_path": str(Path("files", *blob.path.parts[-5:])),
        "content_hash": blob.digest,
        "size": blob.size,
    }


def resolve_reference(ref, files_dir, owner=None):
    digest = ref["content_hash"]
    path = PurePosixPath(ref["storage_path"])
    if path.is_absolute() or len(path.parts) != 6 or path.parts[:2] != ("files", "blobs"):
        raise ValueError("MinerU 产物必须绑定资料库内的相对 CAS 路径。")
    account, first, second, filename = path.parts[2:]
    if (
        len(digest) != 64
        or filename != digest
        or first != digest[:2]
        or second != digest[2:4]
        or len(account) != 64
    ):
        raise ValueError("MinerU 产物路径与内容哈希不匹配。")
    if owner is not None and account != hashlib.sha256(owner.encode()).hexdigest():
        raise ValueError("MinerU 产物账号绑定不匹配。")
    return Path(files_dir).resolve().joinpath(*path.parts[1:])


def _prepare(source, source_hash, size, filename, files_dir, owner, mime_type=""):
    path = Path(source)
    _verify_existing(path, source_hash, size)
    if size > MAX_SOURCE_BYTES:
        raise ValueError("原件超过当前资料库 128 MB 的安全传输上限。")
    store = BlobStore(Path(files_dir), max_bytes=MAX_SOURCE_BYTES)
    with path.open("rb") as original_file:
        original = reference(store.put(owner, iter(lambda: original_file.read(65536), b"")))
    if Path(filename).suffix.lower() != ".pdf" and mime_type != "application/pdf":
        return {"page_count": None, "parts": [{"first_page": 1, "last_page": None, "blob": original}]}
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("加密 PDF 需要先提供可读取的原件。")
    count = len(reader.pages)
    if not 0 < count <= 3000:
        raise ValueError("PDF 页数必须在 1 至 3000 之间。")
    store = BlobStore(Path(files_dir), max_bytes=MAX_SOURCE_BYTES)
    parts = []
    for offset in range(0, count, 200):
        end = min(count, offset + 200)
        blob = original
        if count > 200:
            writer = PdfWriter()
            for page in reader.pages[offset:end]:
                writer.add_page(page)
            out = io.BytesIO()
            writer.write(out)
            blob = reference(store.put_bytes(owner, out.getvalue()))
        parts.append({"first_page": offset + 1, "last_page": end, "blob": blob})
    return {"page_count": count, "parts": parts}


class _TableText(HTMLParser):
    """Keep rows and cells searchable; the original HTML remains an artifact."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell, self.depth = [], None, None, 0
        self.spans, self.cell_span = {}, (1, 1)

    def _carry(self):
        while (len(self.rows), len(self.row)) in self.spans:
            self.row.append(self.spans.pop((len(self.rows), len(self.row))))

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
            attributes = dict(attrs)
            spans = []
            for key in ("rowspan", "colspan"):
                value = attributes.get(key, "1")
                if not str(value).isdigit() or not 1 <= int(value) <= 256:
                    raise ValueError("MinerU 表格合并范围无效或超过限制。")
                spans.append(int(value))
            self.cell_span = tuple(spans)
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")
        elif tag in {"script", "style"}:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self._carry()
                content = " ".join("".join(self.cell).split()).replace("|", "\\|")
                row_span, col_span = self.cell_span
                first_col = len(self.row)
                if len(self.spans) + row_span * col_span > 100000 or first_col + col_span > 1024:
                    raise ValueError("MinerU 表格网格超过处理限制。")
                for offset in range(col_span):
                    column = first_col + offset
                    if (len(self.rows), column) in self.spans:
                        raise ValueError("MinerU 表格合并范围互相覆盖。")
                    self.row.append(content)
                    for delta in range(1, row_span):
                        key = (len(self.rows) + delta, column)
                        if key in self.spans:
                            raise ValueError("MinerU 表格合并范围互相覆盖。")
                        self.spans[key] = content
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self._carry()
            self.rows.append(self.row)
            self.row = None
        elif tag in {"script", "style"}:
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.cell is not None and not self.depth:
            self.cell.append(data)

    def markdown(self):
        if not self.rows:
            return ""
        width = max(map(len, self.rows))
        rows = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in self.rows]
        rows.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n".join(rows)


def _strings(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value).strip()
    return ""


def _read_archive(task, store, owner):
    ref = task["result_blob"]
    archive_path = resolve_reference(ref, store.files_dir, owner)
    _verify_existing(archive_path, ref["content_hash"], ref["size"])
    artifacts, payloads = {}, {}
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 10000 or sum(x.file_size for x in members) > MAX_EXPANDED_BYTES:
            raise ValueError("MinerU 结果包超过解包限制。")
        names = set()
        for item in members:
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item.filename
                or item.filename in names
                or stat.S_ISLNK(item.external_attr >> 16)
                or item.flag_bits & 1
                or item.file_size > MAX_MEMBER_BYTES
            ):
                raise ValueError("MinerU 结果包包含不安全或重复的成员。")
            names.add(item.filename)
            if item.is_dir():
                continue
            data = archive.read(item)
            if len(data) != item.file_size:
                raise ValueError("MinerU 结果包成员长度不一致。")
            # Some exporters include empty auxiliary files. Their exact bytes
            # remain in the ZIP; BlobStore intentionally rejects empty blobs.
            artifact = (
                reference(store.put_bytes(owner, data))
                if data
                else {"content_hash": hashlib.sha256(b"").hexdigest(), "size": 0, "empty": True}
            )
            artifact["mime_type"] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            artifacts[item.filename] = artifact
            if path.name in {
                "layout.json",
                "middle_json.json",
                "full.md",
                "markdown.md",
            } or path.name.endswith(("_content_list.json", "_model.json")):
                payloads[item.filename] = data
    lists = [name for name in payloads if name.endswith("_content_list.json")]
    layouts = [name for name in payloads if PurePosixPath(name).name in {"layout.json", "middle_json.json"}]
    if len(lists) != 1 or len(layouts) != 1:
        raise ValueError("MinerU 结果缺少唯一的内容列表或页布局，不能以 Markdown 代替页码证据。")
    blocks = json.loads(payloads[lists[0]])
    layout = json.loads(payloads[layouts[0]])
    info = layout.get("pdf_info", layout.get("pages")) if isinstance(layout, dict) else None
    if not isinstance(blocks, list) or not isinstance(info, list) or len(blocks) > 20000:
        raise ValueError("MinerU 结构化结果格式无效。")
    indexes = [p.get("page_idx") for p in info if isinstance(p, dict)]
    expected = task["last_page"] - task["first_page"] + 1 if task["last_page"] else len(info)
    if indexes != list(range(expected)) or not 0 < expected <= 200:
        raise ValueError("MinerU 结果页码缺失、重复或与提交分段不匹配。")
    # Hosted hybrid output merges tables across pages in the content list,
    # sometimes dropping every table_body. Model output retains the original
    # HTML on each physical page, before those merges. Match geometrically;
    # never attach a merged table's entire text to a single continuation page.
    models = [name for name in payloads if name.endswith("_model.json")]
    if len(models) > 1:
        raise ValueError("MinerU 结果包含多个模型输出。")
    if models:
        model = json.loads(payloads[models[0]])
        if (
            not isinstance(model, list)
            or len(model) != expected
            or any(not isinstance(p, list) for p in model)
        ):
            raise ValueError("MinerU 模型输出的物理页与布局不一致。")
        if sum(map(len, model)) > 40000:
            raise ValueError("MinerU 模型输出超过内容块限制。")
        used = set()
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "table":
                continue
            page, box = block.get("page_idx"), block.get("bbox")
            if (
                type(page) is not int
                or not 0 <= page < expected
                or not isinstance(box, list)
                or len(box) != 4
            ):
                continue
            matches = []
            for index, raw in enumerate(model[page]):
                if not isinstance(raw, dict) or raw.get("type") != "table" or (page, index) in used:
                    continue
                bbox = raw.get("bbox")
                if (
                    not isinstance(bbox, list)
                    or len(bbox) != 4
                    or any(type(x) not in (int, float) or not 0 <= x <= 1 for x in bbox)
                ):
                    continue
                scaled = [x * 1000 for x in bbox]
                area = max(0, min(box[2], scaled[2]) - max(box[0], scaled[0])) * max(
                    0, min(box[3], scaled[3]) - max(box[1], scaled[1])
                )
                union = (
                    (box[2] - box[0]) * (box[3] - box[1])
                    + (scaled[2] - scaled[0]) * (scaled[3] - scaled[1])
                    - area
                )
                if union > 0 and area / union >= 0.8 and _strings(raw.get("content")):
                    matches.append((area / union, index, raw))
            matches.sort(key=lambda item: item[0], reverse=True)
            if matches and (len(matches) == 1 or matches[0][0] - matches[1][0] > 0.1):
                _, index, raw = matches[0]
                used.add((page, index))
                block["_physical_table_body"] = raw["content"]
                block["_raw_model"] = {
                    "artifact": artifacts[models[0]],
                    "page_idx": page,
                    "block_index": index,
                }
    return blocks, info, artifacts, artifacts[lists[0]]


def _assemble(tasks, source_hash, filename, files_dir, owner, previous_manifest):
    from .ingestion import _parsed_result
    from .vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement

    store = BlobStore(Path(files_dir), max_bytes=MAX_SOURCE_BYTES)
    pages, elements, part_receipts, section, chart_assets = [], [], [], [], []
    warnings, total_chars, image_count, table_count, formula_count = [], 0, 0, 0, 0
    for task in tasks:
        if task["source_hash"] != source_hash or task["state"] != "done":
            raise ValueError("MinerU 任务尚未完成或原件绑定不一致。")
        blocks, layout, artifacts, content_ref = _read_archive(task, store, owner)
        first = task["first_page"]
        if first != len(pages) + 1:
            raise ValueError("MinerU 分段页码不是连续完整的原件范围。")
        texts, issues = defaultdict(list), defaultdict(list)
        base = PurePosixPath(next(name for name in artifacts if name.endswith("_content_list.json"))).parent
        for index, block in enumerate(blocks):
            if (
                not isinstance(block, dict)
                or type(block.get("page_idx")) is not int
                or not 0 <= block["page_idx"] < len(layout)
            ):
                raise ValueError("MinerU 内容块缺少有效物理页绑定。")
            local_page, kind = block["page_idx"], block.get("type")
            number = first + local_page
            text, element_kind = _strings(block.get("text")), "paragraph"
            metadata = {
                "provider": "mineru",
                "task_id": task["id"],
                "source_hash": source_hash,
                "raw_block_index": index,
                "raw_content_list": content_ref,
                "block_type": kind,
            }
            if kind == "table":
                table_count += 1
                body = _strings(block.get("_physical_table_body", block.get("table_body")))
                table = _TableText()
                table.feed(body)
                text = "\n\n".join(
                    filter(
                        None,
                        [
                            _strings(block.get("table_caption")),
                            table.markdown(),
                            _strings(block.get("table_footnote")),
                        ],
                    )
                )
                if body:
                    metadata["table_html"] = reference(store.put_bytes(owner, body.encode()))
                if block.get("_raw_model"):
                    metadata["raw_model"] = block["_raw_model"]
                element_kind = "table"
                if not table.rows:
                    issues[local_page].append("table_structure_missing")
            elif kind in {"equation", "interline_equation"}:
                formula_count += 1
                element_kind = "formula"
                text = _strings(block.get("text", block.get("latex")))
                metadata["latex"] = text
            elif kind in {"image", "chart"}:
                image_count += 1
                chart = _strings(block.get("content")) if kind == "chart" else ""
                if chart:
                    metadata["chart_content"] = reference(store.put_bytes(owner, chart.encode()))
                    if "<table" in chart.lower():
                        chart_table = _TableText()
                        chart_table.feed(chart)
                        chart = chart_table.markdown()
                text = "\n".join(
                    filter(
                        None,
                        [
                            _strings(block.get("image_caption", block.get("img_caption"))),
                            _strings(block.get("chart_caption")),
                            chart,
                            _strings(block.get("chart_footnote")),
                            _strings(block.get("image_footnote", block.get("img_footnote"))),
                        ],
                    )
                )
                element_kind = "caption"
                if kind == "chart" and not chart:
                    issues[local_page].append("chart_content_missing")
            elif kind == "list":
                text = _strings(block.get("list_items", block.get("text")))
                if not text:
                    issues[local_page].append("list_content_missing")
            elif kind == "code":
                text = _strings(block.get("code_body", block.get("text")))
                metadata["language"] = _strings(block.get("code_language"))
                if not text:
                    issues[local_page].append("code_content_missing")
            elif kind == "text" and block.get("text_level"):
                level = max(1, min(6, int(block["text_level"])))
                section = section[: level - 1] + [text]
                element_kind = "heading"
                metadata["heading_level"] = level
            elif kind not in {
                "text",
                "list",
                "code",
                "discarded",
                "header",
                "footer",
                "page_number",
                "footnote",
                "page_footnote",
                "aside_text",
                "ref_text",
            }:
                issues[local_page].append("unsupported_block_type")
            if block.get("img_path"):
                asset_name = str(base / block["img_path"])
                asset = artifacts.get(asset_name)
                if asset is None:
                    issues[local_page].append("referenced_image_missing")
                else:
                    metadata["image"] = asset
                    metadata["image_path"] = asset_name
            bbox = block.get("bbox")
            if bbox is not None and (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or any(type(x) not in (int, float) or not 0 <= x <= 1000 for x in bbox)
                or bbox[0] > bbox[2]
                or bbox[1] > bbox[3]
            ):
                raise ValueError("MinerU 内容块坐标无效。")
            locator = {
                "kind": "pdf",
                "page": number,
                "physical_page": number,
                "bbox": bbox,
                "coordinate_system": "mineru-0-1000",
                "source_hash": source_hash,
                "provider_task_id": task["id"],
                "provider_page_idx": local_page,
            }
            element_key = f"mineru-{number:06}-{index:06}"
            elements.append(
                ParsedElement(
                    element_key,
                    element_kind,
                    len(elements),
                    text,
                    locator,
                    list(section),
                    metadata_json=metadata,
                )
            )
            if kind == "chart" and not chart and isinstance(metadata.get("image"), dict):
                image_blob = metadata["image"]
                chart_assets.append(
                    {
                        "asset_key": "mineru-chart-" + hashlib.sha256(element_key.encode()).hexdigest()[:24],
                        "element_key": element_key,
                        "block_type": "chart",
                        "locator": locator,
                        "section_path": list(section),
                        "source_hash": source_hash,
                        "image_hash": image_blob["content_hash"],
                        "image_blob": image_blob,
                        "size": image_blob["size"],
                    }
                )
            if text and kind not in {"discarded", "header", "footer", "page_number"}:
                texts[local_page].append(text)
                total_chars += len(text)
            if total_chars > MAX_ASSEMBLED_TEXT_CHARACTERS:
                raise ValueError(
                    f"MinerU 正文超过单份资料 {MAX_ASSEMBLED_TEXT_CHARACTERS:,} 字符的组装上限。"
                )
            if len(elements) > MAX_ASSEMBLED_ELEMENTS:
                raise ValueError(
                    f"MinerU 正文超过单份资料 {MAX_ASSEMBLED_ELEMENTS:,} 个内容块的组装上限。"
                )
        for local_page, page_info in enumerate(layout):
            number = first + local_page
            text = "\n\n".join(texts[local_page])
            problems = sorted(set(issues[local_page]))
            if "\ufffd" in text:
                problems.append("decode_replacement")
            page = {
                "page": number,
                "status": "ocr_uncertain" if problems else "ocr_complete",
                "source_hash": source_hash,
                "native_text": "",
                "text": text,
                "ocr_required": False,
                "reasons": problems,
                "page_size": page_info.get("page_size"),
                "ocr": {
                    "provider": "mineru",
                    "task_id": task["id"],
                    "batch_id": task["batch_id"],
                    "provider_page_idx": local_page,
                    "part_first_page": first,
                    "result_hash": task["result_blob"]["content_hash"],
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "complete": not problems,
                    "uncertain_spans": problems,
                    "validation_version": VERSION,
                },
            }
            pages.append(page)
        part_receipts.append(
            {
                "task_id": task["id"],
                "batch_id": task["batch_id"],
                "source_hash": source_hash,
                "first_page": first,
                "last_page": first + len(layout) - 1,
                "part_blob": task["part_blob"],
                "result_blob": task["result_blob"],
                "artifacts": artifacts,
            }
        )
    document = ParsedDocument("mineru", "api-v4", VERSION, elements, [], warnings, pages)
    result = _parsed_result(document, source_hash, Path(filename).stem)
    review = [page["page"] for page in pages if page["status"] != "ocr_complete"]
    result["quality"] = {
        "status": "complete" if not review else "needs_review",
        "complete": not review,
        "page_count": len(pages),
        "completed_pages": [page["page"] for page in pages if page["status"] == "ocr_complete"],
        "pending_pages": [],
        "failed_pages": [],
        "review_pages": review,
        "review_required": bool(review),
        "source_incomplete_pages": [],
    }
    result["mineru_chart_assets"] = chart_assets
    result["mineru_chart_inventory_version"] = CHART_ASSET_INVENTORY_VERSION
    result["remote_parse"] = {
        "provider": "mineru",
        "model_version": "vlm",
        "state": "done",
        "source_hash": source_hash,
        "page_count": len(pages),
        "parts": part_receipts,
        "previous_manifest": previous_manifest,
        "statistics": {
            "tables": table_count,
            "images": image_count,
            "formulas": formula_count,
            "blocks": len(elements),
        },
    }
    return result


def worker(connection, operation, arguments):
    try:
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (90,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        result = (_prepare if operation == "prepare" else _assemble)(*arguments)
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_ASSEMBLED_RESULT_BYTES:
            raise ValueError("MinerU 结构化结果超过存储限额。")
        connection.send_bytes(encoded)
    except Exception as exc:
        message = str(exc)[:300] if isinstance(exc, ValueError) else "MinerU 结果处理失败或超过资源限制。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode())
    finally:
        connection.close()
