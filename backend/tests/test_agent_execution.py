"""One model plus evidence: end-to-end runtime contracts with isolated providers."""

from contextlib import closing

import pytest
from app.contexts.conversations.application import agent_loop
from app.contexts.conversations.application.agent_tools import AgentTools, WebInput
from app.contexts.conversations.application.answer_sources import AnswerSources
from app.contexts.conversations.application.execution_plan import ExecutionBudget
from app.contexts.conversations.application.tool_runtime import ToolFailure
from test_agent_runtime import workspace as workspace_fixture, send, tools, last_run, scenario

workspace = workspace_fixture


def post_emitting(workspace, monkeypatch, turn, events):
    scenario(monkeypatch, [])
    monkeypatch.setattr(agent_loop, "agent_turn", turn)
    _, service, owner, case, payload = workspace
    return service.post_message(
        owner, case["id"], payload, emit=lambda event, data: events.append((event, data))
    )


def displayed_answer(events):
    output = ""
    for event, data in events:
        if event == "start" and not data.get("append"):
            output = ""
        elif event == "delta":
            output += data["text"]
    return output


def test_answer_chunks_are_delivered_before_model_completion_without_final_replay(workspace, monkeypatch):
    events = []

    async def turn(messages, definitions, delta, check, **kwargs):
        delta("先核对产品。")
        assert displayed_answer(events) == "先核对产品。"
        delta("再确认目标市场。")
        assert displayed_answer(events) == "先核对产品。再确认目标市场。"
        return {"role": "assistant", "content": displayed_answer(events)}

    result = post_emitting(workspace, monkeypatch, turn, events)
    assert [value["text"] for event, value in events if event == "delta"] == [
        "先核对产品。",
        "再确认目标市场。",
    ]
    assert sum(event == "start" for event, _ in events) == 1
    assert result["messages"][1]["content"] == displayed_answer(events)


def test_split_citations_and_foreign_downloads_never_leak_into_stream(workspace, monkeypatch):
    events = []
    calls = 0
    monkeypatch.setattr(
        AgentTools, "web", lambda *a: {"sources": [{"url": "https://expo.example/", "title": "官网"}]}
    )

    async def turn(messages, definitions, delta, check, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tools(("web_search", {"query": "官网"}))
        fragments = ["请核对[S", "1]。", "缺少[S99]。", "[下载](/api/tea/documents/foreign/", "download)"]
        for fragment in fragments:
            delta(fragment)
            assert "[S" not in displayed_answer(events)
            assert "/foreign/" not in displayed_answer(events)
        return {"role": "assistant", "content": "".join(fragments)}

    result = post_emitting(workspace, monkeypatch, turn, events)
    assert "https://expo.example/" in displayed_answer(events)
    assert "该处尚未取得对应原文链接" in displayed_answer(events)
    assert result["messages"][1]["content"] == displayed_answer(events)


def test_split_unexecuted_tool_protocol_is_withheld_and_repaired(workspace, monkeypatch):
    events = []
    calls = 0

    async def turn(messages, definitions, delta, check, **kwargs):
        nonlocal calls
        calls += 1
        fragments = ["<tool_", "call><function=web_search>", "private</function></tool_call>"]
        if calls == 1:
            for fragment in fragments:
                delta(fragment)
                assert displayed_answer(events) == ""
            return {"role": "assistant", "content": "".join(fragments)}
        delta("尚未取得对应来源。")
        return {"role": "assistant", "content": "尚未取得对应来源。"}

    result = post_emitting(workspace, monkeypatch, turn, events)
    assert result["messages"][1]["content"] == "尚未取得对应来源。"
    assert not last_run(workspace)["_task_state"]["calls"]


def test_pre_tool_text_is_cleared_before_executing_the_tool(workspace, monkeypatch):
    events = []
    calls = 0

    def web(*args):
        assert displayed_answer(events) == ""
        return {"sources": []}

    monkeypatch.setattr(AgentTools, "web", web)

    async def turn(messages, definitions, delta, check, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            delta("我会先核对来源。")
            return tools(("web_search", {"query": "官网"})) | {"content": "我会先核对来源。"}
        delta("尚未找到来源。")
        return {"role": "assistant", "content": "尚未找到来源。"}

    post_emitting(workspace, monkeypatch, turn, events)
    assert displayed_answer(events) == "尚未找到来源。"


def test_interrupted_live_answer_is_not_replayed_or_retried(workspace, monkeypatch):
    events = []
    calls = 0

    async def turn(messages, definitions, delta, check, **kwargs):
        nonlocal calls
        calls += 1
        delta("已完成的一段。")
        assert displayed_answer(events) == "已完成的一段。"
        error = ToolFailure("upstream_error", "controlled interruption")
        error.partial_content = "已完成的一段。"
        raise error

    result = post_emitting(workspace, monkeypatch, turn, events)
    assert calls == 1
    assert displayed_answer(events).count("已完成的一段。") == 1
    assert result["messages"][1]["content"] == displayed_answer(events)
    assert last_run(workspace)["_task_state"]["delivery"] == "partially_answered"


@pytest.mark.parametrize("field", ["limit", "max_results"])
def test_web_result_count_accepts_bounded_historical_count(field):
    assert WebInput.model_validate({"query": "茶叶", field: "5"}).max_results == 5


@pytest.mark.parametrize("count", ["0", "11", "-1", "1.5", True, "nan"])
def test_web_result_count_keeps_strict_boundaries(count):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WebInput.model_validate({"query": "茶叶", "limit": count})


@pytest.mark.parametrize("recency", [None, "none", "NULL", " "])
def test_web_optional_recency_accepts_an_empty_filter(recency):
    assert WebInput.model_validate({"query": "展会网站", "recency_days": recency}).recency_days is None


@pytest.mark.parametrize("recency", [0, -1, 3651, True, "7.5"])
def test_web_optional_recency_keeps_numeric_boundaries(recency):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WebInput.model_validate({"query": "展会网站", "recency_days": recency})


def test_search_snippet_identity_survives_model_projection_and_citation():
    sources = AnswerSources()
    original = {"sources": [{"url": "https://www.gov.cn/example", "title": "公告", "excerpt": "摘要"}]}
    sources.collect(original)
    projected = sources.project(original)["sources"][0]
    assert projected["id"] == sources.sources["S1"]["id"]
    assert projected["reference"] == "S1"
    assert projected["metadata"]["content_role"] == "search_snippet"
    assert "公告：[https://www.gov.cn/example](https://www.gov.cn/example)" in sources.render("来源[S1]")
    assert "id" not in original["sources"][0]


def test_current_web_links_remain_visible_without_model_citation_handles(workspace, monkeypatch):
    monkeypatch.setattr(
        AgentTools,
        "web",
        lambda *a: {
            "sources": [{"url": "https://expo.example/apply", "title": "茶博会报名", "excerpt": "报名"}]
        },
    )
    scenario(
        monkeypatch,
        [tools(("web_search", {"query": "茶博会网址"})), "可以在茶博会报名页面申请参展。"],
    )
    assert send(workspace).status_code == 200
    result = last_run(workspace)
    assert "茶博会报名：[https://expo.example/apply](https://expo.example/apply)" in result["output"]
    assert result["_task_state"]["model_calls"] == 2
    assert len(result["citations"]) == 1 and not result["citations"][0].get("document_id")
    assert result["_task_state"]["delivery"] == "answered"


def test_web_link_fallback_deduplicates_urls_and_ignores_history():
    sources = AnswerSources()
    sources.collect({"sources": [{"title": "旧网页", "url": "https://old.example/"}]}, historical=True)
    sources.collect({"sources": [{"title": "公司网站", "url": "http://company.example/"}] * 2})
    linked = sources.render(sources.with_web_links("可查看公司网站。"))
    assert linked.count("公司网站：") == 1
    assert "http://company.example/" in linked and "old.example" not in linked
    direct = "公司网站：http://company.example/"
    assert sources.with_web_links(direct) == direct
    assert sources.with_web_links("公司网站[S2]") == "公司网站[S2]"


def test_retrieved_plain_url_after_chinese_punctuation_becomes_clickable():
    sources = AnswerSources()
    sources.collect({"sources": [{"title": "展会报名", "url": "https://expo.example/apply"}]})
    rendered = sources.render("报名入口：https://expo.example/apply。")
    assert rendered == "报名入口：[https://expo.example/apply](https://expo.example/apply)。"
    for protected in (
        "[报名入口](https://expo.example/apply)",
        "`https://expo.example/apply`",
        "```text\nhttps://expo.example/apply\n```",
        "<https://expo.example/apply>",
        "相似地址：https://expo.example/apply/other",
    ):
        assert sources.render(protected) == protected


def test_empty_tool_discovery_does_not_disable_actual_web_search(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(
        AgentTools,
        "web",
        lambda *a: {"sources": [{"url": "https://expo.example/", "title": "展会官网"}]},
    )
    scenario(
        monkeypatch,
        [
            tools(("tool_search", {"query": "xyzzyunavailable"})),
            tools(("web_search", {"query": "展会官网"})),
            "展会官网[S1]。",
        ],
        seen,
        auto_discover=False,
    )
    assert send(workspace).status_code == 200
    assert "web_search" in {tool["function"]["name"] for tool in seen[1][1]}
    calls = last_run(workspace)["_task_state"]["calls"]
    assert [call["tool"] for call in calls] == ["tool_search", "web_search"]
    assert "https://expo.example/" in last_run(workspace)["output"]


@pytest.mark.parametrize("label", ["第五百七十九条", "第579条", "579条"])
def test_exact_official_url_selects_only_the_named_provision(label):
    sources = AnswerSources()
    sources.collect(
        {"items": [{**citation(str(n)), "locator": {"article": str(n)}} for n in (579, 580, 1579)]}
    )
    assert sources.used(f"民法典{label}规定可要求付款。https://www.gov.cn/test-rule") == ["S1"]
    assert sources.used("见https://www.gov.cn/test-rule") == []
    assert sources.used("第579条：https://www.gov.cn/test-rule/other") == []


def test_projected_index_snapshot_survives_history_without_becoming_a_statutory_version():
    import json

    sources = AnswerSources()
    material = {
        "items": [
            {
                **citation("pack-row"),
                "document_id": "pack-CN-law",
                "document_version": 178678979,
                "metadata": {"published_at": "2020-05-28", "effective_from": "2021-01-01"},
            }
        ]
    }
    sources.collect(material)
    projected = sources.project(material)
    row = projected["items"][0]
    assert "document_version" not in row
    assert row["index_snapshot"] == 178678979
    assert row["metadata"]["effective_from"] == "2021-01-01"
    historical = AnswerSources([{"role": "tool", "content": json.dumps(projected)}])
    assert historical.sources["S1"]["document_version"] == 178678979
    assert historical.sources["S1"]["id"] == "pack-row"
    assert material["items"][0]["document_version"] == 178678979


@pytest.mark.parametrize("name", ["民法典", "中华人民共和国民法典"])
def test_natural_law_name_and_article_citation_selects_only_actual_retrieved_records(name):
    sources = AnswerSources()
    sources.collect(
        {
            "items": [
                {**citation("payment"), "title": "中华人民共和国民法典", "locator": {"article": "579"}},
                {**citation("other"), "title": "另一部法律", "locator": {"article": "579"}},
                {**citation("unused"), "title": "中华人民共和国民法典", "locator": {"article": "580"}},
            ]
        }
    )
    assert sources.used(f"依据《{name}》第五百七十九条可以请求支付。") == ["S1"]
    assert sources.used("参见《民法典》。") == []
    assert sources.used("第五百七十九条") == []
    assert sources.used("《不存在的法律》第579条") == []


def test_discovery_budget_removes_discovery_but_keeps_web_available(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(AgentTools, "web", lambda *a: {"sources": []})
    second = tools(("tool_search", {"query": "anotherunavailablecapability"}))
    second["tool_calls"][0]["id"] = "second-discovery"
    scenario(
        monkeypatch,
        [
            tools(("tool_search", {"query": "xyzzyunavailable"})),
            second,
            tools(("web_search", {"query": "展会官网"})),
            "本次搜索未找到对应页面。",
        ],
        seen,
        auto_discover=False,
    )
    assert send(workspace).status_code == 200
    available = {tool["function"]["name"] for tool in seen[2][1]}
    assert "web_search" in available and not available & agent_loop.DISCOVERY
    assert last_run(workspace)["_task_state"]["calls"][-1]["tool"] == "web_search"


def test_business_web_results_accept_public_http_and_nonofficial_sites(workspace, monkeypatch):
    async def gateway(*args, **kwargs):
        return {
            "output": {
                "sources": [
                    {"title": "行业公司", "url": "http://company.example/", "excerpt": "企业业务介绍"},
                    {"title": "行业报道", "url": "https://media.example/news", "snippet": "市场信息"},
                    {"title": "损坏地址", "url": "https://[broken"},
                    {"title": "非网页协议", "url": "javascript:alert(1)"},
                    {"title": "带凭据地址", "url": "https://user:secret@example.com/"},
                ]
            }
        }

    monkeypatch.setattr("app.contexts.conversations.application.agent_tools.gateway_tool", gateway)
    scenario(
        monkeypatch,
        [tools(("web_search", {"query": "茶叶行业网站"})), "可查看这些行业网站。"],
    )
    response = send(workspace)
    assert response.status_code == 200
    result = last_run(workspace)
    assert "http://company.example/" in result["output"] and "https://media.example/news" in result["output"]
    assert len(result["citations"]) == 2
    assert result["citations"][0]["excerpt"] == "企业业务介绍"
    assert "unreviewed_web" not in response.text and "javascript:" not in result["output"]


def test_projection_keeps_all_provisions_and_shares_identical_version_evidence():
    sources = AnswerSources()
    items = [
        {
            **citation(str(i), text="主体条件、正文和例外"),
            "document_id": "law",
            "document_version": 1,
            "metadata": {
                "effective_from": "2021-01-01",
                "version_relations": [{"evidence_text": "完整修法条件"}],
            },
        }
        for i in range(2)
    ]
    original = {"items": items}
    sources.collect(original)
    projected = sources.project(original)["items"]
    assert projected[0]["metadata"]["version_relations"] == items[0]["metadata"]["version_relations"]
    assert projected[1]["metadata"]["same_version_metadata_as"] == "S1"
    assert all(row["excerpt"] == "主体条件、正文和例外" for row in projected)
    assert sources.sources["S2"]["metadata"]["version_relations"] == items[1]["metadata"]["version_relations"]


def test_parameter_error_then_search_still_allows_official_original_read(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(
        AgentTools,
        "web",
        lambda *a: {"sources": [{"url": "https://www.gov.cn/law", "title": "官方页", "excerpt": "摘要"}]},
    )
    monkeypatch.setattr(AgentTools, "read_law", lambda *a: {"items": [citation("original")]})
    search = tools(("web_search", {"query": "付款", "max_results": 3}))
    search["tool_calls"][0]["id"] = "corrected-search"
    scenario(
        monkeypatch,
        [
            tools(("web_search", {"query": "付款", "max_results": 99})),
            search,
            tools(("read_law", {"jurisdiction": "CN", "url": "https://www.gov.cn/law"})),
            "按已读原文可以要求付款。[S2]",
        ],
        seen,
    )
    assert send(workspace).status_code == 200
    state = last_run(workspace)["_task_state"]
    assert state["calls"][0]["error"] == "invalid_input"
    assert state["calls"][-1]["tool"] == "read_law"
    assert "read_law" in {t["function"]["name"] for t in seen[2][1]}
    assert last_run(workspace)["_citation_associations"] == {"S2": "original"}


def test_local_search_then_web_can_read_original_without_another_search(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(AgentTools, "legal_search", lambda *a: {"items": []})
    monkeypatch.setattr(
        AgentTools, "web", lambda *a: {"sources": [{"url": "https://www.gov.cn/law", "excerpt": "摘要"}]}
    )
    monkeypatch.setattr(AgentTools, "read_law", lambda *a: {"items": [citation("original")]})
    scenario(
        monkeypatch,
        [
            tools(("search_legal_evidence", {"jurisdiction": "CN", "query": "付款"})),
            tools(("web_search", {"query": "官方付款条款"})),
            tools(("read_law", {"jurisdiction": "CN", "url": "https://www.gov.cn/law"})),
            "按原文判断。[S2]",
        ],
        seen,
    )
    assert send(workspace).status_code == 200
    assert {t["function"]["name"] for t in seen[2][1]} == {"read_law", "read_tool_result"}
    assert seen[3][1] == []
    assert last_run(workspace)["_citation_associations"] == {"S2": "original"}


def citation(identifier="source", text="合同有效且出卖人完成交付时，买受人应在约定期限内支付价款。"):
    return {
        "id": identifier,
        "title": "测试法原文",
        "excerpt": text,
        "country": "CN",
        "source_url": "https://www.gov.cn/test-rule",
        "metadata": {"effective_from": None, "context_complete": True},
    }


def test_common_evidence_then_direct_answer_is_two_model_calls(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(AgentTools, "legal_search", lambda *a: {"items": [citation()], "mode": "local_pack"})
    monkeypatch.setattr(workspace[1].legal, "analyze", lambda *a, **k: pytest.fail("report submodel called"))
    scenario(
        monkeypatch,
        [
            tools(("search_legal_evidence", {"jurisdiction": "CN", "query": "民法典 付款期限"})),
            "按给定假设，可以要求支付货款并承担违约责任。[S1]",
        ],
        seen,
    )
    response = send(
        workspace,
        {
            **workspace[-1],
            "content": "假设中国国内合同有效、已交货、已逾期且无正当抗辩，可以要求货款及违约责任吗？",
        },
    )
    assert response.status_code == 200, response.text
    result = last_run(workspace)
    assert result["_task_state"]["model_calls"] == 2
    assert [c["tool"] for c in result["_task_state"]["calls"]] == ["search_legal_evidence"]
    assert "plan" not in result["_task_state"] and "brief" not in result["_task_state"]
    assert result["_citation_associations"] == {"S1": "source"}
    assert result["_evidence"][0]["excerpt"] == citation()["excerpt"]
    assert "www.gov.cn/test-rule" in response.text
    assert len(seen) == 2 and seen[0][0][0] == seen[1][0][0]
    assert "analyze_legal" not in str(seen[0][1]) and "update_task_brief" not in str(seen[0][1])
    with closing(workspace[1].db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_analyses").fetchone()[0] == 0


def test_complete_user_original_can_answer_once_without_polishing(workspace, monkeypatch):
    seen = []
    scenario(monkeypatch, ["按提供的条文和交货事实，可以要求支付价款。"], seen)
    assert (
        send(
            workspace, {**workspace[-1], "content": "以下为完整原文……请按合同有效、已交货并逾期的假设分析。"}
        ).status_code
        == 200
    )
    assert len(seen) == 1
    assert last_run(workspace)["_task_state"]["model_calls"] == 1


def test_multi_request_single_country_keeps_original_question_and_all_evidence(workspace, monkeypatch):
    seen = []
    question = (
        "只分析中国手续：我已海关备案，工厂独立，商务备案是否还需办理？工厂要给什么凭据？请分别回答并给顺序。"
    )
    monkeypatch.setattr(
        AgentTools, "legal_search", lambda self, args, attempt: {"items": [citation(args.query)]}
    )
    scenario(
        monkeypatch,
        [
            tools(
                ("search_legal_evidence", {"jurisdiction": "CN", "query": "现行对外贸易法"}),
                ("search_legal_evidence", {"jurisdiction": "CN", "query": "出口食品生产企业备案"}),
            ),
            "商务登记、生产工厂与贸易商分工按各条文分别判断。[S1][S2]",
        ],
        seen,
    )
    assert send(workspace, {**workspace[-1], "content": question}).status_code == 200
    assert all(question in str(messages) for messages, _ in seen)
    assert len(last_run(workspace)["_evidence"]) == 2
    assert all(c["input"]["jurisdiction"] == "CN" for c in last_run(workspace)["_task_state"]["calls"])


def test_empty_evidence_allows_one_supplement_and_preserves_other_answer(workspace, monkeypatch):
    monkeypatch.setattr(AgentTools, "legal_search", lambda *a: {"items": []})
    monkeypatch.setattr(AgentTools, "web", lambda *a: {"sources": []})
    scenario(
        monkeypatch,
        [
            tools(("search_legal_evidence", {"jurisdiction": "ID", "query": "假定证件"})),
            tools(("web_search", {"query": "官方 假定证件 印尼"})),
            "可以先比较签约和收款安排；本次未查明所称证件，无法据此判断是否必办。",
        ],
    )
    r = send(workspace)
    assert r.status_code == 200, r.text
    state = last_run(workspace)["_task_state"]
    assert state["model_calls"] == 3 and state["delivery"] == "partially_answered"
    assert "无法据此判断" in r.text


def test_unknown_reference_preserves_answer_without_another_model_call(workspace, monkeypatch):
    monkeypatch.setattr(AgentTools, "legal_search", lambda *a: {"items": [citation()]})
    scenario(
        monkeypatch,
        [
            tools(("search_legal_evidence", {"jurisdiction": "CN", "query": "付款"})),
            "假设合同有效且无正当抗辩，可以主张。[S99]",
        ],
    )
    r = send(workspace)
    assert r.status_code == 200, r.text
    result = last_run(workspace)
    assert result["_task_state"]["model_calls"] == 2 and len(result["_task_state"]["calls"]) == 1
    assert result["_task_state"]["delivery"] == "partially_answered"
    assert result["_task_state"]["unresolved_citations"] == ["S99"]
    assert "尚未取得对应原文链接" in result["output"]
    assert "无正当抗辩" in r.text


def test_bad_reference_keeps_other_citations_and_evidence(workspace, monkeypatch):
    monkeypatch.setattr(AgentTools, "legal_search", lambda *a: {"items": [citation()]})
    scenario(
        monkeypatch,
        [
            tools(("search_legal_evidence", {"jurisdiction": "CN", "query": "付款"})),
            "已读条文[S1]；尚待核对的出处[S99]。",
        ],
    )
    assert send(workspace).status_code == 200
    result = last_run(workspace)
    assert result["_task_state"]["delivery"] == "partially_answered"
    assert result["_evidence"] and result["_task_state"]["model_calls"] == 2
    assert result["_citation_associations"] == {"S1": "source"}


def test_search_links_survive_a_later_model_failure(workspace, monkeypatch):
    monkeypatch.setattr(
        AgentTools,
        "web",
        lambda *a: {
            "sources": [{"url": "https://example.com/expo", "title": "展会页面", "excerpt": "报名入口"}]
        },
    )
    scenario(
        monkeypatch,
        [tools(("web_search", {"query": "茶博会网址"})), ToolFailure("timeout", "timeout")],
    )
    response = send(workspace)
    assert response.status_code == 200
    result = last_run(workspace)
    assert result["_task_state"]["delivery"] == "partially_answered"
    assert result["_task_state"]["model_calls"] == 2
    assert "https://example.com/expo" in result["output"]
    assert "尚未完成进一步整理" in result["output"]
    assert "未交付法律结论" not in result["output"]
    assert not result["_drafts"] and not result["_card_updates"] and not result["_memories"]


@pytest.mark.parametrize("repaired", [True, False])
def test_xml_call_text_is_repaired_once_and_never_delivered_as_answer(workspace, monkeypatch, repaired):
    protocol = "<tool_call><function=legal_lookup><parameter=query>规则</parameter></function></tool_call>"
    scenario(monkeypatch, [protocol, "本次未取得法律原文，尚不能确认该手续。" if repaired else protocol])
    response = send(workspace)
    result = last_run(workspace)
    assert response.status_code == (200 if repaired else 502)
    assert "<tool_call>" not in result["output"]
    assert result["_task_state"]["model_calls"] == 2
    assert result["_task_state"]["output_errors"][0]["unexecuted_tool_protocol"]
    assert not result["_task_state"]["calls"]
    assert result["_task_state"]["delivery"] == ("answered" if repaired else "execution_failed")


def test_useful_discovery_is_not_lost_when_last_batch_is_a_duplicate(workspace, monkeypatch):
    seen = []
    scenario(
        monkeypatch,
        [
            tools(
                ("tool_search", {"query": "inspect_image"}),
                ("tool_search", {"query": "不存在的能力 xyzzy"}),
                ("tool_search", {"query": "不存在的能力 xyzzy"}),
            ),
            "已知事实可继续讨论，暂未读取图片。",
        ],
        seen,
        auto_discover=False,
    )
    assert send(workspace).status_code == 200
    assert "inspect_image" in {tool["function"]["name"] for tool in seen[1][1]}


def test_cancelled_answer_preserves_evidence_usage_and_input(workspace, monkeypatch):
    from app.shared.application.streaming import StreamCancelled

    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    monkeypatch.setattr(AgentTools, "legal_search", lambda *a: {"items": [citation()]})
    calls = []

    async def turn(*args, **kwargs):
        calls.append(1)
        kwargs["on_usage"]({"total_tokens": 42})
        if len(calls) == 1:
            return tools(("search_legal_evidence", {"jurisdiction": "CN", "query": "付款"}))
        raise StreamCancelled()

    monkeypatch.setattr(agent_loop, "agent_turn", turn)
    with pytest.raises(StreamCancelled):
        workspace[1].post_message(workspace[2], workspace[3]["id"], workspace[-1])
    result = last_run(workspace)
    assert result["_task_state"]["delivery"] == "execution_failed"
    assert result["_evidence"][0]["excerpt"] == citation()["excerpt"]
    assert result["usage"] == [{"total_tokens": 42}, {"total_tokens": 42}]
    assert result["_input"]["question"] == workspace[-1]["content"]
    assert not result["output"]


def test_evidence_budget_does_not_cut_a_legal_condition():
    condition = "仅在满足前款条件时办理。"
    full = citation(text="正文" * 2000 + condition)
    value = agent_loop.bounded_material({"items": [full]}, "call", limit=1000)
    assert value["items"] == [] and value["omitted_count"] == 1
    assert "分页读取" in value["notice"]


@pytest.mark.parametrize("field", ["TOTAL", "MODEL"])
@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_time_budget_rejected(monkeypatch, field, value):
    monkeypatch.setenv(f"AGENT_BUDGET_{field}", value)
    with pytest.raises(ToolFailure):
        ExecutionBudget.configured()


@pytest.mark.parametrize("value", [None, "0"])
def test_default_time_budget_allows_active_long_tasks(monkeypatch, value):
    for field in ("TOTAL", "MODEL"):
        monkeypatch.delenv(f"AGENT_BUDGET_{field}", raising=False)
        if value is not None:
            monkeypatch.setenv(f"AGENT_BUDGET_{field}", value)
    budget = ExecutionBudget.configured()
    assert budget.total_seconds == budget.model_seconds == float("inf")
    assert budget.evidence == 45


@pytest.mark.parametrize("total, model", [(180, 120), (7200, 3600), (0, 120), (180, 0)])
def test_explicit_time_limits_remain_configurable(monkeypatch, total, model):
    monkeypatch.setenv("AGENT_BUDGET_TOTAL", str(total))
    monkeypatch.setenv("AGENT_BUDGET_MODEL", str(model))
    budget = ExecutionBudget.configured()
    assert budget.total_seconds == (total or float("inf"))
    assert budget.model_seconds == (model or float("inf"))


def test_empty_model_stall_has_one_bounded_retry_without_executing_tools(workspace, monkeypatch):
    stalled = ToolFailure("model_stream_stalled", "受控无正文中断")
    stalled.retryable = True
    scenario(monkeypatch, [stalled, "可以先整理产品报价和目标客户名单。"])
    response = send(workspace)
    assert response.status_code == 200
    result = last_run(workspace)
    assert result["_task_state"]["model_calls"] == 2
    assert not result["_task_state"]["calls"]
    assert result["_task_state"]["delivery"] == "answered"
    turn = result["trace"][0]["tools"][0]
    assert turn["attempt"] == 2 and turn["status"] == "completed"


def test_repeated_model_stall_is_bounded_and_never_claims_a_completed_task(workspace, monkeypatch):
    def stalled():
        error = ToolFailure("model_stream_stalled", "受控无正文中断")
        error.retryable = True
        return error

    scenario(monkeypatch, [stalled(), stalled()])
    response = send(workspace)
    assert response.status_code == 502
    result = last_run(workspace)
    assert result["_task_state"]["model_calls"] == 2
    assert result["_task_state"]["delivery"] == "execution_failed"


@pytest.mark.parametrize("value", ["nan", "inf", "0", "100", "24001", "12000.5"])
def test_generation_token_budget_is_bounded_and_integral(monkeypatch, value):
    monkeypatch.setenv("AGENT_BUDGET_TOKENS", value)
    with pytest.raises(ToolFailure):
        ExecutionBudget.configured()


def test_thinking_length_recovery_increases_tokens_and_still_reads_evidence(workspace, monkeypatch):
    monkeypatch.setattr(AgentTools, "chinese_law", lambda *a: {"items": [citation("law")]})
    scenario(
        monkeypatch,
        [
            ToolFailure("generation_truncated", "known length stop"),
            tools(("lookup_chinese_law", {"law": "中华人民共和国民法典", "articles": ["第五百七十七条"]})),
            "依已读条文分析违约责任。[S1]",
        ],
    )
    original = agent_loop.agent_turn
    limits = []

    async def observed(*args, **kwargs):
        limits.append(kwargs["max_tokens"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent_loop, "agent_turn", observed)
    assert send(workspace).status_code == 200
    result = last_run(workspace)
    assert limits[0] == 12000 and limits[1] == 24000
    assert [c["tool"] for c in result["_task_state"]["calls"]] == ["lookup_chinese_law"]
    assert result["_task_state"]["delivery"] == "answered"


def test_truncated_public_answer_is_continued_without_losing_its_conditions(workspace, monkeypatch):
    error = ToolFailure("generation_truncated", "known length stop")
    error.partial_content = "仅在买方完成验收后支付尾款；如有质量争议，"
    seen = []
    scenario(monkeypatch, [error, "先按合同约定核对检验记录。"], seen)
    assert send(workspace).status_code == 200
    result = last_run(workspace)
    assert result["output"] == error.partial_content + "先按合同约定核对检验记录。"
    assert seen[-1][1] == []
    assert any(m.get("content") == error.partial_content for m in seen[-1][0])
    assert result["_task_state"]["length_recovery"]["continued_answer"] is True


@pytest.mark.parametrize("code", ["timeout", "connection_failed", "upstream_error"])
def test_failed_generation_preserves_public_answer_as_partial_without_retry(workspace, monkeypatch, code):
    error = ToolFailure(code, "interrupted")
    error.partial_content = "合同第六条约定由生产工厂提供检测报告，贸易商负责核对并提交。"
    seen = []
    scenario(monkeypatch, [error], seen)
    response = send(workspace)
    assert response.status_code == 200
    result = last_run(workspace)
    assert error.partial_content in result["output"] and "本次答复尚未完成" in result["output"]
    assert result["_task_state"]["delivery"] == "partially_answered" and len(seen) == 1
    assert not result["_drafts"] and not result["_card_updates"] and not result["_memories"]


def test_second_length_stop_keeps_both_public_fragments_and_stops(workspace, monkeypatch):
    first = ToolFailure("generation_truncated", "length")
    first.partial_content = "请先修改付款条款。"
    second = ToolFailure("generation_truncated", "length")
    second.partial_content = "质量标准还需核对附件。"
    seen = []
    scenario(monkeypatch, [first, second], seen)
    assert send(workspace).status_code == 200
    result = last_run(workspace)
    assert first.partial_content + second.partial_content in result["output"]
    assert len(seen) == 2 and result["_task_state"]["delivery"] == "partially_answered"


def test_partial_answer_is_saved_and_delivered_through_the_chat_stream(workspace, monkeypatch):
    import json

    error = ToolFailure("timeout", "interrupted")
    error.partial_content = "第七条的验收期限与质量异议期限需要协调。"
    scenario(monkeypatch, [error])
    response = send(workspace, stream=True)
    assert response.status_code == 200
    events = [frame for frame in response.text.split("\n\n") if frame.startswith("event: done\n")]
    assert len(events) == 1
    answer = json.loads(events[0].split("data: ", 1)[1])["messages"][1]
    assert error.partial_content in answer["content"]
    assert answer["trace"][0]["delivery_status"] == "partially_answered"
    assert last_run(workspace)["output"] == answer["content"]


@pytest.mark.parametrize("status,retries", [(502, 2), (503, 2), (401, 1), (400, 1)])
def test_model_retry_is_bounded_and_preserves_error_classification(monkeypatch, status, retries):
    import httpx
    from app.contexts.conversations.application.tool_runtime import ToolRun, ToolSpec, ToolInput

    spec = ToolSpec("agent_turn", "模型请求", "测试有界恢复", ToolInput, 2, 2)
    run = ToolRun(timeout_seconds=3, catalog=[spec])
    calls = []

    def operation(*args):
        calls.append(1)
        response = httpx.Response(status, request=httpx.Request("POST", "https://provider.invalid"))
        response.raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        run.call(spec, {}, operation)
    assert len(calls) == retries


def test_retry_after_beyond_remaining_budget_does_not_retry():
    import httpx
    from app.contexts.conversations.application.tool_runtime import ToolRun, ToolSpec, ToolInput

    spec = ToolSpec("agent_turn", "模型请求", "测试限流", ToolInput, 2, 0.1)
    run = ToolRun(timeout_seconds=1, catalog=[spec])
    calls = []

    def operation(*args):
        calls.append(1)
        httpx.Response(
            429, headers={"Retry-After": "20"}, request=httpx.Request("POST", "https://provider.invalid")
        ).raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        run.call(spec, {}, operation)
    assert len(calls) == 1
