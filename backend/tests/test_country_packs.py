import json
import sqlite3

import pytest

from app.bootstrap import Service
from app.contexts.documents.infrastructure.country_packs import CountryPacks
from app.contexts.documents.infrastructure import country_semantic
from app.contexts.legal_analysis.application.analysis_engine import _LegalRunner, _eligible_law
from app.contexts.legal_analysis.domain.analysis_models import LegalCitation
from app.shared.domain.errors import AppError


def test_document_semantics_ranks_stored_vectors_against_the_query(tmp_path, monkeypatch):
    import numpy as np

    index = country_semantic.CountrySemantic(tmp_path)
    index.path.parent.mkdir(parents=True)
    with sqlite3.connect(index.path) as conn:
        conn.execute("CREATE TABLE document_vectors(country,document_id,source_hash,dimension,vector)")
        conn.executemany(
            "INSERT INTO document_vectors VALUES(?,?,?,?,?)",
            [
                ("CN", "unrelated", "hash-a", 2, np.array([0, 1], dtype=np.float32).tobytes()),
                ("CN", "relevant", "hash-b", 2, np.array([1, 0], dtype=np.float32).tobytes()),
                ("ID", "outside-scope", "hash-c", 2, np.array([1, 0], dtype=np.float32).tobytes()),
            ],
        )
    monkeypatch.setattr(
        index, "status", lambda _: {"status": "ready", "countries": {"CN": 2}, "dimension": 2}
    )
    monkeypatch.setattr(
        country_semantic, "query_vector", lambda *args: (np.array([1, 0], dtype=np.float32), "ready")
    )
    rows, status = index.search("CN", "payment obligation", "fixture", limit=2)
    assert status == "ready"
    assert rows == [("relevant", "hash-b", 1.0), ("unrelated", "hash-a", 0.0)]


def pack_index(data):
    path = data / "legal-library" / "index.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        conn.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            [("fingerprint", "fixture-v1"), ("record_count_ID", "1"), ("record_count_SG", "1")],
        )
        conn.execute(
            "CREATE TABLE records(id,country,document_id,unit_id,title,article,text,translation,source_url,source_path,source_hash,source_type,language,status)"
        )
        conn.execute("CREATE VIRTUAL TABLE record_search USING fts5(title,text,translation,cjk_terms)")
        for country in ("ID", "SG"):
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    country + "-record",
                    country,
                    country + "-doc",
                    "unit-4",
                    "Food label law",
                    "4",
                    "Food label information for importers.",
                    "食品标签说明",
                    "https://example.gov/food",
                    "source.pdf",
                    country + "-hash",
                    "法规原文",
                    "en",
                    "unreviewed",
                ),
            )
            conn.execute(
                "INSERT INTO record_search VALUES(?,?,?,?)",
                ("Food label law", "Food label information for importers.", "食品标签说明", "食品 品标 标签"),
            )


def test_governed_metadata_preserves_original_identity_and_historical_period(tmp_path):
    pack_index(tmp_path)
    path = tmp_path / "source-metadata.json"
    metadata = {
        "effective_from": "2020-01-01",
        "superseded_at": "2025-06-05",
        "superseded_by": {"document_id": "new", "article": "550(b)"},
    }
    entry = {"country": "ID", "document_id": "ID-doc", "source_hash": "ID-hash", "metadata": metadata}
    path.write_text(json.dumps({"schema_version": 1, "documents": [entry]}))
    packs = CountryPacks(tmp_path, metadata_path=path)
    old = packs.read("ID", "ID-record")
    assert all(old["metadata"][key] == value for key, value in metadata.items())
    assert old["metadata"]["review_status"] == "draft"
    assert old["excerpt"].startswith("Food label information")
    assert packs.read("SG", "SG-record")["metadata"].get("superseded_at") is None
    entry["source_hash"] = "unrelated-original"
    path.write_text(json.dumps({"schema_version": 1, "documents": [entry]}))
    assert packs.read("ID", "ID-record")["metadata"].get("superseded_at") is None


@pytest.mark.parametrize(
    ("page_title", "heading", "expected_title", "status"),
    [
        ("全球法规网-中国商务法规", "中华人民共和国海关法", "中华人民共和国海关法", "source_heading"),
        ("国家税务总局政策法规库", "目　　录", "法规原文（名称待核对）", "unresolved"),
    ],
)
def test_website_title_never_becomes_a_statute_name(tmp_path, page_title, heading, expected_title, status):
    pack_index(tmp_path)
    path = tmp_path / "legal-library/index.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','2')")
        for identifier, article, text in [
            ("CN-heading", "", heading),
            ("CN-article", "9", "第九条 进出口货物收发货人可以依法委托报关企业办理报关手续。"),
        ]:
            conn.execute(
                "INSERT INTO records SELECT ?,'CN','cn-doc',?, ?,?,?,'',source_url,source_path,'cn-hash','法律；规范类型：法律','zh','unreviewed' FROM records WHERE id='ID-record'",
                (identifier, identifier, page_title, article, text),
            )
    catalog = tmp_path / "legal-library/source-packs/CN/茶渡中国法律资料/数据/法规与标准目录.jsonl"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps({"document_id": "cn-doc", "original_file_hash": "cn-hash", "title": page_title})
    )
    item = CountryPacks(tmp_path).read("CN", "CN-article")
    assert item["title"] == expected_title
    assert item["metadata"]["source_page_title"] == page_title
    assert item["metadata"]["title_status"] == status
    assert item["metadata"]["effective_from"] is None
    assert item["metadata"]["review_status"] == "draft"
    if status == "source_heading":
        assert item["metadata"]["title_evidence_record_id"] == "CN-heading"
    else:
        assert item["metadata"]["catalog_title"] is None


def test_trade_direction_uses_conduct_outside_the_statute_title(tmp_path, monkeypatch):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','3')")
        for article, text in (
            ("10", "进口食品的进口商应当办理进口备案，负责进口食品生产企业的进口备案材料。"),
            ("42", "出口食品生产企业应当向住所地海关申请企业备案，并提交相关备案材料。"),
            ("43", "申请企业备案材料的内容应当真实、完整，海关依法记录相应申请情况。"),
        ):
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"CN-{article}",
                    "CN",
                    "test",
                    article,
                    "测试进出口食品管理办法",
                    article,
                    text,
                    "",
                    "https://example.gov/law",
                    "law.html",
                    "hash",
                    "法规原文",
                    "zh",
                    "unreviewed",
                ),
            )
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(packs.semantic, "search", lambda *a: ([], "unavailable"))
    result = packs.search("CN", "《测试进出口食品管理办法》出口食品 生产企业 备案", 2)
    assert result["items"][0]["locator"]["article"] == "42"
    imports = packs.search("CN", "《测试进出口食品管理办法》进口食品 生产企业 备案", 2)
    assert imports["items"][0]["locator"]["article"] == "10"
    explicit = packs.search("CN", "《测试进出口食品管理办法》第十条 出口", 1)
    assert explicit["items"][0]["locator"]["article"] == "10"


def test_short_instrument_name_and_dependency_subject_do_not_match_shared_authorities(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        for identifier, title, body in (
            ("civil", "中华人民共和国民法典", "买受人应当按约定支付价款，违约应承担责任。"),
            (
                "security",
                "全国人民代表大会常务委员会关于修改《中华人民共和国网络安全法》的决定",
                "修改网络安全法有关规定。",
            ),
            (
                "explanation",
                "最高人民法院关于适用《中华人民共和国民法典》有关担保制度的解释",
                "保证人依法承担担保责任。",
            ),
        ):
            conn.execute(
                "INSERT INTO records SELECT ?, 'CN', ?, 'unit-1', ?, '1', ?, '', source_url,source_path,source_hash,source_type,'zh',status FROM records WHERE id='ID-record'",
                (identifier, identifier, title, body),
            )
        conn.row_factory = sqlite3.Row
        packs = CountryPacks(tmp_path)
        exact = packs._exact_rows(conn, "CN", "民法典 买卖合同 支付价款 违约责任")
        assert [r["id"] for r in exact] == ["civil"]
        related = packs._related_rows(conn, "CN", "全国人民代表大会 中华人民共和国 货款", exact)
        assert "security" not in {r["id"] for r in related}


def test_explicit_article_reference_reads_two_hops_in_same_source_version(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        for article, text in (
            ("55", "Pasal 55\nPermohonan Surat Keterangan sebagaimana dimaksud dalam Pasal 54 ayat (1)."),
            ("54", "Pasal 54\nPermohonan pengecualian sebagaimana dimaksud dalam Pasal 53."),
            ("53", "Pasal 53\nPengecualian diberikan untuk barang tertentu, bukan untuk semua impor."),
        ):
            conn.execute(
                "INSERT INTO records SELECT ?,country,document_id,?,title,?,?,translation,source_url,source_path,source_hash,source_type,language,status FROM records WHERE id='ID-record'",
                ("ID-" + article, "unit-" + article, article, text),
            )
        conn.execute(
            "INSERT INTO records SELECT 'wrong-version',country,document_id,unit_id,title,'53','Different version',translation,source_url,source_path,'other-hash',source_type,language,status FROM records WHERE id='ID-record'"
        )
    found = CountryPacks(tmp_path).referenced_articles("ID", "ID-55")
    assert [r["locator"]["article"] for r in found] == ["54", "53"]
    assert all("Different version" not in r["excerpt"] for r in found)
    assert "bukan untuk semua impor" in found[-1]["excerpt"]


def test_adjacent_originals_preserve_exceptions_and_never_cross_source_version(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        # Interleaved jurisdictions, versions and documents must not become
        # surrounding context, even when their rowids are closer to the hit.
        for ident, country, document, source_hash, kind, text in (
            ("before", "ID", "shared", "v1", "article", "仅适用于本条定义的经营者。"),
            ("wrong-country", "SG", "shared", "v1", "article", "不同国家。"),
            ("hit", "ID", "shared", "v1", "article", "符合条件的经营者办理申请。"),
            ("heading", "ID", "shared", "v1", "heading", "下一节"),
            ("wrong-version", "ID", "shared", "v2", "article", "另一版本。"),
            ("wrong-document", "ID", "other", "v1", "article", "另一法规。"),
            ("after", "ID", "shared", "v1", "article", "前条不适用于仅提供样品的情形。"),
        ):
            conn.execute(
                "INSERT INTO records SELECT ?,?,?,?,title,'1',?,'',source_url,source_path,?,?,'zh',status FROM records WHERE id='ID-record'",
                (ident, country, document, ident, text, source_hash, kind),
            )
    packs = CountryPacks(tmp_path)
    items = packs.neighboring_records("ID", "hit")
    assert [item["locator"]["unit_id"] for item in items] == ["before", "after"]
    assert items[1]["excerpt"] == "前条不适用于仅提供样品的情形。"
    assert all(item["metadata"]["adjacent_to_record_id"] == "hit" for item in items)
    assert packs.neighboring_records("SG", "hit") == []
    assert packs.neighboring_records("ID", "missing") == []


def test_long_code_uses_printed_chapter_subject_before_per_document_cap(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure import country_packs

    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','4')")
        for identifier, article, text in (
            ("sales-heading", "", "第九章 买卖合同"),
            ("payment", "628", "买受人应当按照约定的时间支付价款。"),
            ("service-heading", "", "第二十一章 技术合同"),
            ("service", "884", "技术服务合同的委托人不履行合同义务，未支付的报酬应当支付，并承担违约责任。"),
        ):
            conn.execute(
                "INSERT INTO records SELECT ?, 'CN', 'civil', ?, '中华人民共和国民法典', ?, ?, '', source_url,source_path,source_hash,source_type,'zh',status FROM records WHERE id='ID-record'",
                (identifier, identifier, article, text),
            )
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(packs.semantic, "search", lambda *a: ([], "not_configured"))
    rerank_inputs = []
    monkeypatch.setattr(country_packs, "rerank_texts", lambda query, texts: rerank_inputs.extend(texts))
    result = packs.search("CN", "民法典 买卖合同 支付价款 违约责任", 2)
    assert result["items"][0]["locator"]["article"] == "628"
    headings = [{"title": "买卖合同", "record_id": "sales-heading"}]
    assert result["items"][0]["locator"]["headings"] == headings
    assert packs.read("CN", "payment")["locator"]["headings"] == headings
    assert any("买卖合同\n买受人" in text for text in rerank_inputs)
    assert all("买卖合同" not in text for text in rerank_inputs if "技术服务合同的委托人" in text)


def test_source_hierarchy_never_crosses_jurisdiction_document_or_version(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        for ident, country, doc, source, article, text in (
            ("heading", "ID", "law", "v1", "", "第一章 适用范围"),
            ("foreign", "SG", "law", "v1", "1", "不同国家的条款。"),
            ("other", "ID", "other", "v1", "1", "另一法规的条款。"),
            ("revision", "ID", "law", "v2", "1", "另一版本的条款。"),
            ("target", "ID", "law", "v1", "2", "同一原件内的目标条款。"),
        ):
            conn.execute(
                "INSERT INTO records SELECT ?,?,?,?,title,?,?,'',source_url,source_path,?,source_type,'zh',status FROM records WHERE id='ID-record'",
                (ident, country, doc, ident, article, text, source),
            )
    packs = CountryPacks(tmp_path)
    assert packs.read("ID", "target")["locator"]["headings"] == [
        {"title": "适用范围", "record_id": "heading"}
    ]
    for country, identifier in (("SG", "foreign"), ("ID", "other"), ("ID", "revision")):
        assert "headings" not in packs.read(country, identifier)["locator"]


@pytest.mark.parametrize("index_status", ["ready", "partial"])
def test_clause_semantics_recalls_without_lexical_overlap_and_retains_original(
    tmp_path, monkeypatch, index_status
):
    pack_index(tmp_path)
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(
        packs.clauses,
        "search",
        lambda *a: (
            [
                {
                    "record_id": "ID-record",
                    "source_hash": "ID-hash",
                    "text_kind": "text",
                    "start": 0,
                    "end": 41,
                    "score": 0.8,
                }
            ],
            index_status,
        ),
    )
    monkeypatch.setattr(
        packs.semantic,
        "search",
        lambda *a: (
            pytest.fail("clause index is ready") if index_status == "ready" else ([], "not_configured")
        ),
    )
    result = packs.search("ID", "消费者知情权", 3)
    assert result["mode"] == "local_pack_vector"
    assert result["semantic_granularity"] == "clause"
    assert result["clause_semantic_status"] == index_status
    if index_status == "partial":
        assert "尚未全量覆盖" in result["warning"]
    assert result["items"][0]["metadata"]["source_record_id"] == "ID-record"
    assert result["items"][0]["metadata"]["review_status"] == "draft"
    assert packs.read("ID", "ID-record")["excerpt"].startswith("Food label information")


def test_shared_service_and_engine_use_identical_country_evidence_without_approval(tmp_path, monkeypatch):
    pack_index(tmp_path)
    service = Service(tmp_path)
    monkeypatch.setattr(
        service, "search_eligible", lambda *a, **kw: pytest.fail("configured pack must not use RAGFlow")
    )
    template = {
        "origin": "CN",
        "destination": "ID",
        "agent": {
            "language": "中文",
            "instructions": "test",
            "legal": {"enabled": True, "jurisdictions": ["SG"], "rules": []},
        },
        "stages": [],
    }
    direct = service.legal.search("owner", "food label", None, template, ["ID"], 4)
    runner = _LegalRunner(
        service,
        "owner",
        template,
        {"id": "preparation", "name": "准备"},
        "food label",
        [],
        jurisdictions=["ID"],
    )
    through_engine = runner._search("food label", ["ID"])
    assert direct["mode"] == through_engine["mode"] == "local_pack"
    assert direct["items"] == through_engine["items"]
    item = through_engine["items"][0]
    LegalCitation.model_validate(item)
    assert item["country"] == "ID"
    assert item["locator"]["unit_id"] == "unit-4"
    assert item["metadata"]["review_status"] == "draft"
    assert not _eligible_law(item)
    assert runner.allowed == ["ID"]
    other_scope = _LegalRunner(
        service,
        "owner",
        template,
        {"id": "preparation", "name": "准备"},
        "food label",
        [],
        jurisdictions=["SG"],
    )
    assert runner.configuration != other_scope.configuration
    completed = runner.run()
    qa04 = next(step for step in completed["trace"] if step["id"] == "qa04")
    assert qa04["output_data"]["citations"]
    assert all(item["country"] == "ID" for item in qa04["output_data"]["citations"])
    assert all(item["status"] != "supported" for item in completed["issues"])
    assert (
        service.legal.search("owner", "food label", None, template, ["SG"], 4)["items"][0]["country"] == "SG"
    )
    with pytest.raises(AppError):
        service.legal.search("owner", "food label", None, template, ["US"], 4)


def test_explicit_document_selection_does_not_expand_into_global_pack(tmp_path, monkeypatch):
    pack_index(tmp_path)
    service = Service(tmp_path)
    monkeypatch.setattr(service, "search_eligible", lambda *a, **kw: {"items": [], "mode": "unavailable"})
    template = {"origin": "CN", "destination": "ID", "agent": {"legal": {}}, "stages": []}
    assert (
        service.legal.search("owner", "food label", None, template, ["ID"], 4, document_ids=[])["items"] == []
    )
    assert CountryPacks(tmp_path).status()["countries"] == {"CN": 0, "ID": 1, "SG": 1}
    from app.contexts.legal_analysis.infrastructure import official_evidence

    monkeypatch.setenv("AGENT_TOOLS_API_KEY", "fixture-only")
    monkeypatch.setattr(
        official_evidence, "search_official", lambda *a, **kw: pytest.fail("explicit document scope")
    )
    assert not service.legal.acquire_evidence("owner", "food", None, template, ["ID"], document_ids=[])[
        "items"
    ]


def test_multi_country_acquisition_does_not_drop_the_second_country(tmp_path, monkeypatch):
    pack_index(tmp_path)
    service = Service(tmp_path)
    packs = CountryPacks(tmp_path)

    def search(owner, query, case_id, template, countries, *args, **kwargs):
        return {
            "mode": "local_pack",
            "items": [
                {**packs.read(countries[0], countries[0] + "-record"), "id": countries[0] + str(i)}
                for i in range(20)
            ],
        }

    monkeypatch.setattr(service.legal, "search", search)
    result = service.legal.acquire_evidence("owner", "labels", None, {}, ["ID", "SG"], limit=20)
    assert len(result["items"]) == 40
    assert [item["country"] for item in result["items"][:4]] == ["ID", "SG", "ID", "SG"]


def test_explicit_chinese_and_indonesian_article_names_match_source_records(tmp_path):
    pack_index(tmp_path)
    path = tmp_path / "legal-library" / "index.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE records SET title=?,article=? WHERE id='ID-record'", ("印尼2021年第29号政府条例", "20")
        )
        conn.execute(
            "INSERT INTO records SELECT 'CN-record','CN','cn-doc','unit-11','中华人民共和国海关法','11',text,translation,source_url,source_path,source_hash,source_type,language,status FROM records WHERE id='ID-record'"
        )
        conn.row_factory = sqlite3.Row
        packs = CountryPacks(tmp_path)
        assert [r["id"] for r in packs._exact_rows(conn, "ID", "印尼PP 29/2021第20条")] == ["ID-record"]
        assert [r["id"] for r in packs._exact_rows(conn, "CN", "《中华人民共和国海关法》第十一条")] == [
            "CN-record"
        ]


def test_html_section_keeps_import_registration_classification_together(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        conn.execute("UPDATE records SET source_type='paragraph' WHERE id='SG-record'")
        conn.execute(
            "INSERT INTO records SELECT 'SG-classification',country,document_id,'unit-5',title,'','Registration is required for importing processed food.','',source_url,source_path,source_hash,source_type,language,status FROM records WHERE id='SG-record'"
        )
    record = CountryPacks(tmp_path).read("SG", "SG-record")
    assert "Food label information" in record["excerpt"]
    assert "Registration is required for importing processed food" in record["excerpt"]
    assert record["locator"]["context_record_ids"] == ["SG-record", "SG-classification"]
    assert record["metadata"]["context_complete"] is True


def test_retail_table_keeps_neighbouring_exception_without_cutting_rows(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        conn.execute("UPDATE records SET source_type='table_row' WHERE id='SG-record'")
        for index, text in enumerate(
            (
                "Retail operations preparing food require a licence.",
                "Retailers solely selling pre-packed food from registered importers do not require a retail licence.",
                "Other unrelated business category. " * 180,
            )
        ):
            conn.execute(
                "INSERT INTO records SELECT ?,country,document_id,?,title,'',?,'',source_url,source_path,source_hash,source_type,language,status FROM records WHERE id='SG-record'",
                (f"SG-row-{index}", f"unit-{index}", text),
            )
    record = CountryPacks(tmp_path).read("SG", "SG-row-0")
    assert "do not require a retail licence" in record["excerpt"]
    assert "unrelated business category" not in record["excerpt"]
    assert record["locator"]["context_record_ids"] == ["SG-record", "SG-row-0", "SG-row-1"]


def test_chinese_law_alias_finds_empty_pdf_titles_and_amendments(tmp_path):
    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        for doc in ("MOLEG-FOOD-REG", "GAZ-S92-2025"):
            conn.execute(
                "INSERT INTO records SELECT ?,country,?,unit_id,'',article,text,translation,source_url,source_path,source_hash,source_type,language,status FROM records WHERE id='SG-record'",
                (doc, doc),
            )
        conn.row_factory = sqlite3.Row
        found = CountryPacks(tmp_path)._exact_rows(conn, "SG", "新加坡《食品条例》英文标签")
        assert {r["document_id"] for r in found} >= {"MOLEG-FOOD-REG", "GAZ-S92-2025"}


def test_known_old_version_triggers_lookup_but_valid_historical_period_does_not():
    from app.contexts.legal_analysis.application.analysis_service import _version_gap

    old = {"metadata": {"effective_from": "2020-01-01", "superseded_at": "2026-03-01", "exact_match": True}}
    unrelated = {"metadata": {"effective_from": "2024-01-01"}}
    assert _version_gap([old, unrelated], "2026-11-01")
    assert not _version_gap([old], "2025-11-01")
    assert not _version_gap([unrelated], "2026-11-01")


@pytest.mark.parametrize("query", ["出口申报 跨境经营者资格备案", "出口申报"])
def test_procedure_retrieval_follows_enabling_act_and_its_change(tmp_path, query):
    pack_index(tmp_path)
    path = tmp_path / "legal-library/index.sqlite3"
    rows = [
        ("rule", "出口申报管理规定", "4", "第四条 申请出口申报应当取得跨境经营者资格备案。"),
        ("procedure", "跨境经营者资格备案办法", "1", "第一条 根据《测试跨境经营法》制定本办法。"),
        ("act", "测试跨境经营法", "11", "第十一条 跨境经营者依法取得经营资格，可以从事跨境经营。"),
        ("act", "测试跨境经营法", "99", "第九十九条 本法自2026年3月1日起施行。"),
        ("change", "关于修改《测试跨境经营法》的决定", "", "删去第九条。"),
        ("change", "关于修改《测试跨境经营法》的决定", "", "本决定自公布之日起施行。"),
        (
            "notice",
            "主管部门介绍取消跨境经营者资格备案有关情况",
            "",
            "根据修改决定，主管部门停止办理跨境经营者资格备案。该事项不再作为经营资格前提。",
        ),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','6')")
        for n, (doc, title, article, text) in enumerate(rows):
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"CN-{n}",
                    "CN",
                    doc,
                    str(n),
                    title,
                    article,
                    text,
                    "",
                    "https://example.gov/law",
                    "law.pdf",
                    doc,
                    "法律；规范类型：法律",
                    "zh",
                    "local_source_unreviewed",
                ),
            )
            conn.execute("INSERT INTO record_search VALUES(?,?,?,?)", (title, text, "", "出口 申报 备案"))
    result = CountryPacks(tmp_path).search("CN", query, 12, as_of="2026-11-01")
    selected = {item["metadata"]["source_record_id"]: item for item in result["items"]}
    assert {"CN-2", "CN-3", "CN-4", "CN-5", "CN-6"} <= selected.keys()
    assert all(
        selected[key]["metadata"].get("retrieval_relation") for key in ("CN-2", "CN-3", "CN-4", "CN-5")
    )


def test_exact_chinese_clause_is_readable_with_date_provenance_without_approval(tmp_path):
    pack_index(tmp_path)
    path = tmp_path / "legal-library" / "index.sqlite3"
    text = "第五百七十九条 当事人一方未支付价款、报酬、租金、利息，或者不履行其他金钱债务的，对方可以请求其支付。"
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','1')")
        conn.execute(
            "INSERT INTO records SELECT 'CN-record','CN','cn-doc','unit-579','中华人民共和国民法典','579',?,'',source_url,source_path,'cn-hash','法律；规范类型：法律','zh','local_source_unreviewed' FROM records WHERE id='ID-record'",
            (text,),
        )
    catalog = tmp_path / "legal-library/source-packs/CN/茶渡中国法律资料/数据/法规与标准目录.jsonl"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "document_id": "cn-doc",
                "original_file_hash": "cn-hash",
                "version_id": "version-1",
                "effective_date": "2021-01-01",
                "effective_date_evidence": [{"date": "2021-01-01", "quote": "本法自2021年1月1日起施行。"}],
            },
            ensure_ascii=False,
        )
    )
    packs = CountryPacks(tmp_path)
    item = packs.search("CN", "《中华人民共和国民法典》第五百七十九条", 1)["items"][0]
    assert item["locator"]["article"] == "579"
    assert item["metadata"]["effective_from"] == "2021-01-01"
    assert item["metadata"]["date_evidence"][0]["quote"].endswith("施行。")
    assert item["metadata"]["review_status"] == "draft"
    assert item["metadata"]["effectivity"] == "unreviewed"
    assert packs.read("CN", "CN-record")["excerpt"] == text
    assert packs.read("ID", "CN-record") is None
    doc = json.loads(catalog.read_text())
    doc["original_file_hash"] = "different-version"
    catalog.write_text(json.dumps(doc))
    assert packs.read("CN", "CN-record")["metadata"]["effective_from"] is None


def test_old_official_snapshot_resolves_only_exact_catalog_family_in_its_effective_period(
    tmp_path, monkeypatch
):
    pack_index(tmp_path)
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(
        packs,
        "document_metadata",
        lambda row: {
            "catalog_title": "示例合同司法解释",
            "declared_version_date": "2020-12-23",
            "effective_from": "2021-01-01",
        },
    )
    found = packs.later_version("ID", "示例合同司法解释（2012年通过） - 发布机关", "2012-03-31", "2026-12-01")
    assert found["effective_from"] == "2021-01-01"
    assert packs.later_version("ID", "另一合同司法解释", "2012-03-31", "2026-12-01") is None
    assert packs.later_version("ID", "示例合同司法解释", "2012-03-31", "2020-12-30") is None
    assert packs.later_version("ID", "示例合同司法解释", None, "2026-12-01") is None


def test_unlocatable_chinese_record_is_excluded_from_search_but_kept_for_audit(tmp_path):
    pack_index(tmp_path)
    path = tmp_path / "legal-library" / "index.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE records SET country='CN',status='source_location_unverified',title='中华人民共和国海关法',article='11' WHERE id='ID-record'"
        )
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','1')")
    packs = CountryPacks(tmp_path)
    assert packs.search("CN", "《中华人民共和国海关法》第十一条", 5)["items"] == []
    assert packs.read("CN", "ID-record")["metadata"]["source_location_verified"] is False


@pytest.mark.parametrize("unusable", ["unlocatable", "oversized", "empty"])
def test_official_fallback_runs_when_internal_candidates_cannot_supply_original(
    tmp_path, monkeypatch, unusable
):
    from app.contexts.legal_analysis.infrastructure import official_evidence

    pack_index(tmp_path)
    service = Service(tmp_path)
    template = {"origin": "CN", "destination": "ID", "agent": {"legal": {}}}
    candidate = {**CountryPacks(tmp_path).read("ID", "ID-record"), "id": "internal"}
    if unusable == "unlocatable":
        candidate["metadata"]["source_location_verified"] = False
    elif unusable == "oversized":
        candidate["excerpt"] = "a" * 5001
    else:
        candidate["excerpt"] = ""
    monkeypatch.setattr(
        service.legal, "search", lambda *a, **kw: {"items": [candidate], "mode": "local_pack"}
    )
    calls = []
    official = {**CountryPacks(tmp_path).read("ID", "ID-record"), "chunk_id": "official-chunk"}

    def search(*args, **kwargs):
        calls.append(args[2])
        return {"items": [official], "mode": "official_web", "acquisition": []}

    monkeypatch.setenv("AGENT_TOOLS_API_KEY", "fixture-only")
    monkeypatch.setattr(official_evidence, "search_official", search)
    result = service.legal.acquire_evidence("owner", "food", None, template, ["ID"])
    assert calls == ["ID"]
    assert result["mode"] == "official_web"
    assert len(result["items"]) == 1
    assert result["acquisition"][0]["usable_count"] == 0
    assert len(result["acquisition"][0]["excluded"]) == 1


def test_complete_pack_original_is_read_before_deciding_fallback(tmp_path, monkeypatch):
    from app.contexts.legal_analysis.infrastructure import official_evidence

    pack_index(tmp_path)
    with sqlite3.connect(tmp_path / "legal-library/index.sqlite3") as conn:
        conn.execute(
            "UPDATE records SET text=? WHERE id='ID-record'", ("Label requirements and exceptions. " * 90,)
        )
    service = Service(tmp_path)
    monkeypatch.setenv("AGENT_TOOLS_API_KEY", "fixture-only")
    monkeypatch.setattr(
        official_evidence, "search_official", lambda *a, **kw: pytest.fail("full original available")
    )
    template = {"origin": "CN", "destination": "ID", "agent": {"legal": {}}}
    result = service.legal.acquire_evidence("owner", "label", None, template, ["ID"])
    assert result["items"][0]["metadata"]["context_complete"] is True
    assert result["items"][0]["excerpt"].count("exceptions") == 90


def test_named_act_without_article_finds_current_tail_and_commencement(tmp_path, monkeypatch):
    """A highly relevant old article must not outrank a dated replacement."""
    pack_index(tmp_path)
    path = tmp_path / "legal-library"
    with sqlite3.connect(path / "index.sqlite3") as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','8')")
        for version, count in (("old", 2), ("new", 3)):
            for article in ("11", "80", "99")[:count]:
                text = (
                    "本法自2026年3月1日起施行。"
                    if article == "99"
                    else (
                        "经营主体依法登记即可开展对外贸易。"
                        if article == "11"
                        else "经营者备案登记与法律责任适用条件，应根据本法确定。"
                    )
                )
                conn.execute(
                    "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"CN-{version}-{article}",
                        "CN",
                        version,
                        "unit-" + article,
                        "中华人民共和国测试贸易法",
                        article,
                        text,
                        "",
                        "https://www.mofcom.gov.cn/law",
                        "law.html",
                        "hash-" + version,
                        "规范类型：法律",
                        "zh",
                        "unreviewed",
                    ),
                )
                conn.execute(
                    "INSERT INTO record_search VALUES(?,?,?,?)",
                    ("中华人民共和国测试贸易法", text, "", "备案 登记 贸易 法律"),
                )
    catalog = path / "source-packs/CN/茶渡中国法律资料/数据"
    catalog.mkdir(parents=True)
    (catalog / "法规与标准目录.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "document_id": v,
                    "title": "中华人民共和国测试贸易法",
                    "original_file_hash": "hash-" + v,
                    "version_effective_date": day,
                    "version_date": day,
                },
                ensure_ascii=False,
            )
            for v, day in (("old", "2020-01-01"), ("new", "2026-03-01"))
        )
    )
    (catalog / "修改废止替代线索.jsonl").write_text(
        json.dumps(
            {
                "source_document_id": "new",
                "target_document_id": "old",
                "target_version_resolved": True,
                "relation_type": "新版本施行，旧版本区间结束",
                "effective_date": "2026-03-01",
            },
            ensure_ascii=False,
        )
    )
    packs = CountryPacks(tmp_path)
    result = packs.search("CN", "《测试贸易法》经营者备案登记", 3, as_of="2026-11-01")
    assert len(result["items"]) == 3
    assert all(item["metadata"]["source_record_id"].startswith("CN-new") for item in result["items"])
    assert {item["locator"]["article"] for item in result["items"]} == {"11", "80", "99"}
    assert all(item["metadata"]["review_status"] == "draft" for item in result["items"])
    historical = packs.search("CN", "《测试贸易法》第十一条", 1, as_of="2025-06-01")
    assert historical["items"][0]["metadata"]["source_record_id"] == "CN-old-11"
    plain_name = packs.search("CN", "测试贸易法 经营者登记", 3, as_of="2026-11-01")
    assert any(item["locator"]["article"] == "99" for item in plain_name["items"])


def test_named_regulator_gap_is_not_filled_by_general_import_law():
    from app.contexts.legal_analysis.application.analysis_service import _specific_source_gap

    generic = [{"title": "贸易法", "excerpt": "Importers must register certain goods with the Minister."}]
    assert _specific_source_gap("ID: BPOM 注册 食品", generic)
    assert not _specific_source_gap(
        "ID: BPOM 注册 食品",
        [*generic, {"title": "BPOM食品注册规定", "excerpt": "BPOM registers processed foods."}],
    )
    assert _specific_source_gap("《食品注册条例》进口食品", generic)
    assert not _specific_source_gap("ID: import licence", generic)


def test_relevance_precedes_date_completeness(tmp_path, monkeypatch):
    pack_index(tmp_path)
    path = tmp_path / "legal-library/index.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE records SET text=? WHERE id='ID-record'",
            ("Food label information for importers, including mandatory label content.",),
        )
        conn.execute(
            "INSERT INTO records SELECT 'dated','ID','dated-doc',unit_id,'Transport rule',article,'A carrier must keep a transport licence for road transport.',translation,source_url,source_path,'dated-hash',source_type,language,status FROM records WHERE id='ID-record'"
        )
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(
        packs.semantic,
        "search",
        lambda *a: ([("dated-doc", "dated-hash", 0.1), ("ID-doc", "ID-hash", 0.8)], "ready"),
    )
    monkeypatch.setattr(
        packs,
        "document_metadata",
        lambda row: {"effective_from": "2020-01-01"} if row["id"] == "dated" else {},
    )
    result = packs.search("ID", "food label importers", 1)
    assert result["items"][0]["metadata"]["source_record_id"] == "ID-record"


def test_issuing_authority_title_does_not_override_subject_relevance(tmp_path, monkeypatch):
    pack_index(tmp_path)
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(packs.semantic, "search", lambda *a: ([], "unavailable"))
    monkeypatch.setattr(packs.clauses, "search", lambda *a: ([], "unavailable"))
    with sqlite3.connect(packs.path) as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','2')")
        for identifier, title, text in [
            ("authority", "中华人民共和国海关总署", "海关总署公告：关于程序事项的行政处罚规定。"),
            ("food", "测试食品办法", "出口食品生产企业应当向住所地海关备案，备案程序和要求另行制定。"),
        ]:
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    "CN",
                    identifier,
                    "unit-" + identifier,
                    title,
                    "42",
                    text,
                    "",
                    "https://www.gov.cn/test",
                    "test.html",
                    identifier,
                    "法规原文",
                    "zh",
                    "unreviewed",
                ),
            )
            conn.execute(
                "INSERT INTO record_search VALUES(?,?,?,?)", (title, text, "", "出口 食品 生产 企业 备案")
            )
    result = packs.search("CN", "出口食品生产企业备案 海关总署", 1)
    assert result["items"][0]["metadata"]["source_record_id"] == "food"
    assert not result["items"][0]["metadata"]["exact_match"]


def test_current_subject_definition_survives_old_article_and_document_cap(tmp_path, monkeypatch):
    pack_index(tmp_path)
    packs = CountryPacks(tmp_path)
    monkeypatch.setattr(packs.semantic, "search", lambda *a: ([], "unavailable"))
    monkeypatch.setattr(packs.clauses, "search", lambda *a: ([], "unavailable"))
    with sqlite3.connect(packs.path) as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','7')")
        for article, text in [
            ("9", "第九条 进出口经营按照国际协定给予国民待遇，实施管理。"),
            ("11", "第十一条 本法所称跨境经营者，是指依法办理经营主体登记，从事跨境经营的个人、组织。"),
            ("20", "第二十条 跨境经营者备案登记及出口申报活动不得扰乱出口管理秩序。"),
            ("21", "第二十一条 跨境经营者备案登记及出口申报应当遵守行政管理规定。"),
            ("22", "第二十二条 跨境经营者备案登记及出口申报应当如实报告相关信息。"),
            ("23", "第二十三条 跨境经营者备案登记及出口申报不得提供不实信息。"),
            ("99", "第九十九条 本法自2026年3月1日起施行。"),
        ]:
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "CN-" + article,
                    "CN",
                    "act",
                    article,
                    "测试跨境经营法",
                    article,
                    text,
                    "",
                    "https://example.gov/law",
                    "law.html",
                    "hash",
                    "法律；规范类型：法律",
                    "zh",
                    "unreviewed",
                ),
            )
            conn.execute(
                "INSERT INTO record_search VALUES(?,?,?,?)",
                ("测试跨境经营法", text, "", "跨境 经营 备案 登记 出口 申报"),
            )
    result = packs.search("CN", "《测试跨境经营法》第九条 跨境经营者备案登记 出口申报", 6, as_of="2026-11-01")
    articles = {r["locator"]["article"] for r in result["items"]}
    assert {"9", "11", "99"} <= articles


def test_unnamed_procedure_in_recalled_body_follows_enabling_law(tmp_path):
    pack_index(tmp_path)
    packs = CountryPacks(tmp_path)
    records = [
        ("seed", "测试货物申报规定", "申报人应取得特殊商品经营者备案登记，并提交真实申报材料。"),
        ("procedure", "特殊商品经营者备案登记办法", "第一条 根据《测试经营法》制定本办法。"),
        ("parent", "测试经营法", "本法所称经营者，是指依法登记从事经营的组织或个人。"),
        (
            "change",
            "关于修改《测试经营法》的决定",
            "删去特殊商品经营者备案登记条款。本决定自公布之日起施行。",
        ),
    ]
    catalog = packs.path.parent / "source-packs/CN/茶渡中国法律资料/数据"
    catalog.mkdir(parents=True)
    (catalog / "法规与标准目录.jsonl").write_text(
        "\n".join(
            json.dumps({"document_id": identifier, "title": title, "original_file_hash": identifier})
            for identifier, title, _ in records
        )
    )
    with sqlite3.connect(packs.path) as conn:
        for identifier, title, text in records:
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    "CN",
                    identifier,
                    "unit",
                    "测试发布机关",
                    "1",
                    text,
                    "",
                    "https://example.gov/law",
                    "law.html",
                    identifier,
                    "法规原文",
                    "zh",
                    "unreviewed",
                ),
            )
        conn.row_factory = sqlite3.Row
        seed = conn.execute("SELECT * FROM records WHERE id='seed'").fetchone()
        related = packs._related_rows(conn, "CN", "首次出口商品需要哪些申报手续", [seed])
    assert {"procedure", "parent", "change"} <= {row["id"] for row in related}
