"""Public fallback consumes real text, never a provider's search summary."""

import json
import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.contexts.legal_analysis.infrastructure import official_evidence as evidence
from app.shared.application.streaming import StreamCancelled


def test_recent_official_original_is_retrievable_without_answer_cache(tmp_path):
    text = "Pasal 1\nRegistrasi pangan olahan impor BPOM wajib dilakukan sesuai ketentuan. Namun pengecualian berlaku sesuai Pasal ini."
    record = {
        "url": "https://registrasipangan.pom.go.id/layanan",
        "country": "ID",
        "title": "BPOM registrasi pangan olahan",
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    root = tmp_path / "legal-library/web-evidence"
    evidence._save(root, record)
    found = evidence.cached_official(tmp_path, "BPOM pangan impor", "ID")
    assert found and "pengecualian" in found[0]["excerpt"]
    assert found[0]["source_url"] == record["url"]
    assert not evidence.cached_official(tmp_path, "BPOM pangan impor", "SG")
    assert not evidence.cached_official(tmp_path, "bea cukai NIB", "ID")
    record["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    evidence._save(root, record)
    assert not evidence.cached_official(tmp_path, "BPOM pangan impor", "ID")
    record["fetched_at"] = datetime.now(timezone.utc).isoformat()
    record["text"] += " altered"
    evidence._save(root, record)
    assert not evidence.cached_official(tmp_path, "BPOM pangan impor", "ID")


def test_general_law_mentioning_regulator_does_not_cover_special_rule():
    from app.contexts.legal_analysis.application.analysis_service import _specific_source_gap

    assert _specific_source_gap("BPOM registrasi pangan", [{"title": "Perdagangan", "excerpt": "BPOM impor"}])
    assert not _specific_source_gap("BPOM registrasi pangan", [{"title": "BPOM registrasi pangan"}])


def test_cached_official_requires_query_coverage_beyond_incidental_two_terms(tmp_path):
    text = "第一条 企业应当依法登记。第二条 相关行政工作涉及备案和记录。"
    record = {
        "url": "https://www.court.gov.cn/test",
        "country": "CN",
        "title": "司法说明",
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    evidence._save(tmp_path / "legal-library/web-evidence", record)
    assert not evidence.cached_official(
        tmp_path, "CN: 海关 出口 食品 生产 企业 备案 原料 种植 基地 检验", "CN"
    )


def test_official_catalogue_rows_are_not_legal_articles():
    assert (
        evidence.citations_from_text(
            {
                "url": "https://jdih.pom.go.id/rule/search/query",
                "text": "1. Peraturan rules listed here are search results, not original provisions.",
            },
            "BPOM",
            5,
        )
        == []
    )


def test_country_official_hosts_and_private_url_are_rejected():
    for value in (
        "https://example.com/law",
        "https://127.0.0.1/law",
        "http://www.gov.cn/law",
        "https://user@www.gov.cn/law",
    ):
        assert evidence.official_url(value, "CN") is None
    assert evidence.official_url("https://www.gov.cn/law", "ID") is None
    assert evidence.official_url("https://sso.agc.gov.sg/Act/SFA", "SG")


def test_actual_html_is_saved_and_excerpt_offsets_reproduce_original(tmp_path, monkeypatch):
    body = (
        "<html><head><title>食品标签规定</title></head><body><h1>食品标签规定</h1>"
        "<p>第一条 食品经营者需要依规定提供完整标签信息，以下规则适用于本文件规定的产品。</p>"
        "<p>第二条 标签应使用规定语言。但是，符合规定的例外情形可以按本条例外处理，不可省略本句。</p></body></html>"
    ).encode()
    monkeypatch.setattr(
        evidence,
        "_download",
        lambda *a, **kw: {
            "url": "https://www.gov.cn/law",
            "mime_type": "text/html",
            "blob": body,
        },
    )
    record = evidence.fetch_official(tmp_path, "https://www.gov.cn/law", "CN", deadline=time.monotonic() + 2)
    saved = json.loads(next((tmp_path / "legal-library/web-evidence").glob("*.json")).read_text())
    assert saved["text"] == record["text"]
    citations = evidence.citations_from_text(record, "标签 语言", 5)
    assert citations
    for item in citations:
        anchor = item["locator"]
        assert record["text"][anchor["start"] : anchor["end"]].strip() == item["excerpt"]
        assert item["metadata"]["review_status"] == "draft"
    assert any("不可省略本句" in item["excerpt"] for item in citations)


def test_summary_without_fetched_body_is_not_evidence(tmp_path, monkeypatch):
    async def search(*args, **kwargs):
        return {"output": {"sources": [{"url": "https://www.gov.cn/law", "snippet": "必须备案"}]}}

    monkeypatch.setattr(evidence, "gateway_tool", search)
    monkeypatch.setattr(
        evidence, "fetch_official", lambda *a, **kw: (_ for _ in ()).throw(ValueError("no text"))
    )
    result = evidence.search_official(tmp_path, "备案", "CN")
    assert result["items"] == []
    assert result["acquisition"][0]["status"] == "unavailable"


def test_cancel_does_not_start_search(tmp_path, monkeypatch):
    async def forbidden(*a, **kw):
        pytest.fail("cancelled search ran")

    monkeypatch.setattr(evidence, "gateway_tool", forbidden)
    with pytest.raises(StreamCancelled):
        evidence.search_official(
            tmp_path, "标签", "SG", check_cancelled=lambda: (_ for _ in ()).throw(StreamCancelled())
        )


def test_english_section_keeps_exception_in_following_paragraph():
    text = (
        "1.—(1) A label must provide the required food information for consumers.\n"
        "(2) However, the exemption in this paragraph applies to qualifying products.\n"
        "2. Other requirements apply to importers under the following provisions."
    )
    items = evidence.citations_from_text(
        {
            "text": text,
            "content_hash": "f" * 64,
            "title": "Food rules",
            "country": "SG",
            "url": "https://sso.agc.gov.sg/test",
            "fetched_at": "2026-10-28T00:00:00Z",
        },
        "label",
        1,
    )
    assert len(items) == 1
    assert "exemption" in items[0]["excerpt"]
    assert "2. Other" not in items[0]["excerpt"]


def test_oversized_page_without_section_boundaries_is_not_cut_into_lines():
    text = (
        "Requirements for labels and other information.\n" * 120
    ) + "Exceptions apply to this requirement."
    assert evidence.citations_from_text({"text": text}, "labels", 5) == []


def test_long_official_guidance_keeps_complete_heading_sections_and_nested_exception(tmp_path, monkeypatch):
    body = (
        "<h1>Food retail</h1><h2>Other businesses</h2><p>"
        + "other rules " * 500
        + "</p><h2>Prepacked retail</h2><p>A retailer selling only prepacked food is exempt.</p>"
        "<h3>Exception</h3><p>This exemption excludes raw meat and seafood.</p>"
        "<h2>Other permits</h2><p>Other unrelated permits remain applicable.</p>"
    ).encode()
    monkeypatch.setattr(
        evidence,
        "_download",
        lambda *a, **kw: {
            "url": "https://www.sfa.gov.sg/test",
            "mime_type": "text/html",
            "blob": body,
        },
    )
    record = evidence.fetch_official(
        tmp_path, "https://www.sfa.gov.sg/test", "SG", deadline=time.monotonic() + 2
    )
    items = evidence.citations_from_text(record, "prepacked exempt", 1)
    assert len(items) == 1
    assert "excludes raw meat" in items[0]["excerpt"]
    assert "Other permits" not in items[0]["excerpt"]
    where = items[0]["locator"]
    assert record["text"][where["start"] : where["end"]].strip() == items[0]["excerpt"]


def test_redirect_outside_official_country_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_download", lambda *a, **kw: {"url": "https://example.com/law"})
    with pytest.raises(ValueError, match="重定向"):
        evidence.fetch_official(tmp_path, "https://www.gov.cn/law", "CN", deadline=time.monotonic() + 2)
    assert not (tmp_path / "legal-library/web-evidence").exists()


def test_physical_annex_block_without_row_scope_is_not_complete_legal_evidence():
    from app.contexts.legal_analysis.application.evidence import complete_source_unit

    item = {
        "excerpt": "KBLI Kode Judul Persyaratan label Izin Edar angkutan",
        "locator": {"unit_id": "law-operative-BLOCK135"},
        "metadata": {"context_complete": True},
    }
    assert complete_source_unit(item) == (None, "unresolved_table_scope")


def test_official_faq_keeps_numbered_registration_exceptions_with_its_question():
    text = "注册要求与例外\n进口零售加工食品须注册。\n1. 仅供试验的产品除外。\n2. 最少加工的食品除外。\n3. 不供最终消费者销售的大包装除外。\n其他手续\n另有其他手续和具体要求，应按实际情形核对。"
    record = {
        "text": text,
        "section_starts": [0, text.index("其他手续")],
        "content_hash": "a" * 64,
        "title": "Layanan",
        "country": "ID",
        "url": "https://registrasipangan.pom.go.id/layanan",
        "fetched_at": "2026-10-29T00:00:00Z",
    }
    items = evidence.citations_from_text(record, "注册", 1)
    assert len(items) == 1
    assert "须注册" in items[0]["excerpt"] and "最少加工" in items[0]["excerpt"]
    assert items[0]["locator"]["article"] is None


def test_accordion_guidance_preserves_general_requirement_and_nested_exceptions(tmp_path, monkeypatch):
    body = (
        "<h1>Food guidance</h1><div role='button' aria-controls='one'>Registration</div>"
        "<div><p>Retail processed food requires registration.</p><p>Exceptions:</p><ol>"
        "<li>1. Samples not for sale.</li><li>2. Minimally processed foods.</li></ol></div>"
        "<div role='button' aria-controls='two'>Labels</div><div><p>Labels must identify the importer.</p></div>"
    ).encode()
    monkeypatch.setattr(
        evidence,
        "_download",
        lambda *a, **kw: {"url": "https://test.pom.go.id/guidance", "mime_type": "text/html", "blob": body},
    )
    record = evidence.fetch_official(
        tmp_path, "https://test.pom.go.id/guidance", "ID", deadline=time.monotonic() + 2
    )
    assert len(record["section_starts"]) == 2
    item = evidence.citations_from_text(record, "registration", 1)[0]
    assert "requires registration" in item["excerpt"] and "Minimally processed" in item["excerpt"]
    assert "Labels must" not in item["excerpt"]
    assert item["locator"]["article"] is None


def test_separate_scope_and_exemption_panels_precede_procedural_registration_details():
    panels = [
        "Pangan olahan apa saja yang wajib didaftarkan di Badan POM?\nPangan olahan impor dalam kemasan eceran wajib registrasi.\n",
        "Adakah pangan olahan yang tidak wajib didaftarkan di Badan POM?\nPangan yang hanya mengalami pengolahan minimal tidak wajib didaftarkan.\n",
        "Apakah syarat registrasi akun perusahaan?\nBPOM import permit ML tea food registration registrasi pangan impor.\n",
    ]
    text = "".join(panels)
    record = {
        "text": text,
        "section_starts": [0, len(panels[0]), len(panels[0] + panels[1])],
        "content_hash": "e" * 64,
        "title": "Official food registration FAQ",
        "country": "ID",
        "url": "https://registrasipangan.pom.go.id/layanan",
        "fetched_at": "2026-10-29T00:00:00Z",
    }
    item = evidence.citations_from_text(record, "BPOM import permit ML tea", 1)[0]
    assert "wajib registrasi" in item["excerpt"]
    assert "pengolahan minimal" in item["excerpt"]
    assert "syarat registrasi akun" not in item["excerpt"]
    where = item["locator"]
    assert text[where["start"] : where["end"]].strip() == item["excerpt"]


def test_web_version_uses_explicit_revision_and_never_the_fetch_date():
    record = {
        "country": "CN",
        "title": "最高人民法院关于审理买卖合同纠纷案件适用法律问题的解释（2012年3月31日通过）",
        "text": "（2012年3月31日通过，根据2020年12月23日审判委员会决定修正）",
        "fetched_at": "2026-10-29T00:00:00Z",
    }
    assert evidence.source_version(record)["declared_version_date"] == "2020-12-23"
    record["text"] = "发布时间：2026-10-29"
    assert evidence.source_version(record)["declared_version_date"] == "2012-03-31"
    record["title"] = "普通办事网页"
    assert evidence.source_version(record) == {}
    record["text"] = "最高人民法院关于审理买卖合同纠纷案件适用法律问题的解释\n（2012年3月31日通过）"
    assert evidence.source_version(record)["declared_version_date"] == "2012-03-31"
