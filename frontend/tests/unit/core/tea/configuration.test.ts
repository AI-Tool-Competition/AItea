import { describe, expect, it } from "@rstest/core";

import {
  configurationSchema,
  configurationIssueLabel,
  moveItem,
} from "@/core/tea/configuration";

const template = {
  id: "tea",
  version: 1,
  title: "出口准备",
  description: "",
  origin: "中国",
  destination: "印度尼西亚",
  is_demo: true,
  modes: [{ id: "a", name: "模式 A", description: "", enabled: true }],
  stages: [
    {
      id: "prepare",
      name: "准备",
      actor: "企业",
      description: "",
      tasks: [
        {
          id: "product",
          title: "产品",
          description: "",
          required: true,
          fields: [],
          require_document: false,
          require_confirmation: true,
          output_template: null,
        },
      ],
    },
  ],
  agent: {
    language: "中文",
    instructions: "以资料为依据",
    retrieval_limit: 5,
    steps: [{ id: "guide", type: "guide", label: "分析", enabled: true }],
  },
};

describe("configuration compatibility and editing", () => {
  it("round trips select completion rules through export and import", () => {
    const value = structuredClone(template);
    const selected = {
      key: "result",
      label: "办理结果",
      type: "select",
      required: true,
      options: ["待核验", "已核验完成"],
      accepted_values: ["已核验完成"],
    };
    const input = {
      ...value,
      stages: [
        {
          ...value.stages[0],
          tasks: [{ ...value.stages[0]!.tasks[0], fields: [selected] }],
        },
      ],
    };
    const parsed = configurationSchema.parse(JSON.parse(JSON.stringify(input)));
    expect(parsed.stages[0]?.tasks[0]?.fields[0]?.accepted_values).toEqual([
      "已核验完成",
    ]);
    expect(parsed.stages[0]?.tasks[0]?.fields[0]?.options).toEqual([
      "待核验",
      "已核验完成",
    ]);
  });
  it("imports old configurations and per-stage analysis without losing instructions", () => {
    expect(configurationSchema.safeParse(template).success).toBe(true);
    const customized = structuredClone(template);
    const analysis = {
      ...template.agent,
      steps: [{ ...template.agent.steps[0]!, instructions: "先检查产品名称" }],
    };
    const parsed = configurationSchema.parse({
      ...customized,
      stages: [{ ...customized.stages[0], analysis }],
    });
    expect(parsed.stages[0]?.analysis?.steps[0]?.instructions).toBe(
      "先检查产品名称",
    );
  });
  it("rejects unknown rules instead of silently dropping them", () => {
    expect(
      configurationSchema.safeParse({ ...template, arbitrary_rule: true })
        .success,
    ).toBe(false);
  });
  it("moves nodes while retaining their configured tasks and refuses invalid moves", () => {
    const nodes = [
      { id: "a", tasks: ["one"] },
      { id: "b", tasks: ["two"] },
    ];
    expect(moveItem(nodes, 0, 1)).toEqual([nodes[1], nodes[0]]);
    expect(moveItem(nodes, 0, -1)).toEqual(nodes);
    expect(nodes[0]?.id).toBe("a");
  });
  it("translates validation paths into understandable field locations", () => {
    const parsed = configurationSchema.parse(template);
    expect(configurationIssueLabel("stages.0.tasks.0.title", parsed)).toContain(
      "准备",
    );
    expect(configurationIssueLabel("stages.0.tasks.0.title", parsed)).toContain(
      "任务名称",
    );
  });
});
