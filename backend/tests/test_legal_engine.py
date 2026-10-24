"""Legal runtime tests exercise grounded decisions, not model-written authority."""

import copy
import json
from types import SimpleNamespace

import pytest


def runtime():
    from app.contexts.legal.application import legal_engine as legal_engine

    return legal_engine


def template(policy=None):
    return {
        "origin": "CN",
        "destination": "SG",
        "agent": {
            "language": "中文",
            "instructions": "仅提供办理辅助",
            "retrieval_limit": 5,
            "steps": [],
            "legal": policy or {"enabled": True, "rules": []},
        },
    }


def citation(identifier="law-a", *, country="SG", kind="law", review="published"):
    return {
        "id": identifier,
        "document_id": identifier,
        "title": "适用规则原文",
        "excerpt": "本条适用所列行为；提交期限为事件发生后的十个日历日。",
        "locator": {"page": 1},
        "document_version": 1,
        "metadata_revision": 1,
        "country": country,
        "source_url": "https://example.org/rule",
        "metadata": {
            "kind": kind,
            "jurisdiction": country,
            "review_status": review,
            "effective_from": "2020-01-01",
            "effective_to": None,
        },
    }


def fact(key="event_date", value="2026-10-12", **changes):
    return {
        "key": key,
        "label": "事件日期",
        "value": value,
        "source": "用户",
        "confirmed": True,
        "extraction_confidence": "unknown",
        "evidence_status": "verified",
        "evidence_refs": ["business-a"],
        "conflicts": [],
        "revision": 1,
        "review_note": "已核对原始单据",
        **changes,
    }


class Search:
    def __init__(self, items=None, warning=None):
        self.items = items if items is not None else [citation()]
        self.warning = warning
        self.calls = []

    def search(self, owner, query, case_id, template, jurisdictions, limit, **options):
        self.calls.append(
            {
                "owner": owner,
                "query": query,
                "case_id": case_id,
                "jurisdictions": jurisdictions,
                "limit": limit,
                **options,
            }
        )
        result = {"items": copy.deepcopy(self.items), "mode": "lexical"}
        if self.warning:
            result["warning"] = self.warning
        return result


def service(items=None, mode="demo", warning=None):
    return SimpleNamespace(legal=Search(items, warning), capabilities={"assistant_mode": mode})


def run(svc, facts=None, **options):
    return runtime().run_legal_analysis(
        svc,
        "owner-a",
        template(options.pop("policy", None)),
        {"id": "prepare", "name": "准备"},
        options.pop("question", "是否需要提交申报？"),
        facts or [],
        case_id="case-a",
        **options,
    )


def install_model(monkeypatch, overrides=None, issue_ids=("issue-a",)):
    """A deterministic provider stub keeps transport out of real runtime decisions."""
    calls = []
    overrides = overrides or {}

    def generate(agent, step, content, context, citations, checks, previous_output, **kwargs):
        module = step["id"]
        calls.append((module, copy.deepcopy(context)))
        ids = context.get("active_issue_ids", list(issue_ids))
        payloads = {
            "qa01": {
                "issues": [
                    {
                        "id": iid,
                        "question": "是否需要提交申报？",
                        "kind": "legal",
                        "domains": ["trade"],
                        "fact_keys": ["event_date"],
                        "depends_on": [],
                    }
                    for iid in ids
                ]
            },
            "qa02": {
                "facts": [],
                "requirements": [
                    {
                        "issue_id": iid,
                        "facts": [
                            {
                                "key": "event_date",
                                "label": "事件日期",
                                "purpose": "确定规则的适用日期",
                                "decisive": True,
                            }
                        ],
                    }
                    for iid in ids
                ],
            },
            "qa03": {
                "issues": [
                    {
                        "issue_id": iid,
                        "regulatory_jurisdictions": ["SG"],
                        "forum_jurisdictions": [],
                        "governing_law_jurisdictions": [],
                        "candidates": [
                            {"id": "candidate-a", "query": "申报", "jurisdictions": ["SG"], "domain": "trade"}
                        ],
                        "gaps": [],
                    }
                    for iid in ids
                ]
            },
            "qa05": {
                "issues": [
                    {
                        "issue_id": iid,
                        "status": "supported",
                        "conclusion": "满足本条申报条件。",
                        "explanation": "已逐项对照事实与规则。",
                        "citation_ids": ["law-a"],
                        "conditions": [
                            {
                                "label": "规则适用条件",
                                "state": "met",
                                "fact_keys": ["event_date"],
                                "citation_ids": ["law-a"],
                                "reason": "已核对适用日期。",
                            }
                        ],
                        "deadlines": [],
                        "actions": [],
                        "gaps": [],
                        "recheck": False,
                    }
                    for iid in ids
                ]
            },
        }
        override = overrides.get(module)
        value = override(context, payloads[module]) if callable(override) else override or payloads[module]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    monkeypatch.setattr(runtime(), "generate_guidance", generate)
    return calls


def test_demo_runs_six_public_stages_without_legal_conclusions():
    result = run(service(), document_ids=["law-a"])
    assert result["assistant_mode"] == "demo"
    assert [item["id"] for item in result["trace"]] == [f"qa0{i}" for i in range(1, 7)]
    assert all(issue["status"] == "undetermined" for issue in result["issues"])
    assert "未调用" in result["output"]
    assert all(not item["confirmed"] for item in result["facts"])
    assert result["trace"][0]["input_data"]["document_ids"] == ["law-a"]


def test_policy_defaults_are_editable_cards_and_program_checks_are_separate():
    from app.contexts.legal.domain.legal_models import (
        LEGAL_MODULES,
        PROGRAM_CHECKS,
        LegalPolicy,
        default_legal_policy,
    )

    policy = default_legal_policy()
    assert LegalPolicy.model_validate(policy).enabled is False
    assert [item["id"] for item in LEGAL_MODULES] == [f"qa0{i}" for i in range(1, 7)]
    assert {rule["module"] for rule in policy["rules"]} == {item["id"] for item in LEGAL_MODULES}
    assert all({"id", "title", "description"} <= gate.keys() for gate in PROGRAM_CHECKS)
    policy["rules"][0]["instructions"] = "用户自定义"
    assert default_legal_policy()["rules"][0]["instructions"] != "用户自定义"


def test_scoped_initial_retrieval_precedes_dynamic_fact_requirements(monkeypatch):
    svc = service(mode="live")
    calls = install_model(monkeypatch)
    result = run(svc, document_ids=["law-a"])
    assert svc.legal.calls[0]["jurisdictions"] == ["CN", "SG"]
    assert all(call["owner"] == "owner-a" and call["case_id"] == "case-a" for call in svc.legal.calls)
    assert all(call["document_ids"] == ["law-a"] for call in svc.legal.calls)
    qa02 = next(context for module, context in calls if module == "qa02")
    assert qa02["initial_citations"][0]["id"] == "law-a"
    assert [item["key"] for item in result["followups"]] == ["event_date"]
    assert result["issues"][0]["status"] == "conditional"


def test_invalid_structured_output_retries_then_fails_visibly(monkeypatch):
    calls = install_model(monkeypatch, {"qa01": "not JSON"})
    with pytest.raises(runtime().LegalEngineError, match="结构"):
        run(service(mode="live"))
    assert [module for module, _ in calls] == ["qa01", "qa01"]


def test_no_law_is_never_treated_as_proof_of_no_obligation(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa05": lambda context, output: {
                "issues": [
                    {**output["issues"][0], "status": "unsupported", "citation_ids": [], "conditions": []}
                ]
            }
        },
    )
    result = run(service(items=[], mode="live"), [fact()])
    assert result["issues"][0]["status"] == "undetermined"
    assert "资料" in "".join(result["issues"][0]["gaps"])


def test_fabricated_citation_cannot_bypass_retrieved_evidence(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa05": lambda context, output: {
                "issues": [{**output["issues"][0], "citation_ids": ["invented-law"]}]
            }
        },
    )
    with pytest.raises(runtime().LegalEngineError, match="结构"):
        run(service(mode="live"), [fact()])


def test_model_fact_conflicts_preserve_user_fact_and_both_sources(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa02": lambda context, output: {
                **output,
                "facts": [
                    {
                        "key": "event_date",
                        "label": "事件日期",
                        "value": "2026-10-10",
                        "source": "材料摘录",
                        "extraction_confidence": "high",
                        "evidence_refs": ["law-a"],
                    }
                ],
            }
        },
    )
    result = run(service(mode="live"), [fact()])
    stored = result["facts"][0]
    assert stored["value"] == "2026-10-12"
    assert stored["evidence_status"] == "conflict"
    assert stored["conflicts"][0]["value"] == "2026-10-10"
    assert stored["confirmed"] is False
    assert result["issues"][0]["status"] in ("conditional", "undetermined")


def test_confirmed_fact_without_evidence_is_not_verified_by_model(monkeypatch):
    install_model(monkeypatch)
    result = run(service(mode="live"), [fact(evidence_status="unverified", evidence_refs=[])])
    assert result["facts"][0]["evidence_status"] == "unverified"
    assert result["issues"][0]["status"] == "conditional"


def test_disallowed_jurisdiction_is_rejected_before_search(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa03": lambda context, output: {
                "issues": [{**output["issues"][0], "governing_law_jurisdictions": ["US"]}]
            }
        },
    )
    svc = service(mode="live")
    with pytest.raises(runtime().LegalEngineError, match="结构"):
        run(svc, [fact()])
    assert all("US" not in call["jurisdictions"] for call in svc.legal.calls)


def test_rechecks_are_bounded_and_candidate_coverage_is_retained(monkeypatch):
    calls = install_model(
        monkeypatch,
        {
            "qa05": lambda context, output: {
                "issues": [{**output["issues"][0], "recheck": True, "gaps": ["需复核例外条款"]}]
            }
        },
    )
    result = run(
        service(mode="live", warning="检索范围已达预算"),
        [fact()],
        policy={"enabled": True, "max_rounds": 2, "rules": []},
    )
    assert len([module for module, _ in calls if module == "qa05"]) == 2
    coverage = next(item for item in result["trace"] if item["id"] == "qa04")["output_data"]["coverage"]
    assert coverage[0]["candidate_id"] == "candidate-a"
    assert coverage[0]["status"] == "partial"
    assert result["issues"][0]["needs_review"] is True
    assert result["issues"][0]["status"] != "supported"


def test_cancelled_run_retains_completed_public_checkpoint(monkeypatch):
    from app.shared.application.streaming import StreamCancelled

    checkpoints = []
    calls = install_model(monkeypatch)

    def cancelled():
        if calls:
            raise StreamCancelled()

    with pytest.raises(StreamCancelled):
        run(
            service(mode="live"),
            check_cancelled=cancelled,
            on_checkpoint=lambda step, result, state: checkpoints.append(
                (step, copy.deepcopy(result), state)
            ),
        )
    assert checkpoints
    assert any(step["status"] == "cancelled" for step in checkpoints[-1][1]["trace"])


def deadline_spec(**changes):
    return {
        "id": "filing",
        "label": "提交期限",
        "start_fact_key": "event_date",
        "duration": 10,
        "unit": "days",
        "timezone": "Asia/Singapore",
        "jurisdiction": "SG",
        "citation_ids": ["law-a"],
        "include_start": False,
        "day_type": "calendar",
        "roll_forward": False,
        "adjustments_resolved": True,
        **changes,
    }


def test_deadline_uses_explicit_rules_and_returns_conditional_assumption():
    engine = runtime()
    actual = engine.calculate_deadline(deadline_spec(), facts=[fact()], citations=[citation()])
    assert actual["date"] == "2026-10-22" and actual["status"] == "calculated"
    conditional = engine.calculate_deadline(
        deadline_spec(), facts=[fact(evidence_status="unverified")], citations=[citation()]
    )
    assert conditional["date"] == "2026-10-22" and conditional["status"] == "conditional"
    assert conditional["assumptions"]


@pytest.mark.parametrize(
    "spec,facts,citations",
    [
        (deadline_spec(timezone="Invalid/Timezone"), [fact()], [citation()]),
        (deadline_spec(include_start=None), [fact()], [citation()]),
        (deadline_spec(day_type="business"), [fact()], [citation()]),
        (deadline_spec(), [fact(value="impossible-date")], [citation()]),
        (deadline_spec(), [fact(evidence_status="conflict")], [citation()]),
        (deadline_spec(), [fact()], []),
        (deadline_spec(), [fact()], [citation(review="draft")]),
    ],
)
def test_deadline_missing_prerequisites_never_produces_definitive_date(spec, facts, citations):
    result = runtime().calculate_deadline(spec, facts=facts, citations=citations)
    assert result["status"] == "unavailable" and result["date"] is None
    assert result["missing_prerequisites"]


def test_partial_refresh_only_regenerates_affected_issue_and_preserves_stable_sections(monkeypatch):
    calls = install_model(monkeypatch, issue_ids=("issue-a", "issue-b"))
    svc = service(mode="live")
    first = run(svc, [fact()])
    calls.clear()
    refreshed = run(svc, [fact()], previous=first, affected_issue_ids=["issue-a"])
    assert all(context["active_issue_ids"] == ["issue-a"] for _, context in calls)
    assert refreshed["issues"][1] == first["issues"][1]
    assert refreshed["sections"][1] == first["sections"][1]
    assert refreshed["trace"][0]["output_data"]["reused_issue_ids"] == ["issue-b"]
    assert len(svc.legal.calls) > 2  # Reuse still checks current source eligibility.


def test_new_evidence_invalidates_previously_empty_coverage(monkeypatch):
    def no_sources(context, output):
        if not context["citations"]:
            return {
                "issues": [
                    {**item, "status": "undetermined", "citation_ids": [], "conditions": []}
                    for item in output["issues"]
                ]
            }
        return output

    calls = install_model(monkeypatch, {"qa05": no_sources})
    svc = service(items=[], mode="live")
    first = run(svc, [fact()])
    svc.legal.items = [citation()]
    calls.clear()
    refreshed = run(svc, [fact()], previous=first, affected_issue_ids=[])
    assert any(module == "qa05" for module, _ in calls)
    assert refreshed["issues"][0]["status"] == "supported"


def test_source_metadata_change_rejects_reuse(monkeypatch):
    calls = install_model(monkeypatch)
    svc = service(mode="live")
    first = run(svc, [fact()])
    svc.legal.items[0]["metadata"]["review_status"] = "draft"
    svc.legal.items[0]["metadata_revision"] = 2
    calls.clear()
    refreshed = run(svc, [fact()], previous=first, affected_issue_ids=[])
    assert any(module == "qa05" for module, _ in calls)
    assert refreshed["issues"][0]["status"] == "undetermined"


def test_pause_resume_checks_inputs_and_evidence_before_reusing_stages(monkeypatch):
    calls = install_model(monkeypatch)
    svc = service(mode="live")
    saved = []
    first = run(
        svc,
        [fact()],
        stop_after_step_id="qa03",
        on_checkpoint=lambda step, result, state: saved.append(state),
    )
    assert len(first["trace"]) == 3 and first["sections"] == []
    calls.clear()
    resumed = run(svc, [fact()], resume={"state": saved[-1]})
    assert [item["status"] for item in resumed["trace"]][:3] == ["cached"] * 3
    assert [module for module, _ in calls] == ["qa05"]
    with pytest.raises(runtime().LegalEngineError, match="已改变"):
        run(svc, [fact(value="2026-10-11")], resume={"state": saved[-1]})
    svc.legal.items[0]["document_version"] = 2
    with pytest.raises(runtime().LegalEngineError, match="已改变"):
        run(svc, [fact()], resume={"state": saved[-1]})


def test_rule_composition_excludes_other_domains_and_jurisdictions(monkeypatch):
    from app.contexts.legal.domain.legal_models import LegalRule

    selected = LegalRule(
        id="trade-sg",
        title="新加坡贸易规则",
        module="qa05",
        domains=["trade"],
        jurisdictions=["SG"],
        instructions="使用本国规则",
    ).model_dump()
    others = [
        LegalRule(
            id="other-domain",
            title="其他领域",
            module="qa05",
            domains=["criminal"],
            instructions="不应在贸易争点中应用",
        ).model_dump(),
        LegalRule(
            id="other-country",
            title="其他国家",
            module="qa05",
            jurisdictions=["US"],
            instructions="不应应用未允许法域",
        ).model_dump(),
    ]
    calls = install_model(monkeypatch)
    run(service(mode="live"), [fact()], policy={"enabled": True, "rules": [selected, *others]})
    context = next(context for module, context in calls if module == "qa05")
    assert [rule["id"] for rule in context["rule_cards"]] == ["trade-sg"]
    assert [rule["id"] for rule in context["rules_by_issue"]["issue-a"]] == ["trade-sg"]


def test_all_not_applicable_conditions_cannot_support_a_definitive_legal_conclusion(monkeypatch):
    def no_conditions(context, output):
        output["issues"][0]["conditions"][0]["state"] = "not_applicable"
        return output

    install_model(monkeypatch, {"qa05": no_conditions})
    result = run(service(mode="live"), [fact()])
    assert result["issues"][0]["status"] != "supported"


def test_extracted_fact_cannot_claim_its_own_confirmation(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa02": lambda context, output: {
                **output,
                "facts": [
                    {
                        "key": "invented",
                        "label": "口述",
                        "value": "value",
                        "source": "模型",
                        "confirmed": True,
                    }
                ],
            }
        },
    )
    with pytest.raises(runtime().LegalEngineError, match="结构"):
        run(service(mode="live"))


def test_deadline_requires_explicit_resolution_of_extensions_and_suspensions():
    spec = deadline_spec()
    spec.pop("adjustments_resolved", None)
    result = runtime().calculate_deadline(spec, facts=[fact()], citations=[citation()])
    assert result["status"] == "unavailable"
    assert any("中止" in gap for gap in result["missing_prerequisites"])


def test_multicountry_hits_are_not_silently_cut_to_one_country_limit(monkeypatch):
    install_model(monkeypatch)
    svc = service(items=[citation(f"cn-{i}", country="CN") for i in range(5)] + [citation()], mode="demo")
    result = run(svc)
    assert {item["country"] for item in result["citations"]} == {"CN", "SG"}


def test_extraction_can_fill_a_missing_fact_without_creating_a_false_conflict(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa02": lambda context, output: {
                **output,
                "facts": [
                    {
                        "key": "event_date",
                        "label": "事件日期",
                        "value": "2026-10-10",
                        "source": "材料摘录",
                        "extraction_confidence": "high",
                        "evidence_refs": ["law-a"],
                    }
                ],
            }
        },
    )
    result = run(service(mode="live"), [fact(value=None, confirmed=False, evidence_status="unverified")])
    extracted = result["facts"][0]
    assert extracted["value"] == "2026-10-10"
    assert extracted["evidence_status"] == "unverified" and extracted["confirmed"] is False
    assert not extracted["conflicts"]


def test_reconciled_conflicts_remain_visible_without_becoming_active_conflicts():
    from app.contexts.legal.domain.legal_models import LegalFact

    stored = LegalFact.model_validate(
        fact(
            resolved_conflicts=[{"value": "2026-10-10", "source": "旧单据", "evidence_refs": ["business-a"]}]
        )
    ).model_dump()
    assert stored["resolved_conflicts"][0]["value"] == "2026-10-10"
    assert runtime()._fact_ready(stored)


def test_cancelled_assessment_resumes_from_last_completed_stage_not_partial_round(monkeypatch):
    from app.shared.application.streaming import StreamCancelled

    calls = install_model(
        monkeypatch,
        {
            "qa05": lambda context, output: {
                "issues": [{**item, "recheck": True} for item in output["issues"]]
            }
        },
    )
    checkpoints = []

    def cancelled():
        if sum(module == "qa05" for module, _ in calls) >= 2:
            raise StreamCancelled()

    with pytest.raises(StreamCancelled):
        run(
            service(mode="live"),
            [fact()],
            check_cancelled=cancelled,
            on_checkpoint=lambda step, result, state: checkpoints.append((result, state)),
        )
    public, state = checkpoints[-1]
    assert public["trace"][-1]["status"] == "cancelled"
    assert [item["id"] for item in state["result"]["trace"]] == ["qa01", "qa02", "qa03", "qa04"]
    assert state["work"]["rounds"] == []
    assert public["issues"][0]["status"] == "undetermined"


def test_business_context_is_bounded_public_data_and_fences_partial_reuse(monkeypatch):
    calls = install_model(monkeypatch)
    svc = service(mode="live")
    first = run(
        svc,
        [fact()],
        context={"company": "茶公司", "product": "红茶", "mode_id": "direct", "secret": "private"},
    )
    context = calls[0][1]["case_context"]
    assert context["company"] == "茶公司" and context["product"] == "红茶"
    assert context["stage"]["name"] == "准备" and "secret" not in context
    calls.clear()
    run(
        svc,
        [fact()],
        previous=first,
        affected_issue_ids=[],
        context={"company": "另一公司", "product": "红茶", "mode_id": "direct"},
    )
    assert calls[0][0] == "qa01"


def test_initial_only_fact_citation_is_normalized_to_real_document_id(monkeypatch):
    original = citation("initial-ref", kind="business")
    original["document_id"] = "actual-invoice"
    svc = service(items=[original], mode="live")
    install_model(
        monkeypatch,
        {
            "qa02": lambda context, output: {
                **output,
                "facts": [
                    {
                        "key": "extracted",
                        "label": "材料记载",
                        "value": "value",
                        "source": "材料",
                        "evidence_refs": ["initial-ref"],
                    }
                ],
            },
            "qa05": lambda context, output: {
                "issues": [
                    {**item, "citation_ids": [], "conditions": [], "status": "undetermined"}
                    for item in output["issues"]
                ]
            },
        },
    )
    result = run(svc)
    assert result["citations"] == []
    assert result["facts"][0]["evidence_refs"] == ["actual-invoice"]


def test_deadline_only_sources_are_included_in_issue_citation_cards(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa05": lambda context, output: {
                "issues": [
                    {**item, "citation_ids": [], "conditions": [], "deadlines": [deadline_spec()]}
                    for item in output["issues"]
                ]
            }
        },
    )
    result = run(service(mode="live"), [fact()])
    assert [item["id"] for item in result["issues"][0]["citations"]] == ["law-a"]
    assert [item["id"] for item in result["citations"]] == ["law-a"]


def test_deadline_reports_remaining_time_without_upgrading_conditional_premises():
    from datetime import date

    result = runtime().calculate_deadline(
        deadline_spec(),
        facts=[fact(evidence_status="unverified")],
        citations=[citation()],
        as_of=date(2026, 10, 20),
    )
    assert result["as_of"] == "2026-10-20" and result["remaining_days"] == 2
    assert result["risk"] == "imminent" and result["status"] == "conditional"


def test_jurisdiction_specific_required_facts_are_recomputed_after_scope_is_known(monkeypatch):
    from app.contexts.legal.domain.legal_models import LegalRule

    rule = LegalRule(
        id="cn-specific",
        title="中国特有前提",
        module="qa05",
        jurisdictions=["CN"],
        required_facts=[
            {"key": "cn_condition", "label": "中国前提", "purpose": "仅适用于中国法域", "decisive": True}
        ],
    ).model_dump()
    install_model(monkeypatch)
    result = run(service(mode="live"), [fact()], policy={"enabled": True, "rules": [rule]})
    assert "cn_condition" not in result["issues"][0]["fact_keys"]
    assert not result["followups"]


def test_information_only_question_skips_legal_fact_and_jurisdiction_model_stages(monkeypatch):
    calls = install_model(
        monkeypatch,
        {
            "qa01": lambda context, output: {
                "issues": [{**item, "kind": "information"} for item in output["issues"]]
            }
        },
    )
    result = run(service(mode="live"), [fact()])
    assert [module for module, _ in calls] == ["qa01", "qa05"]
    assert result["issues"][0]["kind"] == "information"
    assert result["trace"][1]["status"] == "skipped"
    assert result["trace"][2]["status"] == "skipped"


def test_deadline_remaining_days_uses_the_deadline_timezone(monkeypatch):
    from datetime import datetime, timezone

    instant = datetime(2026, 10, 12, 18, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(runtime(), "datetime", FixedDatetime)
    result = runtime().calculate_deadline(
        deadline_spec(duration=1), facts=[fact(value="2026-10-11")], citations=[citation()]
    )
    assert result["date"] == "2026-10-12"
    assert result["as_of"] == "2026-10-13" and result["remaining_days"] == -1 and result["risk"] == "overdue"


def test_cached_result_does_not_claim_a_new_model_call(monkeypatch):
    calls = install_model(monkeypatch)
    svc = service(mode="live")
    first = run(svc, [fact()])
    calls.clear()
    refreshed = run(svc, [fact()], previous=first, affected_issue_ids=[])
    assert calls == []
    assert all("已执行有界模型提议" not in step["detail"] for step in refreshed["trace"])


def test_local_midnight_invalidates_cached_deadline_even_on_same_utc_day(monkeypatch):
    from datetime import datetime, timezone

    instant = [datetime(2026, 10, 12, 15, tzinfo=timezone.utc)]

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant[0].astimezone(tz) if tz else instant[0].replace(tzinfo=None)

    monkeypatch.setattr(runtime(), "datetime", FixedDatetime)
    calls = install_model(
        monkeypatch,
        {
            "qa05": lambda context, output: {
                "issues": [{**item, "deadlines": [deadline_spec()]} for item in output["issues"]]
            }
        },
    )
    svc = service(mode="live")
    checkpoints = []
    first = run(svc, [fact()], on_checkpoint=lambda step, result, state: checkpoints.append(state))
    instant[0] = datetime(2026, 10, 12, 18, tzinfo=timezone.utc)
    calls.clear()
    result = run(svc, [fact()], previous=first, affected_issue_ids=[])
    assert any(module == "qa05" for module, _ in calls)
    assert result["issues"][0]["deadlines"][0]["as_of"] == "2026-10-13"
    calls.clear()
    resumed = run(svc, [fact()], resume={"state": checkpoints[4]})
    assert calls[0][0] == "qa01"
    assert resumed["issues"][0]["deadlines"][0]["as_of"] == "2026-10-13"


def test_information_label_cannot_allow_definitive_answers_without_any_basis(monkeypatch):
    install_model(
        monkeypatch,
        {
            "qa01": lambda context, output: {
                "issues": [{**item, "kind": "information", "fact_keys": []} for item in output["issues"]]
            },
            "qa05": lambda context, output: {
                "issues": [{**item, "citation_ids": [], "conditions": []} for item in output["issues"]]
            },
        },
    )
    result = run(service(mode="live"))
    assert result["issues"][0]["status"] == "undetermined"


def test_section_identity_binds_full_issue_question_beyond_visible_title():
    prefix = "同一业务背景下需要核对的情况。" * 20
    original_question = prefix + "是否需要提交进口申报？"
    changed_question = prefix + "是否需要承担合同违约责任？"
    original = run(service(), question=original_question)
    unchanged = run(service(), question=original_question)
    changed = run(service(), question=changed_question)

    assert original["sections"][0]["id"] == unchanged["sections"][0]["id"]
    assert original["sections"][0]["title"] == changed["sections"][0]["title"]
    assert original["issues"][0]["id"] == changed["issues"][0]["id"]
    assert original["sections"][0]["id"] != changed["sections"][0]["id"]
