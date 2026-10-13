import { afterEach, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";

import { AnalysisEditor } from "@/components/tea/analysis-editor";
import { type Template } from "@/core/tea/types";

rs.mock("@/components/tea/configuration-canvas", () => ({
  ConfigurationCanvas: () => <div>分析画布</div>,
}));
const template: Template = {
  id: "tea",
  version: 1,
  title: "出口",
  description: "",
  origin: "中国",
  destination: "印度尼西亚",
  is_demo: true,
  modes: [],
  stages: [
    { id: "prepare", name: "准备", actor: "企业", description: "", tasks: [] },
  ],
  agent: { language: "中文", instructions: "", retrieval_limit: 5, steps: [] },
};
const defaults = {
  policy: {
    enabled: false,
    jurisdictions: [],
    question_limit: 3,
    max_rounds: 2,
    retrieval_limit: 5,
    rules: [
      {
        id: "review",
        title: "法律审查",
        module: "qa05",
        enabled: true,
        execution: "model",
        domains: [],
        jurisdictions: [],
        condition: "法律问题",
        instructions: "核对依据",
        required_facts: [],
        output_requirements: "给出引用",
        review_required: true,
        source_reference: "法务手册",
      },
    ],
  },
  modules: [{ id: "qa05", title: "法律分析" }],
  program_checks: [
    { id: "evidence", title: "依据有效性", description: "审核原文版本" },
  ],
};
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});
it("edits legal rule cards in the existing template draft while program gates remain read-only", async () => {
  rs.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(defaults)),
  );
  let latest = template;
  function Editor() {
    const [draft, setDraft] = useState(template);
    return (
      <AnalysisEditor
        draft={draft}
        onChange={(next) => {
          latest = next;
          setDraft(next);
        }}
        stageId="prepare"
        onStage={() => undefined}
        stepId=""
        onStep={() => undefined}
        trace={[]}
        editingDefault
        onEditingDefault={() => undefined}
      />
    );
  }
  render(<Editor />);
  fireEvent.click(screen.getByRole("button", { name: "法律规则" }));
  await waitFor(() =>
    expect(screen.getByLabelText("在对话中启用法律分析")).toBeTruthy(),
  );
  fireEvent.click(screen.getByLabelText("在对话中启用法律分析"));
  fireEvent.change(screen.getByLabelText("规则名称"), {
    target: { value: "进口争议审查" },
  });
  fireEvent.change(screen.getByLabelText("分析要求"), {
    target: { value: "列出例外和抗辩" },
  });
  expect(latest.agent.legal?.enabled).toBe(true);
  expect(latest.agent.legal?.rules[0]).toMatchObject({
    title: "进口争议审查",
    instructions: "列出例外和抗辩",
  });
  expect(screen.getByText("依据有效性")).toBeTruthy();
  expect(screen.queryByLabelText("依据有效性")).toBeNull();
});
