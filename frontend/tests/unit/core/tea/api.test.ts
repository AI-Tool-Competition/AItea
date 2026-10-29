import { afterEach, describe, expect, it, rs } from "@rstest/core";

import { teaRequest, TeaApiError, streamMessage } from "@/core/tea/api";
import type { ToolOutputPreview } from "@/core/tea/types";

describe("Tea API transport", () => {
  afterEach(() => {
    rs.restoreAllMocks();
  });
  it("preserves API conflict messages and status so drafts can be retained", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "stale_revision",
            message: "资料已更新，请刷新后核验",
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(teaRequest("/cases/example")).rejects.toMatchObject({
      status: 409,
      code: "stale_revision",
      message: "资料已更新，请刷新后核验",
    });
  });
  it("handles an expired session even when the response body is unavailable", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("", { status: 401 }),
    );
    await expect(teaRequest("/bootstrap")).rejects.toBeInstanceOf(TeaApiError);
  });
  it("accepts successful empty logout responses", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    await expect(
      teaRequest("/auth/logout", { method: "POST" }),
    ).resolves.toBeUndefined();
  });
  it("delivers fragmented UTF-8 deltas before the response ends", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(value) {
        controller = value;
      },
    });
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const received = rs.fn();
    const done = { messages: [], case: { id: "one" } };
    const request = streamMessage(
      "one",
      { stage_id: "stage", content: "问题", client_message_id: "id" },
      { onEvent: received },
    );
    for (const byte of new TextEncoder().encode(
      'event: delta\r\ndata: {"text":"茶叶"}\r\n\r\n',
    ))
      controller.enqueue(new Uint8Array([byte]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(received).toHaveBeenCalledWith({
      event: "delta",
      data: { text: "茶叶" },
    });
    controller.enqueue(
      new TextEncoder().encode(
        `event: done\ndata: ${JSON.stringify(done)}\n\n`,
      ),
    );
    await expect(request).resolves.toEqual(done);
  });
  it("rejects a truncated stream instead of reporting success", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response('event: delta\ndata: {"text":"半句"}\n\n', {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    await expect(
      streamMessage(
        "one",
        { stage_id: "stage", content: "问题", client_message_id: "id" },
        { onEvent: () => undefined },
      ),
    ).rejects.toMatchObject({ code: "stream_interrupted" });
  });
  it.each([undefined, false, true])(
    "preserves the optional continuation flag %s",
    async (append) => {
      const data = {
        step_id: "agent",
        ...(append === undefined ? {} : { append }),
      };
      const done = { messages: [], case: { id: "one" } };
      rs.spyOn(globalThis, "fetch").mockResolvedValue(
        new Response(
          `event: start\ndata: ${JSON.stringify(data)}\n\nevent: done\ndata: ${JSON.stringify(done)}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );
      const received = rs.fn();
      await streamMessage(
        "one",
        { stage_id: "stage", content: "问题", client_message_id: "id" },
        { onEvent: received },
      );
      expect(received).toHaveBeenCalledWith({ event: "start", data });
    },
  );
  it("rejects an invalid continuation flag instead of silently clearing visible text", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        'event: start\ndata: {"step_id":"agent","append":"false"}\n\n',
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    await expect(
      streamMessage(
        "one",
        { stage_id: "stage", content: "问题", client_message_id: "id" },
        { onEvent: () => undefined },
      ),
    ).rejects.toMatchObject({ code: "invalid_response" });
  });
  it("preserves streaming errors and releases a cancelled reader", async () => {
    const cancelled = rs.fn();
    const body = new ReadableStream<Uint8Array>({ cancel: cancelled });
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const abort = new AbortController();
    const request = streamMessage(
      "one",
      { stage_id: "stage", content: "问题", client_message_id: "id" },
      { onEvent: () => undefined, signal: abort.signal },
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    abort.abort();
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(cancelled).toHaveBeenCalled();
  });
});

it.each([
  "read_law",
  "update_business_card",
  "lookup_chinese_law",
  "get_law_versions",
  "update_task_brief",
])("accepts registered runtime tool snapshots (%s)", async (name) => {
  const step = {
    id: "agent",
    label: "协作",
    status: "running",
    detail: "",
    tools: [
      {
        id: "call",
        name,
        label: "处理",
        position: 1,
        status: "completed",
        attempt: 1,
        max_attempts: 1,
        detail: "",
        duration_ms: 1,
      },
    ],
  };
  const done = { messages: [], case: { id: "one" } };
  rs.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      `event: step\ndata: ${JSON.stringify(step)}\n\nevent: done\ndata: ${JSON.stringify(done)}\n\n`,
      {
        headers: { "Content-Type": "text/event-stream" },
      },
    ),
  );
  const received = rs.fn();
  await expect(
    streamMessage(
      "one",
      { stage_id: "stage", content: "问题", client_message_id: "id" },
      { onEvent: received },
    ),
  ).resolves.toEqual(done);
  expect(received).toHaveBeenCalledWith({ event: "step", data: step });
  rs.restoreAllMocks();
});

it.each(["valid", "oversized", "unicode", "thinking", "thinking-oversized"])(
  "validates streamed tool output bounds (%s)",
  async (mode) => {
    const oversized = mode === "oversized" || mode === "thinking-oversized";
    const thinking = mode.startsWith("thinking");
    const preview: ToolOutputPreview = {
      text: "实际执行输出",
      items: [],
      notice: "",
      truncated: false,
    };
    if (oversized) preview.text = "x".repeat(4001);
    if (thinking) preview.text = "思".repeat(oversized ? 60001 : 16000);
    if (mode === "unicode")
      preview.items = [{ label: "😀".repeat(200), text: "😀".repeat(650) }];
    const step = {
      id: "agent",
      label: "协作",
      status: "running",
      detail: "",
      tools: [
        {
          id: "call",
          name: thinking ? "agent_turn" : "inspect_image",
          label: "识图",
          position: 1,
          status: "completed",
          attempt: 1,
          max_attempts: 1,
          detail: "",
          duration_ms: 50,
          output_preview: preview,
        },
      ],
    };
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        `event: step\ndata: ${JSON.stringify(step)}\n\nevent: done\ndata: ${JSON.stringify({ messages: [], case: { id: "one" } })}\n\n`,
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    const received = rs.fn();
    const promise = streamMessage(
      "one",
      { stage_id: "stage", content: "问题", client_message_id: "output" },
      { onEvent: received },
    );
    if (oversized)
      await expect(promise).rejects.toMatchObject({ code: "invalid_response" });
    else {
      await promise;
      expect(received).toHaveBeenCalledWith({ event: "step", data: step });
    }
    rs.restoreAllMocks();
  },
);

it("accepts a long sequence of cumulative tool results within the bounded stream budget", async () => {
  const step = {
    id: "agent",
    label: "处理进度",
    status: "running",
    detail: "",
    tools: Array.from({ length: 40 }, (_, index) => ({
      id: `call-${index}`,
      name: "web_search",
      label: "搜索公开信息",
      position: index + 1,
      status: "completed",
      attempt: 1,
      max_attempts: 1,
      detail: "",
      duration_ms: 50,
      output_preview: {
        text: "x".repeat(3900),
        items: [],
        notice: "",
        truncated: true,
      },
    })),
  };
  const encoded = new TextEncoder().encode(
    `event: step\ndata: ${JSON.stringify(step)}\n\n`,
  );
  const done = { messages: [], case: { id: "one" } };
  let remaining = 100;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (remaining-- > 0) controller.enqueue(encoded);
      else {
        controller.enqueue(
          new TextEncoder().encode(
            `event: done\ndata: ${JSON.stringify(done)}\n\n`,
          ),
        );
        controller.close();
      }
    },
  });
  let received = 0;
  rs.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
  );
  try {
    await expect(
      streamMessage(
        "one",
        {
          stage_id: "stage",
          content: "检查全部资料",
          client_message_id: "long",
        },
        { onEvent: () => received++ },
      ),
    ).resolves.toEqual(done);
    expect(encoded.byteLength * 100).toBeGreaterThan(8 * 1024 * 1024);
    expect(received).toBe(100);
  } finally {
    rs.restoreAllMocks();
  }
});
