import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";

import {
  clearNodeAnalyses,
  subscribeNodeAnalysis,
} from "@/core/tea/node-analysis";
import type { LegalStreamEvent } from "@/core/tea/types";

const result = {
  id: "entry-one",
  case_id: "case-one",
  stage_id: "prepare",
  case_revision: 1,
  legal_analysis_id: "report-one",
  legal_revision: 2,
  created_at: "2026-10-13T00:00:00Z",
  status: "completed",
  output: "请补充产品名称",
  assistant_mode: "demo",
  trace: [],
  citations: [],
  checks: [],
};

function response(event: string, data: unknown) {
  return new Response(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

function subscribe(
  onEvent: (event: LegalStreamEvent) => void = () => undefined,
  accountId = "account-one",
  retry = false,
) {
  return subscribeNodeAnalysis(
    accountId,
    "case-one",
    "prepare",
    1,
    retry,
    onEvent,
  );
}

describe("node analysis request lifecycle", () => {
  beforeEach(() => {
    rs.useFakeTimers();
  });
  afterEach(() => {
    clearNodeAnalyses();
    rs.useRealTimers();
    rs.restoreAllMocks();
  });

  it("automatically reads the existing result after a running-analysis conflict", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        response("error", {
          code: "analysis_in_progress",
          message: "本业务正在分析",
        }),
      )
      .mockImplementation(async () => response("done", { analysis: result }));
    const events = rs.fn();
    const request = subscribe(events);
    // Attach the rejection handler before advancing the failed first request.
    const outcome = request.promise.catch((error: unknown) => error);
    await rs.advanceTimersByTimeAsync(0);
    expect(events).toHaveBeenCalledWith({
      event: "status",
      data: { label: "已有分析正在运行，正在等待结果…" },
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await rs.advanceTimersByTimeAsync(500);
    expect(await outcome).toEqual(result);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[0]![1]!.body).toBe(
      fetcher.mock.calls[1]![1]!.body,
    );
  });

  it("waits for available capacity after an HTTP busy response", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: { code: "analysis_busy", message: "当前分析较多" },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockImplementation(async () => response("done", { analysis: result }));
    const request = subscribe();
    const outcome = request.promise.catch((error: unknown) => error);
    await rs.advanceTimersByTimeAsync(500);
    expect(await outcome).toEqual(result);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("stops a pending retry immediately and never sends it after stopping", async () => {
    const fetcher = rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response("error", {
        code: "analysis_in_progress",
        message: "已有分析正在运行",
      }),
    );
    const request = subscribe();
    const outcome = request.promise.catch((error: unknown) => error);
    await rs.advanceTimersByTimeAsync(0);
    request.stop();
    expect(await outcome).toMatchObject({ name: "AbortError" });
    await rs.advanceTimersByTimeAsync(10_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(rs.getTimerCount()).toBe(0);
  });

  it("keeps real conflicts visible without retrying them", async () => {
    const fetcher = rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response("error", {
        code: "stale_revision",
        message: "资料已更新，请刷新",
      }),
    );
    await expect(subscribe().promise).rejects.toMatchObject({
      code: "stale_revision",
    });
    await rs.advanceTimersByTimeAsync(10_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("bounds the wait for a running job instead of retrying forever", async () => {
    const fetcher = rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response("error", {
        code: "analysis_in_progress",
        message: "已有分析正在运行",
      }),
    );
    const outcome = subscribe().promise.catch((error: unknown) => error);
    await rs.advanceTimersByTimeAsync(5 * 60_000);
    expect(await outcome).toMatchObject({ code: "analysis_wait_timeout" });
    expect(fetcher.mock.calls.length).toBeLessThan(65);
    const calls = fetcher.mock.calls.length;
    await rs.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(calls);
    expect(rs.getTimerCount()).toBe(0);
  });

  it("reattaches a quick revisit to the same stream and replays its progress", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const fetcher = rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(value) {
            controller = value;
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    const first = subscribe();
    const firstOutcome = first.promise.catch((error: unknown) => error);
    const progress = { event: "status", data: { label: "正在核对资料" } };
    controller.enqueue(
      new TextEncoder().encode(
        'event: status\ndata: {"label":"正在核对资料"}\n\n',
      ),
    );
    await rs.advanceTimersByTimeAsync(0);
    first.unsubscribe();
    await rs.advanceTimersByTimeAsync(50);
    const events = rs.fn();
    const second = subscribe(events);
    const secondOutcome = second.promise.catch((error: unknown) => error);
    expect(second.promise).toBe(first.promise);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(events).toHaveBeenCalledWith(progress);
    controller.enqueue(
      new TextEncoder().encode(
        `event: done\ndata: ${JSON.stringify({ analysis: result })}\n\n`,
      ),
    );
    expect(await firstOutcome).toEqual(result);
    expect(await secondOutcome).toEqual(result);
  });

  it("cancels a stream after leaving the node beyond the revisit window", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new ReadableStream(), {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const request = subscribe();
    const outcome = request.promise.catch((error: unknown) => error);
    await rs.advanceTimersByTimeAsync(0);
    request.unsubscribe();
    await rs.advanceTimersByTimeAsync(501);
    expect(await outcome).toMatchObject({ name: "AbortError" });
    expect(rs.getTimerCount()).toBe(0);
  });

  it("shares active work when a retry consumer attaches to the same inputs", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new ReadableStream(), {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const first = subscribe();
    const firstOutcome = first.promise.catch((error: unknown) => error);
    const second = subscribe(undefined, "account-one", true);
    const secondOutcome = second.promise.catch((error: unknown) => error);
    expect(second.promise).toBe(first.promise);
    first.stop();
    await Promise.all([firstOutcome, secondOutcome]);
  });

  it("keeps accounts isolated and cancels all work immediately on logout", async () => {
    const fetcher = rs.spyOn(globalThis, "fetch").mockImplementation(
      async () =>
        new Response(new ReadableStream(), {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    const first = subscribe();
    const firstOutcome = first.promise.catch((error: unknown) => error);
    const second = subscribe(undefined, "account-two");
    const secondOutcome = second.promise.catch((error: unknown) => error);
    expect(second.promise).not.toBe(first.promise);
    expect(fetcher).toHaveBeenCalledTimes(2);
    first.unsubscribe();
    clearNodeAnalyses();
    expect(await firstOutcome).toMatchObject({ name: "AbortError" });
    expect(await secondOutcome).toMatchObject({ name: "AbortError" });
    expect(rs.getTimerCount()).toBe(0);
  });
});
