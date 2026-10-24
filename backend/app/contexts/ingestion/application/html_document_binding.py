"""Bind official HTML iframes to independently stored and indexed PDF versions."""

from __future__ import annotations

import json
from copy import deepcopy


def bind_ready_official_documents(service, document, manifest, source_context):
    """Attach only same-source PDFs discovered from this exact saved page version."""
    from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete
    from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import (
        LINKED_DOCUMENT_PROTOCOL,
        resolved_url,
    )
    from app.shared.infrastructure.http.download import _validated_url

    if (
        not source_context
        or document.get("owner_id") != SYSTEM_OWNER_ID
        or manifest.get("parser") != "html-basic"
        or manifest.get("source_hash") != document.get("content_hash")
    ):
        return manifest
    source, parent_import = source_context
    if (
        parent_import.get("source_id") != source.get("id")
        or parent_import.get("source_url") != document.get("source_url")
        or parent_import.get("content_hash") != document.get("content_hash")
    ):
        return manifest
    profile = service.embedding_fingerprint()
    if not profile:
        return manifest

    from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import _html_source

    html_source, objects = _html_source(manifest)
    candidates = []
    approved_hosts = set(source.get("allowed_hosts", []))
    for row in objects:
        attrs = row.get("url_attributes", [])
        locator = row.get("locator") or {}
        title = str((row.get("metadata") or {}).get("title", "")).strip().casefold()
        if (
            row.get("tag") != "iframe"
            or row.get("parent_object_key") is not None
            or row.get("duplicate_url_attributes") is not False
            or locator.get("source_hash") != document.get("content_hash")
            or locator.get("source_boundary_complete") is not True
            or not isinstance(attrs, list)
            or len(attrs) != 1
            or attrs[0].get("attribute") != "src"
            or not title.endswith(".pdf")
        ):
            continue
        try:
            requested = resolved_url(document["source_url"], attrs[0]["decoded_value"])
            _, host = _validated_url(requested)
        except (KeyError, TypeError, ValueError):
            continue
        if host not in approved_hosts:
            continue
        candidates.append((row, attrs[0], requested))

    if not candidates:
        return manifest
    bindings = []
    with service.db.connect() as conn:
        for row, url_attribute, requested in candidates:
            targets = conn.execute(
                """SELECT i.source_id,i.source_url,i.content_hash,i.mime_type,i.document_id,
                i.discovery_parent_url,i.discovery_parent_hash,d.owner_id,d.role,d.status,d.version,d.source_url AS document_url,
                d.content_hash AS document_hash,d.manifest,r.generation,r.profile,r.state,
                r.source_hash AS index_hash,r.source_version AS index_version,r.dataset_id,r.remote_id
                FROM official_source_imports i
                JOIN documents d ON d.id=i.document_id AND d.owner_id=?
                JOIN ragflow_documents r ON r.document_id=d.id AND r.owner_id=d.owner_id
                WHERE i.source_id=? AND i.country=? AND i.source_url=? AND i.content_hash=d.content_hash
                    AND d.source_url=i.source_url
                    AND i.status='stored' AND i.mime_type='application/pdf'
                    AND i.discovery_parent_url=? AND i.discovery_parent_hash=?
                    AND d.role='primary' AND d.status='ready' AND d.mime_type='application/pdf'
                    AND r.profile=? AND r.state='ready' AND r.source_hash=d.content_hash
                    AND r.source_version=d.version AND r.dataset_id IS NOT NULL AND r.remote_id IS NOT NULL
                ORDER BY i.updated_at DESC,r.updated_at DESC""",
                (
                    SYSTEM_OWNER_ID,
                    parent_import["source_id"],
                    parent_import["country"],
                    requested,
                    parent_import["source_url"],
                    parent_import["content_hash"],
                    profile,
                ),
            ).fetchall()
            for target in targets:
                try:
                    target_manifest = deepcopy(json.loads(target["manifest"]))
                except (TypeError, ValueError):
                    continue
                if target_manifest.get("source_hash") != target["document_hash"] or not text_complete(
                    target_manifest
                ):
                    continue
                bindings.append(
                    {
                        "protocol": LINKED_DOCUMENT_PROTOCOL,
                        "object_key": row["object_key"],
                        "parent_source_url": parent_import["source_url"],
                        "parent_source_hash": parent_import["content_hash"],
                        "parent_locator": deepcopy(row["locator"]),
                        "source_fragment_sha256": row["source_fragment_sha256"],
                        "url_attribute": deepcopy(url_attribute),
                        "requested_url": requested,
                        "source_id": parent_import["source_id"],
                        "target_document_id": target["document_id"],
                        "target_source_url": target["source_url"],
                        "target_version": target["version"],
                        "target_hash": target["document_hash"],
                        "target_mime_type": target["mime_type"],
                        "target_state": target["state"],
                        "target_text_complete": True,
                        "target_ragflow_generation": target["generation"],
                        "target_embedding_profile": target["profile"],
                        "target_dataset_id": target["dataset_id"],
                        "target_remote_id": target["remote_id"],
                    }
                )
                break
    if not bindings:
        return manifest
    result = deepcopy(manifest)
    existing = result["html_source"].get("linked_documents", [])
    if not isinstance(existing, list):
        raise ValueError("HTML 已保存的关联 PDF 记录无效。")
    replaced = {value["object_key"] for value in bindings}
    result["html_source"]["linked_documents"] = [
        value for value in existing if not isinstance(value, dict) or value.get("object_key") not in replaced
    ] + bindings
    return result
