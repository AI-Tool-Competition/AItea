"""Adapters for periodic raw acquisition; reuse the existing official transports.

No Service, parser, OCR, embedding, or publication entry point is used here.
Each call is one bounded window inside an already frozen, isolated cycle.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from official_source_supervisor_recovery import confined, digest, load, offline_only, value_digest, write


def record(identity, metadata, raw):
    return {"identity": identity, "fingerprint": value_digest(metadata), "metadata": metadata, "record": raw}


def asset(root, relative, expected, size):
    path = confined(root, relative)
    if not path.is_file() or path.stat().st_size != size or digest(path) != expected:
        raise ValueError("original receipt bytes changed")
    return {"path": str(path.relative_to(root)), "sha256": expected, "size": size}


def _sg(config, cycle, stage, selected):
    import collect_egazette as sg

    output = cycle / "source"
    output.mkdir(mode=0o700, exist_ok=True)
    frontend = output / "frontend.html"
    if not frontend.exists():
        response = sg._download(
            sg.EGAZETTE_ORIGIN,
            deadline=time.monotonic() + config["timeout_seconds"],
            required_host="www.egazette.gov.sg",
            max_bytes=sg.MAX_SOURCE_BYTES,
            accept_compressed_html=True,
        )
        raw = response.pop("content")
        sg.public_search_from_html(response["url"], raw)
        sg.save_bytes(frontend, raw)
        write(output / "frontend-receipt.json", {"sha256": digest(frontend), "response": response})
    elif not (output / "frontend-receipt.json").exists():
        # The bytes were durable before a crash. Validate the public config;
        # do not fetch again or invent the missing HTTP response metadata.
        sg.public_search_from_html(sg.EGAZETTE_ORIGIN, frontend.read_bytes())
        write(output / "frontend-receipt.json", {"sha256": digest(frontend), "response_lost": True})
    if digest(frontend) != load(output / "frontend-receipt.json")["sha256"]:
        raise ValueError("official frontend receipt changed")
    request, _ = sg.public_search_from_html(sg.EGAZETTE_ORIGIN, frontend.read_bytes())
    collector = sg.GazetteCollector(
        output,
        request,
        timeout=config["timeout_seconds"],
        time_budget=config["window_seconds"],
        page_size=config["page_size"],
        min_free_bytes=config["min_free_bytes"],
    )
    collector.gates = {
        host: threading.BoundedSemaphore(min(config["workers"], config["per_host"]))
        for host in sg.SOURCE_HOSTS
    }
    if stage == "catalogue":
        scope = config["scope"]
        states = collector.catalog(scope["categories"], range(scope["year_from"], scope["year_through"] + 1))
        states = [collector.confirm_partition_count(state) for state in states]
        rows = collector.rows()
        records = [
            record(
                "egazette:" + row["objectID"],
                {
                    key: value
                    for key, value in row.items()
                    if key
                    not in {"source_locator", "catalog_locators", "current_effect_verified", "source_role"}
                },
                row,
            )
            for row in rows
        ]
        return {
            "complete": all(state["status"] == "catalogue_complete" for state in states),
            "records": records,
            "partitions": states,
            "next_at": max(collector.cooldowns.values(), default=0),
        }
    rows = [entry["record"] for entry in selected]
    result = collector.originals(
        rows,
        max_files=len(rows),
        byte_budget=config["original_byte_budget"],
        retry_incomplete=True,
        max_attempts=config["max_original_attempts"],
    )
    originals = _sg_outcomes(output, selected, sg)
    return {
        "complete": all(row["status"] == "acquired" for row in originals),
        "originals": originals,
        "retryable": bool(result["pending"] or result["retry_next_at"]),
        "next_at": max(result["retry_next_at"] or 0, max(collector.cooldowns.values(), default=0)),
        "collector": result,
    }


def _sg_outcomes(output, selected, sg):
    originals = []
    for entry in selected:
        row = entry["record"]
        path = output / "originals" / (sg.sha256(row["download_url"].encode()).hexdigest() + ".json")
        receipt = load(path) if path.exists() else None
        if receipt:
            if receipt["source_url"] != row["download_url"] or receipt["title"] != row["title"]:
                raise ValueError("original receipt belongs to another frozen candidate")
            sg.original_attempts(output, receipt)
        files = []
        if receipt and receipt["status"] == "downloaded":
            files = [
                {
                    **asset(output, receipt["path"], receipt["sha256"], receipt["size"]),
                    "url": receipt["source_url"],
                }
            ]
        originals.append(
            {
                "identity": entry["identity"],
                "fingerprint": entry["fingerprint"],
                "status": "acquired" if files else "gap",
                "assets": files,
                "receipt": {"path": str(path.relative_to(output)), "sha256": digest(path)}
                if receipt
                else None,
                "source_status": receipt["status"] if receipt else "unattempted",
            }
        )
    return originals


def _id_eligible(task, config):
    if task["status"] in {"queued", "budget_exhausted"}:
        return True
    if task["status"] != "failed" or task["attempts"] >= config["max_original_attempts"]:
        return False
    result = json.loads(task["result"])
    return result.get("retryable") is True and not any(
        f"HTTP {code}" in result.get("error", "") for code in (400, 401, 403, 404, 405, 410, 451)
    )


def _id(config, cycle, stage, selected):
    import collect_indonesia_official as ind

    output = cycle / "source"
    definitions = {
        item["id"]: item
        for item in ind.load_blueprints()["indonesia-20261003"]["items"]
        if ind.SETNEG_ITEMS.get(item["id"]) in config["scope"]["types"]
    }
    definition_hash = config["bindings"]["config/collection-plans/indonesia-20261003.json"]
    with closing(
        ind.open_state(
            output,
            resume=(output / "journal.sqlite3").exists(),
            definition_hash=definition_hash,
            storage_root=cycle.parent.parent,
        )
    ) as conn:
        ind.verify_receipts(conn, output)
        with conn:
            for key, definition in definitions.items():
                for seed in ind.normalized_seeds_for(definition):
                    if seed["url"].startswith("https://jdih.setneg.go.id/api/hukumproduk/produkhukum?"):
                        conn.execute(
                            "INSERT OR IGNORE INTO tasks(item_code,url,purpose) VALUES(?,?,'catalog')",
                            (key, seed["url"]),
                        )
        verifier = ind.SourceVerifier(
            output,
            timeout=config["timeout_seconds"],
            time_budget=config["window_seconds"],
            workers=config["workers"],
            per_host=config["per_host"],
            max_original_bytes=ind.MAX_ORIGINAL_BYTES,
        )
        verifier.blobs = ind.CorpusBlobs(output / "files", max_bytes=ind.MAX_ORIGINAL_BYTES)
        details = {(entry["record"]["item_code"], entry["record"]["url"]) for entry in selected}

        def in_scope(task):
            if task["item_code"] not in definitions:
                return False
            if stage == "catalogue":
                return task["purpose"] == "catalog"
            return (task["purpose"] == "detail" and (task["item_code"], task["url"]) in details) or (
                task["purpose"] == "document" and (task["item_code"], task["parent_url"]) in details
            )

        while time.monotonic() < verifier.deadline:
            cooldown = conn.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()
            if cooldown and float(cooldown[0]) > time.time():
                break
            tasks = [
                dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY purpose DESC,item_code,url")
            ]
            pending = [
                task
                for task in tasks
                if in_scope(task)
                and _id_eligible(task, config)
                and (json.loads(task["result"] or "{}").get("retry_not_before", 0) <= time.time())
            ]
            if not pending:
                break
            used = sum(
                json.loads(t["result"])["size"]
                for t in tasks
                if t["purpose"] == "document" and t["status"] == "downloaded"
            )
            remaining = config["original_byte_budget"] - used
            free = max(0, shutil.disk_usage(output).free - config["min_free_bytes"])
            original_limit = min(remaining, ind.MAX_ORIGINAL_BYTES)
            pending = [task for task in pending if task["purpose"] != "document" or original_limit > 0]
            if not pending:
                break
            purpose = pending[0]["purpose"]
            bound = original_limit if purpose == "document" else ind.MAX_HTML_BYTES
            slots = min(config["workers"], free // bound)
            if purpose == "document":
                slots = min(slots, config["per_host"], remaining // bound)
            batch = [task for task in pending if task["purpose"] == purpose][:slots]
            if not batch:
                break
            verifier.max_original_bytes = bound

            def fetch(task):
                definition = definitions[task["item_code"]]
                return verifier.probe(
                    {"country": "ID", "item_id": task["item_code"], "recipe": ind.recipe_for(definition)},
                    task,
                    definition,
                )

            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                for task, response in zip(batch, pool.map(fetch, batch), strict=True):
                    ind.record_result(conn, task, response)
        report = ind.export_state(conn, output)
        ind.verify_receipts(conn, output)
        ledger = [json.loads(line) for line in (output / "ledger.jsonl").read_text().splitlines()]
        tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks") if in_scope(row)]
        cooldown = float(report["source_retry_state"].get("cooldown_until", 0))
        next_at = max(
            cooldown,
            min(
                (
                    json.loads(task["result"] or "{}").get("retry_not_before", 0)
                    for task in tasks
                    if _id_eligible(task, config)
                ),
                default=0,
            ),
        )
        if stage == "catalogue":
            records = [
                record(
                    row["source_key"],
                    {
                        key: row[key]
                        for key in (
                            "url",
                            "item_code",
                            "official_id",
                            "title",
                            "instrument_number",
                            "instrument_year",
                            "source_asserted_status",
                        )
                    },
                    row,
                )
                for row in ledger
            ]
            return {
                "complete": all(
                    report["categories"][key]["observed_catalogue_complete"] for key in definitions
                ),
                "records": records,
                "next_at": next_at,
            }
        originals = _id_outcomes(output, selected, ledger)
        return {
            "complete": all(row["status"] == "acquired" for row in originals),
            "originals": originals,
            "retryable": any(_id_eligible(task, config) for task in tasks),
            "next_at": next_at,
        }


def _id_outcomes(output, selected, ledger):
    by_id = {row["source_key"]: row for row in ledger}
    originals = []
    for entry in selected:
        row = by_id[entry["identity"]]
        files = [
            {**asset(output, item["evidence_path"], item["content_hash"], item["size"]), "url": item["url"]}
            for item in row["assets"]
            if item.get("original_acquired")
        ]
        originals.append(
            {
                "identity": entry["identity"],
                "fingerprint": entry["fingerprint"],
                "status": "acquired" if row["status"] == "originals_acquired" else "gap",
                "assets": files,
                "source_record": row,
            }
        )
    return originals


@offline_only
def persisted_cooldown(config, cycle):
    """Read source-owned cooldown evidence even when its worker result was lost."""
    output = cycle / "source"
    if config["source"] == "egazette":
        import collect_egazette as sg

        path = confined(output, "network-state.json")
        saved = sg.load_json(path, limit=4096) if path.exists() else {}
        if not isinstance(saved, dict) or not set(saved) <= sg.SOURCE_HOSTS:
            raise ValueError("invalid saved official host cooldown")
        values = list(saved.values())
    else:
        path = confined(output, "journal.sqlite3")
        values = []
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.execute("PRAGMA query_only=ON")
                row = conn.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()
                if row:
                    values.append(float(row[0]))
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("invalid saved official host cooldown")
    return max(values, default=0)


@offline_only
def inspect_originals(config, cycle, selected):
    """Recover a lost terminal report using only verified durable receipts."""
    output = cycle / "source"
    if config["source"] == "egazette":
        import collect_egazette as sg

        outcomes = _sg_outcomes(output, selected, sg)
    else:
        import collect_indonesia_official as ind

        with closing(sqlite3.connect((output / "journal.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            ind.verify_receipts(conn, output)
            ind.export_state(conn, output)
        ledger = [json.loads(line) for line in (output / "ledger.jsonl").read_text().splitlines()]
        outcomes = _id_outcomes(output, selected, ledger)
    from periodic_official_budget import confirm_receipts

    confirm_receipts(cycle.parent.parent, cycle.name, outcomes)
    return {
        "complete": all(row["status"] == "acquired" for row in outcomes),
        "originals": outcomes,
        "retryable": False,
        "offline_recovery": True,
        "network_calls": 0,
        "next_at": persisted_cooldown(config, cycle),
    }


def collect(config, cycle: Path, stage: str, selected: list):
    if stage not in {"catalogue", "originals"}:
        raise ValueError("unknown collection stage")
    # Entry points only use verified official transport and raw CAS. In
    # particular neither prepare_library nor stage_originals starts here.
    from periodic_official_budget import confirm_receipts, guarded_transport, summary

    if config["source"] == "egazette":
        import collect_egazette as module

        adapter = _sg
    else:
        import collect_indonesia_official as ind

        module, adapter = sys.modules[ind.SourceVerifier.__module__], _id
    with guarded_transport(module, config, cycle):
        result = adapter(config, cycle, stage, selected)
    if "originals" in result:
        confirm_receipts(cycle.parent.parent, cycle.name, result["originals"])
    result["transfer_budget"] = summary(cycle.parent.parent)
    return result
