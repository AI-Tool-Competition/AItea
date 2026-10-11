import { type DocumentDetail, type DocumentPart, type Source } from "./types";

// Read-only UI fixtures. These records are never uploaded, reviewed or indexed.
function documentSample(
  id: string,
  title: string,
  sections: { title: string; text: string; kind?: string }[],
  parentId: string | null = null,
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
      role: parentId ? "secondary" : "primary",
      status: parentId ? "stored" : "ready",
      mime_type: "text/plain",
      size: new TextEncoder().encode(sections.map((s) => s.text).join("\n"))
        .length,
      version: 1,
      case_id: null,
      parent_id: parentId,
      tags: ["开发样例"],
      created_at: "2026-10-11T00:00:00Z",
      source_url: null,
      error: null,
      chunk_count: parentId ? 0 : chunks.length,
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
    elements: parentId ? [] : elements,
    chunks: parentId ? [] : chunks,
    element_total: parentId ? 0 : elements.length,
    chunk_total: parentId ? 0 : chunks.length,
    warnings: [],
  };
}

export const librarySamples: DocumentDetail[] = [
  documentSample("sample-import", "茶叶进口登记 · 主资料样例", [
    {
      title: "资料说明",
      text: "开发样例，用于预览资料结构与关联关系，不是真实法规。",
    },
    {
      title: "第一章 · 登记信息",
      text: "示例字段：申请主体、产品名称、生产企业、原产地。每段内容保留所属资料、版本和原文位置。",
    },
    {
      title: "第二章 · 材料清单",
      kind: "list",
      text: "主体信息表\n产品说明\n标签样稿\n关联名词定义",
    },
    {
      title: "第三章 · 关联资料",
      text: "本主资料关联《名词定义 · 引用资料样例》。展开目录中的二级资料可以查看对应文件。",
    },
  ]),
  documentSample("sample-label", "产品标签规范 · 主资料样例", [
    {
      title: "资料说明",
      text: "开发样例，用于预览段落、清单和检索片段，不构成标签合规要求。",
    },
    {
      title: "第一章 · 标签信息",
      text: "示例产品：茉莉花茶。示例规格：100 g。企业、批次与日期等内容由实际资料提供。",
    },
    {
      title: "第二章 · 核对清单",
      kind: "list",
      text: "产品名称\n配料信息\n净含量\n生产主体\n日期与批次",
    },
  ]),
  documentSample(
    "sample-definitions",
    "名词定义 · 引用资料样例",
    [
      {
        title: "名词定义",
        text: "开发样例：申请主体指提交示例登记资料的企业。",
      },
    ],
    "sample-import",
  ),
  documentSample(
    "sample-layout",
    "标签版式 · 关联文件样例",
    [
      {
        title: "标签版式",
        text: "开发样例：展示产品名称、规格与批次字段的位置。",
      },
    ],
    "sample-label",
  ),
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
