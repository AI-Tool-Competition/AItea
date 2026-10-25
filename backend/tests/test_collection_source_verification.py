"""Source access, original proof, and exhaustive legal coverage are distinct."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from app.contexts.ingestion.application.checklists import load_blueprint_version, load_blueprints
from app.configuration.paths import PROJECT_ROOT
from app.shared.infrastructure.http.download import DownloadError

ROOT = PROJECT_ROOT
spec = importlib.util.spec_from_file_location(
    "collection_source_verification", ROOT / "scripts/verify_collection_sources.py"
)
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


def selected(*items, version=None):
    plans = load_blueprints()
    plan = (
        load_blueprint_version("indonesia-20261003", version)
        if version is not None
        else plans["indonesia-20261003"]
    )
    plan["items"] = [item for item in plan["items"] if item["id"] in items]
    return {plan["id"]: plan}


def transport(monkeypatch, bodies):
    requests = []

    def download(url, *, consume, **options):
        requests.append((url, options))
        body, mime = bodies(url)
        if isinstance(body, Exception):
            raise body
        return {
            "url": url,
            "filename": "proof.pdf" if body.startswith(b"%PDF-") else "source.html",
            "mime_type": mime,
            "response_status": 200,
            "blob": consume(iter([body])),
        }

    monkeypatch.setattr(verification, "_download", download)
    return requests


def test_all_checklist_requirements_and_hashes_survive_matrix_generation():
    plans = load_blueprints()
    rows = verification.source_rows(plans, False)
    assert len(rows) == 32
    assert sum(row["state"] == "conditional_not_triggered" for row in rows) == 2
    assert sum(row["execution"] == "dataset_review" for row in rows) == 0
    wto = next(row for row in rows if row["plan_id"] == "indonesia-20261003" and row["item_id"] == "5.4")
    assert not any("itip-services-worldbank.wto.org" in seed["url"] for seed in wto["configured_seeds"])
    assert any(
        "itip-services-worldbank.wto.org" in entry["url"] for entry in wto["reference_only_entries"]
    )
    for row in rows:
        path = ROOT / "config/collection-plans" / f"{row['plan_id']}.json"
        assert row["plan_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert row["requirements"]["steps"]
        source_item = next(item for item in plans[row["plan_id"]]["items"] if item["id"] == row["item_id"])
        assert row["requirements"]["acceptance"] == source_item["acceptance"]
        assert row["coverage"]["full_required_content_verified"] is False


def test_cli_loads_project_proxy_environment_before_starting_source_probes(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(verification, "load_environment", lambda root: calls.append(("environment", root)))
    monkeypatch.setattr(verification, "load_blueprints", lambda: {"fixture": {"id": "fixture"}})
    monkeypatch.setattr(verification, "new_output", lambda _path: tmp_path)

    def run_probes(plans, output, **_options):
        calls.append(("verify", plans, output))
        return {"summary": {"active_items": 0}}

    monkeypatch.setattr(verification, "verify", run_probes)
    monkeypatch.setattr(sys, "argv", ["verify_collection_sources.py", "--output", str(tmp_path / "report")])

    assert verification.main() == 0
    assert calls[0] == ("environment", ROOT)
    assert calls[1][0] == "verify"
    assert json.loads(capsys.readouterr().out) == {
        "report": str(tmp_path / "report.json"),
        "active_items": 0,
    }


def test_historical_verification_uses_its_own_frozen_plan_hash():
    historical = load_blueprint_version("indonesia-20261003", 11)
    rows = verification.source_rows({historical["id"]: historical}, False)
    expected = hashlib.sha256(
        (ROOT / "config/collection-plans/versions/indonesia-20261003.v11.json").read_bytes()
    ).hexdigest()
    assert rows and all(row["plan_version"] == 11 and row["plan_sha256"] == expected for row in rows)


def test_successful_directory_response_cannot_become_coverage_or_original(tmp_path, monkeypatch):
    body = b"<html><title>Access denied</title><h1>Access denied</h1></html>"
    transport(monkeypatch, lambda url: (body, "text/html"))
    report = verification.verify(selected("1.2", version=11), tmp_path, follow_limit=0, time_budget=10)
    row = report["items"][0]
    assert row["coverage"]["root_reachable"] is True
    assert row["coverage"]["scope_discovery_supported"] is False
    assert row["coverage"]["original_acquired"] is False
    assert row["state"] == "reachable_adapter_or_content_gap"
    assert report["all_required_content_verified"] is False
    assert (tmp_path / "report.json").stat().st_mode & 0o777 == 0o600


def test_full_original_bytes_have_immutable_hash_and_do_not_claim_parser_validation(tmp_path, monkeypatch):
    body = b"%PDF-1.7\nactual immutable source original\n%%EOF"
    requests = transport(monkeypatch, lambda url: (body, "application/pdf"))
    report = verification.verify(selected("1.1", version=14), tmp_path, follow_limit=0, time_budget=10)
    row = report["items"][0]
    assert row["state"] == "original_sample_acquired"
    original = row["originals"][0]
    assert original["content_hash"] == hashlib.sha256(body).hexdigest()
    assert original["size"] == len(body)
    path = tmp_path / original["evidence_path"]
    assert path.read_bytes() == body
    assert path.stat().st_mode & 0o777 == 0o400
    assert report["summary"]["original_sample_items"] == 1
    assert report["summary"]["full_required_content_verified_items"] == 0
    assert all(callable(options["connection_slot"]) for _, options in requests)
    assert all(options["max_bytes"] <= 20 * 1024 * 1024 for _, options in requests)
    assert any(
        probe.get("parser_verification") == "not_executed_by_source_access_probe"
        for probe in row["root_probes"]
    )
    assert not (tmp_path / "tea.sqlite3").exists()


@pytest.mark.parametrize("closed", [True, False])
def test_bkpm_representation_retains_http_entity_and_exact_pdf_mapping(tmp_path, monkeypatch, closed):
    boundary = b"----WebKitFormBoundaryabcd1234"
    pdf = b"%PDF-1.7\nimmutable file\n%%EOF\n"
    body = (
        b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="law.pdf"'
        b"\r\nContent-Type: application/pdf\r\n\r\n"
        + pdf
        + b"\r\n--"
        + boundary
        + (b"--\r\n" if closed else b"\r\n")
    )
    transport(monkeypatch, lambda url: (body, "application/pdf"))
    definition = selected("4.1")["indonesia-20261003"]["items"][0]
    row = {"country": "ID", "item_id": "4.1", "recipe": verification.recipe_for(definition)}
    url = "https://jdih-storage.bkpm.go.id/jdih/jdih/2026-law.pdf"
    probe = verification.SourceVerifier(tmp_path).probe(row, {"url": url, "purpose": "document"}, definition)
    assert probe["status"] == "downloaded"
    assert probe["original_acquired"] is closed
    assert probe["parser_verification"] == "not_executed_by_source_access_probe"
    if closed:
        assert (tmp_path / probe["evidence_path"]).read_bytes() == pdf
        original_response = probe["source_response"]
        assert Path(original_response["cas_path"]).read_bytes() == body
        assert original_response["sha256"] == hashlib.sha256(body).hexdigest()
        part = original_response["extraction"]["parts"][0]
        assert body[part["byte_offset"] : part["byte_offset"] + part["size"]] == pdf
    else:
        assert (tmp_path / probe["evidence_path"]).read_bytes() == body
        assert "source_response" not in probe
        assert probe["envelope_error"]


def test_followed_original_keeps_exact_archived_source_proof(tmp_path, monkeypatch):
    html = b'<html><title>Constitution</title><a href="https://s.mkri.id/public/content/infoumum/regulation/pdf/UUD%20NRI%2045%20.pdf">UUD 1945</a></html>'
    pdf = b"%PDF-1.7\nimmutable original\n%%EOF"
    plans = selected("1.1", version=11)
    plans["indonesia-20261003"]["items"][0]["entries"] = plans["indonesia-20261003"]["items"][0]["entries"][
        :1
    ]
    transport(
        monkeypatch, lambda url: (pdf, "application/pdf") if "s.mkri.id" in url else (html, "text/html")
    )
    report = verification.verify(plans, tmp_path, follow_limit=2, time_budget=10)
    row = report["items"][0]
    assert row["coverage"]["original_acquired"] is True
    probe = row["follow_probes"][0]
    assert probe["discovered_from"]["content_hash"] == hashlib.sha256(html).hexdigest()
    assert (tmp_path / probe["discovered_from"]["evidence_path"]).read_bytes() == html
    assert probe["discovered_from"]["final_url"] == row["root_probes"][0]["final_url"]


def test_scoped_json_adapter_retains_replayable_public_request(tmp_path, monkeypatch):
    body = b'{"recordsTotal":0,"data":[]}'
    requests = transport(monkeypatch, lambda url: (body, "application/json"))
    report = verification.verify(selected("1.3"), tmp_path, follow_limit=0, time_budget=10)
    probe = next(p for p in report["items"][0]["root_probes"] if "setneg" in p["requested_url"])
    assert probe["request_method"] == "POST"
    assert probe["request_body"]["jns"] == ["UU"]
    request = next(options for url, options in requests if "setneg" in url)
    assert request["accept_compressed_json"] is True
    assert request["max_bytes"] <= 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("source_limit", "audit_limit", "expected_limit"),
    [
        (300 * 1024 * 1024, 128 * 1024 * 1024, 128 * 1024 * 1024),
        (16 * 1024 * 1024, 128 * 1024 * 1024, 16 * 1024 * 1024),
    ],
)
def test_original_probe_uses_checklist_limit_capped_by_audit_limit(
    tmp_path, monkeypatch, source_limit, audit_limit, expected_limit
):
    plans = selected("1.4")
    definition = plans["indonesia-20261003"]["items"][0]
    row = verification.source_rows(plans, False)[0]
    row["requirements"]["limits"]["max_original_bytes"] = source_limit
    requests = transport(monkeypatch, lambda _url: (DownloadError("bounded test stop"), "application/pdf"))
    probe = verification.SourceVerifier(tmp_path, max_original_bytes=audit_limit).probe(
        row,
        {
            "url": "https://jdih.setneg.go.id/api/hukumproduk/pdf?f=test.pdf&fl=P1&l=uploads",
            "purpose": "document",
        },
        definition,
    )

    assert probe["status"] == "failed"
    assert probe["response_limit_bytes"] == expected_limit
    assert requests[0][1]["max_bytes"] == expected_limit


def test_bi_official_source_verifier_replays_pager_from_saved_parent_without_exporting_tokens(tmp_path, monkeypatch):
    from app.contexts.ingestion.infrastructure.source_adapters.bi_official_discovery import catalog_url

    def page(number):
        start = (number - 1) * 10 + 1
        end = start + 9
        records = "".join(
            f'<a href="/id/publikasi/peraturan/Pages/PBI_{value:02d}2026.aspx">PBI {value}</a>'
            for value in range(start, end + 1)
        )
        pager = (
            '<a href="javascript:__doPostBack(\'ctl00$DataPagerPeraturan$ctl01$ctl01\',\'\')">2</a>'
            if number == 1
            else ""
        )
        return (
            '<html><title>Peraturan</title><body><form>'
            f"<p>Menampilkan {start}–{end} data dari total 20</p>{records}"
            f'<input type="hidden" name="__VIEWSTATE" value="private-viewstate-{number}">'
            f'<input type="hidden" name="__EVENTVALIDATION" value="private-validation-{number}">'
            f'<div id="ctl00_DataPagerPeraturan">{pager}</div></form></body></html>'
        ).encode()

    bodies = iter([(page(1), "text/html"), (page(2), "text/html")])
    requests = []

    def download(url, *, consume, **options):
        requests.append((url, options))
        content, mime = next(bodies)
        return {
            "url": url,
            "filename": "source.html",
            "mime_type": mime,
            "response_status": 200,
            "request_method": "POST" if "form_fields" in options else "GET",
            "blob": consume(iter([content])),
        }

    monkeypatch.setattr(verification, "_download", download)
    plan = selected("4.6")["indonesia-20261003"]
    definition = plan["items"][0]
    row = verification.source_rows({plan["id"]: plan}, False)[0]
    runner = verification.SourceVerifier(tmp_path, timeout=10, time_budget=20)
    root = runner.probe(row, {"url": catalog_url(1), "purpose": "catalog"}, definition)
    continuation = next(link for link in root["discovery"]["links"] if link["purpose"] == "catalog")
    child = runner.probe(row, continuation, definition, parent=root)

    assert child["status"] == "downloaded"
    assert child["discovery"]["scope"]["page"] == 2
    assert child["request_method"] == "POST"
    assert requests[1][0] == catalog_url(1)
    assert requests[1][1]["required_host"] == "www.bi.go.id"
    assert len(requests[1][1]["form_fields"]) >= 2
    assert child["request_evidence"]["parent_page_sha256"] == root["content_hash"]
    assert "private-viewstate" not in json.dumps(child)
    assert "private-validation" not in json.dumps(child)


def test_unexpected_exception_messages_are_not_exported(tmp_path, monkeypatch):
    transport(monkeypatch, lambda url: (RuntimeError("PRIVATE_CREDENTIAL_VALUE"), "text/html"))
    report = verification.verify(selected("1.2", version=11), tmp_path, follow_limit=0, time_budget=10)
    assert report["items"][0]["state"] == "source_access_failed"
    assert "PRIVATE_CREDENTIAL_VALUE" not in json.dumps(report)
    assert all(probe["error_type"] == "RuntimeError" for probe in report["items"][0]["root_probes"])


def test_public_downloader_rejection_is_preserved_not_bypassed(tmp_path, monkeypatch):
    transport(monkeypatch, lambda url: (DownloadError("抓取地址必须是 public 公网地址。"), "text/html"))
    report = verification.verify(selected("1.2", version=11), tmp_path, follow_limit=0, time_budget=10)
    assert all("公网" in probe["error"] for probe in report["items"][0]["root_probes"])
    assert report["summary"]["original_sample_items"] == 0


def test_new_output_stays_in_worktree_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="below this worktree"):
        verification.new_output(tmp_path / "outside")
    output = verification.new_output(tmp_path / "data/verification/new-run")
    assert output.is_dir()
    with pytest.raises(ValueError, match="new directory"):
        verification.new_output(output)
    (tmp_path / "data/escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="below this worktree"):
        verification.new_output(tmp_path / "data/escape/disallowed-run")


@pytest.mark.parametrize(
    "options",
    [
        {"workers": 17},
        {"per_host": 5},
        {"timeout": 91},
        {"time_budget": 1801},
        {"max_original_bytes": 134217729},
    ],
)
def test_limits_do_not_expand_network_boundary(tmp_path, options):
    with pytest.raises(ValueError):
        verification.SourceVerifier(tmp_path, **options)
