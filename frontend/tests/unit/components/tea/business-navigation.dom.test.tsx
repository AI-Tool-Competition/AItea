import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { TeaApp } from "@/components/tea/tea-app";
import type { CaseDetail, Document } from "@/core/tea/types";

rs.mock("next/navigation", () => ({ useRouter: () => ({ push: rs.fn() }) }));
rs.mock("@/components/tea/admin", () => ({ Admin: () => null }));
rs.mock("@/components/tea/workspace", () => ({
  Workspace: ({ conversationId }: { conversationId: string }) => (
    <output data-testid="selected-conversation">{conversationId}</output>
  ),
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
  if (url.pathname.endsWith("/library/countries"))
    return response({ items: [{ country: "ID" }], total: 1 });
  if (url.pathname.endsWith("/conversations"))
    return response({
      items: [
        {
          id: "chat-one",
          stage_id: "prepare",
          title: "产品包装讨论",
          kind: "chat",
        },
      ],
      total: 1,
    });
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
afterEach(() => {
  cleanup();
  rs.useRealTimers();
  rs.restoreAllMocks();
});

describe("workspace startup", () => {
  it("stops a stalled bootstrap, retries, and ignores its late response", async () => {
    rs.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let finish!: (value: Response) => void;
    let signal: AbortSignal | undefined;
    rs.spyOn(globalThis, "fetch")
      .mockImplementationOnce(
        (_input, options) =>
          new Promise<Response>((resolve) => {
            finish = resolve;
            signal = options?.signal ?? undefined;
          }),
      )
      .mockImplementation(async (input) => read(input));
    render(<TeaApp />);
    expect(screen.getByText("正在打开你的工作台…")).toBeTruthy();
    await act(async () => {
      await rs.advanceTimersByTimeAsync(15_000);
    });
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("加载工作台超时");
    expect(screen.queryByText("正在打开你的工作台…")).toBeNull();
    rs.useRealTimers();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("button", { name: "业务资料" });
    await act(async () => {
      finish(response({ user: { id: "stale" }, cases: [], templates: [] }));
    });
    expect(screen.getByRole("button", { name: "业务资料" })).toBeTruthy();
  });

  it("clears the startup deadline after showing the signed-out welcome", async () => {
    rs.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    rs.spyOn(globalThis, "fetch").mockResolvedValue(response({}, 401));
    render(<TeaApp />);
    await act(async () => {
      await rs.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("button", { name: "进入演示工作台" })).toBeTruthy();
    await act(async () => {
      await rs.advanceTimersByTimeAsync(20_000);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("正在打开你的工作台…")).toBeNull();
  });
});

describe("business node navigation", () => {
  it("focuses the current folder, reveals other nodes in order, and collapses independently", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      read(input),
    );
    render(<TeaApp />);
    await screen.findByRole("button", { name: "业务资料" });
    expect(screen.getByRole("button", { name: "项目管理" })).toBeTruthy();
    const project = screen.getByRole("button", {
      name: `项目：${first.title}`,
    });
    expect(project.getAttribute("aria-expanded")).toBe("true");
    expect(
      screen
        .getByRole("button", { name: "引导会话" })
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(screen.queryByRole("button", { name: "对接准备" })).toBeNull();
    expect(screen.getByRole("button", { name: "资料库" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "展开其他节点" }));
    expect(screen.getByRole("button", { name: "对接准备" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "对接准备" }));
    expect(
      screen
        .getByRole("button", { name: "业务资料" })
        .getAttribute("aria-expanded"),
    ).toBe("false");
    expect(
      screen
        .getByRole("button", { name: "对接准备" })
        .getAttribute("aria-expanded"),
    ).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "业务资料" }));
    expect(
      screen
        .getByRole("button", { name: "对接准备" })
        .getAttribute("aria-expanded"),
    ).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "业务资料" }));
    expect(screen.queryByRole("button", { name: "引导会话" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "业务资料" }));
    expect(screen.getByRole("button", { name: "引导会话" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "对接准备" }));
    fireEvent.click(screen.getByRole("button", { name: "收起其他节点" }));
    expect(screen.queryByRole("button", { name: "对接准备" })).toBeNull();
    expect(
      screen
        .getByRole("button", { name: "业务资料" })
        .getAttribute("aria-expanded"),
    ).toBe("true");
    fireEvent.click(project);
    expect(screen.queryByRole("button", { name: "业务资料" })).toBeNull();
    fireEvent.click(project);
    expect(screen.getByRole("button", { name: "业务资料" })).toBeTruthy();
  });

  it("opens the shared and personal library without restricting it to the active business", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => read(input));
    render(<TeaApp />);
    fireEvent.click(await screen.findByRole("button", { name: "资料库" }));
    await screen.findByRole("heading", { name: "资料库" });
    expect(
      screen
        .getByRole("button", { name: "资料库" })
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(
      screen
        .getByRole("button", { name: "法规总库" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(screen.getByRole("button", { name: "上传资料" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "管理后台" })).toBeNull();
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([input]) => {
          const url = urlOf(input);
          return (
            url.pathname.endsWith("/documents") &&
            url.searchParams.get("source_scope") === "system" &&
            !url.searchParams.has("case_id")
          );
        }),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "我的资料" }));
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([input]) => {
          const url = urlOf(input);
          return (
            url.pathname.endsWith("/documents") &&
            url.searchParams.get("source_scope") === "personal" &&
            !url.searchParams.has("case_id")
          );
        }),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "引导会话" }));
    expect(screen.getByTestId("selected-conversation").textContent).toBe(
      "guide",
    );
    expect(
      screen
        .getByRole("button", { name: `项目：${first.title}` })
        .getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("switches business from the dropdown and leaves new business last", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      read(input),
    );
    render(<TeaApp />);
    const selector = await screen.findByRole("button", { name: "切换业务" });
    expect(
      screen
        .getByRole("button", { name: `项目：${first.title}` })
        .getAttribute("aria-expanded"),
    ).toBe("true");
    fireEvent.keyDown(selector, { key: "ArrowDown" });
    await screen.findByRole("menuitem", { name: second.title });
    expect(screen.getAllByRole("menuitem").at(-1)?.textContent).toContain(
      "新建项目",
    );
    fireEvent.click(screen.getByRole("menuitem", { name: second.title }));
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: `项目：${second.title}` })
          .getAttribute("aria-expanded"),
      ).toBe("true"),
    );
    expect(screen.getByTestId("selected-conversation").textContent).toBe(
      "guide",
    );
  });

  it("opens real chats and returns to the same node guide", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      read(input),
    );
    render(<TeaApp />);
    fireEvent.click(
      await screen.findByRole("button", { name: "产品包装讨论" }),
    );
    expect(screen.getByTestId("selected-conversation").textContent).toBe(
      "chat-one",
    );
    const folder = screen.getByRole("button", { name: "业务资料" });
    fireEvent.click(folder);
    expect(folder.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "产品包装讨论" })).toBeNull();
    expect(screen.getByTestId("selected-conversation").textContent).toBe(
      "chat-one",
    );
    fireEvent.click(folder);
    expect(folder.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "引导会话" }));
    expect(screen.getByTestId("selected-conversation").textContent).toBe(
      "guide",
    );
  });

  it("expands long chat lists and keeps the selected older chat visible after collapsing", async () => {
    const chats = Array.from({ length: 8 }, (_, index) => ({
      id: `chat-${index + 1}`,
      stage_id: "prepare",
      title: `业务讨论 ${index + 1}`,
      kind: "chat",
      updated_at: `2026-10-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
    }));
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      urlOf(input).pathname.endsWith("/conversations")
        ? response({ items: chats, total: chats.length })
        : read(input),
    );
    render(<TeaApp />);
    await screen.findByRole("button", { name: "业务讨论 8" });
    expect(screen.getByRole("button", { name: "业务讨论 5" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "业务讨论 4" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开业务资料的会话" }));
    fireEvent.click(screen.getByRole("button", { name: "业务讨论 1" }));
    fireEvent.click(screen.getByRole("button", { name: "收起业务资料的会话" }));
    expect(
      screen
        .getByRole("button", { name: "业务讨论 1" })
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(screen.getByTestId("selected-conversation").textContent).toBe(
      "chat-1",
    );
    expect(screen.queryByRole("button", { name: "业务讨论 4" })).toBeNull();
    expect(screen.queryByRole("button", { name: "业务讨论 5" })).toBeNull();
    expect(screen.getByRole("button", { name: "引导会话" })).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: "展开其他节点" })
        .getAttribute("aria-expanded"),
    ).toBe("false");
  });

  it("keeps the workspace usable when the chat list fails and supports retry", async () => {
    let failed = true;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      failed && urlOf(input).pathname.endsWith("/conversations")
        ? response({ error: { message: "会话暂时不可用" } }, 503)
        : read(input),
    );
    render(<TeaApp />);
    await screen.findByText("会话暂时不可用");
    expect(screen.getByTestId("selected-conversation")).toBeTruthy();
    failed = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("button", { name: "产品包装讨论" });
    expect(screen.queryByText("会话暂时不可用")).toBeNull();
  });

  it("opens account management directly without exposing public regulations", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      read(input),
    );
    render(<TeaApp />);
    fireEvent.click(await screen.findByRole("button", { name: "账号管理" }));
    await screen.findByRole("heading", { name: "账号管理" });
    expect(screen.queryByText("法规与我的资料")).toBeNull();
    expect(screen.queryByRole("button", { name: "管理后台" })).toBeNull();
  });
});
