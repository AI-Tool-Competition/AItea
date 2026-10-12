"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { errorMessage, streamDebug, TeaApiError, teaRequest } from "./api";
import type {
  AgentConfig,
  DebugExpectation,
  DebugRequest,
  DebugRun,
  DebugRunSummary,
  DebugSample,
  DebugSampleSummary,
  ListResult,
  Template,
  TraceStep,
} from "./types";

export interface DebugSampleDraft {
  name: string;
  input: string;
  task_values: Record<string, Record<string, string | number>>;
  document_ids: string[] | null;
  expectations: DebugExpectation[];
}
export interface BatchRow {
  id: string;
  name: string;
  result?: DebugRun;
  error?: string;
}
interface Session {
  key: string;
  sample: DebugSampleDraft;
  sampleId: string | null;
  revision: number;
  saved: string;
  result: DebugRun | null;
  resultRequest: DebugRequest | null;
  comparison: DebugRun | null;
  trace: TraceStep[];
}
const sessions = new Map<string, Session>();
const subscribers = new Set<() => void>();
let generation = 0;
function subscribe(listener: () => void) {
  subscribers.add(listener);
  return () => {
    subscribers.delete(listener);
  };
}
function getGeneration() {
  return generation;
}
export function clearDebuggerSessions() {
  generation++;
  sessions.clear();
  for (const listener of subscribers) listener();
}
function emptySample(): DebugSampleDraft {
  return {
    name: "",
    input: "请分析当前资料，告诉我还缺什么，以及下一步应该做什么。",
    task_values: {},
    document_ids: null,
    expectations: [],
  };
}
function initialSession(key: string): Session {
  const sample = emptySample();
  return (
    sessions.get(key) ?? {
      key,
      sample,
      sampleId: null,
      revision: 0,
      saved: signature(sample),
      result: null,
      resultRequest: null,
      comparison: null,
      trace: [],
    }
  );
}
function sampleData(value: DebugSampleDraft): DebugSampleDraft {
  return {
    name: value.name,
    input: value.input,
    task_values: value.task_values,
    document_ids: value.document_ids,
    expectations: value.expectations,
  };
}
function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value !== null && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .filter(([, item]) => item !== undefined)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, stable(item)]),
    );
  return value;
}
function expectations(value: DebugExpectation[]) {
  return value.map((item) => ({
    ...item,
    text: item.text ?? null,
    task_id: item.task_id ?? null,
    check_id: item.check_id ?? null,
    status: item.status ?? null,
    document_id: item.document_id ?? null,
  }));
}
function documentIds(value: string[] | null) {
  return value === null ? null : [...new Set(value)].sort();
}
function signature(value: DebugSampleDraft) {
  return JSON.stringify(
    stable({
      ...sampleData(value),
      document_ids: documentIds(value.document_ids),
      expectations: expectations(value.expectations),
    }),
  );
}
function normalizedAgent(agent: AgentConfig): AgentConfig {
  return {
    ...agent,
    steps: agent.steps.map((step) => ({
      ...step,
      enabled: step.enabled ?? true,
      instructions: step.instructions ?? "",
    })),
  };
}
function normalizedTemplate(template: Template) {
  return {
    ...template,
    agent: normalizedAgent(template.agent),
    modes: template.modes.map((mode) => ({
      ...mode,
      enabled: mode.enabled ?? true,
    })),
    stages: template.stages.map((stage) => ({
      ...stage,
      analysis: stage.analysis ? normalizedAgent(stage.analysis) : null,
      tasks: stage.tasks.map((task) => ({
        ...task,
        required: task.required ?? true,
        require_document: task.require_document ?? false,
        require_confirmation: task.require_confirmation ?? true,
        output_template: task.output_template ?? null,
        fields: task.fields.map((field) => ({
          ...field,
          required: field.required ?? true,
          min: field.min ?? null,
          placeholder: field.placeholder ?? null,
        })),
      })),
    })),
  };
}
function inputsEqual(a: DebugRequest, b: DebugRequest) {
  const comparable = (value: DebugRequest) =>
    stable({
      template: normalizedTemplate(value.template),
      stage_id: value.stage_id,
      input: value.input,
      task_values: value.task_values,
      document_ids: documentIds(value.document_ids),
      expectations: expectations(value.expectations),
    });
  return JSON.stringify(comparable(a)) === JSON.stringify(comparable(b));
}
export type DebugRunMode = "full" | "to" | "step" | "continue";
type ListKind = "samples" | "history";

export function useWorkflowDebugger({
  ownerId,
  draft,
  stageId,
}: {
  ownerId: string;
  draft: Template;
  stageId: string;
}) {
  const sessionGeneration = useSyncExternalStore(
    subscribe,
    getGeneration,
    getGeneration,
  );
  const key = JSON.stringify([sessionGeneration, ownerId, draft.id, stageId]);
  const token = useMemo(
    () => ({ key, generation: sessionGeneration }),
    [key, sessionGeneration],
  );
  const [stored, setStored] = useState(() => initialSession(key));
  const data = stored.key === key ? stored : initialSession(key);
  const latest = useRef(data);
  latest.current = data;
  const scope = useRef(token);
  scope.current = token;
  const active = useRef<AbortController | null>(null);
  const execution = useRef<{
    abort: AbortController;
    request: DebugRequest;
    id: string | null;
  } | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const requests = useRef(new Set<AbortController>());
  const listRequests = useRef<Record<ListKind, AbortController | null>>({
    samples: null,
    history: null,
  });
  const listItems = useRef<{
    samples: DebugSampleSummary[];
    history: DebugRunSummary[];
  }>({ samples: [], history: [] });
  const [samples, setSamples] = useState<DebugSampleSummary[]>([]);
  const [history, setHistory] = useState<DebugRunSummary[]>([]);
  const [sampleTotal, setSampleTotal] = useState(0);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [progress, setProgress] = useState("");
  const [liveOutput, setLiveOutput] = useState("");
  const [batch, setBatch] = useState<BatchRow[]>([]);
  const [batchTotal, setBatchTotal] = useState(0);
  const prefix = `/templates/${encodeURIComponent(draft.id)}`;
  const stageQuery = `stage_id=${encodeURIComponent(stageId)}`;
  const mounted = useRef(false);

  function current() {
    return (
      mounted.current &&
      scope.current === token &&
      token.generation === generation
    );
  }
  function busy() {
    return !current() || !!active.current || !!mutation.current;
  }
  function adopt(change: Partial<Session>) {
    if (!current()) return;
    const next = { ...latest.current, ...change };
    latest.current = next;
    sessions.set(key, next);
    if (sessions.size > 50) sessions.delete(sessions.keys().next().value!);
    setStored(next);
  }
  function controller() {
    const abort = new AbortController();
    requests.current.add(abort);
    return abort;
  }
  function beginMutation() {
    const abort = controller();
    mutation.current = abort;
    setWorking(true);
    setError("");
    setNotice("");
    return abort;
  }
  function finishMutation(abort: AbortController) {
    requests.current.delete(abort);
    if (mutation.current !== abort) return;
    mutation.current = null;
    if (current()) setWorking(false);
  }
  async function refresh(
    append: ListKind | null = null,
    clearPreviousError = true,
  ) {
    if (!current()) return;
    setLoading(true);
    if (clearPreviousError) setError("");
    const targets: ListKind[] = append ? [append] : ["samples", "history"];
    await Promise.all(
      targets.map(async (kind) => {
        listRequests.current[kind]?.abort();
        const abort = controller();
        listRequests.current[kind] = abort;
        const offset = append === kind ? listItems.current[kind].length : 0;
        try {
          const page = await teaRequest<
            ListResult<DebugSampleSummary | DebugRunSummary>
          >(
            `${prefix}/debug-${kind === "samples" ? "samples" : "runs"}?${stageQuery}&limit=20&offset=${offset}`,
            { signal: abort.signal },
          );
          if (
            !current() ||
            abort.signal.aborted ||
            listRequests.current[kind] !== abort
          )
            return;
          if (kind === "samples") {
            const incoming = page.items as DebugSampleSummary[];
            const items = append
              ? [
                  ...new Map(
                    [...listItems.current.samples, ...incoming].map((item) => [
                      item.id,
                      item,
                    ]),
                  ).values(),
                ]
              : incoming;
            listItems.current.samples = items;
            setSamples(items);
            setSampleTotal(page.total);
          } else {
            const incoming = page.items as DebugRunSummary[];
            const items = append
              ? [
                  ...new Map(
                    [...listItems.current.history, ...incoming].map((item) => [
                      item.id,
                      item,
                    ]),
                  ).values(),
                ]
              : incoming;
            listItems.current.history = items;
            setHistory(items);
            setHistoryTotal(page.total);
          }
        } catch (e) {
          if (current() && !abort.signal.aborted) setError(errorMessage(e));
        } finally {
          requests.current.delete(abort);
          if (current() && listRequests.current[kind] === abort) {
            listRequests.current[kind] = null;
            setLoading(
              !!listRequests.current.samples || !!listRequests.current.history,
            );
          }
        }
      }),
    );
  }
  useEffect(() => {
    mounted.current = true;
    latest.current = initialSession(key);
    setStored(latest.current);
    listItems.current = { samples: [], history: [] };
    listRequests.current = { samples: null, history: null };
    setSamples([]);
    setHistory([]);
    setSampleTotal(0);
    setHistoryTotal(0);
    setLoading(true);
    setError("");
    setNotice("");
    setProgress("");
    setRunning(false);
    setWorking(false);
    setBatch([]);
    setBatchTotal(0);
    setLiveOutput("");
    void refresh();
    const pending = requests.current;
    return () => {
      mounted.current = false;
      for (const abort of pending) abort.abort();
      pending.clear();
      active.current = null;
      mutation.current = null;
      execution.current = null;
    };
    // Each scope owns its requests and saved input; draft edits must not reset it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const sampleDirty = signature(data.sample) !== data.saved;
  useEffect(() => {
    if (
      !sampleDirty ||
      (!data.sampleId && signature(data.sample) === signature(emptySample()))
    )
      return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [sampleDirty, data.sample, data.sampleId]);

  function editSample(changes: Partial<DebugSampleDraft>) {
    if (!current()) return;
    adopt({ sample: { ...latest.current.sample, ...changes } });
    setNotice("");
  }
  function newSample() {
    if (busy()) return;
    const sample = emptySample();
    adopt({
      sample,
      sampleId: null,
      revision: 0,
      saved: signature(sample),
      trace: [],
    });
    setError("");
    setNotice("");
  }
  async function saveSample(asNew = false) {
    if (busy()) return;
    const submitted = latest.current;
    if (!submitted.sample.name.trim()) {
      setError("请给测试样例起一个名称。");
      return;
    }
    const abort = beginMutation();
    try {
      const id =
        !asNew && submitted.sampleId ? submitted.sampleId : crypto.randomUUID();
      const saved = await teaRequest<DebugSample>(
        `${prefix}/debug-samples/${encodeURIComponent(id)}`,
        {
          method: "PUT",
          signal: abort.signal,
          body: JSON.stringify({
            ...submitted.sample,
            template: draft,
            stage_id: stageId,
            expected_revision: !asNew ? submitted.revision : 0,
          }),
        },
      );
      if (!current() || abort.signal.aborted) return;
      const next = sampleData(saved);
      adopt({
        sample:
          signature(latest.current.sample) === signature(submitted.sample)
            ? next
            : latest.current.sample,
        sampleId: saved.id,
        revision: saved.revision,
        saved: signature(next),
      });
      setNotice("样例已保存，资料版本已固定。");
      await refresh();
    } catch (e) {
      if (current() && !abort.signal.aborted) setError(errorMessage(e));
    } finally {
      finishMutation(abort);
    }
  }
  async function loadSample(id: string) {
    if (busy()) return;
    const submitted = latest.current.sample;
    const abort = beginMutation();
    try {
      const value = await teaRequest<DebugSample>(
        `${prefix}/debug-samples/${encodeURIComponent(id)}`,
        { signal: abort.signal },
      );
      if (!current() || abort.signal.aborted) return;
      if (signature(latest.current.sample) !== signature(submitted)) {
        setNotice("输入已修改，当前内容已保留。请重新选择样例。");
        return;
      }
      const sample = sampleData(value);
      adopt({
        sample,
        sampleId: value.id,
        revision: value.revision,
        saved: signature(sample),
        result: null,
        resultRequest: null,
        comparison: null,
        trace: [],
      });
    } catch (e) {
      if (current() && !abort.signal.aborted) setError(errorMessage(e));
    } finally {
      finishMutation(abort);
    }
  }
  async function deleteSample() {
    const submitted = latest.current;
    if (!submitted.sampleId || busy()) return;
    const abort = beginMutation();
    try {
      await teaRequest(
        `${prefix}/debug-samples/${encodeURIComponent(submitted.sampleId)}?expected_revision=${submitted.revision}`,
        { method: "DELETE", signal: abort.signal },
      );
      if (!current() || abort.signal.aborted) return;
      adopt({ sampleId: null, revision: 0, saved: "" });
      setNotice("样例已删除，当前输入仍保留。");
      await refresh();
    } catch (e) {
      if (current() && !abort.signal.aborted) setError(errorMessage(e));
    } finally {
      finishMutation(abort);
    }
  }
  function requestFor(sample = latest.current.sample): DebugRequest {
    return {
      template: draft,
      stage_id: stageId,
      input: sample.input,
      task_values: sample.task_values,
      document_ids: sample.document_ids,
      expectations: sample.expectations,
    };
  }
  function accept(result: DebugRun, submitted: DebugRequest) {
    adopt({
      comparison: latest.current.result ?? latest.current.comparison,
      result,
      resultRequest: submitted,
      trace: result.trace,
    });
    setLiveOutput("");
  }
  function markStopped() {
    adopt({
      trace: latest.current.trace.map((step) =>
        step.status === "running"
          ? { ...step, status: "cancelled", detail: "已停止" }
          : step,
      ),
    });
  }
  async function execute(request: DebugRequest, abort: AbortController) {
    abort.signal.throwIfAborted();
    const pending = { abort, request, id: null as string | null };
    execution.current = pending;
    return streamDebug(
      request,
      (event) => {
        if (!current() || abort.signal.aborted) return;
        if (event.event === "run") pending.id = event.data.id;
        else if (event.event === "step") {
          const old = latest.current.trace;
          const index = old.findIndex((step) => step.id === event.data.id);
          adopt({
            trace:
              index < 0
                ? [...old, event.data]
                : old.map((step) =>
                    step.id === event.data.id ? event.data : step,
                  ),
          });
          if (event.data.status === "running") setLiveOutput("");
        } else if (event.event === "status") setProgress(event.data.label);
        else if (event.event === "delta")
          setLiveOutput((text) => text + event.data.text);
      },
      abort.signal,
    );
  }
  async function recoverCheckpoint(stopped: AbortController) {
    const pending = execution.current;
    if (!current() || pending?.abort !== stopped || !pending.id) return;
    const abort = controller();
    const timer = setTimeout(() => abort.abort(), 3_000);
    setProgress("保存已完成的步骤…");
    let recovered = false;
    try {
      for (let attempt = 0; attempt < 5; attempt++) {
        if (!current() || abort.signal.aborted) return;
        try {
          const result = await teaRequest<DebugRun>(
            `/templates/debug/runs/${encodeURIComponent(pending.id)}`,
            { signal: abort.signal },
          );
          if (!current() || abort.signal.aborted) return;
          accept(result, pending.request);
          recovered = true;
          return;
        } catch (e) {
          if (!(e instanceof TeaApiError) || e.code !== "debug_in_progress")
            return;
          if (attempt === 4) return;
          await new Promise<void>((resolve) => {
            const done = () => {
              clearTimeout(delay);
              abort.signal.removeEventListener("abort", done);
              resolve();
            };
            const delay = setTimeout(done, 200 * (attempt + 1));
            abort.signal.addEventListener("abort", done, { once: true });
            if (abort.signal.aborted) done();
          });
        }
      }
    } finally {
      clearTimeout(timer);
      requests.current.delete(abort);
      if (current()) {
        // A previous run cannot supply the visible stopped run's checkpoint.
        if (!recovered) {
          adopt({
            comparison: latest.current.result ?? latest.current.comparison,
            result: null,
            resultRequest: null,
          });
          setNotice("已停止。运行记录仍在保存，请稍后刷新历史记录。");
        }
        void refresh(null, false);
      }
    }
  }
  async function run(mode: DebugRunMode, stepId?: string) {
    if (busy()) return;
    if (mode !== "full" && !stepId) {
      setError("请先在画布中选择步骤。");
      return;
    }
    const submitted = latest.current;
    const request = { ...requestFor(), client_id: crypto.randomUUID() };
    if (submitted.sampleId && signature(submitted.sample) === submitted.saved) {
      request.sample_id = submitted.sampleId;
      request.sample_revision = submitted.revision;
    }
    if (mode === "to" || mode === "step") request.stop_after_step_id = stepId;
    if (mode === "step" || mode === "continue") {
      if (!submitted.result) {
        setError("请先运行到这一步，准备上游输入。");
        return;
      }
      request.source_run_id = submitted.result.id;
      request.start_step_id = stepId;
    }
    const abort = controller();
    active.current = abort;
    setRunning(true);
    setError("");
    setNotice("");
    setBatch([]);
    setBatchTotal(0);
    setLiveOutput("");
    setProgress("准备试运行…");
    adopt({ trace: [] });
    try {
      const result = await execute(request, abort);
      if (current() && !abort.signal.aborted) {
        accept(result, request);
        await refresh();
      }
    } catch (e) {
      if (!current()) return;
      if (abort.signal.aborted) {
        markStopped();
        setNotice("已停止，已收到的步骤结果仍可查看。");
      } else setError(errorMessage(e));
      await recoverCheckpoint(abort);
    } finally {
      requests.current.delete(abort);
      if (active.current === abort) {
        active.current = null;
        execution.current = null;
        if (current()) {
          setRunning(false);
          setProgress("");
        }
      }
    }
  }
  async function loadRun(id: string, comparison = false) {
    if (busy()) return;
    const abort = beginMutation();
    try {
      const result = await teaRequest<DebugRun>(
        `/templates/debug/runs/${encodeURIComponent(id)}`,
        { signal: abort.signal },
      );
      if (!current() || abort.signal.aborted) return;
      if (
        result.request.template.id !== draft.id ||
        result.stage_id !== stageId
      ) {
        setError("这条运行记录属于其他节点，请在对应节点查看。");
        return;
      }
      if (comparison) adopt({ comparison: result });
      else
        adopt({ result, resultRequest: result.request, trace: result.trace });
    } catch (e) {
      if (current() && !abort.signal.aborted) setError(errorMessage(e));
    } finally {
      finishMutation(abort);
    }
  }
  function restoreInputs() {
    if (busy()) return;
    const request = latest.current.result?.request;
    if (!request) return;
    adopt({
      sample: {
        name: "历史运行样例",
        input: request.input ?? "",
        task_values: request.task_values ?? {},
        document_ids: request.document_ids ?? null,
        expectations: request.expectations ?? [],
      },
      sampleId: null,
      revision: 0,
      saved: "",
    });
    setNotice("已载入历史输入，接下来使用当前配置试运行。");
  }
  async function runBatch() {
    if (busy()) return;
    const abort = controller();
    active.current = abort;
    setRunning(true);
    setError("");
    setNotice("");
    setBatch([]);
    setBatchTotal(0);
    setLiveOutput("");
    try {
      const all: DebugSampleSummary[] = [];
      do {
        const page = await teaRequest<ListResult<DebugSampleSummary>>(
          `${prefix}/debug-samples?${stageQuery}&limit=50&offset=${all.length}`,
          { signal: abort.signal },
        );
        if (!current() || abort.signal.aborted) break;
        if (page.total > 100)
          throw new Error("当前节点超过 100 个样例，请先整理样例再批量测试。");
        all.push(...page.items);
        if (all.length >= page.total || !page.items.length) break;
      } while (!abort.signal.aborted);
      if (!current()) return;
      if (abort.signal.aborted) {
        setNotice("批量测试已停止。");
        return;
      }
      setBatchTotal(all.length);
      if (!all.length) {
        setNotice("请先保存至少一个测试样例。");
        return;
      }
      for (const [index, item] of all.entries()) {
        if (!current() || abort.signal.aborted) break;
        execution.current = null;
        setProgress(`${index + 1} / ${all.length} · ${item.name}`);
        try {
          const sample = await teaRequest<DebugSample>(
            `${prefix}/debug-samples/${encodeURIComponent(item.id)}`,
            { signal: abort.signal },
          );
          if (!current() || abort.signal.aborted) break;
          const request = {
            ...requestFor(sample),
            client_id: crypto.randomUUID(),
            sample_id: sample.id,
            sample_revision: sample.revision,
          };
          adopt({ trace: [] });
          const result = await execute(request, abort);
          if (!current() || abort.signal.aborted) break;
          accept(result, request);
          setBatch((rows) => [
            ...rows,
            { id: item.id, name: item.name, result },
          ]);
        } catch (e) {
          if (!current() || abort.signal.aborted) break;
          setBatch((rows) => [
            ...rows,
            { id: item.id, name: item.name, error: errorMessage(e) },
          ]);
        }
      }
      if (current()) {
        if (abort.signal.aborted) {
          markStopped();
          setNotice("批量测试已停止，已完成的结果仍保留。");
          await recoverCheckpoint(abort);
        } else await refresh();
      }
    } catch (e) {
      if (current()) {
        if (abort.signal.aborted) {
          markStopped();
          setNotice("批量测试已停止。");
        } else setError(errorMessage(e));
      }
    } finally {
      requests.current.delete(abort);
      if (active.current === abort) {
        active.current = null;
        execution.current = null;
        if (current()) {
          setRunning(false);
          setProgress("");
        }
      }
    }
  }
  const stale =
    !!data.result &&
    !inputsEqual(data.resultRequest ?? data.result.request, requestFor());
  return {
    ...data,
    samples,
    sampleTotal,
    history,
    historyTotal,
    sampleDirty,
    loading,
    working,
    running,
    error,
    notice,
    progress,
    liveOutput,
    batch,
    batchTotal,
    stale,
    editSample,
    newSample,
    saveSample,
    loadSample,
    deleteSample,
    run,
    runBatch,
    loadRun,
    restoreInputs,
    refresh,
    stop: () => {
      if (current()) active.current?.abort();
    },
    clearError: () => {
      if (current()) setError("");
    },
  };
}
export type WorkflowDebugger = ReturnType<typeof useWorkflowDebugger>;
