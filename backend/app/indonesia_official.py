"""Reconcile the public Setneg catalogue without treating it as all Indonesian law.

The provider exposes independent paged queries, not an atomic snapshot. Complete
here means that the observed provider enumeration balances, never legal approval
or coverage of BPK and other publication authorities.
"""

from __future__ import annotations

from collections import Counter
import re
from urllib.parse import urlencode

SETNEG_ITEMS = {"1.3": "UU", "1.4": "PERPU", "1.5": "PP", "1.6": "PERPRES"}
SETNEG_API = "https://jdih.setneg.go.id/api/hukumproduk/"
RELATION_FIELDS = (
    "terkait",
    "mengubah",
    "mencabut",
    "diubah",
    "dicabut",
    "diubah_sebagian",
    "dicabut_sebagian",
    "mngbh_s",
    "mcbt_s",
    "mencabutsebagian",
    "mengubahsebagian",
)


def original_file_relations(record: dict) -> list[dict]:
    """Link explicit Lampiran filenames in one official record, without legal effect.

    Multiple non-attachment files are ambiguous: the collector must not invent
    which language/version is the attachment's corresponding main document.
    """
    assets = [asset for asset in record.get("assets", []) if asset.get("official_file")]
    attachments, bodies = [], []
    for asset in assets:
        target = (
            attachments
            if re.search(r"(?<![a-z])lampiran(?![a-z])", asset["official_file"]["filename"], re.I)
            else bodies
        )
        target.append(asset)
    if len(bodies) != 1 or not bodies[0].get("original_acquired"):
        return []
    body = bodies[0]
    return [
        {
            "source_url": body["url"],
            "target_url": appendix["url"],
            "relation": "related",
            "locator": {
                "kind": "same_official_record_file_group",
                "official_id": record["official_id"],
                "catalogue_sha256": record["discovery_page"]["sha256"],
                "detail_sha256": record["detail_evidence"]["content_hash"],
                "source": body["official_file"],
                "target": appendix["official_file"],
                "verification": "official_filename_association_only_no_legal_effect_assertion",
            },
        }
        for appendix in attachments
        if appendix.get("original_acquired")
    ]


def source_relation_claims(row: dict, receipt: dict, known_ids: set[str]) -> list[dict]:
    """Preserve provider fields and locators; never assert verified legal effect."""
    result = []
    for field in RELATION_FIELDS:
        raw = row.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        for target in dict.fromkeys(raw.split(",")):
            result.append(
                {
                    "source_field": field,
                    "source_value": raw,
                    "target_official_id": target,
                    "target_source_key": "setneg:" + target if target in known_ids else None,
                    "target_catalogued": target in known_ids,
                    "verification": "source_assertion_only_original_text_not_checked",
                    "evidence": {
                        "url": receipt["final_url"],
                        "sha256": receipt["content_hash"],
                        "json_pointer": "/row/0/" + field,
                    },
                }
            )
    return result


def reconcile_catalogue(pages: list[dict]) -> dict:
    """Validate continuous offsets, stable total, identities and the terminal page."""
    problems = []
    ordered = sorted(pages, key=lambda page: page["offset"])
    totals = {page["total"] for page in ordered}
    if len(totals) != 1:
        problems.append("missing_or_changed_provider_total")
    total = next(iter(totals)) if len(totals) == 1 else None
    expected = 0
    ids = []
    for page in ordered:
        if page["offset"] != expected:
            problems.append("missing_or_overlapping_page")
        rows = page["official_ids"]
        if page["returned"] != len(rows):
            problems.append("page_identity_count_mismatch")
        if len(rows) != max(0, min(page["page_size"], page["total"] - page["offset"])):
            problems.append("page_row_count_mismatch")
        ids.extend(rows)
        expected = page["offset"] + len(rows)
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        problems.append("duplicate_official_identity")
    if total is None or expected != total or len(set(ids)) != total:
        problems.append("provider_total_not_reconciled")
    return {
        "provider": "setneg",
        "provider_total": total,
        "observed_rows": len(ids),
        "unique_official_ids": len(set(ids)),
        "pages": len(ordered),
        "duplicate_official_ids": duplicates,
        "problems": sorted(set(problems)),
        "observed_catalogue_complete": not problems,
        "atomic_snapshot": False,
        "full_checklist_scope_complete": False,
    }


def ledger_record(row: dict, item_code: str, page: dict, *, assets: list[dict] | None = None) -> dict:
    """Keep source assertions separate from verified force and legal relations."""
    instrument = SETNEG_ITEMS[item_code]
    if row["jns"] != instrument:
        raise ValueError("record type differs from the checklist item")
    detail = (
        SETNEG_API
        + "detaildata?"
        + urlencode({"jns": instrument, "no": row["no_peraturan"], "thn": row["tahun"]})
    )
    return {
        "source_key": "setneg:" + row["idperaturan"],
        "url": detail,
        "country": "ID",
        "item_code": item_code,
        "source_type": instrument,
        "official_id": row["idperaturan"],
        "title": row["tentang"],
        "instrument_number": row["no_peraturan"],
        "instrument_year": row["tahun"],
        "status": "catalogued",
        "source_asserted_status": {key: value for key, value in row.items() if "status" in key.lower()},
        "version": {"source_page_sha256": page["content_hash"]},
        "as_of": page["started_at"],
        "assets": assets or [],
        "relations": [],
        "relations_verification": "unverified; source references are not inferred legal relations",
        "discovery_page": {
            "url": page["final_url"],
            "sha256": page["content_hash"],
            "path": page["evidence_path"],
        },
        "legal_review": "unreviewed",
    }
