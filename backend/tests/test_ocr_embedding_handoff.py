"""Real SQLite/CAS, child processes and local HTTP; no remote providers."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import ocr_embedding_handoff as handoff  # noqa: E402
from embedding_supervisor import persist  # noqa: E402
from app.db import dump  # noqa: E402
from app.embedding_admission import SharedEmbeddingBudget, model_scope, process_identity  # noqa: E402
from app.service import Service  # noqa: E402
from app.system_library import SYSTEM_OWNER_ID, ensure_principal  # noqa: E402
from test_embedding_reuse import register, seed_vectors  # noqa: E402


def wait_for(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("Isolated child checkpoint timed out")
        time.sleep(0.03)


def records(path):
    return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []


def policy_plan(directory, label, docs, service, fingerprint):
    plan = {
        "protocol": "fixed-embedding-continuation-v1",
        "data_dir": str(directory / "data"),
        "owner": SYSTEM_OWNER_ID,
        "fingerprint": fingerprint,
        "documents": [{**{key: doc[key] for key in handoff.SOURCE_FIELDS}, "chunk_count": 2} for doc in docs],
    }
    plan_path, inventory_path = directory / (label + ".json"), directory / (label + "-inputs.json")
    persist(plan_path, plan)
    with closing(service.db.connect()) as conn:
        inputs = {
            doc["id"]: [
                dict(row)
                for row in conn.execute(
                    "SELECT ordinal,input_hash FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal",
                    (doc["id"],),
                )
            ]
            for doc in docs
        }
    persist(
        inventory_path,
        {
            "protocol": "fixed-embedding-input-inventory-v1",
            "plan_sha256": handoff.sha(plan_path),
            "plan_digest": handoff.digest(plan),
            "documents": inputs,
            "unique_inputs_sha256": handoff.digest(
                sorted({r["input_hash"] for rows in inputs.values() for r in rows})
            ),
        },
    )
    return {
        "kind": label,
        "plan": handoff.reference(plan_path),
        "inputs": handoff.reference(inventory_path),
        "expected_documents": len(docs),
    }


OCR_CHILD = r"""
import hashlib, json, os, sys, math
from pathlib import Path
from contextlib import closing
from app.db import dump
from app.service import Service
from embedding_supervisor import persist
from process_frozen_parse import parse_args, frozen_inputs, selected_documents, digest
from frozen_parse_supervisor import acquire_parse_lock, supervise, verify_child_binding
args = parse_args()
if args.supervise:
    raise SystemExit(supervise(args, Path(__file__)))
lock = acquire_parse_lock(args.snapshot, args.supervisor_lock_fd)
verify_child_binding(args, Path(__file__))
report, initial = frozen_inputs(args.snapshot, args.data_dir)
selected = selected_documents(args, initial)
output = args.snapshot / "runs" / args.run_id
output.mkdir(parents=True)
persist(output / "run-input.json", {"run_id":args.run_id, "selected_ids":selected,
    "owner_id":report["owner_id"], "models_file_sha256":hashlib.sha256(args.models_env.read_bytes()).hexdigest()})
service = Service(args.data_dir)
finals = json.loads((args.snapshot / "local-accepted-results.json").read_text())
generation = int(args.run_id.rsplit("-",2)[1])
per_window = math.ceil(len(finals) / int(os.environ.get("HANDOFF_OCR_FIXTURE_WINDOWS","1")))
finals = dict(list(finals.items())[(generation-1)*per_window:generation*per_window])
events, checkpoints = [], []
with service.db.transaction() as conn:
    for identifier, manifest in finals.items():
        conn.execute("UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), identifier))
        service.index.sync_document(conn, identifier, manifest)
        quality = manifest.get("quality",{}).get("complete") is False
        if quality:
            conn.execute("DELETE FROM retrieval_chunks WHERE document_id=?", (identifier,))
        conn.execute("UPDATE processing_jobs SET phase=?,status=?,available_at=0,lease_until=NULL,lease_token=NULL WHERE document_id=?",
            ("parse" if quality else "embed", "completed" if quality else "queued", identifier))
        checkpoint = {"document_id":identifier,"text_complete":not quality,"manifest_sha256":hashlib.sha256(dump(manifest).encode()).hexdigest()}
        checkpoints.append(checkpoint)
        events.extend([{"at":0,"kind":"claim","document_id":identifier}, {"at":1,"kind":"document_checkpoint",**checkpoint}])
(output / "events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
persist(output / "report.json", {"run_id":args.run_id, "workers_running":False,
    "selected_ids_sha256":digest(selected), "initial_source_and_metadata_guards_preserved":True,
    "initial_successful_pages_unchanged":True, "embedding_requests":0,"download_requests":0,
    "claims":len(finals), "blockers":[],"transport":{},"document_checkpoints":checkpoints})
"""


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    def create(count=2, secondary=0, quality=0, windows=1):
        for key, value in {
            "BASE_URL": "https://provider.invalid/v1",
            "API_KEY": "local-test-key",
            "MODEL": "local-model",
        }.items():
            monkeypatch.setenv("EMBEDDING_" + key, value)
        data = tmp_path / "data"
        service = Service(data)
        with service.db.transaction() as conn:
            ensure_principal(conn)
        fingerprint = service.embedding_fingerprint()
        scope = model_scope("https://provider.invalid/v1", "local-model")
        admission = tmp_path / "admission.sqlite3"
        SharedEmbeddingBudget(admission, scope, 64)
        models = tmp_path / "models.env"
        models.write_text(
            "EMBEDDING_BASE_URL=https://provider.invalid/v1\nEMBEDDING_API_KEY=local-test-key\nEMBEDDING_MODEL=local-model\n"
        )

        def add(key, level="primary"):
            return register(
                service,
                key,
                owner=SYSTEM_OWNER_ID,
                country="SG",
                level=level,
                url="https://www.sfa.gov.sg/" + key + ".txt",
                context=key,
                content=key.encode(),
            )

        old, prior = add("legacy"), add("prior")
        seed_vectors(service, old, fingerprint=fingerprint)
        seed_vectors(service, prior, fingerprint=fingerprint)
        plans = [
            policy_plan(tmp_path, "legacy_fixed", [old], service, fingerprint),
            policy_plan(tmp_path, "prior_handoff", [prior], service, fingerprint),
        ]
        docs = [add("new-" + str(i), "secondary" if i < secondary else "primary") for i in range(count)]
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        finals = {}
        guards = []
        with service.db.transaction() as conn:
            for index, doc in enumerate(docs):
                final = json.loads(doc["manifest"])
                final["pages"] = [
                    {
                        "page": 1,
                        "status": "native",
                        "text": "Accepted source page.",
                        "source_hash": doc["content_hash"],
                    }
                ]
                if index >= count - quality:
                    final["quality"] = {"complete": False}
                    final["pages"][0].update(status="ocr_complete", ocr={"complete": False})
                finals[doc["id"]] = final
                pending = {
                    "source_hash": doc["content_hash"],
                    "elements": [],
                    "chunks": [],
                    "pages": [
                        {"page": 1, "status": "pending_ocr", "text": "", "source_hash": doc["content_hash"]}
                    ],
                }
                conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(pending), doc["id"]))
                service.index.sync_document(conn, doc["id"], pending)
                conn.execute("UPDATE processing_jobs SET phase='parse' WHERE document_id=?", (doc["id"],))
                meta = conn.execute(
                    "SELECT payload,revision FROM document_metadata WHERE document_id=?", (doc["id"],)
                ).fetchone()
                guards.append(
                    {
                        **{
                            key: doc[key]
                            for key in (
                                *handoff.SOURCE_FIELDS,
                                "role",
                                "family_id",
                                "case_id",
                                "parent_id",
                                "title",
                                "filename",
                                "mime_type",
                            )
                        },
                        "metadata_revision": meta["revision"],
                        "metadata_sha256": hashlib.sha256(meta["payload"].encode()).hexdigest(),
                        "protected_provenance": {"source_hash": handoff.digest(doc["content_hash"])},
                        "pages": [
                            {
                                "page": 1,
                                "validated_status": "pending_ocr",
                                "sha256": handoff.digest(pending["pages"][0]),
                            }
                        ],
                    }
                )
        persist(snapshot / "local-accepted-results.json", finals)
        (snapshot / "initial-parse-ids.json").write_text(json.dumps([d["id"] for d in docs]))
        (snapshot / "initial-parse-documents.json").write_text(json.dumps(guards))
        persist(
            snapshot / "initial-report.json",
            {
                "owner_id": SYSTEM_OWNER_ID,
                "source_data_dir": str(data),
                "initial_parse_queued_documents": count,
                "initial_id_list_sha256": handoff.sha(snapshot / "initial-parse-ids.json"),
                "initial_documents_sha256": handoff.sha(snapshot / "initial-parse-documents.json"),
            },
        )
        driver = tmp_path / "local_ocr.py"
        driver.write_text(OCR_CHILD)
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend") + ":" + str(ROOT / "scripts"),
            "HANDOFF_OCR_FIXTURE_WINDOWS": str(windows),
        }
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
            "fixture",
            "--supervise",
            "--seconds",
            "30",
        ]
        with (tmp_path / "ocr.log").open("w") as log:
            assert subprocess.run(
                command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=20
            ).returncode == (2 if quality else 0)
        state = handoff.read_json(snapshot / "supervisor-state.json")
        assert state["status"] == ("blocked_quality" if quality else "completed")
        assert "binding_sha256" not in state  # The real OCR supervisor does not emit this field.
        holds, history = tmp_path / "holds.json", tmp_path / "history.json"
        persist(
            holds,
            {
                "current_records": [{"document_id": "historical-held"}],
                "historical_records": [],
                "historical_paid_missing_document_ids": [],
                "historical_paid_missing_records": [],
            },
        )
        persist(history, {"additional_document_holds": []})
        args = SimpleNamespace(
            ocr_snapshot=snapshot,
            handoff_root=tmp_path / "handoffs",
            exclusions=tmp_path / "policy.json",
            models_env=models,
            admission_path=admission,
        )
        persist(
            args.exclusions,
            {
                "protocol": "ocr-embedding-exclusions-v1",
                "handoff_root": str(args.handoff_root),
                "data_dir": str(data),
                "owner": SYSTEM_OWNER_ID,
                "fingerprint": fingerprint,
                "admission_scope": scope,
                "admission_path": str(admission),
                "embedding_plans": plans,
                "ocr_holds": handoff.reference(holds),
                "ocr_history": handoff.reference(history),
                "expected_held_documents": 1,
            },
        )
        return args, service, docs, env

    return create


def prepare(args):
    args.handoff_root.mkdir(exist_ok=True)
    with handoff.owned_lock(args.handoff_root / ".handoff.lock"):
        return handoff.prepare(args)


def command(args):
    result = [sys.executable, str(ROOT / "scripts/ocr_embedding_handoff.py")]
    for name in ("ocr_snapshot", "handoff_root", "exclusions", "models_env", "admission_path"):
        result.extend(("--" + name.replace("_", "-"), str(getattr(args, name))))
    return result


def business_guard(service):
    with closing(service.db.connect()) as conn:
        return {
            table: [tuple(r) for r in conn.execute("SELECT * FROM " + table + " ORDER BY 1")]
            for table in (
                "documents",
                "document_metadata",
                "document_metadata_history",
                "legal_classifications",
                "legal_relations",
                "processing_jobs",
                "retrieval_chunks",
                "retrieval_vectors",
            )
        }


def test_prepare_real_terminal_16_complete_only_15_primary_without_service_or_writes(corpus, monkeypatch):
    args, service, docs, _ = corpus(count=16, secondary=1)
    before = business_guard(service)
    monkeypatch.setattr(Service, "__init__", lambda *_a, **_k: pytest.fail("prepare constructed Service"))
    state, entry = prepare(args)
    plan = handoff.read_json(args.handoff_root / entry["batch_id"] / "selection.json")
    assert len(plan["documents"]) == 15
    assert docs[0]["id"] not in {d["id"] for d in plan["documents"]}
    assert business_guard(service) == before
    again, same = prepare(args)
    assert same == entry and again == state
    assert len(list(args.handoff_root.glob("*/selection.json"))) == 1


def test_real_supervisor_31_complete_nine_quality_with_missing_index_chunks_deferred(corpus):
    args, _, docs, _ = corpus(count=40, quality=9)
    _, entry = prepare(args)
    proof = handoff.read_json(args.handoff_root / entry["batch_id"] / "selection-proof.json")
    assert len(proof["documents"]) == 31
    assert proof["deferred"] == {doc["id"]: "incomplete_or_quality_held" for doc in docs[-9:]}


@pytest.mark.parametrize("damage", ["missing_report", "missing_events", "partial_line", "valid_prefix"])
def test_all_actual_ocr_windows_must_have_closed_evidence(corpus, damage):
    args, _, _, _ = corpus(windows=2)
    assert handoff.read_json(args.ocr_snapshot / "supervisor-state.json")["generation"] == 2
    first = sorted((args.ocr_snapshot / "runs").iterdir())[0]
    if damage == "missing_report":
        (first / "report.json").unlink()
    elif damage == "missing_events":
        (first / "events.jsonl").unlink()
    else:
        path = first / "events.jsonl"
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] if damage == "partial_line" else raw.splitlines(keepends=True)[0])
    with pytest.raises((ValueError, OSError)):
        prepare(args)


@pytest.mark.parametrize(
    "outcomes,extra",
    [
        ([200] * 15 + [429] * 3, {"peak_inflight": 2}),
        ([200, 200, 429, "ConnectTimeout"], {"peak_inflight": 2}),
        ([200], {"peak_inflight": 1, "invalid_response": 1}),
    ],
)
def test_real_transport_counters_close_by_outcome_not_peak_or_other_state(tmp_path, outcomes, extra):
    events = []
    transport = {"requests": len(outcomes), "inflight": 0, **extra}
    for outcome in outcomes:
        events.append({"kind": "request_started", "document_id": "fixed"})
        if isinstance(outcome, int):
            events.append({"kind": "provider_result", "document_id": "fixed", "status_code": outcome})
            key = f"http_{outcome}"
        else:
            events.append({"kind": "provider_result", "document_id": "fixed", "timeout": outcome})
            key = "transport_" + outcome
        transport[key] = transport.get(key, 0) + 1
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in events))
    report = {"claims": 0, "document_checkpoints": [], "transport": transport}
    handoff.audit_ocr_events(path, report, {"fixed"})
    for change in (
        {"inflight": 1},
        {"unknown_paid_outcome": 1},
        {"http_200": transport.get("http_200", 0) + 1},
    ):
        with pytest.raises(ValueError, match="does not close"):
            handoff.audit_ocr_events(path, {**report, "transport": {**transport, **change}}, {"fixed"})


@pytest.mark.parametrize(
    "damage", ["old_input", "old_document", "hold", "dirty", "active", "failed", "published", "unclassified"]
)
def test_candidate_boundaries_fail_closed(corpus, damage):
    args, service, docs, _ = corpus()
    identifier = docs[0]["id"]
    if damage in {"old_input", "old_document", "hold"}:
        policy = handoff.read_json(args.exclusions)
        if damage == "hold":
            holds = Path(policy["ocr_holds"]["path"])
            value = handoff.read_json(holds)
            value["current_records"][0]["document_id"] = identifier
            persist(holds, value)
            policy["ocr_holds"] = handoff.reference(holds)
        else:
            entry = policy["embedding_plans"][0]
            if damage == "old_document":
                plan_path = Path(entry["plan"]["path"])
                plan = handoff.read_json(plan_path)
                old_id = plan["documents"][0]["id"]
                plan["documents"][0]["id"] = identifier
                persist(plan_path, plan)
                entry["plan"] = handoff.reference(plan_path)
            inventory_path = Path(entry["inputs"]["path"])
            inv = handoff.read_json(inventory_path)
            if damage == "old_document":
                inv["documents"][identifier] = inv["documents"].pop(old_id)
                inv["plan_sha256"], inv["plan_digest"] = entry["plan"]["sha256"], handoff.digest(plan)
            else:
                with closing(service.db.connect()) as conn:
                    h = conn.execute(
                        "SELECT input_hash FROM retrieval_chunks WHERE document_id=? LIMIT 1", (identifier,)
                    ).fetchone()[0]
                next(iter(inv["documents"].values()))[0]["input_hash"] = h
            inv["unique_inputs_sha256"] = handoff.digest(
                sorted({r["input_hash"] for rows in inv["documents"].values() for r in rows})
            )
            persist(inventory_path, inv)
            entry["inputs"] = handoff.reference(inventory_path)
        persist(args.exclusions, policy)
    else:
        with service.db.transaction() as conn:
            if damage == "dirty":
                conn.execute("INSERT OR IGNORE INTO retrieval_dirty VALUES(?)", (identifier,))
            elif damage in {"active", "failed"}:
                conn.execute(
                    "UPDATE processing_jobs SET status=?,lease_token=?,lease_until=? WHERE document_id=?",
                    (
                        "processing" if damage == "active" else "failed",
                        "owned" if damage == "active" else None,
                        time.time() + 300,
                        identifier,
                    ),
                )
            elif damage == "unclassified":
                conn.execute("DELETE FROM legal_classifications WHERE document_id=?", (identifier,))
            else:
                conn.execute(
                    "UPDATE document_metadata SET payload=json_set(payload,'$.review_status','published') WHERE document_id=?",
                    (identifier,),
                )
    if damage == "published":
        with pytest.raises(ValueError, match="metadata"):
            prepare(args)  # Source snapshot metadata itself changed.
    else:
        _, entry = prepare(args)
        plan = handoff.read_json(args.handoff_root / entry["batch_id"] / "selection.json")
        assert [d["id"] for d in plan["documents"]] == [docs[1]["id"]]


@pytest.mark.parametrize(
    "damage", ["terminal", "active_ocr", "inventory", "hold_count", "registry", "journal", "scope"]
)
def test_missing_or_changed_evidence_cannot_create_replacement_scope(corpus, damage):
    args, _, _, _ = corpus()
    if damage in {"registry", "journal"}:
        _, entry = prepare(args)
        if damage == "registry":
            (args.handoff_root / "handoff-state.json").unlink()
        else:
            (args.handoff_root / entry["batch_id"] / "output/issued-requests.jsonl").unlink()
    elif damage in {"terminal", "active_ocr"}:
        state = handoff.read_json(args.ocr_snapshot / "supervisor-state.json")
        if damage == "terminal":
            (args.ocr_snapshot / "runs" / state["last_run_id"] / "report.json").unlink()
        else:
            state["active_run_id"] = "unreported"
            persist(args.ocr_snapshot / "supervisor-state.json", state)
    else:
        policy = handoff.read_json(args.exclusions)
        if damage == "inventory":
            entry = policy["embedding_plans"][0]
            path = Path(entry["inputs"]["path"])
            inv = handoff.read_json(path)
            next(iter(inv["documents"].values())).pop()
            persist(path, inv)
            entry["inputs"] = handoff.reference(path)
        elif damage == "scope":
            policy["handoff_root"] = str(args.handoff_root / "replacement")
        else:
            policy["expected_held_documents"] = 52
        persist(args.exclusions, policy)
    with pytest.raises((ValueError, OSError)):
        prepare(args)


LOCAL_PROVIDER = r"""
import json, os, sys, time, subprocess
from pathlib import Path
import httpx
from app.retrieval import RetrievalIndex
from app.service import Service
root = Path(os.environ["HANDOFF_LOCAL_TEST_ROOT"])
def audit(event,args):
    if event in {"socket.connect","socket.getaddrinfo"}:
        raise RuntimeError("Remote networking forbidden in handoff tests")
sys.addaudithook(audit)
original_init = Service.__init__
def paused_init(self,*args,**kwargs):
    if Path(sys.argv[0]).name == "complete_embeddings.py" and "--apply" not in sys.argv and (root / "pause-preflight").exists():
        (root / "preflight-waiting").write_text(str(os.getpid()))
        while (root / "pause-preflight").exists(): time.sleep(.03)
    original_init(self,*args,**kwargs)
Service.__init__ = paused_init
original_spawn = subprocess.Popen
def paused_spawn(command,*args,**kwargs):
    child = original_spawn(command,*args,**kwargs)
    if "--run-batch" in command and (root / "pause-parent-spawn").exists():
        (root / "spawn-gap").write_text(str(child.pid))
        while (root / "pause-parent-spawn").exists(): time.sleep(.03)
    return child
subprocess.Popen = paused_spawn
original = httpx.Client
def provider(request):
    payload = json.loads(request.content)
    with (root / "http.jsonl").open("a") as stream:
        stream.write(json.dumps({"pid":os.getpid(), "inputs":payload["input"]}) + "\n")
        stream.flush();os.fsync(stream.fileno())
    if (root / "pause-http").exists():
        (root / "http-waiting").write_text(str(os.getpid()))
        while (root / "pause-http").exists(): time.sleep(.03)
    if (root / "unknown-http").exists():
        raise httpx.ReadTimeout("Local unknown after send")
    return httpx.Response(200,json={"data":[{"index":i,"embedding":[2.,3.]} for i in range(len(payload["input"]))]})
httpx.Client = lambda **kw: original(transport=httpx.MockTransport(provider),**kw)
store = RetrievalIndex.store_vectors
def paused_store(self,*args,**kwargs):
    if (root / "pause-commit").exists():
        (root / "cache-saved").write_text(str(os.getpid()))
        while (root / "pause-commit").exists(): time.sleep(.03)
    return store(self,*args,**kwargs)
RetrievalIndex.store_vectors = paused_store
"""


def mock_environment(tmp_path, env):
    plugin = tmp_path / "mock-provider"
    plugin.mkdir()
    (plugin / "sitecustomize.py").write_text(LOCAL_PROVIDER)
    return {
        **env,
        "PYTHONPATH": str(plugin) + ":" + env["PYTHONPATH"],
        "HANDOFF_LOCAL_TEST_ROOT": str(tmp_path),
    }


def state_entry(args):
    return handoff.read_json(args.handoff_root / "handoff-state.json")["batches"][0]


def stop_fixture(args):
    if not (args.handoff_root / "handoff-state.json").exists():
        return
    entry = state_entry(args)
    candidates = [(entry.get("pid"), entry.get("start_identity"))]
    supervisor = Path(entry["output"]) / "supervisor-state.json"
    if supervisor.exists():
        state = handoff.read_json(supervisor)
        candidates.append((state.get("child_pid"), state.get("child_start_identity")))
    for pid, identity in candidates:
        if pid and identity and process_identity(pid) == identity:
            os.kill(pid, signal.SIGKILL)


def test_spawn_gap_and_unpaid_preflight_interrupt_resume_the_original_output(corpus, tmp_path):
    args, _, _, env = corpus(count=1)
    env = mock_environment(tmp_path, env)
    for name in ("pause-preflight", "pause-parent-spawn"):
        (tmp_path / name).touch()
    with (tmp_path / "preflight-interrupted.log").open("w") as log:
        parent = subprocess.Popen([*command(args), "--apply"], env=env, stdout=log, stderr=subprocess.STDOUT)
        preflight = wrapper = None
        try:
            wait_for(lambda: (tmp_path / "spawn-gap").exists() and (tmp_path / "preflight-waiting").exists())
            wrapper = int((tmp_path / "spawn-gap").read_text())
            preflight = int((tmp_path / "preflight-waiting").read_text())
            entry = state_entry(args)
            assert not entry.get("paid_launch_intent")
            parent.kill()
            parent.wait(5)
            assert (
                subprocess.run(
                    [*command(args), "--apply"], env=env, capture_output=True, timeout=10
                ).returncode
                == 2
            )
            os.kill(wrapper, signal.SIGKILL)
            os.kill(preflight, signal.SIGKILL)
            wait_for(lambda: process_identity(wrapper) is None and process_identity(preflight) is None)
            for name in ("pause-preflight", "pause-parent-spawn"):
                (tmp_path / name).unlink()
            assert not (Path(entry["output"]) / "successful-inputs.sqlite3").exists()
            assert records(tmp_path / "http.jsonl") == []
            resumed = subprocess.run([*command(args), "--apply"], env=env, capture_output=True, timeout=30)
            assert resumed.returncode == 0, resumed.stderr.decode()
            assert state_entry(args)["output"] == entry["output"]
            assert state_entry(args)["status"] == "completed"
            assert len(records(tmp_path / "http.jsonl")) == 1
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(5)
            stop_fixture(args)
            for pid in (wrapper, preflight):
                if pid and process_identity(pid):
                    os.kill(pid, signal.SIGKILL)


def test_actual_preflight_and_supervisor_complete_then_repeat_zero_requests(corpus, tmp_path):
    args, service, _, env = corpus(count=3)
    env = mock_environment(tmp_path, env)
    before = business_guard(service)
    with (tmp_path / "apply.log").open("w") as log:
        assert (
            subprocess.run(
                [*command(args), "--apply"], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=35
            ).returncode
            == 0
        )
    entry = state_entry(args)
    assert entry["status"] == "completed"
    paid = records(tmp_path / "http.jsonl")
    assert len(paid) == 3
    report = handoff.read_json(Path(entry["output"]) / "report.json")
    assert report["missing_rows"] == 0 and all(report["integrity"].values())
    assert report["configuration"]["http_workers"] == 64
    assert report["configuration"]["document_workers"] == 12
    assert report["configuration"]["request_concurrency"] == report["configuration"]["batch_size"] == 2
    assert report["configuration"]["retry_unresolved"] is False
    assert report["shared_admission"]["limit"] == 64
    assert report["shared_admission"]["path"] == str(args.admission_path)
    assert report["shared_admission"]["query_reserved"] == 4
    assert report["shared_admission"]["bulk_ceiling"] == 60
    assert report["shared_admission"]["bulk_floor_when_waiting"] == 16
    after = business_guard(service)
    for table in (
        "document_metadata",
        "document_metadata_history",
        "legal_classifications",
        "legal_relations",
    ):
        assert after[table] == before[table]
    assert all(row in after["retrieval_vectors"] for row in before["retrieval_vectors"])
    assert (
        subprocess.run([*command(args), "--apply"], env=env, capture_output=True, timeout=15).returncode == 0
    )
    assert records(tmp_path / "http.jsonl") == paid
    assert len(handoff.read_json(args.handoff_root / "handoff-state.json")["batches"]) == 1
    batch = Path(entry["output"]).parent
    identifier = handoff.read_json(batch / "selection.json")["documents"][0]["id"]
    with closing(service.db.connect()) as conn, closing(sqlite3.connect(":memory:")) as saved:
        conn.backup(saved)
        for sql in (
            "DELETE FROM retrieval_chunks WHERE document_id=?",
            "DELETE FROM processing_jobs WHERE document_id=?",
            "UPDATE retrieval_chunks SET input_hash='changed' WHERE document_id=?",
        ):
            conn.execute(sql, (identifier,))
            conn.commit()
            with pytest.raises(ValueError, match="not drained and complete"):
                handoff.completion(batch)
            saved.backup(conn)
    ocr_state = handoff.read_json(args.ocr_snapshot / "supervisor-state.json")
    path = args.ocr_snapshot / "runs" / ocr_state["last_run_id"] / "report.json"
    altered = handoff.read_json(path)
    altered["otherwise_valid_addendum"] = "changed evidence bytes"
    persist(path, altered)
    with pytest.raises(ValueError, match="evidence bytes changed"):
        handoff.revalidate(args, batch)


def test_real_unknown_result_stays_reserved_and_restart_never_repays(corpus, tmp_path):
    args, _, _, env = corpus(count=1)
    env = mock_environment(tmp_path, env)
    (tmp_path / "unknown-http").touch()
    with (tmp_path / "unknown.log").open("w") as log:
        parent = subprocess.Popen([*command(args), "--apply"], env=env, stdout=log, stderr=subprocess.STDOUT)
        resumed = None
        try:
            wait_for(
                lambda: (
                    (args.handoff_root / "handoff-state.json").exists()
                    and handoff.read_json(args.handoff_root / "handoff-state.json").get("batches")
                    and (Path(state_entry(args)["output"]) / "report.json").exists()
                )
            )
            entry = state_entry(args)
            output = Path(entry["output"])
            report = handoff.read_json(output / "report.json")
            assert report["missing_rows"] == 2 and report["failure"] == "issued_request_result_unknown"
            parent.terminate()
            assert parent.wait(15) == 130
            paid = records(tmp_path / "http.jsonl")
            assert len(paid) == 1
            old_preflights = set(output.glob("preflight-*.json"))
            resumed = subprocess.Popen(
                [*command(args), "--apply"], env=env, stdout=log, stderr=subprocess.STDOUT
            )
            wait_for(lambda: bool(set(output.glob("preflight-*.json")) - old_preflights))
            progress = handoff.read_json(output / "progress.json")
            assert progress["quarantined_inputs"] == 2 and progress["deferred_documents"] == 1
            assert progress["explicitly_allowed_unresolved_inputs"] == 0
            resumed.terminate()
            assert resumed.wait(15) == 130
            assert records(tmp_path / "http.jsonl") == paid
            assert state_entry(args)["status"] == "blocked" and state_entry(args)["output"] == str(output)
            # An accidental new output root cannot hide this unresolved receipt.
            changed = command(args)
            changed[changed.index("--handoff-root") + 1] = str(tmp_path / "replacement")
            assert (
                subprocess.run([*changed, "--apply"], env=env, capture_output=True, timeout=10).returncode
                == 2
            )
            (output / "successful-inputs.sqlite3").unlink()
            assert (
                subprocess.run(
                    [*command(args), "--apply"], env=env, capture_output=True, timeout=10
                ).returncode
                == 2
            )
            assert records(tmp_path / "http.jsonl") == paid
        finally:
            for process in (parent, resumed):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)
            stop_fixture(args)


def test_kill_parent_and_wrapper_paid_child_keeps_global_lock(corpus, tmp_path):
    args, _, _, env = corpus(count=1)
    env = mock_environment(tmp_path, env)
    pause = tmp_path / "pause-http"
    pause.touch()
    with (tmp_path / "killed-parent.log").open("w") as log:
        parent = subprocess.Popen([*command(args), "--apply"], env=env, stdout=log, stderr=subprocess.STDOUT)
        runner = wrapper = None
        try:
            wait_for(lambda: (tmp_path / "http-waiting").exists())
            runner = int((tmp_path / "http-waiting").read_text())
            wrapper = state_entry(args)["pid"]
            parent.kill()
            parent.wait(5)
            duplicate = subprocess.run([*command(args), "--apply"], env=env, capture_output=True, timeout=10)
            assert duplicate.returncode == 2
            os.kill(wrapper, signal.SIGKILL)
            wait_for(lambda: process_identity(wrapper) is None)
            duplicate = subprocess.run([*command(args), "--apply"], env=env, capture_output=True, timeout=10)
            assert duplicate.returncode == 2  # Paid grandchild owns the inherited lock too.
            assert len(records(tmp_path / "http.jsonl")) == 1
            pause.unlink()
            wait_for(lambda: process_identity(runner) is None)
            resumed = subprocess.run([*command(args), "--apply"], env=env, capture_output=True, timeout=30)
            assert resumed.returncode == 0, resumed.stderr.decode()
            assert state_entry(args)["status"] == "completed"
            assert len(records(tmp_path / "http.jsonl")) == 1
        finally:
            pause.unlink(missing_ok=True)
            if parent.poll() is None:
                parent.kill()
                parent.wait(5)
            for pid in (wrapper, runner):
                if pid and process_identity(pid):
                    os.kill(pid, signal.SIGKILL)
            stop_fixture(args)


def test_success_cache_survives_kill_before_vector_commit_and_reuses_original_output(corpus, tmp_path):
    args, service, docs, env = corpus(count=1)
    env = mock_environment(tmp_path, env)
    pause = tmp_path / "pause-commit"
    pause.touch()
    with (tmp_path / "killed-commit.log").open("w") as log:
        parent = subprocess.Popen([*command(args), "--apply"], env=env, stdout=log, stderr=subprocess.STDOUT)
        runner = wrapper = None
        try:
            wait_for(lambda: (tmp_path / "cache-saved").exists())
            runner = int((tmp_path / "cache-saved").read_text())
            entry = state_entry(args)
            wrapper = entry["pid"]
            output = Path(entry["output"])
            with closing(sqlite3.connect(output / "successful-inputs.sqlite3")) as conn:
                cached = conn.execute("SELECT * FROM inputs ORDER BY input_hash").fetchall()
                assert len(cached) == 2
            parent.kill()
            parent.wait(5)
            os.kill(wrapper, signal.SIGKILL)
            os.kill(runner, signal.SIGKILL)
            wait_for(lambda: process_identity(wrapper) is None and process_identity(runner) is None)
            pause.unlink()
            # Advance the isolated expired lease only; the production handoff
            # deliberately delegates lease recovery to the existing queue.
            with service.db.transaction() as conn:
                conn.execute("UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (docs[0]["id"],))
            with (tmp_path / "resume.log").open("w") as resumed_log:
                resumed = subprocess.run(
                    [*command(args), "--apply"],
                    env=env,
                    stdout=resumed_log,
                    stderr=subprocess.STDOUT,
                    timeout=90,
                )
            assert resumed.returncode == 0, (tmp_path / "resume.log").read_text()
            assert state_entry(args)["output"] == str(output)
            assert state_entry(args)["status"] == "completed"
            assert len(records(tmp_path / "http.jsonl")) == 1
            with closing(sqlite3.connect(output / "successful-inputs.sqlite3")) as conn:
                assert conn.execute("SELECT * FROM inputs ORDER BY input_hash").fetchall() == cached
        finally:
            pause.unlink(missing_ok=True)
            if parent.poll() is None:
                parent.kill()
                parent.wait(5)
            for pid in (wrapper, runner):
                if pid and process_identity(pid):
                    os.kill(pid, signal.SIGKILL)
