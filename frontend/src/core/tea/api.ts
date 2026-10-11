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

export async function streamMessage(
  caseId: string,
  body: MessageInput,
  {
    signal,
    onEvent,
  }: { signal?: AbortSignal; onEvent: (event: MessageStreamEvent) => void },
): Promise<MessageResult> {
  const abort = new AbortController();
  const cancel = () => abort.abort(signal?.reason);
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  let timedOut = false;
  let timer: ReturnType<typeof setTimeout>;
  const resetTimeout = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timedOut = true;
      abort.abort();
    }, 60_000);
  };
  resetTimeout();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const cancelReader = () => {
    void reader?.cancel().catch(() => undefined);
  };
  try {
    const response = await teaFetch(
      `/cases/${encodeURIComponent(caseId)}/messages/stream`,
      {
        method: "POST",
        body: JSON.stringify(body),
        signal: abort.signal,
        headers: { Accept: "text/event-stream" },
      },
    );
    abort.signal.throwIfAborted();
    if (
      !response.body ||
      !response.headers.get("Content-Type")?.includes("text/event-stream")
    )
      throw new TeaApiError(
        502,
        "invalid_response",
        "无法读取流式回复，请重试。",
      );
    reader = response.body.getReader();
    abort.signal.addEventListener("abort", cancelReader, { once: true });
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      abort.signal.throwIfAborted();
      if (done) break;
      resetTimeout();
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 8 * 1024 * 1024)
        throw new TeaApiError(
          502,
          "invalid_response",
          "回复内容过大，请缩小问题范围后重试。",
        );
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
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
        const data = JSON.parse(lines.join("\n")) as Record<string, unknown>;
        if (event === "error")
          throw new TeaApiError(
            502,
            typeof data.code === "string" ? data.code : "stream_failed",
            typeof data.message === "string"
              ? data.message
              : "回复未能完成，请重试。",
          );
        if (event === "done") {
          if (
            !Array.isArray(data.messages) ||
            !data.case ||
            typeof data.case !== "object"
          )
            throw new Error("Invalid completion");
          return data as unknown as MessageResult;
        }
        if (event === "delta" && typeof data.text === "string")
          onEvent({ event, data: { text: data.text } });
        else if (event === "status" && typeof data.label === "string")
          onEvent({ event, data: { label: data.label } });
        else if (event === "start" && typeof data.step_id === "string")
          onEvent({ event, data: { step_id: data.step_id } });
      }
    }
    throw new TeaApiError(
      502,
      "stream_interrupted",
      "连接中断，回复尚未完成。已收到的内容已保留，请重试。",
    );
  } catch (error) {
    if (timedOut)
      throw new TeaApiError(
        504,
        "stream_timeout",
        "等待回复超时，已收到的内容已保留。请重试。",
      );
    if (abort.signal.aborted) throw abort.signal.reason;
    if (error instanceof TeaApiError) throw error;
    throw new TeaApiError(
      502,
      "stream_interrupted",
      "连接中断，回复尚未完成。已收到的内容已保留，请重试。",
    );
  } finally {
    clearTimeout(timer!);
    signal?.removeEventListener("abort", cancel);
    abort.signal.removeEventListener("abort", cancelReader);
    await reader?.cancel().catch(() => undefined);
    reader?.releaseLock();
  }
}
export function post<T>(path: string, body: unknown = {}) {
  return teaRequest<T>(path, { method: "POST", body: JSON.stringify(body) });
}
export function patch<T>(path: string, body: unknown) {
  return teaRequest<T>(path, { method: "PATCH", body: JSON.stringify(body) });
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
import type { MessageInput, MessageResult, MessageStreamEvent } from "./types";
