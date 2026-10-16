import json
import os
import sqlite3
from collections import deque
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Lock, get_ident
from uuid import uuid4
from weakref import WeakValueDictionary


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL,email TEXT UNIQUE,password_hash TEXT,is_demo INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS templates(owner_id TEXT NOT NULL REFERENCES users(id),id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(owner_id,id,version));
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),title TEXT NOT NULL,company TEXT NOT NULL,product TEXT NOT NULL,mode_id TEXT NOT NULL,template TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cases_owner ON cases(owner_id,updated_at);
CREATE TABLE IF NOT EXISTS task_states(case_id TEXT NOT NULL REFERENCES cases(id),task_id TEXT NOT NULL,stage_id TEXT NOT NULL,status TEXT NOT NULL,needs_review INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,values_json TEXT NOT NULL DEFAULT '{}',evidence_ids TEXT NOT NULL DEFAULT '[]',confirmed INTEGER NOT NULL DEFAULT 0,verification TEXT,PRIMARY KEY(case_id,task_id));
CREATE TABLE IF NOT EXISTS facts(case_id TEXT NOT NULL REFERENCES cases(id),key TEXT NOT NULL,label TEXT NOT NULL,value TEXT NOT NULL,source TEXT NOT NULL,confirmed INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(case_id,key));
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),stage_id TEXT NOT NULL,task_id TEXT,role TEXT NOT NULL,content TEXT NOT NULL,citations TEXT NOT NULL DEFAULT '[]',trace TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_case ON messages(case_id,stage_id,created_at);
CREATE TABLE IF NOT EXISTS message_requests(case_id TEXT NOT NULL REFERENCES cases(id),client_id TEXT NOT NULL,payload_hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(case_id,client_id));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),type TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_case ON events(case_id,created_at);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),case_id TEXT REFERENCES cases(id),parent_id TEXT REFERENCES documents(id),title TEXT NOT NULL,filename TEXT NOT NULL,role TEXT NOT NULL,status TEXT NOT NULL,mime_type TEXT NOT NULL,size INTEGER NOT NULL,version INTEGER NOT NULL,content_hash TEXT NOT NULL,storage_path TEXT NOT NULL,tags TEXT NOT NULL,source_url TEXT,error TEXT,manifest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS documents_scope ON documents(owner_id,case_id,created_at);
CREATE INDEX IF NOT EXISTS documents_source ON documents(owner_id,source_url,content_hash);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),case_id TEXT NOT NULL REFERENCES cases(id),task_id TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,version INTEGER NOT NULL,confirmed INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS artifacts_case ON artifacts(case_id);
CREATE TABLE IF NOT EXISTS artifact_versions(artifact_id TEXT NOT NULL REFERENCES artifacts(id),version INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(artifact_id,version));
CREATE TABLE IF NOT EXISTS verifications(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,url TEXT NOT NULL,enabled INTEGER NOT NULL,interval_hours INTEGER NOT NULL,tags TEXT NOT NULL,last_fetched_at TEXT,last_attempt_at TEXT,status TEXT NOT NULL,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sources_owner ON sources(owner_id);
CREATE TABLE IF NOT EXISTS auth_attempts(ip TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS auth_attempts_ip ON auth_attempts(ip,at);
"""


class _WriteGate:
    """FIFO admission for one process's writers to the same database file."""

    def __init__(self):
        self._condition = Condition(Lock())
        self._queue = deque()
        self._owner = None

    @contextmanager
    def hold(self):
        ticket, thread = object(), get_ident()
        with self._condition:
            if self._owner == thread:
                # A second connection cannot write while this thread owns the
                # first one. Reject instead of sleeping until SQLite times out.
                raise RuntimeError("Nested database transaction: pass the existing connection")
            self._queue.append(ticket)
            try:
                self._condition.wait_for(lambda: self._owner is None and self._queue[0] is ticket)
            except BaseException:
                self._queue.remove(ticket)
                self._condition.notify_all()
                raise
            self._queue.popleft()
            self._owner = thread
        try:
            yield
        finally:
            with self._condition:
                self._owner = None
                self._condition.notify_all()


_WRITE_GATES = WeakValueDictionary()
_WRITE_GATES_LOCK = Lock()


def _reset_write_gates():
    """A fork child must not inherit a parent's waiting or owned thread lock."""
    global _WRITE_GATES, _WRITE_GATES_LOCK
    _WRITE_GATES = WeakValueDictionary()
    _WRITE_GATES_LOCK = Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_write_gates)


def _writer_gate(path):
    identity = Path(path).resolve()
    with _WRITE_GATES_LOCK:
        gate = _WRITE_GATES.get(identity)
        if gate is None:
            gate = _WriteGate()
            _WRITE_GATES[identity] = gate
        return gate


class Database:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(exist_ok=True)
        self.path = self.data_dir / "tea.sqlite3"
        with _writer_gate(self.path).hold():
            with closing(self.connect()) as conn:
                conn.executescript(SCHEMA)
                conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (1, now()))
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=16").fetchone():
                        conn.execute(
                            """CREATE TABLE user_access(
                                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                                can_login INTEGER NOT NULL CHECK(can_login IN (0,1)),
                                is_admin INTEGER NOT NULL CHECK(is_admin IN (0,1)),
                                revision INTEGER NOT NULL CHECK(revision>=1),
                                CHECK(is_admin=0 OR can_login=1))"""
                        )
                        conn.execute(
                            "INSERT INTO user_access(user_id,can_login,is_admin,revision) "
                            "SELECT id,1,0,1 FROM users"
                        )
                        conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (16, now()))
                    elif not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_access'"
                    ).fetchone():
                        raise RuntimeError("account access schema is incomplete")
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise

    def connect(self):
        from .system_library import register_source_guard

        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            register_source_guard(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            return conn
        except BaseException:
            conn.close()
            raise

    @contextmanager
    def transaction(self):
        with _writer_gate(self.path).hold():
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
