"""One sourced subject graph for retrieval, legal reasoning and executable tasks.

The routing turn proposes identities; this module only preserves grounded claims.
Unknown actors remain explicit hypotheses. It never infers a licence or transfers
statutory responsibility from a delegation or a contractual allocation.
"""

import re

from app.contexts.legal_analysis.domain.analysis_models import BusinessContext


def source_texts(question, facts, context=None):
    texts = [question]
    texts.extend(str(f.get("value") or "") for f in facts)
    for message in (context or {}).get("recent_messages", []):
        if message.get("role") == "user":
            texts.append(message.get("content", ""))
    return texts


def ground_context(proposal, question, facts, context=None):
    graph = BusinessContext.model_validate(proposal or {}).model_dump(mode="json")
    texts = source_texts(question, facts, context)

    def grounded(sources):
        return list(dict.fromkeys(s for s in sources if any(s in text for text in texts)))

    actors = {}
    identity_claims = {a.get("id"): a.get("identity_status") for a in (proposal or {}).get("actors", [])}
    for actor in graph["actors"]:
        actor["sources"] = grounded(actor["sources"])
        # A quote supporting one role does not support every additional role.
        # In particular, a factory is not evidence of plantation ownership.
        role_terms = {
            "种植": ("种植", "茶园", "农场"),
            "生产": ("生产", "加工", "工厂", "茶厂", "甲厂"),
            "进口": ("进口",),
            "零售": ("零售", "实体店", "门店"),
        }
        actor["roles"] = [
            role
            for role in actor["roles"]
            if all(
                term not in role or any(word in quote for word in synonyms for quote in actor["sources"])
                for term, synonyms in role_terms.items()
            )
        ]
        actor["identity_status"] = (
            "identified" if actor["sources"] and identity_claims.get(actor["id"]) != "unknown" else "unknown"
        )
        actors.setdefault(actor["id"], actor)
    graph["actors"] = list(actors.values())
    relations = []
    for item in graph["relationships"]:
        if item["from_actor_id"] in actors and item["to_actor_id"] in actors:
            item["sources"] = grounded(item["sources"])
            relations.append(item)
    graph["relationships"] = relations
    credentials = {}
    for item in graph["credentials"]:
        if item["actor_id"] not in actors:
            continue
        item["sources"] = grounded(item["sources"])
        if not item["sources"]:
            item.update(status="unknown", proof="")
        else:
            # Conversation proposals cannot verify documents or external records.
            item["status"] = "reported_completed" if item["status"] != "unknown" else "unknown"
            if item["proof"] and not any(item["proof"] in text for text in texts):
                item["proof"] = ""
        credentials.setdefault(item["id"], item)
    graph["credentials"] = list(credentials.values())
    return graph


def seed_context(question, facts, context=None, proposal=None):
    if proposal and proposal.get("actors"):
        return ground_context(proposal, question, facts, context)
    # A direct workbench request may have no routing turn. Record only explicit
    # self-identification; the analysis may refine this graph against the rules.
    claim = re.search(r"(?:我是|我是一[家名位]?)([^，。；\n]{1,50})", question)
    graph = {}
    if claim:
        description = claim[0]
        roles = [
            role
            for role in ("贸易商", "生产商", "买方", "卖方", "进口商", "雇员", "雇主")
            if role in description
        ]
        if "茶商" in description:
            roles.append("贸易商")
        graph = {
            "actors": [
                {
                    "id": "user",
                    "name": "用户",
                    "roles": list(dict.fromkeys(roles)),
                    "identity_status": "identified",
                    "sources": [description],
                }
            ]
        }
    return ground_context(graph, question, facts, context)


def task_type(question):
    if re.search(r"审查|审阅|修改.{0,8}合同|合同.{0,8}(?:风险|条款)", question):
        return "contract_review"
    if re.search(r"违约|不付款|侵权|赔偿|起诉|仲裁|解除劳动", question):
        return "dispute"
    if re.search(r"规划|顺序|先做什么|流程.{0,10}(?:改变|保留)|负责哪些|怎么安排", question):
        return "business_plan"
    if re.search(r"(?:查|检索|原文来源).{0,30}(?:法|规定|条)|官方原文来源就行", question) and not re.search(
        r"我(?:能否|是否|可以|需要)", question
    ):
        return "lookup"
    return "consultation"
