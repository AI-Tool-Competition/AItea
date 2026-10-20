import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AdminLibrary } from "@/components/tea/admin-library";
import { Sources } from "@/components/tea/sources";
import { type Document } from "@/core/tea/types";

afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});
const parent: Document = {
  id: "primary",
  title: "产品说明",
  filename: "产品.md",
  role: "primary",
  status: "ready",
  mime_type: "text/markdown",
  size: 100,
  version: 1,
  case_id: null,
  parent_id: null,
  tags: [],
  created_at: "2026-10-11T00:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 1,
};
function pathOf(input: RequestInfo | URL) {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}
describe("admin library", () => {
  it("reads sample content without management tabs or requests", async () => {
    const fetcher = rs.spyOn(globalThis, "fetch");
    render(<AdminLibrary samples />);
    fireEvent.click(screen.getByText("茶叶进口登记 · 示例"));
    expect(screen.getByText(/不是真实法规/, { selector: "p" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "下载原件" })).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByText(/资料分类与适用性/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "返回法律总库" }));
    fireEvent.change(screen.getByRole("textbox", { name: "搜索资料名称" }), {
      target: { value: "标签" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(screen.queryByText("茶叶进口登记 · 示例")).toBeNull();
    expect(screen.getByText("产品标签规范 · 示例")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "上传资料" })).toBeNull();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("keeps source samples read-only and restores the real source list on exit", async () => {
    const fetch = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(
        async () => new Response(JSON.stringify({ items: [], total: 0 })),
      );
    const view = render(<Sources key="samples" compact samples />);
    expect(screen.getByText("进口登记资料 · 开发样例")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "立即抓取" })).toBeNull();
    expect(screen.queryByRole("button", { name: "添加来源" })).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "查看 进口登记资料 · 开发样例" }),
    );
    expect(
      screen.getByRole("dialog", { name: "进口登记资料 · 开发样例" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "配置" })).toBeNull();
    expect(
      screen
        .getAllByRole<HTMLInputElement>("checkbox")
        .every((input) => input.disabled),
    ).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
    view.rerender(<Sources key="real" compact />);
    fireEvent.click(screen.getByRole("button", { name: "自定义来源" }));
    await screen.findByText("暂无来源");
    expect(screen.queryByText("进口登记资料 · 开发样例")).toBeNull();
    expect(screen.getByRole("button", { name: "添加来源" })).toBeTruthy();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("shows only the law library and opens content directly without review or hierarchy controls", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => {
        const path = pathOf(url);
        if (path.endsWith("collection-plans"))
          return new Response(
            JSON.stringify({ items: [{ country: "ID" }], total: 1 }),
          );
        return new Response(
          JSON.stringify(
            path.includes("/documents/primary")
              ? {
                  document: parent,
                  elements: [{ content_text: "登记正文" }],
                  chunks: [],
                  element_total: 1,
                  chunk_total: 0,
                  warnings: [],
                }
              : { items: [parent], total: 1 },
          ),
        );
      });
    render(<AdminLibrary ownerId="alice" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "预览 产品说明" }),
    );
    expect(await screen.findByText("登记正文")).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByText("待审核")).toBeNull();
    expect(screen.queryByText("我的资料")).toBeNull();
    expect(screen.queryByText("分级与标签")).toBeNull();
    expect(
      screen.getByRole("link", { name: "下载原件" }).getAttribute("href"),
    ).toBe("/api/tea/documents/primary/download");
    const listReads = fetcher.mock.calls.filter(([url]) =>
      pathOf(url).includes("/documents?"),
    );
    expect(
      listReads.every(
        ([url]) =>
          new URL(pathOf(url), "http://localhost").searchParams.get(
            "source_scope",
          ) === "system",
      ),
    ).toBe(true);
    expect(
      fetcher.mock.calls.every(
        ([, options]) => !options?.method || options.method === "GET",
      ),
    ).toBe(true);
  });
});
