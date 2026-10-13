import { streamStageAnalysis } from "./api";
import type { LegalStreamEvent, StageAnalysis } from "./types";

interface Request {
  abort: AbortController;
  listeners: Set<(event: LegalStreamEvent) => void>;
  progress: Map<string, LegalStreamEvent>;
  promise: Promise<StageAnalysis>;
}
const requests = new Map<string, Request>();

export function clearNodeAnalyses() {
  for (const request of requests.values()) request.abort.abort();
  requests.clear();
}

// Share only active work, scoped to the signed-in account. React remounts can
// reattach before cancellation; revisits read the server's persisted result.
export function subscribeNodeAnalysis(
  accountId: string,
  caseId: string,
  stageId: string,
  revision: number,
  retry: boolean,
  onEvent: (event: LegalStreamEvent) => void,
) {
  const key = JSON.stringify([accountId, caseId, stageId, revision, retry]);
  let request = requests.get(key);
  if (!request || request.abort.signal.aborted) {
    const abort = new AbortController();
    const listeners = new Set<(event: LegalStreamEvent) => void>();
    const progress = new Map<string, LegalStreamEvent>();
    const promise = streamStageAnalysis(
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
      if (requests.get(key)?.promise === promise) requests.delete(key);
    });
    request = { abort, listeners, progress, promise };
    requests.set(key, request);
  }
  const active = request;
  active.listeners.add(onEvent);
  for (const event of active.progress.values()) onEvent(event);
  return {
    promise: active.promise,
    stop: () => active.abort.abort(),
    unsubscribe() {
      active.listeners.delete(onEvent);
      queueMicrotask(() => {
        if (!active.listeners.size) active.abort.abort();
      });
    },
  };
}
