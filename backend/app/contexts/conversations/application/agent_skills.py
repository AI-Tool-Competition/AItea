"""Small discoverable recipes, not hard-coded routes. Tools remain independently callable."""

from dataclasses import dataclass

from app.configuration.paths import InstancePaths


def read_skill(name):
    if name not in SKILLS:
        raise ValueError("未找到该技能")
    path = InstancePaths.resolve().catalogs / "agent-skills" / name / "SKILL.md"
    if path.stat().st_size > 24000:
        raise ValueError("技能文档超过读取上限")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class AgentSkill:
    label: str
    description: str
    tools: tuple[str, ...]


SKILLS = {
    "guidance": AgentSkill(
        "办理指导",
        "用茶商能理解的话说明下一步，询问影响建议的信息并填写业务卡。",
        ("guide_business", "recall_memory", "update_business_card"),
    ),
    "materials": AgentSkill(
        "资料核对",
        "找出材料缺口与事实冲突。",
        ("check_materials", "list_documents", "read_document", "search_documents", "update_business_card"),
    ),
    "legal": AgentSkill(
        "法律分析",
        "采用china-lawyer-analyst的规则解释、请求权和事实对应方法；按问题深度给判断与行动。",
        ("lookup_chinese_law", "search_legal_evidence", "read_law", "get_law_versions"),
    ),
    "draft": AgentSkill(
        "法律文书起草与改写",
        "按legal-writing方法起草完整法律与业务文书、改写原稿或提供写作反馈；结合事实和原文依据交付可用正文。",
        (
            "draft_document",
            "list_documents",
            "read_document",
            "search_documents",
            "lookup_chinese_law",
            "search_legal_evidence",
            "read_law",
            "get_law_versions",
            "web_search",
            "guide_business",
            "recall_memory",
        ),
    ),
    "handoff": AgentSkill(
        "节点交接",
        "回顾前序节点成果、待办和复核要求。",
        ("get_handoff", "recall_memory", "guide_business"),
    ),
    "research": AgentSkill(
        "联网调研",
        "实时搜索外部来源并与业务法规交叉核对。",
        ("web_search", "search_legal_evidence"),
    ),
    "vision": AgentSkill(
        "图片核对",
        "查看当前业务上传的图片并与材料核对。",
        ("list_documents", "inspect_image", "check_materials"),
    ),
    "business": AgentSkill(
        "商业模式决策", "比较渠道、收入、投入和交易风险，形成有理由的建议。", ("web_search",)
    ),
    "export_planning": AgentSkill(
        "跨境交易准备",
        "识别交易主体，组织商业选择、合规依据及办理顺序。",
        ("search_legal_evidence", "read_law", "web_search"),
    ),
    "legal_lookup": AgentSkill(
        "法规原文查询",
        "采用chinese-law-verifier的全称与条号查询；取得原文、位置和实际来源。",
        ("lookup_chinese_law", "search_legal_evidence", "read_law"),
    ),
    "contract_review": AgentSkill(
        "合同审查",
        "按用户立场审查合同及条款，对照谈判底线，说明交易风险并给出可直接替换的条文与备选方案。",
        (
            "list_documents",
            "read_document",
            "search_documents",
            "lookup_chinese_law",
            "search_legal_evidence",
            "read_law",
            "get_law_versions",
            "web_search",
            "draft_document",
        ),
    ),
}


def skill_catalog():
    return [
        {"id": key, "label": value.label, "description": value.description, "tools": list(value.tools)}
        for key, value in SKILLS.items()
    ]


COMPOSITION_HINTS = [
    {
        "label": "图片 + 资料核对",
        "skills": ["vision", "materials"],
        "description": "识别包装图，再核对材料与填写信息。",
    },
    {
        "label": "联网 + 法律分析",
        "skills": ["research", "legal"],
        "description": "查找近期官方原文，与内部法规的版本和适用期间交叉核对。",
    },
    {
        "label": "法律分析 + 文稿",
        "skills": ["legal", "draft"],
        "description": "先核对依据和缺口，再起草可编辑文稿。",
    },
]
