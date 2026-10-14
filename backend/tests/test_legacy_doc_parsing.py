"""Source and artifact fidelity; legacy output must never imply complete law."""

import base64
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from app import legacy_doc_parsing as legacy
from app.ingestion_quality import text_complete


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fib_source(pieces, *, counts=None, properties=b""):
    # Independent MS-DOC source fixture, containing mixed compression and CPs.
    counts = counts or {
        "main": sum(len(value) if compressed else len(value) // 2 for value, compressed in pieces)
    }
    word = bytearray(2048)
    struct.pack_into("<HH", word, 0, 0xA5EC, 0xC1)
    struct.pack_into("<H", word, 10, 0x200)
    struct.pack_into("<H", word, 32, 14)
    struct.pack_into("<H", word, 62, 22)
    struct.pack_into("<I", word, 64, len(word))
    struct.pack_into("<H", word, 152, 93)
    for name, index in legacy._SUBDOCUMENTS:
        struct.pack_into("<i", word, 64 + index * 4, counts.get(name, 0))
    cps, records, offset = [0], bytearray(), 1024
    for value, compressed in pieces:
        word[offset : offset + len(value)] = value
        cps.append(cps[-1] + (len(value) if compressed else len(value) // 2))
        records.extend(struct.pack("<HIH", 0, (offset * 2) | 0x40000000 if compressed else offset, 0))
        offset += len(value) + 8
    plc = struct.pack(f"<{len(cps)}I", *cps) + records
    table = properties + b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 154 + 33 * 8, 0, len(table))
    return bytes(word), table


def piece_map(word, table):
    return legacy._fib_piece_map(word, table, source_hash="a" * 64, table_name="1Table")


def test_source_cp_mixed_unicode_and_compressed_runs_keep_exact_byte_ranges():
    word, table = fib_source(
        [(b"0012\x96X\x80\x81", True), ("茶😀\r".encode("utf-16-le"), False)], properties=b"\x01\x02\x00ab"
    )
    mapping = piece_map(word, table)
    assert mapping["pieces"][0]["text"] == "0012–X\x80\x81"
    assert mapping["pieces"][1]["text"] == "茶😀\r"
    assert mapping["pieces"][1]["cp_start"] == 8
    assert mapping["pieces"][1]["cp_end"] == 12  # astral character uses two CPs
    for piece in mapping["pieces"]:
        raw = word[piece["stream_offset"] : piece["stream_offset"] + piece["length"]]
        assert digest(raw) == piece["sha256"]
        assert "file_offset" not in piece and "page" not in piece
        pcd = piece["pcd_reference"]
        assert digest(table[pcd["stream_offset"] : pcd["stream_offset"] + pcd["length"]]) == pcd["sha256"]
    assert mapping["property_records"][0]["length"] == 5
    assert mapping["physical_pages"] is False


def test_subdocuments_and_field_markers_keep_cp_not_printed_page_numbers():
    word, table = fib_source(
        [(b"A\x13X\x14Y\x15FHF\x0c", True)], counts={"main": 6, "footnotes": 1, "headers_footers": 2}
    )
    result = piece_map(word, table)
    assert result["cp_count"] == 10
    assert next(item for item in result["subdocuments"] if item["kind"] == "footnotes")["cp_start"] == 6
    assert next(item for item in result["subdocuments"] if item["kind"] == "headers_footers")["cp_end"] == 9
    assert [item["cp"] for item in result["control_markers"]] == [1, 3, 5, 9]
    assert all(item["meaning"] == "unresolved_control_or_field" for item in result["control_markers"])


@pytest.mark.parametrize(
    "mutation",
    ["encrypted", "obfuscated", "duplicate_cp", "truncated_clx", "out_of_range", "bad_total", "bad_fib"],
)
def test_invalid_source_ranges_are_rejected_instead_of_filled_or_guessed(mutation):
    word, table = fib_source([(b"ABC", True), (b"DEF", True)])
    word, table = bytearray(word), bytearray(table)
    if mutation == "encrypted":
        struct.pack_into("<H", word, 10, 0x300)
    elif mutation == "obfuscated":
        struct.pack_into("<H", word, 10, 0x8200)
    elif mutation == "duplicate_cp":
        struct.pack_into("<I", table, 9, 0)
    elif mutation == "truncated_clx":
        table.pop()
    elif mutation == "out_of_range":
        struct.pack_into("<I", table, 5 + 12 + 2, 0x40000000 | 5000)
    elif mutation == "bad_total":
        struct.pack_into("<i", word, 76, 7)
    else:
        struct.pack_into("<H", word, 32, 99)
    with pytest.raises(ValueError):
        piece_map(bytes(word), bytes(table))


def test_decode_error_retains_raw_source_range_without_inventing_replacement_text():
    word, table = fib_source([(b"\x00\xd8", False)])
    result = piece_map(word, table)["pieces"][0]
    assert result["text"] is None and result["decode_state"] == "strict_decode_pending"
    assert result["sha256"] == digest(b"\x00\xd8")


def test_docbook_nested_footnotes_tables_headers_and_declared_break_remain_mapped():
    source = b"""<?xml version='1.0' encoding='UTF-8'?>
<!DOCTYPE book PUBLIC '-//OASIS//DTD DocBook XML V4.1.2//EN' 'https://example.invalid/not-fetched'>
<book><title>Law</title><bookinfo><author>metadata-only</author></bookinfo><chapter>
<para>Body <emphasis>word</emphasis><footnote><para>Footnote</para></footnote> tail</para>
<beginpage/><header><para>Header</para></header><footer><para>Footer</para></footer>
<informaltable><tgroup cols='3'><tbody><row><entry>0012</entry><entry/><entry morerows='1'>5%</entry></row></tbody></tgroup></informaltable>
<unknown>Preserve unknown body</unknown></chapter></book>"""
    elements, mapping = legacy._docbook_elements(source, "b" * 64)
    text = "\n".join(element.content_text for element in elements)
    assert text.count("Footnote") == 1 and "Header" in text and "Footer" in text
    assert "metadata-only" not in text and "Preserve unknown body" in text
    assert mapping["tables"][0]["rows"][0][1]["text"] == ""
    assert mapping["tables"][0]["rows"][0][2]["attributes"] == {"morerows": "1"}
    assert base64.b64decode(mapping["artifact"]["content_base64"]) == source
    assert mapping["declared_breaks"][0]["kind"] == "declared_break"
    assert mapping["declared_breaks"][0]["physical_page_verified"] is False
    assert any(node["tag"] == "footnote" for node in mapping["nodes"])
    for element in elements:
        assert element.locator["source_hash"] == "b" * 64
        assert element.locator["artifact_sha256"] == digest(source)
        assert element.locator["source_mapping"] == "unverified"
        assert "page" not in element.locator and "cp_start" not in element.locator


@pytest.mark.parametrize(
    "source",
    [
        b"<book><para>broken",
        b"<!DOCTYPE book [<!ENTITY x 'bomb'>]><book>&x;</book>",
        b"<book>\xef\xbf\xbd</book>",
        b"<book>\xff</book>",
    ],
)
def test_invalid_conversion_or_entities_never_become_accepted_text(source):
    with pytest.raises(ValueError):
        legacy._docbook_elements(source, "a" * 64)


def png_uri():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 4), "white").save(buffer, format="PNG")
    raw = buffer.getvalue()
    return raw, "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def test_wpf_artifact_events_images_and_external_reference_preserve_provenance():
    raw, uri = png_uri()
    html = f"<html><body><header><p>Header</p></header><p>Body<img src='{uri}'></p><footer>Footnote</footer><img src='https://example.invalid/image.png'></body></html>".encode()
    events = b"startDocument()\n  openHeader(type: default)\n    insertText(text: Header)\n  closeHeader()\nendDocument()\n"
    elements, result = legacy._wpf_artifacts(html, events, "c" * 64)
    assert "Header" in "\n".join(element.content_text for element in elements)
    assert "Footnote" in "\n".join(element.content_text for element in elements)
    image = result["images"][0]
    assert image["image_sha256"] == digest(raw)
    assert image["width"] == 8 and image["height"] == 4 and image["state"] == "vision_pending"
    assert image["locator"]["artifact_sha256"] == digest(html)
    assert result["images"][1]["image_bytes_verified"] is False
    assert "content_base64" not in result["images"][1]
    for event in result["events"]:
        raw_line = events[event["artifact_byte_offset"] : event["artifact_byte_offset"] + event["length"]]
        assert event["sha256"] == digest(raw_line)
    assert result["physical_pages"] is False


@pytest.mark.parametrize(
    "uri", ["data:image/png;base64,YWJj", "data:image/jpeg;base64,iVBORw0KGgo=", "data:image/png;base64,ab=="]
)
def test_invalid_image_signature_and_noncanonical_base64_are_rejected(uri):
    with pytest.raises(ValueError):
        legacy._inline_image(uri, {})


def test_image_size_pixels_and_aggregate_limits(monkeypatch):
    raw, uri = png_uri()
    monkeypatch.setattr(legacy, "MAX_IMAGE_PIXELS", 31)
    with pytest.raises(ValueError, match="像素"):
        legacy._inline_image(uri, {})
    monkeypatch.setattr(legacy, "MAX_IMAGE_PIXELS", 32)
    monkeypatch.setattr(legacy, "MAX_OBJECTS", len(raw))
    with pytest.raises(ValueError, match="总字节"):
        legacy._wpf_artifacts(
            f"<html><body><p>x</p><img src='{uri}'><img src='{uri}'></body></html>".encode(),
            b"startDocument()\nendDocument()",
            "a" * 64,
        )


def test_partial_layout_and_objects_block_text_complete_despite_successful_conversion():
    elements, mapping = legacy._docbook_elements(
        b"<book><chapter><para>Complete converted words.</para></chapter></book>", "a" * 64
    )
    result = legacy._result(
        elements,
        source_hash="a" * 64,
        filename="law.doc",
        format_name="doc",
        tool_proof={},
        mapping={"conversion": mapping},
    )
    assert result["chunks"] and result["quality"]["text_extracted"] is True
    assert text_complete(result) is False and result["quality"]["complete"] is False
    assert result["legacy"]["independent_character_evidence"] is False
    assert "pages" not in result


def test_multiple_artifact_paragraphs_keep_each_xpath_and_source_hash_in_chunks():
    elements, mapping = legacy._docbook_elements(
        b"<book><chapter><para>First source paragraph.</para><para>Second source paragraph.</para></chapter></book>",
        "a" * 64,
    )
    result = legacy._result(
        elements,
        source_hash="a" * 64,
        filename="law.doc",
        format_name="doc",
        tool_proof={},
        mapping={"conversion": mapping},
    )
    assert len(result["chunks"]) == 2
    assert result["chunks"][0]["locator"]["xpath"] != result["chunks"][1]["locator"]["xpath"]
    assert all(chunk["locator"]["source_hash"] == "a" * 64 for chunk in result["chunks"])


def test_chunk_anchors_separate_source_artifact_xpath_and_unknown_kinds():
    from app.vendor.geo_parser.document_ingestion import _same_locator_anchor

    locator = {
        "kind": "conversion_artifact",
        "source_hash": "a" * 64,
        "artifact_sha256": "b" * 64,
        "xpath": "/book[1]/para[1]",
    }
    assert _same_locator_anchor(locator, dict(locator)) is True
    for field, value in (
        ("source_hash", "c" * 64),
        ("artifact_sha256", "d" * 64),
        ("xpath", "/book[1]/para[2]"),
    ):
        assert _same_locator_anchor(locator, {**locator, field: value}) is False
    assert _same_locator_anchor({"kind": "unknown"}, {"kind": "unknown"}) is False


def test_converted_table_caption_footnote_and_empty_merged_cell_are_retained():
    xml = b"<book><chapter><informaltable><title>Caption</title><tgroup><tbody><row><entry namest='c1' nameend='c2'/><entry>HS0012<footnote><para>Note</para></footnote></entry></row></tbody></tgroup></informaltable></chapter></book>"
    elements, mapping = legacy._docbook_elements(xml, "a" * 64)
    assert "Caption" in elements[0].content_text and elements[0].content_text.count("Note") == 1
    assert mapping["tables"][0]["rows"][0][0]["text"] == ""
    assert mapping["tables"][0]["rows"][0][0]["attributes"] == {"namest": "c1", "nameend": "c2"}
    html = b"<html><body><table><caption>Caption</caption><tr><td colspan='2'></td><td>0012</td></tr></table></body></html>"
    _, converted = legacy._wpf_artifacts(html, b"startDocument()\nendDocument()", "b" * 64)
    assert converted["tables"][0]["rows"][0][0]["attributes"] == {"colspan": "2"}
    assert converted["tables"][0]["rows"][0][0]["text"] == ""


def test_real_docbook_table_inside_para_maps_cells_without_duplicate_body():
    xml = b"<book><chapter><para>before<informaltable><tgroup><tbody><row><entry>0012</entry><entry/></row></tbody></tgroup></informaltable>after</para></chapter></book>"
    elements, converted = legacy._docbook_elements(xml, "a" * 64)
    assert len(converted["tables"]) == 1
    assert converted["tables"][0]["text_in_ancestor_element"] is True
    assert converted["tables"][0]["rows"][0][1]["text"] == ""
    assert "\n".join(element.content_text for element in elements) == "before0012after"
    html = b"<html><body><header>before<table><tr><td>0012</td><td/></tr></table>after</header></body></html>"
    _, converted = legacy._wpf_artifacts(html, b"startDocument()\nendDocument()", "b" * 64)
    assert len(converted["tables"]) == 1
    assert converted["tables"][0]["text_in_ancestor_element"] is True
    assert converted["tables"][0]["rows"][0][1]["text"] == ""


def test_untrusted_relative_writable_and_symlink_tools_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        legacy._trusted_file("relative-tool", executable=True)
    path = tmp_path / "tool"
    path.write_bytes(b"trusted")
    path.chmod(0o777)
    with pytest.raises(ValueError):
        legacy._trusted_file(path, executable=True)
    path.chmod(0o755)
    link = tmp_path / "tool-link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        legacy._trusted_file(link, executable=True)
    _, proof = legacy._trusted_file(path, executable=True)
    assert proof["sha256"] == digest(b"trusted")


def _script(path, code):
    path.write_text(f"#!{sys.executable}\n{code}")
    path.chmod(0o700)
    return path


def test_converter_output_and_wall_clock_are_bounded_and_children_cleaned(tmp_path, monkeypatch):
    # Fake operator launcher bypasses isolation only in this runner unit test.
    # Real syscall/parent-death isolation is verified below against compiled C.
    launcher = _script(tmp_path / "launcher", "import os,sys\nos.execv(sys.argv[3],[sys.argv[3]])\n")
    tool = _script(tmp_path / "output", "import sys\nsys.stdout.buffer.write(b'x'*10000)\n")
    monkeypatch.setattr(legacy, "MAX_OUTPUT", 100)
    with pytest.raises(ValueError, match="输出"):
        legacy._run_converter(b"original", launcher, tool, tmp_path, "docbook", timeout=2)
    tool = _script(tmp_path / "sleep", "import time\ntime.sleep(5)\n")
    start = time.monotonic()
    with pytest.raises(ValueError, match="超时"):
        legacy._run_converter(b"original", launcher, tool, tmp_path, "docbook", timeout=0.25)
    assert time.monotonic() - start < 2


def test_misconfigured_converter_descendants_are_reaped_on_timeout(tmp_path):
    pids = tmp_path / "pids"
    launcher = _script(tmp_path / "launcher", "import os,sys\nos.execv(sys.argv[3],[sys.argv[3]])\n")
    executable = _script(
        tmp_path / "converter",
        f"import os,time\nchild=os.fork()\nopen({str(pids)!r},'a').write(str(os.getpid())+'\\n')\ntime.sleep(30)\n",
    )
    with pytest.raises(ValueError, match="超时"):
        legacy._run_converter(b"original", launcher, executable, tmp_path, "docbook", timeout=0.25)
    assert len(pids.read_text().splitlines()) == 2
    deadline = time.monotonic() + 2
    for value in pids.read_text().splitlines():
        while time.monotonic() < deadline:
            status = Path(f"/proc/{int(value)}/stat")
            try:
                state = status.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break  # Reaping can finish between locating and reading /proc.
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("owned converter descendant survived timeout")


@pytest.fixture
def native_launcher(tmp_path):
    cc = shutil.which("cc")
    if not cc or sys.platform != "linux":
        pytest.skip("C offline launcher requires Linux and project-local compiler")
    source = Path(__file__).parents[2] / "scripts/tools/legacy-doc/offline-converter.c"
    output = tmp_path / "offline-converter"
    result = subprocess.run(
        [cc, "-Wall", "-Wextra", "-Werror", "-O2", str(source), "-o", str(output)],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode()
    return output


def test_native_launcher_denies_network_and_process_creation(native_launcher, tmp_path):
    executable = _script(
        tmp_path / "converter",
        """import json,os,socket
result = {}
for name, call in [('socket',lambda:socket.socket(socket.AF_INET,socket.SOCK_STREAM)),('unix_socket',lambda:socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)),('fork',os.fork)]:
 try:call(); result[name] = 'unexpected-success'
 except OSError as exc:result[name] = exc.errno
print(json.dumps(result))
""",
    )
    # Environments that prohibit user namespaces reject before any converter.
    try:
        output = legacy._run_converter(
            b"original", native_launcher, executable, tmp_path, "docbook", timeout=5
        )
    except ValueError:
        probe = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "true"], capture_output=True)
        if probe.returncode:
            pytest.skip("Linux user/network namespace unavailable")
        raise
    assert json.loads(output) == {"socket": 1, "unix_socket": 1, "fork": 1}


def test_native_converter_dies_when_its_bound_parent_is_killed(native_launcher, tmp_path):
    child_pid = tmp_path / "child-pid"
    executable = _script(
        tmp_path / "converter",
        f"import os,time\nopen({str(child_pid)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n",
    )
    original = tmp_path / "original"
    original.write_bytes(b"original")
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time; subprocess.Popen([sys.argv[1],str(os.getpid()),'docbook',*sys.argv[2:]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(30)",
            str(native_launcher),
            str(executable),
            str(tmp_path),
            str(original),
        ],
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    try:
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not child_pid.exists():
            probe = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--net", "true"], capture_output=True
            )
            if probe.returncode:
                pytest.skip("Linux user/network namespace unavailable")
            pytest.fail("native converter did not start")
        pid = int(child_pid.read_text())
        parent.kill()
        parent.wait(timeout=2)
        while time.monotonic() < deadline:
            status = Path(f"/proc/{pid}/stat")
            try:
                state = status.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                # The child can exit between opening /proc and reading stat.
                # Either disappearance proves the owned converter has stopped.
                break
            if state == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("converter survived its bounded parent")
    finally:
        try:
            os.killpg(parent.pid, 9)
        except ProcessLookupError:
            pass
        parent.wait()
