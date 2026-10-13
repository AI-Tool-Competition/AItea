import { afterEach, describe, expect, it, rs } from "@rstest/core";

import * as api from "@/core/tea/api";
import { analysisSteps, configurationSchema } from "@/core/tea/configuration";

const policy = {
  enabled: true,
  jurisdictions: ["SG"],
  question_limit: 3,
  max_rounds: 2,
  retrieval_limit: 5,
  rules: [
    {
      id: "facts",
      title: "交付事实",
      module: "qa02",
      enabled: true,
      execution: "model",
      domains: ["contract"],
      jurisdictions: ["CN"],
      condition: "涉及交付",
      instructions: "询问交付日期",
      required_facts: [
        {
          key: "delivery_date",
          label: "交付日期",
          purpose: "核对期限",
          decisive: true,
        },
      ],
      output_requirements: "逐项引用",
      review_required: true,
      source_reference: "法务规则第一条",
    },
  ],
};
const template = {
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
afterEach(() => {
  rs.restoreAllMocks();
});
describe("editable legal contracts", () => {
  it("preserves legal rule cards and optional legacy policies through configuration import", () => {
    const result = configurationSchema.safeParse({
      ...template,
      agent: { ...template.agent, legal: policy },
    });
    expect(result.success).toBe(true);
    if (result.success)
      expect(result.data.agent).toMatchObject({ legal: policy });
    expect(
      configurationSchema.safeParse({
        ...template,
        agent: { ...template.agent, legal: null },
      }).success,
    ).toBe(true);
  });
});

const result = {
  question: "法律问题",
  issues: [],
  facts: [],
  followups: [],
  sections: [],
  trace: [],
  gaps: [],
  assistant_mode: "demo",
  output: "待核实",
  citations: [],
};
const analysis = {
  id: "run-one",
  case_id: "case-one",
  stage_id: "prepare",
  question: "法律问题",
  revision: 1,
  status: "needs_input",
  created_at: "2026-10-12T00:00:00Z",
  updated_at: "2026-10-12T00:00:00Z",
  result,
  sections: [],
  affected_issue_ids: [],
  error: null,
};
const request = {
  stage_id: "prepare",
  question: "法律问题",
  client_request_id: "client-one",
};
const frame = (value: unknown) =>
  new Response(
    `event: done\ndata: ${JSON.stringify({ analysis: value })}\n\n`,
    { headers: { "Content-Type": "text/event-stream" } },
  );
describe("legal analysis transport", () => {
  it("receives public progress and an explicitly scoped saved analysis", async () => {
    const onEvent = rs.fn();
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          `event: status\ndata: {"label":"核对事实"}\n\nevent: done\ndata: ${JSON.stringify({ analysis })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );
    await expect(
      api.streamLegalAnalysis("case-one", request, { onEvent }),
    ).resolves.toEqual(analysis);
    expect(onEvent).toHaveBeenCalledWith({
      event: "status",
      data: { label: "核对事实" },
    });
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      "/api/tea/cases/case-one/legal/analyses/stream",
    );
  });
  it.each([
    { ...analysis, case_id: "other-case" },
    { ...analysis, stage_id: "other-stage" },
    { ...analysis, status: "imagined" },
    { ...analysis, result: { ...result, trace: undefined } },
    { ...analysis, sections: [{ id: "one" }] },
  ])(
    "rejects incomplete or mis-scoped results instead of presenting them as saved analysis",
    async (value) => {
      rs.spyOn(globalThis, "fetch").mockResolvedValue(frame(value));
      await expect(
        api.streamLegalAnalysis("case-one", request, {
          onEvent: () => undefined,
        }),
      ).rejects.toMatchObject({ code: "invalid_response" });
    },
  );
});

it("uses the six fixed legal stages for breakpoints without rewriting legacy steps", () => {
  const legacy = {
    language: "中文",
    instructions: "",
    retrieval_limit: 5,
    steps: [
      { id: "old", label: "旧流程", type: "guide" as const, enabled: true },
    ],
  };
  expect(analysisSteps(legacy)).toEqual(legacy.steps);
  expect(
    analysisSteps({ ...legacy, legal: { ...policy, rules: [] } }).map(
      (step) => step.id,
    ),
  ).toEqual(["qa01", "qa02", "qa03", "qa04", "qa05", "qa06"]);
  expect(legacy.steps[0]?.id).toBe("old");
});
