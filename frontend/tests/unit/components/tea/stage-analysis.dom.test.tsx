import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { StageAnalysisPanel } from "@/components/tea/stage-analysis";
import { type CaseDetail, type StageState } from "@/core/tea/types";

rs.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => (
    <div>{children}</div>
  ),
}));
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});
const record = { id: "case-one", revision: 1 } as CaseDetail;
const stage = { id: "prepare", status: "active" } as StageState;
const result = {
  id: "analysis",
  stage_id: "prepare",
  case_revision: 1,
  status: "completed",
  output: "请先补充产品名称",
  trace: [],
  checks: [],
  citations: [],
  assistant_mode: "demo",
  created_at: "2026-10-11T00:00:00Z",
};
describe("proactive stage analysis", () => {
  it("automatically analyzes an available stage without changing task state", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(result), { status: 200 }));
    render(
      <StageAnalysisPanel
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("请先补充产品名称")).toBeTruthy(),
    );
    expect(fetcher.mock.calls[0]![0]).toBe(
      "/api/tea/cases/case-one/stages/prepare/analysis",
    );
    expect(
      JSON.parse(fetcher.mock.calls[0]![1]!.body as string).expected_revision,
    ).toBe(1);
  });
  it("does not run a locked stage", () => {
    const fetcher = rs.spyOn(globalThis, "fetch");
    render(
      <StageAnalysisPanel
        record={record}
        stage={{ ...stage, status: "locked" }}
        onUpdate={() => undefined}
      />,
    );
    expect(fetcher).not.toHaveBeenCalled();
    expect(screen.getByText(/节点解锁后/)).toBeTruthy();
  });
});
