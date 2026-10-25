"""Real raw storage and state transitions, with only the HTTPS boundary faked."""

import hashlib
import importlib
import io
import json
import socket
import ssl
import sys
import threading
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest
from app.configuration.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
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


def test_versioned_source_registry_validates_hierarchy_relations_and_archives(tmp_path):
    registry_path = ROOT / "config/collection-sources/id-sg-unattended.json"
    registry = json.loads(registry_path.read_text())
    assert registry["protocol"] == "aitea-official-source-pool-v2"
    assert registry["schema_version"] == 2
    assert len(registry["source_relations"]) >= 6
    for country in ("ID", "SG"):
        active = collection.source_pool(registry_path, country)
        assert active
        assert all(source["transport"] == "proxy" and source["status"] == "active" for source in active)
        if country == "ID":
            assert "id-bpk-regulations" not in {source["id"] for source in active}
            assert "id-bpjph-regulations" not in {source["id"] for source in active}
            assert "id-bpjph-jdih-cari" in {source["id"] for source in active}
        if country == "SG":
            assert "sg-ccs-fair-trading" in {source["id"] for source in active}
            assert {source["id"] for source in active} >= {"sg-sso-acts-current", "sg-sso-statutes"}
    archived_bpk = next(source for source in registry["sources"] if source["id"] == "id-bpk-regulations")
    assert archived_bpk["status"] == "archived"
    assert archived_bpk["verification"]["latest_probe"]["failure_code"] == "http_403"
    archived_bpjph_home = next(source for source in registry["sources"] if source["id"] == "id-bpjph-regulations")
    assert archived_bpjph_home["status"] == "archived"
    assert archived_bpjph_home["empty_catalogue"] is True
    bpjph_catalogue = next(source for source in registry["sources"] if source["id"] == "id-bpjph-jdih-cari")
    assert bpjph_catalogue["url"] == "https://jdih.halal.go.id/cari"
    assert bpjph_catalogue["verification"]["latest_probe"]["document_candidates"] == 2
    ccs = next(source for source in registry["sources"] if source["id"] == "sg-ccs-fair-trading")
    assert ccs["url"] == "https://www.ccs.gov.sg/consumer-protection/fair-trading-practices/"
    assert ccs["allowed_hosts"] == ["www.ccs.gov.sg"]
    assert ccs["html_asset_hosts"] == ["isomer-user-content.by.gov.sg"]
    assert len(ccs["html_document_paths"]) == 7
    assert "/consumer-protection/fair-trading-practices/know-more-about-dark-patterns/" in ccs[
        "html_document_paths"
    ]
    assert ccs["group_id"] == "sg-consumer-protection"
    assert ccs["verification"]["checklist_items"] == ["SG-3.5"]
    assert ccs["verification"]["server_status"] == "reachable_content"
    assert any(
        row["source_id"] == "sg-ccs-fair-trading"
        and row["related_source_id"] == "sg-sfa-labelling"
        and row["type"] == "complements"
        for row in registry["source_relations"]
    )
    sso_acts = next(row for row in registry["sources"] if row["id"] == "sg-sso-acts-current")
    sso_laws = next(row for row in registry["sources"] if row["id"] == "sg-sso-statutes")
    assert sso_acts["discovery"] == {
        "adapter": "sso_catalog_2026",
        "category": "Act",
        "state": "Current",
        "page_size": 500,
        "cache_limit": 10000,
    }
    assert sso_laws["discovery"]["category"] == "SL"
    assert sso_laws["verification"]["checklist_items"] == ["SG-1.3"]

    rejected = tmp_path / "unverified-active-source.json"
    invalid_sso_policy = deepcopy(registry)
    next(row for row in invalid_sso_policy["sources"] if row["id"] == "sg-sso-statutes")["discovery"][
        "page_size"
    ] = 501
    rejected.write_text(json.dumps(invalid_sso_policy))
    with pytest.raises(ValueError, match="discovery policy"):
        collection.source_pool(rejected, "SG")

    inaccessible = deepcopy(registry)
    next(source for source in inaccessible["sources"] if source["status"] == "active")["verification"][
        "server_status"
    ] = "access_unavailable"
    rejected.write_text(json.dumps(inaccessible))
    with pytest.raises(ValueError, match="successful bounded content probe"):
        collection.source_pool(rejected, "ID")

    traversal = deepcopy(registry)
    next(source for source in traversal["sources"] if source["id"] == "sg-ccs-fair-trading")[
        "html_document_paths"
    ][0] = "/consumer-protection/%2e%2e%2funapproved/"
    rejected.write_text(json.dumps(traversal))
    with pytest.raises(ValueError, match="HTML document paths"):
        collection.source_pool(rejected, "SG")

    asset_host = deepcopy(registry)
    next(source for source in asset_host["sources"] if source["id"] == "sg-ccs-fair-trading")[
        "html_asset_hosts"
    ] = ["unreviewed.example"]
    rejected.write_text(json.dumps(asset_host))
    with pytest.raises(ValueError, match="HTML asset hosts"):
        collection.source_pool(rejected, "SG")

    archived_id = "id-setneg-perpu"
    next(source for source in registry["sources"] if source["id"] == archived_id)["status"] = "archived"
    migrated = tmp_path / "registry.json"
    migrated.write_text(json.dumps(registry))
    assert archived_id not in {source["id"] for source in collection.source_pool(migrated, "ID")}
    relation = registry["source_relations"][0]
    relation["related_source_id"] = "missing-source"
    migrated.write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="source relationship"):
        collection.source_pool(migrated, "ID")


def html(*links, text="Food import official guidance"):
    return (
        "<html><head><title>Food guidance</title></head><body><h1>"
        + text
        + "</h1><p>Official requirements for food imports and lawful trade.</p>"
        + "".join(f'<a href="{link}">Food official document {index}</a>' for index, link in enumerate(links))
        + "</body></html>"
    ).encode()


def test_sso_source_recipe_preserves_row_identity_and_parent_locator():
    source_row = {
        "id": "sg-sso-statutes",
        "country": "SG",
        "url": "https://sso.agc.gov.sg/Browse/SL/Current/All",
        "allowed_hosts": ["sso.agc.gov.sg"],
        "kind": "catalogue",
        "discovery": {
            "adapter": "sso_catalog_2026",
            "category": "SL",
            "state": "Current",
            "page_size": 20,
            "cache_limit": 10000,
        },
    }
    network_url = collection.request_url(source_row, source_row["url"])
    raw = (ROOT / "backend/tests/fixtures/discovery/sso-sl-current-20261019.html").read_bytes()
    result = collection.discover_page(network_url, raw, "text/html", collection.recipe(source_row))

    assert result["matched"]
    assert result["scope"]["total"] == 5872
    assert len(result["links"]) == 21
    document = next(link for link in result["links"] if link["purpose"] != "catalog")
    evidence = collection.source_reference_fields(source_row, document, hashlib.sha256(raw).hexdigest())
    assert evidence["identity_key"] == document["identity_key"]
    assert evidence["source_locator"] == document["source_locator"]
    assert evidence["source_status"] == "Current"

    timestamped = {**document, "document_date": "20171229170000"}
    assert collection.source_reference_fields(source_row, timestamped, hashlib.sha256(raw).hexdigest())[
        "document_date"
    ] == "20171229170000"
    invalid = {**document, "document_date": "2017122917000"}
    with pytest.raises(ValueError, match="row identity or locator"):
        collection.source_reference_fields(source_row, invalid, hashlib.sha256(raw).hexdigest())


def test_sso_frontier_replays_catalogue_rows_after_each_bounded_batch(tmp_path):
    source_row = {
        "id": "sg-sso-statutes",
        "country": "SG",
        "url": "https://sso.agc.gov.sg/Browse/SL/Current/All",
        "allowed_hosts": ["sso.agc.gov.sg"],
        "kind": "catalogue",
        "discovery": {
            "adapter": "sso_catalog_2026",
            "category": "SL",
            "state": "Current",
            "page_size": 500,
            "cache_limit": 10000,
        },
    }
    parent_hash = "a" * 64
    links = [
        {
            "url": f"https://sso.agc.gov.sg/SL/Rule-{index}?DocDate=20260101",
            "purpose": "document",
            "identity_key": f"sso:sl:rule-{index}",
            "source_locator": {
                "source_sha256": parent_hash,
                "table_class": "browse-list",
                "row": index,
                "unit": "decoded_unicode_char",
                "start": index,
                "end": index + 1,
                "fragment_sha256": "b" * 64,
            },
            "document_date": "20260101",
            "source_status": "Current",
        }
        for index in range(1, 145)
    ]
    catalog_asset = {
        "requested_url": source_row["url"] + "?PageSize=500&SortBy=Title&SortOrder=ASC",
        "content_hash": parent_hash,
        "discovery": {"links": links},
    }
    collector = object.__new__(collection.Collector)
    collector.limits = {"max_frontier": 128}
    collector.root = tmp_path
    collector.event = lambda *args, **kwargs: None
    frontier, cache, urls = {}, {}, []

    collector.map_links(source_row, source_row["url"], catalog_asset, None, frontier, cache, urls)
    assert len(frontier) == 128
    for url in list(frontier)[:16]:
        frontier.pop(url)
        cache[url] = {
            "observed_at": collection.time.time(),
            "source_document_date": "20260101",
            "source_original_verified": True,
        }

    collector.map_links(source_row, source_row["url"], catalog_asset, None, frontier, cache, urls)
    assert len(frontier) == 128
    assert {row["identity_key"] for row in frontier.values()} == {
        f"sso:sl:rule-{index}" for index in range(17, 145)
    }


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
    from app.bootstrap import Service

    monkeypatch.setattr(Service, "__init__", lambda *a, **k: pytest.fail("raw collector constructed Service"))
    responses, calls = {}, []
    call_lock = threading.Lock()

    def download(url, **options):
        with options["connection_slot"](urlsplit(url).hostname):
            with call_lock:
                calls.append(
                    {
                        "url": url,
                        "validators": dict(options.get("validators", {})),
                        "accept_compressed_json": options.get("accept_compressed_json", False),
                        "at": clock[0],
                    }
                )
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

    def make(sources=None, *, limits=None, root=None, pool_path=None, country="SG"):
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
            pool_path, root or tmp_path / f"collector-{serial[0]}", country, limits=settings
        )
        return collector

    def events(collector, kind=None):
        values = [
            json.loads(path.read_text()) for path in sorted((collector.root / "events").glob("*/*.json"))
        ]
        return [row for row in values if kind is None or row["kind"] == kind]

    def restart(collector):
        return make(
            root=collector.root,
            pool_path=collector.pool_path,
            limits=collector.limits,
            country=collector.country,
        )

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


def test_repeated_pre_response_network_failures_do_not_exhaust_byte_budget(environment):
    env = environment
    collector = env.make(limits={"daily_source_bytes": 4 * 1024**2})
    origin = source()["url"]
    env.responses[origin] = lambda *_: collection.DownloadError(
        "抓取连接失败或超时，请检查来源后重试。", retryable=True, bytes_received=0
    )

    receipts = [collector.fetch(source(), origin) for _ in range(4)]
    path, fifth = collector.reserve(source(), origin)

    assert all(receipt["outcome"]["status"] == "network_error" for receipt in receipts)
    assert all(receipt["charged_bytes"] == 0 for receipt in receipts)
    assert (
        sum(row["charged_bytes"] for row in collector.requests.values())
        == collector.limits["max_response_bytes"]
    )
    assert fifth["status"] == "issued"
    assert path.exists()


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (socket.gaierror(-2, "resolver detail"), "dns_resolution_failed"),
        (socket.timeout("socket detail"), "network_timeout"),
        (ssl.SSLCertVerificationError("certificate detail"), "tls_certificate_verification_failed"),
        (ssl.SSLError("handshake detail"), "tls_handshake_failed"),
        (ConnectionRefusedError("peer detail"), "connection_refused"),
        (ConnectionResetError("peer detail"), "connection_interrupted"),
    ],
)
def test_network_failures_have_stable_codes_without_persisting_exception_text(environment, cause, expected):
    collector = environment.make()
    origin = source()["url"]

    def fail_with_cause(*_):
        try:
            raise cause
        except OSError as exc:
            raise collection.DownloadError("抓取连接失败或超时，请检查来源后重试。", retryable=True) from exc

    environment.responses[origin] = fail_with_cause
    receipt = collector.fetch(source(), origin)

    assert receipt["outcome"]["status"] == "network_error"
    assert receipt["outcome"]["failure_code"] == expected
    assert "detail" not in json.dumps(receipt["outcome"], ensure_ascii=False)


def test_nonretryable_scope_rejection_is_not_reported_as_network_failure(environment):
    collector = environment.make()
    origin = source()["url"]
    environment.responses[origin] = lambda *_: collection.DownloadError(
        "Redirect host is outside the reviewed official source scope"
    )

    receipt = collector.fetch(source(), origin)

    assert receipt["outcome"]["status"] == "content_invalid"
    assert receipt["outcome"]["failure_code"] == "redirect_scope_rejected"


def test_http_error_has_stable_status_code(environment):
    collector = environment.make()
    origin = source()["url"]
    environment.responses[origin] = lambda *_: collection.DownloadError("HTTP 403 Forbidden")

    receipt = collector.fetch(source(), origin)

    assert receipt["outcome"]["status"] == "http_error"
    assert receipt["outcome"]["failure_code"] == "http_403"


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


def test_valid_registry_edits_reload_without_reinitializing_evidence(environment):
    env = environment
    collector = env.make()
    pool = json.loads(collector.pool_path.read_text())
    pool["sources"][0]["priority"] -= 1
    collector.pool_path.write_text(json.dumps(pool))
    previous = collector.binding["pool_sha256"]
    resumed = env.restart(collector)
    assert resumed.binding["pool_sha256"] != previous
    assert resumed.sources[0]["priority"] == pool["sources"][0]["priority"]
    assert resumed.state["sources"] == collector.state["sources"]
    assert len(list((collector.root / "registry-versions").glob("*.json"))) == 2
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


def test_new_parent_references_reuse_healthy_common_original_and_preserve_budget_for_new_pdfs(environment):
    env = environment
    collector = env.make()
    origin = source()["url"]
    shared = "https://one.gov.sg/shared-food.pdf"
    details = [f"https://one.gov.sg/detail-{index:02}" for index in range(10)]
    originals = [f"https://one.gov.sg/food-{index:02}.pdf" for index in range(10)]
    env.responses.update({origin: html(shared, *details), shared: pdf("shared")})
    for index, (detail, original) in enumerate(zip(details, originals, strict=True)):
        env.responses[detail] = html(shared, original, text=f"Food official guidance {index}")
        env.responses[original] = pdf(str(index))

    for _ in range(6):
        collector.tick()
        env.clock[0] += collector.limits["tick_seconds"]

    state = collector.state["sources"]["sg-main"]
    called = [row["url"] for row in env.calls]
    assert called.count(shared) == 1
    assert all(called.count(original) == 1 for original in originals)
    assert len(called) == 22  # One root, one shared original, ten details and ten new originals.
    assert state["scheduling_status"] != "budget_wait"
    references = [row for row in env.events(collector, "source_reference") if row["target_url"] == shared]
    assert {row["parent_url"] for row in references} == {origin, *details}
    assert len(state["frontier"][shared]["parents"]) == 8
    assert all(collection.valid_cached(collector.root, state["cache"][url]) for url in originals)

    # The root's original reference has rotated out of the bounded active
    # parents list. Reobserving that unchanged root must still reuse the PDF.
    env.clock[0] = state["root_next_at"]
    collector.tick()
    assert [row["url"] for row in env.calls].count(shared) == 1
    assert len(env.calls) == 23


def test_changed_known_parent_content_revalidates_original_before_its_normal_recheck(environment):
    env = environment
    collector = env.make()
    origin = source()["url"]
    detail, target = "https://one.gov.sg/details", "https://one.gov.sg/food.pdf"
    first_root = html(detail, text="Food initial official root")
    first_detail = html(target, text="Food initial official detail")
    second_detail = html(target, text="Food revised official detail")
    env.responses.update(
        {
            origin: first_root,
            detail: first_detail,
            target: pdf("first"),
        }
    )
    collector.tick()
    first_state = collector.state["sources"]["sg-main"]
    scheduled_target_recheck = first_state["frontier"][target]["health"]["next_at"]
    assert [row["url"] for row in env.calls] == [origin, detail, target]

    env.clock[0] = first_state["root_next_at"]
    assert env.clock[0] < scheduled_target_recheck
    env.responses.update(
        {
            origin: html(detail, text="Food revised official root"),
            detail: second_detail,
            target: pdf("second"),
        }
    )
    collector.tick()
    assert [row["url"] for row in env.calls] == [origin, detail, target, origin, detail, target]
    current = collector.state["sources"]["sg-main"]
    asset = current["cache"][target]
    assert asset["content_hash"] == hashlib.sha256(pdf("second")).hexdigest()
    assert collection.valid_cached(collector.root, asset)
    references = [
        row
        for row in env.events(collector, "source_reference")
        if row["parent_url"] == detail and row["target_url"] == target
    ]
    assert {row["parent_hash"] for row in references} == {
        hashlib.sha256(first_detail).hexdigest(),
        hashlib.sha256(second_detail).hexdigest(),
    }
    mapping = [row for row in env.events(collector, "mapped_resource") if row["target_url"] == target][-1]
    assert mapping["parent_hash"] == hashlib.sha256(second_detail).hexdigest()
    assert mapping["target_hash"] == asset["content_hash"]

    # A previous A hash remains among the parents after B. Returning to A is
    # still a real observed version change, so it must refresh the target.
    env.clock[0] = current["root_next_at"]
    assert env.clock[0] < scheduled_target_recheck
    env.responses.update({origin: first_root, detail: first_detail, target: pdf("third")})
    collector.tick()
    assert [row["url"] for row in env.calls] == [origin, detail, target] * 3
    reverted = collector.state["sources"]["sg-main"]["cache"][target]
    assert reverted["content_hash"] == hashlib.sha256(pdf("third")).hexdigest()
    assert collection.valid_cached(collector.root, reverted)
    mapping = [row for row in env.events(collector, "mapped_resource") if row["target_url"] == target][-1]
    assert mapping["parent_hash"] == hashlib.sha256(first_detail).hexdigest()
    assert mapping["target_hash"] == reverted["content_hash"]


def test_explicit_empty_catalogue_has_low_frequency_recheck_and_retains_raw_response(environment):
    env = environment
    configured = source(kind="catalogue", empty_catalogue=True, interval_seconds=7 * 86400)
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


def test_empty_catalogue_banner_does_not_hide_discovered_originals(environment):
    env = environment
    configured = source(kind="catalogue", empty_catalogue=True, interval_seconds=7 * 86400)
    original = "https://one.gov.sg/food/regulation.pdf"
    collector = env.make([configured])
    env.responses[configured["url"]] = html(original, text="Belum ada peraturan food")
    env.responses[original] = pdf("available-original")

    collector.tick()

    state = collector.state["sources"][configured["id"]]
    assert state["health"] == "healthy"
    assert state["last_observation"]["status"] == "success"
    assert original in state["cache"]


SETNEG = "https://jdih.setneg.go.id/api/hukumproduk/"
SETNEG_ROOT = SETNEG + "produkhukum?jns=PERPU&length=50&start=0"


def setneg_source(**changes):
    return source(
        "id-setneg-perpu",
        host="jdih.setneg.go.id",
        path="/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0",
        **{"country": "ID", "kind": "catalogue", **changes},
    )


def setneg_responses(count=51):
    """Generate bounded rows using the already captured Setneg fixture schema."""
    template = json.loads((ROOT / "backend/tests/fixtures/discovery/setneg-detail.json").read_text())
    rows, responses = [], {}
    for index in range(count):
        row = {
            **template["row"][0],
            "idperaturan": f"P{50000 + index}",
            "jns": "PERPU",
            "no_peraturan": str(index + 1),
            "tahun": "2026",
            "tentang": f"Synthetic food scope fixture {index + 1}",
        }
        rows.append(row)
        name = f"PERPU {index + 1} Tahun 2026.pdf"
        detail = SETNEG + "detaildata?" + urlencode({"jns": "PERPU", "no": str(index + 1), "thn": "2026"})
        original = SETNEG + "pdf?" + urlencode({"f": name, "fl": row["idperaturan"], "l": "uploads"})
        responses[detail] = {
            "content": json.dumps({**template, "row": [row], "file": [{"realName": name}]}).encode(),
            "mime_type": "application/json",
        }
        responses[original] = pdf(str(index + 1))
    for start in range(0, count, 50):
        url = SETNEG + "produkhukum?" + urlencode({"jns": "PERPU", "length": 50, "start": start})
        responses[url] = {
            "content": json.dumps({"jml": count, "jn": ["PERPU"], "data": rows[start : start + 50]}).encode(),
            "mime_type": "application/json",
        }
    return responses


@pytest.mark.parametrize(
    "changes",
    [
        {"url": SETNEG_ROOT.replace("jdih.setneg.go.id", "other.go.id"), "allowed_hosts": ["other.go.id"]},
        {"url": SETNEG_ROOT.replace("jns=PERPU", "jns=UU")},
        {"url": SETNEG_ROOT.replace("length=50", "length=20")},
        {"url": SETNEG_ROOT.replace("start=0", "start=50")},
        {"url": SETNEG_ROOT + "&extra=1"},
        {"allowed_hosts": ["jdih.setneg.go.id", "other.go.id"]},
        {"country": "SG"},
        {"setneg_type": "PP"},
        {"kind": "guidance"},
        {"id": "id-setneg-other"},
    ],
)
def test_special_setneg_source_cannot_change_reviewed_host_url_type_or_scope(environment, changes):
    with pytest.raises((ValueError, collection.DownloadError)):
        environment.make([setneg_source(**changes)], country=changes.get("country", "ID"))
    assert environment.calls == []


def test_setneg_full_fifty_row_page_keeps_next_catalog_detail_and_original_mappings(environment):
    env = environment
    configured = setneg_source()
    collector = env.make([configured], country="ID")
    env.responses.update(setneg_responses())
    collector.tick()
    state = collector.state["sources"]["id-setneg-perpu"]
    assert state["health"] == "healthy"
    root = state["cache"][SETNEG_ROOT]
    discovery = root["discovery"]
    assert discovery["scope"] == {
        "provider": "setneg",
        "jns": "PERPU",
        "total": 51,
        "offset": 0,
        "page_size": 50,
        "returned": 50,
    }
    assert len(discovery["links"]) == 51
    assert sum(row["purpose"] == "detail" for row in discovery["links"]) == 50
    next_catalog = SETNEG + "produkhukum?jns=PERPU&length=50&start=50"
    assert next_catalog in state["frontier"]
    next_page = collector.fetch(configured, next_catalog)
    assert next_page["asset"]["discovery"]["scope"]["returned"] == 1
    assert next_page["asset"]["discovery"]["scope"]["offset"] == 50
    originals = [asset for url, asset in state["cache"].items() if "/pdf?" in url]
    assert originals
    assert all(collection.valid_cached(collector.root, asset) for asset in originals)
    mapped = [row for row in env.events(collector, "mapped_resource") if "/pdf?" in row["target_url"]]
    assert mapped and all("/detaildata?" in row["parent_url"] for row in mapped)
    assert all(row["accept_compressed_json"] for row in env.calls)
    assert len(env.calls) <= collector.limits["requests_per_source_cycle"] + 1


@pytest.mark.parametrize("country", ["ID", "SG"])
def test_only_exact_setneg_source_enables_json_transport_and_original_pool_recipes_stay_bounded(
    environment, country
):
    env = environment
    pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    configured = pool["sources"]
    assert len(configured) == 22
    originals = [
        row
        for row in configured
        if row["id"] != "id-setneg-perpu" and row.get("discovery") is None
    ]
    assert len(originals) == 19
    for item in originals:
        recipe = collection.recipe(item)
        assert "setneg_type" not in recipe
        assert "required_query_by_host" not in recipe
        assert recipe["max_links"] == (200 if item["id"] == "id-beacukai-regulations" else 32)
    collector = env.make(configured, country=country)
    probe_sources = [item for item in collector.sources if item.get("discovery") is None]
    env.responses.update({item["url"]: html() for item in probe_sources})
    env.responses.update(setneg_responses(count=1))
    for item in probe_sources:
        collector.fetch(item, item["url"])
    special = {item["url"] for item in collector.sources if item["id"] == "id-setneg-perpu"}
    assert all(row["accept_compressed_json"] == (row["url"] in special) for row in env.calls)


def test_beacukai_policy_replays_verified_cache_without_requests_and_reclassifies_frontier(environment):
    env = environment
    pool_path = ROOT / "config/collection-sources/id-sg-unattended.json"
    collector = env.make(pool_path=pool_path, country="ID")
    source_row = next(row for row in collector.sources if row["id"] == "id-beacukai-regulations")
    category = "https://peraturan.beacukai.go.id/index.html?page=jenis/12/peraturan-menteri-keuangan/1.html"
    detail = (
        "https://peraturan.beacukai.go.id/index.html?page=detail/jenis/12/576/"
        "peraturan-menteri-keuangan/pmk-208-pmk-11-2013/tarif-bea-masuk.html"
    )
    original = (
        "http://repository.beacukai.go.id/referensi/2026/10/"
        "a5d5a7b56c24-pmk-208-2026.pdf"
    )
    body = (
        '<html><head><title>Direktori Peraturan DJBC</title></head><body><h1>Official directory</h1>'
        '<a href="index.html?page=about">About</a>'
        f'<a href="{category}">Peraturan Menteri Keuangan</a>'
        f'<a href="{detail}">PMK 208 tentang tarif bea masuk</a>'
        f'<a href="{original}">PMK 208/2026 original PDF</a></body></html>'
    ).encode()
    body_hash = hashlib.sha256(body).hexdigest()
    relative = "files/cached-homepage.html"
    local = collector.root / relative
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(body)
    current = collector.state["sources"].setdefault(source_row["id"], {})
    current.update(
        health="healthy",
        discovery_policy_hash="old-policy",
        cache={
            source_row["url"]: {
                "path": relative,
                "size": len(body),
                "content_hash": body_hash,
                "mime_type": "text/html",
            }
        },
        frontier={
            "https://peraturan.beacukai.go.id/index.html?page=about": {
                "parent_url": source_row["url"],
                "parent_hash": body_hash,
                "purpose": "detail",
                "attempt_count": 0,
                "discovered_at": env.clock[0],
            },
            category: {
                "parent_url": source_row["url"],
                "parent_hash": body_hash,
                "purpose": "detail",
                "attempt_count": 0,
                "discovered_at": env.clock[0],
            },
            detail: {
                "parent_url": source_row["url"],
                "parent_hash": body_hash,
                "purpose": "detail",
                "attempt_count": 0,
                "discovered_at": env.clock[0],
            },
        },
    )
    collector.save()

    resumed = env.restart(collector)
    frontier = resumed.state["sources"][source_row["id"]]["frontier"]
    assert "https://peraturan.beacukai.go.id/index.html?page=about" not in frontier
    assert frontier[category]["purpose"] == "catalog"
    assert frontier[detail]["purpose"] == "detail"
    assert "https://repository.beacukai.go.id/referensi/2026/10/a5d5a7b56c24-pmk-208-2026.pdf" in frontier
    assert frontier[
        "https://repository.beacukai.go.id/referensi/2026/10/a5d5a7b56c24-pmk-208-2026.pdf"
    ]["purpose"] == "document"
    policy_event = env.events(resumed, "discovery_policy_revalidated")[-1]
    assert policy_event["replay_status"] == "verified_cached_discovery"
    assert policy_event["archived_frontier"] == 1
    assert policy_event["network_requests"] == 0
    assert env.calls == []


RCEP_ROOT = (
    "https://www.enterprisesg.gov.sg/grow-your-business/go-global/international-agreements/"
    "free-trade-agreements/find-an-fta/rcep"
)
RCEP_FILES = (
    "https://www.enterprisesg.gov.sg/-/media/ESG/Files/Non-Financial-Assistance/"
    "For-Companies/Free-Trade-Agreements/RCEP/"
)


def rcep_source(**changes):
    return source(
        "sg-enterprisesg-rcep",
        host="www.enterprisesg.gov.sg",
        path=RCEP_ROOT.split("www.enterprisesg.gov.sg", 1)[1],
        **{"discovery_keywords": ["RCEP", "agreement", "origin", "tariff", "China"], **changes},
    )


def real_rcep_html():
    path = ROOT / "backend/tests/fixtures/discovery/sg-enterprisesg-rcep-20261020.html"
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == provenance["sha256"]
    return content


def test_real_rcep_html_filters_third_country_tariffs_before_link_cap():
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page

    configured = rcep_source()
    legacy_recipe = collection.recipe(configured)
    legacy_recipe.pop("exclude_patterns")
    before = discover_page(RCEP_ROOT, real_rcep_html(), "text/html", legacy_recipe)
    after = discover_page(RCEP_ROOT, real_rcep_html(), "text/html", collection.recipe(configured))
    assert len(before["links"]) == 32 and before["warnings"]
    assert not any("RCEP_China_" in link["url"] for link in before["links"])
    links = [link["url"] for link in after["links"]]
    assert len(links) == 17 and not after["warnings"]
    assert sum("RCEP_China_" in url for url in links) == 6
    assert RCEP_FILES + "RCEP_Annex_3A.pdf" in links
    assert RCEP_FILES + "RCEP_legal_text" in links
    assert not any(collection.scope_excluded(configured, url) for url in links)


@pytest.mark.parametrize(
    ("filename", "excluded"),
    [
        ("RCEP_Viet_Nam_Appendix_on_Tariff_Differentials.pdf", True),
        ("RCEP_Indonesia_Schedule_for_China.pdf", True),
        ("RCEP_Korea_Schedule_for_China.pdf", True),
        ("RCEP_Malaysia_Schedule_TRQ.pdf", True),
        ("RCEP_China_Schedule_for_ASEAN.pdf", False),
        ("RCEP_China_Appendix_on_Tariff_Differentials.pdf", False),
        ("RCEP_Singapore_Schedule.pdf", False),
        ("RCEP_Annex_3A.pdf", False),
        ("RCEP_Viet_Nam_Product_Specific_Rules.pdf", False),
        ("RCEP_legal_text", False),
    ],
)
def test_rcep_scope_exclusion_is_narrow_to_source_host_and_tariff_instrument(filename, excluded):
    configured = rcep_source()
    url = RCEP_FILES + filename
    assert collection.scope_excluded(configured, url) is excluded
    assert not collection.scope_excluded({**configured, "id": "sg-another-source"}, url)
    assert not collection.scope_excluded(configured, url.replace("www.enterprisesg.gov.sg", "www.mti.gov.sg"))
    assert "exclude_patterns" not in collection.recipe(source())


def seed_legacy_rcep_cache(env, monkeypatch):
    """Create real local legacy bytes/receipts using only the mocked HTTPS edge."""
    configured = rcep_source()
    collector = env.make([configured])
    env.responses[RCEP_ROOT] = real_rcep_html()
    original_recipe = collection.recipe

    def legacy_recipe(source):
        result = original_recipe(source)
        result.pop("exclude_patterns", None)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(collection, "recipe", legacy_recipe)
        root_receipt = collector.fetch(configured, RCEP_ROOT)
    bad = RCEP_FILES + "RCEP_Viet_Nam_Appendix_on_Tariff_Differentials.pdf"
    env.responses[bad] = pdf("legacy-viet-nam-appendix")
    bad_receipt = collector.fetch(configured, bad)
    root_asset = root_receipt["asset"]
    frontier = {
        link["url"]: {
            "parent_url": RCEP_ROOT,
            "parent_hash": root_asset["content_hash"],
            "parents": [{"url": RCEP_ROOT, "content_hash": root_asset["content_hash"]}],
            "purpose": link["purpose"],
            "title": link["title"],
            "attempt_count": 0,
            "discovered_at": env.clock[0],
        }
        for link in root_asset["discovery"]["links"]
    }
    frontier[bad].update(
        attempt_count=1,
        health=collection.observe(None, bad_receipt["outcome"], now=env.clock[0], interval_seconds=7 * 86400),
    )
    current = collection.observe(None, root_receipt["outcome"], now=env.clock[0], interval_seconds=86400)
    current.update(
        cache={RCEP_ROOT: root_asset, bad: bad_receipt["asset"]},
        frontier=frontier,
        root_next_at=env.clock[0] + 86400,
        next_at=env.clock[0] + 3600,
        retry_not_before=env.clock[0] + 3600,
        scheduling_status="budget_wait",
    )
    collector.state["sources"][configured["id"]] = current
    collector.save()
    return configured, collector, bad


def test_rcep_policy_replays_verified_cached_html_once_without_requests_or_quota_reset(
    environment, monkeypatch
):
    env = environment
    configured, first, bad = seed_legacy_rcep_cache(env, monkeypatch)
    before_state = deepcopy(first.state)
    before_requests = {p: p.read_bytes() for p in first.root.glob("requests/*/*.json")}
    before_calls = len(env.calls)
    before_bytes = {p: p.read_bytes() for p in first.root.glob("files/blobs/*/*/*/*")}
    resumed = env.make([configured], root=first.root, pool_path=first.pool_path)
    current = resumed.state["sources"][configured["id"]]
    assert current["candidate_policy"]["replay_status"] == "verified_cached_discovery"
    assert (
        current["candidate_policy"]["root_hash"]
        == before_state["sources"][configured["id"]]["cache"][RCEP_ROOT]["content_hash"]
    )
    assert len(env.calls) == before_calls
    assert {p: p.read_bytes() for p in before_requests} == before_requests
    assert {p: p.read_bytes() for p in before_bytes} == before_bytes
    assert resumed.binding == first.binding
    assert current["retry_not_before"] == before_state["sources"][configured["id"]]["retry_not_before"]
    assert current["next_at"] >= current["retry_not_before"]
    assert current["frontier"][bad]["attempt_count"] == 1
    assert current["frontier"][bad]["scope_excluded"]["version"] == collection.RCEP_POLICY_VERSION
    assert current["cache"][bad] == before_state["sources"][configured["id"]]["cache"][bad]
    assert RCEP_FILES + "RCEP_China_Schedule_for_ASEAN.pdf" in current["frontier"]
    assert RCEP_FILES + "RCEP_Annex_3A.pdf" in current["frontier"]
    assert not env.events(resumed, "mapped_resource") and not env.events(resumed, "content_version")
    event_counts = {
        kind: len(env.events(resumed, kind))
        for kind in ["candidate_scope_excluded", "candidate_policy_applied", "source_reference"]
    }
    assert event_counts["candidate_scope_excluded"] == 28
    assert (
        len(env.events(resumed, "candidate_policy_applied")) == 2
    )  # Fresh empty source, then legacy replay.
    again = env.make([configured], root=first.root, pool_path=first.pool_path)
    assert {kind: len(env.events(again, kind)) for kind in event_counts} == event_counts
    assert len(env.calls) == before_calls


def test_rcep_existing_excluded_frontier_is_retained_but_not_dispatched(environment, monkeypatch):
    env = environment
    configured, first, bad = seed_legacy_rcep_cache(env, monkeypatch)
    resumed = env.make([configured], root=first.root, pool_path=first.pool_path)
    current = resumed.state["sources"][configured["id"]]
    for url, item in current["frontier"].items():
        if not collection.scope_excluded(configured, url):
            env.responses[url] = pdf(url.rsplit("/", 1)[-1]) if item["purpose"] == "document" else html()
    calls_before = len(env.calls)
    env.clock[0] = current["retry_not_before"]
    resumed.tick()
    fresh_calls = [r["url"] for r in env.calls[calls_before:]]
    assert len(fresh_calls) == resumed.limits["requests_per_source_cycle"]
    assert all(not collection.scope_excluded(configured, url) for url in fresh_calls)
    assert any("RCEP_China_" in url or "RCEP_Annex_3A" in url for url in fresh_calls)
    latest = resumed.state["sources"][configured["id"]]
    assert latest["frontier"][bad]["attempt_count"] == 1
    assert collection.valid_cached(resumed.root, latest["cache"][bad])


def test_rcep_policy_does_not_rediscover_from_corrupt_cached_root(environment, monkeypatch):
    env = environment
    configured, first, _ = seed_legacy_rcep_cache(env, monkeypatch)
    root_asset = first.state["sources"][configured["id"]]["cache"][RCEP_ROOT]
    path = first.root / root_asset["path"]
    path.chmod(0o600)
    path.write_bytes(b"damaged fixture")
    requests_before = {p: p.read_bytes() for p in first.root.glob("requests/*/*.json")}
    calls_before = len(env.calls)
    resumed = env.make([configured], root=first.root, pool_path=first.pool_path)
    current = resumed.state["sources"][configured["id"]]
    assert current["candidate_policy"]["replay_status"] == "cached_integrity_failed"
    assert not any("RCEP_China_" in url for url in current["frontier"])
    assert not env.events(resumed, "source_reference")
    assert len(env.calls) == calls_before
    assert {p: p.read_bytes() for p in requests_before} == requests_before


def test_dynamic_registry_preserves_versions_quota_and_last_good_revision(environment):
    env = environment
    collector = env.make()
    env.responses[source()["url"]] = html()
    collector.tick()
    previous = deepcopy(collector.state["sources"])
    requests = deepcopy(collector.requests)
    first_digest = collector.binding["pool_sha256"]
    pool = json.loads(collector.pool_path.read_text())
    pool["sources"].append(source("sg-new", path="/new"))
    collector.pool_path.write_text(json.dumps(pool))
    collector.reload_sources()
    assert len(collector.sources) == 2
    assert collector.state["sources"] == previous
    assert collector.requests == requests
    assert first_digest != collector.binding["pool_sha256"]
    assert {r["pool_sha256"] for r in collector.requests.values()} == {first_digest}

    pool["sources"] = [pool["sources"][1]]
    collector.pool_path.write_text(json.dumps(pool))
    collector.reload_sources()
    assert [s["id"] for s in collector.sources] == ["sg-new"]
    assert collector.state["sources"] == previous
    saved_digest = collector.binding["pool_sha256"]
    collector.pool_path.write_text('{"sources":')
    collector.reload_sources()
    collector.reload_sources()
    assert collector.binding["pool_sha256"] == saved_digest
    assert len(env.events(collector, "source_registry_rejected")) == 1
    resumed = env.restart(collector)
    assert resumed.binding["pool_sha256"] == saved_digest
    assert resumed.requests == requests
    assert resumed.state["sources"] == previous


def test_registry_source_identity_cannot_move_between_countries(environment):
    env = environment
    collector = env.make()
    pool = json.loads(collector.pool_path.read_text())
    pool["sources"] = [source(country="ID", host="one.go.id")]
    collector.pool_path.write_text(json.dumps(pool))
    collector.reload_sources()
    assert collector.sources[0]["country"] == "SG"
    assert collector.state["registry_error"]["code"] == "invalid_source_registry"


def test_proxy_required_source_never_falls_back_to_direct_transport(environment, monkeypatch):
    collector = environment.make()
    configured = {**collector.sources[0], "transport": "proxy"}
    for key in ("COLLECTION_PROXY_URL", "COLLECTION_PROXY_HOSTS", "COLLECTION_DOH_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(collection.DownloadError, match="Required collection proxy"):
        with collector.slot(configured["allowed_hosts"][0], configured):
            pytest.fail("unconfigured proxy allowed a connection")


def test_malformed_nested_registry_keeps_last_good_revision(environment):
    collector = environment.make()
    revision = collector.binding["pool_sha256"]
    pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    pool["source_relations"] = [None]
    for invalid in (json.dumps(pool), "[" * 2000 + "]" * 2000):
        collector.pool_path.write_text(invalid)
        collector.reload_sources()
        assert collector.binding["pool_sha256"] == revision
        assert collector.state["registry_error"]["code"] == "invalid_source_registry"
    assert environment.restart(collector).binding["pool_sha256"] == revision
