import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";

import { clearStudioDrafts, Studio } from "@/components/tea/studio";
import type {
  DebugRequest,
  DebugRun,
  Template,
  TraceStep,
} from "@/core/tea/types";

// Keep the real studio/debugger selection wiring; graph layout is unrelated to
// reading historical execution data and cannot be measured in a DOM simulator.
rs.mock("@/components/tea/analysis-editor", () => ({
  AnalysisEditor: ({
    debugContent,
    debugOpen,
    onStep,
    stepId,
    editRequest,
  }: {
    debugContent: ReactNode;
    debugOpen: boolean;
    onStep: (id: string) => void;
    stepId: string;
    editRequest: number;
  }) => (
    <div>
      <button onClick={() => onStep("b")}>选择画布步骤 B</button>
      <button onClick={() => onStep("c")}>选择画布步骤 C</button>
      <button onClick={() => onStep("qa03")}>选择法域分析</button>
      <output data-testid="canvas-step">{stepId}</output>
      <output data-testid="edit-request">{editRequest}</output>
      {debugOpen && debugContent}
    </div>
  ),
}));

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
    steps: [{ id: "b", label: "当前建议 B", type: "guide", enabled: true }],
  },
};
const trace: TraceStep[] = [
  {
    id: "a",
    label: "历史检查 A",
    status: "completed",
    detail: "检查历史资料",
    input_data: { question: "A 的完整输入" },
    output_data: { content: "A 的完整结果" },
  },
  {
    id: "b",
    label: "历史建议 B",
    status: "completed",
    detail: "根据历史资料建议",
    input_data: { question: "B 的完整输入" },
    output_data: { content: "B 的完整结果" },
  },
];
const historical: DebugRun = {
  id: "history-one",
  created_at: "2026-10-12T00:00:00Z",
  status: "completed",
  stage_id: "prepare",
  assistant_mode: "demo",
  output: "历史最终建议",
  request: {
    template: {
      ...template,
      agent: {
        ...template.agent,
        steps: [
          { id: "a", label: "历史检查 A", type: "check", enabled: true },
          ...template.agent.steps,
        ],
      },
    },
    stage_id: "prepare",
    input: "历史测试",
    task_values: {},
    document_ids: [],
    expectations: [],
  },
  trace,
  checks: [],
  citations: [],
  evaluations: [],
  document_versions: [],
  sample_id: null,
  source_run_id: null,
};
function setupHistory(run = historical, currentTemplate = template) {
  const sent: DebugRequest[] = [];
  rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
    const path =
      typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
    if (path.endsWith("/templates/debug/runs/history-one"))
      return Response.json(run);
    if (path.endsWith("/templates/tea/draft"))
      return Response.json({
        template: currentTemplate,
        revision: 0,
        updated_at: null,
      });
    if (path.includes("/debug-runs?"))
      return Response.json({
        items: [
          {
            id: run.id,
            created_at: run.created_at,
            status: run.status,
            stage_id: run.stage_id,
            sample_id: null,
            evaluation_status: "none",
          },
        ],
        total: 1,
      });
    if (options?.method === "POST") {
      const request = JSON.parse(options.body as string) as DebugRequest;
      sent.push(request);
      return new Response(
        `event: done\ndata: ${JSON.stringify({ ...run, id: "rerun", request, trace: [trace[1]] })}\n\n`,
        { headers: { "Content-Type": "text/event-stream" } },
      );
    }
    return Response.json({ items: [], total: 0 });
  });
  render(
    <Studio
      templates={[currentTemplate]}
      ownerId="history-test"
      onPublished={() => undefined}
      page="analysis"
    />,
  );
  return sent;
}
async function openHistoricalResult() {
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "调试" }).hasAttribute("disabled"),
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "调试" }));
  fireEvent.click(screen.getByRole("tab", { name: "记录" }));
  fireEvent.click(await screen.findByRole("button", { name: "查看" }));
  await screen.findByRole("button", { name: /历史检查 A/ });
}
afterEach(() => {
  cleanup();
  clearStudioDrafts();
  rs.restoreAllMocks();
});

describe("historical debugger step selection", () => {
  it("runs the explicitly selected new step C without displaying or rerunning historical B", async () => {
    const currentTemplate: Template = {
      ...template,
      agent: {
        ...template.agent,
        steps: [
          ...historical.request.template.agent.steps,
          { id: "c", label: "新增分析 C", type: "guide", enabled: true },
        ],
      },
    };
    const sent = setupHistory(historical, currentTemplate);
    await openHistoricalResult();
    fireEvent.click(screen.getByRole("button", { name: "选择画布步骤 C" }));
    expect(screen.queryByText("B 的完整输入")).toBeNull();
    expect(screen.queryByText("A 的完整输入")).toBeNull();
    expect(screen.getByText("此步骤尚未运行。")).toBeTruthy();
    expect(screen.getByText("新增分析 C")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "编辑步骤" }).hasAttribute("disabled"),
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "只试这一步" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      start_step_id: "c",
      stop_after_step_id: "c",
      source_run_id: "history-one",
    });
    fireEvent.click(screen.getByRole("button", { name: "编辑步骤" }));
    expect(screen.getByTestId("canvas-step").textContent).toBe("c");
    expect(screen.getByTestId("edit-request").textContent).toBe("1");
  });

  it("keeps a removed step's real input/output selected and prevents actions on the fallback canvas step", async () => {
    const sent = setupHistory();
    await openHistoricalResult();
    fireEvent.click(screen.getByRole("button", { name: /历史检查 A/ }));
    expect(
      screen
        .getByRole("button", { name: /历史检查 A/ })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(screen.getByText("A 的完整输入")).toBeTruthy();
    expect(screen.getByText("A 的完整结果")).toBeTruthy();
    expect(screen.queryByText("B 的完整输入")).toBeNull();
    expect(
      screen.getByRole("button", { name: "编辑步骤" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "只试这一步" })).toBeNull();
    expect(screen.queryByRole("button", { name: "运行到这里" })).toBeNull();
    expect(screen.queryByRole("button", { name: "从这里继续" })).toBeNull();
    expect(sent).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "选择画布步骤 B" }));
    expect(screen.getByText("B 的完整输入")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "只试这一步" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      start_step_id: "b",
      stop_after_step_id: "b",
      source_run_id: "history-one",
    });
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "编辑步骤" })
          .hasAttribute("disabled"),
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑步骤" }));
    expect(screen.getByTestId("canvas-step").textContent).toBe("b");
    expect(screen.getByTestId("edit-request").textContent).toBe("1");
  });

  it("disables step actions when the displayed historical fallback is absent from the current configuration", async () => {
    const sent = setupHistory({ ...historical, trace: [trace[0]!] });
    await openHistoricalResult();
    expect(screen.getByText("A 的完整输入")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "编辑步骤" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "只试这一步" })).toBeNull();
    expect(sent).toHaveLength(0);
  });
});

it("uses legal stage IDs for debugger run-to actions while preserving the saved legacy steps", async () => {
  const currentTemplate: Template = {
    ...template,
    agent: {
      ...template.agent,
      legal: {
        enabled: true,
        jurisdictions: ["SG"],
        question_limit: 3,
        max_rounds: 2,
        retrieval_limit: 5,
        rules: [],
      },
    },
  };
  const sent = setupHistory(historical, currentTemplate);
  await openHistoricalResult();
  fireEvent.click(screen.getByRole("button", { name: "选择法域分析" }));
  fireEvent.click(screen.getByRole("button", { name: "运行到这里" }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]?.stop_after_step_id).toBe("qa03");
  expect(sent[0]?.template.agent.steps).toEqual(template.agent.steps);
});
