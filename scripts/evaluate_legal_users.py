"""Real HTTP/SSE acceptance; run only against an isolated test data instance.

No provider keys are read by this script. Groups explicitly control shared conversations.
Usage: uv run --project backend --no-sync python scripts/evaluate_legal_users.py \
    --base http://127.0.0.1:8482 --report .dev/legal-p0/results.json
"""

import argparse
import json
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def checked(response):
    response.raise_for_status()
    return response.json()


def read_audit(path, case_id, client_id):
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "SELECT id,status,result FROM agent_runs WHERE case_id=? AND client_id=?",
            (case_id, client_id),
        ).fetchone()
    if not row:
        return None
    result = json.loads(row[2] or "{}")
    return {
        "run_id": row[0],
        "status": row[1],
        "task_state": result.get("_task_state"),
        "trace": result.get("trace", []),
        "usage": result.get("usage", []),
        "evidence": result.get("_evidence", []),
        "citation_associations": result.get("_citation_associations", {}),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--ids", default="Q03,Q04,Q05,Q08")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Consecutive independent repetitions, 1–10",
    )
    parser.add_argument(
        "--audit-db",
        type=Path,
        help="Read-only isolated instance DB for private task audit",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend/tests/fixtures/legal_user_acceptance.json",
    )
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    if urlsplit(args.base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The acceptance runner requires a local isolated service")
    ids = set(args.ids.split(","))
    fixtures = json.loads(args.fixtures.read_text())
    questions = [
        {
            **record,
            "group": f"{record['group']}/repeat-{index}",
            "repetition": index + 1,
        }
        for index in range(args.repeat)
        for record in fixtures
    ]
    groups = {}
    report = {
        "started_at": datetime.now().astimezone().isoformat(),
        "base": args.base,
        "runs": [],
        "note": "Actual HTTP/SSE runs; legal correctness requires separate review",
    }
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=args.base, timeout=httpx.Timeout(360, connect=10), trust_env=False) as client:
        checked(client.post("/api/tea/auth/demo", json={}))
        bootstrap = checked(client.get("/api/tea/bootstrap"))
        for record in questions:
            name, country, question = (
                record["id"],
                record["country_template"],
                record["question"],
            )
            if name not in ids:
                continue
            run = {
                "id": name,
                "repetition": record["repetition"],
                "question": question,
                "events": [],
                "country_template": country,
            }
            report["runs"].append(run)
            start = time.monotonic()
            try:
                candidates = [
                    template
                    for template in bootstrap["templates"]
                    if (country == "SG")
                    == ("新加坡" in template["destination"] or "singapore" in template["destination"].lower())
                ]
                template = candidates[0]
                mode = next(item for item in template["modes"] if item["enabled"])
                if record["group"] not in groups:
                    case = checked(
                        client.post(
                            "/api/tea/cases",
                            json={
                                "title": "六堡茶交易咨询",
                                "company": "",
                                "product": "六堡茶",
                                "mode_id": mode["id"],
                                "template_id": template["id"],
                            },
                        )
                    )
                    stage = case["stages"][0]["id"]
                    conversation = checked(
                        client.post(
                            f"/api/tea/cases/{case['id']}/stages/{stage}/conversations",
                            json={
                                "client_request_id": str(uuid.uuid4()),
                                "title": "交易准备咨询",
                            },
                        )
                    )
                    groups[record["group"]] = (case, stage, conversation)
                case, stage, conversation = groups[record["group"]]
                run["case_id"] = case["id"]
                run["conversation_id"] = conversation["id"]
                payload = {
                    "stage_id": stage,
                    "conversation_id": conversation["id"],
                    "content": question,
                    "client_message_id": str(uuid.uuid4()),
                }
                run["client_message_id"] = payload["client_message_id"]
                event, lines = "", []
                with client.stream(
                    "POST", f"/api/tea/cases/{case['id']}/messages/stream", json=payload
                ) as response:
                    run["http_status"] = response.status_code
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            lines.append(line[5:].lstrip())
                        elif not line and event and lines:
                            data = json.loads("\n".join(lines))
                            run["events"].append(
                                {
                                    "event": event,
                                    "data": data,
                                    "elapsed_seconds": round(time.monotonic() - start, 3),
                                }
                            )
                            if event == "delta" and "first_delta_seconds" not in run:
                                run["first_delta_seconds"] = round(time.monotonic() - start, 3)
                            if event == "done":
                                run["final"] = data
                            elif event == "error":
                                run["error"] = data
                            event, lines = "", []
                if "final" in run:
                    assistant = run["final"]["messages"][1]
                    run["answer"] = assistant["content"]
                    run["tools"] = [
                        tool for step in assistant.get("trace", []) for tool in step.get("tools", [])
                    ]
                workspace = checked(client.get(f"/api/tea/cases/{case['id']}/legal"))
                run["analyses"] = [
                    checked(client.get(f"/api/tea/cases/{case['id']}/legal/analyses/{item['id']}"))
                    for item in workspace["analyses"]
                ]
            except Exception as error:
                run["runner_error"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                }
            if args.audit_db and run.get("client_message_id"):
                try:
                    run["audit"] = read_audit(args.audit_db, run["case_id"], run["client_message_id"])
                except (sqlite3.Error, OSError, ValueError) as error:
                    # Losing audit access must not erase a completed paid attempt.
                    run["audit_error"] = {"type": type(error).__name__, "message": str(error)[:500]}
            state = (run.get("audit") or {}).get("task_state") or {}
            run["delivery_status"] = state.get("delivery")
            run["model_calls"] = state.get("model_calls")
            run["elapsed_seconds"] = round(time.monotonic() - start, 3)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            output.chmod(0o600)
            print(
                json.dumps(
                    {
                        "id": name,
                        "seconds": run["elapsed_seconds"],
                        "transport_completed": "final" in run,
                        "delivery_status": run.get("delivery_status"),
                        "model_calls": run.get("model_calls"),
                        "tools": [item["name"] for item in run.get("tools", [])],
                        "answer_chars": len(run.get("answer", "")),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
