import os
import sqlite3
import stat
from collections import deque
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Condition, Lock, get_ident
from weakref import WeakValueDictionary

from app.shared.domain.values import now

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

# These tables existed in the base schema. Their absence is damaged storage,
# not a later migration that can be repaired by creating empty replacement tables.
_BASE_TABLES = frozenset(
    {
        "schema_migrations",
        "users",
        "sessions",
        "templates",
        "cases",
        "task_states",
        "facts",
        "messages",
        "message_requests",
        "events",
        "documents",
        "artifacts",
        "artifact_versions",
        "verifications",
        "sources",
        "auth_attempts",
    }
)


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


class DatabaseUnavailable(sqlite3.OperationalError):
    """The initialized storage disappeared or was replaced; do not create a new store."""


class Database:
    def __init__(self, data_dir: Path, *, require_existing=False):
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.path = self.data_dir / "tea.sqlite3"
        self._storage_identity = None
        if require_existing:
            self._storage_identity = self._disk_identity()
            try:
                with closing(sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
                    self._check_base_schema(conn)
            except sqlite3.DatabaseError as error:
                raise DatabaseUnavailable("Existing application database is unavailable") from error
            self._check_identity()
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.files_dir.mkdir(exist_ok=True)
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
                    self._migrate_agent_runtime(conn)
                    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=25").fetchone():
                        conn.execute(
                            "ALTER TABLE agent_memory_entries ADD COLUMN context_hash TEXT NOT NULL DEFAULT ''"
                        )
                        conn.execute(
                            "ALTER TABLE agent_session_summaries ADD COLUMN context_hash TEXT NOT NULL DEFAULT ''"
                        )
                        conn.execute("INSERT INTO schema_migrations VALUES(25,?)", (now(),))
                    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=27").fetchone():
                        conn.execute("""CREATE TABLE agent_conversation_context(
                            conversation_id TEXT PRIMARY KEY,
                            owner_id TEXT NOT NULL REFERENCES users(id),
                            case_id TEXT NOT NULL REFERENCES cases(id),stage_id TEXT NOT NULL,
                            signature TEXT NOT NULL,source_message_id TEXT NOT NULL REFERENCES messages(id),
                            messages TEXT NOT NULL,updated_at TEXT NOT NULL)""")
                        conn.execute("INSERT INTO schema_migrations VALUES(27,?)", (now(),))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            if self._storage_identity is None:
                self._storage_identity = self._disk_identity()
            else:
                self._check_identity()

    @staticmethod
    def _migrate_agent_runtime(conn):
        """Additive migration, shared by new and existing stores; no business-data reset."""
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version=24").fetchone():
            return
        statements = (
            """CREATE TABLE agent_runs(
                id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),
                case_id TEXT NOT NULL REFERENCES cases(id),stage_id TEXT NOT NULL,
                conversation_id TEXT,client_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                case_revision INTEGER NOT NULL,status TEXT NOT NULL,lease_until TEXT NOT NULL,
                result TEXT,trace TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,UNIQUE(case_id,client_id))""",
            "CREATE UNIQUE INDEX agent_active_case ON agent_runs(case_id) WHERE status='running'",
            """CREATE TABLE agent_tool_receipts(
                run_id TEXT NOT NULL REFERENCES agent_runs(id),call_id TEXT NOT NULL,
                name TEXT NOT NULL,input_hash TEXT NOT NULL,status TEXT NOT NULL,
                result TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(run_id,call_id))""",
            """CREATE TABLE agent_memory_entries(
                id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),
                case_id TEXT NOT NULL REFERENCES cases(id),stage_id TEXT NOT NULL,
                conversation_id TEXT,kind TEXT NOT NULL,content TEXT NOT NULL,
                revision INTEGER NOT NULL,case_revision INTEGER NOT NULL,
                source_message_id TEXT REFERENCES messages(id),deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
            "CREATE INDEX agent_memory_scope ON agent_memory_entries(case_id,stage_id,deleted,updated_at,id)",
            """CREATE TABLE agent_session_summaries(
                conversation_id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),
                stage_id TEXT NOT NULL,case_revision INTEGER NOT NULL,
                summary TEXT NOT NULL,source_message_id TEXT NOT NULL REFERENCES messages(id),
                updated_at TEXT NOT NULL)""",
        )
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_migrations VALUES(24,?)", (now(),))

    def _disk_identity(self):
        try:
            database, files = self.path.stat(), self.files_dir.stat()
            if (
                not stat.S_ISREG(database.st_mode)
                or database.st_size < 100
                or not stat.S_ISDIR(files.st_mode)
            ):
                raise DatabaseUnavailable("Application storage is missing or incomplete")
            return database.st_dev, database.st_ino, files.st_dev, files.st_ino
        except OSError as error:
            raise DatabaseUnavailable("Application storage is unavailable") from error

    def _check_identity(self):
        if self._disk_identity() != self._storage_identity:
            raise DatabaseUnavailable("Application storage changed; verify recovery before restarting")

    @staticmethod
    def _check_base_schema(conn):
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if (
            not _BASE_TABLES <= tables
            or not conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone()
        ):
            raise DatabaseUnavailable("Application database base schema is incomplete")

    def check_available(self):
        """Bounded readiness check; does not repair, initialize or scan the corpus."""
        try:
            with closing(self.connect()) as conn:
                self._check_base_schema(conn)
        except sqlite3.DatabaseError as error:
            raise DatabaseUnavailable("Application database is unavailable") from error

    def connect(self):
        from app.contexts.documents.application.system_library import register_source_guard

        initializing = self._storage_identity is None
        if not initializing:
            self._check_identity()
        # Only explicit initialization may create a database. A disappearing
        # mount or removed file must not become a fresh empty SQLite store.
        mode = "rwc" if initializing else "rw"
        conn = sqlite3.connect(
            self.path.absolute().as_uri() + "?mode=" + mode,
            uri=True,
            timeout=15,
            isolation_level=None,
        )
        try:
            if not initializing:
                self._check_identity()
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
                self._check_identity()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
