"""Evaluate article selection independently of answer generation.

Uses the existing country-pack search; no upstream project is imported. Offline
mode disables embedding/reranking calls and measures the lexical baseline only.
Metrics follow the article-level approach in STARD and the evidence-precision
approach in LegalBench-RAG. Neither metric verifies legal interpretation.
"""

import argparse
import json
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from time import monotonic
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.contexts.documents.infrastructure.country_packs import CountryPacks


def identity(item):
    return item["title"], str(item.get("locator", {}).get("article") or "")


def selection_metrics(items, expected, *, k):
    # Repeated versions do not earn repeated credit for the same article.
    selected = list(dict.fromkeys(identity(item) for item in items))[:k]
    relevant = {(row["title"], str(row["article"])) for row in expected}
    hits = set(selected) & relevant
    return {
        "recall": len(hits) / len(relevant) if relevant else None,
        "precision": len(hits) / len(selected) if selected else 0.0,
        "reciprocal_rank": next((1 / rank for rank, key in enumerate(selected, 1) if key in relevant), 0.0),
        "missing": [list(key) for key in sorted(relevant - hits)],
    }


def evaluate(packs, cases, *, limit=6):
    results = []
    for case in cases:
        started = monotonic()
        response = packs.search(case["country"], case["query"], limit, as_of=case.get("as_of"))
        items = (response or {}).get("items", [])
        # Read the actual immutable units used by the chat tool, not snippets.
        originals = [
            packs.read(case["country"], item["metadata"]["source_record_id"]) or item for item in items
        ]
        expected = case["expected"]
        expected_keys = {(row["title"], str(row["article"])) for row in expected}
        total = sum(len(item.get("excerpt", "")) for item in originals)
        relevant = sum(len(item.get("excerpt", "")) for item in originals if identity(item) in expected_keys)
        results.append(
            {
                "id": case["id"],
                "query": case["query"],
                "seconds": round(monotonic() - started, 3),
                "mode": (response or {}).get("mode", "unavailable"),
                "metrics": selection_metrics(originals, expected, k=limit),
                "primary_characters": total,
                "relevant_article_character_share": relevant / total if total else 0.0,
                "forbidden_hits": [
                    list(identity(item))
                    for item in originals
                    if list(identity(item)) in case.get("forbidden", [])
                ],
                "evidence": originals,
                "interpretation_review": "not_run",
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=6, choices=range(1, 13))
    parser.add_argument(
        "--with-providers", action="store_true", help="Allow configured embedding/reranking providers"
    )
    args = parser.parse_args()
    if args.with_providers:
        from app.configuration.environment import load_environment

        load_environment()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    packs = CountryPacks(args.data_dir)
    with ExitStack() as stack:
        if not args.with_providers:
            for index in (packs.semantic, packs.clauses):
                stack.enter_context(
                    patch.object(index, "search", return_value=([], "disabled_for_evaluation"))
                )
            stack.enter_context(
                patch("app.contexts.documents.infrastructure.country_packs.rerank_texts", return_value=None)
            )
        results = evaluate(packs, cases, limit=args.limit)
    report = {
        "created_at": datetime.now().astimezone().isoformat(),
        "scope": "configured_providers" if args.with_providers else "lexical_only_no_network",
        "index_fingerprint": packs.status().get("fingerprint"),
        "note": "Development diagnostic, not an independent legal-quality acceptance. Character share uses whole relevant articles, not LegalBench-RAG's minimal gold spans. Dates and interpretation require separate review.",
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.chmod(0o600)
    for row in results:
        print(json.dumps({key: value for key, value in row.items() if key != "evidence"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
