"""Static HTML text and visual source mapping; no scripts or remote resources."""

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
from html import unescape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from .vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement

VERSION = "html-source-mapping-v8"
VISUAL_PENDING_WARNING = "HTML 正文图像或对象已保留来源映射，尚未视觉核验；替代文字不代表图像正文。"
_VOID = {
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
    "track",
    "wbr",
}
_VISUAL = {"img", "picture", "svg", "canvas", "object", "embed", "iframe", "video"}
_ATTRIBUTES = {"src", "srcset", "data-src", "data-srcset", "data", "href", "xlink:href", "poster"}
_ATTRIBUTE = re.compile(r"([^\s<>/=]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]*)))?")
RSC_VIDEO_SCAN_VERSION = "html-next-flight-video-source-v1"
_ERROR_TITLE = re.compile(
    r"\s*(?:404(?:\s*[-:|]\s*|\s+)?(?:not found|page not found)?|403(?:\s*[-:|]\s*|\s+)?(?:forbidden)?|access denied|page not found|just a moment[.! ]*)\s*",
    re.I,
)
_DOWNLOAD_ERROR_BODY = re.compile(r"download\s+url\s+is\s+not\s+correct[.!]?", re.I)


@dataclass(frozen=True, slots=True)
class HTMLParsedDocument(ParsedDocument):
    html_source: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hidden_attributes(values):
    if "hidden" in values or str(values.get("aria-hidden", "")).casefold() == "true":
        return True
    style = str(values.get("style", ""))
    return bool(
        re.search(
            r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important\s*)?(?:;|$)",
            style,
            re.I,
        )
    )


def _youtube_id(url):
    if not isinstance(url, str) or len(url) > 4096:
        return None
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.youtube.com", "www.youtube-nocookie.com", "i.ytimg.com"}:
        return None
    match = re.fullmatch(r"/(?:embed|vi)/([A-Za-z0-9_-]{6,64})(?:/[^/]*)?", parsed.path)
    return match[1] if match else None


def _rsc_videos(source, source_hash, scripts, objects):
    """Read literal Flight data, never execute scripts or transcribe videos.

    Detached component records need an actual visible thumbnail with the same
    video ID. A serialized main subtree is also accepted. Navigation, hidden
    components, modules, resource hints and ordinary scripts are excluded.
    """
    from .discovery import _flight_json

    protocol = {
        "protocol": RSC_VIDEO_SCAN_VERSION,
        "script_execution": False,
        "scripts": 0,
        "records": 0,
        "video_objects": 0,
        "scan_errors": 0,
        "scope": "literal_next_flight_video_components_with_visible_thumbnail_or_body_ancestry",
    }
    thumbnails = {
        video_id
        for obj in objects
        if obj["tag"] == "img"
        for ref in obj.get("url_attributes", [])
        if ref.get("attribute") == "src"
        if (video_id := _youtube_id(ref.get("decoded_value")))
    }
    pieces, blocks, length = [], [], 0
    for script in scripts:
        raw = source[script["content_start"] : script["content_end"]]
        body = raw.strip()
        if not body.startswith("self.__next_f.push("):
            continue
        protocol["scripts"] += 1
        if protocol["scripts"] > 128:
            raise ValueError("HTML Next 视频数据块超过限制。")
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)\s*;?", body, re.S)
        if match is None or not script["closed"]:
            protocol["scan_errors"] += 1
            continue
        try:
            value = _flight_json(match[1])
        except ValueError:
            protocol["scan_errors"] += 1
            continue
        if (
            isinstance(value, list)
            and value
            and type(value[0]) is int
            and (value == [0] or (len(value) == 2 and value[0] == 2))
        ):
            continue  # Bootstrap and form-state records are not component data.
        if (
            not isinstance(value, list)
            or len(value) != 2
            or type(value[0]) is not int
            or value[0] != 1
            or not isinstance(value[1], str)
        ):
            protocol["scan_errors"] += 1
            continue
        payload = value[1].encode("utf-8")
        if length + len(payload) > 2 * 1024 * 1024:
            raise ValueError("HTML Next 视频数据超过字节限制。")
        blocks.append({**script, "stream_start": length, "stream_end": length + len(payload)})
        pieces.append(payload)
        length += len(payload)
    stream, position, nodes, results = b"".join(pieces), 0, 0, []
    while position < len(stream):
        if stream[position : position + 1] == b"\n":
            position += 1
            continue
        protocol["records"] += 1
        if protocol["records"] > 512:
            raise ValueError("HTML Next 视频记录超过限制。")
        record_start = position
        header = re.match(rb"([0-9a-f]{0,8}):", stream[position:])
        if not header:
            protocol["scan_errors"] += 1
            break
        position += header.end()
        if stream[position : position + 1] == b"T":
            text_header = re.match(rb"T([0-9a-f]{1,8}),", stream[position:])
            if not text_header or position + text_header.end() + int(text_header[1], 16) > len(stream):
                protocol["scan_errors"] += 1
                break
            position += text_header.end() + int(text_header[1], 16)
            continue  # Length-framed text is not a serialized video component.
        end = stream.find(b"\n", position)
        if end < 0 or end - position > 1024 * 1024:
            protocol["scan_errors"] += 1
            break
        payload = stream[position:end].decode("utf-8", errors="strict")
        position = end + 1
        if not payload.startswith(("[", "{")):
            continue  # Module/hint/control/diagnostic rows cannot supply video props.
        try:
            value = _flight_json(payload)
        except ValueError:
            protocol["scan_errors"] += 1
            continue
        pending = [(value, "", False, False, 0)]
        while pending:
            value, pointer, in_main, hidden, depth = pending.pop()
            nodes += 1
            if nodes > 50_000 or depth > 64:
                raise ValueError("HTML Next 视频对象数或深度超过限制。")
            if isinstance(value, list):
                if (
                    len(value) == 4
                    and value[0] == "$"
                    and isinstance(value[1], str)
                    and isinstance(value[3], dict)
                ):
                    tag, props = value[1], value[3]
                    style = props.get("style")
                    visibility = props
                    if props.get("hidden") is False or props.get("hidden") is None:
                        visibility = {key: item for key, item in props.items() if key != "hidden"}
                    hidden |= (
                        tag
                        in {
                            "nav",
                            "aside",
                            "head",
                            "script",
                            "noscript",
                        }
                        or _hidden_attributes(visibility)
                        or props.get("role") in {"navigation", "complementary"}
                    )
                    hidden |= tag in {"header", "footer"} and not in_main
                    hidden |= isinstance(style, dict) and (
                        style.get("display") == "none" or style.get("visibility") in {"hidden", "collapse"}
                    )
                    in_main |= tag == "main" and not hidden
                    uri, video_id = props.get("src"), props.get("videoId")
                    known_id = _youtube_id(uri)
                    if (
                        not hidden
                        and isinstance(uri, str)
                        and 0 < len(uri) <= 4096
                        and (
                            (tag in {"video", "iframe"} and in_main)
                            or (known_id and known_id == video_id and (in_main or known_id in thumbnails))
                        )
                    ):
                        spans = [
                            b for b in blocks if b["stream_start"] < end and b["stream_end"] > record_start
                        ]
                        start, finish = spans[0]["start"], spans[-1]["end"]
                        locator = {
                            "kind": "html_source_object",
                            "source_hash": source_hash,
                            "dom_path": spans[0]["dom_path"],
                            "dom_basis": "explicit_script_source_no_execution",
                            "line_start": source.count("\n", 0, start) + 1,
                            "column_start": start - (source.rfind("\n", 0, start) + 1),
                            "char_start": start,
                            "char_end": finish,
                            "offset_basis": "decoded_html_unicode_characters",
                            "source_boundary_complete": True,
                            "end_tag_observed": True,
                            "boundary_kind": "serialized_flight_record_source_scripts",
                        }
                        references = []
                        # Expose an exact literal only when it actually appears
                        # uniquely in the contributing script bytes. Escaped JSON
                        # URLs retain the decoding proof instead of invented offsets.
                        occurrences = [
                            (b, m)
                            for b in spans
                            for m in re.finditer(
                                re.escape(uri), source[b["content_start"] : b["content_end"]]
                            )
                        ]
                        if len(occurrences) == 1:
                            block, occurrence = occurrences[0]
                            first = block["content_start"] + occurrence.start()
                            literal = source[first : first + len(uri)]
                            references.append(
                                {
                                    "attribute": "rsc-video-src",
                                    "raw_value": literal,
                                    "raw_value_sha256": _hash(literal),
                                    "decoded_value": uri,
                                    "char_start": first,
                                    "char_end": first + len(literal),
                                    "offset_basis": "decoded_html_unicode_characters",
                                    "resource_fetched": False,
                                }
                            )
                        if len(objects) + len(results) >= 500:
                            raise ValueError("HTML 图像及对象数量超过限制。")
                        results.append(
                            {
                                "tag": "rsc-video",
                                "source_hash": source_hash,
                                "locator": locator,
                                "url_attributes": references,
                                "metadata": {
                                    "title": str(props.get("title", ""))[:4096],
                                    "type": "untranscribed_video",
                                },
                                "rsc_source": {
                                    "protocol": RSC_VIDEO_SCAN_VERSION,
                                    "record_id": header[1].decode(),
                                    "json_pointer": pointer + "/3/src",
                                    "decoded_record_sha256": _hash(payload),
                                    "decoded_record_start_byte": record_start,
                                    "decoded_record_end_byte": end,
                                    "decoded_stream_sha256": hashlib.sha256(stream).hexdigest(),
                                    "video_src": uri,
                                    "video_src_sha256": _hash(uri),
                                    "visible_thumbnail_id": known_id if known_id in thumbnails else None,
                                    "script_ranges": [
                                        {
                                            "char_start": b["start"],
                                            "char_end": b["end"],
                                            "sha256": _hash(source[b["start"] : b["end"]]),
                                        }
                                        for b in spans
                                    ],
                                },
                                "source_fragment_sha256": _hash(source[start:finish]),
                                "parent_object_key": None,
                                "duplicate_url_attributes": False,
                                "alt_is_recognized_text": False,
                                "vision_status": "audiovisual_transcription_pending",
                                "resource_fetched": False,
                                "indexed": False,
                            }
                        )
                pending.extend(
                    (child, pointer + f"/{index}", in_main, hidden, depth + 1)
                    for index, child in enumerate(value)
                )
            elif isinstance(value, dict):
                pending.extend(
                    (
                        child,
                        pointer + "/" + key.replace("~", "~0").replace("/", "~1"),
                        in_main,
                        hidden,
                        depth + 1,
                    )
                    for key, child in value.items()
                )
    protocol["video_objects"] = len(results)
    return results, protocol


class HTMLDocument(HTMLParser):
    """Map explicit source tags, not a browser's inferred or script-created DOM."""

    _blocks = {
        "p",
        "div",
        "section",
        "article",
        "li",
        "tr",
        "blockquote",
        "pre",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
    _hidden = {"script", "style", "noscript", "template", "head"}

    def __init__(
        self,
        *,
        main_only=False,
        source=None,
        source_hash="",
        exclude_navigation=False,
        max_elements=20_000,
        dom_validation=None,
    ):
        super().__init__(convert_charrefs=True)
        self.main_only = main_only
        self.has_main = False
        self.source = source
        self.source_hash = source_hash
        self.exclude_navigation = exclude_navigation
        self.max_elements = max_elements
        self.dom_validation = dom_validation or {}
        self.elements = []
        self.headings = []
        self.buffer = []
        self.heading_level = 0
        self.start_line = 1
        self.links = []
        self.link_records = []
        self.objects = []
        self._stack = []
        self._root_children = {}
        self._nodes = 0
        self._uncertain = False
        self._visible_quoted_text = False
        self._scripts = []
        self._rsc_scan = None
        self._table_row = None
        self._table_cell = None
        self._table_cell_record = None
        self._table_row_start = 1
        self._table_row_headers = False
        self._table_row_records = []
        self._table_start_line = 1
        self._table_start_char = 0
        self._table_group_id = 0
        self._table_depth = 0
        self._table_uncertain = False
        self._table_reasons = set()
        self._line_offsets = [0]
        if source is not None:
            self._line_offsets.extend(index + 1 for index, char in enumerate(source) if char == "\n")

    def _position(self):
        if self.source is None:
            return 0
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def _visible(self):
        frame = self._stack[-1] if self._stack else None
        return (frame is None or not frame["hidden"]) and (not self.main_only or (frame and frame["main"]))

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        self.buffer = []
        if not text:
            return
        if len(self.elements) >= self.max_elements:
            raise ValueError("HTML 元素数量超过限制。")
        kind = "paragraph"
        list_frame = next((frame for frame in reversed(self._stack) if frame.get("list_item_context")), None)
        if list_frame:
            kind = "list"
        if self.heading_level:
            self.headings = self.headings[: self.heading_level - 1] + [text]
            kind = "heading"
        order = len(self.elements) + 1
        context_frame = next(
            (frame for frame in reversed(self._stack) if frame.get("block_context")),
            None,
        )
        context = context_frame["block_context"] if context_frame else {}
        locator = {
            "kind": "html",
            "element_start": order,
            "element_end": order,
            "line_start": self.start_line,
            "line_end": self.getpos()[0],
            "heading_path": list(self.headings),
        }
        if context.get("tag"):
            locator["semantic_tag"] = context["tag"]
            locator["dom_path"] = context["path"]
            locator["start_tag_char_start"] = context["start_tag_char_start"]
            locator["start_tag_char_end"] = context["start_tag_char_end"]
            locator["start_tag_offset_basis"] = "decoded_html_unicode_characters"
            locator["start_tag_sha256"] = context["start_tag_sha256"]
        if context.get("classes"):
            locator["class_tokens"] = list(context["classes"])
        if context.get("class_attribute_sha256"):
            locator["class_attribute_sha256"] = context["class_attribute_sha256"]
            locator["class_attribute_length"] = context["class_attribute_length"]
            locator["class_tokens_truncated"] = context["class_tokens_truncated"]
        if context.get("title"):
            locator["source_title"] = context["title"]
        if context.get("title_sha256"):
            locator["source_title_sha256"] = context["title_sha256"]
            locator["source_title_length"] = context["title_length"]
            locator["source_title_truncated"] = context["title_truncated"]
        if list_frame and kind == "list":
            locator["list_path"] = list(list_frame["list_item_context"])
        if context.get("id"):
            locator["anchor"] = context["id"]
        self.elements.append(
            ParsedElement(f"html-{order:06}", kind, order - 1, text, locator, list(self.headings))
        )

    def _finish_table_cell(
        self, *, end_char=None, boundary_kind="unobserved_boundary", boundary_complete=False
    ):
        if self._table_row is None or self._table_cell is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._table_cell)).strip()
        self._table_row["cells"].append(text)
        record = self._table_cell_record
        if record is not None:
            record.update(
                text=text,
                char_end=end_char,
                source_boundary_complete=boundary_complete,
                end_tag_observed=boundary_kind == "observed_end_tag",
                boundary_kind=boundary_kind,
            )
            self._table_row["cell_records"].append(record)
        self._table_cell = None
        self._table_cell_record = None

    def _finish_table_row(
        self,
        *,
        end_char=None,
        boundary_kind="unobserved_boundary",
        boundary_complete=False,
        cell_end_char=None,
        cell_boundary_kind=None,
    ):
        if self._table_row is None:
            return
        if self._table_cell is not None:
            self._finish_table_cell(
                end_char=cell_end_char if cell_end_char is not None else end_char,
                boundary_kind=cell_boundary_kind or boundary_kind,
                boundary_complete=boundary_complete,
            )
        cells = self._table_row["cells"]
        row = self._table_row
        row.update(
            char_end=end_char,
            source_boundary_complete=boundary_complete,
            end_tag_observed=boundary_kind == "observed_end_tag",
            boundary_kind=boundary_kind,
        )
        self._table_row = None
        # Physical source-cell counts can differ when a table uses rowspan or
        # colspan. Validate the logical grid once all rows are available.
        self._table_row_records.append(
            {
                "row_index": len(self._table_row_records) + 1,
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "start_tag_char_end": row["start_tag_char_end"],
                "start_tag_sha256": row["start_tag_sha256"],
                "offset_basis": "decoded_html_unicode_characters",
                "source_boundary_complete": row["source_boundary_complete"],
                "end_tag_observed": row["end_tag_observed"],
                "boundary_kind": row["boundary_kind"],
                "column_count": 0,
                "source_cell_count": len(cells),
                "header_row": self._table_row_headers,
                "row_group": row["row_group"],
                "cells": row["cell_records"],
            }
        )

    def _table_grid(self):
        """Expand supported table spans into an auditable logical grid.

        Grid slots retain the originating source-cell index. Merged cells are
        rendered once in Markdown and explicitly annotated; continuation slots
        stay empty so retrieval cannot mistake a duplicated value for source
        text.
        """
        row_count = len(self._table_row_records)
        grid = [{} for _ in range(row_count)]
        width = 0
        valid = True
        for row_index, row in enumerate(self._table_row_records):
            for cell in row["cells"]:
                rowspan, colspan = cell.get("rowspan"), cell.get("colspan")
                if (
                    type(rowspan) is not int
                    or type(colspan) is not int
                    or rowspan < 1
                    or colspan < 1
                    or colspan > 256
                    or row_index + rowspan > row_count
                ):
                    valid = False
                    self._table_reasons.add("unsupported_or_out_of_bounds_cell_span")
                    continue
                row_group = row.get("row_group")
                if any(
                    self._table_row_records[target_row].get("row_group") != row_group
                    for target_row in range(row_index, row_index + rowspan)
                ):
                    valid = False
                    self._table_reasons.add("cell_span_crosses_row_group")
                    continue
                col = 0
                while col < 256:
                    if all(
                        target_col not in grid[target_row]
                        for target_row in range(row_index, row_index + rowspan)
                        for target_col in range(col, col + colspan)
                    ):
                        break
                    col += 1
                if col + colspan > 256:
                    valid = False
                    self._table_reasons.add("table_grid_column_limit")
                    continue
                cell["grid_row"] = row_index + 1
                cell["grid_column"] = col + 1
                cell["grid_row_end"] = row_index + rowspan
                cell["grid_column_end"] = col + colspan
                cell["grid_offset_basis"] = "one_based_logical_table_grid"
                for target_row in range(row_index, row_index + rowspan):
                    for target_col in range(col, col + colspan):
                        grid[target_row][target_col] = {
                            "source_cell_index": cell["cell_index"],
                            "origin_row": row_index + 1,
                            "origin_column": col + 1,
                            "is_origin": target_row == row_index and target_col == col,
                        }
                width = max(width, col + colspan)

        counts = []
        rendered = []
        for row_index, (slots, row) in enumerate(zip(grid, self._table_row_records, strict=True)):
            columns = max(slots, default=-1) + 1
            counts.append(columns)
            row["column_count"] = columns
            row["logical_column_count"] = columns
            if any(column not in slots for column in range(columns)):
                valid = False
                self._table_reasons.add("uneven_logical_grid_rows")
            source_cells = {cell["cell_index"]: cell for cell in row["cells"]}
            values = []
            for column in range(width):
                slot = slots.get(column)
                if not slot or not slot["is_origin"]:
                    values.append("")
                    continue
                cell = source_cells[slot["source_cell_index"]]
                text = cell["text"]
                rowspan = cell["rowspan"]
                colspan = cell["colspan"]
                if rowspan > 1 or colspan > 1:
                    text += f" [merged across {rowspan} row(s) × {colspan} column(s)]"
                values.append(text.replace("|", "\\|").replace("\n", " "))
            rendered.append("| " + " | ".join(values) + " |")
            if row["header_row"] and row_index == 0:
                rendered.append("| " + " | ".join("---" for _ in range(width)) + " |")
        if width > 256:
            valid = False
            self._table_reasons.add("table_grid_column_limit")
        if len(set(counts)) > 1 or (counts and counts[0] != width):
            valid = False
            self._table_reasons.add("uneven_logical_grid_rows")
        return grid, counts, rendered, valid

    def _emit_table(self, *, closed=False, end_char=None):
        if not self._table_row_records:
            return
        grid, column_counts, rendered_rows, grid_valid = self._table_grid()
        if not grid_valid:
            self._uncertain = True
            self._table_uncertain = True
        if len(self.elements) >= self.max_elements:
            raise ValueError("HTML 元素数量超过限制。")
        order = len(self.elements) + 1
        self.elements.append(
            ParsedElement(
                f"html-{order:06}",
                "table",
                order - 1,
                "\n".join(rendered_rows),
                {
                    "kind": "html",
                    "line_start": self._table_start_line,
                    "line_end": self.getpos()[0],
                    "heading_path": list(self.headings),
                    "source_hash": self.source_hash,
                    "char_start": self._table_start_char,
                    "char_end": end_char if end_char is not None else len(self.source or ""),
                    "offset_basis": "decoded_html_unicode_characters",
                    "table_row_count": len(self._table_row_records),
                    "column_counts": column_counts,
                    "grid": grid,
                    "grid_coordinate_basis": "zero-based rows and columns; source cell indices are one-based",
                    "rows": deepcopy(self._table_row_records),
                    "structure_reasons": sorted(self._table_reasons),
                    "structure_status": "complete" if closed and not self._table_uncertain else "partial",
                },
                list(self.headings),
            )
        )
        self._table_row_records = []
        self._table_uncertain = False
        self._table_reasons = set()

    def _start_table_row(self, start, token):
        self._finish_table_row(
            end_char=start,
            boundary_kind="implied_end_before_next_row",
            boundary_complete=True,
            cell_end_char=start,
            cell_boundary_kind="implied_end_before_next_row",
        )
        self._flush()
        self._table_row_start = self.getpos()[0]
        self._table_row = {
            "cells": [],
            "cell_records": [],
            "char_start": start,
            "start_tag_char_end": start + len(token),
            "start_tag_sha256": _hash(token),
            "row_group": next(
                (
                    (frame["tag"], frame["start"])
                    for frame in reversed(self._stack)
                    if frame["tag"] in {"thead", "tbody", "tfoot"}
                ),
                ("implicit", self._table_group_id),
            ),
        }
        self._table_row_headers = False

    @staticmethod
    def _table_span(value):
        if value is None:
            return 1, None
        if value == "":
            return 1, ""
        raw = str(value)
        if not raw.isdecimal():
            return None, raw
        number = int(raw)
        if number > 1000:
            return None, raw
        return number, raw

    def _attributes(self, token, start):
        result = []
        # Skip the opening tag name; preserve each literal URL attribute and entities.
        name = re.match(r"<\s*[^\s/>]+", token)
        for match in _ATTRIBUTE.finditer(token, name.end() if name else 0):
            attribute = match.group(1).lower()
            if attribute not in _ATTRIBUTES:
                continue
            group = next((index for index in (2, 3, 4) if match.group(index) is not None), None)
            raw = match.group(group) if group else ""
            if len(raw) > 65_536:
                raise ValueError("HTML 图片引用属性超过限制。")
            offset = match.start(group) if group else match.end(1)
            result.append(
                {
                    "attribute": attribute,
                    "raw_value": raw,
                    "raw_value_sha256": _hash(raw),
                    "decoded_value": unescape(raw),
                    "char_start": start + offset,
                    "char_end": start + offset + len(raw),
                    "offset_basis": "decoded_html_unicode_characters",
                    "resource_fetched": False,
                }
            )
        return result

    def _finish_object(self, frame, end, *, closed=True):
        index = frame.get("object_index")
        if index is None or self.source is None:
            return
        record = self.objects[index]
        record["locator"].update(
            char_end=end,
            source_boundary_complete=closed,
            end_tag_observed=closed and frame["tag"] not in _VOID and not frame.get("self_closing"),
            boundary_kind=(
                "unterminated_source_range"
                if not closed
                else "void_start_tag"
                if frame["tag"] in _VOID
                else "self_closing_tag"
                if frame.get("self_closing")
                else "observed_end_tag"
            ),
        )
        record["source_fragment_sha256"] = _hash(self.source[frame["start"] : end])
        if not closed:
            self._uncertain = True

    def _close(self, index, end, *, observed=True):
        frames = self._stack[index:]
        for frame in reversed(frames):
            if frame.get("link_index") is not None:
                closed = observed and frame is frames[0]
                self.link_records[frame["link_index"]]["locator"].update(
                    char_end=end,
                    source_boundary_complete=closed,
                    end_tag_observed=closed,
                )
                if not closed:
                    self._uncertain = True
            if frame.get("script_inline") and self.source is not None:
                self._scripts.append(
                    {
                        "start": frame["start"],
                        "end": end,
                        "content_start": frame["script_content_start"],
                        "content_end": self._position(),
                        "dom_path": frame["path"],
                        "closed": observed and frame is frames[0],
                    }
                )
            self._finish_object(frame, end, closed=observed and frame is frames[0])
        self._stack = self._stack[:index]

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, self_closing=True)

    def _start(self, tag, attrs, *, self_closing):
        self._nodes += 1
        if self._nodes > 100_000 or len(self._stack) >= 128:
            raise ValueError("HTML 标签数量或嵌套深度超过限制。")
        if tag == "li":
            list_parent = next(
                (
                    index
                    for index in range(len(self._stack) - 1, -1, -1)
                    if self._stack[index]["tag"] in {"ol", "ul"}
                ),
                -1,
            )
            previous = next(
                (
                    index
                    for index in range(len(self._stack) - 1, list_parent, -1)
                    if self._stack[index]["tag"] == "li"
                ),
                None,
            )
            if previous is not None:
                self._flush()
                self._close(previous, self._position(), observed=False)
                self._uncertain = True
        # HTML keeps the first duplicated attribute. Preserve every URL literal
        # below and keep ambiguous visible source markup incomplete.
        values = {}
        for name, value in attrs:
            values.setdefault(name, value)
        parent = self._stack[-1] if self._stack else None
        hidden = bool(parent and parent["hidden"]) or tag in self._hidden or _hidden_attributes(values)
        if self.exclude_navigation and (
            tag in {"nav", "aside"} or values.get("role") in {"navigation", "complementary"}
        ):
            hidden = True
        in_main = bool(parent and parent["main"]) or (tag == "main" and not hidden)
        if tag == "main" and not hidden:
            self.has_main = True
        visible = not hidden and (not self.main_only or in_main)
        if visible and len(values) != len(attrs):
            self._uncertain = True
        children = parent["children"] if parent else self._root_children
        children[tag] = children.get(tag, 0) + 1
        path = (parent["path"] + "/" if parent else "") + f"{tag}[{children[tag]}]"
        start = self._position() if self.source is not None else 0
        token = self.get_starttag_text() or ""
        if tag == "table":
            if self._table_depth == 0:
                self._flush()
                self._table_start_line = self.getpos()[0]
                self._table_start_char = start
                self._table_uncertain = False
                self._table_group_id += 1
            else:
                self._uncertain = True
                self._table_uncertain = True
                self._table_reasons.add("nested_table")
            self._table_depth += 1
        if tag == "tr":
            self._start_table_row(start, token)
        elif tag in {"td", "th"} and self._table_row is not None:
            if self._table_cell is not None:
                self._finish_table_cell(
                    end_char=start,
                    boundary_kind="implied_end_before_next_cell",
                    boundary_complete=True,
                )
            rowspan, rowspan_value = self._table_span(values.get("rowspan"))
            colspan, colspan_value = self._table_span(values.get("colspan"))
            self._table_cell = []
            self._table_cell_record = {
                "cell_index": len(self._table_row["cell_records"]) + 1,
                "tag": tag,
                "header_cell": tag == "th",
                "rowspan": rowspan,
                "rowspan_attribute_value": rowspan_value,
                "colspan": colspan,
                "colspan_attribute_value": colspan_value,
                "scope": values.get("scope"),
                "headers": values.get("headers"),
                "id": values.get("id"),
                "char_start": start,
                "start_tag_char_end": start + len(token),
                "start_tag_sha256": _hash(token),
                "offset_basis": "decoded_html_unicode_characters",
            }
            self._table_row_headers |= tag == "th"
        parent_object = next(
            (
                self.objects[f["object_index"]]["object_key"]
                for f in reversed(self._stack)
                if f.get("object_index") is not None
            ),
            None,
        )
        inside_visual = bool(parent and parent["visual"])
        visual = visible and (
            tag in _VISUAL
            or (tag == "source" and any(f["tag"] in {"picture", "video"} for f in self._stack))
            or (tag in {"image", "use", "foreignobject"} and any(f["tag"] == "svg" for f in self._stack))
        )
        frame = {
            "tag": tag,
            "hidden": hidden,
            "main": in_main,
            "path": path,
            "children": {},
            "start": start,
            "visual": inside_visual or visual,
            "self_closing": self_closing,
        }
        if tag in {"ol", "ul"}:
            frame["list_next"] = (
                int(values.get("start", "1")) if str(values.get("start", "1")).lstrip("+-").isdigit() else 1
            )
            if tag == "ol" and "reversed" in values:
                self._uncertain = True
        elif tag == "li":
            parent_list = next(
                (value for value in reversed(self._stack) if value["tag"] in {"ol", "ul"}), None
            )
            if parent_list:
                item_index = None
                if parent_list["tag"] == "ol":
                    raw_value = values.get("value")
                    if raw_value is not None and str(raw_value).lstrip("+-").isdigit():
                        item_index = int(raw_value)
                        parent_list["list_next"] = item_index + 1
                    elif raw_value is not None:
                        self._uncertain = True
                    else:
                        item_index = parent_list["list_next"]
                        parent_list["list_next"] += 1
                else:
                    parent_list["list_next"] += 1
                path = [
                    *(item for ancestor in self._stack for item in ancestor.get("list_item_context", [])),
                    {"type": parent_list["tag"], "index": item_index},
                ]
                frame["list_item_context"] = path
        if tag == "script" and not values.get("src"):
            frame.update(script_inline=True, script_content_start=start + len(token))
        if self._visible() and (hidden or visual):
            self._flush()
        if visible and tag == "a" and values.get("href"):
            self.links.append(values["href"])
            if self.source is not None:
                if len(self.link_records) >= 10_000:
                    raise ValueError("HTML 链接映射数量超过处理限制。")
                reference = next(
                    (value for value in self._attributes(token, start) if value["attribute"] == "href"),
                    None,
                )
                if reference is not None:
                    link_index = len(self.link_records)
                    self.link_records.append(
                        {
                            "link_key": f"html-link-{link_index + 1:06}",
                            "href": reference["decoded_value"],
                            "raw_href": reference["raw_value"],
                            "locator": {
                                "kind": "html_source_link",
                                "source_hash": self.source_hash,
                                "dom_path": path,
                                "dom_basis": "explicit_source_tags_no_script_execution",
                                "line_start": self.getpos()[0],
                                "column_start": self.getpos()[1],
                                "char_start": start,
                                "offset_basis": "decoded_html_unicode_characters",
                            },
                            "anchor_text": "",
                        }
                    )
                    frame["link_index"] = link_index
        if visual and self.source is not None:
            if len(self.objects) >= 500:
                raise ValueError("HTML 图像及对象数量超过限制。")
            references = self._attributes(token, start)
            duplicates = len({ref["attribute"] for ref in references}) != len(references)
            self._uncertain |= duplicates
            key = f"html-object-{len(self.objects) + 1:06}"
            locator = {
                "kind": "html_source_object",
                "source_hash": self.source_hash,
                "dom_path": path,
                "dom_basis": "explicit_source_tags_no_script_execution",
                "line_start": self.getpos()[0],
                "column_start": self.getpos()[1],
                "char_start": start,
                "offset_basis": "decoded_html_unicode_characters",
            }
            metadata = {
                key: str(values[key])
                for key in ("alt", "title", "type", "width", "height", "media", "sizes")
                if values.get(key) is not None
            }
            if any(len(value) > 65_536 for value in metadata.values()):
                raise ValueError("HTML 图片元数据超过限制。")
            self.objects.append(
                {
                    "object_key": key,
                    "tag": tag,
                    "source_hash": self.source_hash,
                    "locator": locator,
                    "url_attributes": references,
                    "metadata": metadata,
                    "alt_is_recognized_text": False,
                    "parent_object_key": parent_object,
                    "duplicate_url_attributes": duplicates,
                    "vision_status": "pending",
                    "resource_fetched": False,
                    "indexed": False,
                }
            )
            frame["object_index"] = len(self.objects) - 1
        if visible and not inside_visual and not visual and tag in self._blocks:
            self._flush()
            self.heading_level = int(tag[1]) if re.fullmatch(r"h[1-6]", tag) else 0
            self.start_line = self.getpos()[0]
            raw_classes = str(values.get("class", ""))
            class_truncated = len(raw_classes) > 256
            block_title = values.get("title")
            title_truncated = block_title is not None and len(str(block_title)) > 1024
            if title_truncated:
                block_title = str(block_title)[:1024]
            frame["block_context"] = {
                "tag": tag,
                "path": path,
                "classes": raw_classes[:256].split()[:32],
                "class_attribute_sha256": _hash(raw_classes) if "class" in values else None,
                "class_attribute_length": len(raw_classes) if "class" in values else None,
                "class_tokens_truncated": class_truncated,
                "title": block_title,
                "title_sha256": _hash(str(values["title"])) if "title" in values else None,
                "title_length": len(str(values["title"])) if "title" in values else None,
                "title_truncated": title_truncated,
                "id": values.get("id"),
                "start_tag_char_start": start,
                "start_tag_char_end": start + len(token),
                "start_tag_sha256": _hash(token),
            }
        if tag in _VOID or self_closing:
            self._finish_object(frame, start + len(token))
            if frame.get("link_index") is not None:
                self.link_records[frame["link_index"]]["locator"].update(
                    char_end=start + len(token),
                    source_boundary_complete=False,
                    end_tag_observed=False,
                )
                self._uncertain = True
        else:
            self._stack.append(frame)

    def handle_endtag(self, tag):
        start = self._position()
        if tag in {"ol", "ul"}:
            list_index = next(
                (index for index in range(len(self._stack) - 1, -1, -1) if self._stack[index]["tag"] == tag),
                None,
            )
            item = next(
                (index for index in range(len(self._stack) - 1, -1, -1) if self._stack[index]["tag"] == "li"),
                None,
            )
            if item is not None and list_index is not None and item > list_index:
                self._flush()
                self._close(item, self._position(), observed=False)
                self._uncertain = True
        if tag in {"td", "th"}:
            close = re.match(r"</[^>]*>", self.source[start:]) if self.source else None
            self._finish_table_cell(
                end_char=start + (len(close.group()) if close else 0),
                boundary_kind="observed_end_tag",
                boundary_complete=close is not None,
            )
        elif tag == "tr":
            close = re.match(r"</[^>]*>", self.source[start:]) if self.source else None
            self._finish_table_row(
                end_char=start + (len(close.group()) if close else 0),
                boundary_kind="observed_end_tag",
                boundary_complete=True,
                cell_end_char=start,
                cell_boundary_kind="implied_end_at_row_end",
            )
        elif tag == "table":
            if self._table_row is not None or self._table_cell is not None:
                self._uncertain = True
                self._table_uncertain = True
                self._table_reasons.add("implicit_cell_or_row_closure_at_table_end")
            self._finish_table_row(
                end_char=start,
                boundary_kind="implied_end_at_table_end",
                boundary_complete=True,
                cell_end_char=start,
                cell_boundary_kind="implied_end_at_table_end",
            )
            self._table_depth = max(0, self._table_depth - 1)
            if self._table_depth == 0:
                close = re.match(r"</[^>]*>", self.source[self._position() :]) if self.source else None
                end_char = self._position() + (len(close.group()) if close else 0)
                self._emit_table(
                    closed=close is not None,
                    end_char=end_char,
                )
        if tag in _VOID:
            return
        index = next((i for i in range(len(self._stack) - 1, -1, -1) if self._stack[i]["tag"] == tag), None)
        if index is None:
            return
        frame = self._stack[index]
        if self._visible() and not frame["visual"] and (tag in self._blocks or tag == "main"):
            self._flush()
            self.heading_level = 0
            self.anchor = None
            self.start_line = self.getpos()[0]
        end = self._position() if self.source is not None else 0
        if self.source is not None:
            close = re.match(r"</[^>]*>", self.source[end:])
            end += len(close.group()) if close else 0
        self._close(index, end)

    def handle_data(self, data):
        if self._visible():
            anchor = next(
                (frame for frame in reversed(self._stack) if frame.get("link_index") is not None), None
            )
            if anchor is not None:
                record = self.link_records[anchor["link_index"]]
                if len(record["anchor_text"]) + len(data) <= 32_768:
                    record["anchor_text"] += data
                else:
                    self._uncertain = True
        if self._visible() and self._table_row is not None:
            if self._table_cell is None:
                self._table_cell = []
            self._table_cell.append(data)
        elif self._visible() and not (self._stack and self._stack[-1]["visual"]):
            if data.strip() and any(frame["tag"] in {"blockquote", "q", "code"} for frame in self._stack):
                self._visible_quoted_text = True
            self.buffer.append(data)

    def finish(self):
        self.close()
        if self._table_depth:
            self._uncertain = True
        for frame in self._stack:
            if frame.get("link_index") is not None:
                self.link_records[frame["link_index"]]["locator"].update(
                    char_end=len(self.source or ""),
                    source_boundary_complete=False,
                    end_tag_observed=False,
                )
                self._uncertain = True
        self._finish_table_row()
        self._emit_table(closed=False)
        self._flush()
        for frame in self._stack:
            if frame.get("script_inline") and self.source is not None:
                self._scripts.append(
                    {
                        "start": frame["start"],
                        "end": len(self.source),
                        "content_start": frame["script_content_start"],
                        "content_end": len(self.source),
                        "dom_path": frame["path"],
                        "closed": False,
                    }
                )
            if frame.get("object_index") is not None:
                self._finish_object(frame, len(self.source or ""), closed=False)

    def document(self):
        if self._rsc_scan is None:
            videos, self._rsc_scan = _rsc_videos(
                self.source or "", self.source_hash, self._scripts, self.objects
            )
            if len(self.objects) + len(videos) > 500:
                raise ValueError("HTML 图像及对象数量超过限制。")
            self._uncertain |= bool(self._rsc_scan["scan_errors"])
            for video in videos:
                video["object_key"] = f"html-object-{len(self.objects) + 1:06}"
                self.objects.append(video)
        source_mapping = {
            "mapping_version": VERSION,
            "source_hash": self.source_hash,
            "dom_validation": self.dom_validation,
            "decoded_source_sha256": _hash(self.source or ""),
            "offset_basis": "decoded_html_unicode_characters",
            "dom_basis": "explicit_source_tags_no_script_execution",
            "objects": self.objects,
            "links": self.link_records,
            "main_container_found": self.has_main,
            "navigation_excluded": self.exclude_navigation,
            "external_resources_fetched": False,
            "scripts_executed": False,
            "rsc_video_scan": self._rsc_scan,
        }
        has_body = any(element.kind != "heading" and element.content_text for element in self.elements)
        titles = [element.content_text for element in self.elements if element.kind == "heading"][:1]
        head_title = re.search(r"<title\b[^>]*>([^<]{0,200})</title\s*>", self.source or "", re.I)
        if head_title:
            titles.append(unescape(head_title.group(1)))
        visible_text = [element.content_text for element in self.elements]
        # A successful HTTP response can contain only a download error. Match
        # complete visible elements, never words or an error quoted in legal
        # reasoning. Recognized error headings may accompany the same message.
        download_error = (
            not self._visible_quoted_text
            and any(_DOWNLOAD_ERROR_BODY.fullmatch(text) for text in visible_text)
            and all(
                _DOWNLOAD_ERROR_BODY.fullmatch(text)
                or _ERROR_TITLE.fullmatch(text)
                or text.casefold() == "download error"
                for text in visible_text
            )
        )
        error_page = download_error or any(_ERROR_TITLE.fullmatch(title) for title in titles)
        complete = has_body and not self.objects and not self._uncertain and not error_page
        warnings = []
        if self.objects:
            warnings.append(VISUAL_PENDING_WARNING)
        if self._uncertain:
            warnings.append("HTML 对象边界或重复引用属性仍待核验。")
        if error_page:
            warnings.append("HTML 标题或完整可见正文表明来源为错误或访问验证页，正文完整性不能确认。")
        quality = {
            "status": "complete" if complete else "partial",
            "complete": complete,
            "pending_objects": [record["object_key"] for record in self.objects],
            "source_mapping_complete": not self._uncertain,
            "body_text_present": has_body,
            "visual_verification_required": bool(self.objects),
            "error_page_detected": error_page,
        }
        return HTMLParsedDocument(
            "html-basic",
            "8",
            VERSION,
            self.elements,
            [],
            warnings,
            html_source=source_mapping,
            quality=quality,
        )
