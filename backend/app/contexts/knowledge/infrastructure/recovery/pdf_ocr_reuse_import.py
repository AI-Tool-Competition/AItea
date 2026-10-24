"""Offline derivation of a verified page reuse proof across account CAS roots.

The original v1 proof and both saved page checkpoints remain immutable in CAS.
There was no target-before-reuse checkpoint in v1: importing cannot invent one.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

from app.contexts.knowledge.infrastructure.parsing.pdf_limits import ARCHIVE_MAX_PAGES
from app.contexts.knowledge.infrastructure.recovery import pdf_ocr_reuse as reuse

PROTOCOL = "cross-owner-pdf-page-reuse-import-v1"
ARCHIVE_PROTOCOL = "pdf-page-reuse-source-archive-v1"
MAX_ARCHIVE_BYTES = 2 * reuse.MAX_PAGE_BYTES + 64 * 1024


def _require(condition, reason):
    if not condition:
        raise ValueError("OCR_reuse_import_" + reason)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _identity(doc):
    return {
        "document_id": doc["id"],
        "owner_sha256": _sha(doc["owner_id"].encode()),
        "version": doc["version"],
        "content_hash": doc["content_hash"],
        "source_url": doc["source_url"],
        "size": doc["size"],
        "role": doc["role"],
    }


def _page(manifest, number):
    found = [p for p in manifest.get("pages", []) if p.get("page") == number]
    _require(len(found) == 1, "physical_page_missing_or_ambiguous")
    return found[0]


def _cas_path(owner_hash, digest):
    return f"files/blobs/{owner_hash}/{digest[:2]}/{digest[2:4]}/{digest}"


def _validate_pair(target, donor, page, donor_page, paths):
    """Use the saved provider recipe, never today's model configuration."""
    proof = page["ocr"]["reuse"]
    number = page.get("page")
    _require(type(number) is int and 1 <= number <= ARCHIVE_MAX_PAGES, "invalid_page")
    _require(
        target["document_id"] != donor["document_id"]
        and target["role"] == donor["role"] == "primary"
        and all(
            target[k] == donor[k] for k in ("owner_sha256", "content_hash", "source_url", "size", "version")
        )
        and page.get("source_hash") == target["content_hash"]
        and donor_page.get("source_hash") == donor["content_hash"],
        "identity_mismatch",
    )
    profile = donor_page["ocr"]["provider_profile"]
    _require(
        isinstance(profile, dict)
        and set(profile)
        == {
            "version",
            "base_url",
            "requested_model",
            "prompt_version",
            "prompt_sha256",
            "request_recipe",
            "fingerprint",
        }
        and profile["version"] == 1
        and profile["fingerprint"]
        == reuse._json_hash({k: v for k, v in profile.items() if k != "fingerprint"}),
        "provider_profile_mismatch",
    )
    checked = reuse._successful(donor_page, page, profile)
    _require(checked is not None, "donor_checkpoint_invalid")
    metadata = checked["ocr"]
    image = metadata["image_blob"]
    digest = metadata["image_sha256"]
    _require(
        isinstance(digest, str)
        and digest in paths
        and image.get("content_hash") == digest
        and image.get("storage_path") == _cas_path(donor["owner_sha256"], digest)
        and image.get("mime_type") == metadata.get("mime_type") == "image/png"
        and type(image.get("size")) is int
        and 0 < image["size"] <= 8 * 1024 * 1024,
        "PNG_binding_invalid",
    )
    raw = paths[digest].read_bytes()
    _require(
        len(raw) == image["size"] and _sha(raw) == digest and raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "PNG_bytes_changed",
    )
    expected = {
        "protocol": reuse.PROTOCOL,
        "source_document_id": donor["document_id"],
        "source_document_version": donor["version"],
        "source_content_hash": donor["content_hash"],
        "source_page": number,
        "source_page_checkpoint_sha256": reuse._json_hash(donor_page),
        "target_document_id": target["document_id"],
        "target_document_version": target["version"],
        "profile_fingerprint": profile["fingerprint"],
        "png_content_hash": digest,
        "png_size": len(raw),
        "native_text_sha256": _sha(page["native_text"].encode()),
        "reasons_sha256": reuse._json_hash(page["reasons"]),
        "text_sha256": _sha(checked["text"].encode()),
        "origin_attempts": metadata["attempts"],
        "target_model_calls": 0,
        "local_bounded_render_verified": True,
    }
    _require(proof == expected, "proof_mismatch")
    active_ocr = {k: v for k, v in page["ocr"].items() if k != "reuse_import"}
    _require(
        active_ocr == dict(metadata, attempts=0, reuse=expected)
        and page.get("status") == "ocr_complete"
        and page.get("text") == checked["text"]
        and page.get("printed_page") == checked.get("printed_page"),
        "target_checkpoint_changed",
    )
    return raw


def _protocol_count(value, protocol):
    if isinstance(value, dict):
        return int(value.get("protocol") == protocol) + sum(
            _protocol_count(v, protocol) for v in value.values()
        )
    if isinstance(value, list):
        return sum(_protocol_count(v, protocol) for v in value)
    return 0


def prepare(documents, paths):
    """Read-only, bounded physical rendering; no OCR, HTTP or model requests."""
    from app.contexts.knowledge.infrastructure.parsing.parsing import render_pdf_page_bounded

    by_id = {d["id"]: d for d in documents}
    records, rendered = {}, {}
    for doc in documents:
        manifest = doc["parsed_manifest"]
        _require(not _protocol_count(manifest, PROTOCOL), "derived_history_requires_system_migration")
        count = 0
        for page in manifest.get("pages", []):
            proof = (page.get("ocr") or {}).get("reuse")
            if proof is None:
                continue
            count += 1
            donor = by_id.get(proof.get("source_document_id"))
            _require(donor is not None, "selected_donor_required")
            number = page.get("page")
            _require(_page(manifest, number) is page, "target_physical_page_ambiguous")
            donor_page = _page(donor["parsed_manifest"], number)
            _require(
                all(len(_encoded(p)) <= reuse.MAX_PAGE_BYTES for p in (page, donor_page)),
                "page_scope_exceeded",
            )
            image = _validate_pair(_identity(doc), _identity(donor), page, donor_page, paths)
            key = (doc["content_hash"], number)
            if key not in rendered:
                original = paths[doc["content_hash"]]
                _require(
                    original.stat().st_size == doc["size"]
                    and _sha(original.read_bytes()) == doc["content_hash"],
                    "original_changed",
                )
                rendered[key] = render_pdf_page_bounded(original.read_bytes(), number)
            actual = rendered[key]
            _require(
                all(actual.get(k) == donor_page["ocr"].get(k) for k in reuse._RENDER_KEYS)
                and base64.b64decode(actual["image_base64"], validate=True) == image,
                "bounded_PNG_render_mismatch",
            )
            archive = {
                "protocol": ARCHIVE_PROTOCOL,
                "source_document": _identity(doc),
                "source_donor": _identity(donor),
                "source_manifest_sha256": reuse._json_hash(manifest),
                "source_donor_manifest_sha256": reuse._json_hash(donor["parsed_manifest"]),
                "source_page": copy.deepcopy(page),
                "source_donor_page": copy.deepcopy(donor_page),
                "source_proof": copy.deepcopy(proof),
                "target_before_reuse_checkpoint_available": False,
                "bounded_render": {k: actual[k] for k in reuse._RENDER_KEYS},
                "import_model_calls": 0,
            }
            _require(len(_encoded(archive)) <= MAX_ARCHIVE_BYTES, "archive_scope_exceeded")
            records[(doc["id"], number)] = archive
        _require(_protocol_count(manifest, reuse.PROTOCOL) == count, "misplaced_or_unknown_proof")
    return records


def derive(archive, target, donor, page, donor_page, *, expected_page, expected_donor, blobs, target_dir):
    """Only import-time identity/path changes may differ from the source pages."""
    _require(donor_page == expected_donor, "target_donor_changed")
    existing_receipt = page.get("ocr", {}).get("reuse_import")
    if existing_receipt is not None:
        _require(existing_receipt.get("archive_sha256") == _sha(_encoded(archive)), "source_archive_changed")
        return page  # Caller validates the complete active/archived binding again.
    _require(page == expected_page, "target_page_changed")
    raw = _encoded(archive)
    blob = blobs.put_bytes(target["owner_id"], raw)
    result = copy.deepcopy(page)
    original_proof = archive["source_proof"]
    proof = dict(
        original_proof,
        source_document_id=donor["id"],
        source_document_version=donor["version"],
        target_document_id=target["id"],
        target_document_version=target["version"],
        source_page_checkpoint_sha256=reuse._json_hash(donor_page),
    )
    result["ocr"]["reuse"] = proof
    result["ocr"]["reuse_import"] = {
        "protocol": PROTOCOL,
        "archive_sha256": blob.digest,
        "archive_size": blob.size,
        "archive_path": str(blob.path.relative_to(Path(target_dir).resolve())),
        "source_owner_sha256": archive["source_document"]["owner_sha256"],
        "target_owner_sha256": _sha(target["owner_id"].encode()),
        "source_proof_sha256": reuse._json_hash(original_proof),
        "derived_proof_sha256": reuse._json_hash(proof),
        "derived_page_sha256": reuse._json_hash(result),
        "import_model_calls": 0,
    }
    return result


def validate_archive(page, paths, directory):
    """Validate an archived proof without following its old private paths."""
    receipt = page["ocr"]["reuse_import"]
    _require(receipt.get("protocol") == PROTOCOL, "unknown_receipt")
    digest = receipt.get("archive_sha256")
    _require(digest in paths, "archive_missing")
    path = paths[digest]
    _require(
        receipt.get("archive_path")
        == str(path.relative_to(directory))
        == _cas_path(receipt.get("target_owner_sha256"), digest)
        and type(receipt.get("archive_size")) is int
        and 0 < receipt["archive_size"] <= MAX_ARCHIVE_BYTES
        and path.stat().st_size == receipt["archive_size"],
        "archive_CAS_mismatch",
    )
    raw = path.read_bytes()
    _require(_sha(raw) == digest, "archive_hash_mismatch")
    archive = json.loads(raw)
    _require(
        archive.get("protocol") == ARCHIVE_PROTOCOL
        and archive.get("target_before_reuse_checkpoint_available") is False
        and archive.get("import_model_calls") == receipt.get("import_model_calls") == 0
        and archive["source_proof"] == archive["source_page"]["ocr"]["reuse"]
        and receipt["source_owner_sha256"] == archive["source_document"]["owner_sha256"],
        "archive_identity_mismatch",
    )
    _require(
        archive.get("bounded_render")
        == {key: archive["source_donor_page"]["ocr"][key] for key in reuse._RENDER_KEYS},
        "archived_render_mismatch",
    )
    _validate_pair(
        archive["source_document"],
        archive["source_donor"],
        archive["source_page"],
        archive["source_donor_page"],
        paths,
    )
    current = copy.deepcopy(page)
    current["ocr"].pop("reuse_import")
    _require(
        receipt["source_proof_sha256"] == reuse._json_hash(archive["source_proof"])
        and receipt["derived_proof_sha256"] == reuse._json_hash(current["ocr"]["reuse"])
        and receipt["derived_page_sha256"] == reuse._json_hash(current),
        "derived_checkpoint_mismatch",
    )
    return archive


def validate_imported(target, donor, page, donor_page, paths, directory):
    archive = validate_archive(page, paths, directory)
    for current, original in ((target, archive["source_document"]), (donor, archive["source_donor"])):
        manifest = current["manifest"]
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        origin = manifest.get("collection_import", {})
        _require(
            origin.get("source_owner_sha256") == original["owner_sha256"]
            and origin.get("source_document_id") == original["document_id"]
            and origin.get("source_version") == original["version"]
            and origin.get("source_hash") == original["content_hash"],
            "derived_source_identity_mismatch",
        )
    _require(
        _sha(target["owner_id"].encode()) == page["ocr"]["reuse_import"]["target_owner_sha256"]
        and all(
            _identity(target)[k] == archive["source_document"][k]
            for k in ("content_hash", "source_url", "size", "role")
        )
        and all(
            _identity(donor)[k] == archive["source_donor"][k]
            for k in ("content_hash", "source_url", "size", "role")
        ),
        "derived_identity_mismatch",
    )
    _validate_pair(_identity(target), _identity(donor), page, donor_page, paths)
    # Reconstruct the only permitted differences. Old private paths are aliases,
    # never file-read authority, and the original proof is not recursively edited.
    from app.contexts.collection.application.collection_import import _path_key, _rewrite

    aliases = {}

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _path_key(key) and isinstance(item, str) and Path(item).name in paths:
                    aliases[item] = Path(item).name
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(archive["source_page"])
    collect(archive["source_donor_page"])
    destinations = {digest: str(path.relative_to(directory)) for digest, path in paths.items()}
    identities = {
        archive["source_document"]["document_id"]: {
            "document_id": target["id"],
            "version": target["version"],
        },
        archive["source_donor"]["document_id"]: {"document_id": donor["id"], "version": donor["version"]},
    }
    expected = _rewrite(
        archive["source_page"], aliases, destinations, identities, target_owner=target["owner_id"]
    )
    expected["ocr"]["reuse"] = page["ocr"]["reuse"]
    current = copy.deepcopy(page)
    current["ocr"].pop("reuse_import")
    expected_donor = _rewrite(
        archive["source_donor_page"], aliases, destinations, identities, target_owner=target["owner_id"]
    )
    _require(current == expected and donor_page == expected_donor, "archived_checkpoint_binding_mismatch")
