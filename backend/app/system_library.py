"""Read-only shared official originals; private ownership remains a write boundary."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .db import now


SYSTEM_OWNER_ID = "aitea-system-legal-library"
SYSTEM_OWNER_NAME = "系统法规总库"


@dataclass(frozen=True)
class BackgroundWorkerScope:
    """An injected background queue boundary, independent of API read/write scope."""

    shared_library_workers: str = "internal"

    def __post_init__(self):
        if self.shared_library_workers not in ("internal", "external"):
            raise ValueError("TEA_SHARED_LIBRARY_WORKERS 必须为 internal 或 external。")

    def sql(self, owner_column="owner_id"):
        # Column names are internal call-site constants; owner values stay bound.
        if owner_column not in ("owner_id", "i.owner_id", "j.owner_id", "s.owner_id"):
            raise ValueError("后台任务归属列无效。")
        if self.shared_library_workers == "external":
            return f" AND {owner_column}!=?", (SYSTEM_OWNER_ID,)
        return "", ()

    def allows(self, owner_id):
        return self.shared_library_workers == "internal" or owner_id != SYSTEM_OWNER_ID

    def kwargs(self):
        # Preserve existing unscoped collaborator interfaces in default mode.
        return {"worker_scope": self} if self.shared_library_workers == "external" else {}


# These are operator-maintained official sources from the collection plans.
# A page, upload tag or request payload cannot extend this allowlist.
_GOVERNMENT_SUFFIXES = {"ID": ".go.id", "SG": ".gov.sg"}
_INTERNATIONAL_HOSTS = frozenset(
    {
        "fta.mofcom.gov.cn",
        "www.dfat.gov.au",
        "asean.org",
        "www.asean.org",
        "rcepsec.org",
        "www.wto.org",
        "docs.wto.org",
        "goods-schedules.wto.org",
        "itip-services-worldbank.wto.org",
    }
)
_PLAN_HOSTS = {
    "ID": frozenset({"s.mkri.id", "www.mkri.id", "jdih.mkri.id"}) | _INTERNATIONAL_HOSTS,
    "SG": _INTERNATIONAL_HOSTS,
}
# Operator-reviewed original files cited by the SG treaty collection. Keep the
# complete URL exact: this grants neither an OECD host nor a directory. Evidence
# and the acquired SHA-256 values are recorded in docs/SINGAPORE_COLLECTION.md.
_PLAN_ORIGINAL_URLS = {
    "SG": frozenset(
        {
            "https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/beps-mli/"
            "beps-mli-position-china-instrument-deposit.pdf",
            "https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/beps-mli/"
            "beps-mli-position-singapore-instrument-deposit.pdf",
            "https://treaties.un.org/doc/Publication/UNTS/Volume%20563/volume-563-I-8206-English.pdf",
        }
    ),
}


def official_source_allowed(source_url, country):
    """Admission identity only; downloading still uses the bounded HTTPS guard."""
    if not isinstance(source_url, str) or country not in _GOVERNMENT_SUFFIXES:
        return False
    try:
        parsed = urlsplit(source_url)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or host.endswith(".")
            or not host.isascii()
            or len(host) > 253
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
            )
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in source_url
            )
            or "\\" in source_url
        ):
            return False
    except (ValueError, UnicodeError):
        return False
    return (
        host.endswith(_GOVERNMENT_SUFFIXES[country])
        or host in _PLAN_HOSTS[country]
        or source_url in _PLAN_ORIGINAL_URLS.get(country, ())
    )


def ensure_principal(conn):
    """Explicit operator import step, never a login or a per-request seed."""
    row = conn.execute("SELECT * FROM users WHERE id=?", (SYSTEM_OWNER_ID,)).fetchone()
    if row:
        if (
            row["name"] != SYSTEM_OWNER_NAME
            or row["email"] is not None
            or row["password_hash"] is not None
            or row["is_demo"]
        ):
            raise ValueError("系统法规总库身份与已有可登录账号冲突")
        if conn.execute("SELECT 1 FROM sessions WHERE user_id=?", (SYSTEM_OWNER_ID,)).fetchone():
            raise ValueError("系统法规总库不能持有登录会话")
        return SYSTEM_OWNER_ID
    conn.execute(
        "INSERT INTO users VALUES(?,?,?,?,?,?)",
        (SYSTEM_OWNER_ID, SYSTEM_OWNER_NAME, None, None, 0, now()),
    )
    return SYSTEM_OWNER_ID


def register_source_guard(conn):
    conn.create_function("system_source_allowed", 2, official_source_allowed, deterministic=True)


def public_document_sql(alias="documents"):
    """The parent and its original-only attachments share the same country."""
    return f"""({alias}.owner_id='{SYSTEM_OWNER_ID}' AND {alias}.case_id IS NULL
        AND EXISTS(SELECT 1 FROM document_countries sys_country
            JOIN documents sys_parent ON sys_parent.id=COALESCE({alias}.parent_id,{alias}.id)
            JOIN legal_classifications sys_class ON sys_class.document_id=sys_parent.id
                AND sys_class.collection='country:'||sys_country.country
            WHERE sys_country.document_id=sys_parent.id
                AND sys_parent.owner_id='{SYSTEM_OWNER_ID}' AND sys_parent.case_id IS NULL
                AND sys_parent.role='primary'
                AND NOT EXISTS(SELECT 1 FROM document_countries sys_child_country
                    WHERE sys_child_country.document_id={alias}.id
                        AND sys_child_country.country<>sys_country.country)
                AND ({alias}.role='primary' OR ({alias}.role='secondary' AND {alias}.parent_id IS NOT NULL))
                AND system_source_allowed(sys_parent.source_url,sys_country.country)
                AND system_source_allowed({alias}.source_url,sys_country.country)))"""


def require_admission(owner, source_url, country, *, case_id=None):
    if owner != SYSTEM_OWNER_ID:
        return
    if case_id is not None or not official_source_allowed(source_url, country):
        from .service import fail

        fail(422, "system_source_required", "系统法规总库只接收已定国的官方 HTTPS 原件")


def source_scope(owner):
    return "system" if owner == SYSTEM_OWNER_ID else "personal"
