"""Kill a real collector during a controlled download, retaining its durable evidence."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time

from app.configuration.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "scripts"))
from official_source_supervisor_recovery import (  # noqa: E402
    identity,
    write,
)

CHILD = r"""
from datetime import UTC, datetime
import hashlib, json, os, sys, time
from pathlib import Path
from urllib.parse import urlsplit

repository, project, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(repository / "scripts"), str(repository / "backend")]
import unattended_collection as collection
from app.bootstrap import Service
from official_source_supervisor_recovery import identity, lock, write

base = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
clock = [base if mode == "crash" else base + 3601]
class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(clock[0], tz)

collection.ROOT = project
collection.datetime = Clock
collection.time.time = lambda: clock[0]
network_attempts = []
def offline_audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.bind"}:
        network_attempts.append(event)
        raise AssertionError("controlled continuity test forbids real network and models")
sys.addaudithook(offline_audit)
def forbidden_service(*args, **kwargs):
    raise AssertionError("raw recovery must not construct the application/model service")
Service.__init__ = forbidden_service

content_v1 = b"<html><head><title>Food guidance</title></head><body><h1>Food import rules</h1><p>Official food requirements version one.</p></body></html>"
content_v2 = b"<html><head><title>Food guidance</title></head><body><h1>Food import rules updated</h1><p>Official food requirements version two." + b" " * 32768 + b"</p></body></html>"
calls = []
root = project / "raw"
def download(url, **options):
    # Keep the production URL validator and host/concurrency guard. Only the
    # transport is replaced; no localhost/public-IP exceptions are introduced.
    with options["connection_slot"](urlsplit(url).hostname):
        calls.append(url)
        content = content_v1 if mode == "crash" and len(calls) == 1 else content_v2
        def chunks():
            if mode == "crash" and len(calls) == 2:
                yield content[:16384]
                write(project / "download-entered.json", {
                    "identity": identity(os.getpid()), "url": url,
                    "phase": "streaming-before-response-completion", "received_bytes": 16384,
                })
                while True:
                    time.sleep(.02)
            yield content
        blob = options["consume"](chunks())
        return {"url": url, "blob": blob, "mime_type": "text/html", "response_status": 200,
                "etag": '"v1"' if content == content_v1 else '"v2"'}
collection._download = download
limits = {"workers": 1, "sources_per_tick": 1, "requests_per_source_cycle": 1,
          "max_response_bytes": 128 * 1024, "daily_source_bytes": 1024**2,
          "daily_bytes": 2 * 1024**2, "min_free_bytes": 1, "tick_seconds": 1}
root.mkdir(parents=True, exist_ok=True)
with lock(root / ".collector.lock"):
    collector = collection.Collector(project / "pool.json", root, "SG", limits=limits)
    if mode == "crash":
        assert collector.tick() == ["sg-continuity"]
        write(project / "seed.json", {"state": collector.state, "identity": identity(os.getpid())})
        clock[0] += 3601
        collector.tick()  # Parent kills this process while consume() is blocked.
        raise AssertionError("blocked transport unexpectedly returned")
    source = collector.state["sources"]["sg-continuity"]
    assert source["last_observation"]["error_type"] == "InterruptedGET"
    assert collector.state["active_sources"] == []
    assert collector.tick() == [] and calls == []  # Persisted backoff still applies.
    before = {"state": json.loads(json.dumps(collector.state)),
              "charged_bytes": sum(row["charged_bytes"] for row in collector.requests.values())}
    clock[0] = source["next_at"] + 1
    assert collector.tick() == ["sg-continuity"]
    rows = list(collector.requests.values())
    versions = [json.loads(path.read_text()) for path in (root / "events").glob("*/*.json")]
    write(project / "recovered.json", {
        "identity": identity(os.getpid()), "before_progress": before,
        "state": collector.state, "requests": rows, "calls": calls,
        "network_attempts": network_attempts,
        "versions": [item for item in versions if item["kind"] == "content_version"],
        "v1_hash": hashlib.sha256(content_v1).hexdigest(),
        "v2_hash": hashlib.sha256(content_v2).hexdigest(),
    })
"""


def test_inflight_download_kill_preserves_intent_originals_and_advances_after_restart(tmp_path):
    project = tmp_path / "continuity-project"
    project.mkdir()
    source = {
        "id": "sg-continuity",
        "country": "SG",
        "url": "https://one.gov.sg/food",
        "allowed_hosts": ["one.gov.sg"],
        "priority": 80,
        "interval_seconds": 3600,
        "kind": "guidance",
        "topics": ["food"],
        "discovery_keywords": ["food"],
    }
    (project / "pool.json").write_text(
        json.dumps({"protocol": "aitea-official-source-pool-v1", "sources": [source]})
    )
    processes = []
    launch_identities = []

    def launch(mode):
        log = (project / f"{mode}.log").open("wb")
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(ROOT), str(project), mode],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        processes.append(child)
        launch_identities.append(identity(child.pid))
        return child

    first = launch("crash")
    try:
        deadline = time.monotonic() + 15
        while not (project / "download-entered.json").exists():
            assert first.poll() is None, (project / "crash.log").read_text()
            assert time.monotonic() < deadline, "child did not enter controlled streaming download"
            time.sleep(0.02)
        raw = project / "raw"
        in_flight = json.loads((project / "download-entered.json").read_text())
        assert in_flight["identity"]["pid"] == first.pid
        assert identity(first.pid) == in_flight["identity"]
        assert json.loads((raw / "state.json").read_text())["active_sources"] == ["sg-continuity"]
        requests = list((raw / "requests").glob("*/*.json"))
        issued = [path for path in requests if json.loads(path.read_text())["status"] == "issued"]
        assert len(requests) == 2 and len(issued) == 1
        issued_path = issued[0]
        issued_bytes = issued_path.read_bytes()
        issued_row = json.loads(issued_bytes)
        assert issued_row["charged_bytes"] == 128 * 1024
        staging = list((raw / "files/.staging").glob("*.part"))
        assert staging  # Buffered partial bytes are not yet a completed CAS original.
        assert all(path.stat().st_size <= in_flight["received_bytes"] for path in staging)
        baseline = {
            str(path.relative_to(raw)): path.read_bytes()
            for category in ("requests", "events", "files/blobs")
            for path in (raw / category).rglob("*")
            if path.is_file()
        }
        first.kill()
        assert first.wait(timeout=5) == -signal.SIGKILL
        assert identity(first.pid) is None
        assert issued_path.read_bytes() == issued_bytes
        assert json.loads((raw / "state.json").read_text())["active_sources"] == ["sg-continuity"]

        replacement = launch("recover")
        assert replacement.wait(timeout=15) == 0, (project / "recover.log").read_text()
        recovered = json.loads((project / "recovered.json").read_text())
        assert recovered["network_attempts"] == []
        assert recovered["calls"] == [source["url"]]
        previous = recovered["before_progress"]["state"]["sources"]["sg-continuity"]
        assert previous["last_observation"]["error_type"] == "InterruptedGET"
        assert recovered["before_progress"]["charged_bytes"] >= issued_row["charged_bytes"]
        current = recovered["state"]["sources"]["sg-continuity"]
        assert current["health"] == "healthy"
        assert len(recovered["requests"]) == 3
        assert sum(row["status"] == "issued" for row in recovered["requests"]) == 1
        assert issued_path.read_bytes() == issued_bytes
        for relative, data in baseline.items():
            assert (raw / relative).read_bytes() == data
        assert any(
            event["previous_content_hash"] == recovered["v1_hash"]
            and event["content_hash"] == recovered["v2_hash"]
            for event in recovered["versions"]
        )
        asset = current["cache"][source["url"]]
        assert hashlib.sha256((raw / asset["path"]).read_bytes()).hexdigest() == recovered["v2_hash"]
        write(
            project / "proof.json",
            {
                "protocol": "aitea-controlled-inflight-continuity-v1",
                "status": "passed",
                "transport": "controlled mock streaming; public URL validation unchanged",
                "in_flight_identity": in_flight["identity"],
                "recovered_identity": recovered["identity"],
                "launcher_identities": launch_identities,
                "preserved_files": len(baseline),
                "issued_intent_sha256": hashlib.sha256(issued_bytes).hexdigest(),
                "issued_quota_still_reserved": issued_row["charged_bytes"],
                "new_completed_request": True,
                "old_and_new_version_relation": True,
                "external_network_attempts": recovered["network_attempts"],
                "model_calls": 0,
            },
        )
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
