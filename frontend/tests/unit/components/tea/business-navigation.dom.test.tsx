import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { TeaApp } from "@/components/tea/tea-app";
import type { CaseDetail, Document } from "@/core/tea/types";

rs.mock("next/navigation", () => ({ useRouter: () => ({ push: rs.fn() }) }));
rs.mock("@/components/tea/admin", () => ({ Admin: () => null }));
rs.mock("@/components/tea/workspace", () => ({
  Workspace: () => null,
  clearChatDrafts: () => undefined,
}));
rs.mock("@/components/tea/studio", () => ({
  clearStudioDrafts: () => undefined,
}));

function business(id: string, title: string): CaseDetail {
  return {
    id,
    title,
    company: "测试茶企",
    product: "乌龙茶",
    mode_id: "direct",
    template_version: 1,
    created_at: "2026-10-12T00:00:00Z",
    updated_at: "2026-10-12T00:00:00Z",
    completed_tasks: 0,
    total_tasks: 1,
    current_stage_id: "prepare",
    revision: 1,
    stages: [
      {
        id: "prepare",
        name: "业务资料",
        actor: "企业",
        description: "",
        status: "active",
        tasks: [],
      },
      {
        id: "connect",
        name: "对接准备",
        actor: "企业",
        description: "",
        status: "locked",
        tasks: [],
      },
    ],
    template: { id: "tea", stages: [], modes: [] },
    facts: [],
    documents: [],
    artifacts: [],
    events: [],
  } as unknown as CaseDetail;
}
const first = business("first", "乌龙茶 · 印尼出口准备");
const second = business("second", "红茶 · 印尼出口准备");
const document = {
  id: "material",
  title: "出口资料",
  filename: "material.txt",
  role: "primary",
  status: "ready",
  size: 20,
  version: 1,
  created_at: "2026-10-12T00:00:00Z",
  chunk_count: 1,
} as Document;
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function urlOf(input: RequestInfo | URL) {
  return new URL(
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url,
    "http://localhost",
  );
}
function read(input: RequestInfo | URL) {
  const url = urlOf(input);
  if (url.pathname.endsWith("/bootstrap"))
    return response({
      user: {
        id: "owner",
        name: "体验用户",
        is_demo: false,
        email: "tea@example.com",
      },
      cases: [first, second],
      templates: [],
      capabilities: {},
    });
  if (url.pathname.endsWith("/cases/first")) return response(first);
  if (url.pathname.endsWith("/cases/second")) return response(second);
  if (url.pathname.endsWith("/collection-plans"))
    return response({ items: [{ country: "ID" }], total: 1 });
  const scope = url.searchParams.get("case_id");
  return response({
    items: [
      {
        ...document,
        case_id: scope,
        title: scope === "second" ? "红茶业务文件" : "乌龙茶业务文件",
      },
    ],
    total: 1,
  });
}
function group(title: string) {
  return within(screen.getByRole("button", { name: title }).closest("li")!);
}
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("business navigation", () => {
  it("opens the current business with its node conversations and one library", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      read(input),
    );
    render(<TeaApp />);
    await screen.findByRole("button", { name: "业务资料" });
    const current = group(first.title);
    expect(current.getByRole("button", { name: "对接准备" })).toBeTruthy();
    expect(current.getAllByRole("button", { name: "资料库" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "出口业务" })).toBeNull();
    expect(
      group(second.title).queryByRole("button", { name: "资料库" }),
    ).toBeNull();
  });

  it("opens the business library and defaults uploads to that business", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => read(input));
    render(<TeaApp />);
    await screen.findByRole("button", { name: first.title });
    fireEvent.click(screen.getByRole("button", { name: first.title }));
    fireEvent.click(group(first.title).getByRole("button", { name: "资料库" }));
    await screen.findByRole("button", { name: /乌龙茶业务文件/ });
    expect(screen.queryByLabelText("资料范围")).toBeNull();
    expect(screen.queryByLabelText("资料国家")).toBeNull();
    expect(
      fetcher.mock.calls.some(
        ([input]) => urlOf(input).searchParams.get("case_id") === first.id,
      ),
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "上传资料" }));
    expect(screen.getByLabelText<HTMLSelectElement>("资料归属").value).toBe(
      first.id,
    );
  });

  it("switches libraries and cancels a search from the previous business", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input, options) => {
      if (urlOf(input).pathname.endsWith("/search")) {
        signal = options?.signal;
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      return read(input);
    });
    render(<TeaApp />);
    await screen.findByRole("button", { name: first.title });
    fireEvent.click(screen.getByRole("button", { name: first.title }));
    fireEvent.click(group(first.title).getByRole("button", { name: "资料库" }));
    await screen.findByRole("button", { name: /乌龙茶业务文件/ });
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "出口规则" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await waitFor(() => expect(signal).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: second.title }));
    fireEvent.click(
      group(second.title).getByRole("button", { name: "资料库" }),
    );
    await screen.findByRole("button", { name: /红茶业务文件/ });
    expect(signal?.aborted).toBe(true);
    await act(async () =>
      finish(
        response({
          items: [
            { document_id: "old", title: "旧业务搜索结果", excerpt: "旧资料" },
          ],
        }),
      ),
    );
    expect(screen.queryByText("旧业务搜索结果")).toBeNull();
    expect(screen.queryByRole("button", { name: /乌龙茶业务文件/ })).toBeNull();
    expect(screen.getByLabelText<HTMLInputElement>("搜索资料").value).toBe("");
  });

  it("keeps the business library accessible if conversations fail to load", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      urlOf(input).pathname.endsWith("/cases/first")
        ? response({ error: { message: "读取业务失败" } }, 503)
        : read(input),
    );
    render(<TeaApp />);
    await screen.findByText("读取业务失败");
    fireEvent.click(screen.getByRole("button", { name: first.title }));
    fireEvent.click(group(first.title).getByRole("button", { name: "资料库" }));
    await screen.findByRole("button", { name: /乌龙茶业务文件/ });
  });

  it("drops the previous account's library scope after session expiration", async () => {
    let nextAccount = false;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = urlOf(input);
        if (url.pathname.endsWith("/auth/demo")) {
          nextAccount = true;
          return response({});
        }
        if (nextAccount && url.pathname.endsWith("/bootstrap"))
          return response({
            user: { id: "next-owner", name: "另一账号", is_demo: true },
            cases: [second],
            templates: [],
            capabilities: {},
          });
        return read(input);
      });
    render(<TeaApp />);
    await screen.findByRole("button", { name: first.title });
    fireEvent.click(group(first.title).getByRole("button", { name: "资料库" }));
    await screen.findByRole("button", { name: /乌龙茶业务文件/ });
    await act(async () => {
      window.dispatchEvent(new Event("tea:session-expired"));
    });
    fireEvent.click(
      await screen.findByRole("button", { name: /进入演示工作台/ }),
    );
    await screen.findByRole("button", { name: second.title });
    await waitFor(() =>
      expect(
        group(second.title).getByRole("button", { name: "业务资料" }),
      ).toBeTruthy(),
    );
    const newAccountReads = fetcher.mock.calls.slice(
      fetcher.mock.calls.findIndex(([input]) =>
        urlOf(input).pathname.endsWith("/auth/demo"),
      ),
    );
    expect(
      newAccountReads.some(
        ([input]) => urlOf(input).searchParams.get("case_id") === first.id,
      ),
    ).toBe(false);
    expect(screen.queryByRole("button", { name: /乌龙茶业务文件/ })).toBeNull();
  });

  it("distinguishes the public library from the collapsed business shortcut", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      read(input),
    );
    render(<TeaApp />);
    await screen.findByRole("button", { name: first.title });
    fireEvent.keyDown(screen.getByRole("button", { name: "账号与管理" }), {
      key: "ArrowDown",
    });
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "公共资料库" }),
    );
    await screen.findByRole("heading", { name: "公共资料库" });
    fireEvent.click(screen.getAllByRole("button", { name: "切换侧栏" })[0]!);
    const shortcut = screen.getByRole("button", { name: "资料库" });
    expect(shortcut.getAttribute("data-active")).toBe("false");
    fireEvent.click(shortcut);
    await screen.findByRole("button", { name: /乌龙茶业务文件/ });
    expect(shortcut.getAttribute("data-active")).toBe("true");
  });
});
