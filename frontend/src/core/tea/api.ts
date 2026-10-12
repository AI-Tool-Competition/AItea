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
export async function teaRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
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
  if (response.status === 204) return undefined as T;
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = undefined;
  }
  if (!response.ok) {
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
  if (payload === undefined)
    throw new TeaApiError(
      502,
      "invalid_response",
      "服务返回了无法读取的内容，请稍后重试。",
    );
  return payload as T;
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
