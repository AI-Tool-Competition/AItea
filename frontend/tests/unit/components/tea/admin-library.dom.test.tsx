import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

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
describe("admin library", () => {
  it("browses sample structure and related files without sending library requests", async () => {
    const fetch = rs.spyOn(globalThis, "fetch");
    render(
      <AdminLibrary
        cases={[]}
        revision={0}
        onUpload={() => undefined}
        samples
      />,
    );
    fireEvent.click(screen.getByText("茶叶进口登记 · 主资料样例"));
    expect(screen.getByText(/不是真实法规/, { selector: "p" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "下载原件" })).toBeNull();
    expect(screen.queryByText(/资料分类与适用性/)).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "二级资料" }));
    fireEvent.click(await screen.findByText("名词定义 · 引用资料样例"));
    expect(screen.getByRole("heading", { name: "关联原件" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看所属一级资料" }));
    expect(
      screen.getByRole("heading", { name: "茶叶进口登记 · 主资料样例" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回资料目录" }));
    fireEvent.change(
      screen.getByRole("textbox", { name: "搜索一级资料名称" }),
      { target: { value: "标签" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(screen.queryByText("茶叶进口登记 · 主资料样例")).toBeNull();
    expect(screen.getByText("产品标签规范 · 主资料样例")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "上传资料" })).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps source samples read-only and restores the real source list on exit", async () => {
    const fetch = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 })));
    const view = render(<Sources key="samples" compact samples />);
    expect(screen.getByText("进口登记资料 · 开发样例")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "立即抓取" })).toBeNull();
    expect(screen.queryByRole("button", { name: "添加来源" })).toBeNull();
    expect(
      screen
        .getAllByRole<HTMLInputElement>("checkbox")
        .every((input) => input.disabled),
    ).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
    view.rerender(<Sources key="real" compact />);
    await screen.findByText("暂无来源");
    expect(screen.queryByText("进口登记资料 · 开发样例")).toBeNull();
    expect(screen.getByRole("button", { name: "添加来源" })).toBeTruthy();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("refreshes the open secondary list after an attachment upload without losing the selected view", async () => {
    let uploaded = false;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const path =
        typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
      const payload = path.includes("parent_id=")
        ? {
            items: uploaded
              ? [
                  {
                    ...parent,
                    id: "secondary",
                    parent_id: parent.id,
                    role: "secondary",
                    status: "stored",
                    title: "新关联附件",
                  },
                ]
              : [],
            total: uploaded ? 1 : 0,
          }
        : path.includes("/documents/primary")
          ? {
              document: parent,
              elements: [],
              chunks: [],
              element_total: 0,
              chunk_total: 0,
              warnings: [],
            }
          : { items: [parent], total: 1 };
      return new Response(JSON.stringify(payload), { status: 200 });
    });
    const props = { cases: [], onUpload: () => undefined };
    const view = render(<AdminLibrary {...props} revision={0} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "查看拆解" })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "查看拆解" }));
    fireEvent.click(screen.getByRole("tab", { name: "二级资料" }));
    await waitFor(() =>
      expect(screen.getByText(/尚未添加二级资料/)).toBeTruthy(),
    );
    uploaded = true;
    view.rerender(<AdminLibrary {...props} revision={1} />);
    await waitFor(() => expect(screen.getByText("新关联附件")).toBeTruthy());
    expect(
      screen
        .getByRole("tab", { name: "二级资料" })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });
});
