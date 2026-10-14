"""MDB source fidelity, completion gates and bounded tool cleanup."""

import base64
import hashlib
import json
import os
import struct
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest

from app.ingestion_quality import text_complete
from app.mdb_parsing import PROTOCOL, _jet4_text, _parse_output, _run_reader, _trusted_executable, parse_mdb


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def source_records(*, extra_objects=(), memo=False):
    # Independently constructed raw row bytes, with no parser-generated values.
    source = bytearray(8192)
    source[:19] = b"\x00\x01\x00\x00Standard Jet DB"
    raw_text = b'\xff\xfe0012\nquote: "yes"'
    numeric = struct.pack("<d", 0.1)
    field = raw_text
    if memo:
        field = struct.pack("<III", 0x80000000 | len(raw_text), 0, 0) + raw_text
    start = 3800
    row = field + numeric + b"\x05"  # retained opaque row trailer / null bitmap
    source[4096 + start : 4096 + start + len(row)] = row
    columns = [
        {
            "name": "HS",
            "type": 12 if memo else 10,
            "size": 255,
            "precision": 0,
            "scale": 0,
            "fixed": False,
            "properties": {},
        },
        {"name": "Duty", "type": 7, "size": 8, "precision": 0, "scale": 0, "fixed": True, "properties": {}},
        {
            "name": "Empty",
            "type": 10,
            "size": 50,
            "precision": 0,
            "scale": 0,
            "fixed": False,
            "properties": {},
        },
        {
            "name": "Null",
            "type": 10,
            "size": 50,
            "precision": 0,
            "scale": 0,
            "fixed": False,
            "properties": {},
        },
    ]
    payload = encoded(field)
    if memo:
        payload = {
            "payload": encoded(raw_text),
            "payload_sha256": hashlib.sha256(raw_text).hexdigest(),
            "storage": [],
        }
    records = [
        {
            "kind": "header",
            "protocol": PROTOCOL,
            "libmdb_header_version": "1.0.0",
            "library_sha256": "a" * 64,
            "jet_version": 1,
            "code_page": 1252,
            "page_size": 4096,
            "network_isolated": True,
        },
        {"kind": "object", "name": "Tariffs", "type": 1, "system": False, "flags": 0, "definition_page": 0},
        *extra_objects,
        {"kind": "table", "name": "Tariffs", "definition_page": 0, "declared_rows": 1, "columns": columns},
        {
            "kind": "row",
            "table": "Tariffs",
            "ordinal": 1,
            "storage": {
                "database_page": 1,
                "row_slot": 2,
                "start": start,
                "length": len(row),
                "sha256": hashlib.sha256(row).hexdigest(),
            },
            "fields": [
                [start, len(field), False, payload],
                [start + len(field), 8, False, encoded(numeric)],
                [0, 0, False, ""],
                [0, 0, True, None],
            ],
        },
        {"kind": "table_end", "name": "Tariffs", "rows": 1},
        {"kind": "end", "tables": 1, "rows": 1},
    ]
    return bytes(source), records


def parse(source, records):
    return _parse_output(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records).encode(),
        content=source,
        filename="tariffs.mdb",
        reader_sha256="b" * 64,
    )


def test_null_empty_leading_zero_newline_and_ieee_bytes_remain_distinct():
    source, records = source_records()
    result = parse(source, records)
    element = result["elements"][0]
    values = element["metadata_json"]["column_values"]
    assert values == ['0012\nquote: "yes"', 0.1, "", None]
    numeric = element["metadata_json"]["field_storage"][1]
    assert numeric["storage_base64"] == encoded(struct.pack("<d", 0.1))
    locator = result["chunks"][0]["locator"]
    assert locator["kind"] == "database_storage" and locator["database_page"] == 1
    assert locator["row_slot"] == 2 and locator["row_ordinal"] == 1
    assert locator["source_hash"] == hashlib.sha256(source).hexdigest()
    assert "pages" not in result and result["quality"]["physical_pages"] is False
    assert text_complete(result) is True
    assert result["database"]["queries_executed"] is False


@pytest.mark.parametrize("raw", [b"A", b"\xff\xfe\x00\x01", b"\x00\xd8", b"\xfd\xff"])
def test_corrupt_or_replacement_unicode_is_rejected(raw):
    with pytest.raises(ValueError):
        _jet4_text(raw)


def test_compressed_unicode_switches_without_losing_surrogate_pairs():
    raw = b"\xff\xfeABC\x00" + "茶😁".encode("utf-16-le") + b"\x00Z"
    assert _jet4_text(raw) == "ABC茶😁Z"
    assert _jet4_text("茶😀".encode("utf-16-le")) == "茶😀"


def test_inline_memo_is_reverified_against_source_bytes():
    source, records = source_records(memo=True)
    assert parse(source, records)["elements"][0]["metadata_json"]["column_values"][0].startswith("0012")
    wrong = deepcopy(records)
    payload = wrong[-3]["fields"][0][3]
    bad = b"\xff\xfe9999"
    payload.update(payload=encoded(bad), payload_sha256=hashlib.sha256(bad).hexdigest())
    with pytest.raises(ValueError, match="原件字节"):
        parse(source, wrong)


@pytest.mark.parametrize("object_type", [0, 2, 4, 5, 6, 7, 9, 10, 11])
def test_unparsed_reports_queries_and_linked_objects_block_completion(object_type):
    source, records = source_records(
        extra_objects=[
            {
                "kind": "object",
                "name": "Unparsed object",
                "type": object_type,
                "system": False,
                "flags": 0,
                "definition_page": 100000,
            }
        ]
    )
    result = parse(source, records)
    assert result["quality"]["complete"] is False and text_complete(result) is False
    assert result["quality"]["pending_review"][0]["locator"]["kind"] == "database_object"
    assert result["database"]["table_mapping_complete"] is True


def test_unknown_complex_cells_retain_source_bytes_and_block_completion():
    source, records = source_records()
    records[2]["columns"][1]["type"] = 18
    result = parse(source, records)
    pending = result["quality"]["pending_review"][0]
    assert pending["reason"] == "unsupported_binary_or_complex_column"
    assert pending["locator"]["source_type"] == "Complex"
    assert pending["locator"]["field_storage"]["storage_base64"] == encoded(struct.pack("<d", 0.1))
    assert text_complete(result) is False


@pytest.mark.parametrize(
    "mutation",
    ["row_count", "row_hash", "field_hash", "missing_end", "no_namespace", "out_of_range", "duplicate_table"],
)
def test_partial_mismatched_or_unsafe_exports_fail_closed(mutation):
    source, records = source_records()
    if mutation == "row_count":
        records[-2]["rows"] = 2
    elif mutation == "row_hash":
        records[-3]["storage"]["sha256"] = "0" * 64
    elif mutation == "field_hash":
        records[-3]["fields"][1][3] = encoded(struct.pack("<d", 0.2))
    elif mutation == "missing_end":
        records.pop()
    elif mutation == "no_namespace":
        records[0]["network_isolated"] = False
    elif mutation == "out_of_range":
        records[-3]["storage"]["database_page"] = 2
    else:
        records.insert(-1, deepcopy(records[2]))
    with pytest.raises(ValueError):
        parse(source, records)


def test_invalid_legacy_header_is_preserved_and_rejected_before_helper(monkeypatch):
    monkeypatch.delenv("TEA_MDB_READER_PATH", raising=False)
    with pytest.raises(ValueError, match="文件头"):
        parse_mdb(b"\x01" + bytes(8191))
    source, _ = source_records()
    with pytest.raises(ValueError, match="绝对路径"):
        parse_mdb(source)


def test_helper_path_must_be_regular_owned_absolute_and_not_world_writable(tmp_path):
    helper = tmp_path / "reader"
    helper.write_bytes(b"exe")
    helper.chmod(0o755)
    assert _trusted_executable(str(helper))[1] == hashlib.sha256(b"exe").hexdigest()
    helper.chmod(0o777)
    with pytest.raises(ValueError):
        _trusted_executable(str(helper))
    helper.chmod(0o755)
    link = tmp_path / "linked"
    link.symlink_to(helper)
    with pytest.raises(ValueError):
        _trusted_executable(str(link))
    with pytest.raises(ValueError):
        _trusted_executable("reader")


def fake_helper(tmp_path, program):
    helper = tmp_path / "helper"
    helper.write_text(f"#!{sys.executable}\n{program}\n")
    helper.chmod(0o700)
    return helper


def test_helper_timeout_kills_only_its_process_group_and_cleans_owned_source(tmp_path):
    marker = tmp_path / "pid.json"
    helper = fake_helper(
        tmp_path,
        f"import os,sys,json,time\nopen({str(marker)!r}, 'w').write(json.dumps([os.getpid(),sys.argv[2]]))\ntime.sleep(20)",
    )
    start = time.monotonic()
    with pytest.raises(ValueError, match="超时"):
        _run_reader(b"content", helper, timeout=0.3)
    assert time.monotonic() - start < 2
    pid, original = json.loads(marker.read_text())
    assert not Path(original).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_helper_output_cap_is_enforced_during_streaming(tmp_path, monkeypatch):
    monkeypatch.setattr("app.mdb_parsing.MAX_OUTPUT", 1024)
    helper = fake_helper(tmp_path, "import os\nos.write(1,b'x'*4096)")
    with pytest.raises(ValueError, match="输出超过"):
        _run_reader(b"content", helper)


def test_exited_helper_cannot_leave_a_descendant_holding_output_pipes(tmp_path):
    marker = tmp_path / "child-pid"
    helper = fake_helper(
        tmp_path,
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))",
    )
    with pytest.raises(ValueError, match="超时"):
        _run_reader(b"content", helper, timeout=0.3)
    status = Path(f"/proc/{int(marker.read_text())}/stat")
    deadline = time.monotonic() + 1
    while status.exists() and status.read_text().split()[2] != "Z" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_duplicate_json_keys_do_not_become_valid_source_values():
    source, records = source_records()
    encoded = "\n".join(json.dumps(record) for record in records)
    encoded = encoded.replace('"kind": "header"', '"kind": "header", "kind": "header"', 1)
    with pytest.raises(ValueError):
        _parse_output(encoded.encode(), content=source, filename="x.mdb", reader_sha256="b" * 64)


def test_non_finite_json_constants_are_rejected_even_in_metadata():
    source, records = source_records()
    records[0]["code_page"] = float("nan")
    with pytest.raises(ValueError):
        parse(source, records)
