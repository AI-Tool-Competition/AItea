"""Large source mapping stays file-backed, immutable and separately bounded."""

import hashlib
from pathlib import Path

import pytest

from app.docx_archive_parsing import MAX_ARCHIVE_BYTES, parse_archive_docx_bounded
from app.docx_parsing import parse_docx
from app.parsing import parse_bounded
from test_docx_parsing import _package, _p


def original(tmp_path, data):
    path = tmp_path / "source.docx"
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def test_file_source_preserves_exact_native_mapping_and_original(tmp_path):
    content = _package(_p("Full text and exact source locator."))
    path, digest = original(tmp_path, content)
    before = path.stat()
    manifest = parse_archive_docx_bounded(path, source_hash=digest, size=len(content))
    marker = manifest.pop("archive_docx")
    assert marker == {"version": "file-source-docx-v1", "source_hash": digest, "size": len(content)}
    assert manifest == parse_docx(content)
    assert path.read_bytes() == content and path.stat().st_mtime_ns == before.st_mtime_ns


def test_large_package_uses_separate_limit_without_altering_uploads(tmp_path):
    # Deliberately uncompressible stored binary keeps the actual file >20 MiB;
    # its unknown content is mapped as pending review, never fake legal text.
    import io
    import zipfile

    small = _package(_p("Native text"))
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(small)) as source, zipfile.ZipFile(stream, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr("word/embedded-source.bin", b"\x00" * (21 * 1024 * 1024))
    content = stream.getvalue()
    path, digest = original(tmp_path, content)
    with pytest.raises(ValueError, match="20 MB"):
        parse_bounded("source.docx", content)
    manifest = parse_archive_docx_bounded(path, source_hash=digest, size=len(content))
    assert manifest["source_hash"] == digest and manifest["archive_docx"]["size"] == len(content)
    assert manifest["chunks"] and not manifest["quality"]["complete"]
    assert "unmapped_binary_part" in manifest["quality"]["review_reasons"]
    assert any(part["size"] == 21 * 1024 * 1024 for part in manifest["package_parts"])


@pytest.mark.parametrize("size", [0, -1, True, MAX_ARCHIVE_BYTES + 1])
def test_file_source_size_is_bounded_before_child_or_file_read(tmp_path, size):
    with pytest.raises(ValueError, match="64 MiB"):
        parse_archive_docx_bounded(tmp_path / "missing.docx", source_hash="a" * 64, size=size)


def test_file_source_rejects_wrong_hash_size_and_symlink(tmp_path):
    content = _package(_p("Verified source"))
    path, digest = original(tmp_path, content)
    with pytest.raises(ValueError):
        parse_archive_docx_bounded(path, source_hash="0" * 64, size=len(content))
    with pytest.raises(ValueError):
        parse_archive_docx_bounded(path, source_hash=digest, size=len(content) + 1)
    link = Path(tmp_path / "link.docx")
    link.symlink_to(path)
    with pytest.raises(ValueError):
        parse_archive_docx_bounded(link, source_hash=digest, size=len(content))
    assert path.read_bytes() == content
