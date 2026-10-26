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
  it("restores scoped cached messages immediately and refreshes them without a loading flash", async () => {
    const saved = {
      id: "cached",
      role: "assistant",
      content: "已保存的对话内容",
      stage_id: "stage",
      task_id: null,
      created_at: "2026-10-26T00:00:00Z",
      citations: [],
      trace: [],
    };
    let refresh!: (value: Response) => void;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [saved], total: 1 })),
      )
      .mockImplementation(
        () =>
          new Promise<Response>((resolve) => {
            refresh = resolve;
          }),
      );
    const first = render(<StageChat {...props} conversationId="cache" />);
    await screen.findByText(saved.content);
    first.unmount();
    const second = render(<StageChat {...props} conversationId="cache" />);
    expect(screen.getByText(saved.content)).toBeTruthy();
    expect(screen.queryByText("读取对话记录…")).toBeNull();
    await act(async () => {
      refresh(
        new Response(
          JSON.stringify({
            items: [
              saved,
              {
                ...saved,
                id: "new",
                content: "同步的新消息",
                created_at: "2026-10-26T00:00:01Z",
              },
            ],
            total: 2,
          }),
        ),
      );
    });
    await screen.findByText("同步的新消息");
    expect(screen.getAllByText(saved.content)).toHaveLength(1);
    second.unmount();
    const other = render(
      <StageChat {...props} accountId="another" conversationId="cache" />,
    );
    expect(screen.queryByText(saved.content)).toBeNull();
    expect(screen.getByText("读取对话记录…")).toBeTruthy();
    expect(fetcher).toHaveBeenCalledTimes(3);
    other.unmount();
    clearChatDrafts();
    render(<StageChat {...props} conversationId="cache" />);
    expect(screen.queryByText(saved.content)).toBeNull();
  });

  it("aborts cached-history refresh on logout and prevents a late response from repopulating it", async () => {
    let finish!: (value: Response) => void;
    const fetcher = rs.spyOn(globalThis, "fetch").mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    const view = render(<StageChat {...props} conversationId="late" />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    clearChatDrafts();
    expect(fetcher.mock.calls[0]![1]!.signal?.aborted).toBe(true);
    await act(async () => {
      finish(
        new Response(
          JSON.stringify({
            items: [
              {
                id: "old",
                role: "assistant",
                content: "旧账号回复",
                trace: [],
                citations: [],
              },
            ],
            total: 1,
          }),
        ),
      );
    });
    view.unmount();
    render(<StageChat {...props} conversationId="late" />);
    expect(screen.queryByText("旧账号回复")).toBeNull();
    expect(screen.getByText("读取对话记录…")).toBeTruthy();
  });

  it("handles capabilities internally and sends ordinary conversation requests", async () => {
    const stream = transport();
    render(<StageChat {...props} conversationId="conversation" />);
    await send("请结合图片核对资料，看看下一步要做什么。");
    expect(
      screen.queryByRole("button", { name: /智能调用|法律分析|记忆|能力/ }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "上传资料" })).toBeTruthy();
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    const request = JSON.parse(
      stream.fetcher.mock.calls[1]![1]!.body as string,
    );
    expect(request.conversation_id).toBe("conversation");
    expect(request.mode).toBeUndefined();
    expect(request.skills).toBeUndefined();
    expect(
      stream.fetcher.mock.calls.some(([url]) =>
        /agent-tools|memories/.test(
          typeof url === "string"
            ? url
            : url instanceof URL
              ? url.href
              : url.url,
        ),
      ),
    ).toBe(false);
  });

  it("updates streamed activity in place, ignores late snapshots and collapses saved progress", async () => {
    const stream = transport();
    render(<StageChat {...props} conversationId="conversation" />);
    await send();
    const article = screen.getByRole("article", { name: "茶渡助手回复" });
    const progress = screen.getByRole("button", { name: /处理进度/ });
    const answer = article.querySelector(".tea-message-content");
    expect(progress.getAttribute("aria-expanded")).toBe("true");
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    const call = {
      id: "call",
      name: "web_search",
      label: "provider-tool",
      position: 1,
      status: "running",
      attempt: 1,
      max_attempts: 1,
      detail: "private-detail",
      duration_ms: 0,
    };
    const step = {
      id: "agent",
      run_id: "run",
      sequence: 1,
      label: "智能工具协作",
      status: "running",
      detail: "internal",
      tools: [
        call,
        { ...call, id: "memory", name: "recall_memory", label: "检索记忆" },
      ],
    };
    await act(async () => {
      stream.event("step", step);
      stream.event("status", { label: "正在选择工具与整理记忆…" });
    });
    await screen.findByText("正在搜索公开信息");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    const completed = {
      ...step,
      sequence: 3,
      status: "completed",
      tools: [{ ...call, status: "completed", duration_ms: 1400 }],
    };
    await act(async () => {
      stream.event("step", completed);
      stream.event("step", { ...step, sequence: 2 });
      stream.event("delta", { text: "已找到可供核对的公开来源。" });
    });
    await screen.findByText("已找到可供核对的公开来源。");
    expect(progress.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("进行中")).toBeNull();
    expect(
      screen.queryByText(
        /智能工具协作|检索记忆|正在选择工具|provider-tool|private-detail/,
      ),
    ).toBeNull();
    // Even a manually reopened progress area closes at the end of this turn.
    fireEvent.click(progress);
    expect(screen.getByText("已完成")).toBeTruthy();
    await act(async () => {
      stream.event("done", {
        messages: [
          {
            id: "u",
            role: "user",
            content: "准备哪些资料？",
            stage_id: "stage",
            task_id: null,
            created_at: "2026-10-26T00:00:00Z",
            citations: [],
            trace: [],
          },
          {
            id: "a",
            role: "assistant",
            content: "已找到可供核对的公开来源。",
            stage_id: "stage",
            task_id: null,
            created_at: "2026-10-26T00:00:01Z",
            citations: [],
            trace: [completed],
          },
        ],
        case: record,
      });
    });
    await waitFor(() => expect(props.onUpdate).toHaveBeenCalledTimes(1));
    const trigger = screen.getByRole("button", { name: /已完成 1 项处理/ });
    expect(trigger).toBe(progress);
    expect(screen.getByRole("article", { name: "茶渡助手回复" })).toBe(article);
    expect(article.querySelector(".tea-message-content")).toBe(answer);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(trigger);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("1.4 秒")).toBeTruthy();
  });

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

  it.each([
    "agent_request_not_replayable",
    "agent_context_changed",
    "analysis_context_changed",
    "agent_lease_lost",
  ])("uses a new request only after explicit retry of %s", async (code) => {
    const stream = transport();
    render(<StageChat {...props} />);
    await send();
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(2));
    await act(async () => {
      stream.event("error", { code, message: "依据已变化，请重新发送" });
    });
    await screen.findByRole("button", { name: "重试回复" });
    expect(stream.fetcher).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "重试回复" }));
    await waitFor(() => expect(stream.fetcher).toHaveBeenCalledTimes(3));
    const before = JSON.parse(stream.fetcher.mock.calls[1]![1]!.body as string);
    const after = JSON.parse(stream.fetcher.mock.calls[2]![1]!.body as string);
    expect(after.client_message_id).not.toBe(before.client_message_id);
    expect(after.content).toBe(before.content);
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

  it.each(["completed", "detail-only"])(
    "opens uploaded materials and synchronizes %s processing with task evidence",
    async (change) => {
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
        ...(change === "completed"
          ? { status: "ready", chunk_count: 2 }
          : {
              processing_detail: {
                state: "ocr_disabled",
                phase: "parse",
                retry_after_seconds: null,
              },
            }),
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
        screen
          .getByRole("tab", { name: "资料库" })
          .getAttribute("aria-selected"),
      ).toBe("true");
      expect(
        screen.getByRole("button", { name: /上传的业务资料.*等待处理/ }),
      ).toBeTruthy();
      await act(async () => {
        await rs.advanceTimersByTimeAsync(3000);
      });
      expect(props.onUpdate).toHaveBeenCalledWith({
        ...record,
        documents: [processed],
      });
    },
  );

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
