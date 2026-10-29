import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest


spec = importlib.util.spec_from_file_location(
    "import_china_pack", Path(__file__).resolve().parents[2] / "scripts" / "import_china_pack.py"
)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def make_index(data):
    path = data / "legal-library" / "index.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE records(" + ",".join(f"{column} TEXT" for column in importer.COLUMNS) + ")"
        )
        conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        conn.executemany(
            "INSERT INTO metadata VALUES(?,?)", [("fingerprint", "prior"), ("record_count_SG", "1")]
        )
        conn.execute("CREATE VIRTUAL TABLE record_search USING fts5(title,text,translation,cjk_terms)")
        conn.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "SG:existing",
                "SG",
                "sg-doc",
                "1",
                "Food law",
                "1",
                "food",
                "",
                "https://example.gov/food",
                "original.pdf",
                "original-hash",
                "law",
                "en",
                "unreviewed",
            ),
        )
        conn.execute("INSERT INTO record_search(rowid,title,text) VALUES(1,'Food law','food')")
    return path


def make_pack(tmp, *, wrong_hash=False, saved_text=None):
    text = "第四条　进出口货物收发货人应当依法办理备案。"
    original = text.encode()
    sha = hashlib.sha256(original).hexdigest()
    original_path = f"原件/{sha}.html"
    files = {
        original_path: original,
        "整理正文/doc.txt": original if saved_text is None else saved_text.encode(),
        importer.DOCS: (
            json.dumps(
                {
                    "document_id": "CN-doc",
                    "version_id": "v1",
                    "original_text_path": "整理正文/doc.txt",
                    "official_title": "测试规定",
                    "source_type": "部门规章",
                    "status": "recognized",
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode(),
        importer.SOURCES: (
            json.dumps(
                {
                    "source_id": "source",
                    "original_path": original_path,
                    "original_file_hash": sha,
                    "url": "https://example.gov/law",
                    "status": "recognized",
                }
            )
            + "\n"
        ).encode(),
        importer.UNITS: (
            json.dumps(
                {
                    "record_id": "CN-doc-R1",
                    "document_id": "CN-doc",
                    "version_id": "v1",
                    "source_id": "source",
                    "source_sha256": "wrong" if wrong_hash else sha,
                    "original_text": text,
                    "char_start": 0,
                    "char_end": len(text),
                    "article_anchor": "第四条",
                    "structure_level": "article",
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode(),
    }
    files[importer.CHECKSUMS] = "\n".join(
        json.dumps(
            {
                "path": name,
                "bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            },
            ensure_ascii=False,
        )
        for name, value in files.items()
    ).encode()
    archive = tmp / "pack.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, value in files.items():
            z.writestr(f"{importer.ROOT}/{name}", value)
    return archive


def test_import_keeps_other_country_and_draft_and_is_repeatable(tmp_path):
    data = tmp_path / "data"
    db = make_index(data)
    archive = make_pack(tmp_path)
    first = importer.import_pack(archive, data)
    assert first["changed"] and Path(first["backup"]).is_file()
    assert first["location_validation"] == {"exact_character_range": 1}
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT country,status,source_hash,article FROM records WHERE country='CN'"
        ).fetchone() == (
            "CN",
            "local_source_unreviewed",
            hashlib.sha256("第四条　进出口货物收发货人应当依法办理备案。".encode()).hexdigest(),
            "4",
        )
        assert conn.execute("SELECT id,text FROM records WHERE country='SG'").fetchone() == (
            "SG:existing",
            "food",
        )
        assert (
            conn.execute("SELECT count(*) FROM record_search WHERE record_search MATCH '备案'").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) FROM record_search WHERE record_search MATCH 'food'").fetchone()[0]
            == 1
        )
        before = conn.execute("SELECT rowid,* FROM records ORDER BY rowid").fetchall()
    second = importer.import_pack(archive, data)
    assert not second["changed"] and second["backup"] is None
    assert first["fingerprint"] == second["fingerprint"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT rowid,* FROM records ORDER BY rowid").fetchall() == before


def test_bad_source_hash_does_not_change_existing_index(tmp_path):
    data = tmp_path / "data"
    db = make_index(data)
    archive = make_pack(tmp_path, wrong_hash=True)
    with pytest.raises(ValueError, match="Original hash mismatch"):
        importer.import_pack(archive, data)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT country FROM records").fetchall() == [("SG",)]
        assert dict(conn.execute("SELECT * FROM metadata"))["fingerprint"] == "prior"


@pytest.mark.parametrize(
    ("saved_text", "expected_status"),
    [
        ("另一个无法对应的正文", "source_location_unverified"),
        ("第四条\n进出口货物收发货人应当依法办理备案。", "local_source_unreviewed"),
    ],
)
def test_text_location_status_does_not_treat_declared_record_as_verified(
    tmp_path, saved_text, expected_status
):
    data = tmp_path / "data"
    db = make_index(data)
    importer.import_pack(make_pack(tmp_path, saved_text=saved_text), data)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM records WHERE country='CN'").fetchone()[0] == expected_status


@pytest.mark.parametrize("entry", ["../escape", "/absolute", "茶渡中国法律资料/../../escape"])
def test_reject_archive_traversal(tmp_path, entry):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(entry, "danger")
    with pytest.raises(ValueError):
        importer.extract_verified(archive, tmp_path / "destination")
    assert not (tmp_path / "escape").exists()


def test_reject_zip_symlink(tmp_path):
    archive = tmp_path / "unsafe.zip"
    info = zipfile.ZipInfo("茶渡中国法律资料/link")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(info, "/tmp/escape")
    with pytest.raises(ValueError, match="symlink"):
        importer.extract_verified(archive, tmp_path / "destination")
