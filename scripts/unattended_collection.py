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
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]

from app.blob_store import BlobStore  # noqa: E402
from app.discovery import discover_page  # noqa: E402
from app.ingestion import DownloadError, _download, _validated_url  # noqa: E402
from official_source_supervisor_recovery import load, lock, write  # noqa: E402
from source_health import choose_sources, due, observe, replacement_decisions  # noqa: E402

PROTOCOL = "aitea-unattended-collection-v1"
SETNEG_ID = "id-setneg-perpu"
SETNEG_URL = "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0"
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


def scope_excluded(source, url):
    parsed = urlsplit(url)
    return bool(
        rcep_policy(source)
        and parsed.hostname == "www.enterprisesg.gov.sg"
        and re.search(RCEP_OUTSIDE_SCOPE, unquote(parsed.path), re.I)
    )


class Deferred(Exception):
    def __init__(self, status, *, retry_after=0, reason=None):
        self.status = status
        self.retry_after = retry_after
        self.reason = reason
        super().__init__(status)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def official_host(host, country):
    return isinstance(host, str) and host.endswith(".go.id" if country == "ID" else ".gov.sg")


def source_pool(path, country):
    pool = load(path)
    protocol = pool.get("protocol")
    if protocol not in {"aitea-official-source-pool-v1", "aitea-official-source-pool-v2"} or country not in {"ID", "SG"}:
        raise ValueError("Unsupported official source pool/country")
    if protocol == "aitea-official-source-pool-v2" and pool.get("schema_version") != 2:
        raise ValueError("Unsupported official source pool schema version")
    sources = pool["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 64:
        raise ValueError("Official source pool must contain 1–64 bounded sources")
    ids = set()
    source_rows = {}
    for source in sources:
        identifier = source["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", identifier) or identifier in ids:
            raise ValueError("Source IDs must be unique stable identifiers")
        ids.add(identifier)
        source_rows[identifier] = source
        scope = source["country"]
        hosts = source["allowed_hosts"]
        normalized, host = _validated_url(source["url"])
        if (identifier == SETNEG_ID or host == "jdih.setneg.go.id") and (
            identifier != SETNEG_ID
            or scope != "ID"
            or hosts != ["jdih.setneg.go.id"]
            or normalized != SETNEG_URL
            or source["kind"] != "catalogue"
            or source.get("setneg_type", "PERPU") != "PERPU"
        ):
            raise ValueError("Setneg collection must retain the reviewed PERPU scope")
        if (
            scope not in {"ID", "SG"}
            or not isinstance(hosts, list)
            or not 1 <= len(hosts) <= 16
            or any(not official_host(h, scope) for h in hosts)
            or host not in hosts
            or normalized != source["url"]
            or type(source["priority"]) is not int
            or not 1 <= source["priority"] <= 100
            or type(source["interval_seconds"]) is not int
            or not 3600 <= source["interval_seconds"] <= 30 * 86400
            or source["kind"] not in {"guidance", "legislation", "catalogue"}
            or not source["topics"]
            or not all(isinstance(topic, str) and 1 <= len(topic) <= 100 for topic in source["topics"])
            or not 1 <= len(source["discovery_keywords"]) <= 40
            or any(not isinstance(s, str) or not 1 <= len(s) <= 100 for s in source["discovery_keywords"])
        ):
            raise ValueError("Invalid official source scope or bounds")
        if protocol == "aitea-official-source-pool-v2":
            if source.get("status") not in {"active", "archived"} or source.get("transport") != "proxy":
                raise ValueError("Every v2 source needs an explicit status and proxy transport")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", source.get("group_id", "")):
                raise ValueError("Every v2 source needs one stable primary group")

    if protocol == "aitea-official-source-pool-v2":
        groups = pool.get("groups")
        if not isinstance(groups, list) or not 2 <= len(groups) <= 64:
            raise ValueError("V2 source pool needs a bounded hierarchical group list")
        group_rows = {}
        group_sources = set()

        def validate_group(group, parent_id=None, depth=0):
            if not isinstance(group, dict) or depth > 3:
                raise ValueError("Source group hierarchy exceeds its depth limit")
            group_id = group.get("id")
            if (
                not isinstance(group_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", group_id)
                or group_id in group_rows
                or group.get("parent_id") != parent_id
                or group.get("country") not in {"ID", "SG"}
                or not isinstance(group.get("title"), str)
                or not 1 <= len(group["title"]) <= 100
                or not isinstance(group.get("checklist_items"), list)
                or any(not re.fullmatch(r"(?:ID|SG)-[0-9]+(?:\.[0-9]+)*", item) for item in group["checklist_items"])
                or not isinstance(group.get("source_ids"), list)
            ):
                raise ValueError("Invalid source group or checklist relationship")
            group_rows[group_id] = group
            if parent_id and group_rows[parent_id]["country"] != group["country"]:
                raise ValueError("Source group cannot cross country boundaries")
            for source_id in group["source_ids"]:
                source = source_rows.get(source_id)
                if (
                    source is None
                    or source["country"] != group["country"]
                    or source.get("group_id") != group_id
                    or source_id in group_sources
                ):
                    raise ValueError("Source groups must map each source exactly once within its country")
                group_sources.add(source_id)
            children = group.get("children", [])
            if not isinstance(children, list):
                raise TypeError("Source group children must be a list")
            for child in children:
                validate_group(child, group_id, depth + 1)

        for group in groups:
            validate_group(group)
        if group_sources != ids:
            raise ValueError("Every configured source must appear in exactly one group")
        for source in sources:
            if source["group_id"] not in group_rows:
                raise ValueError("Source references a missing group")
            checklist = set(source.get("verification", {}).get("checklist_items", []))
            if not checklist.issubset(set(group_rows[source["group_id"]]["checklist_items"])):
                raise ValueError("Source checklist links must be covered by its group")
        relations = pool.get("source_relations")
        if not isinstance(relations, list) or len(relations) > 256:
            raise ValueError("Source relationships must be a bounded list")
        relation_keys = set()
        for relation in relations:
            left, right = relation.get("source_id"), relation.get("related_source_id")
            key = (left, right, relation.get("type"))
            related_checklists = {
                *source_rows.get(left, {}).get("verification", {}).get("checklist_items", []),
                *source_rows.get(right, {}).get("verification", {}).get("checklist_items", []),
            }
            if (
                left not in ids
                or right not in ids
                or left == right
                or key in relation_keys
                or relation.get("type") not in {"complements", "cross_checks", "implements", "publishes_original"}
                or not isinstance(relation.get("reason"), str)
                or not 10 <= len(relation["reason"]) <= 500
                or (relation.get("checklist_items") and not all(
                    isinstance(item, str)
                    and re.fullmatch(r"(?:ID|SG)-[0-9]+(?:\.[0-9]+)*", item)
                    and item in related_checklists
                    for item in relation["checklist_items"]
                ))
            ):
                raise ValueError("Invalid or duplicate source relationship")
            relation_keys.add(key)

    return [
        source for source in sources
        if source["country"] == country
        and (protocol == "aitea-official-source-pool-v1" or source["status"] == "active")
    ]


def recipe(source):
    root_path = urlsplit(source["url"]).path or "/"
    root_pattern = "^" + re.escape(root_path) + "$"
    result = {
        "adapter": "html_links",
        "allowed_hosts": source["allowed_hosts"],
        "catalog_patterns": [root_pattern] if source["kind"] == "catalogue" else [],
        "html_document_patterns": [] if source["kind"] == "catalogue" else [root_pattern],
        "document_patterns": [r"\.(?:pdf|docx?)(?:$|\?)", r"/(?:download|storage|uploads)/"],
        "detail_patterns": [r"^/"],
        "include_terms": source["discovery_keywords"],
        "exclude_terms": ["facebook", "instagram", "privacy policy", "careers", "login"],
        "max_links": 32,
    }
    if rcep_policy(source):
        result["exclude_patterns"] = [RCEP_OUTSIDE_SCOPE]
    if source["id"] == SETNEG_ID:
        result.update(
            setneg_type="PERPU",
            required_query_by_host={"jdih.setneg.go.id": {"jns": "PERPU"}},
            max_links=51,
        )
    return result


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
        self.pool_path, self.root, self.country = Path(pool).resolve(), Path(root).resolve(), country
        if not self.root.is_relative_to(ROOT) or self.root == ROOT:
            raise ValueError("Collection output must remain in this project")
        self.sources = source_pool(self.pool_path, country)
        if not self.sources:
            raise ValueError("No configured sources for country")
        self.limits = {**LIMITS, **(limits or {})}
        if any(type(v) is not int or v < 1 for v in self.limits.values()):
            raise ValueError("Collection limits must be positive integers")
        if self.limits["workers"] > 8 or self.limits["per_host"] > 2:
            raise ValueError("Unattended concurrency exceeds verified limits")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binding = {
            "protocol": PROTOCOL,
            "pool_sha256": sha(self.pool_path),
            "country": country,
            "root": str(self.root),
            "limits": self.limits,
        }
        self.state_path = self.root / "state.json"
        if self.state_path.exists():
            self.state = load(self.state_path)
            if self.state["binding"] != self.binding:
                raise ValueError("Collector scope changed; preserve and explicitly migrate its state")
        else:
            if any(p.name != ".collector.lock" for p in self.root.iterdir()):
                raise ValueError("Missing state for existing evidence; refusing an empty replacement")
            self.state = {"binding": self.binding, "sources": {}, "active_sources": [], "status": "ready"}
            self.save()
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.host_slots = {}
        self.host_cooldowns_path = self.root / "host-cooldowns.json"
        self.host_cooldowns = load(self.host_cooldowns_path) if self.host_cooldowns_path.exists() else {}
        self.blobs = BlobStore(self.root / "files", max_bytes=self.limits["max_response_bytes"])
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
                {"status": "network_error", "error_type": "InterruptedGET", "url": source["url"]},
                now=time.time(),
                interval_seconds=source["interval_seconds"],
            )
        self.state["active_sources"] = []
        self.apply_candidate_policy()
        self.save()

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

    def reserve(self, source, url):
        with self.mutex:
            self.roll_budget()
            if self.stop.is_set() or (self.root / "STOP").exists():
                raise Deferred("stopping")
            wait = self.host_cooldowns.get(urlsplit(url).hostname, 0) - time.time()
            if wait > 0:
                raise Deferred("budget_wait", retry_after=wait, reason="HostCooldown")
            if shutil.disk_usage(self.root).free < self.limits["min_free_bytes"] + 128 * 1024**2:
                raise Deferred("storage_wait")
            own = [row for row in self.requests.values() if row["source_id"] == source["id"]]
            limit = self.limits["max_response_bytes"]
            if (
                len(self.requests) >= self.limits["daily_requests"]
                or len(own) >= self.limits["daily_source_requests"]
                or sum(row["charged_bytes"] for row in self.requests.values()) + limit
                > self.limits["daily_bytes"]
                or sum(row["charged_bytes"] for row in own) + limit > self.limits["daily_source_bytes"]
            ):
                raise Deferred(
                    "budget_wait",
                    retry_after=(int(time.time()) // 86400 + 1) * 86400 + 5 - time.time(),
                    reason="DailyQuota",
                )
            identifier = uuid.uuid4().hex
            row = {
                "request_id": identifier,
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
    def slot(self, host, source):
        if host not in source["allowed_hosts"]:
            raise DownloadError("Redirect host is outside the reviewed official source scope")
        with self.mutex:
            semaphore = self.host_slots.setdefault(host, threading.BoundedSemaphore(self.limits["per_host"]))
        if not semaphore.acquire(timeout=self.limits["timeout_seconds"]):
            raise DownloadError("Official source concurrency wait exceeded deadline", retryable=True)
        try:
            yield
        finally:
            semaphore.release()

    def fetch(self, source, url, cached=None):
        path, receipt = self.reserve(source, url)
        outcome = None
        try:
            validators = {}
            if valid_cached(self.root, cached) and cached["url"] == url:
                validators = {k: cached.get(k) for k in ("etag", "last_modified")}
            fetched = _download(
                url,
                deadline=time.monotonic() + self.limits["timeout_seconds"],
                consume=lambda chunks: self.blobs.put(
                    "official-updates:" + self.country + ":" + source["id"], chunks
                ),
                validators=validators,
                connection_slot=lambda host: self.slot(host, source),
                accept_compressed_html=True,
                accept_compressed_json=source["id"] == SETNEG_ID,
                max_bytes=self.limits["max_response_bytes"],
            )
            if fetched.get("not_modified"):
                if fetched["url"] != url or not valid_cached(self.root, cached):
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
                blob = fetched["blob"]
                with blob.path.open("rb") as stream:
                    content = stream.read(4 * 1024**2 + 1)
                inspected = discover_page(fetched["url"], content, fetched["mime_type"], recipe(source))
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
                empty = source["kind"] == "catalogue" and b"belum ada peraturan" in content.lower()
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
                        "requested_url": url,
                        "path": str(blob.path.relative_to(self.root)),
                        "content_hash": blob.digest,
                        "size": blob.size,
                        "mime_type": fetched["mime_type"],
                        "etag": fetched.get("etag"),
                        "last_modified": fetched.get("last_modified"),
                        "observed_at": time.time(),
                        "discovery": inspected,
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
                    "http_error"
                    if status
                    else "network_error"
                    if network_failure
                    else "content_invalid"
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
            outcome = {"status": "content_invalid", "error_type": type(error).__name__, "url": url}
            receipt.update(status="finished", outcome=outcome, asset=None, finished_at=time.time())
        with self.mutex:
            write(path, receipt)
            if receipt["day"] == self.request_day:
                self.requests[receipt["request_id"]] = receipt
        return receipt

    def apply_candidate_policy(self):
        """Replay verified cached discovery once, without GETs, receipts or quota changes."""
        for source in self.sources:
            if not rcep_policy(source):
                continue
            current = self.state["sources"].setdefault(source["id"], {})
            if current.get("candidate_policy", {}).get("version") == RCEP_POLICY_VERSION:
                continue
            cache = current.get("cache", {})
            frontier = current.setdefault("frontier", {})
            excluded = 0
            for url, item in frontier.items():
                if not scope_excluded(source, url):
                    continue
                marker = {"version": RCEP_POLICY_VERSION, "reason": "third_country_tariff_schedule"}
                if item.get("scope_excluded") != marker:
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
                    self.map_links(source, source["url"], refreshed, cached, frontier, cache, deque())
                    replay_status, root_hash = "verified_cached_discovery", cached["content_hash"]
                else:
                    replay_status = "cached_discovery_unmatched"
            elif cached:
                replay_status = "cached_integrity_failed"
            added = len(set(frontier) - before)
            current["candidate_policy"] = {
                "version": RCEP_POLICY_VERSION,
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
                    max((self.host_cooldowns.get(h, 0) for h in source["allowed_hosts"]), default=0),
                )
            self.event(
                "candidate_policy_applied",
                source_id=source["id"],
                policy_version=RCEP_POLICY_VERSION,
                replay_status=replay_status,
                root_hash=root_hash,
                excluded_frontier=excluded,
                discovered_candidates=added,
                network_requests=0,
            )

    def map_links(self, source, url, asset, previous_asset, frontier, cache, urls):
        for link in asset.get("discovery", {}).get("links", []):
            candidate, host = _validated_url(link["url"])
            if (
                host not in source["allowed_hosts"]
                or candidate == source["url"]
                or scope_excluded(source, candidate)
            ):
                continue
            parent = {"url": url, "content_hash": asset["content_hash"]}
            if candidate in frontier:
                item = frontier[candidate]
                parents = item.get(
                    "parents", [{"url": item["parent_url"], "content_hash": item["parent_hash"]}]
                )
                prior_parent = previous_asset or next(
                    (known for known in reversed(parents) if known["url"] == url), None
                )
                parent_changed = (
                    prior_parent is not None and prior_parent["content_hash"] != asset["content_hash"]
                )
                if parent in parents and not parent_changed:
                    continue
                self.event(
                    "source_reference",
                    source_id=source["id"],
                    parent_url=url,
                    parent_hash=asset["content_hash"],
                    target_url=candidate,
                    last_known_target_hash=cache.get(candidate, {}).get("content_hash"),
                )
                item.update(
                    parent_url=url,
                    parent_hash=asset["content_hash"],
                    parents=([known for known in parents if known != parent] + [parent])[-8:],
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
            if len(frontier) >= self.limits["max_frontier"]:
                victim = min(
                    frontier,
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
                )
            frontier[candidate] = {
                "parent_url": url,
                "parent_hash": asset["content_hash"],
                "parents": [parent],
                "title": link.get("title", ""),
                "purpose": link["purpose"],
                "attempt_count": 0,
                "discovered_at": time.time(),
            }
            self.event(
                "source_reference",
                source_id=source["id"],
                parent_url=url,
                parent_hash=asset["content_hash"],
                target_url=candidate,
                last_known_target_hash=cache.get(candidate, {}).get("content_hash"),
            )
            # Use this same bounded cycle for newly discovered useful
            # originals, rather than delaying the first follow to tomorrow.
            if link["purpose"] == "document":
                urls.appendleft(candidate)
            else:
                urls.append(candidate)

    def collect_source(self, source, previous):
        current = copy.deepcopy(previous)
        cache, frontier = dict(current.get("cache", {})), dict(current.get("frontier", {}))
        root_due = time.time() >= current.get("root_next_at", 0) or source["url"] not in cache
        urls = [source["url"]] if root_due else []
        urls += [
            url
            for url, item in sorted(
                frontier.items(),
                key=lambda pair: (
                    pair[1].get("attempt_count", 0),
                    -int(pair[1].get("purpose") == "document"),
                    pair[0],
                ),
            )
            if url != source["url"]
            and not scope_excluded(source, url)
            and due(item.get("health"), time.time())
        ]
        urls = deque(urls)
        visited = set()
        root_outcome = None
        deferred = None
        receipts = []
        while urls and len(receipts) < self.limits["requests_per_source_cycle"]:
            url = urls.popleft()
            if (
                url in visited
                or scope_excluded(source, url)
                or (url != source["url"] and url not in frontier)
            ):
                continue
            visited.add(url)
            try:
                receipt = self.fetch(source, url, cache.get(url))
            except Deferred as error:
                deferred = error
                break
            receipts.append(receipt)
            outcome, asset = receipt["outcome"], receipt.get("asset")
            if url == source["url"]:
                root_outcome = outcome
            else:
                item = frontier[url]
                item["attempt_count"] = item.get("attempt_count", 0) + 1
                item["health"] = observe(
                    item.get("health"), outcome, now=time.time(), interval_seconds=7 * 86400
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
                )
            if asset:
                previous_asset = cache.get(url)
                if previous_asset is None or previous_asset["content_hash"] != asset["content_hash"]:
                    self.event(
                        "content_version",
                        source_id=source["id"],
                        url=url,
                        previous_content_hash=previous_asset["content_hash"] if previous_asset else None,
                        content_hash=asset["content_hash"],
                        path=asset["path"],
                        mime_type=asset["mime_type"],
                        discovery_matched=(
                            True if url != source["url"] and frontier.get(url, {}).get("purpose") == "document"
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
                        source_root=url == source["url"],
                        source_kind=source["kind"],
                        legal_review="unreviewed",
                    )
                if url not in cache and len(cache) >= self.limits["max_frontier"] + 1:
                    victim = min(
                        (k for k in cache if k != source["url"]), key=lambda k: cache[k]["observed_at"]
                    )
                    self.event(
                        "cache_metadata_archived", source_id=source["id"], url=victim, asset=cache.pop(victim)
                    )
                cache[url] = asset
                self.map_links(source, url, asset, previous_asset, frontier, cache, urls)
            if url == source["url"] and outcome["status"] != "success":
                break
            if self.host_cooldowns.get(urlsplit(url).hostname, 0) > time.time():
                break
        if root_outcome is not None:
            current = observe(
                current, root_outcome, now=time.time(), interval_seconds=source["interval_seconds"]
            )
            current["root_next_at"] = current["next_at"]
            if root_outcome.get("error_type") == "EmptyCatalogue":
                current["root_next_at"] = max(
                    current["root_next_at"], time.time() + source["interval_seconds"]
                )
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
            if not scope_excluded(source, url)
        )
        next_at = max(time.time() + self.limits["tick_seconds"], min(candidates))
        if deferred is not None and deferred.status != "stopping":
            next_at = max(next_at, time.time() + max(300, deferred.retry_after))
        host_until = max((self.host_cooldowns.get(host, 0) for host in source["allowed_hosts"]), default=0)
        current["next_at"] = max(next_at, host_until, current.get("retry_not_before") or 0)
        if host_until > time.time():
            current["retry_not_before"] = max(current.get("retry_not_before") or 0, host_until)
            current["scheduling_status"] = "host_cooldown"
        current.update(cache=cache, frontier=frontier, last_receipts=[r["request_id"] for r in receipts])
        return current

    def tick(self):
        selected = choose_sources(
            self.sources, self.state["sources"], now=time.time(), max_sources=self.limits["sources_per_tick"]
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
                self.event(
                    "source_observation",
                    source_id=identifier,
                    health=result.get("health"),
                    next_at=result.get("next_at"),
                    last_observation=result.get("last_observation"),
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
        print(json.dumps(load(args.root.resolve() / "state.json"), ensure_ascii=False))
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
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
