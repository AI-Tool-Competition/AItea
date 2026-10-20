"""Real child processes/SQLite checkpoints; local receipts replace paid OCR."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from frozen_parse_supervisor import (  # noqa: E402
    acquire_parse_lock,
    classify_document,
    decide,
    inspect_scope,
)
from process_frozen_parse import digest, frozen_inputs, parse_args  # noqa: E402


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("Timed out waiting for the isolated child checkpoint")
        time.sleep(0.02)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


FAKE_CHILD = """
import json, os, signal, sqlite3, sys, time
from pathlib import Path
sys.path[:0] = [SCRIPTS, BACKEND]
from embedding_supervisor import persist
from app.embedding_admission import process_identity
from process_frozen_parse import parse_args, frozen_inputs, selected_documents, digest, inspect_frozen_document
from frozen_parse_supervisor import acquire_parse_lock, classify_document, verify_child_binding

args = parse_args()
owner_lock = acquire_parse_lock(args.snapshot, args.supervisor_lock_fd)
guard = verify_child_binding(args, Path(__file__))
report, initial = frozen_inputs(args.snapshot, args.data_dir)
selected = selected_documents(args, initial)
output = args.snapshot / "runs" / args.run_id
output.mkdir(parents=True)
control = json.loads((args.snapshot / "control.json").read_text())
def event(name, value):
    with (args.snapshot / name).open("a") as stream:
        stream.write(json.dumps(value) + "\\n")
        stream.flush()
        os.fsync(stream.fileno())
event("starts.jsonl", {"pid": os.getpid(), "identity": process_identity(os.getpid()),
    "run_id": args.run_id, "at": time.time()})
signal.signal(signal.SIGTERM, lambda *_: event("signals.jsonl", {"signal": "TERM"}))
persist(output / "run-input.json", {
    "run_id": args.run_id, "selected_ids": selected, "owner_id": report["owner_id"],
    "models_file_sha256": __import__("hashlib").sha256(args.models_env.read_bytes()).hexdigest(),
})
claims = 0
for identifier in selected:
    row, manifest = inspect_frozen_document(args.data_dir, initial, identifier,
        successful_pages=guard.get(identifier, ()))
    if classify_document(row, manifest, time.time())["status"] != "ready":
        continue
    page = next(page for page in manifest["pages"] if page["status"] == "pending_ocr")
    page["ocr"] = {"attempts": 1, "request_id": args.run_id}
    with sqlite3.connect(args.data_dir / "tea.sqlite3") as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), identifier))
        conn.execute("UPDATE processing_jobs SET status='processing',lease_token=?,lease_until=? "
            "WHERE document_id=?", (args.run_id, time.time() + 60, identifier))
    event("payments.jsonl", {"document_id": identifier, "page": page["page"],
        "run_id": args.run_id, "at": time.time()})
    claims += 1
    if control.get("mode") == "unknown":
        os._exit(9)
    time.sleep(control.get("sleep", 0.15))
    quality = control.get("mode") == "quality" and identifier == "alpha"
    page.update(status="ocr_complete", text="saved successful transcription")
    page["ocr"].update(complete=not quality, text=page["text"], finish_reason="stop", http_status=200)
    manifest["quality"] = {"complete": not quality}
    with sqlite3.connect(args.data_dir / "tea.sqlite3") as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), identifier))
        conn.execute("UPDATE processing_jobs SET phase=?,status='queued',lease_token=NULL,lease_until=NULL "
            "WHERE document_id=?", ("parse" if quality else "embed", identifier))
    break
persist(output / "report.json", {
    "run_id": args.run_id, "selected_ids_sha256": digest(selected), "workers_running": False,
    "initial_source_and_metadata_guards_preserved": True, "initial_successful_pages_unchanged": True,
    "embedding_requests": 0, "download_requests": 0, "claims": claims, "blockers": [],
})
event("ends.jsonl", {"run_id": args.run_id, "at": time.time()})
"""

DRIVER = """
import json, os, signal, sys, time
from pathlib import Path
sys.path[:0] = [SCRIPTS, BACKEND]
from process_frozen_parse import parse_args
import frozen_parse_supervisor as supervisor
args = parse_args()
original = supervisor.subprocess.Popen
def spawn(*a, **kw):
    child = original(*a, **kw)
    control = json.loads((args.snapshot / "control.json").read_text())
    marker = args.snapshot / "spawn-gap-crashed"
    if control.get("kill_after_spawn_once") and not marker.exists():
        marker.touch()
        os.kill(os.getpid(), signal.SIGKILL)
    return child
supervisor.subprocess.Popen = spawn
raise SystemExit(supervisor.supervise(args, Path(CHILD)))
"""


@pytest.fixture
def scope(tmp_path):
    data, snapshot = tmp_path / "data", tmp_path / "snapshot"
    data.mkdir()
    snapshot.mkdir()
    models = tmp_path / "models.env"
    models.write_text("")
    ids = ["alpha", "beta"]
    guards = []
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        conn.executescript("""
            CREATE TABLE documents(id TEXT PRIMARY KEY,owner_id TEXT,version INTEGER,content_hash TEXT,
                storage_path TEXT,size INTEGER,role TEXT,source_url TEXT,family_id TEXT,case_id TEXT,
                parent_id TEXT,title TEXT,filename TEXT,mime_type TEXT,manifest TEXT);
            CREATE TABLE document_metadata(document_id TEXT PRIMARY KEY,payload TEXT,revision INTEGER);
            CREATE TABLE processing_jobs(document_id TEXT PRIMARY KEY,phase TEXT,status TEXT,
                available_at REAL,lease_token TEXT,lease_until REAL);
        """)
        for identifier in [*ids, "independent-embedding"]:
            original = data / (identifier + ".pdf")
            original.write_bytes(("Original " + identifier).encode())
            source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            manifest = {
                "source": {"immutable": identifier},
                "quality": {"complete": False},
                "pages": [
                    {"page": 1, "status": "native", "text": "preserved native", "source_hash": source_hash},
                    {"page": 2, "status": "pending_ocr", "source_hash": source_hash},
                ],
            }
            row = dict(
                id=identifier,
                owner_id="shared",
                version=1,
                content_hash=source_hash,
                storage_path=str(original),
                size=original.stat().st_size,
                role="primary",
                source_url="https://example.gov/legal/" + identifier,
                family_id=identifier,
                case_id=None,
                parent_id=None,
                title=identifier,
                filename=original.name,
                mime_type="application/pdf",
            )
            conn.execute(
                "INSERT INTO documents VALUES(" + ",".join("?" for _ in range(15)) + ")",
                (*row.values(), json.dumps(manifest)),
            )
            conn.execute("INSERT INTO document_metadata VALUES(?,?,?)", (identifier, "{}", 1))
            phase = "embed" if identifier == "independent-embedding" else "parse"
            conn.execute(
                "INSERT INTO processing_jobs VALUES(?,?,?,0,NULL,NULL)", (identifier, phase, "queued")
            )
            if identifier in ids:
                guards.append(
                    {
                        **row,
                        "metadata_revision": 1,
                        "metadata_sha256": hashlib.sha256(b"{}").hexdigest(),
                        "protected_provenance": {"source": digest(manifest["source"])},
                        "quality": manifest["quality"],
                        "pages": [
                            {"page": page["page"], "validated_status": page["status"], "sha256": digest(page)}
                            for page in manifest["pages"]
                        ],
                    }
                )
    raw_ids, raw_docs = json.dumps(ids).encode(), json.dumps(guards).encode()
    (snapshot / "initial-parse-ids.json").write_bytes(raw_ids)
    (snapshot / "initial-parse-documents.json").write_bytes(raw_docs)
    (snapshot / "initial-report.json").write_text(
        json.dumps(
            {
                "initial_id_list_sha256": hashlib.sha256(raw_ids).hexdigest(),
                "initial_documents_sha256": hashlib.sha256(raw_docs).hexdigest(),
                "source_data_dir": str(data),
                "owner_id": "shared",
                "initial_parse_queued_documents": 2,
            }
        )
    )
    (snapshot / "control.json").write_text(json.dumps({"sleep": 0.15}))
    child, driver = tmp_path / "child.py", tmp_path / "driver.py"
    constants = f"SCRIPTS={str(ROOT / 'scripts')!r}\nBACKEND={str(ROOT / 'backend')!r}\n"
    child.write_text(constants + FAKE_CHILD)
    driver.write_text(constants + f"CHILD={str(child)!r}\n" + DRIVER)
    options = [
        "--data-dir",
        str(data),
        "--snapshot",
        str(snapshot),
        "--models-env",
        str(models),
        "--run-id",
        "test",
        "--supervise",
        "--claim-limit",
        "1",
        "--seconds",
        "30",
    ]
    processes = []

    def start(*extra, real_runner=False):
        log = (tmp_path / f"supervisor-{len(processes)}.log").open("w")
        command = [sys.executable, str(ROOT / "scripts/process_frozen_parse.py" if real_runner else driver)]
        process = subprocess.Popen([*command, *options, *extra], stdout=log, stderr=subprocess.STDOUT)
        log.close()
        processes.append(process)
        return process

    yield data, snapshot, start, parse_args(options)
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
    # Only this fixture's recorded children; never use process-name matching.
    from app.embedding_admission import process_identity

    for row in records(snapshot / "starts.jsonl"):
        if process_identity(row["pid"]) != row["identity"]:
            continue
        try:
            os.kill(row["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass


def state(snapshot):
    return read_json(snapshot / "supervisor-state.json")


def test_two_windows_drain_before_relaunch_and_preserve_unselected_embedding(scope):
    data, snapshot, start, _ = scope
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        before = conn.execute("SELECT * FROM documents WHERE id='independent-embedding'").fetchone()
        job = conn.execute(
            "SELECT * FROM processing_jobs WHERE document_id='independent-embedding'"
        ).fetchone()
    process = start()
    assert process.wait(12) == 0
    assert state(snapshot)["status"] == "completed"
    assert state(snapshot)["counts"]["completed"] == 2
    paid = records(snapshot / "payments.jsonl")
    assert [(row["document_id"], row["page"]) for row in paid] == [("alpha", 2), ("beta", 2)]
    starts, ends = records(snapshot / "starts.jsonl"), records(snapshot / "ends.jsonl")
    assert len({row["run_id"] for row in starts}) == 2
    assert starts[1]["at"] >= ends[0]["at"]
    assert start().wait(10) == 0
    assert records(snapshot / "payments.jsonl") == paid
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        assert conn.execute("SELECT * FROM documents WHERE id='independent-embedding'").fetchone() == before
        assert (
            conn.execute("SELECT * FROM processing_jobs WHERE document_id='independent-embedding'").fetchone()
            == job
        )
    success = read_json(snapshot / "supervisor-success-pages.json")["documents"]
    assert all([page["page"] for page in success[key]] == [1, 2] for key in ("alpha", "beta"))


@pytest.mark.parametrize("spawn_gap", [False, True])
def test_killed_parent_restart_waits_for_inherited_child_and_never_repays(scope, spawn_gap):
    _, snapshot, start, _ = scope
    (snapshot / "control.json").write_text(json.dumps({"sleep": 1, "kill_after_spawn_once": spawn_gap}))
    parent = start()
    wait_for(lambda: len(records(snapshot / "payments.jsonl")) == 1)
    if spawn_gap:
        assert parent.wait(5) == -signal.SIGKILL
        assert state(snapshot)["child_pid"] is None
    else:
        parent.kill()
        parent.wait(5)
    second = start()
    wait_for(lambda: state(snapshot).get("status") == "waiting_child")
    assert len(records(snapshot / "starts.jsonl")) == 1
    assert second.wait(12) == 0
    assert [row["document_id"] for row in records(snapshot / "payments.jsonl")] == ["alpha", "beta"]


def test_duplicate_supervisor_does_not_replace_live_state(scope):
    _, snapshot, start, _ = scope
    (snapshot / "control.json").write_text(json.dumps({"sleep": 1}))
    first = start()
    wait_for(lambda: len(records(snapshot / "payments.jsonl")) == 1)
    second = start()
    assert second.wait(5) == 2
    assert state(snapshot)["supervisor_pid"] == first.pid
    assert first.wait(12) == 0
    assert len(records(snapshot / "payments.jsonl")) == 2


def test_unknown_child_result_remains_blocked_after_restart(scope):
    data, snapshot, start, _ = scope
    (snapshot / "control.json").write_text(json.dumps({"mode": "unknown"}))
    assert start().wait(10) == 2
    assert state(snapshot)["status"] == "blocked_unknown"
    assert state(snapshot)["reason"] == "missing_terminal_window_evidence"
    paid = records(snapshot / "payments.jsonl")
    assert len(paid) == 1
    # Even a later complete-looking database cannot substitute for the missing
    # window receipt: the supervisor does not infer payment outcomes from counts.
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        for identifier, raw in conn.execute("SELECT id,manifest FROM documents").fetchall():
            manifest = json.loads(raw)
            manifest["quality"] = {"complete": True}
            for page in manifest["pages"]:
                if page["status"] == "native":
                    continue
                page.update(status="ocr_complete", text="late saved text")
                page["ocr"] = {"text": page["text"], "complete": True, "finish_reason": "stop"}
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), identifier))
    assert start().wait(10) == 2
    assert state(snapshot)["status"] == "blocked_unknown"
    assert state(snapshot)["reason"] == "missing_terminal_window_evidence"
    assert records(snapshot / "payments.jsonl") == paid


def test_corrupt_launch_ledger_is_preserved_and_not_reset(scope):
    _, snapshot, start, _ = scope
    original = b'{"generation": 1, "active_run_id": "unconfirmed'
    (snapshot / "supervisor-state.json").write_bytes(original)
    (snapshot / "supervisor-binding.json").write_text("{}")
    (snapshot / "supervisor-success-pages.json").write_text("{}")
    assert start().wait(10) == 2
    assert (snapshot / "supervisor-state.json").read_bytes() == original
    assert read_json(snapshot / "supervisor-startup-error.json")["status"] == "failed_configuration"
    assert not records(snapshot / "starts.jsonl")


@pytest.mark.parametrize(
    "missing", ["supervisor-state.json", "supervisor-binding.json", "supervisor-success-pages.json"]
)
def test_lost_supervisor_ledger_member_never_reinitializes_paid_scope(scope, missing):
    _, snapshot, start, _ = scope
    assert start().wait(12) == 0
    paid = records(snapshot / "payments.jsonl")
    (snapshot / missing).unlink()
    assert start().wait(10) == 2
    assert start().wait(10) == 2
    assert not (snapshot / missing).exists()
    assert read_json(snapshot / "supervisor-startup-error.json")["status"] == "failed_configuration"
    assert records(snapshot / "payments.jsonl") == paid


def test_saved_quality_failure_is_held_while_other_fixed_documents_finish(scope):
    _, snapshot, start, _ = scope
    (snapshot / "control.json").write_text(json.dumps({"mode": "quality"}))
    assert start().wait(10) == 2
    current = state(snapshot)
    assert current["status"] == "blocked_quality"
    assert current["counts"] == dict(
        completed=1, ready=0, waiting=0, blocked_unknown=0, blocked_quality=1, blocked_inputs=0
    )
    assert current["held_documents"]["alpha"]["reason"] == "saved_text_requires_quality_review"
    assert [row["document_id"] for row in records(snapshot / "payments.jsonl")] == ["alpha", "beta"]
    assert start().wait(10) == 2
    assert len(records(snapshot / "payments.jsonl")) == 2


def test_sigterm_drains_child_without_launching_next_window(scope):
    _, snapshot, start, _ = scope
    (snapshot / "control.json").write_text(json.dumps({"sleep": 0.7}))
    process = start()
    wait_for(lambda: len(records(snapshot / "payments.jsonl")) == 1)
    process.terminate()
    assert process.wait(10) == 0
    assert state(snapshot)["status"] == "stopped"
    assert len(records(snapshot / "ends.jsonl")) == 1
    assert len(records(snapshot / "starts.jsonl")) == 1
    assert start().wait(10) == 0
    assert len(records(snapshot / "payments.jsonl")) == 2


def test_provider_cooldown_and_available_at_are_both_respected(scope):
    data, snapshot, start, _ = scope
    available_at, provider_at = time.time() + 0.5, time.time() + 1.1
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        conn.execute("UPDATE processing_jobs SET available_at=? WHERE phase='parse'", (available_at,))
    (snapshot / "provider-cooldown.json").write_text(json.dumps({"retry_not_before": provider_at}))
    process = start()
    wait_for(lambda: state(snapshot).get("status") == "cooldown")
    assert state(snapshot)["next_run_at"] == provider_at
    assert not records(snapshot / "starts.jsonl")
    assert process.wait(10) == 0
    assert records(snapshot / "payments.jsonl")[0]["at"] >= provider_at


def test_real_runner_bad_model_config_is_observable_before_service_or_requests(scope):
    _, snapshot, start, _ = scope
    assert start(real_runner=True).wait(12) == 2
    assert state(snapshot)["status"] == "failed_configuration"
    assert state(snapshot)["reason"] == "invalid_ocr_configuration"
    assert not records(snapshot / "payments.jsonl")


@pytest.mark.parametrize("damage", ["source", "metadata", "provenance", "native", "binding"])
def test_frozen_guard_damage_fails_before_any_child(scope, damage):
    data, snapshot, start, _ = scope
    if damage == "source":
        (data / "alpha.pdf").write_bytes(b"Changed original")
    elif damage == "binding":
        (snapshot / "initial-parse-ids.json").write_text('["beta"]')
    else:
        with sqlite3.connect(data / "tea.sqlite3") as conn:
            if damage == "metadata":
                conn.execute("UPDATE document_metadata SET payload='changed' WHERE document_id='alpha'")
            else:
                manifest = json.loads(
                    conn.execute("SELECT manifest FROM documents WHERE id='alpha'").fetchone()[0]
                )
                if damage == "provenance":
                    manifest["source"] = {"changed": True}
                else:
                    manifest["pages"][0]["text"] = "changed previously successful page"
                conn.execute("UPDATE documents SET manifest=? WHERE id='alpha'", (json.dumps(manifest),))
    assert start().wait(10) == 2
    assert state(snapshot)["status"] == "failed_configuration"
    assert not records(snapshot / "starts.jsonl")


def test_newly_successful_pages_are_guarded_across_restarts(scope):
    data, snapshot, start, _ = scope
    assert start().wait(12) == 0
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        manifest = json.loads(conn.execute("SELECT manifest FROM documents WHERE id='alpha'").fetchone()[0])
        manifest["pages"][1]["text"] = "changed later"
        conn.execute("UPDATE documents SET manifest=? WHERE id='alpha'", (json.dumps(manifest),))
    assert start().wait(10) == 2
    assert state(snapshot)["status"] == "failed_configuration"
    assert len(records(snapshot / "payments.jsonl")) == 2


def test_scope_inspection_is_readonly_and_accumulates_only_validated_pages(scope):
    data, snapshot, _, args = scope
    _, initial = frozen_inputs(snapshot, data)
    before = (data / "tea.sqlite3").read_bytes()
    observed, guard = inspect_scope(args, initial, list(initial), {})
    assert (data / "tea.sqlite3").read_bytes() == before
    assert all(item["status"] == "ready" for item in observed.values())
    assert all(len(pages) == 1 for pages in guard.values())
    with closing(sqlite3.connect(data / "tea.sqlite3")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_lock_descriptor_must_belong_to_same_snapshot(scope, tmp_path):
    _, snapshot, _, _ = scope
    wrong = tmp_path / "different"
    wrong.mkdir()
    (snapshot / ".parse-owner.lock").touch()
    with acquire_parse_lock(wrong) as handle:
        with pytest.raises(ValueError, match="does not belong"):
            acquire_parse_lock(snapshot, os.dup(handle.fileno()))


def test_page_retry_deadline_and_unknown_receipt_are_not_reinterpreted():
    row = dict(phase="parse", job_status="queued", available_at=0, lease_until=None)
    manifest = {
        "quality": {"complete": False},
        "pages": [
            {
                "page": 1,
                "status": "pending_ocr",
                "error": "rate limited",
                "ocr": {"http_status": 429, "retry_not_before": 1234, "error_response": {"blob": {}}},
            }
        ],
    }
    result = classify_document(row, manifest, 100)
    assert result["status"] == "waiting" and result["due_at"] == 1234
    held = deepcopy(manifest)
    held["pages"][0]["ocr"]["http_status"] = 504
    assert classify_document(row, held, 100)["status"] == "blocked_unknown"
    decision = decide({"delayed": result, "unknown": {"status": "blocked_unknown"}}, 100)
    assert decision["status"] == "cooldown" and decision["next_run_at"] == 1234


@pytest.mark.parametrize("quality_failure", [False, True])
def test_actual_runner_service_and_bounded_pdf_chain_across_windows(tmp_path, quality_failure):
    """Only HTTP is replaced; real leases, CAS pixels, OCR validation and CLI run."""
    from app.db import now
    from app.ingestion_quality import pdf_page_status, text_complete
    from app.service import Service
    from test_ingestion_quality import make_pdf
    from test_pdf_ocr_reuse import register

    data = tmp_path / "library"
    snapshot = tmp_path / "scope"
    snapshot.mkdir()
    service = Service(data)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("owner", "owner", None, None, 1, now()))
    docs = [
        register(service, make_pdf([("Pasal 1\nSaved original text.", True)] * 5), key="long"),
        register(service, make_pdf([("Pasal 1\nOther original text.", True)]), key="short"),
    ]
    guards = []
    with closing(service.db.connect()) as conn:
        for doc in docs:
            row = dict(
                conn.execute(
                    "SELECT d.*,m.payload metadata,m.revision metadata_revision FROM documents d "
                    "LEFT JOIN document_metadata m ON m.document_id=d.id WHERE d.id=?",
                    (doc["id"],),
                ).fetchone()
            )
            manifest = json.loads(row["manifest"])
            guards.append(
                {
                    **{
                        key: row[key]
                        for key in (
                            "id",
                            "owner_id",
                            "version",
                            "content_hash",
                            "storage_path",
                            "size",
                            "role",
                            "source_url",
                            "family_id",
                            "case_id",
                            "parent_id",
                            "title",
                            "filename",
                            "mime_type",
                            "metadata_revision",
                        )
                    },
                    "metadata_sha256": hashlib.sha256((row["metadata"] or "").encode()).hexdigest(),
                    "protected_provenance": {"source_hash": digest(manifest.get("source_hash"))},
                    "quality": manifest["quality"],
                    "pages": [
                        {
                            "page": page["page"],
                            "validated_status": pdf_page_status(page),
                            "sha256": digest(page),
                        }
                        for page in manifest["pages"]
                    ],
                }
            )
    ids_raw = json.dumps([doc["id"] for doc in docs]).encode()
    docs_raw = json.dumps(guards).encode()
    (snapshot / "initial-parse-ids.json").write_bytes(ids_raw)
    (snapshot / "initial-parse-documents.json").write_bytes(docs_raw)
    (snapshot / "initial-report.json").write_text(
        json.dumps(
            {
                "owner_id": "owner",
                "source_data_dir": str(data),
                "initial_parse_queued_documents": 2,
                "initial_id_list_sha256": hashlib.sha256(ids_raw).hexdigest(),
                "initial_documents_sha256": hashlib.sha256(docs_raw).hexdigest(),
            }
        )
    )
    models = tmp_path / "models.env"
    models.write_text(
        "LLM_BASE_URL=https://provider.invalid/v1\nLLM_API_KEY=offline-test-key\nLLM_MODEL=test\n"
    )
    driver = tmp_path / "actual_cli.py"
    driver.write_text(
        f"SCRIPTS={str(ROOT / 'scripts')!r}\nBACKEND={str(ROOT / 'backend')!r}\n"
        f"SNAPSHOT={str(snapshot)!r}\nQUALITY_DOCUMENT={docs[1]['id'] if quality_failure else None!r}\n"
        + """
import json, os, sys, time
from pathlib import Path
sys.path[:0] = [SCRIPTS, BACKEND]
import httpx
from app import vision
import process_frozen_parse as runner
original_client = httpx.Client
def local_provider(request):
    scope = runner.SCOPE.get()
    failed_quality = scope["document_id"] == QUALITY_DOCUMENT
    with (Path(SNAPSHOT) / "http-local.jsonl").open("a") as stream:
        stream.write(json.dumps({**scope, "at": time.time()}) + "\\n")
        stream.flush()
        os.fsync(stream.fileno())
    content = json.dumps({"text": "Pasal 1\\nSaved original text.", "printed_page": None,
        "is_blank": False, "complete": not failed_quality, "uncertain_spans": []})
    return httpx.Response(200, json={"model": "local-test", "choices": [
        {"finish_reason": "stop", "message": {"content": content}}]})
vision.httpx.Client = lambda **kw: original_client(transport=httpx.MockTransport(local_provider), **kw)
runner.__file__ = __file__
if __name__ == "__main__":
    raise SystemExit(runner.main())
"""
    )
    command = [
        sys.executable,
        str(driver),
        "--data-dir",
        str(data),
        "--snapshot",
        str(snapshot),
        "--models-env",
        str(models),
        "--run-id",
        "actual",
        "--supervise",
        "--seconds",
        "30",
        "--claim-limit",
        "1",
        "--document-workers",
        "1",
        "--ocr-workers",
        "2",
    ]
    with (tmp_path / "actual.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            assert process.wait(45) == (2 if quality_failure else 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(20)
    current = state(snapshot)
    assert current["status"] == ("blocked_quality" if quality_failure else "completed")
    paid = records(snapshot / "http-local.jsonl")
    assert len(paid) == 6
    assert len({(row["document_id"], row["page"]) for row in paid}) == 6
    runs = sorted((snapshot / "runs").iterdir())
    assert len(runs) == 3
    for run in runs:
        report = read_json(run / "report.json")
        assert report["workers_running"] is False
        assert report["embedding_requests"] == report["download_requests"] == 0
        assert report["blockers"] == []
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM processing_jobs WHERE lease_token IS NOT NULL").fetchone()[0]
            == 0
        )
        for index, doc in enumerate(docs):
            raw = conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
            manifest = json.loads(raw)
            assert text_complete(manifest) == (not quality_failure or index == 0)
            assert all(page["source_hash"] == doc["content_hash"] for page in manifest["pages"])
    # A second process reads durable state and makes no additional local request.
    with (tmp_path / "restart.log").open("w") as log:
        assert subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=15).returncode == (
            2 if quality_failure else 0
        )
    assert records(snapshot / "http-local.jsonl") == paid
