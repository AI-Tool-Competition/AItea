"""Read Jet4 table storage through an explicitly configured offline helper.

Called inside parse_bounded. Database storage pages and row slots are not
printed pages. Access reports, linked tables, OLE and complex columns remain
review gates rather than being silently treated as complete legal text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from .vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement

MAX_BYTES = 20 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
MAX_TEXT = 4_000_000
MAX_ROWS = 20_000
MAX_VALUE = 2 * 1024 * 1024
MAX_REVIEW = 2000
PROTOCOL = "aitea-mdb-storage-v1"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TYPES = {
    1: "Boolean",
    2: "Byte",
    3: "Integer",
    4: "Long Integer",
    5: "Currency",
    6: "Single",
    7: "Double",
    8: "DateTime",
    9: "Binary",
    10: "Text",
    11: "OLE",
    12: "Memo/Hyperlink",
    15: "Replication ID",
    16: "Numeric",
    18: "Complex",
}


def _integer(value, low=0, high=2**31 - 1):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("MDB 导出整数或记录定位无效。")
    return value


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("MDB 导出哈希无效。")
    return value


def _text(value, limit=16_384):
    if not isinstance(value, str) or len(value) > limit or "\ufffd" in value or "\x00" in value:
        raise ValueError("MDB 文本或元数据解码不完整。")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise ValueError("MDB 文本含无效 Unicode。") from exc
    return value


def _decode_binary(value):
    if not isinstance(value, str) or len(value) > ((MAX_VALUE + 2) // 3) * 4:
        raise ValueError("MDB 字段编码超过限制。")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("MDB 字段不是完整的 Base64。") from exc
    if base64.b64encode(raw).decode("ascii") != value or len(raw) > MAX_VALUE:
        raise ValueError("MDB 字段 Base64 不规范或超过限制。")
    return raw


def _jet4_text(raw: bytes) -> str:
    """Strict Jet4 Unicode compression, matching upstream iconv.c mode rules.

    A zero byte toggles compressed/uncompressed mode only at character
    boundaries. Uncompressed UTF-16 code units, including surrogate pairs,
    are decoded together; incomplete units and surrogates are rejected.
    """
    if raw.startswith(b"\xff\xfe"):
        expanded = bytearray()
        compressed, offset = True, 2
        while offset < len(raw):
            if raw[offset] == 0:
                compressed = not compressed
                offset += 1
            elif compressed:
                expanded.extend((raw[offset], 0))
                offset += 1
            else:
                if offset + 2 > len(raw):
                    raise ValueError("MDB 压缩 Unicode 尾部不完整。")
                expanded.extend(raw[offset : offset + 2])
                offset += 2
        raw = bytes(expanded)
    try:
        return _text(raw.decode("utf-16-le", "strict"), MAX_VALUE)
    except UnicodeError as exc:
        raise ValueError("MDB Unicode 原字段无法完整解码。") from exc


def _storage(record, *, source_size, page_size=4096):
    if not isinstance(record, dict) or set(record) != {
        "database_page",
        "row_slot",
        "start",
        "length",
        "sha256",
    }:
        raise ValueError("MDB 原始行定位结构无效。")
    page = _integer(record["database_page"], 0, source_size // page_size - 1)
    start = _integer(record["start"], 0, page_size - 1)
    length = _integer(record["length"], 1, page_size - start)
    _integer(record["row_slot"], 0, 1000)
    _hash(record["sha256"])
    return {**record, "file_offset": page * page_size + start, "length": length}


def _trusted_executable(path):
    if not path or not Path(path).is_absolute():
        raise ValueError("MDB 解析需要配置可信辅助程序绝对路径 TEA_MDB_READER_PATH。")
    executable = Path(path)
    try:
        info = executable.lstat()
    except OSError as exc:
        raise ValueError("MDB 辅助程序路径不可用。") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & stat.S_IWOTH
        or info.st_uid not in {0, os.getuid()}
        or not info.st_mode & stat.S_IXUSR
        or not 0 < info.st_size <= 16 * 1024 * 1024
    ):
        raise ValueError("MDB 辅助程序必须是可信、可执行的普通文件。")
    return executable, hashlib.sha256(executable.read_bytes()).hexdigest()


def _run_reader(content: bytes, executable: Path, *, timeout=25):
    """One process, a new process group, bounded pipes; helper enforces PDEATHSIG.

    The C executable itself creates its network namespace after binding the
    parent PID. There is no shell, query interpreter or unshare child chain.
    """
    if not 0 < timeout <= 30:
        raise ValueError("MDB 辅助程序超时设置无效。")
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    with tempfile.TemporaryDirectory(prefix="aitea-mdb-") as directory:
        original = Path(directory) / "original.mdb"
        original.write_bytes(content)
        original.chmod(0o400)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "MDBICONV": "UTF-8"}
        process = subprocess.Popen(
            [str(executable), str(os.getpid()), str(original)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        try:
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("MDB 辅助程序解析超时，原件已保留。")
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        outputs[key.data].extend(chunk)
                        if len(outputs[key.data]) > (MAX_OUTPUT if key.data == "stdout" else 65536):
                            raise ValueError("MDB 辅助程序输出超过限制。")
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code or outputs["stderr"]:
                # Source bytes, paths and native diagnostics never become a user error.
                raise ValueError("MDB 辅助程序拒绝此文件、无法建立离线隔离或提取不完整；原件已保留。")
            return bytes(outputs["stdout"])
        except subprocess.TimeoutExpired as exc:
            raise ValueError("MDB 辅助程序退出超时，原件已保留。") from exc
        finally:
            # Pipes retained after the direct child exits indicate descendants
            # still in this newly created process group. Reap those on failure.
            live_pipes = bool(selector.get_map())
            selector.close()
            if process.poll() is None or live_pipes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


def _value(column, field, *, source_size):
    if not isinstance(field, list) or len(field) != 4 or type(field[2]) is not bool:
        raise ValueError("MDB 原字段映射无效。")
    start, length, null_bit, payload = field
    _integer(start, 0, 4096)
    _integer(length, 0, 4096 - start)
    kind = column["type"]
    reference = {"start": start, "length": length, "storage_null_bit": null_bit}
    if kind == 1:
        if type(payload) is not bool or payload == null_bit:
            raise ValueError("MDB 布尔位映射无效。")
        return payload, reference, None
    if null_bit:
        if payload is not None:
            raise ValueError("MDB NULL 字段含意外值。")
        return None, reference, None
    if kind == 12:
        if not isinstance(payload, dict) or set(payload) != {"storage", "payload", "payload_sha256"}:
            raise ValueError("MDB Memo 原件映射无效。")
        raw = _decode_binary(payload["payload"])
        if hashlib.sha256(raw).hexdigest() != _hash(payload["payload_sha256"]):
            raise ValueError("MDB Memo 内容哈希不一致。")
        if not isinstance(payload["storage"], list) or len(payload["storage"]) > 4096:
            raise ValueError("MDB Memo 链超过限制。")
        reference["memo_storage"] = [_storage(row, source_size=source_size) for row in payload["storage"]]
        reference["payload_sha256"] = payload["payload_sha256"]
    else:
        raw = _decode_binary(payload)
        if len(raw) != length:
            raise ValueError("MDB 原字段长度不一致。")
        reference["sha256"] = hashlib.sha256(raw).hexdigest()
    if kind in {10, 12}:
        value = _jet4_text(raw)
        reason = (
            "rich_text_or_hyperlink_requires_review"
            if column["properties"].get("TextFormat") not in {None, "0"}
            else None
        )
        return value, reference, reason
    fixed = {2: (1, "<B"), 3: (2, "<h"), 4: (4, "<i"), 5: (8, "<q"), 6: (4, "<f"), 7: (8, "<d"), 8: (8, "<d")}
    reference["storage_base64"] = base64.b64encode(raw).decode("ascii")
    if kind in fixed:
        size, fmt = fixed[kind]
        if len(raw) != size:
            raise ValueError("MDB 数值字段字节数与原类型不符。")
        value = struct.unpack(fmt, raw)[0]
        if isinstance(value, float) and not math.isfinite(value):
            return None, reference, "non_finite_numeric"
        if kind == 5:
            value = str(Decimal(value) / Decimal(10_000))
        if kind == 8:
            return value, reference, "date_serial_requires_display_review"
        return value, reference, None
    return None, reference, "unsupported_binary_or_complex_column"


def _verify_memo_source(content, row_storage, field, reference):
    offset = row_storage["database_page"] * 4096 + field[0]
    header = content[offset : offset + field[1]]
    if len(header) < 12:
        raise ValueError("MDB Memo 原字段头不完整。")
    flags, pointer = struct.unpack_from("<II", header)
    expected = flags & 0x00FFFFFF
    if flags & 0x80000000:
        if reference.get("memo_storage"):
            raise ValueError("MDB 内联 Memo 出现外部链。")
        payload = header[12:]
    else:
        single = bool(flags & 0x40000000)
        if not single and flags & 0xFF000000:
            raise ValueError("MDB Memo 标志不支持。")
        pieces = []
        for storage in reference.get("memo_storage", []):
            if pointer != storage["database_page"] << 8 | storage["row_slot"]:
                raise ValueError("MDB Memo 链定位与原字段不一致。")
            raw = content[storage["file_offset"] : storage["file_offset"] + storage["length"]]
            if hashlib.sha256(raw).hexdigest() != storage["sha256"]:
                raise ValueError("MDB Memo 链字节哈希与原件不一致。")
            if single:
                pieces.append(raw)
                pointer = 0
            else:
                if len(raw) < 4:
                    raise ValueError("MDB Memo 分段不完整。")
                pointer = struct.unpack_from("<I", raw)[0]
                pieces.append(raw[4:])
        if pointer:
            raise ValueError("MDB Memo 链缺少尾部分段。")
        payload = b"".join(pieces)
    if len(payload) != expected or hashlib.sha256(payload).hexdigest() != reference["payload_sha256"]:
        raise ValueError("MDB Memo 导出正文与原件字节不一致。")


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("MDB 导出含重复字段。")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("MDB 导出包含非有限 JSON 常量。")


def _parse_output(encoded: bytes, *, content: bytes, filename: str, reader_sha256: str):
    from .ingestion import _parsed_result

    if not encoded or len(encoded) > MAX_OUTPUT:
        raise ValueError("MDB 导出为空或超过限制。")
    source_hash = hashlib.sha256(content).hexdigest()
    elements, objects, tables, review = [], [], [], []
    active, header, ended, rows, text_length = None, None, False, 0, 0
    names = set()

    def pending(reason, locator):
        if len(review) >= MAX_REVIEW:
            raise ValueError("MDB 待核验对象超过限制。")
        review.append({"reason": reason, "locator": locator})

    for line in encoded.splitlines():
        if not line or len(line) > 8 * 1024 * 1024:
            raise ValueError("MDB JSONL 记录为空或超过限制。")
        try:
            record = json.loads(
                line.decode("utf-8", "strict"),
                object_pairs_hook=_reject_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError) as exc:
            raise ValueError("MDB 导出不是完整的 UTF-8 JSONL。") from exc
        if not isinstance(record, dict) or ended:
            raise ValueError("MDB 导出记录结构或结束状态无效。")
        kind = record.get("kind")
        if header is None:
            if (
                kind != "header"
                or record.get("protocol") != PROTOCOL
                or record.get("libmdb_header_version") != "1.0.0"
                or type(record.get("jet_version")) is not int
                or record["jet_version"] != 1
                or type(record.get("page_size")) is not int
                or record["page_size"] != 4096
                or record.get("network_isolated") is not True
            ):
                raise ValueError("MDB 辅助程序版本、文件格式或离线隔离证明无效。")
            _hash(record.get("library_sha256"))
            _integer(record.get("code_page"), 0, 65535)
            header = record
        elif kind == "object" and active is None:
            _text(record.get("name"), 256)
            object_type = _integer(record.get("type"), 0, 32)
            if type(record.get("system")) is not bool:
                raise ValueError("MDB catalog 类型无效。")
            _integer(record.get("definition_page"), 0, 2**32 - 1)
            _integer(record.get("flags"), -(2**31), 2**31 - 1)
            objects.append(record)
            if len(objects) > 512:
                raise ValueError("MDB catalog 对象超过限制。")
            builtin_database_marker = (
                record["name"] == "MSysDb"
                and object_type == 2
                and record["flags"] == -(2**31)
                and record["definition_page"] == 0
            )
            if (
                not record["system"]
                and object_type in {0, 2, 4, 5, 6, 7, 9, 10, 11}
                and not builtin_database_marker
            ):
                # For non-table objects, definition_page is a catalog identifier,
                # not a validated storage offset. Keep it in the source record.
                pending(
                    "unparsed_access_object",
                    {
                        "kind": "database_object",
                        "source_hash": source_hash,
                        "name": record["name"],
                        "object_type": object_type,
                        "catalog_record": record,
                    },
                )
        elif kind == "table" and active is None:
            name = _text(record.get("name"), 256)
            if name in names or len(tables) >= 64:
                raise ValueError("MDB 表重复或超过限制。")
            names.add(name)
            columns = record.get("columns")
            if not isinstance(columns, list) or not 1 <= len(columns) <= 128:
                raise ValueError("MDB 列数无效。")
            _integer(record.get("declared_rows"), 0, MAX_ROWS)
            _integer(record.get("definition_page"), 0, len(content) // 4096 - 1)
            for column in columns:
                if not isinstance(column, dict):
                    raise ValueError("MDB 列定义无效。")
                _text(column.get("name"), 256)
                _integer(column.get("type"), 0, 255)
                _integer(column.get("size"), 0, MAX_VALUE)
                _integer(column.get("precision"), 0, 255)
                _integer(column.get("scale"), 0, 255)
                if type(column.get("fixed")) is not bool:
                    raise ValueError("MDB 列存储类型无效。")
                properties = column.get("properties")
                if not isinstance(properties, dict) or len(properties) > 64:
                    raise ValueError("MDB 列属性无效。")
                for key, value in properties.items():
                    _text(key, 256)
                    _text(value)
                column["source_type"] = _TYPES.get(column["type"], f"Unknown {column['type']}")
            if len({column["name"] for column in columns}) != len(columns):
                raise ValueError("MDB 列名重复。")
            active = {**record, "recovered_rows": 0}
            tables.append(active)
        elif kind == "row" and active is not None:
            if record.get("table") != active["name"] or record.get("ordinal") != active["recovered_rows"] + 1:
                raise ValueError("MDB 表行顺序或归属不一致。")
            _integer(record.get("ordinal"), 1, MAX_ROWS)
            storage = _storage(record.get("storage"), source_size=len(content))
            original = content[storage["file_offset"] : storage["file_offset"] + storage["length"]]
            if hashlib.sha256(original).hexdigest() != storage["sha256"]:
                raise ValueError("MDB 原行字节哈希不一致。")
            fields = record.get("fields")
            if not isinstance(fields, list) or len(fields) != len(active["columns"]):
                raise ValueError("MDB 字段数与列定义不一致。")
            values, references, text = [], [], []
            locator = {
                "kind": "database_storage",
                "source_format": "mdb",
                "source_hash": source_hash,
                "table": active["name"],
                "row_ordinal": record["ordinal"],
                **storage,
            }
            for index, (column, field) in enumerate(zip(active["columns"], fields, strict=True)):
                value, reference, reason = _value(column, field, source_size=len(content))
                if reference["length"] and not (
                    storage["start"]
                    <= reference["start"]
                    <= reference["start"] + reference["length"]
                    <= storage["start"] + storage["length"]
                ):
                    raise ValueError("MDB 字段位置超出原始行。")
                if column["type"] != 12 and not reference["storage_null_bit"]:
                    raw = content[
                        storage["database_page"] * 4096 + reference["start"] : storage["database_page"] * 4096
                        + reference["start"]
                        + reference["length"]
                    ]
                    if reference.get("sha256") and hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                        raise ValueError("MDB 字段字节哈希与原件不一致。")
                if column["type"] == 12 and not reference["storage_null_bit"]:
                    _verify_memo_source(content, storage, field, reference)
                values.append(value)
                references.append(reference)
                if reason:
                    pending(
                        reason,
                        {
                            **locator,
                            "column": column["name"],
                            "column_ordinal": index + 1,
                            "source_type": column["source_type"],
                            "field_storage": reference,
                        },
                    )
                text.append(
                    f"{json.dumps(column['name'], ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False, allow_nan=False)}"
                )
            body = "\n".join(text)
            text_length += len(body)
            rows += 1
            if rows > MAX_ROWS or text_length > MAX_TEXT:
                raise ValueError("MDB 行数或正文超过处理限制。")
            elements.append(
                ParsedElement(
                    f"table-{len(tables)}-row-{record['ordinal']}",
                    "table",
                    len(elements),
                    body,
                    locator,
                    [active["name"]],
                    metadata_json={"column_values": values, "field_storage": references},
                )
            )
            active["recovered_rows"] += 1
        elif kind == "table_end" and active is not None:
            _integer(record.get("rows"), 0, MAX_ROWS)
            if (
                record.get("name") != active["name"]
                or record.get("rows") != active["recovered_rows"]
                or record["rows"] != active["declared_rows"]
            ):
                raise ValueError("MDB 表行数不完整。")
            active = None
        elif kind == "end" and active is None:
            _integer(record.get("tables"), 1, 64)
            _integer(record.get("rows"), 0, MAX_ROWS)
            if record.get("tables") != len(tables) or record.get("rows") != rows:
                raise ValueError("MDB 完成统计与实际记录不一致。")
            ended = True
        else:
            raise ValueError("MDB 导出记录顺序或类型无效。")
    if not ended or active is not None or not elements:
        raise ValueError("MDB 导出未完整结束或没有正文表格。")
    warnings = sorted({entry["reason"] for entry in review})
    result = _parsed_result(
        ParsedDocument("mdb-libmdb-storage", "1.0.0", PROTOCOL, elements, [], warnings),
        source_hash,
        Path(filename).stem,
    )
    result["quality"] = {
        "format": "mdb",
        "complete": not review,
        "pending_review": review,
        "table_count": len(tables),
        "row_count": rows,
        "physical_pages": False,
    }
    result["database"] = {
        "source_hash": source_hash,
        "tables": tables,
        "catalog": objects,
        "reader_sha256": _hash(reader_sha256),
        "reader_protocol": PROTOCOL,
        "reader_header": header,
        "table_mapping_complete": True,
        "queries_executed": False,
        "macros_executed": False,
    }
    return result


def parse_mdb(content: bytes, *, filename: str = "database.mdb") -> dict:
    if not content or len(content) > MAX_BYTES:
        raise ValueError("MDB 文件不能为空且不能超过 20 MB。")
    if len(content) < 4096 or content[:4] != b"\x00\x01\x00\x00" or content[4:19] != b"Standard Jet DB":
        raise ValueError("MDB 原件缺少 Standard Jet DB 文件头，不能猜测修复或作为完整正文。")
    executable, reader_hash = _trusted_executable(os.getenv("TEA_MDB_READER_PATH", ""))
    encoded = _run_reader(content, executable)
    return _parse_output(encoded, content=content, filename=filename, reader_sha256=reader_hash)
