import { type DocumentDetail, type DocumentPart, type Source } from "./types";

// Read-only UI fixtures. These records are never uploaded, reviewed or indexed.
function documentSample(
  id: string,
  title: string,
  sections: { title: string; text: string; kind?: string }[],
): DocumentDetail {
  const elements: DocumentPart[] = sections.map((section, index) => ({
    element_key: `${id}-${index}`,
    kind: section.kind ?? "paragraph",
    section_path: [section.title],
    content_text: section.text,
    locator: { paragraph_index: index },
  }));
  const chunks = elements.map((element) => ({
    ...element,
    text: element.content_text,
  }));
  return {
    document: {
      id,
      title,
      filename: `${title}.txt`,
      role: "primary",
      status: "ready",
      mime_type: "text/plain",
      size: new TextEncoder().encode(sections.map((s) => s.text).join("\n"))
        .length,
      version: 1,
      case_id: null,
      parent_id: null,
      tags: ["开发样例"],
      created_at: "2026-10-11T00:00:00Z",
      source_url: null,
      error: null,
      chunk_count: chunks.length,
      metadata: {
        kind: "demo",
        jurisdiction: "",
        language: "zh",
        published_at: null,
        effective_from: null,
        effective_to: null,
        review_status: "draft",
      },
    },
    elements,
    chunks,
    element_total: elements.length,
    chunk_total: chunks.length,
    warnings: [],
  };
}

export const librarySamples: DocumentDetail[] = [
  documentSample("sample-import", "茶叶进口登记 · 示例", [
    { title: "资料说明", text: "开发样例，不是真实法规。" },
    { title: "登记信息", kind: "heading", text: "登记信息" },
    {
      title: "登记信息",
      text: "申请资料通常包含主体信息、产品名称、生产企业和原产地，具体要求以正式资料为准。",
    },
    { title: "材料清单", kind: "heading", text: "材料清单" },
    { title: "材料清单", kind: "list", text: "主体信息表\n产品说明\n标签样稿" },
  ]),
  documentSample("sample-label", "产品标签规范 · 示例", [
    { title: "资料说明", text: "开发样例，用于展示排版。" },
    { title: "标签信息", kind: "heading", text: "标签信息" },
    {
      title: "标签信息",
      text: "示例产品为茉莉花茶，规格为 100 g。企业、批次与日期由实际资料提供。",
    },
    {
      title: "核对清单",
      kind: "list",
      text: "产品名称\n配料信息\n净含量\n生产主体\n日期与批次",
    },
  ]),
];

export const sourceSamples: Source[] = [
  {
    id: "sample-source-import",
    name: "进口登记资料 · 开发样例",
    url: "https://example.org/import",
    enabled: false,
    interval_hours: 24,
    tags: ["开发样例"],
    last_fetched_at: null,
    status: "idle",
    last_error: null,
  },
  {
    id: "sample-source-label",
    name: "产品标签资料 · 开发样例",
    url: "https://example.org/labels",
    enabled: false,
    interval_hours: 72,
    tags: ["开发样例"],
    last_fetched_at: null,
    status: "idle",
    last_error: null,
  },
];
