import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";

import { StageChat } from "@/components/tea/stage-chat";
import { Workspace } from "@/components/tea/workspace";
import type { CaseDetail, Capabilities, Document } from "@/core/tea/types";
import { clearChatDrafts } from "@/core/tea/use-stage-chat";

rs.mock("@/components/ai-elements/conversation", () => ({
  Conversation: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ConversationContent: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ConversationScrollButton: () => null,
}));
rs.mock("@/components/tea/stage-analysis", () => ({
  StageAnalysisPanel: () => null,
}));
const mobile = rs.hoisted(() => ({ value: false }));
rs.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => mobile.value }));
rs.mock("@/components/ui/sidebar", () => ({
  SidebarTrigger: () => <button>导航</button>,
}));
rs.mock("@/components/ui/resizable", () => ({
  ResizablePanelGroup: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ResizablePanel: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ResizableHandle: () => null,
}));
rs.mock("@/components/ai-elements/streamdown", () => ({
  ClipboardSafeStreamdown: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
}));

const record = {
  id: "one",
  revision: 1,
  artifacts: [],
  facts: [],
  documents: [],
  events: [],
  stages: [{ id: "stage", name: "业务资料", status: "active", tasks: [] }],
} as unknown as CaseDetail;
const capabilities: Capabilities = {
  assistant_mode: "live",
  retrieval_mode: "lexical",
  parser: "basic",
};
const props = {
  record,
  stage: record.stages[0]!,
  accountId: "owner",
  taskId: "",
  capabilities,
  onTask: () => undefined,
  onUpdate: rs.fn(),
  onUpload: () => undefined,
};

beforeEach(() => {
  mobile.value = false;
  clearChatDrafts();
  props.onUpdate.mockClear();
});
afterEach(() => {
  cleanup();
  rs.useRealTimers();
  rs.restoreAllMocks();
});

function transport() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const fetcher = rs
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (_url, options) => {
      if (options?.method !== "POST")
        return new Response(JSON.stringify({ items: [], total: 0 }));
      return new Response(
        new ReadableStream<Uint8Array>({
          start(value) {
            controller = value;
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
  return {
    fetcher,
    event: (event: string, data: unknown) =>
      controller.enqueue(
        new TextEncoder().encode(
          `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
        ),
      ),
    close: () => controller.close(),
  };
}

async function send(text = "准备哪些资料？") {
  const input = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "向茶渡助手提问",
  });
  await waitFor(() => expect(screen.queryByText("读取对话记录…")).toBeNull());
  fireEvent.change(input, { target: { value: text } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
  return input;
}

describe("stage chat experience", () => {
  it("shows the sent question immediately, streams the answer and keeps the next draft", async () => {
    const stream = transport();
    render(<StageChat {...props} />);
    const input = await send();
    expect(screen.getByText("准备哪些资料？")).toBeTruthy();
    expect(input.value).toBe("");
    expect(input.disabled).toBe(false);
    fireEvent.change(input, { target: { value: "我的下一条问题" } });
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    await act(async () => {
      stream.event("delta", { text: "先准备产品说明" });
    });
    await waitFor(() =>
      expect(screen.getByText("先准备产品说明")).toBeTruthy(),
    );
    const messages = [
      {
        id: "u",
        role: "user",
        content: "准备哪些资料？",
        stage_id: "stage",
        task_id: null,
        created_at: "2026-10-11T00:00:00Z",
        citations: [],
        trace: [],
      },
      {
        id: "a",
        role: "assistant",
        content: "先准备产品说明。",
        stage_id: "stage",
        task_id: null,
        created_at: "2026-10-11T00:00:01Z",
        citations: [],
        trace: [],
      },
    ];
    await act(async () => {
      stream.event("done", { messages, case: { ...record, revision: 2 } });
    });
    await waitFor(() => expect(props.onUpdate).toHaveBeenCalledTimes(1));
    expect(screen.getAllByText("准备哪些资料？")).toHaveLength(1);
    expect(input.value).toBe("我的下一条问题");
  });

  it("keeps partial text on interruption and retries the same request ID", async () => {
    const stream = transport();
    render(<StageChat {...props} />);
    await send();
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    await act(async () => {
      stream.event("delta", { text: "已收到的部分回复" });
      stream.close();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "重试回复" })).toBeTruthy(),
    );
    expect(screen.getByText("已收到的部分回复")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试回复" }));
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(3));
    expect(stream.fetcher.mock.calls[1]![1]!.body).toBe(
      stream.fetcher.mock.calls[2]![1]!.body,
    );
  });

  it("does not send during history loading or IME composition and preserves scoped drafts", async () => {
    transport();
    const first = render(<StageChat {...props} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "尚未发出" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(screen.queryByRole("button", { name: "停止回复" })).toBeNull();
    first.unmount();
    const other = render(<StageChat {...props} accountId="other" />);
    expect(screen.getByRole<HTMLTextAreaElement>("textbox").value).toBe("");
    other.unmount();
    render(<StageChat {...props} />);
    expect(screen.getByRole<HTMLTextAreaElement>("textbox").value).toBe(
      "尚未发出",
    );
  });

  it("cancels on unmount and ignores obsolete case updates", async () => {
    const stream = transport();
    const mounted = render(<StageChat {...props} />);
    await send();
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    mounted.unmount();
    expect(stream.fetcher.mock.calls[1]![1]!.signal?.aborted).toBe(true);
    expect(props.onUpdate).not.toHaveBeenCalled();
  });

  it("opens uploaded materials and synchronizes completed processing with task evidence", async () => {
    const uploaded: Document = {
      id: "uploaded",
      title: "上传的业务资料",
      filename: "material.md",
      role: "primary",
      status: "processing",
      mime_type: "text/markdown",
      size: 50,
      version: 1,
      case_id: record.id,
      parent_id: null,
      tags: [],
      created_at: "2026-10-19T00:00:00Z",
      source_url: null,
      error: null,
      chunk_count: 0,
    };
    const processed = {
      ...uploaded,
      status: "ready",
      chunk_count: 2,
    } as Document;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      return new Response(
        JSON.stringify(
          path.includes("/documents/uploaded")
            ? { document: processed }
            : path.includes("/documents?")
              ? { items: [uploaded], total: 1 }
              : { items: [], total: 0 },
        ),
      );
    });
    rs.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    render(
      <Workspace
        accountId="owner"
        record={{ ...record, documents: [uploaded] }}
        uploadedDocument={uploaded}
        conversationId="chat-test"
        stageId="stage"
        setStageId={() => undefined}
        capabilities={capabilities}
        onUpdate={props.onUpdate}
        onUpload={() => undefined}
      />,
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(0);
    });
    expect(
      screen.getByRole("tab", { name: "资料库" }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.getByRole("button", { name: /上传的业务资料.*处理中/ }),
    ).toBeTruthy();
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    expect(props.onUpdate).toHaveBeenCalledWith({
      ...record,
      documents: [processed],
    });
  });

  it("keeps the same live conversation when crossing the mobile breakpoint", async () => {
    const stream = transport();
    const workspace = (
      <Workspace
        accountId="owner"
        record={record}
        conversationId="chat-test"
        stageId="stage"
        setStageId={() => undefined}
        capabilities={capabilities}
        onUpdate={props.onUpdate}
        onUpload={() => undefined}
      />
    );
    const mounted = render(workspace);
    const input = await send();
    fireEvent.change(input, { target: { value: "正在编辑下一条" } });
    await waitFor(() =>
      expect(
        stream.fetcher.mock.calls.filter(
          ([, options]) => options?.method === "POST",
        ),
      ).toHaveLength(1),
    );
    await act(async () => {
      stream.event("delta", { text: "正在流式接收" });
    });
    mobile.value = true;
    mounted.rerender(
      <Workspace
        accountId="owner"
        record={record}
        conversationId="chat-test"
        stageId="stage"
        setStageId={() => undefined}
        capabilities={capabilities}
        onUpdate={props.onUpdate}
        onUpload={() => undefined}
      />,
    );
    expect(
      stream.fetcher.mock.calls.find(
        ([, options]) => options?.method === "POST",
      )?.[1]?.signal?.aborted,
    ).toBe(false);
    expect(screen.getByRole<HTMLTextAreaElement>("textbox").value).toBe(
      "正在编辑下一条",
    );
    await act(async () => {
      stream.event("delta", { text: "，切换后继续" });
    });
    await waitFor(() =>
      expect(screen.getByText("正在流式接收，切换后继续")).toBeTruthy(),
    );
  });

  it("keeps a failed question in order when a later question succeeds and after navigation", async () => {
    const stream = transport();
    const first = render(<StageChat {...props} />);
    await send("第一条失败的问题");
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    await act(async () => {
      stream.event("delta", { text: "第一条部分回复" });
      stream.close();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "重试回复" })).toBeTruthy(),
    );
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    await send("第二条成功的问题");
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(3));
    const messages = [
      {
        id: "second",
        role: "user",
        content: "第二条成功的问题",
        stage_id: "stage",
        task_id: null,
        created_at: "2026-10-11T00:00:00Z",
        citations: [],
        trace: [],
      },
    ];
    await act(async () => {
      stream.event("done", { messages, case: { ...record, revision: 2 } });
    });
    await waitFor(() => expect(props.onUpdate).toHaveBeenCalledTimes(1));
    const questions = Array.from(
      document.querySelectorAll(".tea-message.user .tea-message-content"),
      (node) => node.textContent,
    );
    expect(questions).toEqual(["第一条失败的问题", "第二条成功的问题"]);
    first.unmount();
    stream.fetcher.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: messages, total: 1 })),
    );
    render(<StageChat {...props} />);
    await waitFor(() =>
      expect(screen.getByText("第一条部分回复")).toBeTruthy(),
    );
    expect(screen.getByRole("button", { name: "重试回复" })).toBeTruthy();
  });
});
