"""The ordinary analysis route does not turn draft governance into a refusal."""

import copy
import json
from types import SimpleNamespace

import pytest

from app.contexts.legal_analysis.application import simple_analysis as simple
from app.shared.application.streaming import StreamCancelled
from app.shared.domain.errors import AppError


def source(identifier="source-a", country="CN", **metadata):
    return {
        "id": identifier,
        "document_id": "document-" + identifier,
        "document_version": 1,
        "title": "供测试的付款责任条文",
        "excerpt": "买受人应当按照约定的数额和时间支付价款；不履行合同义务的，应当承担违约责任。",
        "locator": {"article": "4", "start": 10, "end": 65},
        "country": country,
        "source_url": "https://example.gov/law",
        "metadata": {"review_status": "draft", "kind": "law", **metadata},
    }


class Search:
    def __init__(self, items):
        self.items, self.calls = items, []

    def search(self, owner, question, case_id, template, countries, limit, **options):
        self.calls.append({"query": question, "countries": countries, **options})
        return {"items": copy.deepcopy(self.items), "mode": "local_pack"}


def fixture(items=None):
    template = {
        "origin": "CN",
        "destination": "ID",
        "agent": {"language": "中文", "instructions": "分析本次问题", "legal": {"jurisdictions": ["SG"]}},
    }
    return SimpleNamespace(
        legal=Search(items if items is not None else [source()]), capabilities={"assistant_mode": "live"}
    ), template


def proposal(**changes):
    return {
        "issues": [
            {
                "issue_id": "issue-1",
                "question": "能否请求支付价款及承担违约责任？",
                "jurisdictions": ["CN"],
                "fact_keys": [],
                "status": "supported",
                "conclusion": "按题设可请求买方支付到期货款，并主张相应违约责任。",
                "explanation": "合同有效、交货及到期未付款均为本题明确假设，直接对应付款义务与不履行义务的责任规则；损失金额未知不影响付款请求的基础分析。",
                "citation_ids": ["source-a"],
                "actions": [
                    {
                        "id": "a1",
                        "title": "提出付款请求",
                        "nature": "recommended",
                        "details": "列明欠款及到期时间。",
                    }
                ],
                **changes,
            }
        ]
    }


def run(service, template, **options):
    return simple.run_simple_analysis(
        service,
        "owner",
        template,
        {"id": "stage"},
        "假设合同有效、货款到期、买方收到货物未付款且没有正当抗辩，能要求付款及承担违约责任吗？",
        [],
        case_id="case",
        jurisdictions=options.pop("jurisdictions", ["CN"]),
        **options,
    )


def test_one_call_explains_draft_source_and_user_assumptions_without_qa_approval(monkeypatch):
    service, template = fixture()
    calls = []

    def generate(agent, step, question, context, *args, **options):
        calls.append((step, question, context, options))
        return json.dumps(proposal(), ensure_ascii=False)

    monkeypatch.setattr(simple, "generate_guidance", generate)
    result = run(service, template)
    assert len(calls) == len(service.legal.calls) == 1
    assert service.legal.calls[0]["countries"] == ["CN"]
    assert calls[0][3]["structured"] is True
    assert "没有正当抗辩" in calls[0][1]
    assert result["issues"][0]["status"] == "supported"
    assert "可请求买方支付到期货款" in result["output"]
    assert "适用版本尚待确认" not in result["output"]
    assert "QA-" not in result["output"]
    assert [item["id"] for item in result["trace"]] == ["scope", "retrieve", "analyze"]
    audit = result["trace"][1]["output_data"]["evidence"][0]
    assert audit["id"] == "source-a" and len(audit["text_hash"]) == 64
    assert result["trace"][2]["input_data"]["evidence_ids"] == ["source-a"]
    assert result["citations"][0]["excerpt"] == source()["excerpt"]
    assert result["citations"][0]["metadata"]["review_status"] == "draft"


def test_one_retrieval_failure_preserves_other_queries_and_records_the_gap(monkeypatch):
    service, template = fixture()

    def search(owner, query, *args, **options):
        if query == "unavailable":
            raise TimeoutError("private provider detail")
        return {"items": [source()], "mode": "local_pack"}

    service.legal.search = search
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(proposal()))
    result = run(service, template, search_queries=["available", "unavailable"])
    assert result["issues"][0]["conclusion"] == proposal()["issues"][0]["conclusion"]
    assert result["citations"][0]["id"] == "source-a"
    retrieval = next(t for t in result["trace"] if t["id"] == "retrieve")
    acquisition = retrieval["output_data"]["acquisition"]
    assert acquisition[1] == {
        "query": "unavailable",
        "sources": [{"status": "failed", "error_code": "timeout"}],
    }
    assert "private provider detail" not in str(result)


def test_retrieval_permission_failure_is_not_treated_as_a_missing_source(monkeypatch):
    service, template = fixture()

    def search(*args, **options):
        raise AppError(403, "forbidden", "拒绝读取")

    service.legal.search = search
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: pytest.fail("scope denied"))
    with pytest.raises(AppError) as error:
        run(service, template)
    assert error.value.status == 403


def test_provider_failure_keeps_acquired_originals_in_checkpoint(monkeypatch):
    service, template = fixture()
    checkpoints = []
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):
        run(service, template, on_checkpoint=lambda step, result, state: checkpoints.append((step, result)))
    assert checkpoints[-1][0] == "retrieve"
    assert checkpoints[-1][1]["citations"][0]["excerpt"] == source()["excerpt"]
    assert checkpoints[-1][1]["issues"] == []


def test_report_uses_subject_names_in_user_text_without_rewriting_words():
    result = simple._report(
        [
            {
                "question": "样品安排",
                "conclusion": "sg_importer负责样品进口。",
                "explanation": "与sg_importer确认；保留sg_importer_extra原词。",
                "citations": [],
                "actions": [
                    {
                        "title": "确认安排",
                        "details": "通知sg_importer。",
                        "responsible_actor_id": "sg_importer",
                    }
                ],
            }
        ],
        {"actors": [{"id": "sg_importer", "name": "新加坡经销商"}]},
    )
    assert "与新加坡经销商确认" in result
    assert "通知新加坡经销商" in result
    assert "sg_importer_extra" in result


def test_planned_queries_keep_user_question_and_deduplicate_evidence(monkeypatch):
    service, template = fixture()
    calls = []

    def generate(agent, step, question, context, *args, **options):
        calls.append((question, context))
        return json.dumps(proposal(), ensure_ascii=False)

    monkeypatch.setattr(simple, "generate_guidance", generate)
    queries = ["《民法典》第五百七十九条", "《民法典》第五百七十七条"]
    result = run(service, template, search_queries=[*queries, queries[0]])
    assert [item["query"] for item in service.legal.calls] == queries
    assert len(calls) == 1 and "没有正当抗辩" in calls[0][0]
    assert len(calls[0][1]["evidence"]) == len(result["citations"]) == 1
    assert result["trace"][1]["input_data"]["search_queries"] == queries


def test_unresolved_text_location_is_not_presented_as_a_verified_original(monkeypatch):
    service, template = fixture([source(source_location_verified=False)])
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: pytest.fail("no locatable original"))
    result = run(service, template)
    assert not result["citations"]
    assert result["trace"][1]["output_data"]["excluded"][0]["reason"] == "source_location_unverified"


def test_chat_legal_deliverable_recalls_original_question_in_unused_query_slot(monkeypatch):
    service, template = fixture()
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(proposal()))
    result = run(
        service,
        template,
        search_queries=["价款"],
        context={"conversation": {"legal_deliverables": [{"deliverable": "付款与违约责任"}]}},
    )
    queries = result["trace"][1]["input_data"]["search_queries"]
    assert queries[0] == "价款" and queries[1] == result["question"]
    assert len(service.legal.calls) == 2


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("replacement", [{}, {"superseded_by": {"document_id": "current"}}])
def test_overdue_days_does_not_request_a_replaced_legal_version(monkeypatch, historical, replacement):
    previous = source("old", superseded_at="2021-01-01", **replacement)
    service, template = fixture([previous, source()])
    inputs = []

    def generate(agent, step, question, context, *args, **kwargs):
        inputs.append(context)
        return json.dumps(proposal())

    monkeypatch.setattr(simple, "generate_guidance", generate)
    result = simple.run_simple_analysis(
        service,
        "owner",
        template,
        {"id": "stage"},
        "请对比修改前后的历史版本" if historical else "交付茶叶后付款期限已经过去20天，对方仍未付货款。",
        [],
        case_id="case",
        jurisdictions=["CN"],
    )
    texts = [entry["document_id"] for entry in inputs[0]["evidence"]]
    assert (previous["document_id"] in texts) is historical
    if not historical:
        assert any(
            item["id"] == "old" and item["reason"] == "historical_source_outside_current_period"
            for item in result["trace"][1]["output_data"]["excluded"]
        )


def test_repeated_document_metadata_keeps_one_complete_copy(monkeypatch):
    first = source(version_relations=[{"evidence_text": "完整的修改关系原文" * 100}])
    second = {**source("source-b"), "document_id": first["document_id"], "metadata": first["metadata"]}
    service, template = fixture([first, second])
    contexts = []

    def generate(agent, step, question, context, *a, **kw):
        contexts.append(context)
        return json.dumps(proposal(citation_ids=["source-a", "source-b"]))

    monkeypatch.setattr(simple, "generate_guidance", generate)
    result = run(service, template)
    entries = contexts[0]["evidence"]
    assert entries[0]["metadata"]["version_relations"] == first["metadata"]["version_relations"]
    assert entries[1]["metadata"] == {} and entries[1]["metadata_ref"] == "document-source-a:1"
    assert result["citations"][1]["metadata"]["version_relations"] == first["metadata"]["version_relations"]


def test_query_budget_is_checked_before_searching(monkeypatch):
    service, template = fixture()
    with pytest.raises(simple.LegalEngineError):
        run(service, template, search_queries=[str(i) for i in range(5)])
    assert service.legal.calls == []


def test_evidence_budget_preserves_sources_from_each_requested_country(monkeypatch):
    items = [{**source(f"cn-{i}"), "excerpt": "付款义务及例外。" * 500} for i in range(12)]
    items.append(source("id-law", country="ID"))
    service, template = fixture(items)
    contexts = []

    def generate(agent, step, question, context, *args, **kwargs):
        contexts.append(context)
        return json.dumps(proposal(citation_ids=["cn-0"]))

    monkeypatch.setattr(simple, "generate_guidance", generate)
    result = run(service, template, jurisdictions=["CN", "ID"])
    evidence = contexts[0]["evidence"]
    assert [item["country"] for item in evidence[:2]] == ["CN", "ID"]
    assert len(evidence) < len(items)
    assert result["trace"][1]["output_data"]["excluded"]


@pytest.mark.parametrize(
    "changes", [{"citation_ids": ["invented"]}, {"fact_keys": ["invented"]}, {"jurisdictions": ["ID"]}]
)
def test_unknown_reference_or_jurisdiction_is_not_allowed_to_reach_report(monkeypatch, changes):
    service, template = fixture()
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(proposal(**changes)))
    result = run(service, template)
    assert "invented" not in json.dumps(result["issues"])
    if "fact_keys" not in changes:
        assert result["issues"][0]["status"] == "undetermined"
    assert result["issues"][0]["jurisdictions"] == ([] if "jurisdictions" in changes else ["CN"])


def test_no_source_does_not_call_model_or_import_another_jurisdiction(monkeypatch):
    service, template = fixture([source(country="ID")])
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: pytest.fail("no in-scope original"))
    result = run(service, template)
    assert result["issues"][0]["status"] == "undetermined"
    assert not result["citations"]
    assert result["trace"][1]["output_data"]["excluded"][0]["reason"] == "jurisdiction_mismatch"
    assert result["trace"][2]["output_data"]["model_calls"] == 0


def test_failed_independent_issue_does_not_delete_supported_issue(monkeypatch):
    service, template = fixture([source(review_status="published", effective_from="2020-01-01")])
    answer = proposal()
    answer["issues"].append(
        {
            **answer["issues"][0],
            "issue_id": "issue-2",
            "question": "具体损失赔多少？",
            "citation_ids": [],
            "status": "undetermined",
            "conclusion": "赔偿金额须另核实。",
        }
    )
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert [issue["status"] for issue in result["issues"]] == ["supported", "undetermined"]
    assert "可请求买方支付到期货款" in result["output"]
    assert len(result["citations"]) == 1


def test_known_expired_requirement_is_not_rendered_as_current_mandatory_action(monkeypatch):
    service, template = fixture(
        [source(review_status="published", effective_from="2020-01-01", effective_to="2021-01-01")]
    )
    answer = proposal(
        conclusion="现行法律要求必须先办理旧备案。",
        actions=[
            {"id": "bad", "title": "必须办理旧备案", "nature": "mandatory", "details": "立即办理，否则违法。"}
        ],
    )
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert result["issues"][0]["status"] == "undetermined"
    assert "现行法律要求必须先办理旧备案" not in result["output"]
    assert "否则违法" not in result["output"]
    assert not any(a["nature"] == "mandatory" for a in result["issues"][0]["actions"])


def test_cancel_stops_before_retrieval_or_model(monkeypatch):
    service, template = fixture()

    def cancel():
        raise StreamCancelled()

    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: pytest.fail("cancelled"))
    with pytest.raises(StreamCancelled):
        run(service, template, check_cancelled=cancel)
    assert not service.legal.calls


def test_strictly_selected_documents_are_forwarded_to_acquisition(monkeypatch):
    service, template = fixture()
    service.legal.acquire_evidence = service.legal.search
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(proposal()))
    run(service, template, document_ids=["selected-document"])
    assert service.legal.calls[0]["document_ids"] == ["selected-document"]


def test_explicit_historical_date_uses_that_period_instead_of_current_effectivity(monkeypatch):
    service, template = fixture(
        [
            source(
                review_status="published",
                effectivity="repealed",
                effective_from="2020-01-01",
                effective_to="2021-12-31",
            )
        ]
    )
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(proposal()))
    result = run(service, template, as_of="2021-06-01")
    assert result["issues"][0]["status"] == "supported"
    assert str(service.legal.calls[0]["as_of"]) == "2021-06-01"
    assert result["trace"][0]["output_data"]["effective_at"] == "2021-06-01"


def test_multiple_historical_dates_are_not_silently_replaced_with_today():
    at, basis = simple._target_date("请解释2022年修改和2025年修订对2026年的影响。", [])
    assert at is None and basis == "historical_or_multiple_dates"
    assert (
        simple._source_label({"title": "PP", "locator": {"article": "Pasal 20 ayat (3)"}})
        == "PP，Pasal 20 ayat (3)"
    )
    assert simple._source_label({"title": "海关法", "locator": {"article": "第十一条"}}) == "海关法，第十一条"


def test_condition_can_reference_an_actual_source_missing_from_issue_list(monkeypatch):
    service, template = fixture([source(), source("source-b")])
    answer = proposal(
        conditions=[
            {
                "label": "不履行付款义务",
                "state": "met",
                "citation_ids": ["source-b"],
                "reason": "题设逾期未付",
                "fact_sources": ["买方收到货物未付款"],
            }
        ]
    )
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert {c["id"] for c in result["issues"][0]["citations"]} == {"source-a", "source-b"}
    assert result["issues"][0]["conditions"][0]["state"] == "met"


def test_invalid_independent_issue_does_not_erase_payment_analysis(monkeypatch):
    service, template = fixture()
    answer = proposal()
    answer["issues"].append({"question": "另一个争点", "status": "invalid"})
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert result["issues"][0]["status"] == "supported"
    assert result["issues"][1]["status"] == "undetermined"
    assert "可请求买方支付到期货款" in result["output"]


def test_invalid_optional_action_preserves_conclusion_and_valid_sibling(monkeypatch):
    service, template = fixture()
    answer = proposal()
    answer["issues"][0]["actions"].append(
        {"id": "坏编号", "title": "无效附注", "nature": "recommended", "details": "不应抹掉其他分析"}
    )
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert result["issues"][0]["status"] == "supported"
    assert [a["id"] for a in result["issues"][0]["actions"]] == ["a1"]
    assert "可请求买方支付到期货款" in result["output"]
    errors = result["trace"][2]["output_data"]["reference_diagnostics"]
    assert errors[0]["fields"][0]["field"] == ["actions", 1, "id"]


def test_commercial_arrangements_do_not_require_a_legal_citation(monkeypatch):
    service, template = fixture()
    answer = proposal(kind="information", citation_ids=[], conclusion="先与经销商确认数量和付款方式。")
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert result["issues"][0]["kind"] == "information"
    assert result["issues"][0]["status"] == "supported"
    assert "先与经销商确认数量和付款方式" in result["output"]


def test_evidence_budget_preserves_later_search_topics(monkeypatch):
    service, template = fixture()
    service.legal.search = lambda owner, query, *a, **kw: {
        "items": [source("later-topic")]
        if query == "later"
        else [{**source(f"early-{i}"), "excerpt": "付款义务及例外。" * 500} for i in range(12)],
        "mode": "local_pack",
    }
    contexts = []

    def generate(agent, step, question, context, *a, **kw):
        contexts.append(context)
        return json.dumps(proposal(citation_ids=["later-topic"]))

    monkeypatch.setattr(simple, "generate_guidance", generate)
    result = run(service, template, search_queries=["early", "later"])
    assert contexts[0]["evidence"][1]["id"] == "E2"
    assert contexts[0]["evidence"][1]["document_id"] == "document-later-topic"
    assert result["issues"][0]["citations"][0]["id"] == "later-topic"


def test_report_has_clickable_original_links_and_deduplicates_shared_sources(monkeypatch):
    service, template = fixture()
    answer = proposal()
    answer["issues"].append({**answer["issues"][0], "issue_id": "issue-2"})
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(answer))
    result = run(service, template)
    assert result["output"].count("](https://example.gov/law)") == 1
    assert result["output"].count("提出付款请求") == 1


def test_report_groups_articles_of_one_document_without_losing_locations(monkeypatch):
    first = source()
    second = {**source("source-b"), "document_id": first["document_id"], "locator": {"article": "8"}}
    service, template = fixture([first, second])
    monkeypatch.setattr(
        simple,
        "generate_guidance",
        lambda *a, **kw: json.dumps(proposal(citation_ids=["source-a", "source-b"])),
    )
    result = run(service, template)
    assert result["output"].count("](https://example.gov/law)") == 1
    assert "第4条、第8条" in result["output"]
    assert len(result["citations"]) == 2


def test_future_transaction_month_is_used_not_analysis_day():
    from datetime import date

    assert simple._target_date("2026年11月准备出口，解释2022年变动。", []) == (
        date(2026, 11, 1),
        "specified_month",
    )


def test_explicit_query_country_does_not_consume_another_countrys_budget():
    assert simple._query_countries("新加坡预包装食品英文标签", ["CN", "SG"]) == ["SG"]
    assert simple._query_countries("中国出口食品生产企业备案", ["CN", "SG"]) == ["CN"]
    assert simple._query_countries("中国与新加坡的合同责任", ["CN", "SG"]) == ["CN", "SG"]
    assert simple._query_countries("进口注册", ["CN", "SG"]) == ["CN", "SG"]
    assert simple._query_countries("SG Food Regulations 标签", ["CN", "SG"]) == ["SG"]
    assert simple._query_countries("CN 海关备案", ["CN", "SG"]) == ["CN"]


def test_valid_json_answer_survives_unrequested_trailing_model_text():
    answer, errors = simple._parse_answer(json.dumps(proposal()) + "\n额外说明，不进入结果。", ["CN"])
    assert answer["issues"][0]["conclusion"] == proposal()["issues"][0]["conclusion"]
    assert errors == [{"reason": "trailing_model_text_ignored"}]
    wrapped, _ = simple._parse_answer("```json\n" + json.dumps(proposal()) + "\n```", ["CN"])
    assert wrapped["issues"][0]["conclusion"] == answer["issues"][0]["conclusion"]


def test_followup_reuses_unmodified_transaction_date(monkeypatch):
    service, template = fixture()
    monkeypatch.setattr(simple, "generate_guidance", lambda *a, **kw: json.dumps(proposal()))
    result = run(
        service,
        template,
        context={
            "conversation": {
                "recent_messages": [
                    {"role": "user", "content": "计划2026年12月出口茶叶。"},
                    {"role": "assistant", "content": "2020年1月1日是旧规则日期。"},
                ]
            }
        },
    )
    assert result["trace"][0]["output_data"]["effective_at"] == "2026-12-01"
    assert result["trace"][0]["output_data"]["time_basis"] == "conversation_transaction_date"


def test_subject_graph_preserves_multiple_roles_but_does_not_verify_claimed_licence():
    from app.contexts.legal_analysis.application.business_subjects import ground_context

    question = "我是贸易商，也是卖方和出口申报主体。我已办理收发货人备案，生产由甲厂负责，报关委托乙公司。"
    graph = ground_context(
        {
            "actors": [
                {
                    "id": "user",
                    "name": "用户",
                    "roles": ["贸易商", "卖方", "出口申报主体"],
                    "sources": ["我是贸易商，也是卖方和出口申报主体"],
                },
                {"id": "factory", "name": "甲厂", "roles": ["生产商"], "sources": ["生产由甲厂负责"]},
                {"id": "broker", "name": "乙公司", "roles": ["受托报关企业"], "sources": ["报关委托乙公司"]},
            ],
            "credentials": [
                {
                    "id": "customs",
                    "actor_id": "user",
                    "credential_type": "收发货人备案",
                    "status": "documented",
                    "proof": "虚构的回执123",
                    "sources": ["我已办理收发货人备案"],
                }
            ],
        },
        question,
        [],
    )
    assert len(graph["actors"]) == 3 and len(graph["actors"][0]["roles"]) == 3
    assert graph["credentials"][0]["actor_id"] == "user"
    assert graph["credentials"][0]["status"] == "reported_completed"
    assert graph["credentials"][0]["proof"] == ""


def test_delegation_keeps_four_identities_and_completed_credential_is_not_reused_by_factory():
    graph = {
        "actors": [{"id": id, "name": id} for id in ("trader", "factory", "broker", "importer", "retailer")],
        "credentials": [{"id": "customs", "actor_id": "trader"}],
    }
    answer = simple._Answer.model_validate(
        proposal(
            obligations=[
                {
                    "id": "production",
                    "matter": "工厂备案",
                    "legal_actor_id": "factory",
                    "performer_id": "broker",
                    "registrant_id": "factory",
                    "business_responsible_id": "trader",
                    "trigger": "实际从事生产活动",
                    "citation_ids": ["source-a"],
                    "credential_ids": ["customs"],
                    "status": "completed",
                }
            ]
        )
    ).model_dump(mode="json")
    result = simple._validate_answer(answer, {"source-a": source()}, [], ["CN"], None, graph)
    obligation = result[0]["obligations"][0]
    assert [
        obligation[k] for k in ("legal_actor_id", "performer_id", "registrant_id", "business_responsible_id")
    ] == ["factory", "broker", "factory", "trader"]
    assert obligation["credential_ids"] == [] and obligation["status"] == "unconfirmed"


def test_completed_obligation_is_retained_without_a_duplicate_filing_task():
    answer = simple._Answer.model_validate(
        proposal(
            obligations=[
                {
                    "id": "filing",
                    "matter": "收发货人备案",
                    "legal_actor_id": "user",
                    "registrant_id": "user",
                    "trigger": "作为出口收发货人",
                    "citation_ids": ["source-a"],
                    "credential_ids": ["customs"],
                    "status": "completed",
                }
            ],
            actions=[
                {
                    "id": "repeat",
                    "title": "再次办理备案",
                    "nature": "mandatory",
                    "obligation_ids": ["filing"],
                    "responsible_actor_id": "user",
                }
            ],
        )
    ).model_dump(mode="json")
    graph = {
        "actors": [{"id": "user", "name": "贸易商"}],
        "credentials": [{"id": "customs", "actor_id": "user"}],
    }
    issues = simple._validate_answer(answer, {"source-a": source()}, [], ["CN"], None, graph)
    assert issues[0]["obligations"][0]["status"] == "completed"
    assert "再次办理备案" not in simple._report(issues, graph)


def test_factory_quote_does_not_create_a_plantation_role():
    from app.contexts.legal_analysis.application.business_subjects import ground_context

    graph = ground_context(
        {
            "actors": [
                {
                    "id": "factory",
                    "name": "甲茶厂",
                    "roles": ["生产工厂", "种植场"],
                    "sources": ["向独立的甲茶厂采购"],
                }
            ]
        },
        "我向独立的甲茶厂采购。",
        [],
    )
    assert graph["actors"][0]["roles"] == ["生产工厂"]


def test_unquoted_fact_cannot_satisfy_packaging_condition_or_disprove_certificate():
    answer = simple._Answer.model_validate(
        proposal(
            conditions=[
                {
                    "label": "零售预包装",
                    "state": "met",
                    "reason": "进口商卖给零售商，因此属于预包装",
                    "citation_ids": ["source-a"],
                }
            ],
            conclusion="法律不存在这个证件。",
        )
    ).model_dump(mode="json")
    result = simple._validate_answer(
        answer, {"source-a": source()}, [], ["CN"], None, {}, ["进口商卖给当地零售商。"]
    )
    assert result[0]["conditions"][0]["state"] == "unknown"
    assert result[0]["status"] == "conditional"
    assert "不存在" not in result[0]["conclusion"]


def test_contract_allocation_requires_sourced_agreement_and_rule_quote():
    answer = simple._Answer.model_validate(
        proposal(
            obligations=[
                {
                    "id": "filing",
                    "matter": "生产备案",
                    "legal_actor_id": "factory",
                    "performer_id": "trader",
                    "registrant_id": "factory",
                    "business_responsible_id": "factory",
                    "trigger": "从事生产",
                    "citation_ids": ["source-a"],
                    "fact_sources": ["甲厂生产"],
                    "reason": "备案在工厂名下，所以贸易商严禁代办。",
                    "status": "pending",
                }
            ]
        )
    ).model_dump(mode="json")
    graph = {
        "actors": [
            {"id": "factory", "identity_status": "identified"},
            {"id": "trader", "identity_status": "identified"},
        ]
    }
    result = simple._validate_answer(answer, {"source-a": source()}, [], ["CN"], None, graph, ["甲厂生产"])
    obligation = result[0]["obligations"][0]
    assert obligation["business_responsible_id"] is None
    assert obligation["performer_id"] == "trader"
    assert obligation["validity_status"] == "unconfirmed"
    assert obligation["fact_status"] == "sourced"
    assert obligation["status"] == "unconfirmed"
