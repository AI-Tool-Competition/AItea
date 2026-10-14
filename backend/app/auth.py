import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .db import now, uid
from .defaults import DEFAULT_TEMPLATE
from .service import fail

COOKIE = "tea_session"
hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def user_dict(row):
    return {
        key: bool(row[key]) if key == "is_demo" else row[key] for key in ("id", "name", "email", "is_demo")
    }


class Auth:
    def __init__(self, service):
        self.service = service

    def current(self, token):
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with closing(self.service.db.connect()) as conn:
            row = conn.execute(
                "SELECT users.* FROM users JOIN sessions ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>?",
                (token_hash, now()),
            ).fetchone()
            return user_dict(row) if row else None

    def throttle(self, ip):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self.service.db.transaction() as conn:
            conn.execute("DELETE FROM auth_attempts WHERE at<?", (cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) n FROM auth_attempts WHERE ip=? AND at>?", (ip, cutoff)
            ).fetchone()["n"]
            if count >= 30:
                fail(429, "rate_limited", "操作过于频繁，请稍后再试")
            conn.execute("INSERT INTO auth_attempts VALUES (?,?)", (ip, now()))

    def session(self, owner, old_token=None):
        token = secrets.token_urlsafe(48)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        with self.service.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
            if old_token:
                conn.execute(
                    "DELETE FROM sessions WHERE token_hash=?",
                    (hashlib.sha256(old_token.encode()).hexdigest(),),
                )
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), owner, expires),
            )
        return token

    def demo(self, token):
        current = self.current(token)
        if current:
            return current, token
        id = uid()
        with self.service.db.transaction() as conn:
            conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (id, "体验用户", None, None, 1, now()))
        mode_id = next(mode["id"] for mode in DEFAULT_TEMPLATE["modes"] if mode["enabled"])
        self.service.create_case(
            id,
            {
                "title": DEFAULT_TEMPLATE["title"],
                "company": "",
                "product": "六堡茶",
                "mode_id": mode_id,
                "template_id": DEFAULT_TEMPLATE["id"],
            },
        )
        return {"id": id, "name": "体验用户", "email": None, "is_demo": True}, self.session(id)

    def register(self, values, old_token=None):
        email = values["email"].lower()
        password_hash = hasher.hash(values["password"])
        current = self.current(old_token)
        id = current["id"] if current and current["is_demo"] else uid()
        try:
            with self.service.db.transaction() as conn:
                if current and current["is_demo"]:
                    conn.execute(
                        "UPDATE users SET name=?,email=?,password_hash=?,is_demo=0 WHERE id=?",
                        (values["name"], email, password_hash, id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO users VALUES (?,?,?,?,?,?)",
                        (id, values["name"], email, password_hash, 0, now()),
                    )
        except sqlite3.IntegrityError:
            fail(409, "email_registered", "该邮箱已注册，请直接登录")
        return {"id": id, "name": values["name"], "email": email, "is_demo": False}, self.session(
            id, old_token
        )

    def login(self, values, old_token=None):
        with closing(self.service.db.connect()) as conn:
            row = conn.execute("SELECT * FROM users WHERE email=?", (values["email"].lower(),)).fetchone()
        try:
            valid = hasher.verify(row["password_hash"] if row else DUMMY_HASH, values["password"])
        except (VerificationError, InvalidHashError):
            valid = False
        if not row or not valid:
            fail(401, "invalid_credentials", "邮箱或密码不正确")
        return user_dict(row), self.session(row["id"], old_token)

    def logout(self, token):
        if token:
            with self.service.db.transaction() as conn:
                conn.execute(
                    "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
                )
