"""Single-host model admission across API and batch processes, without credentials.

Budgets and provider cooldowns survive instance migration. Restoring an offline
snapshot releases the old machine's PID leases before any clients can start.
"""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

LEASE_SECONDS = 360  # Greater than the existing complete embedding call deadline.
APPLICATION_ID = 0x41495445
PROTOCOL = "aitea-embedding-admission-v1"
logger = logging.getLogger(__name__)


def model_scope(base, model):
    return hashlib.sha256(json.dumps([base.rstrip("/"), model], separators=(",", ":")).encode()).hexdigest()


class SharedEmbeddingCooldown(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("Shared embedding provider cooldown")


def process_identity(pid):
    try:
        # Linux process start ticks prevent a recycled PID from inheriting a slot.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def _validate_protocol(path):
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (
                tables != {"protocol", "budgets", "leases", "sqlite_sequence"}
                or conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or conn.execute("SELECT version,name FROM protocol").fetchall() != [(1, PROTOCOL)]
                or conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()
            ):
                raise ValueError("Not a dedicated embedding admission database")
    except sqlite3.Error:
        raise ValueError("Not a dedicated embedding admission database") from None


def release_restored_leases(path):
    """Use only on a newly restored, offline copy; retain provider cooldowns."""
    _validate_protocol(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute("DELETE FROM leases").rowcount


class SharedEmbeddingBudget:
    def __init__(self, path, scope, limit):
        self.path, self.scope, self.limit = Path(path).absolute(), scope, limit
        self.lock = threading.RLock()
        for item in (self.path, *self.path.parents):
            if item.is_symlink():
                raise ValueError("Embedding admission path must not contain symlinks")
        self.pid, self.started = os.getpid(), process_identity(os.getpid())
        if not self.started:
            raise RuntimeError("Shared embedding admission requires Linux process identities")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._initialization_lock():
            self._initialize(scope, limit)

    @contextmanager
    def _initialization_lock(self):
        # Directory flock also closes the short atomic-link publication window.
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _initialize(self, scope, limit):
        if not self.path.exists():
            self._publish_new_database()
        identity = self.path.lstat()
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise ValueError("Embedding admission requires a dedicated regular file")
        self.file_identity = identity.st_dev, identity.st_ino
        # Serialize only initialization, including PRAGMA/DDL, across processes.
        # Validation is read-only and precedes chmod, WAL or any schema mutation.
        with open(self.path, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _validate_protocol(self.path)
            with closing(self.connect()) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
            os.chmod(self.path, 0o600)
            with self.transaction() as conn:
                self._reap(conn)
                conn.execute("INSERT OR IGNORE INTO budgets(scope,capacity) VALUES(?,?)", (scope, limit))
                capacity = conn.execute("SELECT capacity FROM budgets WHERE scope=?", (scope,)).fetchone()[0]
                if capacity != limit:
                    if conn.execute("SELECT 1 FROM leases WHERE scope=? LIMIT 1", (scope,)).fetchone():
                        raise ValueError("Shared embedding capacity differs; drain and restart all clients")
                    conn.execute("UPDATE budgets SET capacity=? WHERE scope=?", (limit, scope))

    def _publish_new_database(self):
        # Publish a complete protocol atomically. A concurrent first user never
        # sees an empty, partially initialized database or fights over its DDL.
        fd, temporary = tempfile.mkstemp(prefix=".embedding-admission-", dir=self.path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(temporary)) as conn:
                conn.executescript(f"""
                PRAGMA application_id={APPLICATION_ID};
                CREATE TABLE protocol(version INTEGER PRIMARY KEY CHECK(version=1), name TEXT NOT NULL);
                INSERT INTO protocol VALUES(1,'{PROTOCOL}');
                CREATE TABLE budgets(
                    scope TEXT PRIMARY KEY, capacity INTEGER NOT NULL,
                    query_streak INTEGER NOT NULL DEFAULT 0, peak INTEGER NOT NULL DEFAULT 0,
                    query_grants INTEGER NOT NULL DEFAULT 0, bulk_grants INTEGER NOT NULL DEFAULT 0,
                    reclaimed INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE leases(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL, lane TEXT NOT NULL CHECK(lane IN ('query','bulk')),
                    status TEXT NOT NULL CHECK(status IN ('waiting','active')),
                    pid INTEGER NOT NULL, process_start TEXT NOT NULL,
                    expires_at REAL NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX embedding_admission_scope ON leases(scope,status,lane,sequence);
            """)
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)

    def connect(self):
        identity = self.path.lstat()
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_nlink != 1
            or (identity.st_dev, identity.st_ino) != self.file_identity
        ):
            raise ValueError("Embedding admission file identity changed")
        conn = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def transaction(self):
        # SQLite still coordinates independent processes. Serialize the tiny
        # local transactions so dozens of HTTP threads do not fight themselves.
        with self.lock, closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _reap(self, conn):
        removed = conn.execute(
            "DELETE FROM leases WHERE scope=? AND expires_at<=?", (self.scope, time.time())
        ).rowcount
        for row in conn.execute(
            "SELECT DISTINCT pid,process_start FROM leases WHERE scope=?", (self.scope,)
        ).fetchall():
            if row["pid"] == self.pid and row["process_start"] == self.started:
                continue
            if process_identity(row["pid"]) != row["process_start"]:
                removed += conn.execute(
                    "DELETE FROM leases WHERE scope=? AND pid=? AND process_start=?",
                    (self.scope, row["pid"], row["process_start"]),
                ).rowcount
        if removed:
            conn.execute("UPDATE budgets SET reclaimed=reclaimed+? WHERE scope=?", (removed, self.scope))

    def try_acquire(self, token, lane):
        timestamp = time.time()
        with self.transaction() as conn:
            self._reap(conn)
            budget = conn.execute("SELECT * FROM budgets WHERE scope=?", (self.scope,)).fetchone()
            if budget["capacity"] != self.limit:
                raise ValueError("Shared embedding capacity changed; restart this client")
            if not conn.execute("SELECT 1 FROM leases WHERE token=?", (token,)).fetchone():
                if (
                    conn.execute("SELECT count(*) FROM leases WHERE scope=?", (self.scope,)).fetchone()[0]
                    >= 2048
                ):
                    return False
                conn.execute(
                    "INSERT INTO leases(token,scope,lane,status,pid,process_start,expires_at,created_at) VALUES(?,?,?,'waiting',?,?,?,?)",
                    (token, self.scope, lane, self.pid, self.started, timestamp + LEASE_SECONDS, timestamp),
                )
            active = {"query": 0, "bulk": 0}
            for row in conn.execute(
                "SELECT lane,count(*) n FROM leases WHERE scope=? AND status='active' GROUP BY lane",
                (self.scope,),
            ):
                active[row["lane"]] = row["n"]
            if budget["cooldown_until"] > timestamp:
                raise SharedEmbeddingCooldown(budget["cooldown_until"] - timestamp)
            if sum(active.values()) >= self.limit:
                return False
            first = {
                row["lane"]: row["sequence"]
                for row in conn.execute(
                    "SELECT lane,min(sequence) sequence FROM leases WHERE scope=? AND status='waiting' GROUP BY lane",
                    (self.scope,),
                )
            }
            if len(first) == 2 and self.limit == 1:
                chosen = "query" if budget["query_streak"] < 3 else "bulk"
            elif "bulk" in first and active["bulk"] < max(1, self.limit // 4):
                chosen = "bulk"
            elif "query" in first:
                chosen = "query"
            elif "bulk" in first and active["bulk"] < self.limit - min(4, self.limit // 16):
                chosen = "bulk"
            else:
                return False
            row = conn.execute("SELECT sequence FROM leases WHERE token=?", (token,)).fetchone()
            if chosen != lane or row["sequence"] != first[chosen]:
                return False
            conn.execute(
                "UPDATE leases SET status='active',expires_at=? WHERE token=?",
                (timestamp + LEASE_SECONDS, token),
            )
            conn.execute(
                "UPDATE budgets SET peak=MAX(peak,?),query_streak=?,query_grants=query_grants+?,bulk_grants=bulk_grants+? WHERE scope=?",
                (
                    sum(active.values()) + 1,
                    budget["query_streak"] + 1 if lane == "query" else 0,
                    int(lane == "query"),
                    int(lane == "bulk"),
                    self.scope,
                ),
            )
            return True

    def release(self, token, *, waiting_only=False):
        for attempt in range(3):
            try:
                with self.transaction() as conn:
                    conn.execute(
                        "DELETE FROM leases WHERE token=? AND scope=? AND pid=? AND process_start=?"
                        + (" AND status='waiting'" if waiting_only else ""),
                        (token, self.scope, self.pid, self.started),
                    )
                return
            except sqlite3.Error as error:
                if attempt == 2:
                    # Losing a local slot release must not discard an already
                    # paid successful response. The conservative lease expires.
                    logger.warning("Embedding slot release deferred: %s", type(error).__name__)
                    return
                time.sleep(0.025 * (attempt + 1))

    def cooldown(self, seconds):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE budgets SET cooldown_until=MAX(cooldown_until,?) WHERE scope=?",
                (time.time() + max(0, seconds), self.scope),
            )

    def snapshot(self):
        with self.transaction() as conn:
            self._reap(conn)
            budget = dict(conn.execute("SELECT * FROM budgets WHERE scope=?", (self.scope,)).fetchone())
            active, waiting = {"query": 0, "bulk": 0}, {"query": 0, "bulk": 0}
            for row in conn.execute(
                "SELECT status,lane,count(*) n FROM leases WHERE scope=? GROUP BY status,lane", (self.scope,)
            ):
                (active if row["status"] == "active" else waiting)[row["lane"]] = row["n"]
            return {
                "scope": "host_model",
                "model_scope_hash": self.scope,
                "path": str(self.path),
                "limit": budget["capacity"],
                "query_reserved": min(4, self.limit // 16),
                "bulk_floor_when_waiting": max(1, self.limit // 4),
                "bulk_ceiling": self.limit - min(4, self.limit // 16),
                "active": active,
                "waiting": waiting,
                "peak": budget["peak"],
                "grants": {"query": budget["query_grants"], "bulk": budget["bulk_grants"]},
                "reclaimed": budget["reclaimed"],
                "cooldown_until": budget["cooldown_until"],
            }
