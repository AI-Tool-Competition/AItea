"""Account-bound tools; model arguments cannot choose owners, services or file paths."""

import asyncio
import base64
import copy
import json
import hashlib
import re
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

from app.contexts.conversations.application.agent_skills import (
    COMPOSITION_HINTS,
    SKILLS,
    read_skill,
    resolve_skill,
    skill_catalog,
)
from app.contexts.conversations.application.tool_runtime import ToolFailure, ToolInput, ToolSpec
from app.contexts.conversations.infrastructure.agent_transport import gateway_tool
from app.shared.domain.countries import resolve_country
from app.shared.domain.errors import AppError, fail
from app.shared.domain.models import StrictModel
from app.shared.domain.values import now


class EmptyInput(ToolInput):
    reason: str = Field(
        default="", max_length=300, description="可选的简短调用目的，不改变工具权限或执行规则。"
    )


class QueryInput(ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)


class SkillInput(ToolInput):
    name: str = Field(min_length=1, max_length=64, description="技能目录的id或alias。")
    resource: str = Field(
        default="SKILL.md",
        min_length=1,
        max_length=120,
        description="默认加载入口；仅需细节时传已返回的resources名称，不能传本机文件位置。",
    )


class TasksInput(ToolInput):
    task_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="不传或 [] 核对当前节点全部任务；仅用户限定某任务时传服务端返回的任务 id，不推测编号。",
    )


class CardFieldInput(StrictModel):
    key: str = Field(min_length=1, max_length=64)
    value: StrictStr | StrictInt | StrictFloat = Field(
        description="从茶商原话摘录的字段值；不填推测、建议或假设。"
    )
    source_quote: str = Field(min_length=1, max_length=20000, description="包含该值的逐字原话。")
    source_message_id: str | None = Field(
        default=None, max_length=100, description="当前发言省略；引用较早发言时传当前业务真实用户消息id。"
    )
    replace_existing: bool = Field(
        default=False, description="仅原话明确改为、更正或调整时，才可替换已填值。"
    )


class CardUpdateInput(StrictModel):
    task_id: str = Field(min_length=1, max_length=64)
    fields: list[CardFieldInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_fields(self):
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("同一业务卡字段只提供一次")
        return self


class CardEntryInput(ToolInput):
    task_id: str = Field(min_length=1, max_length=64, description="业务卡的实际task_id。")
    values_json: str = Field(
        min_length=2,
        max_length=12000,
        description='同一张卡的全部已知字段，JSON对象字符串，例如 {"p01_owner":"李明","p01_stock":"200盒"}。只用实际key，值逐字摘录用户原话。',
    )


class DocumentsInput(ToolInput):
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=20, ge=1, le=50)


class DocumentInput(ToolInput):
    document_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=10, ge=1, le=30)


class ImageInput(ToolInput):
    document_id: str = Field(
        min_length=1, max_length=100, description="list_documents 返回的当前业务文档 id，不是文件名。"
    )
    version: int = Field(ge=1, description="该文档实际返回的不可变版本号。")
    prompt: str = Field(
        min_length=1, max_length=2000, description="只询问图中可见内容，不清楚的内容标为不确定。"
    )
    image_id: str | None = Field(
        default=None,
        max_length=200,
        description="原始 PNG/JPEG/WebP 图片必须省略或传 JSON null；仅查看文档页面图时传 read_document 返回的真实 image_id，不能填字符串 none/null 或猜测。",
    )

    @field_validator("image_id")
    @classmethod
    def real_page_id(cls, value):
        if value is not None and value.strip().lower() in ("", "none", "null", "undefined"):
            raise ValueError("原图请省略 image_id 或传 JSON null；页面图必须使用实际返回的编号")
        return value


class ImagePDFInput(ToolInput):
    document_id: str = Field(min_length=1, max_length=100, description="当前业务已上传原图的真实编号。")
    version: int = Field(ge=1)
    slice_height: int = Field(default=2000, ge=400, le=4000)
    overlap: int = Field(default=200, ge=0, le=2000)
    cols: int = Field(default=2, ge=1, le=3)
    rows: int = Field(default=2, ge=1, le=4)
    layout: Literal["grid", "column"] = "grid"

    @model_validator(mode="after")
    def bounded_overlap(self):
        if self.overlap > self.slice_height // 2:
            raise ValueError("重叠不能超过切片高度的一半")
        return self


class FileInspectInput(ToolInput):
    document_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=20000)
    limit: int = Field(default=20, ge=1, le=30)


class FileCreateInput(ToolInput):
    filename: str = Field(min_length=1, max_length=160, description="带真实扩展名的文件名，不传本机位置。")
    format: Literal["docx", "pdf", "xlsx", "pptx", "md", "txt", "csv"]
    content: str = Field(
        min_length=1,
        max_length=40000,
        description='Word/PDF/MD/TXT用正文，支持#标题和管线表格；CSV用实际CSV。Excel用JSON字符串 {"sheets":[{"name":"报价","rows":[["产品","价格"],["六堡茶",120]]}]}；PPT用 {"slides":[{"title":"标题","bullets":["内容"]}]}。不传代码或任意文件位置。',
    )


class FileReplacement(StrictModel):
    part: str = Field(
        default="",
        max_length=160,
        description="Word/PPT须逐字使用inspect_document_file返回的part；文本文件留空。",
    )
    old_text: str = Field(min_length=1, max_length=6000)
    new_text: str = Field(max_length=6000)
    expected_count: int = Field(default=1, ge=1, le=20)


class FileCellEdit(StrictModel):
    sheet: str = Field(min_length=1, max_length=31)
    cell: str = Field(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    expected_value: StrictStr | StrictInt | StrictFloat | bool | None
    value: StrictStr | StrictInt | StrictFloat | bool | None

    @field_validator("expected_value", "value")
    @classmethod
    def bounded_cell(cls, value):
        import math

        if isinstance(value, str) and len(value) > 4000:
            raise ValueError("单元格文字最多4000字")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("数值必须有限")
        return value


class FileEditInput(ToolInput):
    document_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    filename: str = Field(min_length=1, max_length=160, description="另存副本名称，扩展名保持原格式。")
    replacements: list[FileReplacement] = Field(default_factory=list, max_length=30)
    cells: list[FileCellEdit] = Field(default_factory=list, max_length=100)


class WebInput(ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    domains: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="仅填用户指定或已由实际搜索结果确认的域名，不猜测政府域名后缀；不确定时省略或传 []。",
    )
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    max_results: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="before")
    @classmethod
    def result_count(cls, value):
        # Older tool examples used limit. Normalize only this harmless count;
        # keep strict validation of scope, dates, extra fields and boundaries.
        if isinstance(value, dict):
            value = dict(value)
            if "limit" in value:
                if "max_results" in value:
                    raise ValueError("仅传 max_results，不同时传 limit")
                value["max_results"] = value.pop("limit")
            count = value.get("max_results")
            if isinstance(count, str) and re.fullmatch(r"[0-9]{1,2}", count):
                value["max_results"] = int(count)
            recency = value.get("recency_days")
            if isinstance(recency, str) and recency.strip().lower() in {"", "none", "null"}:
                value["recency_days"] = None
        return value


class LegalSearchInput(QueryInput):
    limit: int = Field(default=4, ge=1, le=12)
    as_of: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="交易或法律适用日期；未知时省略，不推定所有入库资料现行有效。",
    )
    jurisdiction: Literal["CN", "ID", "SG"] = Field(
        description="按本次法律问题选择单一法域，不根据贸易目的地追加国家。"
    )

    @field_validator("as_of")
    @classmethod
    def calendar_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


class ChineseLawInput(ToolInput):
    law: str = Field(
        min_length=4,
        max_length=150,
        description="法规标准全称，例如中华人民共和国民法典，不使用简称；只发送法规名称，不传案件事实。",
    )
    articles: list[str] = Field(
        min_length=1,
        max_length=4,
        description="具体条号，最多四条；例如第五百七十九条、579，程序转为中文条号。",
    )
    source: Literal["auto", "api"] = Field(
        default="auto",
        description="auto优先读取内置同名同条原文，缺项调用公益API；api直接核对公益API，失败时仍查内置原文。",
    )

    @field_validator("law")
    @classmethod
    def full_name(cls, value):
        if not re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9（）()〈〉·—\-〔〕]+", value):
            raise ValueError("只传法规标准全称")
        return value

    @field_validator("articles")
    @classmethod
    def article_labels(cls, value):
        from app.contexts.legal_analysis.infrastructure.chinese_law import article_number

        return list(dict.fromkeys(article_number(label) for label in value))


class LawReadInput(ToolInput):
    jurisdiction: Literal["CN", "ID", "SG"]
    record_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description="search_legal_evidence 返回的record_id（原始收据为metadata.source_record_id），不猜测条款编号。",
    )
    url: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
        description="用户提供或检索返回的本法域官方原文URL；与record_id二选一。",
    )
    query: str = Field(default="", max_length=500, description="读取官方页面时定位具体条款的关键词或条号。")

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.record_id) == bool(self.url):
            raise ValueError("请选择一个内部记录编号或官方原文URL。")
        return self


class LawVersionInput(ToolInput):
    jurisdiction: Literal["CN", "ID", "SG"]
    record_id: str = Field(
        min_length=1, max_length=300, description="检索或读取原文返回的真实 source_record_id。"
    )


class DraftInput(ToolInput):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(
        min_length=1,
        max_length=18000,
        description="完整 Markdown 正文。用户陈述标明来源，未核验内容不能写已确认；正文不包含工具名、任务编号或内部状态。",
    )
    task_id: str = Field(
        min_length=1,
        max_length=64,
        description="当前节点的真实任务 id；上下文或 guide_business 可取得，不使用任务标题或猜测编号。",
    )


class RememberInput(ToolInput):
    content: str = Field(min_length=1, max_length=2000)
    source_quote: str = Field(min_length=1, max_length=2000)
    kind: Literal["preference", "note"] = Field(
        default="note", description="回复格式、语言、先后顺序等表达偏好选 preference；业务事项笔记选 note。"
    )


class PlanInput(ToolInput):
    steps: list[str] = Field(min_length=1, max_length=8)
    current: int = Field(ge=0, le=8)


class ReceiptInput(ToolInput):
    call_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0, le=240000)
    limit: int = Field(default=8000, ge=1, le=12000)


@dataclass(frozen=True)
class BoundTool:
    spec: ToolSpec
    handler: object

    def schema(self):
        schema = self.spec.input_model.model_json_schema()
        # Accept historical inputs without requiring a discarded task object.
        schema.get("properties", {}).pop("objective_ids", None)
        schema.get("properties", {}).pop("purpose", None)

        def inline(value):
            # Some OpenAI-compatible gateways discard $defs when translating
            # function declarations. Send complete nested input types instead
            # of leaving objectives/actors as unresolved references.
            if isinstance(value, list):
                return [inline(item) for item in value]
            if not isinstance(value, dict):
                return value
            if "$ref" in value:
                name = value["$ref"].removeprefix("#/$defs/")
                return inline(
                    {
                        **copy.deepcopy(schema["$defs"][name]),
                        **{k: v for k, v in value.items() if k != "$ref"},
                    }
                )
            return {key: inline(item) for key, item in value.items() if key != "$defs"}

        return {
            "type": "function",
            "function": {
                "name": self.spec.name,
                "description": (
                    f"{self.spec.description}\n进入条件：{self.spec.entry_condition}"
                    f"\n返回内容：{self.spec.result_contract}"
                    f"\n调用成本：{self.spec.cost_class}；超时上限：{self.spec.timeout_seconds}秒。"
                ),
                "parameters": inline(schema),
            },
        }

    def mcp_schema(self):
        function = self.schema()["function"]
        return {
            "name": self.spec.name,
            "title": self.spec.label,
            "description": function["description"],
            "inputSchema": function["parameters"],
        }


class AgentTools:
    def __init__(self, service, owner, case, stage, payload, prepared, run_id, check_cancelled):
        self.service, self.owner, self.case, self.stage = service, owner, case, stage
        self.payload, self.prepared, self.run_id = payload, prepared, run_id
        self.check_cancelled = check_cancelled
        self.pending_pdfs = []
        self.pending_files = []
        self.inspected_files = set()
        definitions = [
            (
                "inspect_document_file",
                "检查文档文件",
                "检查可读业务原件的段落、正文部件、工作表单元格或PDF页；不调用OCR、不索引、不外发。编辑前须读取目标位置，返回原文、part或sheet/cell。分页不会省略其余内容的存在。",
                FileInspectInput,
                self.inspect_file,
                True,
                25,
            ),
            (
                "create_document_file",
                "生成文档文件",
                "仅新建DOCX、PDF、XLSX、PPTX、MD、TXT或CSV；修改已有文件使用inspect_document_file再edit_document_file，不用全文重写替代定点修改。实际生成并重开检查，答复成功后由程序提供下载链接，不编造链接。每轮最多3份。格式细节见document_operations技能。",
                FileCreateInput,
                self.create_file,
                False,
                25,
            ),
            (
                "edit_document_file",
                "编辑文档副本",
                "用户要求修改文件时，先inspect_document_file，后对当前账号本业务文件精确替换文字，或修改Excel已存在的非公式单元格。核对旧值、保留非目标部件和原件，另存副本。PDF不支持直接改字；每轮最多3份文件。",
                FileEditInput,
                self.edit_file,
                False,
                25,
            ),
            (
                "convert_long_image_to_pdf",
                "长截图转 PDF",
                "用户要求将当前业务上传原图排成PDF时调用；重叠切片并排成A4，原图不变。不进行OCR或外发图片。返回实际页数及pending_commit，下载链接由答复提交后程序追加。每轮最多生成一个PDF。",
                ImagePDFInput,
                self.image_pdf,
                False,
                30,
            ),
            (
                "update_business_card",
                "填写业务卡",
                "茶商提供业务事实或更正时，主动一次填写同一卡片的全部已知字段；多张卡可在同轮分别调用。只用实际task_id和key，values_json是JSON对象字符串；程序自动关联真实用户发言。未知项留空，不填假设、建议或核验结果。答复成功后共同提交，供茶商核对。",
                CardEntryInput,
                self.fill_card,
                False,
                10,
            ),
            (
                "read_tool_result",
                "回查工具结果",
                "分页回查本轮已完成工具的持久收据。上下文压缩或结果过长时按 call_id 读取，不重新调用外部服务。",
                ReceiptInput,
                self.receipt,
                True,
                10,
            ),
            (
                "guide_business",
                "办理指导",
                "仅在用户要求查看或填写工作台现有节点时，读取办理要求、任务字段和服务端进度；一般商业或法律咨询不调用。",
                EmptyInput,
                self.guide,
                True,
                30,
            ),
            (
                "check_materials",
                "资料核对",
                "按现有服务端规则核对当前节点的材料缺口、确认状态和字段；不会将任务标记完成。",
                TasksInput,
                self.materials,
                True,
                30,
            ),
            (
                "draft_document",
                "工作台文稿保存",
                "保存工作台内的Markdown文稿，须关联当前任务；此工具不生成Word、PDF、Excel或PPT下载文件。用户要文件时直接create_document_file，编辑原件时按需加载document_operations。",
                DraftInput,
                self.draft,
                False,
                30,
            ),
            (
                "get_handoff",
                "节点交接",
                "读取前序及当前节点的服务端进度、复核标记，以及明确的下一节点。tasks 是节点内任务，不能当成下一节点；可与材料核对同时调用。",
                EmptyInput,
                self.handoff,
                True,
                30,
            ),
            (
                "tool_search",
                "查找系统能力",
                "仅查找本系统工具及参数，不能查询网站或业务信息。查询网址、展会、企业、渠道或市场信息直接调用已提供的web_search；只有缺少对应能力时才用此工具。",
                QueryInput,
                self.discover,
                False,
                10,
            ),
            (
                "discover_tools",
                "发现工具",
                "tool_search的兼容名称：搜索并加载本次所需工具。",
                QueryInput,
                self.discover,
                False,
                10,
            ),
            (
                "load_skill",
                "加载组合技能",
                "按技能id或别名读取SKILL.md及相关工具参数；细节按返回的resources名称读取。按需调用，无固定执行顺序。",
                SkillInput,
                self.skill,
                False,
                10,
            ),
            (
                "list_documents",
                "列出业务资料",
                "分页查找当前业务及可读资料库的文档标识、版本和类型。",
                DocumentsInput,
                self.documents,
                True,
                30,
            ),
            (
                "read_document",
                "读取资料原文",
                "分页读取当前业务上传文档的已有解析结果。法规库的pack/web记录应使用read_law及其record_id，不传入此工具。未解析材料返回真实状态。",
                DocumentInput,
                self.document,
                True,
                30,
            ),
            (
                "search_documents",
                "检索业务资料",
                "通过 RAGFlow 查找当前业务和允许资料库中的已审核原文，返回不可变版本和定位。",
                QueryInput,
                self.search,
                True,
                150,
            ),
            (
                "lookup_chinese_law",
                "精确查询中国法条",
                "用户或已读原文明示中国法规全称和条号时直接取完整原文与定位；根据案情选规则先用search_legal_evidence，勿凭记忆猜条号。内置缺项调用chinese-law-verifier公益API（非官方来源），短超时后保留内置结果；不推定接口文本现行有效。",
                ChineseLawInput,
                self.chinese_law,
                True,
                10,
            ),
            (
                "search_legal_evidence",
                "检索法律依据",
                "按指定法域查询内部资料，返回原文、版本、来源与审核状态；草稿资料仍须标明效力限制。",
                LegalSearchInput,
                self.legal_search,
                True,
                120,
            ),
            (
                "read_law",
                "读取法律原文",
                "按实际记录编号或本法域官方URL读取法律原文，保留例外和版本限制；官方PDF需先解析，搜索摘要不能作为原文。",
                LawReadInput,
                self.read_law,
                True,
                30,
            ),
            (
                "get_law_versions",
                "读取法规版本关系",
                "按已返回的记录编号取得公布、施行、失效日期及已查明的修改替代关系；未知日期保持为空，不作法律适用结论。",
                LawVersionInput,
                self.law_versions,
                True,
                15,
            ),
            (
                "inspect_image",
                "识别图片",
                "分析当前业务原图或已有页面图。原始图片省略 image_id，即使正文解析失败也可查看原图；页面图只能使用 read_document 返回的真实编号。识图失败不能声称看到了图中文字，观察也不等于已确认事实。",
                ImageInput,
                self.image,
                True,
                90,
            ),
            (
                "web_search",
                "联网搜索",
                "直接联网查询网站、展会报名、企业、行业协会、平台、渠道和市场信息。官网、企业网站、行业媒体及其他相关公开网页均可使用，无需审核后才能提供内容或网址。返回网站名称、完整URL、摘要及检索时间；域名不明时省略domains。无结果可改关键词再查一次。不要外发用户敏感信息。",
                WebInput,
                self.web,
                True,
                90,
            ),
            (
                "recall_memory",
                "检索记忆",
                "按当前账号、业务、节点检索已保存记忆、会话摘要及带来源的历史讨论。",
                QueryInput,
                self.recall,
                True,
                30,
            ),
            (
                "remember_memory",
                "保存用户记忆",
                "仅用户明确要求记住时，暂存原话中的偏好或笔记。必须提供当前消息中的逐字 source_quote。",
                RememberInput,
                self.remember,
                False,
                10,
            ),
            (
                "update_plan",
                "调整执行计划",
                "记录简短公开操作计划及当前步骤；不能标记业务任务完成，不输出思考过程。",
                PlanInput,
                self.plan,
                False,
                10,
            ),
        ]
        self.registry = {
            name: BoundTool(
                ToolSpec(
                    name,
                    label,
                    description,
                    model,
                    2
                    if name in {"web_search", "search_legal_evidence", "read_law", "lookup_chinese_law"}
                    else 1,
                    timeout,
                    safe,
                ),
                handler,
            )
            for name, label, description, model, handler, safe, timeout in definitions
        }
        capabilities = {
            "guide_business": (
                "operations",
                "用户要求查看当前工作台节点、填写现有任务或核对办理进度；一般经营咨询无需读取。",
                "实际节点、任务、填写情况及相关办理指导，不能扩展用户咨询范围。",
                "local",
            ),
            "check_materials": (
                "operations",
                "用户要求核对已建立业务中的字段、上传材料或任务。",
                "服务端材料检查结果及具体缺项，不改变核验状态。",
                "local",
            ),
            "get_handoff": (
                "operations",
                "用户要求核对工作台节点进度或办理交接；连续咨询沿用会话事实，无需读取节点。",
                "实际节点进度与交接要求，不能将模板事项当作已经讨论或完成的事实。",
                "local",
            ),
            "search_legal_evidence": (
                "legal_lookup",
                "需要查找法律依据、相关完整条文或版本线索。",
                "规范原文、版本、定位和来源；未命中仅表示本次覆盖不足。",
                "retrieval",
            ),
            "read_law": (
                "legal_lookup",
                "已取得记录编号或官方URL，须读取原文。",
                "完整原文单元、定位、版本与官方链接。",
                "local_or_network",
            ),
            "web_search": (
                "research",
                "需要网站地址、展会报名、企业、渠道、市场或其他公开实时信息。",
                "相关公开网页的名称、完整网址、摘要及检索时间，可直接用于业务答复；具体法条内容仍读取原文。",
                "network",
            ),
            "draft_document": (
                "document",
                "用户要求文书，内容及真实任务编号已具备。",
                "完整文稿及待提交状态。",
                "local_write",
            ),
        }
        for name, metadata in capabilities.items():
            tool = self.registry[name]
            self.registry[name] = BoundTool(
                replace(
                    tool.spec,
                    **dict(
                        zip(
                            ("capability", "entry_condition", "result_contract", "cost_class"),
                            metadata,
                            strict=True,
                        )
                    ),
                ),
                tool.handler,
            )

    def current(self):
        with closing(self.service.db.connect()) as conn:
            case = self.service.case_detail(self.owner, self.case["id"], conn)
            if self.payload.get("conversation_id"):
                self.service.node_workspace.own_conversation(
                    conn, self.owner, case["id"], self.payload["conversation_id"], self.stage["id"]
                )
            if case["revision"] != self.case["revision"]:
                fail(409, "agent_context_changed", "业务资料已变化，请重新发送后再使用本次结果")
            stage = next(s for s in case["stages"] if s["id"] == self.stage["id"])
            if stage["status"] == "locked":
                fail(409, "stage_locked", "节点尚未解锁")
            return case, stage

    def guide(self, args, attempt):
        case, stage = self.current()
        return {
            "stage": stage["name"],
            "tasks": stage["tasks"],
            "facts": case["facts"],
            "workflow_guidance": (stage.get("analysis") or case["template"]["agent"]).get("instructions", "")[
                :8000
            ],
            "notice": "这是服务端当前办理要求；聊天建议不能替代任务核验。",
        }

    def fill_card(self, args, attempt):
        from app.contexts.workflows.application.card_updates import prepare_card_updates

        case, _ = self.current()
        attempt.check()
        try:
            values = json.loads(args.values_json)
        except ValueError:
            fail(422, "invalid_card_values", "values_json需为JSON对象字符串，字段值使用字符串。")
        if (
            not isinstance(values, dict)
            or not 1 <= len(values) <= 20
            or any(not isinstance(value, str) for value in values.values())
        ):
            fail(422, "invalid_card_values", "每次填写同一卡片1至20个已有字段，值使用原话字符串。")
        fields = []
        with closing(self.service.db.connect()) as conn:
            for key, value in values.items():
                if not value.strip():
                    continue
                text, message_id = self.payload["content"], None
                if value and value not in text:
                    row = conn.execute(
                        "SELECT id,content FROM messages WHERE case_id=? AND role='user' "
                        "AND instr(content,?)>0 ORDER BY created_at DESC,id DESC LIMIT 1",
                        (case["id"], value),
                    ).fetchone()
                    if row:
                        text, message_id = row["content"], row["id"]
                fields.append(
                    CardFieldInput(
                        key=key,
                        value=value,
                        source_quote=text,
                        source_message_id=message_id,
                        replace_existing=message_id is None,
                    )
                )
        updates = [CardUpdateInput(task_id=args.task_id, fields=fields)] if fields else []
        return prepare_card_updates(self.service, self.owner, case, self.payload["content"], updates)

    def materials(self, args, attempt):
        _, stage = self.current()
        tasks = {task["id"]: task for task in stage["tasks"]}
        selected = args.task_ids or list(tasks)
        if any(key not in tasks for key in selected):
            fail(422, "task_scope", "只能核对当前节点的任务")
        with closing(self.service.db.connect()) as conn:
            checks = []
            for key in dict.fromkeys(selected):
                attempt.check()
                task = tasks[key]
                checks.extend(
                    item | {"task_id": key, "task_title": task["title"]}
                    for item in self.service.checks(conn, task, task.get("confirmed", False))
                )
        return {
            "checks": checks,
            "notice": "缺项针对尚未填写或核验的服务端记录；聊天中已明确提供的信息应主动填写对应业务卡，不重复索要。检查不会改变任务完成状态。",
        }

    def handoff(self, args, attempt):
        case, _ = self.current()
        index = next(i for i, stage in enumerate(case["stages"]) if stage["id"] == self.stage["id"])
        return {
            "current_stage_id": self.stage["id"],
            "stages": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "status": s["status"],
                    "tasks": [
                        {key: task.get(key) for key in ("id", "title", "status", "needs_review")}
                        for task in s["tasks"]
                    ],
                }
                for s in case["stages"][: index + 1]
            ],
            "facts": case["facts"],
            "next_stage": (
                {key: case["stages"][index + 1][key] for key in ("id", "name", "status")}
                if index + 1 < len(case["stages"])
                else None
            ),
            "notice": "stages 内的 tasks 是同节点任务，下一节点仅以 next_stage 为准；null 表示没有后续节点。状态直接来自服务端，不因聊天建议解锁。向用户使用节点名称和任务标题，不展示内部编号或状态码。",
        }

    def discover(self, args, attempt):
        words = set(re.findall(r"[a-z_][a-z0-9_]*|[\u3400-\u9fff]{2}", args.query.lower()))
        for phrase in re.findall(r"[\u3400-\u9fff]{2,}", args.query):
            words.update(phrase[i : i + 2] for i in range(len(phrase) - 1))

        def text(tool):
            return (
                tool.spec.name
                + tool.spec.description
                + tool.spec.label
                + tool.spec.entry_condition
                + tool.spec.result_contract
                + tool.spec.capability
            ).lower()

        ranked = sorted(
            [
                t
                for t in self.registry.values()
                if t.spec.name not in {"tool_search", "discover_tools", "load_skill"}
            ],
            key=lambda tool: (
                -(100 if tool.spec.name in args.query else 0) - sum(word in text(tool) for word in words)
            ),
        )
        matched = [t for t in ranked if any(w in text(t) for w in words)]
        names = [tool.spec.name for tool in matched[: min(args.limit, 5)]]
        return {
            "kind": "capability_discovery",
            "business_progress": False,
            "activate_tools": names,
            "tools": [self.registry[name].schema() for name in names],
            "notice": "这里只查找系统能力，没有查询网页或业务资料。网站和公开业务信息请直接执行web_search，已有常用工具不依赖本次发现结果。",
        }

    def skill(self, args, attempt):
        try:
            name = resolve_skill(args.name)
        except ValueError:
            raise ToolFailure("unknown_skill", "未找到该技能，请使用目录中的id或别名。") from None
        skill = SKILLS[name]
        try:
            instructions = read_skill(name, args.resource)
        except (OSError, ValueError):
            raise ToolFailure(
                "skill_unavailable", "该技能文档暂时无法读取；可搜索独立工具完成已有条件支持的任务。"
            ) from None
        return {
            "skill": name,
            "requested_name": args.name,
            "resource": args.resource,
            "resources": list(skill.resources),
            "instructions": instructions,
            "activate_tools": [name for name in skill.tools if name in self.registry],
            "tools": [self.registry[name].schema() for name in skill.tools if name in self.registry],
        }

    def documents(self, args, attempt):
        self.current()
        result = self.service.documents(
            self.owner, self.case["id"], args.limit, args.offset, q=args.query or None
        )
        return {
            "items": [
                {
                    key: doc.get(key)
                    for key in ("id", "title", "version", "mime_type", "role", "status", "case_id")
                }
                for doc in result["items"]
            ],
            "total": result["total"],
            "offset": args.offset,
        }

    def scoped_document(self, document_id, version):
        self.current()
        with closing(self.service.db.connect()) as conn:
            doc = dict(self.service.read_document(conn, self.owner, document_id))
            if doc["case_id"] not in (None, self.case["id"]):
                fail(404, "not_found", "未找到当前业务的资料")
            if doc["version"] != version:
                fail(409, "document_version_changed", "资料版本不一致，请重新选择")
            return doc

    def document(self, args, attempt):
        self.scoped_document(args.document_id, args.version)
        result = self.service.document_detail(
            self.owner,
            args.document_id,
            element_offset=args.offset,
            chunk_offset=args.offset,
            limit=args.limit,
            include_preview=True,
        )
        return {
            key: result[key]
            for key in (
                "document",
                "elements",
                "chunks",
                "element_total",
                "chunk_total",
                "warnings",
                "preview",
            )
        }

    def search(self, args, attempt):
        self.current()
        return self.service.search(
            self.owner,
            args.query,
            self.case["id"],
            args.limit,
            country=resolve_country(self.case["template"]["destination"]) or "unassigned",
        )

    def chinese_law(self, args, attempt):
        from app.contexts.documents.infrastructure.country_packs import CountryPacks
        from app.contexts.legal_analysis.infrastructure.chinese_law import article_index, fetch_article

        self.current()
        if "CN" not in self.service.legal.allowed_countries(self.case["template"], self.stage):
            fail(422, "country_mismatch", "中国法不在当前业务允许范围内")
        attempt.check()
        packs = CountryPacks(self.service.db.data_dir)
        local = packs.lookup_chinese_articles(args.law, [article_index(a) for a in args.articles])
        local = [self.identify_law(item) for item in local]
        found = {str(item.get("locator", {}).get("article")) for item in local}
        needed = [a for a in args.articles if args.source == "api" or article_index(a) not in found]
        deadline = monotonic() + min(6, attempt.remaining)

        def obtain(article):
            try:
                return article, fetch_article(args.law, article, deadline=deadline, check=attempt.check)
            except (ValueError, OSError):
                return article, None

        remote = []
        if needed:
            with ThreadPoolExecutor(max_workers=min(4, len(needed)), thread_name_prefix="tea-cn-law") as pool:
                remote = list(pool.map(obtain, needed))
        attempt.check()
        items = [item for _, item in remote if item]
        remote_found = {article_index(a) for a, item in remote if item}
        items.extend(
            item for item in local if str(item.get("locator", {}).get("article")) not in remote_found
        )
        missing = [
            a
            for a in args.articles
            if not any(str(item.get("locator", {}).get("article")) in {a, article_index(a)} for item in items)
        ]
        return {
            "items": items,
            "mode": "exact_article",
            "missing_articles": missing,
            "api_status": "unavailable_or_unmatched"
            if any(item is None for _, item in remote)
            else "retrieved"
            if remote
            else "not_needed",
            "notice": "原文来源、条号与版本随每条返回；公益API为第三方文本，未提供的施行日期及修订关系仍未知。接口失败只影响缺项，已有内置原文可继续分析；引用现行或历史义务时仍须核对适用时间。",
        }

    def legal_search(self, args, attempt):
        self.current()
        if args.jurisdiction not in self.service.legal.allowed_countries(self.case["template"], self.stage):
            fail(422, "country_mismatch", "该法域不在当前业务允许范围内")
        attempt.check()
        result = self.service.legal.search(
            self.owner,
            args.query,
            self.case["id"],
            self.case["template"],
            [args.jurisdiction],
            args.limit,
            as_of=args.as_of,
            check_cancelled=attempt.check,
        )
        from app.contexts.documents.infrastructure.country_packs import CountryPacks

        packs = CountryPacks(self.service.db.data_dir)
        primary, context = {}, {}
        for candidate in result["items"]:
            attempt.check()
            record = candidate.get("metadata", {}).get("source_record_id")
            original = packs.read(args.jurisdiction, record) if record else None
            if original:
                original["id"] = candidate["id"]
                # Completing the text must not erase how this unit was found.
                # Source/version fields still come from the original record.
                original["metadata"].update(
                    {
                        key: candidate["metadata"][key]
                        for key in (
                            "semantic_document_score",
                            "semantic_clause_match",
                            "retrieval_date_state",
                            "exact_match",
                            "retrieval_relation",
                        )
                        if key in candidate.get("metadata", {})
                    }
                )
            primary[candidate["id"]] = original or candidate
            related_units = (
                [
                    *packs.referenced_articles(args.jurisdiction, record, limit=3),
                    *packs.neighboring_records(args.jurisdiction, record),
                ]
                if record
                else []
            )
            for related in related_units:
                related = self.identify_law(related)
                context[related["id"]] = related
        # Preserve the search order before adding context. A neighbour of the
        # first hit must not displace a better-ranked primary provision, or
        # overwrite its provenance when it is reached again through a reference.
        items = {**primary, **{key: value for key, value in context.items() if key not in primary}}
        return {**result, "items": list(items.values()), "as_of": args.as_of}

    @staticmethod
    def identify_law(citation):
        from app.contexts.legal_analysis.application.analysis_service import digest

        return {
            **citation,
            "id": f"{citation['document_id']}:{citation['document_version']}:{digest(citation['chunk_id'])[:16]}",
        }

    def law_versions(self, args, attempt):
        from app.contexts.documents.infrastructure.country_packs import CountryPacks

        self.current()
        if args.jurisdiction not in self.service.legal.allowed_countries(self.case["template"], self.stage):
            fail(422, "country_mismatch", "该法域不在当前业务允许范围内")
        attempt.check()
        item = CountryPacks(self.service.db.data_dir).read(args.jurisdiction, args.record_id)
        if not item:
            fail(404, "not_found", "未找到该法域的法规版本记录")
        meta = item.get("metadata", {})
        return {
            "record_id": args.record_id,
            "title": item["title"],
            "source_url": item.get("source_url"),
            "document_version": item["document_version"],
            "dates": {
                key: meta.get(key)
                for key in (
                    "published_at",
                    "declared_version_date",
                    "effective_from",
                    "effective_to",
                    "superseded_at",
                )
            },
            "relations": meta.get("version_relations", []),
            "superseded_by": meta.get("superseded_by"),
            "date_evidence": meta.get("date_evidence", []),
            "notice": "仅返回已有来源记录；空值表示尚未查明，资料入库不证明当前有效。",
        }

    def read_law(self, args, attempt):
        from app.contexts.documents.infrastructure.country_packs import CountryPacks
        from app.contexts.legal_analysis.application.analysis_service import digest

        self.current()
        allowed = self.service.legal.allowed_countries(self.case["template"], self.stage)
        if args.jurisdiction not in allowed:
            fail(422, "country_mismatch", "该法域不在当前业务允许范围内")
        attempt.check()
        if args.url:
            from app.contexts.legal_analysis.infrastructure.official_evidence import (
                citations_from_text,
                fetch_official,
                official_url,
            )

            if not official_url(args.url, args.jurisdiction):
                fail(422, "official_source_required", "请使用指定法域的官方HTTPS原文地址")
            try:
                record = fetch_official(
                    self.service.db.data_dir,
                    args.url,
                    args.jurisdiction,
                    deadline=monotonic() + 20,
                    check_cancelled=attempt.check,
                )
            except ValueError as error:
                # The source reader emits bounded public-source diagnostics,
                # never provider bodies, credentials or private account data.
                raise ToolFailure("official_source_unavailable", str(error)[:300]) from None
            items = citations_from_text(record, args.query, 5)
            if not items:
                raise ToolFailure(
                    "source_section_unavailable",
                    "已取得官方页面，但未识别出可完整引用的条文或章节；不能以搜索摘要代替原文。",
                )
            for citation in items:
                citation["id"] = (
                    f"{citation['document_id']}:{citation['document_version']}:{digest(citation['chunk_id'])[:16]}"
                )
            attempt.check()
            return {
                "items": items,
                "mode": "official_web",
                "warning": "已读取官方正文；相关日期的适用版本仍需核对。",
            }
        citation = CountryPacks(self.service.db.data_dir).read(args.jurisdiction, args.record_id)
        if not citation:
            fail(404, "not_found", "未找到该法域的法律原文记录，请重新检索")
        citation["id"] = (
            f"{citation['document_id']}:{citation['document_version']}:{digest(citation['chunk_id'])[:16]}"
        )
        attempt.check()
        return {
            "items": [citation],
            "mode": "local_pack",
            "warning": "原文与中文整理已区分；适用版本以来源及修法信息为准。",
        }

    def draft(self, args, attempt):
        _, stage = self.current()
        requested = bool({"draft", "legal-writing"} & set(self.payload.get("skills", []))) or re.search(
            r"(?:新建|创建|起草|撰写|编写|生成|写|拟).{0,18}(?:文稿|报告|合同|邮件|说明|方案|函|清单|报价|证明|计划)",
            self.payload["content"],
        )
        if not requested:
            fail(403, "draft_not_requested", "请先让用户明确要求创建文稿")
        if args.task_id not in {t["id"] for t in stage["tasks"]}:
            fail(422, "task_scope", "文稿必须属于当前节点任务")
        return {
            "draft": args.model_dump(exclude={"purpose", "objective_ids"}),
            "status": "pending_commit",
            "notice": "正文已准备，本轮回复成功后与消息一起保存。",
        }

    def image_pdf(self, args, attempt):
        from app.contexts.documents.application.image_pdf import prepare_pdf

        if not re.search(r"pdf|PDF|分页|长图整理", self.payload.get("content", "")):
            fail(403, "conversion_not_requested", "需要用户明确要求转换 PDF 或分页")
        if self.pending_pdfs:
            raise ToolFailure("write_limit", "每轮最多生成一个 PDF，请先完成当前文件")
        doc = self.scoped_document(args.document_id, args.version)
        if doc["owner_id"] != self.owner or doc["case_id"] != self.case["id"] or doc["role"] != "primary":
            fail(403, "image_scope", "转换只接收当前账号、本业务上传的主文件图片")
        options = args.model_dump(exclude={"document_id", "version", "purpose", "objective_ids"})
        pending = prepare_pdf(self.service, self.owner, doc, options, timeout=min(20, attempt.remaining))
        attempt.check()
        self.current()
        self.pending_pdfs.append(pending)
        return {
            "status": "pending_commit",
            "filename": pending["filename"],
            **{
                k: pending["pagination"][k]
                for k in ("page_count", "slice_count", "width", "height", "layout")
            },
            "notice": "PDF 已生成，答复提交后程序提供实际下载链接；原图保留，不进行文字识别。",
        }

    def inspect_file(self, args, attempt):
        from app.contexts.documents.application.document_files import inspect_file

        doc = self.scoped_document(args.document_id, args.version)
        result = inspect_file(
            self.service, doc, offset=args.offset, limit=args.limit, timeout=attempt.remaining
        )
        attempt.check()
        self.current()
        self.inspected_files.add((args.document_id, args.version))
        return {"document_id": doc["id"], "version": doc["version"], "filename": doc["filename"], **result}

    def file_write_requested(self):
        self.current()
        if not re.search(
            r"生成|新建|创建|起草|撰写|编写|拟|写|做|制作|整理|导出|下载|保存|另存|修改|改|编辑|填|替换|转换|转成|转为|排版|补充|create|edit|draft|export|save|generate|fill|replace|convert",
            self.payload.get("content", ""),
            re.I,
        ):
            fail(403, "file_write_not_requested", "需要用户提出生成或修改文件的要求")
        if len(self.pending_files) >= 3:
            raise ToolFailure("write_limit", "每轮最多生成3份文件，请先完成本轮文件")

    def prepare_file(self, filename, options, attempt, *, source=None):
        from app.contexts.documents.application.document_files import prepare_file

        self.file_write_requested()
        pending = prepare_file(
            self.service, self.owner, filename, options, source=source, timeout=attempt.remaining
        )
        attempt.check()
        self.current()
        self.pending_files.append(pending)
        return {
            "status": "pending_commit",
            "filename": pending["filename"],
            "size": pending["size"],
            "validation": pending["validation"],
            "notice": "文件已生成并重开检查；答复提交后程序提供实际下载链接。未进行视觉排版检查，原文件保留。",
        }

    def create_file(self, args, attempt):
        return self.prepare_file(args.filename, {"format": args.format, "content": args.content}, attempt)

    def checked_file_links(self, output):
        """Drop nonexistent/foreign download IDs; leave public website URLs alone."""
        pattern = re.compile(r"\[[^\]\n]{1,200}\]\(/api/tea/documents/([^/\s)]+)/download\)")
        allowed = {p["id"] for p in (*self.pending_files, *self.pending_pdfs)}
        with closing(self.service.db.connect()) as conn:
            for document_id in set(pattern.findall(output)) - allowed:
                try:
                    doc = self.service.read_document(conn, self.owner, document_id)
                    if doc["case_id"] in (None, self.case["id"]):
                        allowed.add(document_id)
                except AppError as error:
                    if error.status not in (403, 404):
                        raise
        return pattern.sub(lambda match: match[0] if match[1] in allowed else "", output)

    def edit_file(self, args, attempt):
        from app.contexts.documents.application.document_files import file_format

        doc = self.scoped_document(args.document_id, args.version)
        if doc["owner_id"] != self.owner or doc["case_id"] != self.case["id"]:
            fail(403, "file_scope", "修改只接收当前账号、本业务文件；可读取的共享资料不授予修改权限")
        if (args.document_id, args.version) not in self.inspected_files:
            fail(422, "file_inspection_required", "请先调用inspect_document_file读取目标位置及原文")
        options = {
            "format": file_format(doc),
            "replacements": [c.model_dump() for c in args.replacements],
            "cells": [c.model_dump() for c in args.cells],
        }
        return self.prepare_file(args.filename, options, attempt, source=doc)

    def image(self, args, attempt):
        doc = self.scoped_document(args.document_id, args.version)
        if doc["case_id"] != self.case["id"]:
            fail(403, "image_scope", "识图仅接收当前业务资料")
        if args.image_id:
            content = self.service.library.document_image(self.owner, args.document_id, args.image_id)
            mime = "image/png"  # Existing preview adapter owns bounded page rendering.
        else:
            mime = doc["mime_type"]
            if (
                mime not in ("image/png", "image/jpeg", "image/webp")
                or not 0 < doc["size"] <= 8 * 1024 * 1024
            ):
                fail(422, "image_required", "请选择不超过 8 MB 的 PNG、JPEG、WebP 图片，或已有页面图")
            path = Path(doc["storage_path"]).resolve()
            if not path.is_relative_to(self.service.db.files_dir.resolve()):
                fail(422, "image_unavailable", "图片原件路径无效")
            with path.open("rb") as stream:
                content = stream.read(8 * 1024 * 1024 + 1)
            if hashlib.sha256(content).hexdigest() != doc["content_hash"]:
                fail(409, "image_changed", "原件校验不一致")
        if len(content) > 8 * 1024 * 1024 or mime not in ("image/png", "image/jpeg", "image/webp"):
            fail(422, "image_limit", "图片超过识别限制")
        from app.contexts.documents.infrastructure.providers.vision import (
            OCRCapacityDeferred,
            _VISION_REQUEST_SLOT,
            vision_ocr_provider,
        )

        shared = vision_ocr_provider() == "sub2api"
        if shared and not _VISION_REQUEST_SLOT.acquire(
            timeout=min(5, attempt.remaining), data_root=self.service.db.data_dir
        ):
            raise OCRCapacityDeferred()
        try:
            result = asyncio.run(
                gateway_tool(
                    "image_recognition",
                    {
                        "image": f"data:{mime};base64," + base64.b64encode(content).decode(),
                        "prompt": args.prompt,
                        "max_output_tokens": 2048,
                    },
                    attempt.check,
                    timeout=attempt.remaining,
                )
            )
        finally:
            if shared:
                _VISION_REQUEST_SLOT.release()
        return {
            "document_id": doc["id"],
            "document_version": doc["version"],
            "observation": result.get("output", {}).get("text", ""),
            "status": "unverified_observation",
            "notice": "仅为本轮图片观察，不改变解析、索引或审核状态。",
        }

    def web(self, args, attempt):
        self.current()
        if any(not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}", host) for host in args.domains):
            fail(422, "invalid_domain", "搜索域名格式不正确")
        result = asyncio.run(
            gateway_tool(
                "web_search",
                args.model_dump(exclude_none=True, exclude={"purpose", "objective_ids"})
                | {"response_length": "short"},
                attempt.check,
                timeout=attempt.remaining,
            )
        )
        output = result.get("output", {})
        sources = []
        for item in output.get("sources", [])[: args.max_results]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            try:
                url = urlsplit(item["url"])
            except ValueError:
                continue
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                continue
            sources.append(
                {
                    "title": str(item.get("title", ""))[:200],
                    "url": item["url"][:2000],
                    "excerpt": str(item.get("content") or item.get("snippet") or item.get("excerpt") or "")[
                        :1600
                    ],
                }
            )
        return {
            "sources": sources,
            "summary": str(output.get("summary", ""))[:1200],
            "retrieved_at": now(),
            "status": "completed",
            "notice": "这些网页信息可直接用于业务答复，同时给出网站名称及完整网址，方便用户打开查询。无需等待审核；搜索摘要与网页全文保留区别，尚未打开页面时不声称已阅读全文。",
        }

    def recall(self, args, attempt):
        return self.service.agent_memory.recall(
            self.owner,
            self.case["id"],
            self.stage["id"],
            self.payload.get("conversation_id"),
            args.query,
            limit=min(args.limit, 10),
        )

    def remember(self, args, attempt):
        self.current()
        if (
            not re.search(
                r"记住|记下|保存.{0,5}记忆|以后.{0,6}(?:都|请)|remember", self.payload["content"], re.I
            )
            or args.source_quote not in self.payload["content"]
            or args.content not in args.source_quote
        ):
            fail(403, "memory_not_requested", "记忆必须来自用户当前明确要求保存的原话")
        return {
            "memory": {
                "content": args.content,
                "kind": args.kind,
                "conversation_id": self.payload.get("conversation_id"),
            },
            "status": "pending_commit",
        }

    def plan(self, args, attempt):
        if args.current > len(args.steps) or any(not step.strip() or len(step) > 160 for step in args.steps):
            fail(422, "invalid_plan", "计划步骤格式不正确")
        return {"plan": args.model_dump()}

    def catalog(self):
        return {
            "tools": [
                {
                    "name": t.spec.name,
                    "label": t.spec.label,
                    "description": t.spec.description,
                    "parallel": t.spec.concurrency_safe,
                    "capability": t.spec.capability,
                    "entry_condition": t.spec.entry_condition,
                    "result_contract": t.spec.result_contract,
                    "cost_class": t.spec.cost_class,
                    "timeout_seconds": t.spec.timeout_seconds,
                }
                for t in self.registry.values()
            ],
            "skills": skill_catalog(),
            "combinations": COMPOSITION_HINTS,
        }

    def receipt(self, args, attempt):
        self.current()
        with closing(self.service.db.connect()) as conn:
            row = conn.execute(
                "SELECT t.name,t.status,t.result FROM agent_tool_receipts t JOIN agent_runs r ON r.id=t.run_id WHERE r.id=? AND r.owner_id=? AND r.case_id=? AND t.call_id=?",
                (self.run_id, self.owner, self.case["id"], args.call_id),
            ).fetchone()
        if not row or row["result"] is None:
            fail(404, "tool_result_unavailable", "没有找到已完成的工具收据")
        text = row["result"]
        return {
            "call_id": args.call_id,
            "tool": row["name"],
            "status": row["status"],
            "text": text[args.offset : args.offset + args.limit],
            "offset": args.offset,
            "total": len(text),
            "next_offset": args.offset + args.limit if args.offset + args.limit < len(text) else None,
        }
