import { afterEach, describe, expect, it, rs } from "@rstest/core";

import { streamDebug } from "@/core/tea/api";
import type { DebugRequest, DebugRun, TraceStep } from "@/core/tea/types";

const request: DebugRequest = {
  template: {
    id: "tea",
    version: 1,
    title: "测试流程",
    description: "",
    origin: "中国",
    destination: "英国",
    is_demo: true,
    modes: [],
    stages: [],
    agent: {
      language: "中文",
      instructions: "",
      retrieval_limit: 3,
      steps: [],
    },
  },
  stage_id: "documents",
  input: "检查企业资料",
  task_values: {},
  document_ids: [],
  expectations: [],
  client_id: "attempt-one",
};
const step: TraceStep = {
  id: "check",
  type: "check",
  label: "检查资料",
  status: "completed",
  detail: "缺少企业名称",
  duration_ms: 12,
  input_data: { tasks: [{ id: "company", values: {} }] },
  output_data: { checks: [{ id: "company.name", status: "missing" }] },
};
const run: DebugRun = {
  id: "run-one",
  created_at: "2026-10-12T00:00:00Z",
  status: "completed",
  request,
  sample_id: null,
  source_run_id: null,
  document_versions: [],
  evaluations: [],
  stage_id: "documents",
  output: "请补充企业名称。",
  assistant_mode: "demo",
  trace: [step],
  citations: [],
  checks: [],
};
const encode = (event: string, data: unknown) =>
  new TextEncoder().encode(
    `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
  );

function openStream(cancel?: () => void | Promise<void>) {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
    },
    cancel,
  });
  const fetch = rs
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(
      new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
    );
  return { controller, fetch };
}

describe("workflow debug streaming", () => {
  afterEach(() => {
    rs.restoreAllMocks();
    rs.useRealTimers();
  });

  it("delivers real step input/output before completion and posts the checkpoint request", async () => {
    const { controller, fetch } = openStream();
    const onEvent = rs.fn();
    const body = {
      ...request,
      source_run_id: "earlier",
      start_step_id: "check",
    };
    const pending = streamDebug(body, onEvent);
    controller.enqueue(encode("run", { id: run.id }));
    controller.enqueue(encode("step", { ...step, status: "running" }));
    controller.enqueue(encode("step", step));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onEvent).toHaveBeenCalledWith({
      event: "run",
      data: { id: run.id },
    });
    expect(onEvent).toHaveBeenCalledWith({ event: "step", data: step });
    expect(fetch).toHaveBeenCalledWith(
      "/api/tea/templates/debug/stream",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
      }),
    );
    controller.enqueue(encode("done", run));
    await expect(pending).resolves.toEqual(run);
  });

  it("reads split UTF-8 frames, heartbeats and step-scoped progress", async () => {
    const { controller } = openStream();
    const onEvent = rs.fn();
    const pending = streamDebug(request, onEvent);
    const frames =
      ': keepalive\r\n\r\nevent: status\r\ndata: {"label":"正在检索"}\r\n\r\nevent: delta\r\ndata: {"text":"茶叶",\r\ndata: "step_id":"guide"}\r\n\r\n';
    for (const byte of new TextEncoder().encode(frames))
      controller.enqueue(new Uint8Array([byte]));
    controller.enqueue(encode("done", run));
    await expect(pending).resolves.toEqual(run);
    expect(onEvent).toHaveBeenCalledWith({
      event: "status",
      data: { label: "正在检索" },
    });
    expect(onEvent).toHaveBeenCalledWith({
      event: "delta",
      data: { text: "茶叶", step_id: "guide" },
    });
  });

  it("preserves checkpoint conflict errors", async () => {
    const { controller } = openStream();
    const pending = streamDebug(request, () => undefined);
    controller.enqueue(
      encode("error", {
        code: "debug_checkpoint_stale",
        message: "资料已变化，请重新试运行。",
      }),
    );
    await expect(pending).rejects.toMatchObject({
      code: "debug_checkpoint_stale",
      message: "资料已变化，请重新试运行。",
    });
  });

  it("returns failed execution snapshots as runs so their completed steps remain inspectable", async () => {
    const { controller } = openStream();
    const failed = {
      ...run,
      status: "failed",
      error: { code: "provider_failed", message: "分析暂时无法完成" },
    };
    const pending = streamDebug(request, () => undefined);
    controller.enqueue(encode("done", failed));
    await expect(pending).resolves.toEqual(failed);
  });

  it.each([
    "event: step\ndata: {broken}\n\n",
    "event: step\ndata: null\n\n",
    'event: step\ndata: {"id":"check","status":"invented"}\n\n',
    'event: done\ndata: {"id":"run-one"}\n\n',
    'event: delta\ndata: {"text":42}\n\n',
  ])(
    "rejects malformed known events instead of exposing unusable results: %s",
    async (frame) => {
      const { controller } = openStream();
      const pending = streamDebug(request, () => undefined);
      controller.enqueue(new TextEncoder().encode(frame));
      controller.close();
      await expect(pending).rejects.toMatchObject({ code: "invalid_response" });
    },
  );

  it("rejects a stream ending after a completed step without a final run", async () => {
    const { controller } = openStream();
    const pending = streamDebug(request, () => undefined);
    controller.enqueue(encode("step", step));
    controller.close();
    await expect(pending).rejects.toMatchObject({ code: "stream_interrupted" });
  });

  it.each([
    { request: {} },
    { evaluations: [null] },
    { checks: [{ status: "missing" }] },
    { citations: [{ excerpt: "资料原文" }] },
    { document_versions: [{ document_id: "document" }] },
  ])("rejects incomplete nested result data: %o", async (invalid) => {
    const { controller } = openStream();
    const pending = streamDebug(request, () => undefined);
    controller.enqueue(encode("done", { ...run, ...invalid }));
    await expect(pending).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects cancellation promptly even when the source never finishes cancelling", async () => {
    const cancel = rs.fn(() => new Promise<void>(() => undefined));
    openStream(cancel);
    const abort = new AbortController();
    const pending = streamDebug(request, () => undefined, abort.signal).catch(
      (error: unknown) => error,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    abort.abort();
    const outcome = await Promise.race([
      pending,
      new Promise((resolve) => setTimeout(() => resolve("still waiting"), 100)),
    ]);
    expect(outcome).toMatchObject({ name: "AbortError" });
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("does not send an already cancelled request", async () => {
    const { fetch } = openStream();
    const abort = new AbortController();
    abort.abort();
    await expect(
      streamDebug(request, () => undefined, abort.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("retains the 60-second inactivity bound and cancels the network request", async () => {
    rs.useFakeTimers();
    const cancel = rs.fn();
    const { fetch } = openStream(cancel);
    const pending = streamDebug(request, () => undefined).catch(
      (error: unknown) => error,
    );
    await rs.advanceTimersByTimeAsync(60_000);
    expect(await pending).toMatchObject({ code: "stream_timeout" });
    expect(fetch.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("allows multi-step runs longer than a minute while limiting total execution to twenty minutes", async () => {
    rs.useFakeTimers();
    const { controller } = openStream();
    const pending = streamDebug(request, () => undefined).catch(
      (error: unknown) => error,
    );
    for (let elapsed = 0; elapsed < 1_200_000; elapsed += 50_000) {
      controller.enqueue(new TextEncoder().encode(": keepalive\n\n"));
      await rs.advanceTimersByTimeAsync(50_000);
    }
    expect(await pending).toMatchObject({ code: "stream_timeout" });
  });

  it("bounds an unfinished frame", async () => {
    const { controller } = openStream();
    const pending = streamDebug(request, () => undefined);
    controller.enqueue(
      new TextEncoder().encode(
        `event: step\ndata: ${"x".repeat(8 * 1024 * 1024)}`,
      ),
    );
    await expect(pending).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("bounds the total stream even when each event is small", async () => {
    const { controller } = openStream();
    const pending = streamDebug(request, () => undefined);
    const chunk = encode("delta", { text: "茶".repeat(180_000) });
    for (let i = 0; i < 64; i++) controller.enqueue(chunk);
    await expect(pending).rejects.toMatchObject({ code: "invalid_response" });
  });
});
