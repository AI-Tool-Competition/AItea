"""Small discoverable recipes, not hard-coded routes. Tools remain independently callable."""

from dataclasses import dataclass

from app.configuration.paths import InstancePaths


def resolve_skill(name):
    name = SKILL_ALIASES.get(name, name)
    if name not in SKILLS:
        raise ValueError("未找到该技能")
    return name


def read_skill(name, resource="SKILL.md"):
    name = resolve_skill(name)
    if resource not in ("SKILL.md", *SKILLS[name].resources):
        raise ValueError("未找到该技能资源")
    root = (InstancePaths.resolve().catalogs / "agent-skills" / name).resolve()
    path = (root / resource).resolve()
    if not path.is_relative_to(root):
        raise ValueError("技能资源位置无效")
    if path.stat().st_size > 24000:
        raise ValueError("技能文档超过读取上限")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class AgentSkill:
    label: str
    description: str
    tools: tuple[str, ...]
    resources: tuple[str, ...] = ()


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
        "优先查内部法律库，按需开展中国法或外国法研究、规则适用、法律命题与来源对应检查，解释事实缺项和行动。",
        (
            "lookup_chinese_law",
            "search_legal_evidence",
            "read_law",
            "get_law_versions",
            "web_search",
            "list_documents",
            "read_document",
            "search_documents",
        ),
        ("references/interpretation.md", "references/research.md", "references/foreign-law.md"),
    ),
    "draft": AgentSkill(
        "法律文书起草与改写",
        "起草、改写法律与业务文书；从本业务材料溯源填写模板，按需翻译，使用真实文档工具交付文件。",
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
            "inspect_document_file",
            "create_document_file",
            "edit_document_file",
        ),
        ("references/template-fill.md", "references/translation.md"),
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
        "优先查内部原文和版本，缺项补查官方来源；支持中国法条定位及新加坡法规、判例引用核对方法。",
        ("lookup_chinese_law", "search_legal_evidence", "read_law", "get_law_versions", "web_search"),
        ("references/singapore.md",),
    ),
    "contract_review": AgentSkill(
        "合同审查",
        "回答合同具体问题或审查全文，引用原条款；对照用户业务与谈判底线，区分法律风险和交易影响，给出替换条文。",
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
            "recall_memory",
            "inspect_document_file",
            "create_document_file",
            "edit_document_file",
        ),
        ("references/practice-profile.md", "references/revisions.md"),
    ),
    "litigation_analysis": AgentSkill(
        "诉讼材料与争议分析",
        "读取起诉材料、证据、裁判和庭审记录，区分各方主张与法院认定，解释请求、证明缺口和后续行动。",
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
        ("references/materials.md",),
    ),
    "long_image_to_pdf": AgentSkill(
        "长截图转 PDF",
        "将本业务上传的长截图重叠分页，按行或列排成 A4 PDF，保留原图并提供下载；不进行 OCR。",
        ("list_documents", "convert_long_image_to_pdf"),
    ),
    "document_operations": AgentSkill(
        "文档生成与编辑",
        "生成可下载的Word、PDF、Excel、PPT及文本文件；读取原件后定点修改文字或单元格，保留原件并检查保存结果。",
        ("list_documents", "inspect_document_file", "create_document_file", "edit_document_file"),
        ("references/formats.md",),
    ),
}

SKILL_ALIASES = {
    "china-lawyer-analyst": "legal",
    "chinese-law-verifier": "legal_lookup",
    "contract-review": "contract_review",
    "legal-writing": "draft",
    "litigation-analysis": "litigation_analysis",
    "long-image-to-pdf": "long_image_to_pdf",
    "document-operations": "document_operations",
    "legal-research": "legal",
    "foreign-law-research": "legal",
    "proposition-checking": "legal",
    "adversarial-qc": "legal",
    "sgcite": "legal_lookup",
    "bart-statutory-reference-checker": "legal_lookup",
    "document-fill": "draft",
    "coquill": "draft",
    "legal-translation": "draft",
    "contract-qa": "contract_review",
    "redlines": "contract_review",
    "comms-improver": "guidance",
}


def skill_catalog():
    return [
        {
            "id": key,
            "label": value.label,
            "description": value.description,
            "aliases": [alias for alias, target in SKILL_ALIASES.items() if target == key],
            "tools": list(value.tools),
        }
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
