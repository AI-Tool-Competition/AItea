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


@pytest.mark.parametrize("field", ["limit", "max_results"])
def test_web_result_count_accepts_bounded_historical_count(field):
    assert WebInput.model_validate({"query": "茶叶", field: "5"}).max_results == 5


@pytest.mark.parametrize("count", ["0", "11", "-1", "1.5", True, "nan"])
def test_web_result_count_keeps_strict_boundaries(count):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WebInput.model_validate({"query": "茶叶", "limit": count})


def test_search_snippet_identity_survives_model_projection_and_citation():
    sources = AnswerSources()
    original = {"sources": [{"url": "https://www.gov.cn/example", "title": "公告", "excerpt": "摘要"}]}
    sources.collect(original)
    projected = sources.project(original)["sources"][0]
    assert projected["id"] == sources.sources["S1"]["id"]
    assert projected["reference"] == "S1"
    assert projected["metadata"]["content_role"] == "search_snippet"
    assert "搜索摘要" in sources.render("来源[S1]")
    assert "id" not in original["sources"][0]


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


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
def test_invalid_time_budget_rejected(monkeypatch, value):
    monkeypatch.setenv("AGENT_BUDGET_TOTAL", value)
    with pytest.raises(ToolFailure):
        ExecutionBudget.configured()


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
