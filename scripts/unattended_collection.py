"""Continuously collect a bounded, reviewed official source pool without models.

Raw originals, URL/version mappings, failed attempts and source health persist.
This collector never constructs Service, imports laws, or approves their force.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import sys
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]

from app.contexts.ingestion.application.source_catalog import (  # noqa: E402
    SourceCatalog,
    active_sources,
)
from app.contexts.ingestion.infrastructure.collection_proxy import proxy_for_host  # noqa: E402
from app.contexts.ingestion.infrastructure.source_adapters import (  # noqa: E402
    bi_jdih_discovery,
    bi_official_discovery,
    dephub_discovery,
    treaty_room_discovery,
)
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (  # noqa: E402
    discover_page,
    page_body_limit,
    page_purpose,
    setneg_request_parameters,
)
from app.contexts.ingestion.infrastructure.source_registry_json import (  # noqa: E402
    JsonSourceRegistry,
    read_source_registry,
)
from app.shared.infrastructure.http.download import (  # noqa: E402
    DownloadError,
    _download,
    _validated_url,
)
from app.shared.infrastructure.persistence.blob_store import BlobStore  # noqa: E402
from official_source_supervisor_recovery import (  # noqa: E402
    load,
    lock,
    write,
)
from source_health import (  # noqa: E402
    choose_sources,
    due,
    observe,
    replacement_decisions,
)

PROTOCOL = "aitea-unattended-collection-v1"
SETNEG_ID = "id-setneg-perpu"
SETNEG_URL = "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0"


class PoolMigrationRequired(ValueError):
    """Raised when a v2 pool was edited without its ledger migration."""
LIMITS = {
    "workers": 4,
    "sources_per_tick": 4,
    "per_host": 2,
    "timeout_seconds": 20,
    "requests_per_source_cycle": 4,
    "daily_requests": 256,
    "daily_source_requests": 24,
    "daily_source_bytes": 32 * 1024**2,
    "daily_bytes": 512 * 1024**2,
    "max_response_bytes": 8 * 1024**2,
    "max_frontier": 128,
    "min_free_bytes": 8 * 1024**3,
    "tick_seconds": 30,
}
MAX_STATUS_SOURCES = 256
MAX_STATUS_ACTIVE_SOURCES = 32


def _status_counter(value):
    return value if type(value) is int and value >= 0 else 0


def _status_timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _status_label(value, *, limit=64):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def status_summary(state, *, current_pool_sha256=None, current_pool_sources=None):
    """Return bounded source health and queue counts without raw URLs or candidates."""
    if not isinstance(state, dict):
        raise TypeError("collector state must be a JSON object")
    binding = state.get("binding") if isinstance(state.get("binding"), dict) else {}
    source_states = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    country = binding.get("country")
    configured_sources = None
    if isinstance(current_pool_sources, list):
        configured_sources = {}
        for item in current_pool_sources:
            if (
                isinstance(item, dict)
                and item.get("country") == country
                and isinstance(item.get("id"), str)
                and item.get("status") in ("active", "archived")
            ):
                configured_sources[item["id"]] = item["status"]
    rows = []
    health_counts, scheduling_counts = Counter(), Counter()
    health_by_configuration = {"active": Counter(), "archived": Counter(), "unlisted": Counter(), "unknown": Counter()}
    scheduling_by_configuration = {
        "active": Counter(),
        "archived": Counter(),
        "unlisted": Counter(),
        "unknown": Counter(),
    }
    totals = {"cached_urls": 0, "frontier_urls": 0, "scope_excluded_urls": 0}
    for identifier, source in source_states.items():
        if not isinstance(identifier, str) or not isinstance(source, dict):
            continue
        cache = source.get("cache") if isinstance(source.get("cache"), dict) else {}
        frontier = source.get("frontier")
        if isinstance(frontier, dict):
            frontier_count, frontier_items = len(frontier), frontier.values()
        elif isinstance(frontier, list):
            frontier_count, frontier_items = len(frontier), frontier
        else:
            frontier_count, frontier_items = 0, ()
        excluded = sum(
            isinstance(item, dict) and item.get("scope_excluded") is True for item in frontier_items
        )
        health = _status_label(source.get("health"), limit=32) or "unknown"
        scheduling_status = _status_label(source.get("scheduling_status"), limit=48) or "unknown"
        configured_status = (
            configured_sources.get(identifier, "unlisted") if configured_sources is not None else "unknown"
        )
        health_counts[health] += 1
        scheduling_counts[scheduling_status] += 1
        health_by_configuration[configured_status][health] += 1
        scheduling_by_configuration[configured_status][scheduling_status] += 1
        totals["cached_urls"] += len(cache)
        totals["frontier_urls"] += frontier_count
        totals["scope_excluded_urls"] += excluded
        observation = source.get("last_observation")
        observation = observation if isinstance(observation, dict) else {}
        summarized_observation = {
            "status": _status_label(observation.get("status"), limit=48),
            "http_status": observation.get("http_status")
            if type(observation.get("http_status")) is int and 100 <= observation["http_status"] <= 599
            else None,
            "failure_code": _status_label(observation.get("failure_code"), limit=64),
            "error_type": _status_label(observation.get("error_type"), limit=96),
            "retry_after_seconds": _status_timestamp(observation.get("retry_after_seconds")),
        }
        rows.append(
            {
                "source_id": identifier[:128],
                "configured_status": configured_status,
                "health": health,
                "scheduling_status": scheduling_status,
                "attempt_count": _status_counter(source.get("attempt_count")),
                "success_count": _status_counter(source.get("success_count")),
                "failure_count": _status_counter(source.get("failure_count")),
                "consecutive_failures": _status_counter(source.get("consecutive_failures")),
                "cached_urls": len(cache),
                "frontier_urls": frontier_count,
                "scope_excluded_urls": excluded,
                "last_attempt_at": _status_timestamp(source.get("last_attempt_at")),
                "last_success_at": _status_timestamp(source.get("last_success_at")),
                "next_at": _status_timestamp(source.get("next_at")),
                "root_next_at": _status_timestamp(source.get("root_next_at")),
                "last_observation": summarized_observation,
            }
        )
    rows.sort(
        key=lambda row: (
            row["configured_status"] != "active",
            row["health"] == "healthy",
            -row["consecutive_failures"],
            row["source_id"],
        )
    )
    active_sources = state.get("active_sources")
    active_sources = (
        [item[:128] for item in active_sources if isinstance(item, str)][:MAX_STATUS_ACTIVE_SOURCES]
        if isinstance(active_sources, list)
        else []
    )
    pool_sha256 = binding.get("pool_sha256")
    pool_match = None
    if isinstance(pool_sha256, str) and isinstance(current_pool_sha256, str):
        pool_match = pool_sha256 == current_pool_sha256
    limits = binding.get("limits") if isinstance(binding.get("limits"), dict) else {}
    summarized_limits = {
        key: _status_counter(limits.get(key))
        for key in ("workers", "per_host", "daily_requests", "daily_source_requests", "tick_seconds")
        if key in limits
    }
    migrations = state.get("pool_migrations")
    migrations = migrations if isinstance(migrations, list) else []
    last_migration = migrations[-1] if migrations and isinstance(migrations[-1], dict) else {}
    return {
        "protocol": "aitea-unattended-collection-status-v1",
        "country": _status_label(country, limit=2),
        "status": _status_label(state.get("status"), limit=32) or "unknown",
        "root": _status_label(binding.get("root"), limit=1024),
        "updated_at": _status_timestamp(state.get("updated_at")),
        "last_progress_at": _status_timestamp(state.get("last_progress_at")),
        "pool_binding": {
            "bound_sha256": pool_sha256 if isinstance(pool_sha256, str) and len(pool_sha256) == 64 else None,
            "current_sha256": current_pool_sha256,
            "matches": pool_match,
        },
        "limits": summarized_limits,
        "in_flight_source_count": len(state.get("active_sources", []))
        if isinstance(state.get("active_sources"), list)
        else 0,
        "in_flight_source_ids": active_sources,
        "configured_source_counts": {
            "active": sum(value == "active" for value in configured_sources.values()),
            "archived": sum(value == "archived" for value in configured_sources.values()),
        }
        if configured_sources is not None
        else None,
        "configured_active_without_state_count": sum(
            status == "active" and identifier not in source_states
            for identifier, status in configured_sources.items()
        )
        if configured_sources is not None
        else None,
        "configured_archived_without_state_count": sum(
            status == "archived" and identifier not in source_states
            for identifier, status in configured_sources.items()
        )
        if configured_sources is not None
        else None,
        "unlisted_state_source_count": len(health_by_configuration["unlisted"]),
        "health_counts_by_configuration": {
            key: dict(sorted(value.items()))
            for key, value in health_by_configuration.items()
            if value
        },
        "scheduling_counts_by_configuration": {
            key: dict(sorted(value.items()))
            for key, value in scheduling_by_configuration.items()
            if value
        },
        "source_count": len(source_states),
        "reported_source_count": min(len(rows), MAX_STATUS_SOURCES),
        "truncated_source_count": max(0, len(rows) - MAX_STATUS_SOURCES),
        "health_counts": dict(sorted(health_counts.items())),
        "scheduling_counts": dict(sorted(scheduling_counts.items())),
        "totals": totals,
        "pool_migration_count": len(migrations),
        "last_pool_migration": {
            "at": _status_timestamp(last_migration.get("at")),
            "added_source_count": len(last_migration.get("added_source_ids", []))
            if isinstance(last_migration.get("added_source_ids"), list)
            else 0,
            "archived_source_count": len(last_migration.get("archived_source_ids", []))
            if isinstance(last_migration.get("archived_source_ids"), list)
            else 0,
        },
        "sources": rows[:MAX_STATUS_SOURCES],
    }


def _charged_bytes(receipt):
    """Return response-body bytes actually consumed under the bounded transport."""
    charged = receipt.get("charged_bytes", 0)
    if type(charged) is not int or charged < 0:
        raise ValueError("Invalid collection receipt byte charge")
    outcome = receipt.get("outcome")
    if (
        receipt.get("status") == "finished"
        and isinstance(outcome, dict)
        and outcome.get("status") == "http_error"
        and type(outcome.get("http_status")) is int
        and 400 <= outcome["http_status"] <= 599
        and not receipt.get("raw_response")
        and "bytes_received" not in receipt
    ):
        # Older transport receipts reserved the full response limit for HTTP
        # errors even though the downloader closed them without reading a body.
        return 0
    return charged


def download_failure_code(error):
    """Return a bounded diagnostic code, never persist exception text or secrets."""
    status = re.search(r"HTTP (\d{3})(?!\d)", str(error))
    if status:
        return f"http_{status[1]}"

    cause = error
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "tls_certificate_verification_failed"
        if isinstance(cause, ssl.SSLError):
            return "tls_handshake_failed"
        if isinstance(cause, socket.gaierror):
            return "dns_resolution_failed"
        if isinstance(cause, (socket.timeout, TimeoutError)):
            return "network_timeout"
        if isinstance(cause, ConnectionRefusedError):
            return "connection_refused"
        if isinstance(cause, (ConnectionResetError, BrokenPipeError)):
            return "connection_interrupted"
        if isinstance(cause, http.client.HTTPException):
            return "http_protocol_error"
        cause = cause.__cause__

    message_codes = {
        "DNS 解析超过时间限制或队列已满。": "dns_timeout_or_capacity",
        "DNS 解析超过时间限制。": "dns_timeout",
        "抓取来源域名无法解析。": "dns_resolution_failed",
        "抓取来源没有可用地址。": "dns_no_addresses",
        "抓取超过总时间限制。": "request_deadline_exceeded",
        "抓取连接失败或超时，请检查来源后重试。": "network_transport_error",
        "抓取来源的所有 DNS 地址必须是 public 公网地址。": "public_address_policy_rejected",
        "抓取地址必须是 public 公网地址。": "public_address_policy_rejected",
        "Redirect host is outside the reviewed official source scope": "redirect_scope_rejected",
    }
    default_code = "transient_network_error" if error.retryable else "request_rejected"
    return message_codes.get(str(error), default_code)


RCEP_SOURCE_ID = "sg-enterprisesg-rcep"
RCEP_POLICY_VERSION = "china-singapore-tariff-scope-v1"
DISCOVERY_EXCLUSION_POLICY_VERSION = "source-discovery-exclusions-v1"
# SG-5.1 scopes tariff schedules to China/Singapore actual trade. General treaty
# text, PSR/origin annexes and China/Singapore schedules remain discoverable.
RCEP_OUTSIDE_SCOPE = (
    r"^/-/media/esg/files/non-financial-assistance/for-companies/free-trade-agreements/"
    r"rcep/rcep_(?:Australia|Brunei_Darussalam|Cambodia|Indonesia|Japan|Korea|Lao_PDR|"
    r"Malaysia|Myanmar|New_Zealand|Philippines|Thailand|Viet_Nam)_"
    r"(?:Schedule(?:_[^/?]*)?|Appendix_on_Tariff_Differentials)\.pdf$"
)


def rcep_policy(source):
    return (
        source["id"] == RCEP_SOURCE_ID
        and source["country"] == "SG"
        and urlsplit(source["url"]).hostname == "www.enterprisesg.gov.sg"
    )


def scope_exclusion(source, url, title=""):
    parsed = urlsplit(url)
    if (
        rcep_policy(source)
        and parsed.hostname == "www.enterprisesg.gov.sg"
        and re.search(RCEP_OUTSIDE_SCOPE, unquote(parsed.path), re.I)
    ):
        return {"version": RCEP_POLICY_VERSION, "reason": "third_country_tariff_schedule"}
    context = re.sub(r"[^\w]+", " ", f"{unquote(parsed.path)} {title}".casefold()).strip()
    for term in source.get("discovery_exclude_keywords", []):
        normalized = re.sub(r"[^\w]+", " ", term.casefold()).strip()
        if normalized and normalized in context:
            return {
                "version": DISCOVERY_EXCLUSION_POLICY_VERSION,
                "reason": "configured_source_exclusion",
                "term": term,
            }
    return None


def scope_excluded(source, url, title=""):
    return scope_exclusion(source, url, title) is not None


def candidate_policy_version(source):
    versions = [RCEP_POLICY_VERSION] if rcep_policy(source) else []
    terms = source.get("discovery_exclude_keywords", [])
    if terms:
        canonical = json.dumps(terms, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        versions.append(f"{DISCOVERY_EXCLUSION_POLICY_VERSION}:{digest}")
    return "+".join(versions) or None


class Deferred(Exception):
    def __init__(self, status, *, retry_after=0, reason=None):
        self.status = status
        self.retry_after = retry_after
        self.reason = reason
        super().__init__(status)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_pool(path, country):
    """Load and validate the active source entries through the collection context."""
    return active_sources(read_source_registry(path), country, _validated_url)


def request_window_wait_until(source, now):
    """Return the next permitted request time, or None while inside a source window."""
    policy = source.get("request_window")
    if policy is None and source.get("discovery", {}).get("adapter") == "sso_catalog_2026":
        # Historical pool revisions predate the explicit field. Keep them safe
        # during migration and recovery by applying the same fixed official window.
        policy = {"timezone": "Asia/Singapore", "start": "03:00", "end": "07:00"}
    if policy is None:
        return None
    timezone = ZoneInfo(policy["timezone"])
    local_now = datetime.fromtimestamp(now, timezone)
    start = datetime_time.fromisoformat(policy["start"])
    end = datetime_time.fromisoformat(policy["end"])
    opens_today = datetime.combine(local_now.date(), start, tzinfo=timezone)
    closes_today = datetime.combine(local_now.date(), end, tzinfo=timezone)
    if opens_today <= local_now < closes_today:
        return None
    if local_now < opens_today:
        return opens_today.timestamp()
    return datetime.combine(local_now.date() + timedelta(days=1), start, tzinfo=timezone).timestamp()


def recipe(source):
    discovery = source.get("discovery")
    if discovery is not None:
        if discovery.get("adapter") == "bi_official":
            return {
                "adapter": "bi_official",
                "allowed_hosts": source["allowed_hosts"],
                "catalog_patterns": [],
                "detail_patterns": [],
                "document_patterns": [],
                "html_document_patterns": [],
                "include_terms": [],
                "exclude_terms": [],
                "max_links": 100,
            }
        if discovery.get("adapter") == "bi_jdih":
            return {
                "adapter": "bi_jdih",
                "allowed_hosts": source["allowed_hosts"],
                "catalog_patterns": [],
                "detail_patterns": [],
                "document_patterns": [],
                "html_document_patterns": [],
                "include_terms": [],
                "exclude_terms": [],
                "max_links": 200,
                "max_catalog_records": discovery["max_records"],
            }
        if discovery.get("adapter") == "dephub_regulations":
            return {
                "adapter": "bpk",
                "allowed_hosts": source["allowed_hosts"],
                "catalog_patterns": [r"^/peraturan/index(?:\?|$)"],
                "detail_patterns": [r"^/peraturan/detail(?:\?|$)"],
                "document_patterns": [r"^/api/media(?:\?|$)"],
                "html_document_patterns": [],
                "include_terms": [],
                "exclude_terms": [],
                "max_links": 200,
                "dephub_regulations": True,
                "dephub_max_pages": discovery["max_pages"],
            }
        if discovery.get("adapter") == "treaty_room_public":
            return {
                "adapter": "html_links",
                "allowed_hosts": source["allowed_hosts"],
                "catalog_patterns": [r"^/publik/perjanjian(?:\?|$)"],
                "detail_patterns": [],
                "document_patterns": [],
                "html_document_patterns": [],
                "include_terms": [],
                "exclude_terms": [],
                "max_links": 200,
                "max_catalog_records": discovery["max_records"],
                "max_catalog_pages": discovery["max_pages"],
                "treaty_room": True,
            }
        category = discovery["category"]
        page_size = discovery["page_size"]
        return {
            "adapter": "sso",
            "allowed_hosts": source["allowed_hosts"],
            "catalog_patterns": [
                rf"^/Browse/{category}/Current(?:/All(?:/[0-9]{{1,6}})?)?(?:\?|$)"
            ],
            "detail_patterns": [
                rf"^/{category}/[^/?]+/(?:Uncommenced|Repealed|Revoked|Spent)(?:/[^?]*)?(?:\?|$)"
            ],
            "document_patterns": [],
            "html_document_patterns": [rf"^/{category}/[^/?]+(?:/\d{{8}})?/?(?:\?|$)"],
            "include_terms": [],
            "exclude_terms": [],
            "required_query": {
                "PageSize": str(page_size),
                "SortBy": "Title",
                "SortOrder": "ASC",
            },
            "sso_catalog_2026": True,
            "max_links": page_size,
        }
    root_path = urlsplit(source["url"]).path or "/"
    root_pattern = "^" + re.escape(root_path) + "$"
    result = {
        "adapter": "html_links",
        "allowed_hosts": source["allowed_hosts"],
        "catalog_patterns": [root_pattern] if source["kind"] == "catalogue" else [],
        "html_document_patterns": (
            []
            if source["kind"] == "catalogue"
            else [root_pattern]
            + ["^" + re.escape(path) + "$" for path in source.get("html_document_paths", [])]
        ),
        "document_patterns": [
            r"\.(?:pdf|docx?)(?:$|\?)",
            r"/(?:download|storage|uploads)/",
        ],
        "detail_patterns": [r"^/"],
        "include_terms": source["discovery_keywords"],
        "exclude_terms": [
            "facebook",
            "instagram",
            "privacy policy",
            "careers",
            "login",
        ],
        "discovery_exclude_keywords": source.get("discovery_exclude_keywords", []),
        "max_links": 32,
    }
    if source["id"] == "id-beacukai-regulations":
        # The portal is a legacy query-routed directory: its shell and
        # navigation are not regulation records. Keep category, detail and
        # original-file paths distinct so daily request budget reaches laws.
        result.update(
            catalog_patterns=[
                r"^/$",
                r"^/index\.html\?page=jenis/[1-9][0-9]{0,3}/[a-z0-9-]{1,100}/[1-9][0-9]{0,5}\.html$",
            ],
            detail_patterns=[
                r"^/index\.html\?page=detail/jenis/[1-9][0-9]{0,3}/[1-9][0-9]{0,7}/[a-z0-9-]{1,100}(?:/[a-z0-9-]{1,160}){1,8}\.html$",
            ],
            document_patterns=[
                r"^/referensi/[0-9]{4}/(?:0[1-9]|1[0-2])/[a-f0-9]{12,64}-[^/?]+\.(?:pdf|docx?)$",
            ],
            exclude_patterns=[r"^/referensi/[0-9]{4}/[0-9]{2}/[a-f0-9]{12,64}-[^/?]*user-manual[^/?]*\.pdf$"],
            include_terms=[],
            max_links=200,
        )
    if source["id"] == "id-kemnaker-regulations":
        # The ministry renders a server-side catalogue with bounded pagination,
        # detail records, and originals under one exact path.
        # Keep unrelated news, regional laws, training and informational pages
        # out of the labour-law crawl.
        result.update(
            catalog_patterns=[root_pattern, r"^/peraturan\?hal=[1-9][0-9]{0,3}$"],
            detail_patterns=[r"^/peraturan/detail/[1-9][0-9]{0,6}/[a-z0-9-]{1,180}$"],
            document_patterns=[r"^/asset/data_puu/[^/?#]{1,240}\.(?:pdf|docx?)$"],
            exclude_terms=[
                *result["exclude_terms"],
                "berita",
                "glosarium",
                "infografis",
                "artikel",
                "monografi",
                "pelatihan",
                "panduan",
                "tutorial",
                "peraturan daerah",
                "putusan pengadilan",
            ],
            max_links=200,
        )
    if rcep_policy(source):
        result["exclude_patterns"] = [RCEP_OUTSIDE_SCOPE]
    instrument = source.get("setneg_type", "PERPU" if source["id"] == SETNEG_ID else None)
    if instrument:
        result.update(
            setneg_type=instrument,
            required_query_by_host={"jdih.setneg.go.id": {"jns": instrument}},
            max_links=51,
        )
    return result


def request_url(source, url):
    """Use the registry's fixed page size without changing the source identity URL."""
    discovery = source.get("discovery")
    if discovery and discovery.get("adapter") == "bi_official":
        if bi_official_discovery.page_number(url) is not None:
            return bi_official_discovery.network_catalog_url(url)
        return url
    if discovery and discovery.get("adapter") == "bi_jdih":
        if bi_jdih_discovery.purpose(url) == "catalog":
            return source["url"]
        return url
    if discovery and discovery.get("adapter") == "dephub_regulations":
        return url
    if discovery and discovery.get("adapter") == "treaty_room_public":
        return url
    if discovery is None or url != source["url"]:
        return url
    query = urlencode(
        {
            "PageSize": discovery["page_size"],
            "SortBy": "Title",
            "SortOrder": "ASC",
        }
    )
    return f"{url}?{query}"


def source_reference_fields(source, link, parent_hash, parent_url=None):
    """Keep SSO row identity and source-byte location bound to the parent page."""
    discovery = source.get("discovery", {})
    if discovery.get("adapter") == "bi_official":
        record = link.get("source_record")
        if record is None:
            return {}
        if not isinstance(record, dict):
            raise ValueError("Bank Indonesia attachment source record is invalid")
        fields = record.get("fields")
        structure = record.get("structure")
        if not isinstance(fields, dict) or not isinstance(structure, dict):
            raise ValueError("Bank Indonesia attachment source record is incomplete")
        metadata = fields.get("page_metadata")
        detail_url = fields.get("detail_page_url")
        detail_id = structure.get("parent_detail_id")
        if (
            record.get("provider") != "bi_official"
            or record.get("interpretation") != "unreviewed_source_values"
            or set(fields)
            != {
                "detail_page_url",
                "detail_page_title",
                "detail_page_sha256",
                "page_metadata",
                "attachment_url",
                "attachment_label",
                "attachment_role",
                "filename",
                "filename_identity_verified",
            }
            or set(structure)
            != {
                "unit_type",
                "number",
                "title",
                "instrument_type",
                "parent_titles",
                "file_name",
                "anchor_class",
                "link_mechanism",
                "parent_detail_id",
            }
            or not isinstance(detail_url, str)
            or detail_url != parent_url
            or fields.get("detail_page_sha256") != parent_hash
            or not isinstance(metadata, dict)
            or len(metadata) > 20
            or any(
                not isinstance(key, str)
                or not 1 <= len(key) <= 120
                or not isinstance(value, str)
                or len(value) > 1_800
                for key, value in metadata.items()
            )
            or fields.get("attachment_url") != link.get("url")
            or not isinstance(fields.get("detail_page_title"), str)
            or not 1 <= len(fields["detail_page_title"]) <= 1_000
            or not isinstance(fields.get("attachment_label"), str)
            or not 1 <= len(fields["attachment_label"]) <= 500
            or fields.get("attachment_role")
            not in {"document_candidate", "official_attachment", "faq", "english_version"}
            or not isinstance(fields.get("filename"), str)
            or not 1 <= len(fields["filename"]) <= 240
            or fields.get("filename_identity_verified") is not False
            or structure.get("unit_type") != "official_attachment_candidate"
            or detail_id != urlsplit(detail_url).path.rsplit("/", 1)[-1]
            or structure.get("number") is not None
            or structure.get("parent_titles") != []
            or structure.get("link_mechanism") != "official_detail_page_attachment_link"
            or structure.get("title") != fields["detail_page_title"]
        ):
            raise ValueError("Bank Indonesia attachment source record is invalid or detached")
        return {"source_record": copy.deepcopy(record)}
    if discovery.get("adapter") == "bi_jdih":
        record = link.get("source_record")
        if record is None:
            return {}
        if not isinstance(record, dict):
            raise ValueError("Bank Indonesia JDIH source record is invalid")
        fields = record.get("fields")
        structure = record.get("structure")
        identity = link.get("identity_key")
        match = re.fullmatch(r"bi:([1-9][0-9]{0,11})", identity) if isinstance(identity, str) else None
        identifier = match.group(1) if match else None
        expected_fields = {
            "number",
            "title",
            "instrument_type",
            "issuer",
            "established_date",
            "promulgated_date",
            "effective_date",
            "status",
            "official_source",
            "legal_basis",
            "amends",
            "repeals",
            "amended_by",
            "repealed_by",
            "related_regulations",
        }
        if (
            record.get("provider") != "bi_jdih"
            or record.get("interpretation") != "unreviewed_source_values"
            or not isinstance(fields, dict)
            or not fields
            or len(fields) > len(expected_fields)
            or not set(fields).issubset(expected_fields)
            or not isinstance(structure, dict)
            or set(structure)
            != {
                "unit_type",
                "number",
                "title",
                "instrument_type",
                "parent_titles",
                "file_name",
                "anchor_class",
                "link_mechanism",
            }
            or not identifier
            or link.get("purpose") != "document"
            or link.get("url") != f"https://jdih.bi.go.id/api/WebJDIH/GetFilePeraturan/{identifier}"
            or not isinstance(parent_url, str)
            or parent_url
            != "https://jdih.bi.go.id/api/WebJDIH/GetDataWebPeraturan?"
            + urlencode({"PeraturanID": identifier})
            or not isinstance(parent_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", parent_hash)
            or structure.get("unit_type") != "official_regulation_metadata_candidate"
            or structure.get("number") != fields.get("number")
            or structure.get("title") != fields.get("title")
            or structure.get("file_name") != f"BI-JDIH-{identifier}.pdf"
            or structure.get("link_mechanism") != "read_only_official_metadata_api"
            or link.get("title") != fields.get("title")
        ):
            raise ValueError("Bank Indonesia JDIH source record is invalid or detached")
        return {
            "identity_key": identity,
            "source_record": copy.deepcopy(record),
        }
    if discovery.get("adapter") == "treaty_room_public":
        record = link.get("source_record")
        if record is None:
            return {}
        from app.contexts.ingestion.infrastructure.source_adapters.treaty_room_discovery import (
            validate_candidate_reference,
        )

        fields = record.get("fields") if isinstance(record, dict) else None
        identity = link.get("identity_key")
        if (
            link.get("purpose") != "document"
            or not isinstance(fields, dict)
            or link.get("title") != fields.get("title", "")[:300]
        ):
            raise ValueError("TreatyRoom 原件标题或用途与目录记录不一致")
        validate_candidate_reference(
            record,
            parent_url=parent_url,
            parent_hash=parent_hash,
            target_url=link.get("url"),
            identity_key=identity,
        )
        return {"identity_key": identity, "source_record": copy.deepcopy(record)}
    if discovery.get("adapter") != "sso_catalog_2026":
        return {}
    if link.get("purpose") == "catalog":
        return {}
    identity = link.get("identity_key")
    locator = link.get("source_locator")
    category = discovery["category"].casefold()
    if (
        not isinstance(identity, str)
        or not re.fullmatch(rf"sso:{re.escape(category)}:[a-z0-9_-]{{1,80}}", identity)
        or not isinstance(locator, dict)
        or locator.get("source_sha256") != parent_hash
        or locator.get("table_class") != "browse-list"
        or locator.get("unit") != "decoded_unicode_char"
        or type(locator.get("row")) is not int
        or not 1 <= locator["row"] <= discovery["page_size"]
        or type(locator.get("start")) is not int
        or type(locator.get("end")) is not int
        or not 0 <= locator["start"] < locator["end"] <= 4 * 1024 * 1024
        or not re.fullmatch(r"[a-f0-9]{64}", str(locator.get("fragment_sha256", "")))
        or link.get("source_status") != "Current"
        or (
            link.get("document_date") is not None
            and not re.fullmatch(r"\d{8}(?:\d{6})?", str(link["document_date"]))
        )
    ):
        raise ValueError("SSO catalogue row identity or locator is invalid")
    return {
        "identity_key": identity,
        "source_locator": locator,
        "document_date": link.get("document_date"),
        "source_status": link["source_status"],
    }


def _revalidate_beacukai_discovery(self):
    """Reclassify this legacy catalogue from verified cached HTML without GETs."""
    source = next((row for row in self.sources if row["id"] == "id-beacukai-regulations"), None)
    if source is None:
        return
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _Recipe

    current = self.state["sources"].setdefault(source["id"], {})
    policy_hash = hashlib.sha256(
        json.dumps(recipe(source), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if current.get("discovery_policy_hash") == policy_hash:
        return
    cache = current.setdefault("cache", {})
    frontier = current.setdefault("frontier", {})
    rules = _Recipe(recipe(source))
    archived = reclassified = 0
    for url, item in list(frontier.items()):
        purpose = rules.purpose(url)
        if purpose is None:
            self.event(
                "frontier_archived",
                source_id=source["id"],
                url=url,
                mapping=item,
                reason="discovery_scope_revalidated",
            )
            del frontier[url]
            archived += 1
        elif item.get("purpose") != purpose:
            item["purpose"] = purpose
            reclassified += 1
    cached = cache.get(source["url"])
    replay_status, root_hash = "no_cached_root", None
    if cached and valid_cached(self.root, cached):
        raw = (self.root / cached["path"]).read_bytes()
        inspected = discover_page(source["url"], raw, cached["mime_type"], recipe(source))
        if inspected.get("matched"):
            refreshed = {**cached, "discovery": inspected}
            cache[source["url"]] = refreshed
            self.map_links(source, source["url"], refreshed, cached, frontier, cache, deque())
            replay_status, root_hash = "verified_cached_discovery", cached["content_hash"]
        else:
            replay_status = "cached_discovery_unmatched"
    elif cached:
        replay_status = "cached_integrity_failed"
    before_hash = current.get("discovery_policy_hash")
    current["discovery_policy_hash"] = policy_hash
    # A changed policy may expose useful targets immediately. Their GETs still
    # pass the original per-source, per-host and daily budgets.
    if replay_status == "verified_cached_discovery" and current.get("health") == "healthy":
        current["next_at"] = max(
            time.time() + self.limits["tick_seconds"],
            current.get("retry_not_before") or 0,
            max((self.host_cooldowns.get(h, 0) for h in source["allowed_hosts"]), default=0),
        )
    self.event(
        "discovery_policy_revalidated",
        source_id=source["id"],
        previous_policy_hash=before_hash,
        policy_hash=policy_hash,
        replay_status=replay_status,
        root_hash=root_hash,
        archived_frontier=archived,
        reclassified_frontier=reclassified,
        frontier_count=len(frontier),
        network_requests=0,
    )
    self.save()


def valid_cached(root, cached):
    if not cached:
        return False
    path = root / cached["path"]
    return bool(
        path.resolve().is_relative_to(root)
        and not path.is_symlink()
        and path.is_file()
        and path.stat().st_size == cached["size"]
        and sha(path) == cached["content_hash"]
    )


class Collector:
    def __init__(self, pool, root, country, *, limits=None):
        self.pool_path, self.root, self.country = (
            Path(pool).resolve(),
            Path(root).resolve(),
            country,
        )
        if not self.root.is_relative_to(ROOT) or self.root == ROOT:
            raise ValueError("Collection output must remain in this project")
        self.limits = {**LIMITS, **(limits or {})}
        if any(type(v) is not int or v < 1 for v in self.limits.values()):
            raise ValueError("Collection limits must be positive integers")
        if self.limits["workers"] > 8 or self.limits["per_host"] > 2:
            raise ValueError("Unattended concurrency exceeds verified limits")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.root / "state.json"
        saved = load(self.state_path) if self.state_path.exists() else None
        if saved is None and any(p.name != ".collector.lock" for p in self.root.iterdir()):
            raise ValueError("Missing state for existing evidence; refusing an empty replacement")
        self.catalog = SourceCatalog(
            JsonSourceRegistry(self.pool_path, self.root / "registry-versions"),
            _validated_url,
            previous=saved["binding"]["pool_sha256"] if saved else None,
        )
        self.sources = self.catalog.sources(country)
        self.binding = {
            "protocol": PROTOCOL,
            "pool_sha256": self.catalog.current.digest,
            "country": country,
            "root": str(self.root),
            "limits": self.limits,
        }
        if saved is not None:
            self.state = saved
            if self.state["binding"] != self.binding:
                raise ValueError("Collector scope changed; preserve and explicitly migrate its state")
        else:
            self.state = {
                "binding": self.binding,
                "sources": {},
                "active_sources": [],
                "status": "ready",
            }
            self.save()
        self.catalog.accept(self.catalog.current)
        self.identities = self.state.setdefault("source_countries", {})
        self.identities.update({s["id"]: s["country"] for s in self.catalog.current.payload["sources"]})
        self._doh_hosts = set(filter(None, os.getenv("COLLECTION_DOH_HOSTS", "").split(",")))
        self.configure_transport()
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.host_slots = {}
        self.host_cooldowns_path = self.root / "host-cooldowns.json"
        self.host_cooldowns = load(self.host_cooldowns_path) if self.host_cooldowns_path.exists() else {}
        max_document_bytes = max(
            (
                source.get("collection_limits", {}).get("max_document_bytes", 0)
                for source in self.sources
            ),
            default=0,
        )
        self.blobs = BlobStore(
            self.root / "files",
            max_bytes=max(self.limits["max_response_bytes"], max_document_bytes),
        )
        self.request_day = None
        self.requests = {}
        self.roll_budget()
        # An interrupted GET is idempotent and remains charged to its original
        # day. Recover scheduling only; never reset attempts or old evidence.
        for identifier in self.state.get("active_sources", []):
            source = next(s for s in self.sources if s["id"] == identifier)
            previous = self.state["sources"].get(identifier, {})
            self.state["sources"][identifier] = observe(
                previous,
                {
                    "status": "network_error",
                    "error_type": "InterruptedGET",
                    "url": source["url"],
                },
                now=time.time(),
                interval_seconds=source["interval_seconds"],
            )
        self.state["active_sources"] = []
        self.reload_sources()
        self.apply_candidate_policy()
        _revalidate_beacukai_discovery(self)
        self.save()

    def configure_transport(self):
        hosts = {
            host
            for source in self.sources
            if source.get("transport") == "proxy"
            for host in (*source["allowed_hosts"], *source.get("html_asset_hosts", []))
        }
        if len(hosts) > 64:
            raise ValueError("Collection proxy scope exceeds 64 hosts")
        if hosts or self.catalog.current.payload["protocol"] == "aitea-official-source-pool-v2":
            os.environ["COLLECTION_PROXY_HOSTS"] = ",".join(sorted(hosts))
            os.environ["COLLECTION_DOH_HOSTS"] = ",".join(sorted(self._doh_hosts & hosts))

    def reload_sources(self):
        try:
            revision = self.catalog.candidate(self.identities)
            if revision is None:
                self.state.pop("registry_error", None)
                return
            if (
                revision.payload.get("protocol") == "aitea-official-source-pool-v2"
                and revision.digest != self.binding["pool_sha256"]
            ):
                raise PoolMigrationRequired("Source pool changes require an explicit ledger migration and restart")
            candidate = active_sources(revision.payload, self.country, _validated_url)
            if len({h for s in candidate for h in (*s["allowed_hosts"], *s.get("html_asset_hosts", []))}) > 64:
                raise ValueError("Collection proxy scope exceeds 64 hosts")
            before = {s["id"]: s for s in self.sources}
            after = {s["id"]: s for s in candidate}
            old_digest = self.catalog.current.digest
            self.catalog.accept(revision)
            self.sources = candidate
            self.configure_transport()
            self.binding["pool_sha256"] = revision.digest
            self.state["binding"] = self.binding
            self.identities.update({s["id"]: s["country"] for s in revision.payload["sources"]})
            self.state.pop("registry_error", None)
            self.event(
                "source_registry_reloaded",
                previous_sha256=old_digest,
                pool_sha256=revision.digest,
                added=sorted(after.keys() - before.keys()),
                removed=sorted(before.keys() - after.keys()),
                changed=sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
            )
            self.save()
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Last good revision and every receipt/budget remain intact; log no raw configuration.
            failure = {
                "code": "source_pool_migration_required"
                if isinstance(error, PoolMigrationRequired)
                else "invalid_source_registry",
                "error_type": type(error).__name__,
            }
            if self.state.get("registry_error") != failure:
                self.event("source_registry_rejected", **failure)
            self.state["registry_error"] = failure

    def save(self):
        self.state["updated_at"] = time.time()
        write(self.state_path, self.state)

    def event(self, kind, **values):
        directory = self.root / "events" / datetime.now(UTC).strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        write(
            directory / (str(time.time_ns()) + "-" + uuid.uuid4().hex + ".json"),
            {"kind": kind, "at": time.time(), **values},
            exclusive=True,
        )

    def roll_budget(self):
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if day != self.request_day:
            directory = self.root / "requests" / day
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            files = list(directory.glob("*.json"))
            if len(files) > self.limits["daily_requests"]:
                raise ValueError("Daily receipt inventory exceeds its fixed request bound")
            self.requests = {path.stem: load(path) for path in files}
            self.request_day = day

    def response_limit(self, source, url):
        collection_limits = source.get("collection_limits", {})
        adapter = source.get("discovery", {}).get("adapter")
        purpose = page_purpose(url, recipe(source))
        if adapter == "bi_jdih" and purpose == "catalog":
            return bi_jdih_discovery.MAX_HTML_BYTES
        if adapter == "bi_jdih" and purpose == "detail":
            return bi_jdih_discovery.MAX_JSON_BYTES
        if adapter == "treaty_room_public" and purpose == "catalog":
            return treaty_room_discovery.MAX_JSON_BYTES
        if purpose == "document":
            return collection_limits.get("max_document_bytes", self.limits["max_response_bytes"])
        return self.limits["max_response_bytes"]

    def next_due_request(self, source, current, now):
        cache = current.get("cache", {})
        if (
            now >= current.get("root_next_at", 0)
            or source["url"] not in cache
            or current.get("force_root_refresh") is True
        ):
            return source["url"]
        adapter = source.get("discovery", {}).get("adapter")
        is_catalog_adapter = adapter in {
            "sso_catalog_2026",
            "bi_official",
            "bi_jdih",
            "dephub_regulations",
            "treaty_room_public",
        }
        candidates = []
        for url, item in current.get("frontier", {}).items():
            if (
                url == source["url"]
                or scope_excluded(source, url, item.get("title", ""))
                or urlsplit(url).hostname not in source["allowed_hosts"]
                or not due(item.get("health"), now)
            ):
                continue
            candidates.append(
                (
                    (
                        -int(is_catalog_adapter and item.get("purpose") == "catalog"),
                        item.get("attempt_count", 0),
                        -int(item.get("purpose") == "document"),
                        url,
                    ),
                    url,
                )
            )
        return min(candidates, default=(None, None))[1]

    def budget_fingerprint(self, source_id):
        own = [row for row in self.requests.values() if row.get("source_id") == source_id]
        snapshot = (
            self.request_day,
            len(self.requests),
            sum(_charged_bytes(row) for row in self.requests.values()),
            len(own),
            sum(_charged_bytes(row) for row in own),
        )
        return hashlib.sha256(json.dumps(snapshot, separators=(",", ":")).encode()).hexdigest()

    def request_fits_budget(self, source, url, now):
        network_url = request_url(source, url)
        host = urlsplit(network_url).hostname
        if (
            host not in source["allowed_hosts"]
            or self.host_cooldowns.get(host, 0) > now
            or (source.get("transport") == "proxy" and proxy_for_host(host) is None)
        ):
            return False
        if request_window_wait_until(source, now) is not None:
            return False
        own = [row for row in self.requests.values() if row.get("source_id") == source["id"]]
        if len(self.requests) >= self.limits["daily_requests"]:
            return False
        if len(own) >= self.limits["daily_source_requests"]:
            return False
        limit = self.response_limit(source, url)
        source_limit = source.get("collection_limits", {}).get(
            "daily_bytes", self.limits["daily_source_bytes"]
        )
        if (
            sum(_charged_bytes(row) for row in self.requests.values()) + limit > self.limits["daily_bytes"]
            or sum(_charged_bytes(row) for row in own) + limit > source_limit
            or shutil.disk_usage(self.root).free
            < self.limits["min_free_bytes"] + max(128 * 1024**2, limit)
        ):
            return False
        return True

    def release_recovered_daily_quotas(self, now):
        """Wake a quota-paused source when receipt reconciliation frees a real slot."""
        for source in self.sources:
            current = self.state.get("sources", {}).get(source["id"])
            observation = (current or {}).get("last_observation") or {}
            if (
                not isinstance(current, dict)
                or observation.get("status") != "budget_wait"
                or observation.get("error_type") != "DailyQuota"
            ):
                continue
            retry_after = observation.get("retry_after_seconds")
            observed_at = observation.get("at")
            if (
                isinstance(retry_after, bool)
                or not isinstance(retry_after, (int, float))
                or not math.isfinite(retry_after)
                or retry_after < 0
                or isinstance(observed_at, bool)
                or not isinstance(observed_at, (int, float))
                or not math.isfinite(observed_at)
            ):
                continue
            quota_until = observed_at + retry_after
            retry_not_before = current.get("retry_not_before", 0)
            next_at = current.get("next_at", 0)
            if (
                isinstance(retry_not_before, bool)
                or not isinstance(retry_not_before, (int, float))
                or not math.isfinite(retry_not_before)
                or isinstance(next_at, bool)
                or not isinstance(next_at, (int, float))
                or not math.isfinite(next_at)
                or retry_not_before > quota_until + 60
                or next_at > quota_until + 60
            ):
                continue
            fingerprint = self.budget_fingerprint(source["id"])
            if current.get("quota_recheck_fingerprint") == fingerprint:
                continue
            url = self.next_due_request(source, current, now)
            if url is None or not self.request_fits_budget(source, url, now):
                continue
            adjusted = [
                row
                for row in self.requests.values()
                if row.get("source_id") == source["id"]
                and row.get("charged_bytes", 0) > _charged_bytes(row)
            ]
            released_bytes = sum(row.get("charged_bytes", 0) - _charged_bytes(row) for row in adjusted)
            current["next_at"] = now
            current.pop("retry_not_before", None)
            current["quota_recheck_fingerprint"] = fingerprint
            self.event(
                "daily_quota_released",
                source_id=source["id"],
                released_bytes=released_bytes,
                adjusted_request_count=len(adjusted),
                next_request_limit=self.response_limit(source, url),
            )

    def reserve(self, source, url, *, response_limit=None):
        with self.mutex:
            self.roll_budget()
            if self.stop.is_set() or (self.root / "STOP").exists():
                raise Deferred("stopping")
            wait = self.host_cooldowns.get(urlsplit(url).hostname, 0) - time.time()
            if wait > 0:
                raise Deferred("budget_wait", retry_after=wait, reason="HostCooldown")
            limit = self.limits["max_response_bytes"] if response_limit is None else response_limit
            if type(limit) is not int or limit < 1:
                raise ValueError("Response limit must be a positive integer")
            disk_reserve = max(128 * 1024**2, limit)
            if shutil.disk_usage(self.root).free < self.limits["min_free_bytes"] + disk_reserve:
                raise Deferred("storage_wait")
            own = [row for row in self.requests.values() if row["source_id"] == source["id"]]
            source_daily_bytes = source.get("collection_limits", {}).get(
                "daily_bytes", self.limits["daily_source_bytes"]
            )
            if (
                len(self.requests) >= self.limits["daily_requests"]
                or len(own) >= self.limits["daily_source_requests"]
                or sum(_charged_bytes(row) for row in self.requests.values()) + limit
                > self.limits["daily_bytes"]
                or sum(_charged_bytes(row) for row in own) + limit > source_daily_bytes
            ):
                raise Deferred(
                    "budget_wait",
                    retry_after=(int(time.time()) // 86400 + 1) * 86400 + 5 - time.time(),
                    reason="DailyQuota",
                )
            identifier = uuid.uuid4().hex
            row = {
                "request_id": identifier,
                "pool_sha256": self.catalog.current.digest,
                "source_id": source["id"],
                "url": url,
                "day": self.request_day,
                "started_at": time.time(),
                "status": "issued",
                "charged_bytes": limit,
            }
            path = self.root / "requests" / self.request_day / (identifier + ".json")
            write(path, row, exclusive=True)
            self.requests[identifier] = row
            return path, row

    def cooldown(self, host, until):
        with self.mutex:
            self.host_cooldowns[host] = max(self.host_cooldowns.get(host, 0), until)
            write(self.host_cooldowns_path, self.host_cooldowns)

    @contextmanager
    def slot(self, host, source, *, timeout_seconds=None):
        if host not in source["allowed_hosts"]:
            raise DownloadError("Redirect host is outside the reviewed official source scope")
        if source.get("transport") == "proxy" and proxy_for_host(host) is None:
            raise DownloadError("Required collection proxy is not configured for this source")
        with self.mutex:
            semaphore = self.host_slots.setdefault(host, threading.BoundedSemaphore(self.limits["per_host"]))
        timeout = self.limits["timeout_seconds"] if timeout_seconds is None else timeout_seconds
        if not semaphore.acquire(timeout=timeout):
            raise DownloadError("Official source concurrency wait exceeded deadline", retryable=True)
        try:
            yield
        finally:
            semaphore.release()

    def fetch(self, source, url, cached=None, *, parent_cached=None):
        collection_limits = source.get("collection_limits", {})
        adapter = source.get("discovery", {}).get("adapter")
        purpose = page_purpose(url, recipe(source))
        is_document = purpose == "document"
        response_limit = self.response_limit(source, url)
        timeout_seconds = (
            collection_limits.get("timeout_seconds", self.limits["timeout_seconds"])
            if is_document
            else self.limits["timeout_seconds"]
        )
        path, receipt = self.reserve(source, url, response_limit=response_limit)
        outcome = None
        network_url = request_url(source, url)
        receipt["network_url"] = network_url
        try:
            validators = {}
            cached_request_url = (cached or {}).get("requested_url", (cached or {}).get("url"))
            is_bi_official = adapter == "bi_official"
            is_bi_jdih = adapter == "bi_jdih"
            page = bi_official_discovery.page_number(url) if is_bi_official else None
            if (
                valid_cached(self.root, cached)
                and cached_request_url == network_url
                and (not is_bi_official or page in (None, 1))
            ):
                validators = {k: cached.get(k) for k in ("etag", "last_modified")}
            instrument = source.get("setneg_type", "PERPU" if source["id"] == SETNEG_ID else None)
            request_options = setneg_request_parameters(network_url, instrument) if instrument else {}
            if adapter == "treaty_room_public":
                request_options["accept_compressed_json"] = True
            if is_bi_official:
                request_options["max_compressed_html_bytes"] = bi_official_discovery.MAX_HTML_BYTES
                if page is not None and page > 1:
                    parent_url = bi_official_discovery.previous_catalog_url(url)
                    parent_scope = ((parent_cached or {}).get("discovery") or {}).get("scope", {})
                    if (
                        not valid_cached(self.root, parent_cached)
                        or (parent_cached or {}).get("queued_url", (parent_cached or {}).get("url"))
                        != parent_url
                        or parent_scope.get("page") != page - 1
                        or (parent_cached or {}).get("discovery", {}).get("purpose") != "catalog"
                    ):
                        raise DownloadError(
                            "央行目录下一页缺少已校验的相邻父页。", retryable=True
                        )
                    parent_path = self.root / parent_cached["path"]
                    parent_content = parent_path.read_bytes()
                    if len(parent_content) > bi_official_discovery.MAX_HTML_BYTES:
                        raise DownloadError("央行目录父页超过解析大小上限。")
                    fields, evidence = bi_official_discovery.catalog_form_request(
                        parent_url, parent_content, page
                    )
                    validators = {}
                    request_options.update(
                        form_fields=fields,
                        required_host=bi_official_discovery.HOST,
                    )
                    receipt["request_evidence"] = {
                        "method": "POST",
                        "queued_url": url,
                        "network_url": network_url,
                        "parent_url": parent_url,
                        "parent_content_hash": parent_cached["content_hash"],
                        "parent_page": evidence["parent_page"],
                        "target_page": evidence["target_page"],
                        "event_target": evidence["event_target"],
                        "image_submit": evidence["image_submit"],
                    }
                else:
                    receipt["request_evidence"] = {
                        "method": "GET",
                        "queued_url": url,
                        "network_url": network_url,
                    }
            elif is_bi_jdih:
                if purpose == "catalog":
                    request_options["max_compressed_html_bytes"] = bi_jdih_discovery.MAX_HTML_BYTES
                receipt["request_evidence"] = {
                    "method": "GET",
                    "queued_url": url,
                    "network_url": network_url,
                }
            elif not request_options.get("form_fields") and not request_options.get("json_body"):
                receipt["request_evidence"] = {
                    "method": "GET",
                    "queued_url": url,
                    "network_url": network_url,
                }
            if request_options.get("json_body"):
                validators = {}
            fetched = _download(
                network_url,
                deadline=time.monotonic() + timeout_seconds,
                consume=lambda chunks: self.blobs.put(
                    "official-updates:" + self.country + ":" + source["id"], chunks
                ),
                validators=validators,
                connection_slot=lambda host: self.slot(host, source, timeout_seconds=timeout_seconds),
                accept_compressed_html=True,
                max_bytes=response_limit,
                **request_options,
            )
            if fetched.get("not_modified"):
                if fetched["url"] != network_url or not valid_cached(self.root, cached):
                    raise ValueError("304 lacks its verified matching cached original")
                asset = {**cached, "observed_at": time.time()}
                outcome = {
                    "status": "success",
                    "http_status": 304,
                    "url": url,
                    "content_hash": asset["content_hash"],
                    "not_modified": True,
                }
                receipt["charged_bytes"] = 0
            else:
                if (is_bi_official or is_bi_jdih) and fetched["url"] != network_url:
                    raise DownloadError("央行官方目录请求重定向到了未核验页面。")
                if receipt.get("request_evidence") is not None:
                    receipt["request_evidence"].update(
                        method=fetched.get("request_method", receipt["request_evidence"]["method"]),
                        request_body_sha256=fetched.get("request_body_sha256"),
                    )
                blob = fetched["blob"]
                with blob.path.open("rb") as stream:
                    inspection_url = url if is_bi_official or is_bi_jdih else fetched["url"]
                    inspection_limit = (
                        page_body_limit(inspection_url, fetched["mime_type"], recipe(source))
                        if is_bi_official or is_bi_jdih
                        else treaty_room_discovery.MAX_JSON_BYTES
                        if adapter == "treaty_room_public"
                        else 4 * 1024**2
                    )
                    content = stream.read(inspection_limit + 1)
                inspected = discover_page(inspection_url, content, fetched["mime_type"], recipe(source))
                if content.startswith(b"%PDF-"):
                    inspected = {
                        "matched": True,
                        "purpose": "document",
                        "links": [],
                        "parser_verification": "deferred_to_bounded_parser",
                    }
                elif content.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")) and (
                    source["url"] == url or Path(urlsplit(url).path).suffix.lower() in {".doc", ".docx"}
                ):
                    inspected = {
                        "matched": True,
                        "purpose": "document",
                        "links": [],
                        "parser_verification": "deferred_to_bounded_parser",
                    }
                empty = (
                    source["kind"] == "catalogue"
                    and source.get("empty_catalogue") is True
                    and url == source["url"]
                    and b"belum ada peraturan" in content.lower()
                    and not any(link.get("purpose") == "document" for link in inspected.get("links", []))
                )
                if not inspected.get("matched") or empty:
                    outcome = {
                        "status": "content_invalid",
                        "http_status": fetched.get("response_status", 200),
                        "url": url,
                        "error_type": "EmptyCatalogue" if empty else "UnverifiedOrChallengeContent",
                    }
                    asset = None
                else:
                    asset = {
                        "url": fetched["url"],
                        "queued_url": url,
                        "requested_url": network_url,
                        "path": str(blob.path.relative_to(self.root)),
                        "content_hash": blob.digest,
                        "size": blob.size,
                        "mime_type": fetched["mime_type"],
                        "etag": fetched.get("etag"),
                        "last_modified": fetched.get("last_modified"),
                        "observed_at": time.time(),
                        "discovery": inspected,
                        "request_evidence": copy.deepcopy(receipt.get("request_evidence")),
                        "legal_review": "unreviewed",
                        "source_id": source["id"],
                        "country": self.country,
                    }
                    outcome = {
                        "status": "success",
                        "http_status": fetched.get("response_status", 200),
                        "url": url,
                        "content_hash": blob.digest,
                    }
                receipt["charged_bytes"] = blob.size
                receipt["raw_response"] = {
                    "path": str(blob.path.relative_to(self.root)),
                    "content_hash": blob.digest,
                    "size": blob.size,
                }
            receipt.update(outcome=outcome, asset=asset, status="finished", finished_at=time.time())
        except DownloadError as error:
            if error.bytes_received is not None:
                receipt["charged_bytes"] = error.bytes_received
            status = re.search(r"HTTP (\d{3})(?!\d)", str(error))
            failure_code = download_failure_code(error)
            network_failure = error.retryable or failure_code in {
                "dns_resolution_failed",
                "dns_no_addresses",
                "dns_timeout",
                "dns_timeout_or_capacity",
                "network_timeout",
                "network_transport_error",
                "request_deadline_exceeded",
                "connection_refused",
                "connection_interrupted",
                "http_protocol_error",
                "tls_certificate_verification_failed",
                "tls_handshake_failed",
                "transient_network_error",
            }
            outcome = {
                "status": (
                    "http_error" if status else "network_error" if network_failure else "content_invalid"
                ),
                "http_status": int(status[1]) if status else None,
                "error_type": type(error).__name__,
                "failure_code": failure_code,
                "retry_after_seconds": error.retry_after,
                "url": url,
            }
            receipt.update(status="finished", outcome=outcome, asset=None, finished_at=time.time())
            if outcome["http_status"] in {401, 403, 429} or outcome.get("retry_after_seconds"):
                delay = outcome.get("retry_after_seconds") or (
                    86400 if outcome["http_status"] in {401, 403} else 60
                )
                self.cooldown(error.response_host or urlsplit(url).hostname, time.time() + delay)
        except Exception as error:
            outcome = {
                "status": "content_invalid",
                "error_type": type(error).__name__,
                "url": url,
            }
            receipt.update(status="finished", outcome=outcome, asset=None, finished_at=time.time())
        with self.mutex:
            write(path, receipt)
            if receipt["day"] == self.request_day:
                self.requests[receipt["request_id"]] = receipt
        return receipt

    def apply_candidate_policy(self):
        """Replay verified cached discovery once, without GETs, receipts or quota changes."""
        for source in self.sources:
            policy_version = candidate_policy_version(source)
            current = self.state["sources"].get(source["id"], {})
            previous_version = current.get("candidate_policy", {}).get("version")
            if policy_version is None:
                if not (
                    isinstance(previous_version, str)
                    and previous_version.startswith(DISCOVERY_EXCLUSION_POLICY_VERSION + ":")
                ):
                    continue
                policy_version = f"{DISCOVERY_EXCLUSION_POLICY_VERSION}:none"
            current = self.state["sources"].setdefault(source["id"], {})
            if current.get("candidate_policy", {}).get("version") == policy_version:
                continue
            cache = current.get("cache", {})
            frontier = current.setdefault("frontier", {})
            excluded = 0
            for url, item in frontier.items():
                marker = scope_exclusion(source, url, item.get("title", ""))
                previous_marker = item.get("scope_excluded")
                if marker and previous_marker != marker:
                    self.event(
                        "candidate_scope_excluded",
                        source_id=source["id"],
                        target_url=url,
                        parent_url=item["parent_url"],
                        parent_hash=item["parent_hash"],
                        policy=marker,
                        retained_cached_hash=cache.get(url, {}).get("content_hash"),
                    )
                    item["scope_excluded"] = marker
                elif not marker and previous_marker:
                    self.event(
                        "candidate_scope_restored",
                        source_id=source["id"],
                        target_url=url,
                        parent_url=item["parent_url"],
                        parent_hash=item["parent_hash"],
                        previous_policy=previous_marker,
                        retained_cached_hash=cache.get(url, {}).get("content_hash"),
                    )
                    item.pop("scope_excluded", None)
                if marker:
                    excluded += 1
            cached = cache.get(source["url"])
            before = set(frontier)
            replay_status, root_hash = "no_cached_root", None
            if cached and valid_cached(self.root, cached):
                raw = (self.root / cached["path"]).read_bytes()
                inspected = discover_page(source["url"], raw, cached["mime_type"], recipe(source))
                if inspected.get("matched"):
                    refreshed = {**cached, "discovery": inspected}
                    cache[source["url"]] = refreshed
                    self.map_links(
                        source,
                        source["url"],
                        refreshed,
                        cached,
                        frontier,
                        cache,
                        deque(),
                    )
                    replay_status, root_hash = (
                        "verified_cached_discovery",
                        cached["content_hash"],
                    )
                else:
                    replay_status = "cached_discovery_unmatched"
            elif cached:
                replay_status = "cached_integrity_failed"
            added = len(set(frontier) - before)
            current["candidate_policy"] = {
                "version": policy_version,
                "applied_at": time.time(),
                "replay_status": replay_status,
                "root_hash": root_hash,
            }
            # Existing upstream/quota cooldowns remain lower bounds. Newly found
            # URLs may run before the unchanged root's next daily recheck.
            if added and current.get("health") == "healthy":
                current["next_at"] = max(
                    time.time() + self.limits["tick_seconds"],
                    current.get("retry_not_before") or 0,
                    max(
                        (self.host_cooldowns.get(h, 0) for h in source["allowed_hosts"]),
                        default=0,
                    ),
                )
            self.event(
                "candidate_policy_applied",
                source_id=source["id"],
                policy_version=policy_version,
                replay_status=replay_status,
                root_hash=root_hash,
                excluded_frontier=excluded,
                discovered_candidates=added,
                network_requests=0,
            )

    def map_links(self, source, url, asset, previous_asset, frontier, cache, urls):
        adapter = source.get("discovery", {}).get("adapter")
        is_sso = adapter == "sso_catalog_2026"
        is_bi_official = adapter == "bi_official"
        is_bi_jdih = adapter == "bi_jdih"
        is_dephub = adapter == "dephub_regulations"
        is_treaty_room = adapter == "treaty_room_public"
        is_catalog_adapter = is_sso or is_bi_official or is_bi_jdih or is_dephub or is_treaty_room
        frontier_limit = (
            source["discovery"]["max_records"] + source["discovery"]["max_pages"]
            if is_bi_official
            else source["discovery"]["max_records"] + bi_jdih_discovery.MAX_CATALOG_PAGES
            if is_bi_jdih
            else source["discovery"]["max_records"] + source["discovery"]["max_pages"]
            if is_dephub or is_treaty_room
            else self.limits["max_frontier"]
        )
        parent_url = url
        if is_sso and url == source["url"]:
            parent_url = asset.get("requested_url", url)
        source_catalog_offset = bi_jdih_discovery.catalog_offset(url) if is_bi_jdih else None
        if is_bi_jdih and source_catalog_offset is None:
            source_catalog_offset = frontier.get(url, {}).get("catalog_offset")
        dephub_max_pages = source.get("discovery", {}).get("max_pages", 2_000)
        source_catalog_page = (
            dephub_discovery.page_number(url, max_pages=dephub_max_pages) if is_dephub else None
        )
        if is_dephub and source_catalog_page is None:
            source_catalog_page = frontier.get(url, {}).get("catalog_page_number")
        links = asset.get("discovery", {}).get("links", [])
        if is_catalog_adapter:
            links = sorted(links, key=lambda link: link.get("purpose") != "catalog")
        for link in links:
            candidate, host = _validated_url(link["url"])
            if (
                host not in source["allowed_hosts"]
                or candidate == source["url"]
                or scope_excluded(source, candidate, link.get("title", ""))
            ):
                continue
            link_catalog_page = (
                bi_official_discovery.page_number(candidate)
                or bi_official_discovery.page_number(parent_url)
                or asset.get("catalog_page_number")
            ) if is_bi_official else None
            if is_treaty_room:
                link_catalog_page = int(
                    treaty_room_discovery.catalog_query(
                        candidate if link["purpose"] == "catalog" else parent_url
                    )["page"]
                )
            if is_dephub:
                link_catalog_page = (
                    dephub_discovery.page_number(candidate, max_pages=dephub_max_pages)
                    or source_catalog_page
                    or asset.get("catalog_page_number")
                )
            link_catalog_offset = (
                bi_jdih_discovery.catalog_offset(candidate)
                if is_bi_jdih and link["purpose"] == "catalog"
                else source_catalog_offset
            ) if is_bi_jdih else None
            cached_target = cache.get(candidate)
            if (
                is_bi_official
                and isinstance(cached_target, dict)
                and cached_target.get("source_parent_url") == parent_url
                and cached_target.get("source_parent_hash") == asset["content_hash"]
                and time.time() - cached_target.get("observed_at", 0) < 7 * 86400
            ):
                continue
            if (
                is_dephub
                and isinstance(cached_target, dict)
                and cached_target.get("source_parent_url") == parent_url
                and cached_target.get("source_parent_hash") == asset["content_hash"]
                and time.time() - cached_target.get("observed_at", 0) < 7 * 86400
            ):
                continue
            if is_sso and link["purpose"] != "catalog" and isinstance(cached_target, dict):
                same_immutable_version = (
                    link.get("document_date") is not None
                    and cached_target.get("source_document_date") == link.get("document_date")
                    and cached_target.get("source_original_verified") is True
                )
                if same_immutable_version or (
                    cached_target.get("source_original_verified") is True
                    and time.time() - cached_target.get("observed_at", 0) < 7 * 86400
                ):
                    continue
            parent = {"url": parent_url, "content_hash": asset["content_hash"]}
            reference_fields = source_reference_fields(source, link, asset["content_hash"], parent_url)
            if candidate in frontier:
                item = frontier[candidate]
                parents = item.get(
                    "parents",
                    [{"url": item["parent_url"], "content_hash": item["parent_hash"]}],
                )
                prior_parent = previous_asset or next(
                    (known for known in reversed(parents) if known["url"] == parent_url), None
                )
                parent_changed = (
                    prior_parent is not None and prior_parent["content_hash"] != asset["content_hash"]
                )
                if parent in parents and not parent_changed:
                    continue
                self.event(
                    "source_reference",
                    source_id=source["id"],
                    parent_url=parent_url,
                    parent_hash=asset["content_hash"],
                    target_url=candidate,
                    last_known_target_hash=cache.get(candidate, {}).get("content_hash"),
                    **reference_fields,
                )
                item.update(
                    parent_url=parent_url,
                    parent_hash=asset["content_hash"],
                    parents=([known for known in parents if known != parent] + [parent])[-8:],
                    **reference_fields,
                )
                if is_bi_official and link_catalog_page is not None:
                    item["catalog_page_number"] = min(
                        link_catalog_page,
                        item.get("catalog_page_number", link_catalog_page),
                    )
                if is_bi_jdih and link_catalog_offset is not None:
                    item["catalog_offset"] = min(
                        link_catalog_offset,
                        item.get("catalog_offset", link_catalog_offset),
                    )
                if is_dephub and link_catalog_page is not None:
                    item["catalog_page_number"] = min(
                        link_catalog_page,
                        item.get("catalog_page_number", link_catalog_page),
                    )
                if is_treaty_room and link_catalog_page is not None:
                    item["catalog_page_number"] = min(
                        link_catalog_page,
                        item.get("catalog_page_number", link_catalog_page),
                    )
                # A changed known parent version invalidates its target.
                # A first reference from a different page only adds a
                # mapping: shared navigation must not repeatedly consume
                # quota ahead of undiscovered originals.
                if parent_changed and item.get("health", {}).get("health") == "healthy":
                    item["health"]["next_at"] = time.time()
                if due(item.get("health"), time.time()):
                    urls.appendleft(candidate)
                continue
            if len(frontier) >= frontier_limit and is_catalog_adapter and link["purpose"] == "catalog":
                deferred_details = [
                    key for key, value in frontier.items() if value.get("purpose") != "catalog"
                ]
                if deferred_details:
                    if is_sso:
                        victim = min(
                            deferred_details,
                            key=lambda key: (
                                frontier[key].get("attempt_count", 0),
                                frontier[key].get("discovered_at", 0),
                                key,
                            ),
                        )
                    elif is_bi_jdih:
                        victim = max(
                            deferred_details,
                            key=lambda key: (
                                frontier[key].get("catalog_offset", 0),
                                frontier[key].get("discovered_at", 0),
                                key,
                            ),
                        )
                    else:
                        victim = max(
                            deferred_details,
                            key=lambda key: (
                                frontier[key].get("catalog_page_number", 0),
                                frontier[key].get("discovered_at", 0),
                                key,
                            ),
                        )
                    self.event(
                        "frontier_archived",
                        source_id=source["id"],
                        url=victim,
                        mapping=frontier.pop(victim),
                        reason=(
                            "sso_catalogue_pagination_priority"
                            if is_sso
                            else "bi_jdih_catalogue_pagination_priority"
                            if is_bi_jdih
                            else "dephub_catalogue_pagination_priority"
                            if is_dephub
                            else "treaty_room_catalogue_pagination_priority"
                            if is_treaty_room
                            else "bi_official_catalogue_pagination_priority"
                        ),
                    )
            if len(frontier) >= frontier_limit:
                if is_sso or link["purpose"] == "catalog":
                    # Cached catalogue snapshots are the durable backlog for
                    # rows that do not fit in the bounded in-memory frontier.
                    continue
                candidates = (
                    [key for key, value in frontier.items() if value.get("purpose") != "catalog"]
                    if is_bi_official or is_bi_jdih or is_dephub or is_treaty_room
                    else list(frontier)
                )
                if not candidates:
                    continue
                if is_bi_official:
                    victim = max(
                        candidates,
                        key=lambda key: (
                            frontier[key].get("catalog_page_number", 0),
                            frontier[key].get("discovered_at", 0),
                            key,
                        ),
                    )
                    victim_page = frontier[victim].get("catalog_page_number", 0)
                    if link_catalog_page is None or link_catalog_page > victim_page:
                        continue
                elif is_bi_jdih:
                    victim = max(
                        candidates,
                        key=lambda key: (
                            frontier[key].get("catalog_offset", 0),
                            frontier[key].get("discovered_at", 0),
                            key,
                        ),
                    )
                    victim_offset = frontier[victim].get("catalog_offset")
                    if link_catalog_offset is None or link_catalog_offset > (victim_offset or 0):
                        continue
                elif is_dephub:
                    victim = max(
                        candidates,
                        key=lambda key: (
                            frontier[key].get("catalog_page_number", 0),
                            frontier[key].get("discovered_at", 0),
                            key,
                        ),
                    )
                    victim_page = frontier[victim].get("catalog_page_number", 0)
                    if link_catalog_page is None or link_catalog_page > victim_page:
                        continue
                elif is_treaty_room:
                    victim = max(
                        candidates,
                        key=lambda key: (
                            frontier[key].get("catalog_page_number", 0),
                            frontier[key].get("discovered_at", 0),
                            key,
                        ),
                    )
                    victim_page = frontier[victim].get("catalog_page_number", 0)
                    if link_catalog_page is None or link_catalog_page > victim_page:
                        continue
                else:
                    victim = min(
                        candidates,
                        key=lambda k: (
                            -int(bool(frontier[k].get("attempt_count"))),
                            frontier[k].get("discovered_at", 0),
                            k,
                        ),
                    )
                self.event(
                    "frontier_archived",
                    source_id=source["id"],
                    url=victim,
                    mapping=frontier.pop(victim),
                    **(
                        {"reason": "bi_official_frontier_rotation"}
                        if is_bi_official
                        else {"reason": "bi_jdih_frontier_rotation"}
                        if is_bi_jdih
                        else {"reason": "dephub_frontier_rotation"}
                        if is_dephub
                        else {"reason": "treaty_room_frontier_rotation"}
                        if is_treaty_room
                        else {}
                    ),
                )
            frontier[candidate] = {
                "parent_url": parent_url,
                "parent_hash": asset["content_hash"],
                "parents": [parent],
                "title": link.get("title", ""),
                "purpose": link["purpose"],
                "attempt_count": 0,
                "discovered_at": time.time(),
                **(
                    {"catalog_page_number": link_catalog_page}
                    if is_bi_official or is_dephub or is_treaty_room
                    else {}
                ),
                **({"catalog_offset": link_catalog_offset} if is_bi_jdih and link_catalog_offset is not None else {}),
                **reference_fields,
            }
            self.event(
                "source_reference",
                source_id=source["id"],
                parent_url=parent_url,
                parent_hash=asset["content_hash"],
                target_url=candidate,
                last_known_target_hash=cache.get(candidate, {}).get("content_hash"),
                **reference_fields,
            )
            # Use this same bounded cycle for newly discovered useful
            # originals, rather than delaying the first follow to tomorrow.
            if is_catalog_adapter and link["purpose"] == "catalog":
                urls.appendleft(candidate)
            elif is_sso:
                urls.append(candidate)
            elif link["purpose"] == "document":
                urls.appendleft(candidate)
            else:
                urls.append(candidate)

    def collect_source(self, source, previous):
        current = copy.deepcopy(previous)
        cache, frontier = (
            dict(current.get("cache", {})),
            dict(current.get("frontier", {})),
        )
        window_wait_until = request_window_wait_until(source, time.time())
        if window_wait_until is not None:
            current.update(cache=cache, frontier=frontier, last_receipts=[])
            current["next_at"] = max(
                window_wait_until,
                current.get("next_at", 0),
                current.get("retry_not_before") or 0,
            )
            current["scheduling_status"] = "request_window_wait"
            return current
        adapter = source.get("discovery", {}).get("adapter")
        is_sso = adapter == "sso_catalog_2026"
        is_bi_official = adapter == "bi_official"
        is_bi_jdih = adapter == "bi_jdih"
        is_dephub = adapter == "dephub_regulations"
        is_treaty_room = adapter == "treaty_room_public"
        is_catalog_adapter = is_sso or is_bi_official or is_bi_jdih or is_dephub or is_treaty_room
        dephub_max_pages = source.get("discovery", {}).get("max_pages", 2_000)
        if is_bi_official:
            latest_catalog_drift = max(
                (
                    asset.get("observed_at", 0)
                    for asset in cache.values()
                    if (asset.get("discovery", {}).get("scope") or {}).get("total_changed") is True
                ),
                default=0,
            )
            if latest_catalog_drift > current.get("catalog_total_drift_root_rechecked_at", 0):
                # Resume an already observed mismatch after a process restart as
                # an unconditional root read, even if older code wrote the cache.
                current["force_root_refresh"] = True
                current["root_next_at"] = min(current.get("root_next_at", 0), time.time())
        root_due = (
            time.time() >= current.get("root_next_at", 0)
            or source["url"] not in cache
            or current.get("force_root_refresh") is True
        )
        urls = deque()
        frontier_limit = (
            source["discovery"]["max_records"] + source["discovery"]["max_pages"]
            if is_bi_official
            else source["discovery"]["max_records"] + bi_jdih_discovery.MAX_CATALOG_PAGES
            if is_bi_jdih
            else source["discovery"]["max_records"] + dephub_max_pages
            if is_dephub or is_treaty_room
            else self.limits["max_frontier"]
        )
        cached_detail_replay = (
            is_bi_official or is_bi_jdih or is_dephub or is_treaty_room
        ) and len(frontier) < frontier_limit
        if is_catalog_adapter:
            def catalogue_index(entry):
                if is_bi_official:
                    page = bi_official_discovery.page_number(entry[0])
                    asset = entry[1]
                    return (
                        page or asset.get("catalog_page_number") or 0,
                        0 if asset.get("discovery", {}).get("purpose") == "catalog" else 1,
                    )
                if is_bi_jdih:
                    offset = bi_jdih_discovery.catalog_offset(entry[0])
                    return (
                        offset or 0,
                        0 if entry[1].get("discovery", {}).get("purpose") == "catalog" else 1,
                    )
                if is_dephub:
                    page = dephub_discovery.page_number(entry[0], max_pages=dephub_max_pages)
                    return (
                        page or entry[1].get("catalog_page_number") or 0,
                        0 if entry[1].get("discovery", {}).get("purpose") == "catalog" else 1,
                    )
                if is_treaty_room:
                    page = int(treaty_room_discovery.catalog_query(entry[0])["page"])
                    return (
                        page,
                        0 if entry[1].get("discovery", {}).get("purpose") == "catalog" else 1,
                    )
                match = re.search(r"/All(?:/(\d+))?$", urlsplit(entry[0]).path)
                return int(match.group(1) or 0) if match else 0

            for catalogue_url, catalogue_asset in sorted(
                (
                    (cached_url, cached_asset)
                    for cached_url, cached_asset in cache.items()
                    if cached_asset.get("discovery", {}).get("purpose") == "catalog"
                    or (
                        cached_detail_replay
                        and cached_asset.get("discovery", {}).get("purpose") == "detail"
                    )
                ),
                key=catalogue_index,
            ):
                self.map_links(source, catalogue_url, catalogue_asset, None, frontier, cache, urls)
            if root_due:
                urls.appendleft(source["url"])
        elif root_due:
            urls.append(source["url"])
        urls.extend(
            [
                url
                for url, item in sorted(
                    frontier.items(),
                    key=lambda pair: (
                        -int(
                            (is_bi_official or is_bi_jdih or is_dephub)
                            and pair[1].get("purpose") == "catalog"
                        ),
                        pair[1].get("attempt_count", 0),
                        -int(pair[1].get("purpose") == "document"),
                        pair[0],
                    ),
                )
                if url != source["url"]
                and not scope_excluded(source, url, item.get("title", ""))
                and urlsplit(url).hostname in source["allowed_hosts"]
                and due(item.get("health"), time.time())
            ]
        )
        visited = set()
        root_outcome = None
        root_request_id = None
        catalog_total_drift = False
        deferred = None
        receipts = []
        while urls and len(receipts) < self.limits["requests_per_source_cycle"]:
            window_wait_until = request_window_wait_until(source, time.time())
            if window_wait_until is not None:
                break
            url = urls.popleft()
            if (
                url in visited
                or scope_excluded(source, url, frontier.get(url, {}).get("title", ""))
                or (url != source["url"] and url not in frontier)
            ):
                continue
            visited.add(url)
            try:
                if is_bi_official and (bi_official_discovery.page_number(url) or 1) > 1:
                    parent_url = bi_official_discovery.previous_catalog_url(url)
                    receipt = self.fetch(
                        source,
                        url,
                        cache.get(url),
                        parent_cached=cache.get(parent_url),
                    )
                elif url == source["url"] and current.get("force_root_refresh") is True:
                    receipt = self.fetch(source, url, None)
                else:
                    receipt = self.fetch(source, url, cache.get(url))
            except Deferred as error:
                deferred = error
                break
            receipts.append(receipt)
            outcome, asset = receipt["outcome"], receipt.get("asset")
            if url == source["url"]:
                root_outcome = outcome
                root_request_id = receipt["request_id"]
            else:
                item = frontier[url]
                item["attempt_count"] = item.get("attempt_count", 0) + 1
                item["health"] = observe(
                    item.get("health"),
                    outcome,
                    now=time.time(),
                    interval_seconds=7 * 86400,
                )
                self.event(
                    "mapped_resource",
                    source_id=source["id"],
                    parent_url=item["parent_url"],
                    parent_hash=item["parent_hash"],
                    parents=item.get("parents", []),
                    target_url=url,
                    outcome=outcome,
                    target_hash=asset["content_hash"] if asset else None,
                    **({"source_record": item["source_record"]} if item.get("source_record") else {}),
                )
            if asset:
                previous_asset = cache.get(url)
                if (is_bi_official or is_dephub) and url != source["url"]:
                    mapping = frontier.get(url, {})
                    asset["source_parent_url"] = mapping.get("parent_url")
                    asset["source_parent_hash"] = mapping.get("parent_hash")
                    asset["catalog_page_number"] = mapping.get("catalog_page_number")
                if previous_asset is None or previous_asset["content_hash"] != asset["content_hash"]:
                    self.event(
                        "content_version",
                        source_id=source["id"],
                        url=url,
                        previous_content_hash=previous_asset["content_hash"] if previous_asset else None,
                        content_hash=asset["content_hash"],
                        path=asset["path"],
                        mime_type=asset["mime_type"],
                        discovery_matched=bool(asset.get("discovery", {}).get("matched"))
                        if is_sso
                        else (
                            True
                            if url != source["url"] and frontier.get(url, {}).get("purpose") == "document"
                            else bool(asset.get("discovery", {}).get("matched"))
                        ),
                        discovery_purpose=(
                            frontier.get(url, {}).get("purpose")
                            if url != source["url"]
                            else asset.get("discovery", {}).get("purpose")
                        ),
                        discovery_parent_url=(
                            frontier.get(url, {}).get("parent_url") if url != source["url"] else None
                        ),
                        discovery_parent_hash=(
                            frontier.get(url, {}).get("parent_hash") if url != source["url"] else None
                        ),
                        discovery_title=frontier.get(url, {}).get("title") if url != source["url"] else None,
                        requested_url=(asset or {}).get("requested_url"),
                        source_identity_key=frontier.get(url, {}).get("identity_key"),
                        source_locator=frontier.get(url, {}).get("source_locator"),
                        source_document_date=frontier.get(url, {}).get("document_date"),
                        source_status=frontier.get(url, {}).get("source_status"),
                        source_record=frontier.get(url, {}).get("source_record"),
                        source_root=url == source["url"],
                        source_kind=source["kind"],
                        request_evidence=(asset or {}).get("request_evidence"),
                        legal_review="unreviewed",
                    )
                cache_limit = source["discovery"]["cache_limit"] if is_sso else self.limits["max_frontier"] + 1
                if is_bi_official:
                    cache_limit = (
                        2 * source["discovery"]["max_records"]
                        + source["discovery"]["max_pages"]
                        + 1
                    )
                elif is_bi_jdih:
                    cache_limit = (
                        2 * source["discovery"]["max_records"]
                        + bi_jdih_discovery.MAX_CATALOG_PAGES
                        + 1
                    )
                elif is_dephub:
                    cache_limit = (
                        2 * source["discovery"]["max_records"]
                        + dephub_max_pages
                        + 1
                    )
                if url not in cache and len(cache) >= cache_limit:
                    candidates = [key for key in cache if key != source["url"]]
                    if is_bi_official or is_dephub:
                        non_catalogue = [
                            key
                            for key in candidates
                            if cache[key].get("discovery", {}).get("purpose") != "catalog"
                        ]
                        if non_catalogue:
                            candidates = non_catalogue
                    victim = min(
                        candidates,
                        key=lambda k: cache[k]["observed_at"],
                    )
                    self.event(
                        "cache_metadata_archived",
                        source_id=source["id"],
                        url=victim,
                        asset=cache.pop(victim),
                    )
                if is_sso and url != source["url"]:
                    asset["source_document_date"] = frontier.get(url, {}).get("document_date")
                    asset["source_original_verified"] = bool(asset.get("discovery", {}).get("matched"))
                cache[url] = asset
                page_scope = asset.get("discovery", {}).get("scope", {})
                if is_bi_official and page_scope.get("total_changed") is True:
                    catalog_total_drift = True
                    self.event(
                        "catalog_total_drift",
                        source_id=source["id"],
                        url=url,
                        page=page_scope.get("page"),
                        previous_total=page_scope.get("previous_total"),
                        reported_total=page_scope.get("total"),
                        action="unconditional_root_recheck_scheduled",
                    )
                self.map_links(source, url, asset, previous_asset, frontier, cache, urls)
                if (
                    is_sso
                    and url != source["url"]
                    and frontier.get(url, {}).get("purpose") != "catalog"
                    and outcome.get("status") == "success"
                    and asset.get("source_original_verified") is True
                ):
                    frontier.pop(url, None)
                if (
                    is_bi_official
                    and url != source["url"]
                    and frontier.get(url, {}).get("purpose") != "catalog"
                    and outcome.get("status") == "success"
                    and asset.get("discovery", {}).get("matched") is True
                ):
                    frontier.pop(url, None)
            if url == source["url"] and outcome["status"] != "success":
                break
            if self.host_cooldowns.get(urlsplit(url).hostname, 0) > time.time():
                break
        if root_outcome is not None:
            current = observe(
                current,
                root_outcome,
                now=time.time(),
                interval_seconds=source["interval_seconds"],
            )
            current["root_next_at"] = current["next_at"]
            if root_outcome.get("error_type") == "EmptyCatalogue":
                current["root_next_at"] = max(
                    current["root_next_at"], time.time() + source["interval_seconds"]
                )
            if root_outcome.get("status") == "success" and current.get("force_root_refresh") is True:
                current["catalog_total_drift_root_rechecked_at"] = time.time()
                current.pop("force_root_refresh", None)
        if catalog_total_drift:
            current["force_root_refresh"] = True
            current["root_next_at"] = time.time()
        elif root_outcome is not None and root_outcome.get("status") == "success":
            current.pop("force_root_refresh", None)
        if deferred is not None and deferred.status != "stopping":
            current = observe(
                current,
                {
                    "status": deferred.status,
                    "retry_after_seconds": deferred.retry_after,
                    "error_type": deferred.reason,
                },
                now=time.time(),
                interval_seconds=source["interval_seconds"],
            )
        candidates = [current.get("root_next_at", current.get("next_at", time.time()))]
        candidates.extend(
            item.get("health", {}).get("next_at", time.time())
            for url, item in frontier.items()
            if not scope_excluded(source, url, frontier.get(url, {}).get("title", ""))
        )
        next_at = max(time.time() + self.limits["tick_seconds"], min(candidates))
        if deferred is not None and deferred.status != "stopping":
            next_at = max(next_at, time.time() + max(300, deferred.retry_after))
        host_until = max(
            (self.host_cooldowns.get(host, 0) for host in source["allowed_hosts"]),
            default=0,
        )
        current["next_at"] = max(next_at, host_until, current.get("retry_not_before") or 0)
        if window_wait_until is not None:
            current["next_at"] = max(current["next_at"], window_wait_until)
            current["scheduling_status"] = "request_window_wait"
        elif current.get("scheduling_status") == "request_window_wait":
            current.pop("scheduling_status", None)
        if host_until > time.time():
            current["retry_not_before"] = max(current.get("retry_not_before") or 0, host_until)
            current["scheduling_status"] = "host_cooldown"
        current.update(
            cache=cache,
            frontier=frontier,
            last_receipts=[r["request_id"] for r in receipts],
            last_root_request_id=root_request_id,
        )
        return current

    def tick(self):
        self.reload_sources()
        now = time.time()
        for source in self.sources:
            window_wait_until = request_window_wait_until(source, now)
            state = self.state["sources"].get(source["id"])
            if window_wait_until is None:
                if state and state.get("scheduling_status") == "request_window_wait":
                    state.pop("scheduling_status", None)
                continue
            state = self.state["sources"].setdefault(source["id"], {})
            state["next_at"] = max(
                window_wait_until,
                state.get("next_at", 0),
                state.get("retry_not_before") or 0,
            )
            state["scheduling_status"] = "request_window_wait"
        self.release_recovered_daily_quotas(now)
        selected = choose_sources(
            self.sources,
            self.state["sources"],
            now=now,
            max_sources=self.limits["sources_per_tick"],
        )
        for decision in replacement_decisions(self.sources, self.state["sources"], selected, now=time.time()):
            self.event("source_replacement", **decision)
        if not selected:
            self.state["status"] = "waiting"
            self.save()
            return []
        for identifier in selected:
            previous = self.state["sources"].setdefault(identifier, {})
            previous["last_selected_at"] = time.time()
            previous["selection_count"] = (
                previous.get("selection_count", previous.get("attempt_count", 0)) + 1
            )
        self.state.update(active_sources=list(selected), status="collecting")
        self.save()
        by_id = {s["id"]: s for s in self.sources}
        with ThreadPoolExecutor(max_workers=self.limits["workers"]) as pool:
            futures = {
                pool.submit(self.collect_source, by_id[i], self.state["sources"][i]): i for i in selected
            }
            for future in as_completed(futures):
                identifier = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    result = observe(
                        self.state["sources"][identifier],
                        {"status": "network_error", "error_type": type(error).__name__},
                        now=time.time(),
                        interval_seconds=by_id[identifier]["interval_seconds"],
                    )
                self.state["sources"][identifier] = result
                self.state["active_sources"].remove(identifier)
                self.state["last_progress_at"] = time.time()
                root_request = self.requests.get(result.get("last_root_request_id"))
                root_evidence = None
                if root_request is not None:
                    root_evidence = {
                        "request_id": root_request["request_id"],
                        "source_id": root_request["source_id"],
                        "url": root_request["url"],
                        "network_url": root_request.get("network_url"),
                        "status": root_request["status"],
                        "outcome": root_request.get("outcome"),
                        "request_evidence": root_request.get("request_evidence"),
                    }
                self.event(
                    "source_observation",
                    source_id=identifier,
                    health=result.get("health"),
                    next_at=result.get("next_at"),
                    last_observation=result.get("last_observation"),
                    root_request=root_evidence,
                )
                self.save()
        self.state["status"] = "waiting"
        self.save()
        return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status"))
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--country", choices=("ID", "SG"), required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.action == "status":
        root = args.root.resolve()
        state = load(root / "state.json")
        pool_sha256 = None
        pool_sources = None
        pool_read_error = None
        try:
            revision = JsonSourceRegistry(args.pool, root / "registry-versions").read()
            pool_sha256 = revision.digest
            pool_sources = revision.payload.get("sources")
        except (OSError, ValueError) as error:
            pool_read_error = type(error).__name__
        report = status_summary(
            state,
            current_pool_sha256=pool_sha256,
            current_pool_sources=pool_sources,
        )
        if pool_read_error:
            report["pool_read_error"] = pool_read_error
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0
    args.root.resolve().mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with lock(args.root.resolve() / ".collector.lock"):
            collector = Collector(args.pool, args.root, args.country)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: collector.stop.set())
            while not collector.stop.is_set() and not (collector.root / "STOP").exists():
                collector.tick()
                if args.once:
                    break
                collector.stop.wait(collector.limits["tick_seconds"])
            if collector.stop.is_set() or (collector.root / "STOP").exists():
                collector.state["status"] = "stopped"
                collector.save()
                return 75
    except (ValueError, OSError) as error:
        print(
            json.dumps({"status": "blocked", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
