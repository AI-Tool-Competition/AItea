import json
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


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


class Database:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(exist_ok=True)
        self.path = self.data_dir / "tea.sqlite3"
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (1, now()))

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self):
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
