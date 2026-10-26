from app.contexts.conversations.application.tool_output import tool_output_preview
from app.contexts.conversations.application.agent_tools import EmptyInput
from app.contexts.conversations.application.tool_runtime import ToolRun, ToolSpec
from app.contexts.conversations.application.contracts import MessageStreamEvent
from app.shared.domain.values import dump


def test_live_output_is_incremental_bounded_and_matches_the_completed_snapshot():
    events = []
    spec = ToolSpec("check_materials", "材料核对", "核对实际资料。", EmptyInput)
    run = ToolRun(
        emit=lambda event, data: events.append(MessageStreamEvent(event=event, data=data)), catalog=[spec]
    )
    trace = {"id": "agent", "label": "协作", "status": "running", "detail": ""}

    def operation(args, attempt):
        for _ in range(100):
            attempt.emit_text("实际摘要😀" * 10)
        return "done"

    run.call(spec, {}, operation, trace)
    fragments = [event.data for event in events if event.event == "tool_delta"]
    output = trace["tools"][0]["output_preview"]
    assert "".join(data["text"] for data in fragments) == output["text"]
    assert len(output["text"].encode()) <= 3500 and output["truncated"]
    assert len(dump(output).encode()) <= 4096
    assert len({data["sequence"] for data in fragments}) == len(fragments)
    assert events[-1].data["tools"][0]["status"] == "completed"


def test_long_thinking_stream_has_a_separate_budget_and_valid_bounded_frames():
    events = []
    spec = ToolSpec("agent_turn", "思考", "读取接口返回的思考。", EmptyInput)
    run = ToolRun(
        emit=lambda event, data: events.append(MessageStreamEvent(event=event, data=data)), catalog=[spec]
    )
    trace = {"id": "agent", "label": "协作", "status": "running", "detail": ""}
    text = "实际返回的思考😀" * 2000
    run.call(spec, {}, lambda _, attempt: attempt.emit_text(text), trace)
    output = trace["tools"][0]["output_preview"]
    fragments = [event.data["text"] for event in events if event.event == "tool_delta"]
    assert output["text"] == "".join(fragments) == text
    assert not output["truncated"]
    assert all(len(part) <= 3500 for part in fragments)
    # Per-call and per-run bounds apply even to many long model rounds.
    for _ in range(3):
        run.call(spec, {}, lambda _, attempt: attempt.emit_text("思考" * 20000), trace)
    assert all(len(item["output_preview"]["text"].encode()) <= 60000 for item in trace["tools"])
    assert sum(len(item["output_preview"]["text"].encode()) for item in trace["tools"]) <= 120000
    assert trace["tools"][-1]["output_preview"]["truncated"]


def test_actual_result_fields_are_preserved_without_private_tool_metadata():
    value = {
        "checks": [
            {
                "label": "实际进口主体",
                "status": "missing",
                "reason": "需要补充此信息",
                "task_id": "private-id",
            }
        ],
        "notice": "检查结果不会改变任务完成状态。",
        "private": "credential",
    }
    result = tool_output_preview("check_materials", value)
    assert result["items"] == [{"label": "实际进口主体", "text": "需要补充此信息", "status": "missing"}]
    assert result["notice"] == value["notice"]
    assert "private-id" not in str(result) and "credential" not in str(result)
    assert not result["truncated"]
    for tool in ("recall_memory", "remember_memory", "agent_turn", "load_skill", "compact_context"):
        assert tool_output_preview(tool, value) is None


def test_long_observations_are_exact_excerpts_and_marked_truncated():
    observation = "模型实际输出。" * 1000
    result = tool_output_preview("inspect_image", {"observation": observation})
    assert result["text"] and observation.startswith(result["text"])
    assert len(dump(result).encode()) <= 4096
    assert result["truncated"]


def test_source_links_and_item_sizes_are_bounded_without_rewriting_returned_text():
    result = tool_output_preview(
        "web_search",
        {
            "summary": "工具返回摘要",
            "sources": [
                {"title": "实际来源", "excerpt": "原文", "url": url}
                for url in [
                    "https://example.org/page",
                    "javascript:alert(1)",
                    "https://name:secret@example.org/",
                    "https://[invalid",
                    *(["https://example.org/"] * 8),
                ]
            ],
        },
    )
    assert result["text"] == "工具返回摘要"
    assert len(result["items"]) == 8
    assert result["items"][0]["url"] == "https://example.org/page"
    assert all("url" not in item for item in result["items"][1:4])
    assert all(len(item["text"]) <= 1200 for item in result["items"])
    assert result["truncated"]


def test_maximum_tool_history_fits_the_stream_budget_with_large_multibyte_results():
    frames = []
    run = ToolRun(
        emit=lambda event, data: frames.append(MessageStreamEvent(event=event, data=data).encode()),
        max_calls=64,
        catalog=(
            ToolSpec("web_search", "搜索公开信息", "返回公开资料。", EmptyInput),
            ToolSpec("agent_turn", "规划下一步", "整理回复。", EmptyInput),
        ),
    )
    trace = {"id": "agent", "label": "协作", "status": "running", "detail": "", "tools": []}
    value = {
        "summary": "实际结果😀" * 1000,
        "sources": [{"title": "标题" * 100, "excerpt": "实际内容😀" * 1000}] * 8,
        "notice": "实际未审核说明。",
    }
    preview = tool_output_preview("web_search", value)
    assert preview["truncated"] and preview["notice"] == value["notice"]
    assert len(dump(preview).encode()) <= 4096
    for index in range(64):
        business = index < 40
        run.call(
            run.catalog[0 if business else 1],
            {},
            lambda *_: value,
            trace,
            preview=(lambda _: preview) if business else None,
        )
        run.publish(trace)
    # Leave at least 8 MiB for deltas, the final response and protocol overhead.
    assert sum(len(frame.encode()) for frame in frames) < 24 * 1024 * 1024
    assert len(trace["tools"]) == 64


def test_pending_draft_preview_does_not_claim_saved_and_legal_conclusions_stay_verbatim():
    draft = tool_output_preview(
        "draft_document",
        {
            "draft": {"content": "# 实际草稿\n原文"},
            "status": "pending_commit",
            "notice": "正文已准备，本轮回复成功后与消息一起保存。",
        },
    )
    assert draft["text"] == "# 实际草稿\n原文"
    assert draft["notice"] == "正文已准备，本轮回复成功后与消息一起保存。"
    legal = tool_output_preview(
        "analyze_legal",
        {
            "result": {
                "output": "目前依据不足。",
                "issues": [
                    {
                        "question": "是否可以办理？",
                        "conclusion": "需要补充资质信息。",
                        "status": "undetermined",
                    }
                ],
            }
        },
    )
    assert legal["text"] == "目前依据不足。"
    assert legal["items"][0]["text"] == "需要补充资质信息。"
    assert legal["items"][0]["status"] == "undetermined"
