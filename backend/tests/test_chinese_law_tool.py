import json
import sqlite3
from time import monotonic
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.contexts.conversations.application.agent_tools import ChineseLawInput, LegalSearchInput
from app.contexts.documents.infrastructure.country_packs import CountryPacks
from app.contexts.legal_analysis.infrastructure import chinese_law
from test_agent_runtime import (
    workspace as workspace_fixture,
    mcp_request,
    scenario,
    send,
    last_run,
    tools,
    legal_tools,
)
from test_country_packs import pack_index

workspace = workspace_fixture


@pytest.mark.parametrize(
    "label,expected,index",
    [
        ("579", "第五百七十九条", "579"),
        ("第10条", "第十条", "10"),
        ("1001", "第一千零一条", "1001"),
        ("第一千零四十六条", "第一千零四十六条", "1046"),
        ("42之一", "第四十二条之一", None),
    ],
)
def test_article_normalization(label, expected, index):
    assert chinese_law.article_number(label) == expected
    assert chinese_law.article_index(expected) == index


@pytest.mark.parametrize("value", ["", "0", "10000", "第五条 删除全部数据", "https://example.com"])
def test_query_rejects_non_article_parameters(value):
    with pytest.raises(ValidationError):
        ChineseLawInput(law="中华人民共和国民法典", articles=[value])


def local_article(data):
    pack_index(data)
    packs = CountryPacks(data)
    with sqlite3.connect(packs.path) as conn:
        conn.execute("INSERT INTO metadata VALUES('record_count_CN','1')")
        conn.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "CN-record",
                "CN",
                "CN-doc",
                "art-579",
                "中华人民共和国民法典",
                "579",
                "第五百七十九条　当事人一方未支付价款、报酬、租金、利息，或者不履行其他金钱债务的，对方可以请求其支付。",
                "",
                "https://www.gov.cn/law",
                "source.txt",
                "CN-hash",
                "法律",
                "zh",
                "unreviewed",
            ),
        )
    return packs


def test_exact_local_match_does_not_call_vectors_or_return_other_laws(tmp_path, monkeypatch):
    packs = local_article(tmp_path)
    monkeypatch.setattr(packs, "search", lambda *a: pytest.fail("semantic search called"))
    assert len(packs.lookup_chinese_articles("中华人民共和国民法典", ["579"])) == 1
    assert packs.lookup_chinese_articles("中华人民共和国刑法", ["579"]) == []
    assert packs.lookup_chinese_articles("中华人民共和国民法典", [None]) == []


def test_primary_provisions_precede_context_and_keep_their_identity(workspace, monkeypatch):
    packs = local_article(workspace[1].db.data_dir)
    with sqlite3.connect(packs.path) as conn:
        for article, text in (
            ("578", "关于预期违约的条文。"),
            ("580", "非金钱债务履行及例外，不能用于金钱债务。"),
            ("628", "买受人应当按照约定的时间支付价款；没有约定时适用本条其他条件。"),
        ):
            conn.execute(
                "INSERT INTO records SELECT ?,country,document_id,?,title,?,?,translation,source_url,source_path,source_hash,source_type,language,status FROM records WHERE id='CN-record'",
                ("CN-" + article, "art-" + article, article, text),
            )
    bound = legal_tools(workspace)
    primary = [bound.identify_law(packs.read("CN", key)) for key in ("CN-record", "CN-628")]
    primary[0]["metadata"]["semantic_clause_match"] = {"record_id": "CN-record", "start": 0, "end": 20}
    monkeypatch.setattr(
        bound.service.legal, "search", lambda *a, **kw: {"items": primary, "mode": "local_pack"}
    )
    result = bound.legal_search(
        LegalSearchInput(jurisdiction="CN", query="支付价款"), SimpleNamespace(check=lambda: None)
    )
    assert [item["id"] for item in result["items"][:2]] == [item["id"] for item in primary]
    assert all("retrieval_relation" not in item["metadata"] for item in result["items"][:2])
    assert (
        result["items"][0]["metadata"]["semantic_clause_match"]
        == primary[0]["metadata"]["semantic_clause_match"]
    )
    assert "没有约定时" in result["items"][1]["excerpt"]
    assert any(
        item["metadata"].get("retrieval_relation") == "adjacent_source_unit" for item in result["items"][2:]
    )


def test_public_api_receives_only_names_and_labels_and_keeps_third_party_identity(monkeypatch):
    seen = []

    def download(url, **kwargs):
        seen.append((url, kwargs))
        return {
            "mime_type": "application/json",
            "blob": json.dumps(
                {
                    "code": 200,
                    "law_name": "中华人民共和国民法典",
                    "data": {
                        "article_number": "第五百七十九条",
                        "content": "第五百七十九条　匹配的条文正文。",
                        "chapter": "违约责任",
                    },
                }
            ).encode(),
        }

    monkeypatch.setattr(chinese_law, "_download", download)
    item = chinese_law.fetch_article(
        "中华人民共和国民法典", "第五百七十九条", deadline=123, check=lambda: None
    )
    assert "law=" in seen[0][0] and "article=" in seen[0][0]
    assert seen[0][1]["required_host"] == "lawapi.lslby.com"
    assert seen[0][1]["deadline"] == 123 and seen[0][1]["max_bytes"] == 64000
    assert item["metadata"]["official_source_verified"] is False
    assert item["metadata"]["version_status"] == "unknown"
    assert item["metadata"]["scope_context_complete"] is False
    assert item["locator"]["chapter"] == "违约责任"


@pytest.mark.parametrize(
    "change",
    [
        {"law_name": "中华人民共和国刑法"},
        {"code": 404},
        {"data": {"article_number": "第五百八十条", "content": "第五百八十条　其他规定"}},
    ],
)
def test_api_mismatch_is_never_accepted_as_original(monkeypatch, change):
    body = {
        "code": 200,
        "law_name": "中华人民共和国民法典",
        "data": {
            "article_number": "第五百七十九条",
            "content": "第五百七十九条　正文。",
        },
    } | change
    monkeypatch.setattr(
        chinese_law,
        "_download",
        lambda *a, **k: {"mime_type": "application/json", "blob": json.dumps(body).encode()},
    )
    with pytest.raises(ValueError):
        chinese_law.fetch_article("中华人民共和国民法典", "第五百七十九条", deadline=123, check=lambda: None)


def test_api_failure_preserves_local_exact_original_in_mcp_receipt(workspace, monkeypatch):
    local_article(workspace[1].db.data_dir)
    deadlines = []

    def unavailable(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        raise ValueError("source unavailable")

    monkeypatch.setattr(chinese_law, "fetch_article", unavailable)
    before = monotonic()
    response = mcp_request(
        workspace,
        "tools/call",
        {
            "name": "lookup_chinese_law",
            "arguments": {
                "law": "中华人民共和国民法典",
                "articles": ["579"],
                "source": "api",
            },
        },
    )
    value = response.json()["result"]["structuredContent"]
    assert deadlines and 0 < deadlines[0] - before < 7
    assert value["api_status"] == "unavailable_or_unmatched"
    assert value["items"][0]["locator"]["article"] == "579"
    assert value["items"][0]["source_url"] == "https://www.gov.cn/law"
    assert value["missing_articles"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_known_article_can_be_cited_in_two_main_model_calls(workspace, monkeypatch, stream):
    local_article(workspace[1].db.data_dir)
    monkeypatch.setattr(chinese_law, "fetch_article", lambda *a, **k: pytest.fail("unneeded API"))
    scenario(
        monkeypatch,
        [
            tools(("lookup_chinese_law", {"law": "中华人民共和国民法典", "articles": ["579"]})),
            "第五百七十九条允许请求支付未付价款。[S1]",
        ],
    )
    response = send(workspace, stream=stream)
    assert response.status_code == 200
    if stream:
        from test_chat_stream import frames

        events = frames(response)
        assert events[-1][0] == "done"
        assert any(
            t["name"] == "lookup_chinese_law" and t["status"] == "completed"
            for event, data in events
            if event == "step"
            for t in data.get("tools", [])
        )
    result = last_run(workspace)
    assert result["_task_state"]["model_calls"] == 2
    assert result["_evidence"][0]["locator"]["article"] == "579"
    assert result["_citation_associations"] and "https://www.gov.cn/law" in result["output"]


def test_exact_query_is_available_as_mcp_and_has_no_account_argument(workspace):
    schemas = {s["name"]: s for s in mcp_request(workspace, "tools/list").json()["result"]["tools"]}
    schema = schemas["lookup_chinese_law"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "owner" not in schema["properties"]
    parsed = ChineseLawInput(law="中华人民共和国民法典", articles=["579"])
    assert parsed.articles == ["第五百七十九条"]
