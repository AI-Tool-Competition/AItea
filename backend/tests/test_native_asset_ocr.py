"""Native image source identity, durable checkpoints and opt-in processing."""

import base64
import hashlib
import io
import json
from copy import deepcopy

import httpx
import pytest
from PIL import Image

from app import native_asset_ocr as native
from app import parsing, vision
from app.docx_parsing import parse_docx
from app.legacy_doc_parsing import _wpf_artifacts
from test_docx_parsing import _p, _package


def digest(value):
    return hashlib.sha256(value).hexdigest()


def raster(*, format="PNG", frames=False, size=(8, 4)):
    output = io.BytesIO()
    image = Image.new("RGB", size, "white")
    options = {"save_all": True, "append_images": [Image.new("RGB", size, "black")]} if frames else {}
    image.save(output, format=format, **options)
    return output.getvalue()


def docx(raw=None, *, count=1):
    raw = raster() if raw is None else raw
    body, parts, relationships = _p("Native text before images"), {}, []
    for index in range(count):
        body += (
            '<w:p><w:r><w:drawing><a:blip xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            f'r:embed="im{index}"/></w:drawing></w:r></w:p>'
        )
        parts[f"word/media/im{index}.png"] = raw
        relationships.append((f"im{index}", "image", f"media/im{index}.png", None))
    content = _package(body, parts=parts, relations=relationships)
    return content, parse_docx(content)


def wpf(*, external=False):
    content = b"\xffWPCtest-source-fixture"
    uri = (
        "https://example.invalid/image.png"
        if external
        else "data:image/png;base64," + base64.b64encode(raster()).decode()
    )
    html = f"<html><body><p>Native text<img src='{uri}'></p></body></html>".encode()
    events = b"startDocument()\n  insertText(text: Native text)\nendDocument()\n"
    _, conversion = _wpf_artifacts(html, events, digest(content))
    return content, {
        "source_hash": digest(content),
        "parser": "legacy-wpf",
        "elements": [],
        "chunks": [],
        "legacy": {"source_hash": digest(content), "format": "wpf", "conversion": conversion},
        "quality": {
            "complete": False,
            "pending_review": [{"reason": "source_artifact_layout_mapping_unverified"}],
        },
    }


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-vision-model")


@pytest.fixture
def local_workers(monkeypatch):
    """State-machine tests use tiny rasters; a separate test runs the real worker."""
    original = parsing._run_bounded

    def run(worker, args, timeout):
        if worker is not native._worker:
            return original(worker, args, timeout)
        operation, values = args
        if operation == "render":
            return native._render(*values)
        if operation == "validate_source":
            from app.docx_archive_parsing import read_docx_source

            path, source_hash, size = values
            content = read_docx_source(path, source_hash=source_hash, size=size)
            return {"source_hash": digest(content), "size": len(content)}
        return native.rebuild_native_asset_text(*values)

    monkeypatch.setattr(parsing, "_run_bounded", run)


def transcript(*, text="Visible tea text 2026", uncertain=None, complete=True, blank=False):
    return {
        "text": text,
        "uncertain_spans": uncertain or [],
        "complete": complete,
        "is_blank": blank,
        "model": "verified-test-model",
        "requested_model": "test-vision-model",
        "prompt_version": vision.IMAGE_PROMPT_VERSION,
        "review_status": "unreviewed",
        "physical_pages_known": False,
        "finish_reason": "stop",
    }


def test_completed_assets_checkpoint_before_http_rebuild_once_and_keep_all_native_gates(
    enabled, local_workers, monkeypatch
):
    content, manifest = docx()
    old_quality, old_objects, old_review = (
        deepcopy(manifest["quality"]),
        deepcopy(manifest["objects"]),
        deepcopy(manifest["review"]),
    )
    events, checkpoints = [], []

    def store_image(rendered):
        events.append("cas")
        assert rendered["source_hash"] == digest(content)
        assert rendered["original_image_sha256"] == digest(raster())
        assert digest(base64.b64decode(rendered["image_base64"])) == rendered["image_sha256"]
        assert "page" not in rendered and "page" not in rendered["locator"]
        return {
            "content_hash": rendered["image_sha256"],
            "size": rendered["image_size"],
            "storage_path": "owned/cas/image.png",
        }

    def checkpoint(value):
        events.append("checkpoint")
        checkpoints.append(deepcopy(value))

    def recognize(rendered):
        assert events == ["cas", "checkpoint"]
        assert checkpoints[-1]["ocr"]["attempts"] == 1
        assert checkpoints[-1]["ocr"]["image_blob"]["content_hash"] == rendered["image_sha256"]
        events.append("http")
        return transcript()

    monkeypatch.setattr(vision, "transcribe_image", recognize)
    result = native.ocr_native_assets_bounded(content, manifest, on_asset=checkpoint, on_image=store_image)
    assert events == ["cas", "checkpoint", "http", "checkpoint"]
    assert (
        result["quality"] == old_quality
        and result["objects"] == old_objects
        and result["review"] == old_review
    )
    assert result["native_asset_ocr"]["completed_assets"] == ["docx-image-000001"]
    assert result["native_asset_ocr"]["pending_assets"] == []
    assert "pages" not in result
    derived = [c for c in result["chunks"] if c["id"].startswith("native-asset-ocr-")]
    assert len(derived) == 1 and derived[0]["text"] == "Visible tea text 2026"
    assert "page" not in derived[0]["locator"]
    assert derived[0]["locator"]["package_part"] == "word/document.xml"
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("completed asset repeated"))
    resumed = native.ocr_native_assets_bounded(content, result)
    assert resumed == result
    missing_derived = deepcopy(result)
    missing_derived["chunks"] = [c for c in result["chunks"] if not c["id"].startswith("native-asset-ocr-")]
    missing_derived["elements"] = [
        e for e in result["elements"] if not e["element_key"].startswith("native-asset-ocr-")
    ]
    assert native.ocr_native_assets_bounded(content, missing_derived) == result


def test_wpf_keeps_original_to_artifact_mapping_unverified_and_exact_xpath(
    enabled, local_workers, monkeypatch
):
    content, manifest = wpf()
    image = native.assets(manifest)[0]
    monkeypatch.setattr(vision, "transcribe_image", lambda _: transcript())
    result = native.ocr_native_assets_bounded(content, manifest)
    accepted = native.assets(result)[0]
    assert accepted["asset_key"] == image["asset_key"]
    assert accepted["locator"] == image["locator"]
    assert accepted["original_object_mapping"] == "unverified"
    assert (
        accepted["ocr"]["locator"]["artifact_sha256"]
        == manifest["legacy"]["conversion"]["artifacts"][0]["sha256"]
    )
    assert accepted["ocr"]["original_image_sha256"] == image["image_sha256"]
    assert result["quality"] == manifest["quality"] and "pages" not in result


def test_disabled_model_does_not_decode_images_or_make_requests(local_workers, monkeypatch):
    content, manifest = docx()
    monkeypatch.delenv("VISION_OCR_ENABLED", raising=False)
    monkeypatch.setattr(native, "_render", lambda *a: pytest.fail("disabled OCR decoded image"))
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("disabled OCR request"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert len(native.pending_assets(result)) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("package_part", "word/media/other.png"),
        ("relationship_id", "wrong"),
        ("source_part", "word/header.xml"),
        ("image_hash", "a" * 64),
    ],
)
def test_docx_image_mapping_tampering_fails_before_model(enabled, local_workers, monkeypatch, field, value):
    content, manifest = docx()
    manifest["assets_pending"][0][field] = value
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("tampered source reached model"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert native.assets(result)[0]["asset_status"] == "ocr_failed"
    assert native.assets(result)[0]["ocr"]["attempts"] == 0


def test_parent_and_locator_hash_and_checkpoint_identity_are_verified(local_workers):
    content, manifest = docx()
    with pytest.raises(ValueError, match="父原件 hash"):
        native.ocr_native_assets_bounded(content + b"changed", manifest)
    manifest["assets_pending"][0]["locator"]["source_hash"] = "a" * 64
    with pytest.raises(ValueError, match="定位与父原件"):
        native.assets(manifest)
    _, manifest = docx()
    asset = native.assets(manifest)[0]
    asset["relationship_id"] = "edited"
    with pytest.raises(ValueError, match="修改了原始来源"):
        native.apply_asset_checkpoint(manifest, asset)


@pytest.mark.parametrize("field", ["content_base64", "sha256"])
def test_wpf_artifact_hash_and_full_html_bytes_must_match(enabled, local_workers, monkeypatch, field):
    content, manifest = wpf()
    manifest["legacy"]["conversion"]["artifacts"][0][field] = "edited"
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("invalid artifact reached model"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert native.assets(result)[0]["asset_status"] == "ocr_failed"


def test_multiframe_image_is_not_silently_reduced_to_first_frame(enabled, local_workers, monkeypatch):
    content, manifest = docx(raster(format="GIF", frames=True))
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("partial GIF reached model"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert "多帧" in native.assets(result)[0]["error"]
    assert native.assets(result)[0]["ocr"]["attempts"] == 0


def test_pixel_limits_reject_before_expensive_decode(enabled, local_workers, monkeypatch):
    content, manifest = docx(raster(size=(3000, 2001)))
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("oversized pixels reached model"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert "像素" in native.assets(result)[0]["error"]


def test_external_image_reference_never_fetches_or_becomes_complete(enabled, local_workers, monkeypatch):
    content, manifest = wpf(external=True)
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("external image request"))
    monkeypatch.setattr(native, "_render", lambda *a: pytest.fail("external image render"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert result["native_asset_ocr"]["review_assets"]
    assert result["native_asset_ocr"]["completed_assets"] == []
    assert native.assets(result)[0]["asset_status"] == "image_reference_pending"


def test_retry_checkpoint_has_at_most_three_attempts_and_nonzero_delay(enabled, local_workers, monkeypatch):
    content, manifest = docx()
    calls, checkpoints = [], []

    def failure(_):
        calls.append(True)
        raise vision.OCRResponseError("timeout", retryable=True)

    monkeypatch.setattr(vision, "transcribe_image", failure)
    for attempt in range(1, 4):
        manifest = native.ocr_native_assets_bounded(
            content, manifest, on_asset=lambda a: checkpoints.append(deepcopy(a))
        )
        current = native.assets(manifest)[0]
        assert current["ocr"]["attempts"] == attempt
        assert current["asset_status"] == ("pending_vision" if attempt < 3 else "ocr_failed")
        if attempt < 3:
            assert (
                manifest["native_asset_ocr"]["deferred_assets"]
                and manifest["native_asset_ocr"]["retry_after_seconds"] >= 5
            )
    native.ocr_native_assets_bounded(content, manifest)
    assert len(calls) == 3 and len(checkpoints) == 6


@pytest.mark.parametrize("callback", ["on_image", "on_asset"])
def test_persistence_failures_propagate_without_model_call(enabled, local_workers, monkeypatch, callback):
    content, manifest = docx()
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("persistence failure made request"))

    def failure(_):
        raise RuntimeError("lease expired")

    with pytest.raises(RuntimeError, match="lease expired"):
        native.ocr_native_assets_bounded(content, manifest, **{callback: failure})


def test_capacity_deferral_does_not_consume_provider_attempts(enabled, local_workers, monkeypatch):
    content, manifest = docx()

    class Busy:
        def acquire(self, **kwargs):
            return False

        def release(self):
            pytest.fail("unowned slot released")

    monkeypatch.setattr(parsing, "_OCR_SLOTS", Busy())
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("busy slot made request"))
    result = native.ocr_native_assets_bounded(content, manifest)
    asset = native.assets(result)[0]
    assert asset["deferred_reason"] == "ocr_capacity" and "ocr" not in asset
    assert result["native_asset_ocr"]["deferred_assets"] == [asset["asset_key"]]


def test_batch_bound_and_terminal_uncertainty_prevent_repeat_requests(enabled, local_workers, monkeypatch):
    content, manifest = docx(count=3)
    calls = []

    def uncertain(_):
        calls.append(True)
        return transcript(uncertain=["unreadable number"], complete=False)

    monkeypatch.setattr(vision, "transcribe_image", uncertain)
    result = native.ocr_native_assets_bounded(content, manifest, max_assets=1)
    assert len(calls) == 1 and len(native.pending_assets(result)) == 2
    assert result["native_asset_ocr"]["review_assets"] == ["docx-image-000001"]
    assert not any(c["id"].startswith("native-asset-ocr-") for c in result["chunks"])
    for invalid in (0, 21, True, 1.5):
        with pytest.raises(ValueError, match="单批"):
            native.ocr_native_assets_bounded(content, result, max_assets=invalid)


def test_completed_checkpoint_cannot_hide_later_source_or_status_changes(enabled, local_workers, monkeypatch):
    content, manifest = docx()
    monkeypatch.setattr(vision, "transcribe_image", lambda _: transcript())
    result = native.ocr_native_assets_bounded(content, manifest)
    result["assets_pending"][0]["ocr"]["complete"] = False
    with pytest.raises(ValueError, match="检查点与来源"):
        native.assets(result)


def test_actual_resource_bounded_worker_keeps_exact_native_mapping_without_fake_page():
    content, manifest = docx()
    value = native.assets(manifest)[0]
    rendered = parsing._run_bounded(native._worker, ("render", (content, manifest, value)), 45)
    assert rendered["input_identity"] == value["input_identity"]
    assert rendered["original_image_sha256"] == digest(raster())
    assert rendered["physical_pages_known"] is False and "page" not in rendered
    assert rendered["frame_basis"] == "all_frames_verified_single_frame"
    assert digest(base64.b64decode(rendered["image_base64"])) == rendered["image_sha256"]


def test_unsupported_and_empty_native_manifests_keep_existing_callers_compatible():
    for manifest in (
        {},
        {"parser": "text", "quality": {"complete": True}},
        {"parser": "pdf", "pages": [{"page": 1}]},
        {"parser": "docx-basic", "parser_version": "1"},
        {"parser": "docx-basic", "parser_version": "2", "assets_pending": []},
        {"parser": "legacy-doc", "legacy": {}},
    ):
        assert native.assets(manifest) == []


def test_archive_file_mode_verifies_source_and_reuses_complete_images(
    enabled, local_workers, monkeypatch, tmp_path
):
    content, manifest = docx()
    path = tmp_path / "source.docx"
    path.write_bytes(content)
    manifest["archive_docx"] = {"source_hash": digest(content), "size": len(content)}
    monkeypatch.setattr(vision, "transcribe_image", lambda _: transcript())
    result = native.ocr_native_assets_bounded(None, manifest, archive_path=path, archive_size=len(content))
    assert result["native_asset_ocr"]["completed_assets"]
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("archive complete asset repeated"))
    assert (
        native.ocr_native_assets_bounded(None, result, archive_path=path, archive_size=len(content)) == result
    )
    path.write_bytes(content[:-1] + b"changed")
    with pytest.raises(ValueError):
        native.ocr_native_assets_bounded(None, result, archive_path=path, archive_size=len(content))


def test_archive_file_mode_rejects_symlinks_missing_marker_and_wpf(tmp_path):
    content, manifest = docx()
    path = tmp_path / "source.docx"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="来源标记"):
        native.ocr_native_assets_bounded(None, manifest, archive_path=path, archive_size=len(content))
    manifest["archive_docx"] = {"source_hash": digest(content), "size": len(content)}
    link = tmp_path / "link.docx"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        native.ocr_native_assets_bounded(None, manifest, archive_path=link, archive_size=len(content))
    raw, old = wpf()
    old["archive_docx"] = {"source_hash": digest(raw), "size": len(raw)}
    with pytest.raises(ValueError, match="来源标记"):
        native.ocr_native_assets_bounded(None, old, archive_path=path, archive_size=len(raw))


def test_exif_orientation_is_mapped_to_exact_sent_pixels(enabled, local_workers, monkeypatch):
    output = io.BytesIO()
    image = Image.new("RGB", (8, 4), "white")
    exif = image.getexif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    content, manifest = docx(output.getvalue())
    renders = []

    def recognize(rendered):
        renders.append(rendered)
        return transcript()

    monkeypatch.setattr(vision, "transcribe_image", recognize)
    native.ocr_native_assets_bounded(content, manifest)
    rendered = renders[0]
    assert (rendered["original_width"], rendered["original_height"]) == (8, 4)
    assert (rendered["width"], rendered["height"]) == (4, 8)
    assert rendered["exif_orientation"] == 6
    assert rendered["original_image_sha256"] == digest(output.getvalue())


def test_corrupt_pixel_bytes_do_not_reach_model_despite_plausible_header(enabled, local_workers, monkeypatch):
    content, manifest = docx(raster()[:33])
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("corrupt pixels reached model"))
    result = native.ocr_native_assets_bounded(content, manifest)
    assert native.assets(result)[0]["asset_status"] == "ocr_failed"
    assert native.assets(result)[0]["ocr"]["attempts"] == 0


def test_wrong_cas_reference_propagates_before_request(enabled, local_workers, monkeypatch):
    content, manifest = docx()
    monkeypatch.setattr(vision, "transcribe_image", lambda _: pytest.fail("wrong CAS reached model"))
    with pytest.raises(ValueError, match="持久引用与请求 PNG"):
        native.ocr_native_assets_bounded(
            content,
            manifest,
            on_image=lambda _: {"content_hash": "a" * 64, "size": 1, "storage_path": "other.png"},
        )


def test_completed_checkpoint_failure_is_not_relabelled_as_provider_failure(
    enabled, local_workers, monkeypatch
):
    content, manifest = docx()
    monkeypatch.setattr(vision, "transcribe_image", lambda _: transcript())
    saved = []

    def checkpoint(value):
        saved.append(value)
        if value["asset_status"] == "ocr_complete":
            raise RuntimeError("final checkpoint lease expired")

    with pytest.raises(RuntimeError, match="final checkpoint lease expired"):
        native.ocr_native_assets_bounded(content, manifest, on_asset=checkpoint)
    assert len(saved) == 2 and saved[-1]["asset_status"] == "ocr_complete"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"complete": False}, "model_reported_incomplete"),
        ({"text": "bad\ufffdtext"}, "transcription_decode_replacement"),
        ({"text": "[illegible]"}, "illegible_text"),
    ],
)
def test_native_vision_response_uncertainties_never_fabricate_page_fields(
    enabled, monkeypatch, mutation, expected
):
    value = {"complete": True, "text": "Visible text", "uncertain_spans": [], "is_blank": False}
    value.update(mutation)
    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "provider-revision",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            },
        )

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    result = vision.transcribe_image({"image_base64": base64.b64encode(raster()).decode()})
    assert expected in result["uncertain_spans"]
    assert "page" not in result and "printed_page" not in result
    assert requests[0]["messages"][0]["content"] == vision._IMAGE_PROMPT


@pytest.mark.parametrize(
    "field,value", [("printed_page", "1"), ("complete", "true"), ("uncertain_spans", [1]), ("text", "")]
)
def test_native_vision_rejects_wrong_schema_or_suspicious_empty_image(enabled, monkeypatch, field, value):
    response = {"complete": True, "text": "Visible", "uncertain_spans": [], "is_blank": False}
    response[field] = value
    monkeypatch.setattr(vision, "_invoke", lambda _: ({}, json.dumps(response), "stop"))
    with pytest.raises(vision.OCRResponseError):
        vision.transcribe_image({"image_base64": "ignored-by-test"})


def test_chart_vision_prompt_keeps_exact_labels_and_never_estimates_plot_values(enabled, monkeypatch):
    response = {
        "complete": True,
        "text": "| Year | Value |\n| 2024 | 17 |",
        "uncertain_spans": [],
        "is_blank": False,
    }
    requests = []

    def invoke(body):
        requests.append(body)
        return ({"model": "provider-chart-model"}, json.dumps(response), "stop")

    monkeypatch.setattr(vision, "_invoke", invoke)
    result = vision.transcribe_chart_image({"image_base64": base64.b64encode(raster()).decode()})
    prompt = " ".join(requests[0]["messages"][0]["content"].split())
    assert result["prompt_version"] == vision.CHART_PROMPT_VERSION
    assert "estimate values from plotted marks" in prompt
    assert "every printed value" in prompt
    assert "| 2024 | 17 |" in result["text"]


@pytest.mark.parametrize("blank", [False, True])
def test_mineru_chart_ocr_is_source_bound_structured_and_completes_only_with_evidence(
    enabled, local_workers, monkeypatch, tmp_path, blank
):
    from app.blob_store import BlobStore
    from app.ingestion_quality import text_complete
    from app.mineru_results import reference

    store = BlobStore(tmp_path / "files", max_bytes=8 * 1024 * 1024)
    source = store.put_bytes("owner", b"verified-source-pdf")
    chart = raster()
    chart_blob = store.put_bytes("owner", chart)
    result_hash = "a" * 64
    page_text = "Article 1. Chart follows."
    element_key = "mineru-000001-000002"
    locator = {
        "kind": "pdf",
        "page": 1,
        "physical_page": 1,
        "bbox": [100, 200, 800, 700],
        "coordinate_system": "mineru-0-1000",
        "source_hash": source.digest,
        "provider_task_id": "task-1",
        "provider_page_idx": 0,
    }
    manifest = {
        "parser": "mineru",
        "parser_version": "api-v4",
        "pipeline_version": "mineru-precise-v1",
        "source_hash": source.digest,
        "quality": {"status": "needs_review", "complete": False, "review_required": True, "review_pages": [1]},
        "pages": [
            {
                "page": 1,
                "status": "ocr_uncertain",
                "source_hash": source.digest,
                "text": page_text,
                "reasons": ["chart_content_missing"],
                "ocr": {
                    "provider": "mineru",
                    "task_id": "task-1",
                    "batch_id": "batch-1",
                    "provider_page_idx": 0,
                    "part_first_page": 1,
                    "result_hash": result_hash,
                    "text_sha256": digest(page_text.encode()),
                    "complete": False,
                    "uncertain_spans": ["chart_content_missing"],
                },
            }
        ],
        "elements": [],
        "chunks": [],
        "remote_parse": {
            "provider": "mineru",
            "state": "done",
            "source_hash": source.digest,
            "page_count": 1,
            "parts": [
                {
                    "task_id": "task-1",
                    "batch_id": "batch-1",
                    "source_hash": source.digest,
                    "first_page": 1,
                    "last_page": 1,
                    "result_blob": {"content_hash": result_hash},
                }
            ],
        },
        "mineru_chart_assets": [
            {
                "asset_key": "mineru-chart-" + digest(element_key.encode())[:24],
                "element_key": element_key,
                "block_type": "chart",
                "locator": locator,
                "source_hash": source.digest,
                "image_hash": chart_blob.digest,
                "image_blob": reference(chart_blob),
                "size": chart_blob.size,
            }
        ],
    }
    assert native.read_mineru_chart_images(store.files_dir, "owner", manifest) == {
        manifest["mineru_chart_assets"][0]["asset_key"]: chart
    }
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    monkeypatch.setattr(
        vision,
        "transcribe_chart_image",
        lambda _rendered: {
            "text": "" if blank else "Chart title: Export value\n| Year | Value |\n| 2024 | 17 |",
            "complete": True,
            "is_blank": blank,
            "uncertain_spans": [],
            "model": "verified-chart-model",
            "requested_model": "test-vision-model",
            "prompt_version": vision.CHART_PROMPT_VERSION,
            "review_status": "unreviewed",
            "physical_pages_known": False,
            "finish_reason": "stop",
        },
    )
    result = native.ocr_native_assets_bounded(
        None,
        manifest,
        max_assets=1,
        image_contents={manifest["mineru_chart_assets"][0]["asset_key"]: chart},
        on_image=lambda rendered: reference(store.put_bytes("owner", base64.b64decode(rendered["image_base64"]))),
    )
    if blank:
        assert not text_complete(result)
        assert result["pages"][0]["status"] == "ocr_uncertain"
        assert "chart_content_missing" in result["pages"][0]["reasons"]
        assert result["mineru_chart_assets"][0]["asset_status"] == "ocr_uncertain"
    else:
        assert text_complete(result)
        assert result["pages"][0]["status"] == "ocr_complete"
        assert result["pages"][0]["reasons"] == []
        assert result["pages"][0]["ocr"]["supplemental_chart_assets"][0]["element_key"] == element_key
        assert result["mineru_chart_assets"][0]["ocr"]["prompt_version"] == vision.CHART_PROMPT_VERSION
        assert any(element["kind"] == "figure_transcription" for element in result["elements"])
        reused = native.reuse_mineru_chart_checkpoints(result, deepcopy(manifest))
        assert text_complete(reused)
    changed = deepcopy(manifest)
    changed["mineru_chart_assets"][0]["image_hash"] = "b" * 64
    changed["mineru_chart_assets"][0]["image_blob"]["content_hash"] = "b" * 64
    assert not text_complete(native.reuse_mineru_chart_checkpoints(result, changed))
