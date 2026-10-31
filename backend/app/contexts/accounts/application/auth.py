import hashlib
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from argon2.exceptions import InvalidHashError, VerificationError

from app.contexts.accounts.infrastructure.credentials import DUMMY_HASH, hasher
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID
from app.contexts.workflows.infrastructure.defaults import DEFAULT_TEMPLATE
from app.shared.domain.errors import fail
from app.shared.domain.values import now, uid

COOKIE = "tea_session"
DEFAULT_ADMIN_EMAIL = "admin@tea.local"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def demo_enabled():
    default = "false" if os.getenv("TEA_ENV") == "production" else "true"
    return os.getenv("TEA_ALLOW_DEMO", default).lower() == "true"


def user_dict(row):
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "is_demo": bool(row["is_demo"]),
        "can_login": bool(row["can_login"]) if "can_login" in keys else True,
        "is_admin": bool(row["is_admin"]) if "is_admin" in keys else False,
    }


class Auth:
    def __init__(self, service):
        self.service = service
        self.admin_bootstrap_token = os.getenv("TEA_ADMIN_BOOTSTRAP_TOKEN", "")

    def ensure_default_admin(self):
        """Create the requested administrator login once, without resetting it later."""
        with closing(self.service.db.connect()) as conn:
            if conn.execute("SELECT 1 FROM users WHERE lower(email)=?", (DEFAULT_ADMIN_EMAIL,)).fetchone():
                return

        password_hash = hasher.hash(DEFAULT_ADMIN_PASSWORD)
        user_id = uid()
        with self.service.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE lower(email)=?", (DEFAULT_ADMIN_EMAIL,)).fetchone():
                return
            conn.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (user_id, "管理员", DEFAULT_ADMIN_EMAIL, password_hash, 0, now()),
            )
            conn.execute(
                "INSERT INTO user_access(user_id,can_login,is_admin,revision) VALUES (?,1,1,1)",
                (user_id,),
            )

    def admin_setup_required(self):
        with closing(self.service.db.connect()) as conn:
            return not conn.execute(
                "SELECT 1 FROM user_access WHERE can_login=1 AND is_admin=1 LIMIT 1"
            ).fetchone()

    def bootstrap_admin(self, user_id, supplied_token):
        if not self.admin_bootstrap_token or not secrets.compare_digest(
            supplied_token, self.admin_bootstrap_token
        ):
            fail(403, "invalid_admin_bootstrap", "管理员引导码无效")
        with self.service.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM user_access WHERE can_login=1 AND is_admin=1 LIMIT 1").fetchone():
                fail(409, "admin_setup_complete", "管理员已完成设置")
            row = conn.execute(
                """SELECT u.id,u.is_demo,u.email,a.can_login,a.is_admin
                    FROM users u JOIN user_access a ON a.user_id=u.id WHERE u.id=?""",
                (user_id,),
            ).fetchone()
            if not row or row["is_demo"] or not row["email"] or not row["can_login"]:
                fail(403, "account_not_eligible", "请使用已开放登录的正式账号完成管理员设置")
            conn.execute(
                "UPDATE user_access SET is_admin=1,revision=revision+1 WHERE user_id=?",
                (user_id,),
            )
            updated = conn.execute(
                """SELECT users.*,user_access.can_login,user_access.is_admin
                    FROM users JOIN user_access ON user_access.user_id=users.id WHERE users.id=?""",
                (user_id,),
            ).fetchone()
        return user_dict(updated)

    def current(self, token):
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with closing(self.service.db.connect()) as conn:
            row = conn.execute(
                """SELECT users.*,COALESCE(user_access.can_login,1) can_login,
                    COALESCE(user_access.is_admin,0) is_admin
                    FROM users JOIN sessions ON users.id=sessions.user_id
                    LEFT JOIN user_access ON user_access.user_id=users.id
                    WHERE sessions.token_hash=? AND sessions.expires_at>?""",
                (token_hash, now()),
            ).fetchone()
            if row and row["is_demo"] and not demo_enabled():
                return None
            return user_dict(row) if row and row["id"] != SYSTEM_OWNER_ID and row["can_login"] else None

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
        if owner == SYSTEM_OWNER_ID:
            fail(403, "system_principal", "系统法规总库不能登录")
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
        if not demo_enabled():
            fail(403, "demo_disabled", "请使用已开放的账号登录；生产环境未开放匿名演示。")
        current = self.current(token)
        if current:
            return current, token
        id = uid()
        with self.service.db.transaction() as conn:
            conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (id, "体验用户", None, None, 1, now()))
            conn.execute(
                "INSERT INTO user_access(user_id,can_login,is_admin,revision) VALUES (?,1,0,1)",
                (id,),
            )
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
        return {
            "id": id,
            "name": "体验用户",
            "email": None,
            "is_demo": True,
            "can_login": True,
            "is_admin": False,
        }, self.session(id)

    def register(self, values, old_token=None):
        email = values["email"].lower()
        if email == DEFAULT_ADMIN_EMAIL:
            fail(409, "reserved_email", "该账号名称已被系统管理员保留")
        password_hash = hasher.hash(values["password"])
        current = self.current(old_token)
        id = current["id"] if current and current["is_demo"] else uid()
        bootstrap_token = values.get("bootstrap_token", "")
        is_admin = False
        can_login = False
        try:
            with self.service.db.transaction() as conn:
                if bootstrap_token:
                    if not self.admin_bootstrap_token or not secrets.compare_digest(
                        bootstrap_token, self.admin_bootstrap_token
                    ):
                        fail(403, "invalid_admin_bootstrap", "管理员引导码无效")
                    if conn.execute(
                        "SELECT 1 FROM user_access WHERE can_login=1 AND is_admin=1 LIMIT 1"
                    ).fetchone():
                        fail(409, "admin_setup_complete", "管理员已完成设置")
                    is_admin = True
                    can_login = True
                if current and current["is_demo"]:
                    conn.execute(
                        "UPDATE users SET name=?,email=?,password_hash=?,is_demo=0 WHERE id=?",
                        (values["name"], email, password_hash, id),
                    )
                    conn.execute(
                        """UPDATE user_access SET can_login=?,is_admin=?,revision=revision+1
                            WHERE user_id=?""",
                        (int(can_login), int(is_admin), id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO users VALUES (?,?,?,?,?,?)",
                        (id, values["name"], email, password_hash, 0, now()),
                    )
                    conn.execute(
                        "INSERT INTO user_access(user_id,can_login,is_admin,revision) VALUES (?,?,?,1)",
                        (id, int(can_login), int(is_admin)),
                    )
                if not can_login:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (id,))
        except sqlite3.IntegrityError:
            fail(409, "email_registered", "该邮箱已注册，请直接登录")
        result = {
            "id": id,
            "name": values["name"],
            "email": email,
            "is_demo": False,
            "can_login": can_login,
            "is_admin": is_admin,
        }
        return result, self.session(id, old_token) if can_login else None

    def login(self, values, old_token=None):
        identity = values["email"].strip().lower()
        if identity == DEFAULT_ADMIN_USERNAME:
            identity = DEFAULT_ADMIN_EMAIL
        with closing(self.service.db.connect()) as conn:
            row = conn.execute(
                """SELECT users.*,COALESCE(user_access.can_login,1) can_login,
                    COALESCE(user_access.is_admin,0) is_admin
                    FROM users LEFT JOIN user_access ON user_access.user_id=users.id
                    WHERE lower(users.email)=?""",
                (identity,),
            ).fetchone()
        try:
            valid = hasher.verify(
                row["password_hash"] if row and row["id"] != SYSTEM_OWNER_ID else DUMMY_HASH,
                values["password"],
            )
        except (VerificationError, InvalidHashError):
            valid = False
        if not row or not valid or row["id"] == SYSTEM_OWNER_ID:
            fail(401, "invalid_credentials", "邮箱或密码不正确")
        if not row["can_login"]:
            fail(403, "account_not_allowed", "账号暂未开放登录，请联系管理员")
        return user_dict(row), self.session(row["id"], old_token)

    def list_users(self, *, limit=50, offset=0, query=""):
        query = query.strip()
        where = "u.is_demo=0 AND u.email IS NOT NULL"
        parameters = []
        if query:
            where += " AND (instr(lower(u.name),lower(?))>0 OR instr(lower(u.email),lower(?))>0)"
            parameters.extend((query, query))
        with closing(self.service.db.connect()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM users u WHERE {where}", parameters).fetchone()[0]
            rows = conn.execute(
                f"""SELECT u.id,u.name,u.email,u.created_at,
                    COALESCE(a.can_login,1) can_login,COALESCE(a.is_admin,0) is_admin,
                    COALESCE(a.revision,0) revision
                    FROM users u LEFT JOIN user_access a ON a.user_id=u.id
                    WHERE {where} ORDER BY u.created_at DESC,u.id DESC LIMIT ? OFFSET ?""",
                [*parameters, limit, offset],
            ).fetchall()
        return {
            "items": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "email": row["email"],
                    "created_at": row["created_at"],
                    "can_login": bool(row["can_login"]),
                    "is_admin": bool(row["is_admin"]),
                    "revision": row["revision"],
                }
                for row in rows
            ],
            "total": total,
        }

    def update_user_access(self, user_id, values):
        with self.service.db.transaction() as conn:
            row = conn.execute(
                """SELECT u.id,u.is_demo,u.email,COALESCE(a.can_login,1) can_login,
                    COALESCE(a.is_admin,0) is_admin,COALESCE(a.revision,0) revision
                    FROM users u LEFT JOIN user_access a ON a.user_id=u.id WHERE u.id=?""",
                (user_id,),
            ).fetchone()
            if not row or row["is_demo"] or not row["email"] or user_id == SYSTEM_OWNER_ID:
                fail(404, "not_found", "未找到账号")
            if row["revision"] != values["expected_revision"]:
                fail(409, "stale_account", "账号权限已被其他管理员修改，请刷新后重试")
            can_login = values.get("can_login", bool(row["can_login"]))
            is_admin = values.get("is_admin", bool(row["is_admin"]))
            if is_admin and not can_login:
                fail(422, "invalid_account_access", "不可登录账号不能保留管理员身份")
            if row["can_login"] and row["is_admin"] and (not can_login or not is_admin):
                active_admins = conn.execute(
                    "SELECT COUNT(*) FROM user_access WHERE can_login=1 AND is_admin=1"
                ).fetchone()[0]
                if active_admins <= 1:
                    fail(409, "last_admin", "至少需要保留一名可登录的管理员")
            if row["revision"]:
                changed = conn.execute(
                    """UPDATE user_access SET can_login=?,is_admin=?,revision=revision+1
                        WHERE user_id=? AND revision=?""",
                    (int(can_login), int(is_admin), user_id, values["expected_revision"]),
                ).rowcount
                if changed != 1:
                    fail(409, "stale_account", "账号权限已被其他管理员修改，请刷新后重试")
            else:
                conn.execute(
                    "INSERT INTO user_access(user_id,can_login,is_admin,revision) VALUES (?,?,?,1)",
                    (user_id, int(can_login), int(is_admin)),
                )
            if not can_login:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            updated = conn.execute(
                """SELECT u.id,u.name,u.email,u.created_at,a.can_login,a.is_admin,a.revision
                    FROM users u JOIN user_access a ON a.user_id=u.id WHERE u.id=?""",
                (user_id,),
            ).fetchone()
        return {
            "id": updated["id"],
            "name": updated["name"],
            "email": updated["email"],
            "created_at": updated["created_at"],
            "can_login": bool(updated["can_login"]),
            "is_admin": bool(updated["is_admin"]),
            "revision": updated["revision"],
        }

    def logout(self, token):
        if token:
            with self.service.db.transaction() as conn:
                conn.execute(
                    "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
                )
