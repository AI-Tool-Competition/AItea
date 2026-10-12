import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import type {
  DebugRequest,
  DebugRun,
  DebugSample,
  Template,
} from "@/core/tea/types";
import {
  useWorkflowDebugger,
  clearDebuggerSessions,
} from "@/core/tea/use-workflow-debugger";

const template: Template = {
  id: "tea",
  title: "办理",
  description: "",
  version: 1,
  origin: "中国",
  destination: "印度尼西亚",
  is_demo: true,
  modes: [{ id: "mode", name: "普通", description: "", enabled: true }],
  stages: [
    {
      id: "prepare",
      name: "准备",
      actor: "企业",
      description: "",
      tasks: [
        {
          id: "info",
          title: "基本资料",
          description: "",
          required: true,
          fields: [
            { key: "name", label: "企业名称", type: "text", required: true },
          ],
          require_document: false,
          require_confirmation: false,
        },
      ],
    },
  ],
  agent: {
    language: "中文",
    instructions: "",
    retrieval_limit: 3,
    steps: [
      { id: "check", label: "检查", type: "check", enabled: true },
      { id: "guide", label: "建议", type: "guide", enabled: true },
    ],
  },
};
const reply = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
function requestUrl(value: RequestInfo | URL) {
  return typeof value === "string"
    ? value
    : value instanceof URL
      ? value.href
      : value.url;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function sample(id = "sample-one"): DebugSample {
  return {
    id,
    name: "缺少企业名称",
    template_id: template.id,
    template,
    stage_id: "prepare",
    input: "请检查",
    task_values: {},
    document_ids: ["document-one"],
    expectations: [],
    revision: 2,
    updated_at: "2026-10-12T00:00:00Z",
    document_versions: [
      { document_id: "document-one", version: 1, metadata_revision: 1 },
    ],
  };
}
function completed(request: DebugRequest, id = "run-one"): DebugRun {
  return {
    id,
    created_at: "2026-10-12T00:00:00Z",
    status: "completed",
    request,
    sample_id: request.sample_id ?? null,
    source_run_id: request.source_run_id ?? null,
    stage_id: request.stage_id,
    assistant_mode: "demo",
    trace: [
      {
        id: "check",
        label: "检查",
        status: "completed",
        detail: "缺少企业名称",
      },
    ],
    output: "补充企业名称",
    checks: [],
    citations: [],
    evaluations: [],
    document_versions: [],
  };
}
function streamResult(run: DebugRun) {
  return new Response(`event: done\ndata: ${JSON.stringify(run)}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  });
}
afterEach(() => {
  cleanup();
  clearDebuggerSessions();
  rs.restoreAllMocks();
});

describe("workflow debugger", () => {
  it("clears private state and aborts in-flight requests immediately when sessions are cleared", async () => {
    const pending = deferred<Response>();
    let signal: AbortSignal | null | undefined;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method === "PUT") {
        signal = init.signal;
        return pending.promise;
      }
      return reply({ items: [], total: 0 });
    });
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() =>
      result.current.editSample({
        name: "私有内容",
        input: "不应留在退出后的页面",
      }),
    );
    let saving!: Promise<void>;
    act(() => {
      saving = result.current.saveSample();
    });
    act(() => clearDebuggerSessions());
    expect(signal?.aborted).toBe(true);
    expect(result.current.sample.input).not.toBe("不应留在退出后的页面");
    await act(async () => {
      pending.resolve(reply(sample()));
      await saving;
    });
    expect(result.current.sampleId).toBe(null);
    act(() => result.current.editSample({ input: "新会话仍可编辑" }));
    expect(result.current.sample.input).toBe("新会话仍可编辑");
  });

  it("synchronously locks saves and retains edits made before the response", async () => {
    const pending = deferred<Response>();
    const writes: RequestInit[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method === "PUT") {
        writes.push(init);
        return pending.promise;
      }
      return reply({ items: [], total: 0 });
    });
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.editSample({ name: "原样例", input: "原输入" }));
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.saveSample();
      second = result.current.saveSample();
      result.current.editSample({ input: "保存期间的新输入" });
    });
    expect(writes).toHaveLength(1);
    await act(async () => {
      pending.resolve(reply({ ...sample(), name: "原样例", input: "原输入" }));
      await Promise.all([first, second]);
    });
    expect(result.current.sample.input).toBe("保存期间的新输入");
    expect(result.current.sampleId).toBe("sample-one");
    expect(result.current.revision).toBe(2);
    expect(result.current.sampleDirty).toBe(true);
  });

  it.each(["owner", "stage", "template"])(
    "ignores an obsolete sample response after changing %s and switching back",
    async (changed) => {
      const pending = deferred<Response>();
      let signal: AbortSignal | null | undefined;
      rs.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
        if (requestUrl(url).endsWith("/debug-samples/old")) {
          signal = init?.signal;
          return pending.promise;
        }
        return reply({ items: [], total: 0 });
      });
      const original = { ownerId: "a", draft: template, stageId: "prepare" };
      const { result, rerender } = renderHook(
        (props) => useWorkflowDebugger(props),
        { initialProps: original },
      );
      await waitFor(() => expect(result.current.loading).toBe(false));
      let loading!: Promise<void>;
      act(() => {
        loading = result.current.loadSample("old");
      });
      rerender({
        ...original,
        ...(changed === "owner"
          ? { ownerId: "b" }
          : changed === "stage"
            ? { stageId: "other" }
            : { draft: { ...template, id: "other" } }),
      });
      expect(signal?.aborted).toBe(true);
      rerender(original);
      act(() => result.current.editSample({ input: "返回后的新输入" }));
      await act(async () => {
        pending.resolve(reply(sample("old")));
        await loading;
      });
      expect(result.current.sample.input).toBe("返回后的新输入");
      expect(result.current.sampleId).toBe(null);
    },
  );

  it("does not treat server defaults or resolved library IDs as a changed test", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method !== "POST") return reply({ items: [], total: 0 });
      const request = JSON.parse(init.body as string) as DebugRequest;
      const normalized = structuredClone(request);
      normalized.document_ids = ["document-one"];
      normalized.template.agent.steps.forEach((step) => {
        step.instructions ??= "";
      });
      normalized.template.stages.forEach((stage) => {
        stage.analysis ??= null;
      });
      normalized.expectations = normalized.expectations.map((value) => ({
        ...value,
        task_id: null,
        check_id: null,
        status: null,
        document_id: null,
      }));
      return streamResult(completed(normalized));
    });
    const { result, rerender } = renderHook(
      ({ draft }) =>
        useWorkflowDebugger({ ownerId: "a", draft, stageId: "prepare" }),
      { initialProps: { draft: template } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() =>
      result.current.editSample({
        expectations: [{ id: "expect", kind: "manual", text: "清楚说明缺项" }],
      }),
    );
    await act(async () => {
      await result.current.run("full");
    });
    expect(result.current.stale).toBe(false);
    rerender({
      draft: {
        ...template,
        agent: {
          ...template.agent,
          steps: template.agent.steps.map((step) => ({
            ...step,
            instructions: "",
          })),
        },
        stages: template.stages.map((stage) => ({ ...stage, analysis: null })),
      },
    });
    expect(result.current.stale).toBe(false);
    act(() => result.current.editSample({ input: "确实变更了问题" }));
    expect(result.current.stale).toBe(true);
  });

  it("uses current source IDs for single-step and continuation and detaches edited saved inputs", async () => {
    const requests: DebugRequest[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (requestUrl(url).endsWith("/debug-samples/sample-one"))
        return reply(sample());
      if (init?.method !== "POST") return reply({ items: [], total: 0 });
      const request = JSON.parse(init.body as string) as DebugRequest;
      requests.push(request);
      return streamResult(completed(request, `run-${requests.length}`));
    });
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.loadSample("sample-one");
      await result.current.run("full");
    });
    expect(requests[0]).toMatchObject({
      sample_id: "sample-one",
      sample_revision: 2,
      document_ids: ["document-one"],
    });
    await act(async () => {
      await result.current.run("step", "guide");
      await result.current.run("continue", "guide");
    });
    expect(requests[1]).toMatchObject({
      source_run_id: "run-1",
      start_step_id: "guide",
      stop_after_step_id: "guide",
    });
    expect(requests[2]).toMatchObject({
      source_run_id: "run-2",
      start_step_id: "guide",
    });
    expect(requests[2]?.stop_after_step_id).toBeUndefined();
    await act(async () => {
      result.current.editSample({ input: "修改后的问题" });
      await result.current.run("full");
    });
    expect(requests[3]).toMatchObject({
      input: "修改后的问题",
      document_ids: ["document-one"],
    });
    expect(requests[3]?.sample_id).toBeUndefined();
  });

  it("stops a batch during sample loading without starting another execution", async () => {
    const pending = deferred<Response>();
    const posts: string[] = [];
    let loadingSample = false;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (requestUrl(url).endsWith("/debug-samples/sample-one")) {
        loadingSample = true;
        return pending.promise;
      }
      if (requestUrl(url).includes("/debug-samples?"))
        return reply({
          items: [
            {
              id: "sample-one",
              name: "样例",
              stage_id: "prepare",
              revision: 2,
              updated_at: "now",
            },
          ],
          total: 1,
        });
      if (init?.method === "POST") {
        posts.push(requestUrl(url));
        return streamResult(
          completed(JSON.parse(init.body as string) as DebugRequest),
        );
      }
      return reply({ items: [], total: 0 });
    });
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    let batch!: Promise<void>;
    act(() => {
      batch = result.current.runBatch();
    });
    await waitFor(() => expect(loadingSample).toBe(true));
    act(() => result.current.stop());
    await act(async () => {
      pending.resolve(reply(sample()));
      await batch;
    });
    expect(posts).toHaveLength(0);
    expect(result.current.running).toBe(false);
    expect(result.current.notice).toContain("已停止");
  });

  it("recovers a stopped run checkpoint before continuing from its completed input", async () => {
    const requests: DebugRequest[] = [];
    let stoppedRequest!: DebugRequest;
    let recoveryReads = 0;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (requestUrl(url).endsWith("/templates/debug/runs/stopped-run")) {
        recoveryReads++;
        if (recoveryReads === 1)
          return reply(
            { error: { code: "debug_in_progress", message: "仍在保存" } },
            409,
          );
        return reply({
          ...completed(stoppedRequest, "stopped-run"),
          status: "cancelled",
        });
      }
      if (init?.method !== "POST") return reply({ items: [], total: 0 });
      const request = JSON.parse(init.body as string) as DebugRequest;
      requests.push(request);
      if (requests.length > 1)
        return streamResult(completed(request, "continued-run"));
      stoppedRequest = request;
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: run\ndata: {"id":"stopped-run"}\n\nevent: step\ndata: {"id":"check","label":"检查","status":"completed","detail":"缺少企业名称"}\n\nevent: step\ndata: {"id":"guide","label":"建议","status":"running","detail":"分析中"}\n\n',
              ),
            );
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    let running!: Promise<void>;
    act(() => {
      running = result.current.run("full");
    });
    await waitFor(() =>
      expect(
        result.current.trace.find((step) => step.id === "guide")?.status,
      ).toBe("running"),
    );
    await act(async () => {
      result.current.stop();
      await running;
    });
    expect(result.current.result?.id).toBe("stopped-run");
    expect(result.current.result?.status).toBe("cancelled");
    await act(async () => {
      await result.current.run("continue", "guide");
    });
    expect(requests[1]).toMatchObject({
      source_run_id: "stopped-run",
      start_step_id: "guide",
    });
    expect(recoveryReads).toBe(2);
  });

  it("clears live events and cancels the stream when the owner changes", async () => {
    const cancelled = rs.fn();
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method !== "POST") return reply({ items: [], total: 0 });
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              new TextEncoder().encode(
                'event: step\ndata: {"id":"guide","label":"建议","status":"running","detail":"旧账号的内容"}\n\nevent: delta\ndata: {"text":"旧账号的分析"}\n\n',
              ),
            );
          },
          cancel: cancelled,
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    const { result, rerender } = renderHook(
      ({ ownerId }) =>
        useWorkflowDebugger({ ownerId, draft: template, stageId: "prepare" }),
      { initialProps: { ownerId: "a" } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    let running!: Promise<void>;
    act(() => {
      running = result.current.run("full");
    });
    await waitFor(() => expect(result.current.liveOutput).toBe("旧账号的分析"));
    rerender({ ownerId: "b" });
    await act(async () => {
      await running;
    });
    expect(cancelled).toHaveBeenCalledTimes(1);
    expect(result.current.trace).toEqual([]);
    expect(result.current.liveOutput).toBe("");
    expect(result.current.result).toBe(null);
    expect(result.current.running).toBe(false);
  });

  it("restores historical input without adopting its older configuration or sample revision", async () => {
    const oldDraft = {
      ...template,
      agent: { ...template.agent, instructions: "旧的配置" },
    };
    const historical: DebugRequest = {
      template: oldDraft,
      stage_id: "prepare",
      input: "历史问题",
      task_values: { info: { name: "历史企业" } },
      document_ids: [],
      expectations: [{ id: "one", kind: "manual", text: "历史预期" }],
      sample_id: "old-sample",
      sample_revision: 3,
    };
    let sent: DebugRequest | null = null;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (requestUrl(url).endsWith("/templates/debug/runs/history-one"))
        return reply(completed(historical, "history-one"));
      if (init?.method === "POST") {
        sent = JSON.parse(init.body as string) as DebugRequest;
        return streamResult(completed(sent));
      }
      return reply({ items: [], total: 0 });
    });
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.loadRun("history-one");
    });
    act(() => result.current.restoreInputs());
    expect(result.current.sample).toMatchObject({
      input: "历史问题",
      task_values: { info: { name: "历史企业" } },
      document_ids: [],
    });
    expect(result.current.sampleId).toBe(null);
    expect(result.current.revision).toBe(0);
    expect(result.current.stale).toBe(true);
    await act(async () => {
      await result.current.run("full");
    });
    expect(sent).toMatchObject({
      input: "历史问题",
      template,
      document_ids: [],
    });
    expect(sent).not.toHaveProperty("sample_id");
  });

  it("retries failed history loading with visible loading state and clears the old error", async () => {
    const retry = deferred<Response>();
    let failed = true;
    rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      failed
        ? reply({ error: { code: "network", message: "暂时失败" } }, 503)
        : retry.promise.then((response) => response.clone()),
    );
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toContain("暂时失败");
    failed = false;
    let refreshing!: Promise<void>;
    act(() => {
      refreshing = result.current.refresh();
    });
    expect(result.current.loading).toBe(true);
    await act(async () => {
      retry.resolve(reply({ items: [], total: 0 }));
      await refreshing;
    });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe("");
  });
  it("keeps the edited example when saving conflicts", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) =>
      init?.method === "PUT"
        ? reply(
            {
              error: {
                code: "stale_sample",
                message: "样例已更新，请重新载入",
              },
            },
            409,
          )
        : reply({ items: [], total: 0 }),
    );
    const { result } = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() =>
      result.current.editSample({
        name: "缺少企业名称",
        input: "仍要保留的问题",
      }),
    );
    await act(async () => {
      await result.current.saveSample();
    });
    expect(result.current.sample.input).toBe("仍要保留的问题");
    expect(result.current.error).toContain("样例已更新");
    expect(result.current.sampleDirty).toBe(true);
  });
  it("retains unsaved examples across remounts without leaking to another owner", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      reply({ items: [], total: 0 }),
    );
    const first = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    act(() =>
      first.result.current.editSample({
        name: "私有样例",
        input: "未保存内容",
      }),
    );
    first.unmount();
    const second = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "a",
        draft: template,
        stageId: "prepare",
      }),
    );
    expect(second.result.current.sample.input).toBe("未保存内容");
    const other = renderHook(() =>
      useWorkflowDebugger({
        ownerId: "b",
        draft: template,
        stageId: "prepare",
      }),
    );
    expect(other.result.current.sample.input).not.toBe("未保存内容");
  });
  it("shows live steps and retains the completed result after a configuration edit", async () => {
    let finish!: () => void;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method !== "POST") return reply({ items: [], total: 0 });
      const request = JSON.parse(init.body as string);
      const encoder = new TextEncoder();
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                'event: step\ndata: {"id":"check","label":"检查","status":"completed","detail":"缺少企业名称","input_data":{"name":""}}\n\n',
              ),
            );
            finish = () => {
              controller.enqueue(
                encoder.encode(
                  `event: done\ndata: ${JSON.stringify({ id: "run1", created_at: "2026-10-12T00:00:00Z", status: "completed", request, stage_id: "prepare", assistant_mode: "demo", trace: [{ id: "check", label: "检查", status: "completed", detail: "缺少企业名称" }], output: "补充企业名称", checks: [], citations: [], evaluations: [], document_versions: [], sample_id: null, source_run_id: null })}\n\n`,
                ),
              );
              controller.close();
            };
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    const { result, rerender } = renderHook(
      ({ draft }) =>
        useWorkflowDebugger({ ownerId: "a", draft, stageId: "prepare" }),
      { initialProps: { draft: template } },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    let running!: Promise<void>;
    act(() => {
      running = result.current.run("full");
    });
    await waitFor(() =>
      expect(result.current.trace[0]?.status).toBe("completed"),
    );
    expect(result.current.running).toBe(true);
    await act(async () => {
      finish();
      await running;
    });
    expect(result.current.result?.output).toBe("补充企业名称");
    rerender({
      draft: {
        ...template,
        agent: { ...template.agent, instructions: "新的分析要求" },
      },
    });
    expect(result.current.result?.id).toBe("run1");
    expect(result.current.stale).toBe(true);
  });
});
