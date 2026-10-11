import { afterEach, describe, expect, it, rs } from "@rstest/core";

import { teaRequest, TeaApiError, streamMessage } from "@/core/tea/api";

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
