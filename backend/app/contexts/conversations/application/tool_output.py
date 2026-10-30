"""Select bounded, verbatim business fields from the result consumed by the agent."""

from urllib.parse import urlsplit

from app.contexts.conversations.application.contracts import ToolOutputItem, ToolOutputPreview
from app.shared.domain.values import dump


def bounded_preview(preview):
    """Snapshots repeat earlier results, so bound serialized bytes as well as fields."""
    result = preview.model_dump(exclude_none=True)
    while len(dump(result).encode()) > 4096:
        result["truncated"] = True
        if len(result["items"]) > 1:
            result["items"].pop()
        elif result["text"]:
            result["text"] = result["text"][: len(result["text"]) // 2]
        elif result["items"] and result["items"][0]["text"]:
            item = result["items"][0]
            item["text"] = item["text"][: len(item["text"]) // 2]
        else:
            # Keep the original notice, including pending-save and evidence caveats.
            result["items"].clear()
    return result


def tool_output_preview(name, value):
    if not isinstance(value, dict):
        return None
    preview = ToolOutputPreview()

    def clipped(value, limit):
        if not isinstance(value, str):
            return ""
        preview.truncated |= len(value) > limit
        return value[:limit]

    def items(values, label="title", text="description", status=False, source=False):
        if not isinstance(values, list):
            return
        preview.truncated |= len(values) > 8
        for row in values[:8]:
            if not isinstance(row, dict):
                continue
            item = ToolOutputItem(label=clipped(row.get(label), 200), text=clipped(row.get(text), 1200))
            if status and isinstance(row.get("status"), str):
                item.status = clipped(row["status"], 64)
            if source and isinstance(url := row.get("url") or row.get("source_url"), str):
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme in {"http", "https"}
                        and parsed.hostname
                        and not parsed.username
                        and not parsed.password
                        and len(url) <= 2000
                    ):
                        item.url = url
                except ValueError:
                    pass
            if item.label or item.text:
                preview.items.append(item)

    if name == "update_business_card":
        items(value.get("card_updates"), text="title")
    elif name == "guide_business":
        items(value.get("tasks"), status=True)
    elif name == "check_materials":
        items(value.get("checks"), label="label", text="reason", status=True)
    elif name == "get_handoff":
        stages = value.get("stages", [])
        tasks = [task for stage in stages if isinstance(stage, dict) for task in stage.get("tasks", [])]
        items(tasks, status=True)
    elif name in {"search_documents", "search_legal_evidence", "lookup_chinese_law", "read_law"}:
        items(value.get("items"), text="excerpt", source=True)
    elif name == "list_documents":
        items(value.get("items"))
    elif name == "read_document":
        chunks = value.get("chunks") or value.get("elements") or []
        items(chunks, text="text")
    elif name == "analyze_legal":
        result = value.get("result") or {}
        preview.text = clipped(result.get("output"), 4000)
        items(result.get("issues"), label="question", text="conclusion", status=True)
    elif name == "draft_document":
        draft = value.get("draft") or {}
        preview.text = clipped(draft.get("content"), 4000)
    elif name == "inspect_image":
        preview.text = clipped(value.get("observation"), 4000)
    elif name == "web_search":
        preview.text = clipped(value.get("summary"), 4000)
        items(value.get("sources"), text="excerpt", source=True)
    else:
        # Memory, discovery, private model turns, plans and receipts stay internal.
        return None
    preview.notice = clipped(value.get("warning") or value.get("notice"), 400)
    return bounded_preview(preview) if preview.text or preview.items or preview.notice else None
