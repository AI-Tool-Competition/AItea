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

// Tea consumers use the same framing, cancellation and resource limits.
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
        else if (event === "step") {
          if (!isTraceStep(data)) throw invalidStream();
          onEvent({ event, data });
        }
      },
    },
  );
}

function isToolCall(data: unknown): data is ToolCall {
  return (
    record(data) &&
    typeof data.id === "string" &&
    data.id.length > 0 &&
    data.id.length <= 100 &&
    [
      "search_documents",
      "search_legal_evidence",
      "check_materials",
      "generate_guidance",
    ].includes(String(data.name)) &&
    typeof data.label === "string" &&
    data.label.length <= 100 &&
    ["running", "retrying", "completed", "failed", "cancelled"].includes(
      String(data.status),
    ) &&
    Number.isSafeInteger(data.position) &&
    Number(data.position) > 0 &&
    Number.isSafeInteger(data.attempt) &&
    Number(data.attempt) > 0 &&
    Number.isSafeInteger(data.max_attempts) &&
    Number(data.max_attempts) <= 2 &&
    Number(data.max_attempts) >= Number(data.attempt) &&
    typeof data.detail === "string" &&
    data.detail.length <= 400 &&
    typeof data.duration_ms === "number" &&
    Number.isFinite(data.duration_ms) &&
    data.duration_ms >= 0 &&
    (data.error_code === undefined ||
      (typeof data.error_code === "string" && data.error_code.length <= 64))
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
    (data.run_id === undefined ||
      (typeof data.run_id === "string" && data.run_id.length <= 64)) &&
    (data.sequence === undefined ||
      (Number.isSafeInteger(data.sequence) && Number(data.sequence) > 0)) &&
    (data.tools === undefined ||
      (Array.isArray(data.tools) &&
        data.tools.length <= 128 &&
        data.tools.every(isToolCall) &&
        new Set(data.tools.map((tool) => tool.id)).size ===
          data.tools.length)) &&
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
export interface DocumentSearchInput {
  query: string;
  limit?: number;
  case_id?: string;
  country?: string;
  source_scope?: "personal" | "system";
}

export function searchDocuments(
  input: DocumentSearchInput,
  signal?: AbortSignal,
) {
  return post<SearchResult>("/search", input, signal);
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
export function documentImageUrl(documentId: string, imageId: string) {
  return `/api/tea/documents/${encodeURIComponent(documentId)}/images/${encodeURIComponent(imageId)}`;
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
  LegalAnalysis,
  LegalFact,
  LegalFactInput,
  LegalHistory,
  LegalPolicyDefaults,
  LegalResult,
  LegalStreamEvent,
  Template,
  DebugRequest,
  DebugRun,
  DebugStreamEvent,
  MessageInput,
  MessageResult,
  MessageStreamEvent,
  TraceStep,
  ToolCall,
  StageAnalysis,
  SearchResult,
} from "./types";

const legalPath = (caseId: string) =>
  `/cases/${encodeURIComponent(caseId)}/legal`;
export const getLegalPolicy = (signal?: AbortSignal) =>
  teaRequest<LegalPolicyDefaults>("/legal/policy", { signal });
export function getLegalHistory(
  caseId: string,
  stageId: string,
  offset = 0,
  signal?: AbortSignal,
) {
  return teaRequest<LegalHistory>(
    `${legalPath(caseId)}?stage_id=${encodeURIComponent(stageId)}&limit=20&offset=${offset}`,
    { signal },
  );
}
export function getLegalAnalysis(
  caseId: string,
  analysisId: string,
  signal?: AbortSignal,
) {
  return teaRequest<LegalAnalysis>(
    `${legalPath(caseId)}/analyses/${encodeURIComponent(analysisId)}`,
    { signal },
  );
}
function strings(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}
function legalFactShape(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.key === "string" &&
    typeof value.label === "string" &&
    typeof value.source === "string" &&
    (value.value === null ||
      typeof value.value === "string" ||
      (typeof value.value === "number" && Number.isFinite(value.value))) &&
    typeof value.confirmed === "boolean" &&
    ["unknown", "low", "medium", "high"].includes(
      String(value.extraction_confidence),
    ) &&
    ["unverified", "verified", "conflict"].includes(
      String(value.evidence_status),
    ) &&
    strings(value.evidence_refs) &&
    Array.isArray(value.conflicts) &&
    value.conflicts.every(
      (item) =>
        record(item) &&
        typeof item.source === "string" &&
        strings(item.evidence_refs),
    ) &&
    (value.resolved_conflicts === undefined ||
      (Array.isArray(value.resolved_conflicts) &&
        value.resolved_conflicts.every(
          (item) =>
            record(item) &&
            typeof item.source === "string" &&
            strings(item.evidence_refs),
        ))) &&
    typeof value.revision === "number" &&
    Number.isInteger(value.revision) &&
    value.revision >= 0
  );
}
function legalRequirementShape(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.key === "string" &&
    typeof value.label === "string" &&
    typeof value.purpose === "string" &&
    typeof value.decisive === "boolean"
  );
}
function legalCitationShape(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.document_id === "string" &&
    typeof value.title === "string" &&
    typeof value.excerpt === "string" &&
    record(value.locator)
  );
}
function legalSectionShape(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.id === "string" &&
    typeof value.issue_id === "string" &&
    typeof value.title === "string" &&
    typeof value.content === "string"
  );
}
function legalIssueShape(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.id === "string" &&
    typeof value.question === "string" &&
    ["information", "legal"].includes(String(value.kind)) &&
    ["supported", "unsupported", "conditional", "undetermined"].includes(
      String(value.status),
    ) &&
    typeof value.conclusion === "string" &&
    typeof value.explanation === "string" &&
    typeof value.needs_review === "boolean" &&
    strings(value.domains) &&
    strings(value.jurisdictions) &&
    strings(value.fact_keys) &&
    strings(value.gaps) &&
    Array.isArray(value.missing_facts) &&
    value.missing_facts.every(legalRequirementShape) &&
    Array.isArray(value.citations) &&
    value.citations.every(legalCitationShape) &&
    Array.isArray(value.conditions) &&
    value.conditions.every(
      (item) =>
        record(item) &&
        typeof item.label === "string" &&
        typeof item.reason === "string" &&
        ["met", "not_met", "unknown", "not_applicable"].includes(
          String(item.state),
        ) &&
        strings(item.citation_ids) &&
        strings(item.fact_keys),
    ) &&
    Array.isArray(value.actions) &&
    value.actions.every(
      (item) =>
        record(item) &&
        typeof item.id === "string" &&
        typeof item.title === "string" &&
        typeof item.details === "string" &&
        [
          "mandatory",
          "conditional",
          "recommended",
          "commercial",
          "unverified",
        ].includes(String(item.nature)),
    ) &&
    Array.isArray(value.deadlines) &&
    value.deadlines.every(
      (item) =>
        record(item) &&
        typeof item.id === "string" &&
        typeof item.label === "string" &&
        ["calculated", "conditional", "unavailable"].includes(
          String(item.status),
        ) &&
        (item.date === null || typeof item.date === "string") &&
        (item.timezone === null || typeof item.timezone === "string") &&
        strings(item.assumptions) &&
        strings(item.missing_prerequisites) &&
        strings(item.citation_ids),
    )
  );
}
function legalAnalysisShape(value: unknown): value is LegalAnalysis {
  if (!record(value) || !record(value.result)) return false;
  const result = value.result;
  return (
    typeof value.id === "string" &&
    typeof value.case_id === "string" &&
    typeof value.stage_id === "string" &&
    typeof value.question === "string" &&
    typeof value.revision === "number" &&
    Number.isInteger(value.revision) &&
    value.revision >= 1 &&
    [
      "running",
      "completed",
      "needs_input",
      "needs_review",
      "failed",
      "cancelled",
    ].includes(String(value.status)) &&
    typeof value.created_at === "string" &&
    typeof value.updated_at === "string" &&
    (value.error === null || typeof value.error === "string") &&
    strings(value.affected_issue_ids) &&
    Array.isArray(value.sections) &&
    value.sections.every(
      (item) =>
        legalSectionShape(item) &&
        record(item) &&
        typeof item.generated_content === "string" &&
        (item.suggested_content === null ||
          typeof item.suggested_content === "string") &&
        typeof item.edited === "boolean" &&
        typeof item.needs_review === "boolean",
    ) &&
    typeof result.question === "string" &&
    typeof result.output === "string" &&
    ["demo", "live"].includes(String(result.assistant_mode)) &&
    Array.isArray(result.issues) &&
    result.issues.every(legalIssueShape) &&
    Array.isArray(result.facts) &&
    result.facts.every(legalFactShape) &&
    Array.isArray(result.followups) &&
    result.followups.every(legalRequirementShape) &&
    Array.isArray(result.sections) &&
    result.sections.every(legalSectionShape) &&
    Array.isArray(result.trace) &&
    result.trace.every(isTraceStep) &&
    strings(result.gaps) &&
    Array.isArray(result.citations) &&
    result.citations.every(legalCitationShape)
  );
}

export function streamStageAnalysis(
  caseId: string,
  stageId: string,
  request: { expected_revision: number; retry: boolean },
  options: { signal: AbortSignal; onEvent: (event: LegalStreamEvent) => void },
): Promise<StageAnalysis> {
  return streamTea(
    `/cases/${encodeURIComponent(caseId)}/stages/${encodeURIComponent(stageId)}/analysis/stream`,
    request,
    {
      signal: options.signal,
      maxBytes: 32 * 1024 * 1024,
      maxDurationMs: 20 * 60_000,
      onFrame(event, data) {
        if (event === "done") {
          const value = data.analysis;
          if (
            !record(value) ||
            value.case_id !== caseId ||
            value.stage_id !== stageId ||
            typeof value.id !== "string" ||
            typeof value.output !== "string" ||
            typeof value.legal_analysis_id !== "string" ||
            !Number.isInteger(value.legal_revision) ||
            !Number.isInteger(value.case_revision) ||
            typeof value.created_at !== "string" ||
            !["completed", "failed"].includes(String(value.status)) ||
            !["live", "demo"].includes(String(value.assistant_mode)) ||
            !Array.isArray(value.trace) ||
            !value.trace.every(isTraceStep) ||
            !Array.isArray(value.citations) ||
            !value.citations.every(legalCitationShape) ||
            !Array.isArray(value.checks)
          )
            throw invalidStream();
          return value as unknown as StageAnalysis;
        }
        if (event === "step") {
          if (!isTraceStep(data)) throw invalidStream();
          options.onEvent({ event, data });
        } else if (event === "status") {
          if (typeof data.label !== "string") throw invalidStream();
          options.onEvent({ event, data: { label: data.label } });
        }
      },
    },
  );
}

export function streamLegalAnalysis(
  caseId: string,
  request:
    | { stage_id: string; question: string; client_request_id: string }
    | { expected_revision: number; client_request_id: string },
  {
    analysisId,
    signal,
    onEvent,
  }: {
    analysisId?: string;
    signal?: AbortSignal;
    onEvent: (event: LegalStreamEvent) => void;
  },
): Promise<LegalAnalysis> {
  const path = analysisId
    ? `${legalPath(caseId)}/analyses/${encodeURIComponent(analysisId)}/refresh/stream`
    : `${legalPath(caseId)}/analyses/stream`;
  return streamTea(path, request, {
    signal,
    maxBytes: 32 * 1024 * 1024,
    maxDurationMs: 20 * 60_000,
    onFrame(event, data) {
      if (event === "done") {
        const value = data.analysis;
        if (
          !legalAnalysisShape(value) ||
          value.case_id !== caseId ||
          ("stage_id" in request && value.stage_id !== request.stage_id) ||
          (analysisId !== undefined && value.id !== analysisId)
        )
          throw invalidStream();
        return value;
      }
      if (event === "step") {
        if (!isTraceStep(data)) throw invalidStream();
        onEvent({ event, data });
      } else if (event === "status") {
        if (typeof data.label !== "string") throw invalidStream();
        onEvent({ event, data: { label: data.label } });
      }
    },
  });
}
export function saveLegalFact(
  caseId: string,
  key: string,
  input: LegalFactInput,
  signal?: AbortSignal,
) {
  return patch<{ fact: LegalFact; affected_analysis_ids: string[] }>(
    `${legalPath(caseId)}/facts/${encodeURIComponent(key)}`,
    input,
    signal,
  );
}
export function saveLegalSection(
  caseId: string,
  analysisId: string,
  sectionId: string,
  input: {
    expected_revision: number;
    content: string;
    accept_suggestion?: boolean;
  },
  signal?: AbortSignal,
) {
  return patch<LegalAnalysis>(
    `${legalPath(caseId)}/analyses/${encodeURIComponent(analysisId)}/sections/${encodeURIComponent(sectionId)}`,
    input,
    signal,
  );
}
export function getLegalVersions(
  caseId: string,
  analysisId: string,
  offset = 0,
  signal?: AbortSignal,
) {
  return teaRequest<{
    items: { revision: number; created_at: string }[];
    total: number;
  }>(
    `${legalPath(caseId)}/analyses/${encodeURIComponent(analysisId)}/versions?limit=20&offset=${offset}`,
    { signal },
  );
}
export function previewLegalAnalysis(
  template: Template,
  stageId: string,
  input: string,
  signal?: AbortSignal,
) {
  return post<LegalResult>(
    "/templates/legal/preview",
    { template, stage_id: stageId, input },
    signal,
  );
}
