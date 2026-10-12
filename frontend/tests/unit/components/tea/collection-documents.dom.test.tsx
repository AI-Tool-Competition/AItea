import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AdminLibrary } from "@/components/tea/admin-library";
import { type Document } from "@/core/tea/types";

const current: Document = {
  id: "law-v2",
  title: "示例法规",
  filename: "law.html",
  role: "primary",
  legal_level: "primary",
  status: "ready",
  mime_type: "text/html",
  size: 200,
  version: 2,
  case_id: null,
  parent_id: null,
  tags: ["ID"],
  created_at: "2026-10-12T00:00:00Z",
  source_url: "https://example.com/law",
  error: null,
  chunk_count: 1,
  family_id: "family",
  classifications: [
    { collection: "ID", level: "primary", tags: ["原标签"], revision: 3 },
  ],
};
const old = { ...current, id: "law-v1", version: 1 };
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}
function pathOf(url: RequestInfo | URL) {
  return typeof url === "string"
    ? url
    : url instanceof URL
      ? url.href
      : url.url;
}
function detail(document: Document) {
  return {
    document,
    elements: [
      {
        content_text: `第 ${document.version} 版正文`,
        locator: { line_start: 1 },
      },
    ],
    chunks: [],
    element_total: 1,
    chunk_total: 0,
    warnings: [],
  };
}
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("document version and relationship operations", () => {
  it("opens an immutable old version without replacing the latest document", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const path = pathOf(url);
      if (path.includes("/versions"))
        return response({ items: [current, old], total: 2 });
      if (path.includes("/documents/law-v1")) return response(detail(old));
      if (path.includes("/documents/law-v2")) return response(detail(current));
      return response({ items: [current], total: 1 });
    });
    render(<AdminLibrary cases={[]} revision={0} onUpload={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看拆解" }));
    fireEvent.click(screen.getByRole("tab", { name: "版本" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看 v1" }));
    expect(
      await screen.findByText("第 1 版正文", { selector: "p" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "下载原件" }).getAttribute("href"),
    ).toBe("/api/tea/documents/law-v1/download");
    fireEvent.click(screen.getByRole("button", { name: "返回资料目录" }));
    expect(await screen.findByText(/一级资料 · v2/)).toBeTruthy();
  });

  it("shows parsed secondary laws separately from raw attachments and pins incoming relations to versions", async () => {
    const secondary: Document = {
      ...current,
      legal_level: "secondary",
      classifications: [
        { collection: "ID", level: "secondary", tags: [], revision: 1 },
      ],
    };
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const path = pathOf(url);
      if (path.includes("/relations"))
        return response({
          items: [
            {
              id: "rel",
              collection: "ID",
              relation: "cites",
              source_document_id: "parent-v3",
              target_document_id: secondary.id,
              source_title: "主法规",
              target_title: secondary.title,
              source_version: 3,
              target_version: 2,
              locator: "第十二条",
              created_at: secondary.created_at,
            },
          ],
          total: 1,
        });
      if (path.includes("/documents/law-v2"))
        return response(detail(secondary));
      return response({ items: [secondary], total: 1 });
    });
    render(<AdminLibrary cases={[]} revision={0} onUpload={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看拆解" }));
    expect(
      await screen.findByText("第 2 版正文", { selector: "p" }),
    ).toBeTruthy();
    expect(
      screen.getByText("二级法规", { selector: ".ta-badge" }),
    ).toBeTruthy();
    expect(screen.getByRole("tab", { name: "附件" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "法规关联" }));
    expect(await screen.findByText("主法规 · v3")).toBeTruthy();
    expect(screen.getByText("被引用")).toBeTruthy();
    expect(screen.getByText(/第十二条/)).toBeTruthy();
  });

  it("preserves tags and the submitted classification revision after a conflict", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        if (options?.method === "PATCH")
          return response(
            {
              error: {
                code: "stale_revision",
                message: "分类已变化，请核对最新版本",
              },
            },
            409,
          );
        if (pathOf(url).includes("/documents/law-v2"))
          return response(detail(current));
        return response({ items: [current], total: 1 });
      });
    render(<AdminLibrary cases={[]} revision={0} onUpload={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "查看拆解" }));
    fireEvent.click(screen.getByText("分级与标签"));
    fireEvent.change(screen.getByLabelText("分类标签"), {
      target: { value: "ID, 1.3, 税务" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存分级" }));
    await screen.findByText("分类已变化，请核对最新版本");
    expect(screen.getByLabelText<HTMLInputElement>("分类标签").value).toBe(
      "ID, 1.3, 税务",
    );
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([, options]) => options?.method === "PATCH"),
      ).toBe(true),
    );
    const save = fetcher.mock.calls.find(
      ([, options]) => options?.method === "PATCH",
    )!;
    expect(JSON.parse(save[1]!.body as string)).toMatchObject({
      collection: "ID",
      level: "primary",
      tags: ["ID", "1.3", "税务"],
      expected_revision: 3,
    });
  });
});
