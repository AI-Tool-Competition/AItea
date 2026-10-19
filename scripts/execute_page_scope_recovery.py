"""Bounded, explicitly authorized v3 page recovery; default is read-only preflight."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--concurrency", type=int, choices=range(1, 65), default=64)
    parser.add_argument("--models-env", type=Path)
    parser.add_argument("--live-models", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        from app.page_checkpoint_recovery import isolated_directory, readonly_service
        from app.page_scope_execution import execute, preflight

        if not 0 < args.plan.stat().st_size <= 2 * 1024 * 1024:
            raise ValueError("execution_plan_exceeds_2MiB")
        plan = json.loads(args.plan.read_bytes())
        directory = isolated_directory(args.data_dir)
        _, report = preflight(readonly_service(directory), plan)
        if args.apply:
            if not args.live_models or not args.models_env or not args.output:
                raise ValueError("apply_requires_live_models_models_env_and_output")
            from verify_ingestion import configure_models

            configure_models(args.models_env)
            os.environ.update(
                VISION_OCR_ENABLED="true",
                VISION_OCR_WORKERS=str(args.concurrency),
                VISION_OCR_ADAPTIVE="true",
            )
            from app.service import Service

            report = execute(Service(directory), plan, args.output, concurrency=args.concurrency)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 3 if report.get("status") == "needs_review" else 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)[:500]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
