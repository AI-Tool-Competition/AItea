"""Migrate a stopped official collector's pool binding without resetting evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from official_source_supervisor_recovery import load, lock, write  # noqa: E402
from unattended_collection import source_pool  # noqa: E402

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_MAX_RECEIPT_BYTES = 64 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verified_addition(source: dict) -> None:
    """Require a recent successful collector receipt before admitting a source."""
    verification = source.get("verification", {})
    receipt_value = verification.get("server_receipt_path", "")
    if not isinstance(receipt_value, str):
        raise TypeError("New sources require a private, project-local server receipt")
    relative = Path(receipt_value)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != ".dev" or ".." in relative.parts:
        raise ValueError("New sources require a private, project-local server receipt")
    receipt = ROOT / relative
    current = ROOT
    try:
        for part in relative.parts:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError("New source receipt cannot traverse symlinks")
        info = receipt.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > _MAX_RECEIPT_BYTES:
            raise ValueError("New source receipt must be a bounded regular file")
        descriptor = os.open(receipt, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("New source receipt changed during validation")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(_MAX_RECEIPT_BYTES + 1)
        finally:
            os.close(descriptor)
        if (
            len(raw) > _MAX_RECEIPT_BYTES
            or hashlib.sha256(raw).hexdigest() != verification.get("server_receipt_sha256")
        ):
            raise ValueError("New source receipt checksum does not match")
        proof = json.loads(raw)
        observation = proof.get("last_observation", {})
        checked = datetime.fromisoformat(verification["checked_at"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("New source needs a valid successful collection receipt") from error
    age = datetime.now(UTC) - checked.astimezone(UTC)
    if (
        age < timedelta(minutes=-5)
        or age > timedelta(days=30)
        or proof.get("kind") != "source_observation"
        or proof.get("source_id") != source.get("id")
        or observation.get("status") != "success"
        or observation.get("http_status") != 200
        or observation.get("url") != source.get("url")
        or observation.get("content_hash") != verification.get("body_sha256")
        or not _SHA256.fullmatch(str(verification.get("body_sha256", "")))
    ):
        raise ValueError("New source receipt must prove recent HTTP 200 content from its official entry")


def _verified_host_expansion(source: dict, added_hosts: set[str], root: Path) -> list[str]:
    """Allow only host additions linked by the source's verified cached HTML."""
    if not added_hosts:
        return []
    verification = source.get("verification", {})
    rows = verification.get("host_expansions")
    if not isinstance(rows, list) or {row.get("host") for row in rows if isinstance(row, dict)} != added_hosts:
        raise ValueError("Pool migration cannot change a source's reviewed network/document scope")
    try:
        _verified_addition(source)
        state_path = root / "state.json"
        state_info = state_path.lstat()
        if not stat.S_ISREG(state_info.st_mode) or state_info.st_size > 16 * 1024 * 1024:
            raise ValueError("invalid collector state")
        state = load(state_path)
        cached = state.get("sources", {}).get(source["id"], {}).get("cache", {}).get(source["url"], {})
        relative = Path(cached.get("path", ""))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or cached.get("content_hash") != verification.get("body_sha256")
        ):
            raise ValueError("cached source body does not match its receipt")
        body_path = root / relative
        current = root
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("cached source body traverses a symlink")
        body_info = body_path.lstat()
        if not stat.S_ISREG(body_info.st_mode) or not 1 <= body_info.st_size <= 8 * 1024 * 1024:
            raise ValueError("cached source body is not a bounded regular file")
        body = body_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != verification.get("body_sha256"):
            raise ValueError("cached source body checksum does not match")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Pool migration cannot change a source's reviewed network/document scope") from error

    class Anchors(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.hrefs = []

        def handle_starttag(self, tag, attrs):
            if tag.casefold() == "a":
                value = dict(attrs).get("href")
                if isinstance(value, str) and len(value) <= 2048:
                    self.hrefs.append(value)

    try:
        anchors = Anchors()
        anchors.feed(body.decode("utf-8-sig"))
        anchors.close()
        proven = set()
        for row in rows:
            host = row.get("host")
            prefix = row.get("path_prefix")
            if (
                host not in added_hosts
                or row.get("evidence_url") != source["url"]
                or row.get("body_sha256") != verification.get("body_sha256")
                or not isinstance(prefix, str)
                or not prefix.startswith("/")
                or ".." in prefix.split("/")
            ):
                raise ValueError("invalid host expansion evidence")
            found = False
            for href in anchors.hrefs:
                try:
                    parsed = urlsplit(href)
                except ValueError:
                    continue
                if (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname == host
                    and parsed.path.startswith(prefix)
                    and parsed.path.casefold().endswith((".pdf", ".doc", ".docx"))
                ):
                    found = True
                    break
            if not found:
                raise ValueError("source HTML does not link an original on the added host")
            proven.add(host)
        if proven != added_hosts:
            raise ValueError("incomplete host expansion evidence")
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("Pool migration cannot change a source's reviewed network/document scope") from error
    return sorted(proven)


def _has_reload_transition(root: Path, old_hash: str, new_hash: str) -> bool:
    """Recognize a recent legacy hot reload so its scope can be audited safely."""
    cutoff = time.time() - 30 * 86400
    events = root / "events"
    if events.is_symlink() or not events.is_dir():
        return False
    for offset in range(31):
        day = (datetime.now(UTC) - timedelta(days=offset)).strftime("%Y-%m-%d")
        directory = events / day
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                prefix = path.name.split("-", 1)[0]
                if not prefix.isdigit() or int(prefix) / 1_000_000_000 < cutoff:
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECEIPT_BYTES:
                    continue
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if (
                event.get("kind") == "source_registry_reloaded"
                and event.get("previous_sha256") == old_hash
                and event.get("pool_sha256") == new_hash
            ):
                return True
    return False


def migrate(old_pool: Path, new_pool: Path, root: Path, country: str) -> dict:
    old_pool, new_pool, root = old_pool.resolve(), new_pool.resolve(), root.resolve()
    old_hash, new_hash = digest(old_pool), digest(new_pool)
    old = load(old_pool)
    new = load(new_pool)
    if old_hash == new_hash:
        raise ValueError("source pool is unchanged")
    for candidate in (old_pool, new_pool):
        source_pool(candidate, country)
    old_sources = {row["id"]: row for row in old["sources"] if row["country"] == country}
    new_sources = {row["id"]: row for row in new["sources"] if row["country"] == country}
    if not old_sources.keys() <= new_sources.keys():
        raise ValueError("Pool migration must preserve every source ID; archive instead of deleting")
    added_source_ids = sorted(new_sources.keys() - old_sources.keys())
    archived_source_ids = sorted(
        source_id
        for source_id, prior in old_sources.items()
        if prior.get("status") == "active" and new_sources[source_id].get("status") == "archived"
    )
    expanded_source_hosts = {}
    for source_id in added_source_ids:
        source = new_sources[source_id]
        verification_status = source.get("verification", {}).get("server_status")
        if source.get("status") == "active":
            if verification_status != "reachable_content":
                raise ValueError("Only verified active sources can be added to the pool")
            _verified_addition(source)
        elif source.get("status") != "archived" or verification_status not in {
            "access_unavailable",
            "http_404_pending_spaced_recheck",
            "http_410_resource_removed",
        }:
            raise ValueError("New archived sources must record why they are unavailable")
    protected = (
        "country",
        "url",
        "kind",
        "topics",
        "discovery_keywords",
        "setneg_type",
    )
    for source_id, prior in old_sources.items():
        updated = new_sources[source_id]
        if any(prior.get(key) != updated.get(key) for key in protected) or prior.get("verification", {}).get(
            "checklist_items", []
        ) != updated.get("verification", {}).get("checklist_items", []):
            raise ValueError("Pool migration cannot change a source's reviewed network/document scope")
        prior_hosts = set(prior.get("allowed_hosts", []))
        updated_hosts = set(updated.get("allowed_hosts", []))
        if not prior_hosts <= updated_hosts:
            raise ValueError("Pool migration cannot change a source's reviewed network/document scope")
        additions = updated_hosts - prior_hosts
        if additions:
            expanded_source_hosts[source_id] = _verified_host_expansion(updated, additions, root)

    state_path = root / "state.json"
    if not root.is_dir() or state_path.is_symlink():
        raise ValueError("Collector state must already exist in a regular project directory")
    with lock(root / ".collector.lock"):
        state = load(state_path)
        binding = state.get("binding", {})
        if binding.get("country") != country:
            raise ValueError("Collector binding does not match the supplied prior pool and country")
        if binding.get("root") != str(root):
            raise ValueError("Collector state path differs from the requested root")
        if state.get("active_sources"):
            raise ValueError("Collector still records active requests; stop and reconcile before migration")
        original = copy.deepcopy(state)
        binding_hash = binding.get("pool_sha256")
        already_reloaded = binding_hash == new_hash
        if binding_hash != old_hash and not (
            already_reloaded and _has_reload_transition(root, old_hash, new_hash)
        ):
            raise ValueError("Collector binding does not match the supplied prior pool and country")
        migration = {
            "at": time.time(),
            "country": country,
            "old_pool_sha256": old_hash,
            "new_pool_sha256": new_hash,
            "source_ids": sorted(new_sources),
            "added_source_ids": added_source_ids,
            "archived_source_ids": archived_source_ids,
            "expanded_source_hosts": expanded_source_hosts,
            "binding_reconciled": already_reloaded,
            "prior_state_sha256": hashlib.sha256(
                json.dumps(original, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest(),
        }
        if not already_reloaded:
            state["binding"]["pool_sha256"] = new_hash
        state.setdefault("pool_migrations", []).append(migration)
        state["updated_at"] = time.time()
        write(state_path, state)
        if load(state_path) != state:
            raise OSError("Migrated collector state verification failed")
        event_dir = root / "events" / datetime.now(UTC).strftime("%Y-%m-%d")
        event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write(
            event_dir / f"{time.time_ns()}-source-pool-migration.json",
            {
                "kind": (
                    "source_pool_binding_migration_reconciled"
                    if already_reloaded
                    else "source_pool_binding_migrated"
                ),
                **migration,
            },
            exclusive=True,
        )
    return {
        "country": country,
        "old_pool_sha256": old_hash,
        "new_pool_sha256": new_hash,
        "added_source_ids": added_source_ids,
        "archived_source_ids": archived_source_ids,
        "expanded_source_hosts": expanded_source_hosts,
        "binding_reconciled": already_reloaded,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-pool", type=Path, required=True)
    parser.add_argument("--new-pool", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--country", choices=("ID", "SG"), required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        print(json.dumps(migrate(args.old_pool, args.new_pool, args.root, args.country)))
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
