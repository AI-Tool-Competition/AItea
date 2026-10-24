#!/usr/bin/env python3
"""Run or resume a bounded, isolated resource campaign on this server."""

import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

from verify_ingestion import configure_models, terminate_owned, write_report

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def options_for(args, plan):
    known = [item["id"] for item in plan["items"]]
    selected = args.items.split(",") if args.items else known
    confirmed = args.confirm_items.split(",") if args.confirm_items else []
    if (
        len(selected) != len(set(selected))
        or set(selected) - set(known)
        or len(confirmed) != len(set(confirmed))
        or set(confirmed) - set(selected)
    ):
        raise ValueError("items and confirm-items must be unique identifiers from the selected plan")
    options = {
        "items": selected,
        "confirm_items": confirmed,
        "discovery_batches": args.discovery_batches,
        "originals_per_item": args.originals_per_item,
        "live_models": args.live_models,
    }
    if args.selection:
        from app.contexts.collection.application.campaign_selection import validate_selection

        if (
            args.selection.is_symlink()
            or not args.selection.is_file()
            or args.selection.stat().st_size > 2 * 1024 * 1024
        ):
            raise ValueError("selection must be a regular JSON file of at most 2 MiB")
        options["selection"] = json.loads(args.selection.read_text())
        options["selection"] = validate_selection(plan, options).model_dump()
    return options


def child(
    output,
    plan_id,
    options,
    state,
    extend,
    models_env,
    retry_embeddings,
    revalidate_layout,
    revalidate_native_tables,
    retry_ocr_recovery,
    revalidate_pdf_decode,
    include_source_mapping=False,
):
    if hasattr(os, "setsid"):
        os.setsid()
    os.umask(0o077)
    configure_models(models_env)
    from app.bootstrap import Service
    from app.contexts.collection.application.collection import CollectionWorkers
    from app.contexts.collection.application.resource_campaign import ResourceCampaign
    from app.contexts.collection.infrastructure.archive_inventory import export_inventory

    with (output / "worker.log").open("a", encoding="utf-8", buffering=1) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        workers = None
        try:
            service = Service(output)
            campaign = ResourceCampaign(
                service,
                plan_id,
                options,
                state=state,
                extend=extend,
                checkpoint=lambda payload: write_report(output, payload, "campaign.json"),
            )
            if revalidate_layout:
                campaign.revalidate_layout()
            if revalidate_native_tables:
                campaign.revalidate_native_tables()
            if retry_embeddings:
                campaign.retry_embeddings()
            if retry_ocr_recovery:
                campaign.retry_ocr_recovery()
            if revalidate_pdf_decode:
                campaign.revalidate_pdf_decode()
            workers = CollectionWorkers(service.collection).start()
            started = time.monotonic()
            while campaign.step():
                report = campaign.report()
                report["elapsed_seconds"] = round(time.monotonic() - started, 3)
                write_report(output, report)
                time.sleep(1)
            workers.stop()
            campaign.verify_originals()
            report = campaign.report()
            summary = report["summary"]
            gaps = (
                summary["categories_with_candidates"] < summary["categories"]
                or summary["remaining_pages"] > 0
                or summary["failed_queue_items"] > 0
                or summary["integrity_verified"] != summary["originals"]
                or summary["text_complete"] != summary["originals"]
                or summary["unresolved_relations"]
                or summary["secondary_vectors"]
                or (
                    options["live_models"]
                    and summary["primary_vector_complete"] != summary["primary_originals"]
                )
                or (
                    options.get("selection") and summary["originals"] < len(options["selection"]["materials"])
                )
                or (
                    options["originals_per_item"]
                    and summary["categories_with_originals"] < summary["categories"]
                )
            )
            report["status"] = "finished_with_gaps" if gaps else "finished_scope"
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            inventory = export_inventory(
                output,
                country=campaign.plan["country"],
                owner=campaign.state["owner_id"],
                include_text=True,
                include_source_mapping=include_source_mapping,
            )
            report["inventory"] = inventory | {"path": str(inventory["path"])}
            campaign.state["status"] = report["status"]
            campaign.save()
            write_report(output, report)
        except Exception as error:  # noqa: BLE001 -- child failure must preserve a safe checkpoint
            path = output / "report.json"
            report = json.loads(path.read_text()) if path.exists() else {}
            report.update(status="failed", error_type=type(error).__name__)
            write_report(output, report)
            raise SystemExit(1) from None
        finally:
            if workers:
                workers.stop()
    raise SystemExit(2 if gaps else 0)


def main():
    from app.contexts.collection.application.checklists import load_blueprint_version, load_blueprints
    from app.shared.domain.values import dump

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", default="indonesia-20261003")
    output_default = os.getenv("TEA_CAMPAIGN_OUTPUT")
    parser.add_argument("--output", type=Path, default=output_default, required=not bool(output_default))
    parser.add_argument(
        "--items",
        default=os.getenv("TEA_CAMPAIGN_ITEMS"),
        help="Comma-separated checklist identifiers",
    )
    parser.add_argument(
        "--confirm-items",
        default="",
        help="Explicitly confirmed conditional item identifiers",
    )
    parser.add_argument(
        "--discovery-batches",
        type=int,
        default=os.getenv("TEA_CAMPAIGN_BATCHES", "1"),
        choices=range(1, 21),
        metavar="1..20",
    )
    parser.add_argument(
        "--originals-per-item",
        type=int,
        default=os.getenv("TEA_CAMPAIGN_ORIGINALS", "0"),
        choices=range(201),
        metavar="0..200 (sampling: 0..20)",
    )
    parser.add_argument(
        "--time-budget",
        type=int,
        default=os.getenv("TEA_CAMPAIGN_BUDGET", "600"),
        choices=range(10, 7201),
        metavar="10..7200",
    )
    parser.add_argument("--resume", action="store_true", default=os.getenv("TEA_CAMPAIGN_RESUME") == "1")
    parser.add_argument("--extend", action="store_true", default=os.getenv("TEA_CAMPAIGN_EXTEND") == "1")
    parser.add_argument("--live-models", action="store_true")
    parser.add_argument("--models-env", type=Path)
    parser.add_argument(
        "--include-source-mapping",
        action="store_true",
        help="Export native package, image, table and conversion source mappings in inventory format 5",
    )
    parser.add_argument(
        "--selection",
        type=Path,
        help="Checklist-scoped original URLs, material tiers and association evidence",
    )
    parser.add_argument(
        "--retry-embeddings",
        action="store_true",
        help="On resume, retry failed complete primary embeddings once",
    )
    parser.add_argument(
        "--revalidate-layout",
        action="store_true",
        help="On resume, recheck false table geometry warnings against immutable originals",
    )
    parser.add_argument(
        "--revalidate-native-tables",
        action="store_true",
        help="On resume, recheck unfinished draft table pages using verified native cell geometry",
    )
    parser.add_argument(
        "--revalidate-pdf-decode",
        action="store_true",
        help="On resume, quarantine damaged accepted PDF text in owned draft checkpoints",
    )
    parser.add_argument(
        "--retry-ocr-recovery",
        action="store_true",
        help="On resume, retry draft review pages with a newer region recovery implementation",
    )
    args = parser.parse_args()
    lock = None
    try:
        if args.extend and not args.resume:
            raise ValueError("extend requires --resume")
        if args.retry_embeddings and not (args.resume and args.live_models):
            raise ValueError("retry-embeddings requires --resume and real model configuration")
        if args.revalidate_layout and not args.resume:
            raise ValueError("revalidate-layout requires --resume")
        if args.revalidate_native_tables and not args.resume:
            raise ValueError("revalidate-native-tables requires --resume")
        if args.revalidate_pdf_decode and not args.resume:
            raise ValueError("revalidate-pdf-decode requires --resume")
        if args.retry_ocr_recovery and not (args.resume and args.live_models):
            raise ValueError("retry-ocr-recovery requires --resume and real model configuration")
        if args.live_models != bool(args.models_env) or (args.models_env and not args.models_env.is_file()):
            raise ValueError("real providers require both --live-models and an existing --models-env file")
        plan = load_blueprints().get(args.plan_id)
        if not plan:
            raise ValueError("unknown collection plan")
        if args.output.is_symlink():
            raise ValueError("output must be a real independent directory")
        output = args.output.resolve()
        if args.resume:
            path = output / "campaign.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("resume requires an existing campaign checkpoint")
        elif output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("output must be new or empty; use --resume only for this campaign")
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(output / "campaign.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((output / "campaign.json").read_text()) if args.resume else None
        if state is not None:
            if (
                not isinstance(state, dict)
                or state.get("format") != 1
                or state.get("plan_id") != args.plan_id
            ):
                raise ValueError("resume requires the same frozen plan and campaign options")
            plan = load_blueprint_version(args.plan_id, state.get("plan_version"))
        options = options_for(args, plan)
        if (
            not 1 <= args.discovery_batches <= 20
            or not 0 <= args.originals_per_item <= (200 if options.get("selection") else 20)
            or not 10 <= args.time_budget <= 7200
        ):
            raise ValueError("campaign budgets exceed the supported bounds")
        if state is not None and (
            state.get("format") != 1
            or state.get("plan_hash") != hashlib.sha256(dump(plan).encode()).hexdigest()
            or (state.get("options") != options and not args.extend)
        ):
            raise ValueError("resume requires the same frozen plan and campaign options")
        if args.extend:
            from app.contexts.collection.application.resource_campaign import validate_extension

            validate_extension(state["options"], options)
        if state is None and any(path.name != "campaign.lock" for path in output.iterdir()):
            raise ValueError("output changed while acquiring the campaign lock")
        previous_report = output / "report.json"
        if state is not None and previous_report.is_file():
            # Keep the preceding budget, failures and timing when a later
            # invocation resumes or extends the same immutable originals.
            previous_report.replace(output / f"report-{time.time_ns()}.json")
    except (ValueError, OSError, TypeError) as error:
        if lock is not None:
            os.close(lock)
        parser.error(str(error))
    process = multiprocessing.get_context("spawn").Process(
        target=child,
        args=(
            output,
            args.plan_id,
            options,
            state,
            args.extend,
            str(args.models_env.resolve()) if args.models_env else None,
            args.retry_embeddings,
            args.revalidate_layout,
            args.revalidate_native_tables,
            args.retry_ocr_recovery,
            args.revalidate_pdf_decode,
            args.include_source_mapping,
        ),
    )
    try:
        process.start()
        process.join(args.time_budget - 3)
        if process.is_alive():
            terminate_owned(process)
            path = output / "report.json"
            report = json.loads(path.read_text()) if path.exists() else {}
            report["status"] = "budget_reached"
            write_report(output, report)
            code = 3
        else:
            code = process.exitcode or 0
    except KeyboardInterrupt:
        terminate_owned(process)
        code = 130
    finally:
        os.close(lock)
    print(f"Campaign report: {output / 'report.json'}")
    print(f"Resume with the same plan and limits: --output {output} --resume")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
