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
  fireEvent.click(screen.getByText("查看本步发现"));
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
  fireEvent.click(screen.getByText("查看本步发现"));
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
  fireEvent.click(screen.getAllByText("查看本步发现")[1]!);
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
  fireEvent.click(screen.getByText("查看本步发现"));
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
  fireEvent.click(screen.getByText("查看本步发现"));
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
  fireEvent.click(screen.getByText("查看本步发现"));
  expect(screen.getByText("原文资料名称")).toBeTruthy();
  expect(screen.getByText("资料版本：第 2 版")).toBeTruthy();
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
  fireEvent.click(screen.getByText("查看本步发现"));
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
  fireEvent.click(screen.getByText("查看本步发现"));
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
  fireEvent.click(screen.getByText("查看本步发现"));
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
