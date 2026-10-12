export class TeaApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
    this.name = "TeaApiError";
  }
}
async function teaFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/api/tea${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new TeaApiError(
      0,
      "network",
      "暂时无法连接服务，请检查网络后重试。你的输入会保留。",
    );
  }
  if (!response.ok) {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      payload = undefined;
    }
    if (
      response.status === 401 &&
      typeof window !== "undefined" &&
      !path.startsWith("/auth/") &&
      path !== "/bootstrap"
    )
      window.dispatchEvent(new Event("tea:session-expired"));
    const data = payload as
      | { error?: { code?: string; message?: string }; detail?: string }
      | undefined;
    throw new TeaApiError(
      response.status,
      data?.error?.code ?? "request_failed",
      data?.error?.message ??
        (typeof data?.detail === "string"
          ? data.detail
          : response.status === 401
            ? "登录已过期，请重新登录。"
            : "服务暂时无法处理请求，请稍后重试。"),
    );
  }
  return response;
}
export async function teaRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await teaFetch(path, options);
  if (response.status === 204) return undefined as T;
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = undefined;
  }
  if (payload === undefined)
    throw new TeaApiError(
      502,
      "invalid_response",
      "服务返回了无法读取的内容，请稍后重试。",
    );
  return payload as T;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalidStream() {
  return new TeaApiError(
    502,
    "invalid_response",
    "服务返回了无法读取的执行结果，请重试。",
  );
}

// Both tea consumers use the same framing, cancellation and resource limits.
// Progress does not turn an incomplete stream into a successful result.
async function streamTea<T>(
  path: string,
  body: unknown,
  {
    signal,
    maxBytes,
    maxDurationMs,
    onFrame,
  }: {
    signal?: AbortSignal;
    maxBytes: number;
    maxDurationMs?: number;
    onFrame: (event: string, data: Record<string, unknown>) => T | undefined;
  },
): Promise<T> {
  const abort = new AbortController();
  const cancel = () => abort.abort(signal?.reason);
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  let timedOut = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = () => {
    timedOut = true;
    abort.abort();
  };
  const resetTimeout = () => {
    clearTimeout(timer);
    timer = setTimeout(timeout, 60_000);
  };
  resetTimeout();
  const deadline = maxDurationMs
    ? setTimeout(timeout, maxDurationMs)
    : undefined;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let readerCancelled = false;
  const cancelReader = () => {
    if (!reader || readerCancelled) return;
    readerCancelled = true;
    // A misbehaving source must not delay local cancellation or timeout feedback.
    void reader.cancel().catch(() => undefined);
  };
  try {
    abort.signal.throwIfAborted();
    const response = await teaFetch(path, {
      method: "POST",
      body: JSON.stringify(body),
      signal: abort.signal,
      headers: { Accept: "text/event-stream" },
    });
    abort.signal.throwIfAborted();
    if (
      !response.body ||
      !response.headers.get("Content-Type")?.includes("text/event-stream")
    )
      throw invalidStream();
    reader = response.body.getReader();
    abort.signal.addEventListener("abort", cancelReader, { once: true });
    const decoder = new TextDecoder();
    let buffer = "";
    let bytesReceived = 0;
    while (true) {
      const { value, done } = await reader.read();
      abort.signal.throwIfAborted();
      if (done) break;
      resetTimeout();
      bytesReceived += value.byteLength;
      if (bytesReceived > maxBytes)
        throw new TeaApiError(
          502,
          "invalid_response",
          "执行结果过大，请缩小资料范围后重试。",
        );
      buffer += decoder.decode(value, { stream: true });
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        if (boundary.index > 8 * 1024 * 1024) throw invalidStream();
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let event = "";
        const lines: string[] = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:"))
            lines.push(line.slice(5).replace(/^ /, ""));
        }
        if (!lines.length) continue; // heartbeats
        let data: unknown;
        try {
          data = JSON.parse(lines.join("\n"));
        } catch {
          throw invalidStream();
        }
        if (!record(data)) throw invalidStream();
        if (event === "error")
          throw new TeaApiError(
            502,
            typeof data.code === "string" ? data.code : "stream_failed",
            typeof data.message === "string"
              ? data.message
              : "执行未能完成，请重试。",
          );
        const result = onFrame(event, data);
        abort.signal.throwIfAborted();
        if (result !== undefined) return result;
      }
      if (buffer.length > 8 * 1024 * 1024) throw invalidStream();
    }
    throw new TeaApiError(
      502,
      "stream_interrupted",
      "连接中断，结果尚未完整返回。已收到的内容已保留，请重试。",
    );
  } catch (error) {
    if (timedOut)
      throw new TeaApiError(
        504,
        "stream_timeout",
        "等待执行结果超时，已收到的内容已保留。请重试。",
      );
    if (abort.signal.aborted) throw abort.signal.reason;
    if (error instanceof TeaApiError) throw error;
    throw new TeaApiError(
      502,
      "stream_interrupted",
      "连接中断，结果尚未完整返回。已收到的内容已保留，请重试。",
    );
  } finally {
    clearTimeout(timer);
    clearTimeout(deadline);
    signal?.removeEventListener("abort", cancel);
    abort.signal.removeEventListener("abort", cancelReader);
    cancelReader();
    reader?.releaseLock();
  }
}

export function streamMessage(
  caseId: string,
  body: MessageInput,
  {
    signal,
    onEvent,
  }: { signal?: AbortSignal; onEvent: (event: MessageStreamEvent) => void },
): Promise<MessageResult> {
  return streamTea(
    `/cases/${encodeURIComponent(caseId)}/messages/stream`,
    body,
    {
      signal,
      maxBytes: 8 * 1024 * 1024,
      onFrame(event, data) {
        if (event === "done") {
          if (!Array.isArray(data.messages) || !record(data.case))
            throw invalidStream();
          return data as unknown as MessageResult;
        }
        if (event === "delta" && typeof data.text === "string")
          onEvent({ event, data: { text: data.text } });
        else if (event === "status" && typeof data.label === "string")
          onEvent({ event, data: { label: data.label } });
        else if (event === "start" && typeof data.step_id === "string")
          onEvent({ event, data: { step_id: data.step_id } });
      },
    },
  );
}

function isTraceStep(data: unknown): data is TraceStep {
  return (
    record(data) &&
    typeof data.id === "string" &&
    typeof data.label === "string" &&
    typeof data.detail === "string" &&
    typeof data.status === "string" &&
    [
      "completed",
      "skipped",
      "failed",
      "running",
      "cached",
      "cancelled",
    ].includes(data.status) &&
    (data.type === undefined ||
      (typeof data.type === "string" &&
        ["retrieve", "check", "guide", "draft", "verify"].includes(
          data.type,
        ))) &&
    (data.duration_ms === undefined ||
      (typeof data.duration_ms === "number" &&
        Number.isFinite(data.duration_ms) &&
        data.duration_ms >= 0)) &&
    (data.input === undefined || typeof data.input === "string") &&
    (data.output === undefined || typeof data.output === "string") &&
    (data.input_data === undefined || record(data.input_data)) &&
    (data.output_data === undefined || record(data.output_data))
  );
}

function isDebugRun(data: Record<string, unknown>): boolean {
  return (
    typeof data.id === "string" &&
    typeof data.created_at === "string" &&
    typeof data.stage_id === "string" &&
    typeof data.output === "string" &&
    typeof data.status === "string" &&
    ["completed", "paused", "failed", "cancelled"].includes(data.status) &&
    ["demo", "live"].includes(String(data.assistant_mode)) &&
    record(data.request) &&
    record(data.request.template) &&
    typeof data.request.template.id === "string" &&
    Array.isArray(data.request.template.stages) &&
    typeof data.request.stage_id === "string" &&
    typeof data.request.input === "string" &&
    record(data.request.task_values) &&
    (data.request.document_ids === null ||
      (Array.isArray(data.request.document_ids) &&
        data.request.document_ids.every((id) => typeof id === "string"))) &&
    Array.isArray(data.request.expectations) &&
    Array.isArray(data.trace) &&
    data.trace.every(isTraceStep) &&
    Array.isArray(data.checks) &&
    data.checks.every(
      (item) =>
        record(item) &&
        typeof item.id === "string" &&
        typeof item.label === "string" &&
        ["passed", "missing", "conflict"].includes(String(item.status)) &&
        typeof item.reason === "string" &&
        Array.isArray(item.evidence_refs),
    ) &&
    Array.isArray(data.citations) &&
    data.citations.every(
      (item) =>
        record(item) &&
        typeof item.document_id === "string" &&
        typeof item.title === "string" &&
        typeof item.excerpt === "string" &&
        record(item.locator),
    ) &&
    Array.isArray(data.evaluations) &&
    data.evaluations.every(
      (item) =>
        record(item) &&
        typeof item.id === "string" &&
        typeof item.label === "string" &&
        ["passed", "failed", "manual"].includes(String(item.status)) &&
        typeof item.actual === "string",
    ) &&
    Array.isArray(data.document_versions) &&
    data.document_versions.every(
      (item) =>
        record(item) &&
        typeof item.document_id === "string" &&
        typeof item.version === "number" &&
        typeof item.metadata_revision === "number",
    )
  );
}

/** Progress is delivered immediately; protocol/server failures reject the promise. */
export function streamDebug(
  request: DebugRequest,
  onEvent: (event: DebugStreamEvent) => void,
  signal?: AbortSignal,
): Promise<DebugRun> {
  return streamTea("/templates/debug/stream", request, {
    signal,
    maxBytes: 32 * 1024 * 1024,
    // Up to 20 configured steps can each spend 45 seconds waiting on a model.
    maxDurationMs: 20 * 60_000,
    onFrame(event, data) {
      if (event === "done") {
        if (!isDebugRun(data)) throw invalidStream();
        const run = data as unknown as DebugRun;
        onEvent({ event, data: run });
        return run;
      }
      if (event === "step") {
        if (!isTraceStep(data)) throw invalidStream();
        onEvent({ event, data });
      } else if (event === "run") {
        if (typeof data.id !== "string") throw invalidStream();
        onEvent({ event, data: { id: data.id } });
      } else if (event === "delta" || event === "status") {
        if (data.step_id !== undefined && typeof data.step_id !== "string")
          throw invalidStream();
        const scope =
          data.step_id === undefined ? {} : { step_id: data.step_id };
        if (event === "delta") {
          if (typeof data.text !== "string") throw invalidStream();
          onEvent({ event, data: { text: data.text, ...scope } });
        } else {
          if (typeof data.label !== "string") throw invalidStream();
          onEvent({ event, data: { label: data.label, ...scope } });
        }
      }
    },
  });
}
export function post<T>(
  path: string,
  body: unknown = {},
  signal?: AbortSignal,
) {
  return teaRequest<T>(path, {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });
}
export function patch<T>(path: string, body: unknown, signal?: AbortSignal) {
  return teaRequest<T>(path, {
    method: "PATCH",
    body: JSON.stringify(body),
    signal,
  });
}
export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}
export function downloadUrl(
  id: string,
  kind: "documents" | "artifacts" = "documents",
) {
  return `/api/tea/${kind}/${encodeURIComponent(id)}/download`;
}
export function safeSourceUrl(url?: string | null) {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.href
      : undefined;
  } catch {
    return undefined;
  }
}
import type {
  DebugRequest,
  DebugRun,
  DebugStreamEvent,
  MessageInput,
  MessageResult,
  MessageStreamEvent,
  TraceStep,
} from "./types";
