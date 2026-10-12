import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { DebugSampleEditor } from "@/components/tea/debug-sample-editor";
import type { DebugExpectation, DebugSample, Template } from "@/core/tea/types";
import {
  clearDebuggerSessions,
  useWorkflowDebugger,
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
    steps: [{ id: "check", label: "检查", type: "check", enabled: true }],
  },
};
function Editor({
  values = {},
  expectations = [],
}: {
  values?: Record<string, Record<string, string | number>>;
  expectations?: DebugExpectation[];
}) {
  const debug = useWorkflowDebugger({
    ownerId: "test",
    draft: template,
    stageId: "prepare",
  });
  return (
    <>
      <button
        onClick={() => debug.editSample({ task_values: values, expectations })}
      >
        载入样例
      </button>
      <button onClick={() => void debug.loadSample("stored")}>
        读取已保存样例
      </button>
      <DebugSampleEditor debug={debug} stage={template.stages[0]!} />
      <output data-testid="values">
        {JSON.stringify(debug.sample.task_values)}
      </output>
      <output data-testid="expectations">
        {JSON.stringify(debug.sample.expectations)}
      </output>
    </>
  );
}
afterEach(() => {
  cleanup();
  clearDebuggerSessions();
  rs.restoreAllMocks();
});

describe("debug sample repair", () => {
  it("lets an unchanged saved sample refresh its evidence snapshot without duplicating it", async () => {
    const sample: DebugSample = {
      id: "stored",
      name: "固定资料样例",
      template_id: template.id,
      template,
      stage_id: "prepare",
      input: "请检查",
      task_values: { info: { name: "茶公司" } },
      document_ids: [],
      expectations: [],
      revision: 3,
      updated_at: "2026-10-12T00:00:00Z",
      document_versions: [],
    };
    const writes: unknown[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (typeof url === "string" && url.endsWith("/debug-samples/stored")) {
        if (init?.method === "PUT" && typeof init.body === "string")
          writes.push(JSON.parse(init.body));
        return new Response(
          JSON.stringify({
            ...sample,
            revision: init?.method === "PUT" ? 4 : 3,
          }),
        );
      }
      return new Response(JSON.stringify({ items: [], total: 0 }));
    });
    render(<Editor />);
    fireEvent.click(screen.getByText("读取已保存样例"));
    await screen.findByDisplayValue("固定资料样例");
    const save = screen.getByRole("button", { name: "保存样例" });
    expect((save as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      expected_revision: 3,
      task_values: sample.task_values,
      document_ids: [],
    });
  });

  it("removes deleted fields and tasks while preserving current inputs", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 })),
    );
    render(
      <Editor
        values={{
          info: { name: "茶公司", obsolete: "旧填写" },
          removed: { value: 12 },
        }}
      />,
    );
    fireEvent.click(screen.getByText("载入样例"));
    fireEvent.click(await screen.findByText("移除旧输入"));
    expect(screen.getByTestId("values").textContent).toBe(
      JSON.stringify({ info: { name: "茶公司" } }),
    );
    expect(screen.queryByText("移除旧输入")).toBeNull();
  });

  it("can repair a deleted task even when all its values were cleared", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 })),
    );
    render(<Editor values={{ info: { name: "茶公司" }, removed: {} }} />);
    fireEvent.click(screen.getByText("载入样例"));
    fireEvent.click(await screen.findByText("移除旧输入"));
    expect(screen.getByTestId("values").textContent).toBe(
      JSON.stringify({ info: { name: "茶公司" } }),
    );
  });

  it("drops text-only fields when changing an expectation to a citation", () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 })),
    );
    render(
      <Editor
        expectations={[{ id: "one", kind: "manual", text: "核对建议" }]}
      />,
    );
    fireEvent.click(screen.getByText("载入样例"));
    fireEvent.change(screen.getByLabelText("预期 1 的检查方式"), {
      target: { value: "citation" },
    });
    expect(screen.getByTestId("expectations").textContent).toBe(
      JSON.stringify([{ id: "one", kind: "citation" }]),
    );
  });
});
