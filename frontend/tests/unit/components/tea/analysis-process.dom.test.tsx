import { afterEach, describe, expect, it } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AnalysisProcess } from "@/components/tea/analysis-process";
import { Trace } from "@/components/tea/shared";
import { type ProcessStep } from "@/core/tea/analysis-process";
import { type TraceStep } from "@/core/tea/types";

afterEach(cleanup);

it("keeps manual disclosure choices through completion, another run and a collapsed process", () => {
  const trace: ProcessStep[] = [
    {
      id: "qa01",
      label: "理解业务问题",
      status: "completed",
      detail: "已整理业务问题",
      output_data: {
        issues: [{ question: "保留这项问题的展开状态", domains: [] }],
      },
    },
  ];
  const view = render(<AnalysisProcess steps={trace} running />);
  const process = screen.getByRole("button", { name: /正在分析/ });
  expect(process.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(process);
  view.rerender(<AnalysisProcess steps={trace} />);
  expect(
    screen
      .getByRole("button", { name: /分析过程/ })
      .getAttribute("aria-expanded"),
  ).toBe("false");
  view.rerender(<AnalysisProcess steps={trace} running />);
  expect(process.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(process);
  fireEvent.click(screen.getByRole("button", { name: /理解业务问题/ }));
  fireEvent.click(process);
  expect(screen.queryByText("保留这项问题的展开状态")).toBeNull();
  fireEvent.click(process);
  expect(screen.getByText("保留这项问题的展开状态")).toBeTruthy();
  view.rerender(<AnalysisProcess steps={trace} />);
  expect(
    screen
      .getByRole("button", { name: /分析过程/ })
      .getAttribute("aria-expanded"),
  ).toBe("true");
  expect(screen.getByText("保留这项问题的展开状态")).toBeTruthy();
});

it("shows a truthful waiting state without exposing empty output or internal input keys", () => {
  render(
    <AnalysisProcess
      running
      steps={[
        {
          id: "qa03",
          label: "QA-03 法域判断",
          status: "running",
          detail: "",
          input_data: {
            document_ids: null,
            jurisdictions: ["CN", "ID"],
            active_issue_ids: ["internal-issue"],
          },
          output_data: {},
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /确认适用规则/ }));
  expect(screen.getByText("正在整理，本步完成后会显示结果。")).toBeTruthy();
  expect(screen.getByText("涉及地区：中国、印尼")).toBeTruthy();
  expect(
    screen.queryByText(/active_issue_ids|internal-issue|jurisdictions/),
  ).toBeNull();
  expect(screen.queryByText("本步结果")).toBeNull();
});

const steps: TraceStep[] = [
  {
    id: "qa01",
    label: "QA-01 问题识别",
    status: "completed",
    detail: "本阶段已调用模型 1 次并完成程序核对。",
    input_data: { jurisdictions: ["CN", "ID"], document_ids: null },
    output_data: {
      issues: [
        {
          id: "issue-internal-id",
          question: "进口主体还需提供什么资料？",
          domains: ["食品准入"],
          depends_on: [],
        },
      ],
      input_fingerprint: "private-fingerprint",
      initial_citations: [],
    },
  },
];

it("shows readable findings and keeps complete technical records behind a separate disclosure", async () => {
  render(<AnalysisProcess steps={steps} />);
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  expect(screen.getByText("已整理 1 个需要关注的问题。")).toBeTruthy();
  const stepToggle = screen.getByRole("button", { name: /理解业务问题/ });
  expect(stepToggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText("进口主体还需提供什么资料？")).toBeNull();
  fireEvent.click(stepToggle);
  expect(stepToggle.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("region", { name: /理解业务问题/ }).id).toBe(
    stepToggle.getAttribute("aria-controls"),
  );
  expect(screen.getByText("进口主体还需提供什么资料？")).toBeTruthy();
  expect(screen.getByText("涉及地区：中国、印尼")).toBeTruthy();
  expect(screen.queryByText(/private-fingerprint/)).toBeNull();
  expect(document.querySelector("pre")).toBeNull();
  fireEvent.click(screen.getByText("技术记录"));
  await waitFor(() =>
    expect(screen.getByText(/private-fingerprint/)).toBeTruthy(),
  );
  expect(screen.getByText(/本阶段已调用模型 1 次/)).toBeTruthy();
  fireEvent.click(screen.getByText("技术记录"));
  await waitFor(() => expect(document.querySelector("pre")).toBeNull());
  fireEvent.click(stepToggle);
  expect(stepToggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText("进口主体还需提供什么资料？")).toBeNull();
});

it("shows versioned source cards with the original locator and safe destinations", () => {
  render(
    <AnalysisProcess
      steps={[
        {
          ...steps[0]!,
          id: "qa04",
          output_data: {
            coverage: [],
            citations: [
              {
                id: "source-internal-id",
                document_id: "doc-source",
                document_version: 3,
                title: "进口材料清单（界面验收示例）",
                excerpt: "应补充进口责任主体资料。",
                locator: { line_start: 10, line_end: 12 },
                source_url: "https://example.com/guide",
                metadata: {
                  kind: "demo",
                  effective_from: "2026-01-01",
                  effective_to: "2026-12-31",
                },
                metadata_revision: 2,
              },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  fireEvent.click(screen.getByRole("button", { name: /查找与核对依据/ }));
  expect(screen.getByText("进口材料清单（界面验收示例）")).toBeTruthy();
  expect(screen.getByText("v3")).toBeTruthy();
  expect(screen.getByText("演示资料 · 第 10–12 行")).toBeTruthy();
  expect(screen.getByText("适用日期：2026-01-01 至 2026-12-31")).toBeTruthy();
  expect(screen.getByText("应补充进口责任主体资料。")).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "原始文件" }).getAttribute("href"),
  ).toContain("doc-source");
  expect(
    screen.getByRole("link", { name: "来源网站" }).getAttribute("href"),
  ).toBe("https://example.com/guide");
  expect(screen.queryByText("source-internal-id")).toBeNull();
});

it("connects scope findings to the business question and preserves missing information", () => {
  render(
    <AnalysisProcess
      steps={[
        ...steps,
        {
          ...steps[0]!,
          id: "qa03",
          output_data: {
            scopes: [
              {
                issue_id: "issue-internal-id",
                regulatory_jurisdictions: ["CN", "ID"],
                forum_jurisdictions: [],
                governing_law_jurisdictions: ["SG"],
                gaps: ["进口主体尚未提供资质材料"],
              },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  fireEvent.click(screen.getByRole("button", { name: /确认适用规则/ }));
  expect(screen.getByText("监管涉及地区：中国、印尼")).toBeTruthy();
  expect(screen.getByText("争议处理地区：待明确")).toBeTruthy();
  expect(screen.getByText("合同适用法律地区：新加坡")).toBeTruthy();
  expect(screen.getByText("进口主体尚未提供资质材料")).toBeTruthy();
  expect(screen.queryByText(/issue-internal-id/)).toBeNull();
});

it("makes missing evidence and search failures explicit even when the step completed", () => {
  render(
    <AnalysisProcess
      steps={[
        {
          ...steps[0]!,
          id: "qa04",
          output_data: {
            citations: [],
            coverage: [
              {
                domain: "进口监管",
                query: "六堡茶进口要求",
                jurisdictions: ["ID"],
                status: "failed",
                warning: "资料服务暂时不可用，请重试",
                citation_ids: [],
              },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  expect(
    screen.getByText("暂未找到可引用的原文依据，仍需补充或核对资料。"),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /查找与核对依据/ }));
  expect(screen.getByText("印尼 · 本次查找未成功")).toBeTruthy();
  expect(screen.getByText("资料服务暂时不可用，请重试")).toBeTruthy();
});

it("keeps recorded facts distinct from verified evidence and reveals long lists on demand", () => {
  render(
    <AnalysisProcess
      steps={[
        {
          ...steps[0]!,
          id: "qa02",
          output_data: {
            facts: Array.from({ length: 5 }, (_, index) => ({
              key: `field-${index}`,
              label: `业务信息 ${index + 1}`,
              value: `已填写内容 ${index + 1}`,
              confirmed: true,
              evidence_status: "unverified",
            })),
            followups: [
              { label: "进口人名称", purpose: "用于核对进口责任主体" },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  fireEvent.click(screen.getByRole("button", { name: /梳理事实与缺项/ }));
  expect(
    screen.getByText("已整理 5 项业务信息，还有 1 项待补充。"),
  ).toBeTruthy();
  expect(screen.getAllByText("已人工确认 · 依据待核验")).toHaveLength(3);
  expect(screen.queryByText("业务信息 5")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开其余 2 项" }));
  expect(screen.getByText("业务信息 5")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "收起更多内容" }));
  expect(screen.queryByText("业务信息 5")).toBeNull();
});

it("shows the source title, version and excerpt when evidence exists", () => {
  render(
    <AnalysisProcess
      steps={[
        {
          ...steps[0]!,
          id: "qa04",
          output_data: {
            coverage: [],
            citations: [
              {
                document_id: "internal-document",
                title: "原文资料名称",
                document_version: 2,
                excerpt: "需要核对的原文片段",
              },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  fireEvent.click(screen.getByRole("button", { name: /查找与核对依据/ }));
  expect(screen.getByText("原文资料名称")).toBeTruthy();
  expect(screen.getByText("v2")).toBeTruthy();
  expect(screen.getByText("需要核对的原文片段")).toBeTruthy();
  expect(screen.queryByText(/internal-document/)).toBeNull();
});

it("shows report follow-ups and a clear empty report state without fabricating content", () => {
  render(
    <AnalysisProcess
      steps={[
        {
          ...steps[0]!,
          id: "qa06",
          output_data: {
            sections: [],
            followups: [
              { label: "实际装运日期", purpose: "补充后才能计算期限" },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  fireEvent.click(screen.getByRole("button", { name: /整理报告与下一步/ }));
  expect(screen.getByText("暂未形成报告内容")).toBeTruthy();
  expect(screen.getByText("实际装运日期")).toBeTruthy();
  expect(screen.getByText("补充后才能计算期限")).toBeTruthy();
});

it("does not confuse a completed analysis with a settled conclusion or deadline", () => {
  render(
    <AnalysisProcess
      steps={[
        {
          ...steps[0]!,
          id: "qa05",
          output_data: {
            issues: [
              {
                question: "能否按当前资料办理？",
                status: "undetermined",
                needs_review: true,
                conclusion: "现有资料不足，需补充后再判断。",
                missing_facts: [{ label: "装运日期" }],
                deadlines: [
                  { label: "申报期限", status: "unavailable", date: null },
                ],
              },
            ],
          },
        },
      ]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /分析过程/ }));
  expect(
    screen.getByText("已整理 1 项分析意见，其中 1 项仍需补充或复核。"),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /分析结论与期限/ }));
  expect(screen.getByText("尚无法判断")).toBeTruthy();
  expect(screen.getByText("待补充：装运日期")).toBeTruthy();
  expect(screen.getByText("申报期限：尚不能确定日期")).toBeTruthy();
});

it("preserves failed, cancelled and unexecuted states and legacy business details", () => {
  const trace: ProcessStep[] = [
    {
      id: "check",
      label: "核对资料",
      status: "failed",
      detail: "资料读取失败，请重试",
      input_data: { scope: "用户选择的资料" },
    },
    { id: "qa05", label: "分析", status: "cancelled", detail: "本次已停止" },
    { id: "qa06", label: "报告", status: "pending", detail: "" },
  ];
  render(<AnalysisProcess steps={trace} />);
  fireEvent.click(screen.getByRole("button", { name: /分析未完成/ }));
  expect(screen.getByText("资料读取失败，请重试")).toBeTruthy();
  expect(screen.getByText("已停止")).toBeTruthy();
  expect(screen.getByText("未执行")).toBeTruthy();
  expect(screen.queryByText("已完成")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /核对资料/ }));
  expect(screen.getByText("用户选择的资料")).toBeTruthy();
});

describe("analysis process presentation", () => {
  it("retains configured labels for step IDs that match object prototype names", () => {
    render(
      <Trace
        steps={[
          {
            id: "constructor",
            label: "核对业务资料",
            status: "completed",
            detail: "已核对资料",
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /依据与检查记录/ }));
    expect(screen.getByText("核对业务资料")).toBeTruthy();
  });
  it("distinguishes cached, skipped and stopped steps from newly completed work", () => {
    render(
      <Trace
        steps={[
          {
            id: "one",
            label: "资料检查",
            status: "cached",
            detail: "沿用已保存资料",
          },
          {
            id: "two",
            label: "条件检查",
            status: "skipped",
            detail: "本次不适用",
          },
          {
            id: "three",
            label: "报告检查",
            status: "cancelled",
            detail: "用户停止了本次分析",
          },
        ]}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /分析已停止.*2\s*\/\s*3/ }),
    );
    expect(screen.getByText("沿用结果")).toBeTruthy();
    expect(screen.getByText("无需执行")).toBeTruthy();
    expect(screen.getByText("已停止")).toBeTruthy();
    expect(screen.queryByText("已完成")).toBeNull();
  });
});
