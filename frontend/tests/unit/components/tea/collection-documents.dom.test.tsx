import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { DocumentPreview } from "@/components/tea/document-preview";
import { type Document, type DocumentDetail } from "@/core/tea/types";

const document: Document = {
  id: "law",
  title: "进口条例",
  filename: "law.pdf",
  role: "primary",
  status: "ready",
  mime_type: "application/pdf",
  size: 1000,
  version: 2,
  case_id: null,
  parent_id: null,
  tags: ["内部标签"],
  created_at: "2026-10-12T00:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 5,
};
const sample: DocumentDetail = {
  document,
  elements: [],
  chunks: [],
  element_total: 0,
  chunk_total: 0,
  warnings: [],
  preview: {
    summary: "介绍进口申请的材料和步骤。",
    total: 4,
    offset: 0,
    blocks: [
      { kind: "heading", text: "申请条件" },
      {
        kind: "paragraph",
        text: "申请人应提供 **产品说明**。\n\n| 材料 | 数量 |\n| --- | --- |\n| 登记表 | 1 |\n\n![不可信图片](https://example.com/tracker.png)",
      },
      {
        kind: "table",
        cells: [
          { row: 1, text: "主体信息", col_span: 2, row_span: 1 },
          { row: 2, text: "企业名称", col_span: 1, row_span: 1 },
          { row: 2, text: "茶叶公司", col_span: 1, row_span: 1 },
        ],
      },
      {
        kind: "image",
        image_id: "native-0",
        width: 1200,
        height: 800,
        caption: "标签示意图",
        summary: "图中列出名称、规格与产地。",
      },
    ],
  },
};
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("content-focused document preview", () => {
  it("renders parsed headings, Markdown, tables and authenticated images with their summaries", () => {
    render(<DocumentPreview document={document} sample={sample} />);
    expect(screen.getByRole("heading", { name: "申请条件" })).toBeTruthy();
    expect(
      screen.getByText("产品说明").closest('[data-streamdown="strong"]'),
    ).toBeTruthy();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(
      screen.getByRole("cell", { name: "主体信息" }).getAttribute("colspan"),
    ).toBe("2");
    const image = screen.getByRole("img", { name: "标签示意图" });
    expect(image.getAttribute("src")).toBe(
      "/api/tea/documents/law/images/native-0",
    );
    expect(image.getAttribute("width")).toBe("1200");
    expect(screen.getAllByRole("img")).toHaveLength(1);
    expect(screen.getByText("图中列出名称、规格与产地。")).toBeTruthy();
    expect(screen.getByText("介绍进口申请的材料和步骤。")).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByText("内部标签")).toBeNull();
    fireEvent.error(image);
    expect(screen.getByText("图片读取失败")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试图片" }));
    expect(screen.getByRole("img", { name: "标签示意图" })).toBeTruthy();
  });

  it("does not execute raw HTML or unsafe links from parsed text", () => {
    render(
      <DocumentPreview
        document={document}
        sample={{
          ...sample,
          preview: {
            ...sample.preview!,
            blocks: [
              {
                kind: "paragraph",
                text: "<script>window.pwned=true</script>\n\n[恶意链接](javascript:alert(1))\n\n[原文](https://example.org/law)",
              },
            ],
          },
        }}
      />,
    );
    expect(globalThis.document.querySelector("script")).toBeNull();
    expect(screen.queryByRole("link", { name: "恶意链接" })).toBeNull();
    expect(
      screen.getByRole("link", { name: "原文" }).getAttribute("href"),
    ).toBe("https://example.org/law");
  });
});
