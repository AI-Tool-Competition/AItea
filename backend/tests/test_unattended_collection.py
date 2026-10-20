"""Real raw storage and state transitions, with only the HTTPS boundary faked."""

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
collection = importlib.import_module("unattended_collection")


def source(identifier="sg-main", *, host="one.gov.sg", path="/food", **changes):
    return {
        "id": identifier,
        "country": "SG",
        "url": f"https://{host}{path}",
        "allowed_hosts": [host],
        "priority": 80,
        "interval_seconds": 86400,
        "kind": "guidance",
        "topics": ["food"],
        "discovery_keywords": ["food"],
        **changes,
    }


def html(*links, text="Food import official guidance"):
    return (
        "<html><head><title>Food guidance</title></head><body><h1>"
        + text
        + "</h1><p>Official requirements for food imports and lawful trade.</p>"
        + "".join(f'<a href="{link}">Food official document {index}</a>' for index, link in enumerate(links))
        + "</body></html>"
    ).encode()


def pdf(marker="one"):
    return b"%PDF-1.7\n% bounded raw fixture " + marker.encode() + b"\n%%EOF\n"


def docx():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Food official guidance</w:t></w:r></w:p></w:body></w:document>",
        )
    return stream.getvalue()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0], tz)

    monkeypatch.setattr(collection, "ROOT", tmp_path)
    monkeypatch.setattr(collection, "datetime", Clock)
    monkeypatch.setattr(collection.time, "time", lambda: clock[0])
    monkeypatch.setattr(collection.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024**4))
    from app.service import Service

    monkeypatch.setattr(Service, "__init__", lambda *a, **k: pytest.fail("raw collector constructed Service"))
    responses, calls = {}, []
    call_lock = threading.Lock()

    def download(url, **options):
        with options["connection_slot"](urlsplit(url).hostname):
            with call_lock:
                calls.append({"url": url, "validators": dict(options.get("validators", {})), "at": clock[0]})
            response = responses[url]
            if callable(response):
                response = response(url, options)
            if isinstance(response, BaseException):
                raise response
            response = response if isinstance(response, dict) else {"content": response}
            final_url = response.get("url", url)
            if final_url != url:
                with options["connection_slot"](urlsplit(final_url).hostname):
                    pass
            if response.get("not_modified"):
                return {"url": final_url, "not_modified": True, "etag": response.get("etag", '"v1"')}
            content = response["content"]
            return {
                "url": final_url,
                "blob": options["consume"]([content[:7], content[7:]]),
                "mime_type": response.get(
                    "mime_type", "application/pdf" if content.startswith(b"%PDF-") else "text/html"
                ),
                "response_status": 200,
                "etag": response.get("etag", '"v1"'),
                "last_modified": "Tue, 20 Oct 2026 00:00:00 GMT",
            }

    monkeypatch.setattr(collection, "_download", download)
    serial = [0]

    def make(sources=None, *, limits=None, root=None, pool_path=None):
        serial[0] += 1
        pool_path = pool_path or tmp_path / f"pool-{serial[0]}.json"
        if not pool_path.exists():
            pool_path.write_text(
                json.dumps({"protocol": "aitea-official-source-pool-v1", "sources": sources or [source()]}),
                encoding="utf-8",
            )
        settings = {
            "max_response_bytes": 1024**2,
            "daily_source_bytes": 8 * 1024**2,
            "daily_bytes": 32 * 1024**2,
            "min_free_bytes": 1,
            **(limits or {}),
        }
        collector = collection.Collector(
            pool_path, root or tmp_path / f"collector-{serial[0]}", "SG", limits=settings
        )
        return collector

    def events(collector, kind=None):
        values = [
            json.loads(path.read_text()) for path in sorted((collector.root / "events").glob("*/*.json"))
        ]
        return [row for row in values if kind is None or row["kind"] == kind]

    def restart(collector):
        return make(root=collector.root, pool_path=collector.pool_path, limits=collector.limits)

    return SimpleNamespace(
        clock=clock,
        responses=responses,
        calls=calls,
        make=make,
        events=events,
        restart=restart,
        monkeypatch=monkeypatch,
    )


def test_new_original_is_followed_in_same_cycle_and_preserves_raw_mapping(environment):
    env = environment
    collector = env.make()
    origin = source()["url"]
    target = "https://one.gov.sg/food.pdf"
    env.responses.update({origin: html(target), target: pdf()})
    assert collector.tick() == ["sg-main"]
    assert [row["url"] for row in env.calls] == [origin, target]
    state = collector.state["sources"]["sg-main"]
    asset = state["cache"][target]
    assert collection.valid_cached(collector.root, asset)
    assert (collector.root / asset["path"]).read_bytes() == pdf()
    assert asset["legal_review"] == "unreviewed"
    assert asset["discovery"]["parser_verification"] == "deferred_to_bounded_parser"
    mapping = env.events(collector, "mapped_resource")[-1]
    assert mapping["target_hash"] == hashlib.sha256(pdf()).hexdigest()
    assert mapping["parent_hash"] == hashlib.sha256(html(target)).hexdigest()
    assert len(env.events(collector, "content_version")) == 2


@pytest.mark.parametrize(
    ("extension", "content", "mime"),
    [
        ("docx", docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"raw legacy office fixture", "application/msword"),
    ],
)
def test_office_original_is_saved_and_mapped_for_bounded_parser(environment, extension, content, mime):
    env = environment
    collector = env.make()
    origin = source()["url"]
    target = f"https://one.gov.sg/food.{extension}"
    env.responses.update({origin: html(target), target: {"content": content, "mime_type": mime}})
    collector.tick()
    asset = collector.state["sources"]["sg-main"]["cache"][target]
    assert asset["discovery"]["parser_verification"] == "deferred_to_bounded_parser"
    assert (collector.root / asset["path"]).read_bytes() == content
    assert env.events(collector, "mapped_resource")[-1]["target_hash"] == asset["content_hash"]


def test_child_retry_after_stops_same_host_and_survives_restart_and_midnight(environment):
    env = environment
    collector = env.make()
    origin = source()["url"]
    limited, other = "https://one.gov.sg/a.pdf", "https://one.gov.sg/b.pdf"
    collector.state["sources"]["sg-main"] = {
        "frontier": {
            other: {"parent_url": origin, "parent_hash": "a" * 64, "purpose": "document", "attempt_count": 0}
        }
    }
    env.responses.update(
        {
            origin: html(limited),
            limited: collection.DownloadError(
                "HTTP 429", retryable=True, retry_after=2 * 86400, response_host="one.gov.sg"
            ),
            other: pdf("other"),
        }
    )
    started = env.clock[0]
    collector.tick()
    assert [row["url"] for row in env.calls] == [origin, limited]
    assert collector.state["sources"]["sg-main"]["next_at"] >= started + 2 * 86400
    restarted = env.restart(collector)
    env.clock[0] += 86400
    assert restarted.tick() == []
    with pytest.raises(collection.Deferred, match="budget_wait"):
        restarted.fetch(source(), other)
    assert len(env.calls) == 2
    assert restarted.host_cooldowns["one.gov.sg"] == started + 2 * 86400


def test_failed_source_records_relevant_replacement_without_blocking_other_country_source(environment):
    env = environment
    old = source("sg-old", host="old.gov.sg", priority=90)
    new = source("sg-new", host="new.gov.sg", priority=80)
    collector = env.make([old, new])
    env.responses.update({old["url"]: collection.DownloadError("HTTP 410"), new["url"]: html()})
    collector.tick()
    assert collector.state["sources"]["sg-old"]["health"] == "gone"
    assert collector.state["sources"]["sg-new"]["health"] == "healthy"
    env.clock[0] += 86400
    assert collector.tick() == ["sg-new"]
    decision = env.events(collector, "source_replacement")[-1]
    assert (decision["source_id"], decision["target_id"], decision["reason"]) == (
        "sg-old",
        "sg-new",
        "http_410",
    )
    assert decision["shared_topics"] == ["food"]


def test_full_frontier_rotates_new_original_and_skips_evicted_deque_entries(environment):
    env = environment
    collector = env.make(limits={"max_frontier": 2})
    origin = source()["url"]
    old_a, old_b, fresh = [f"https://one.gov.sg/{name}.pdf" for name in ("a", "b", "fresh")]
    collector.state["sources"]["sg-main"] = {
        "frontier": {
            url: {
                "parent_url": origin,
                "parent_hash": "a" * 64,
                "purpose": "document",
                "attempt_count": 1,
                "discovered_at": 0,
            }
            for url in (old_a, old_b)
        }
    }
    env.responses.update({origin: html(fresh), fresh: pdf("fresh"), old_b: pdf("old-b")})
    collector.tick()
    state = collector.state["sources"]["sg-main"]
    assert state["health"] == "healthy"
    assert fresh in state["cache"]
    assert old_a not in [row["url"] for row in env.calls]
    assert len(state["frontier"]) <= 2
    assert env.events(collector, "frontier_archived")[0]["url"] == old_a


def test_cache_metadata_rotates_while_old_blob_and_version_evidence_remain(environment):
    env = environment
    collector = env.make(limits={"max_frontier": 2})
    origin = source()["url"]
    first, second, fresh = [f"https://one.gov.sg/{name}.pdf" for name in ("a", "b", "fresh")]
    env.responses.update(
        {origin: html(first, second), first: pdf("a"), second: pdf("b"), fresh: pdf("fresh")}
    )
    collector.tick()
    old_assets = deepcopy(collector.state["sources"]["sg-main"]["cache"])
    env.clock[0] += 86400
    env.responses[origin] = html(fresh)
    collector.tick()
    current = collector.state["sources"]["sg-main"]
    assert fresh in current["cache"]
    assert len(current["cache"]) <= 3
    archived = env.events(collector, "cache_metadata_archived")
    assert archived
    old = archived[0]["url"]
    assert collection.valid_cached(collector.root, old_assets[old])
    assert any(
        row["content_hash"] == old_assets[old]["content_hash"]
        for row in env.events(collector, "content_version")
    )


def test_pending_originals_drain_without_refetching_root_before_its_interval(environment):
    env = environment
    collector = env.make(limits={"requests_per_source_cycle": 2})
    origin = source()["url"]
    originals = [f"https://one.gov.sg/food-{index}.pdf" for index in range(4)]
    env.responses[origin] = html(*originals)
    env.responses.update({url: pdf(str(index)) for index, url in enumerate(originals)})
    collector.tick()
    assert len(env.calls) == 2
    env.clock[0] += 30
    collector.tick()
    env.clock[0] += 30
    collector.tick()
    assert [row["url"] for row in env.calls].count(origin) == 1
    assert set(originals) <= set(collector.state["sources"]["sg-main"]["cache"])
    assert len(env.calls) == 5


def test_daily_quota_resumes_child_after_midnight_without_waiting_for_root_interval(environment):
    env = environment
    collector = env.make(limits={"daily_requests": 1})
    origin, target = source()["url"], "https://one.gov.sg/food.pdf"
    env.responses.update({origin: html(target), target: pdf()})
    started = env.clock[0]
    collector.tick()
    midnight = (int(started) // 86400 + 1) * 86400
    state = collector.state["sources"]["sg-main"]
    assert midnight <= state["next_at"] <= midnight + 300
    assert state["root_next_at"] == started + 86400
    env.clock[0] = state["next_at"]
    collector.tick()
    assert [row["url"] for row in env.calls] == [origin, target]
    assert target in collector.state["sources"]["sg-main"]["cache"]
    assert len(list((collector.root / "requests").glob("*/*.json"))) == 2


def test_304_reuses_verified_original_and_does_not_invent_new_version(environment):
    env = environment
    collector = env.make()
    origin = source()["url"]
    env.responses[origin] = {"content": html(), "etag": '"version-a"'}
    collector.tick()
    first = deepcopy(collector.state["sources"]["sg-main"]["cache"][origin])
    env.clock[0] += 86400
    env.responses[origin] = {"not_modified": True, "etag": '"version-a"'}
    collector.tick()
    current = collector.state["sources"]["sg-main"]["cache"][origin]
    assert current["content_hash"] == first["content_hash"]
    assert current["path"] == first["path"]
    assert env.calls[-1]["validators"]["etag"] == '"version-a"'
    assert collection.valid_cached(collector.root, current)
    assert len(env.events(collector, "content_version")) == 1
    receipt = next(iter(collector.requests.values()))
    assert receipt["outcome"]["http_status"] == 304
    assert receipt["charged_bytes"] == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_304_cannot_be_success_when_cached_original_is_missing_or_corrupt(environment, damage):
    env = environment
    collector = env.make()
    origin = source()["url"]
    env.responses[origin] = html()
    receipt = collector.fetch(source(), origin)
    asset = receipt["asset"]
    path = collector.root / asset["path"]
    if damage == "missing":
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"tampered")
    env.responses[origin] = {"not_modified": True}
    failed = collector.fetch(source(), origin, asset)
    assert env.calls[-1]["validators"] == {}
    assert failed["outcome"]["status"] == "content_invalid"
    assert failed["asset"] is None
    assert failed["charged_bytes"] == collector.limits["max_response_bytes"]


def test_restart_retains_issued_quota_and_interrupted_source_then_next_day_recovers(environment):
    env = environment
    collector = env.make(limits={"daily_requests": 1})
    origin = source()["url"]
    path, receipt = collector.reserve(source(), origin)
    collector.state["sources"]["sg-main"] = {"selection_count": 1}
    collector.state["active_sources"] = ["sg-main"]
    collector.save()
    restarted = env.restart(collector)
    interrupted = restarted.state["sources"]["sg-main"]
    assert restarted.state["active_sources"] == []
    assert interrupted["last_observation"]["error_type"] == "InterruptedGET"
    assert interrupted["failure_count"] == 1
    assert (
        restarted.requests[receipt["request_id"]]["charged_bytes"] == collector.limits["max_response_bytes"]
    )
    env.clock[0] = interrupted["next_at"]
    restarted.tick()
    assert env.calls == []
    state = restarted.state["sources"]["sg-main"]
    assert state["failure_count"] == 1
    env.responses[origin] = html()
    env.clock[0] = state["next_at"]
    restarted.tick()
    assert len(env.calls) == 1
    assert restarted.state["sources"]["sg-main"]["health"] == "healthy"
    assert json.loads(path.read_text()) == receipt


def test_stop_before_first_request_preserves_website_health_without_get(environment):
    env = environment
    collector = env.make()
    collector.stop.set()
    collector.tick()
    state = collector.state["sources"]["sg-main"]
    assert env.calls == []
    assert state.get("failure_count", 0) == 0
    assert state.get("health") not in {"degraded", "unavailable"}
    assert not list((collector.root / "requests").glob("*/*.json"))


def test_storage_wait_preserves_health_and_recovers_when_space_returns(environment):
    env = environment
    collector = env.make()
    env.monkeypatch.setattr(collection.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    collector.tick()
    state = collector.state["sources"]["sg-main"]
    assert state["scheduling_status"] == "storage_wait"
    assert state["failure_count"] == 0
    assert env.calls == []
    env.clock[0] = state["next_at"]
    env.monkeypatch.setattr(collection.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024**4))
    env.responses[source()["url"]] = html()
    collector.tick()
    assert collector.state["sources"]["sg-main"]["health"] == "healthy"


def test_two_sources_execute_concurrently_and_failure_is_isolated(environment):
    env = environment
    one, two = source("sg-one"), source("sg-two", host="two.gov.sg")
    collector = env.make([one, two], limits={"workers": 2, "sources_per_tick": 2})
    barrier = threading.Barrier(2)

    def first(url, options):
        barrier.wait(timeout=3)
        return collection.DownloadError("HTTP 503", retryable=True)

    def second(url, options):
        barrier.wait(timeout=3)
        return html()

    env.responses.update({one["url"]: first, two["url"]: second})
    collector.tick()
    states = collector.state["sources"]
    assert states["sg-one"]["failure_count"] == 1
    assert states["sg-two"]["success_count"] == 1
    assert states["sg-two"]["failure_count"] == 0
    assert len(env.calls) == 2
    assert collector.state["active_sources"] == []


def test_per_host_slot_bound_is_enforced_for_concurrent_source_workers(environment):
    env = environment
    sources = [source(f"sg-{index}", path=f"/food-{index}") for index in range(4)]
    collector = env.make(sources, limits={"workers": 4, "sources_per_tick": 4, "per_host": 2})
    release, two_entered = threading.Event(), threading.Event()
    mutex = threading.Lock()
    active, peaks = [0], [0]

    def response(url, options):
        with mutex:
            active[0] += 1
            peaks[0] = max(peaks[0], active[0])
            if active[0] == 2:
                two_entered.set()
        assert release.wait(timeout=3)
        with mutex:
            active[0] -= 1
        return html()

    env.responses.update({row["url"]: response for row in sources})
    failures = []

    def tick():
        try:
            collector.tick()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=tick)
    thread.start()
    assert two_entered.wait(timeout=3)
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    assert peaks[0] == 2
    assert all(row["health"] == "healthy" for row in collector.state["sources"].values())


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://private.example/food", "allowed_hosts": ["private.example"]},
        {"url": "https://one.gov.sg.evil.test/food", "allowed_hosts": ["one.gov.sg.evil.test"]},
        {"url": "https://one.go.id/food", "allowed_hosts": ["one.go.id"]},
        {"url": "http://one.gov.sg/food"},
        {"url": "https://user:secret@one.gov.sg/food"},
        {"url": "https://127.0.0.1/food", "allowed_hosts": ["127.0.0.1"]},
    ],
)
def test_unreviewed_or_nonofficial_source_scope_is_rejected(environment, changes):
    with pytest.raises((ValueError, collection.DownloadError)):
        environment.make([source(**changes)])
    assert environment.calls == []


def test_redirect_host_must_remain_in_reviewed_allowlist(environment):
    env = environment
    collector = env.make()
    origin = source()["url"]
    env.responses[origin] = {"content": html(), "url": "https://outside.example/food"}
    collector.tick()
    state = collector.state["sources"]["sg-main"]
    assert state["failure_count"] == 1
    assert not state["cache"]
    assert not env.events(collector, "content_version")


def test_existing_scope_cannot_silently_change_or_reinitialize_missing_state(environment):
    env = environment
    collector = env.make()
    pool = json.loads(collector.pool_path.read_text())
    pool["sources"][0]["priority"] -= 1
    collector.pool_path.write_text(json.dumps(pool))
    with pytest.raises(ValueError, match="scope changed"):
        env.restart(collector)
    collector.state_path.unlink()
    with pytest.raises(ValueError, match="Missing state"):
        env.restart(collector)


def test_worker_keeps_in_progress_source_state_private_until_it_returns(environment):
    env = environment
    collector = env.make()
    origin, target = source()["url"], "https://one.gov.sg/food.pdf"
    previous = {
        "frontier": {
            target: {"parent_url": origin, "parent_hash": "a" * 64, "purpose": "document", "attempt_count": 1}
        }
    }
    frozen = deepcopy(previous)
    env.responses.update({origin: html(target), target: pdf()})
    result = collector.collect_source(source(), previous)
    assert previous == frozen
    assert result["frontier"][target]["attempt_count"] == 2


def test_parent_version_and_multiple_parent_references_are_preserved(environment):
    env = environment
    collector = env.make()
    origin, detail, target = source()["url"], "https://one.gov.sg/details", "https://one.gov.sg/food.pdf"
    env.responses.update({origin: html(target, detail), detail: html(target), target: pdf("first")})
    collector.tick()
    first_root_hash = collector.state["sources"]["sg-main"]["cache"][origin]["content_hash"]
    env.clock[0] += 7 * 86400
    env.responses[origin] = html(target, detail, text="Food updated official guidance")
    env.responses[target] = pdf("second")
    collector.tick()
    references = [row for row in env.events(collector, "source_reference") if row["target_url"] == target]
    root_references = [row for row in references if row["parent_url"] == origin]
    assert first_root_hash in {row["parent_hash"] for row in root_references}
    assert len({row["parent_hash"] for row in root_references}) == 2
    assert any(row["parent_url"] == detail for row in references)
    mapping = [row for row in env.events(collector, "mapped_resource") if row["target_url"] == target][-1]
    assert mapping["target_hash"] == hashlib.sha256(pdf("second")).hexdigest()
    assert {row["url"] for row in mapping["parents"]} >= {origin, detail}
    assert all(row["legal_review"] == "unreviewed" for row in env.events(collector, "content_version"))


def test_explicit_empty_catalogue_has_low_frequency_recheck_and_retains_raw_response(environment):
    env = environment
    configured = source(kind="catalogue", interval_seconds=7 * 86400)
    collector = env.make([configured])
    env.responses[configured["url"]] = html(text="Belum ada peraturan food")
    started = env.clock[0]
    collector.tick()
    state = collector.state["sources"]["sg-main"]
    assert state["last_observation"]["error_type"] == "EmptyCatalogue"
    assert state["next_at"] >= started + 7 * 86400
    receipt = next(iter(collector.requests.values()))
    raw = receipt["raw_response"]
    assert collection.valid_cached(collector.root, raw)
    assert not state["cache"]
