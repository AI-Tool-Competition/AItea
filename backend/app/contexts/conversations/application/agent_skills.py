"""Small discoverable recipes, not hard-coded routes. Tools remain independently callable."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSkill:
    label: str
    description: str
    tools: tuple[str, ...]
    instructions: str


SKILLS = {
    "guidance": AgentSkill(
        "办理指导",
        "解释当前节点要求并给出下一步建议。",
        ("guide_business", "recall_memory"),
        "先读当前节点、任务状态和用户问题，需要时回忆讨论、检索资料。建议应引用实际结果，不把建议说成已办理。",
    ),
    "materials": AgentSkill(
        "资料核对",
        "找出材料缺口与事实冲突。",
        ("check_materials", "list_documents", "read_document", "search_documents"),
        "结合规则检查和原文核对；需要看包装图时发现 inspect_image。列出缺项、冲突、版本与来源；不自行核验通过。",
    ),
    "legal": AgentSkill(
        "法律分析",
        "使用已配置的法域和证据门禁进行分析。",
        ("analyze_legal", "search_legal_evidence"),
        "法律结论交由 analyze_legal 的既有分析引擎；需要近期信息可组合 web_search，但网页线索不是已审核法规。明确依据不足、适用期与复核状态。",
    ),
    "draft": AgentSkill(
        "文稿起草",
        "结合事实和依据生成可编辑文稿。",
        ("draft_document", "guide_business", "recall_memory"),
        "先收集已知事实；涉及法律要求可组合法律分析；未知信息标待补充。组织完整 Markdown 正文后调用 draft_document，返回保存结果前不声称文稿已经创建。",
    ),
    "handoff": AgentSkill(
        "节点交接",
        "回顾前序节点成果、待办和复核要求。",
        ("get_handoff", "recall_memory", "guide_business"),
        "读取服务端节点进度，区分已完成、待核对和历史讨论。指出当前可做的下一步，不通过聊天改变进度。",
    ),
    "research": AgentSkill(
        "联网调研",
        "实时搜索外部来源并与业务法规交叉核对。",
        ("web_search", "search_legal_evidence"),
        "搜索词不要包含个人身份信息或未公开商业秘密。保留网页链接和抓取时间，优先官方原文；不把搜索摘要当作已审核的法规证据。",
    ),
    "vision": AgentSkill(
        "图片核对",
        "查看当前业务上传的图片并与材料核对。",
        ("list_documents", "inspect_image", "check_materials"),
        "从资料清单选择图片及不可变版本，询问图中可见的信息；不清晰的内容标不确定。图片分析只是本轮观察，不替代 MinerU 入库解析和审核。",
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
        "description": "查找近期官方信息，再与已审核法规交叉核对。",
    },
    {
        "label": "法律分析 + 文稿",
        "skills": ["legal", "draft"],
        "description": "先核对依据和缺口，再起草可编辑文稿。",
    },
]
