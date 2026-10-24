"""HTML body image originals, OCR checkpoints and unknown-source boundaries."""

import base64
import hashlib
import io
import json
from contextlib import closing
from copy import deepcopy

import pytest
from app.contexts.knowledge.infrastructure.parsing.html_parsing import VISUAL_PENDING_WARNING
from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import ocr_work_pending, text_complete
from app.contexts.knowledge.infrastructure.parsing.native import parse_file
from app.contexts.knowledge.infrastructure.parsing.native_mapping_inventory import mappings, validate_mapping
from app.contexts.knowledge.infrastructure.providers import native_asset_ocr as native
from app.contexts.knowledge.infrastructure.providers import vision as vision
from app.contexts.knowledge.infrastructure.providers.html_linked_asset_ocr import (
    bind_linked_image_bounded,
    read_linked_images,
)
from app.shared.infrastructure.blob_store import BlobStore
from PIL import Image


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def raster(*, multi=False):
    output = io.BytesIO()
    image = Image.new("RGB", (80, 40), "white")
    options = {"save_all": True, "append_images": [Image.new("RGB", image.size, "black")]} if multi else {}
    image.save(output, format="GIF" if multi else "PNG", **options)
    return output.getvalue()


def binding(
    tmp_path, *, graphic='<img src="/flow chart.png?x=1&amp;y=2" alt="NOT OCR TEXT">', raw=None, head=""
):
    body = f'{head}<main><nav><img src="logo.png"></nav><p>Keep import permits.</p>{graphic}</main>'.encode()
    manifest = parse_file("law.html", body)
    cas, raw = BlobStore(tmp_path / "files"), raster() if raw is None else raw
    blob = cas.put_bytes("owner", raw)
    reference = {
        "content_hash": blob.digest,
        "size": blob.size,
        "storage_path": str(blob.path.relative_to(tmp_path)),
    }
    result = bind_linked_image_bounded(
        body,
        manifest,
        object_key=manifest["html_source"]["objects"][0]["object_key"],
        source_url="https://example.org/law",
        image=raw,
        image_blob=reference,
    )
    return body, result, raw


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)


def transcript(*, uncertain=False):
    return {
        "text": "The complete printed image text.",
        "complete": not uncertain,
        "is_blank": False,
        "uncertain_spans": ["model_reported_incomplete"] if uncertain else [],
        "model": "actual-test-model",
        "requested_model": "test-model",
        "prompt_version": vision.IMAGE_PROMPT_VERSION,
        "review_status": "unreviewed",
        "physical_pages_known": False,
        "finish_reason": "stop",
    }


def test_verified_original_and_png_checkpoint_precede_model_and_completed_resume_skips_it(
    tmp_path, enabled, monkeypatch
):
    body, manifest, raw = binding(tmp_path)
    manifest["warnings"].append("Independent source version requires review.")
    assert ocr_work_pending(manifest) and not text_complete(manifest)
    assert len(manifest["html_source"]["objects"]) == 1
    asset = native.assets(manifest)[0]
    assert asset["requested_url"] == "https://example.org/flow%20chart.png?x=1&y=2"
    assert asset["url_attribute"]["raw_value"] == "/flow chart.png?x=1&amp;y=2"
    assert read_linked_images(tmp_path, "owner", manifest) == {asset["asset_key"]: raw}
    calls, events = [], []

    def image(rendered):
        png = base64.b64decode(rendered["image_base64"])
        assert rendered["original_image_sha256"] == digest(raw) and "page" not in rendered
        blob = BlobStore(tmp_path / "files").put_bytes("owner", png)
        events.append("pixels")
        return {
            "content_hash": blob.digest,
            "size": blob.size,
            "storage_path": str(blob.path.relative_to(tmp_path)),
        }

    def checkpoint(value):
        native.apply_asset_checkpoint(manifest, value)
        events.append(value["asset_status"])

    def recognize(rendered):
        stored = native.assets(manifest)[0]
        assert stored["ocr"]["attempts"] == 1 and stored["ocr"]["image_blob"]
        assert events[:2] == ["pixels", "pending_vision"]
        calls.append(rendered)
        return transcript()

    monkeypatch.setattr(vision, "transcribe_image", recognize)
    result = native.ocr_native_assets_bounded(
        body, manifest, image_contents={asset["asset_key"]: raw}, on_image=image, on_asset=checkpoint
    )
    assert len(calls) == 1 and text_complete(result) and not ocr_work_pending(result)
    assert result["warnings"] == ["Independent source version requires review."]
    assert result["quality"]["resolved_warnings"] == [VISUAL_PENDING_WARNING]
    assert len(result["chunks"]) == len(manifest["chunks"]) + 1
    assert "NOT OCR TEXT" not in " ".join(row["text"] for row in result["chunks"])
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("Completed image was resent"))
    resumed = native.ocr_native_assets_bounded(body, result)
    assert resumed == result
    exported = list(mappings(result, "a" * 64))
    image_row = next(row for kind, _, row in exported if kind == "html_linked_image")
    assert image_row["original_image_blob"]["content_hash"] == digest(raw)
    assert image_row["requested_url"]["target_export_status"] == "redacted"
    assert image_row["ocr"]["text_reference"]["json_pointer"].endswith("/ocr/text")
    for _, _, row in exported:
        validate_mapping(row, digest(body))


@pytest.mark.parametrize(
    "graphic",
    [
        '<picture><img src="plain.png"></picture>',
        '<svg><image href="plain.png"/></svg>',
        '<img src="plain.png" srcset="large.png 2x">',
        '<img src="plain.png" src="other.png">',
        "<canvas>fallback</canvas>",
        '<img src="https://127.0.0.1/private.png">',
        '<img src="data:image/png;base64,AAAA">',
    ],
)
def test_unknown_or_ambiguous_sources_cannot_bind(tmp_path, graphic):
    with pytest.raises(ValueError):
        binding(tmp_path, graphic=graphic)


def test_base_href_is_not_guessed_but_script_and_comment_literals_do_not_change_the_base(tmp_path):
    with pytest.raises(ValueError, match="base/href"):
        binding(tmp_path / "actual", head='<head><base href="https://other.org/path/"></head>')
    body, manifest, _ = binding(
        tmp_path / "literal",
        head='<!-- <base href="https://other.org"> --><script>const s = \'<base href="https://other.org">\';</script>',
    )
    assert native.assets(manifest)[0]["source_hash"] == digest(body)


def test_html_original_above_pixel_limit_is_preserved_without_sending_a_model_request(
    tmp_path, enabled, monkeypatch
):
    output = io.BytesIO()
    Image.new("RGB", (4000, 3001), "white").save(output, format="PNG")
    body, manifest, raw = binding(tmp_path, raw=output.getvalue())
    key = native.assets(manifest)[0]["asset_key"]
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("Oversized image was sent"))
    result = native.ocr_native_assets_bounded(body, manifest, image_contents={key: raw})
    asset = native.assets(result)[0]
    assert asset["asset_status"] == "ocr_failed" and "像素" in asset["error"]
    assert read_linked_images(tmp_path, "owner", result)[key] == raw


def test_unknown_second_object_keeps_fulltext_gate_even_after_actual_image_success(
    tmp_path, enabled, monkeypatch
):
    body, manifest, raw = binding(tmp_path, graphic='<img src="ok.png"><canvas>unknown drawing</canvas>')
    key = native.assets(manifest)[0]["asset_key"]
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: transcript())
    result = native.ocr_native_assets_bounded(body, manifest, image_contents={key: raw})
    assert VISUAL_PENDING_WARNING in result["warnings"]
    assert not result["quality"].get("resolved_warnings")
    assert not text_complete(result)
    assert result["quality"]["pending_objects"] == ["html-object-000002"]


def test_partial_vision_and_multiframe_sources_are_never_complete(tmp_path, enabled, monkeypatch):
    body, manifest, raw = binding(tmp_path)
    key = native.assets(manifest)[0]["asset_key"]
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: transcript(uncertain=True))
    result = native.ocr_native_assets_bounded(body, manifest, image_contents={key: raw})
    assert native.assets(result)[0]["asset_status"] == "ocr_uncertain" and not text_complete(result)
    other = tmp_path / "multiframe"
    body, manifest, raw = binding(other, raw=raster(multi=True))
    key = native.assets(manifest)[0]["asset_key"]
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("Unmapped second frame was sent"))
    result = native.ocr_native_assets_bounded(body, manifest, image_contents={key: raw})
    assert native.assets(result)[0]["asset_status"] == "ocr_failed" and not text_complete(result)


def test_html_long_infographic_has_explicit_pixel_bound_without_changing_word_limit(
    tmp_path, enabled, monkeypatch
):
    output = io.BytesIO()
    Image.new("RGB", (1838, 5993), "white").save(output, format="PNG")
    body, manifest, raw = binding(tmp_path, raw=output.getvalue())
    key = native.assets(manifest)[0]["asset_key"]
    calls = []

    def recognize(rendered):
        calls.append((rendered["width"], rendered["height"], rendered["pixel_transform"]))
        return transcript(uncertain=True)

    monkeypatch.setattr(vision, "transcribe_image", recognize)
    result = native.ocr_native_assets_bounded(body, manifest, image_contents={key: raw})
    assert calls == [(1838, 5993, "exif_orientation_then_white_alpha_composite_no_resize")]
    assert not text_complete(result) and native.MAX_PIXELS == 6_000_000
    assert native.MAX_HTML_PIXELS == 12_000_000


def test_changed_parent_reference_pixels_and_cross_account_cas_are_rejected(tmp_path, enabled, monkeypatch):
    body, manifest, raw = binding(tmp_path)
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("Changed source was sent"))
    with pytest.raises(ValueError):
        read_linked_images(tmp_path, "other-owner", manifest)
    with pytest.raises(ValueError):
        read_linked_images(tmp_path, "owner", manifest, source_url="https://other.org/law")
    key = native.assets(manifest)[0]["asset_key"]
    result = native.ocr_native_assets_bounded(body, manifest, image_contents={key: raw + b"changed"})
    assert native.assets(result)[0]["asset_status"] == "ocr_failed"
    changed = deepcopy(manifest)
    changed["html_source"]["objects"][0]["locator"]["char_start"] += 1
    with pytest.raises(ValueError):
        native.assets(changed)
    with pytest.raises(ValueError):
        native.ocr_native_assets_bounded(body + b"changed", manifest)
    path = tmp_path / manifest["html_source"]["linked_images"][0]["original_image_blob"]["storage_path"]
    original = path.with_name(path.name + "-saved")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(OSError):
        read_linked_images(tmp_path, "owner", manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("html_source", "broken"),
        ("objects", ["broken"]),
        ("objects", None),
        ("locator", "broken"),
        ("url_attributes", [None]),
    ],
)
def test_malformed_source_maps_fail_closed_without_attribute_errors(tmp_path, field, value):
    _, manifest, _ = binding(tmp_path)
    if field == "html_source":
        manifest[field] = value
    elif field == "objects":
        manifest["html_source"][field] = value
    else:
        manifest["html_source"]["objects"][0][field] = value
    assert not text_complete(manifest)
    assert not ocr_work_pending(manifest)
    with pytest.raises(ValueError):
        native.assets(manifest)


@pytest.mark.parametrize("level", ["primary", "secondary", "attachment"])
@pytest.mark.parametrize("redirected", [False, True])
def test_existing_worker_textualizes_only_main_files_and_secondary_laws_never_get_vectors(
    tmp_path, monkeypatch, level, redirected
):
    from app.bootstrap import Service
    from app.contexts.knowledge.infrastructure.providers import embedding as ingestion_embedding

    service = Service(tmp_path / "business")
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES('owner','owner',0,'2026-10-13')")
    body, _, raw = binding(tmp_path / "fixture")
    with service.db.transaction() as conn:
        blob = service.archive.blobs.put_bytes("owner", body)
        doc = dict(
            service.archive.store_original(
                conn,
                "owner",
                blob,
                "law.html",
                "text/html",
                "https://example.org/law",
                collection="country:SG",
                level=level,
                tags=[],
            )
        )
        image = service.archive.blobs.put_bytes("owner", raw)
        original_image = {
            "content_hash": image.digest,
            "size": image.size,
            "storage_path": str(image.path.relative_to(service.db.data_dir)),
        }
        parent_source_url = doc["source_url"]
        if redirected:
            parent_source_url = "https://cdn.example.org/download/law.html"
            source = service.save_source(
                "owner",
                {
                    "name": "Redirect proof",
                    "url": doc["source_url"],
                    "enabled": False,
                    "interval_hours": 24,
                    "tags": [],
                    "plan": {"collection": "country:SG", "discover_attachments": False},
                },
                conn=conn,
            )
            run = service.collection.enqueue(
                "owner", source["id"], {"client_request_id": "redirect-parent"}, conn=conn
            )
            conn.execute(
                "UPDATE collection_items SET document_id=?,status='stored',final_url=? WHERE run_id=?",
                (doc["id"], parent_source_url, run["id"]),
            )
        manifest = bind_linked_image_bounded(
            body,
            parse_file("law.html", body),
            object_key="html-object-000001",
            source_url=parent_source_url,
            image=raw,
            image_blob=original_image,
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), doc["id"]))
    monkeypatch.setattr(
        ingestion_embedding,
        "embed_texts",
        lambda *_: pytest.fail("Text processing made unexpected vector request"),
    )
    calls = []
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)

    def recognize(rendered):
        with closing(service.db.connect()) as conn:
            current = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
            )
        asset = native.assets(current)[0]
        assert asset["ocr"]["attempts"] == 1
        assert (service.db.data_dir / asset["ocr"]["image_blob"]["storage_path"]).is_file()
        assert (service.db.data_dir / asset["original_image_blob"]["storage_path"]).read_bytes() == raw
        calls.append(rendered)
        return transcript()

    monkeypatch.setattr(vision, "transcribe_image", recognize)
    claim = service.collection.claim_processing()
    if level == "attachment":
        assert claim is None
        assert not calls
    else:
        assert claim is not None
        service.collection.process_item(claim)
        with closing(service.db.connect()) as conn:
            current = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
            )
        assert len(calls) == 1 and text_complete(current)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0
