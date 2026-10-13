import { streamStageAnalysis, TeaApiError } from "./api";
import type { LegalStreamEvent, StageAnalysis } from "./types";

interface Request {
  abort: AbortController;
  listeners: Set<(event: LegalStreamEvent) => void>;
  progress: Map<string, LegalStreamEvent>;
  promise: Promise<StageAnalysis>;
  cancelTimer?: ReturnType<typeof setTimeout>;
}
const requests = new Map<string, Request>();

function cancelRequest(request: Request) {
  clearTimeout(request.cancelTimer);
  request.abort.abort();
}

export function clearNodeAnalyses() {
  for (const request of requests.values()) cancelRequest(request);
  requests.clear();
}

function waitForRetry(delay: number, signal: AbortSignal) {
  signal.throwIfAborted();
  return new Promise<void>((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      reject(
        signal.reason instanceof Error
          ? signal.reason
          : new DOMException("Aborted", "AbortError"),
      );
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, delay);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

async function readNodeAnalysis(
  caseId: string,
  stageId: string,
  body: Parameters<typeof streamStageAnalysis>[2],
  options: Parameters<typeof streamStageAnalysis>[3],
) {
  const deadline = Date.now() + 5 * 60_000;
  let delay = 500;
  const timedOut = () =>
    new TeaApiError(
      504,
      "analysis_wait_timeout",
      "等待已有分析结束超时。已保存的内容仍然保留，请检查运行状态后重试。",
    );
  while (true) {
    options.signal.throwIfAborted();
    if (Date.now() >= deadline) throw timedOut();
    try {
      return await streamStageAnalysis(caseId, stageId, body, options);
    } catch (error) {
      options.signal.throwIfAborted();
      // Only admission conflicts are safe to repeat: the server either returns
      // the persisted node result or reserves one run for these same inputs.
      if (
        !(error instanceof TeaApiError) ||
        !["analysis_in_progress", "analysis_busy"].includes(error.code)
      )
        throw error;
      options.onEvent({
        event: "status",
        data: {
          label:
            error.code === "analysis_in_progress"
              ? "已有分析正在运行，正在等待结果…"
              : "分析任务较多，正在等待空闲…",
        },
      });
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw timedOut();
      await waitForRetry(Math.min(delay, remaining), options.signal);
      delay = Math.min(delay * 2, 5_000);
    }
  }
}

// Share active work for the same account, node and input revision, including
// retry consumers. Brief navigation remounts reattach before cancellation.
export function subscribeNodeAnalysis(
  accountId: string,
  caseId: string,
  stageId: string,
  revision: number,
  retry: boolean,
  onEvent: (event: LegalStreamEvent) => void,
) {
  const key = JSON.stringify([accountId, caseId, stageId, revision]);
  let request = requests.get(key);
  if (!request || request.abort.signal.aborted) {
    const abort = new AbortController();
    const listeners = new Set<(event: LegalStreamEvent) => void>();
    const progress = new Map<string, LegalStreamEvent>();
    const promise = readNodeAnalysis(
      caseId,
      stageId,
      { expected_revision: revision, retry },
      {
        signal: abort.signal,
        onEvent(event) {
          progress.set(
            event.event === "step" ? event.data.id : "status",
            event,
          );
          for (const listener of listeners) listener(event);
        },
      },
    ).finally(() => {
      const completed = requests.get(key);
      if (completed?.promise === promise) {
        clearTimeout(completed.cancelTimer);
        requests.delete(key);
      }
    });
    request = { abort, listeners, progress, promise };
    requests.set(key, request);
  }
  const active = request;
  clearTimeout(active.cancelTimer);
  active.cancelTimer = undefined;
  active.listeners.add(onEvent);
  for (const event of active.progress.values()) onEvent(event);
  return {
    promise: active.promise,
    stop: () => cancelRequest(active),
    unsubscribe() {
      active.listeners.delete(onEvent);
      if (active.listeners.size || requests.get(key) !== active) return;
      clearTimeout(active.cancelTimer);
      active.cancelTimer = setTimeout(() => {
        if (!active.listeners.size) cancelRequest(active);
      }, 500);
    },
  };
}
