"""A missing/replaced volume must not be silently recreated by live workers."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from app.shared.infrastructure.persistence.database import SCHEMA, Database, DatabaseUnavailable
from fastapi.testclient import TestClient


@pytest.mark.parametrize("missing", ["database", "files", "directory"])
def test_lost_storage_is_not_recreated_by_parallel_workers(tmp_path, missing):
    database = Database(tmp_path / "store")
    moved = {"database": database.path, "files": database.files_dir, "directory": database.data_dir}[missing]
    saved = tmp_path / "preserved"
    moved.rename(saved)

    def attempt(_):
        with pytest.raises(DatabaseUnavailable):
            with database.transaction():
                pytest.fail("A writer entered a missing store")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(attempt, range(32)))
    assert not moved.exists()
    assert saved.exists()
    # Returning the same directory/file identity permits normal recovery;
    # the failed writers did not keep the FIFO gate locked.
    saved.rename(moved)
    database.check_available()


def test_replaced_database_requires_new_instance_without_mutating_replacement(tmp_path):
    database = Database(tmp_path / "store")
    replacement = Database(tmp_path / "replacement")
    raw = replacement.path.read_bytes()
    replacement.path.replace(database.path)
    with pytest.raises(DatabaseUnavailable, match="changed"):
        database.connect()
    assert database.path.read_bytes() == raw
    restored = Database(database.data_dir, require_existing=True)
    restored.check_available()


def test_truncated_database_is_rejected_without_reinitialization(tmp_path):
    database = Database(tmp_path / "store")
    database.path.write_bytes(b"")
    with pytest.raises(DatabaseUnavailable):
        database.connect()
    assert database.path.read_bytes() == b""


@pytest.mark.parametrize("replace", [False, True])
def test_storage_changed_during_transaction_is_rolled_back(tmp_path, replace):
    database = Database(tmp_path / "store")
    with database.transaction() as conn:
        conn.execute("CREATE TABLE existing_probe(value TEXT)")
    replacement = Database(tmp_path / "replacement") if replace else None
    with pytest.raises(DatabaseUnavailable):
        with database.transaction() as conn:
            conn.execute("INSERT INTO existing_probe VALUES('must roll back')")
            database.data_dir.rename(tmp_path / "preserved")
            if replacement:
                replacement.data_dir.rename(database.data_dir)
    if replacement:
        database.data_dir.rename(replacement.data_dir)
        replacement.check_available()
    (tmp_path / "preserved").rename(database.data_dir)
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM existing_probe").fetchone()[0] == 0


@pytest.mark.parametrize("present", ["missing", "empty", "uninitialized", "without_files"])
def test_require_existing_never_initializes_missing_or_empty_storage(tmp_path, present):
    root = tmp_path / "store"
    if present != "missing":
        root.mkdir()
        if present != "without_files":
            (root / "files").mkdir()
        (root / "tea.sqlite3").write_bytes(b"" if present == "empty" else b"\0" * 4096)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(DatabaseUnavailable):
        Database(root, require_existing=True)
    assert before == {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    if present == "missing":
        assert not root.exists()
    if present == "without_files":
        assert not (root / "files").exists()


def test_database_uri_preserves_literal_query_fragment_and_unicode_in_path(tmp_path):
    database = Database(tmp_path / "法律 ?#原文")
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone()
    assert database.path.exists()


@pytest.mark.parametrize("missing", ["users", "documents", "sessions", "cases"])
def test_existing_startup_and_health_reject_missing_base_tables_without_repair(tmp_path, missing):
    database = Database(tmp_path / "store")
    with closing(sqlite3.connect(database.path)) as conn:
        conn.execute(f'DROP TABLE "{missing}"')
        conn.commit()
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    with pytest.raises(DatabaseUnavailable):
        database.check_available()
    with pytest.raises(DatabaseUnavailable):
        Database(database.data_dir, require_existing=True)
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before


def test_existing_startup_allows_normal_versioned_migration(tmp_path):
    root = tmp_path / "store"
    (root / "files").mkdir(parents=True)
    with closing(sqlite3.connect(root / "tea.sqlite3")) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO schema_migrations VALUES(1,'historical')")
        conn.commit()
    database = Database(root, require_existing=True)
    database.check_available()
    with closing(database.connect()) as conn:
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=16").fetchone()


def test_health_and_private_writes_refuse_missing_storage_without_creating_database(tmp_path):
    from app.interfaces.http.factory import create_app

    app = create_app(tmp_path / "store", enable_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/tea/health").status_code == 200
        database = app.state.service.db
        database.path.rename(tmp_path / "preserved.sqlite3")
        for response in [client.get("/api/tea/health"), client.post("/api/tea/auth/demo")]:
            assert response.status_code == 503
            error = response.json()["error"]
            assert error["code"] == "database_unavailable" and error["request_id"]
            assert str(tmp_path) not in response.text
        assert not database.path.exists()


def test_health_detects_removed_schema_without_repair(tmp_path):
    from app.interfaces.http.factory import create_app

    app = create_app(tmp_path / "store", enable_scheduler=False)
    with app.state.service.db.transaction() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=1")
    before = hashlib.sha256(app.state.service.db.path.read_bytes()).hexdigest()
    with TestClient(app) as client:
        assert client.get("/api/tea/health").json()["error"]["code"] == "database_unavailable"
    assert hashlib.sha256(app.state.service.db.path.read_bytes()).hexdigest() == before


def test_existing_app_mode_rejects_missing_directory_before_service_initialization(tmp_path, monkeypatch):
    from app.interfaces.http.factory import create_app

    root = tmp_path / "missing"
    monkeypatch.setenv("TEA_REQUIRE_EXISTING_DATA", "true")
    with pytest.raises(DatabaseUnavailable):
        create_app(root, enable_scheduler=False)
    assert not root.exists()


def test_existing_app_mode_reopens_existing_database_and_rejects_invalid_setting(tmp_path, monkeypatch):
    from app.interfaces.http.factory import create_app

    root = tmp_path / "store"
    first = create_app(root, enable_scheduler=False)
    with first.state.service.db.transaction() as conn:
        conn.execute("CREATE TABLE existing_probe(value TEXT)")
        conn.execute("INSERT INTO existing_probe VALUES('retained')")
    monkeypatch.setenv("TEA_REQUIRE_EXISTING_DATA", "true")
    second = create_app(root, enable_scheduler=False)
    with closing(second.state.service.db.connect()) as conn:
        assert conn.execute("SELECT value FROM existing_probe").fetchone()[0] == "retained"
    monkeypatch.setenv("TEA_REQUIRE_EXISTING_DATA", "ture")
    with pytest.raises(ValueError, match="TEA_REQUIRE_EXISTING_DATA"):
        create_app(root, enable_scheduler=False)
