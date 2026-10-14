import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { StrictMode } from "react";

import { StageAnalysisPanel } from "@/components/tea/stage-analysis";
import { type CaseDetail, type StageState } from "@/core/tea/types";

rs.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => (
    <div>{children}</div>
  ),
}));
rs.mock("@/components/tea/legal-workbench", () => ({
  LegalWorkbench: ({ analysisId }: { analysisId: string }) => (
    <div>本节点编辑：{analysisId}</div>
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
  case_id: "case-one",
  stage_id: "prepare",
  case_revision: 1,
  status: "completed",
  output: "请先补充产品名称",
  trace: [],
  checks: [],
  citations: [],
  assistant_mode: "demo",
  created_at: "2026-10-11T00:00:00Z",
  legal_analysis_id: "node-report",
  legal_revision: 2,
};
function response() {
  return new Response(
    `event: done\ndata: ${JSON.stringify({ analysis: result })}\n\n`,
    {
      headers: { "Content-Type": "text/event-stream" },
    },
  );
}
describe("proactive stage analysis", () => {
  it("keeps waiting through an existing run and automatically displays its result", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          'event: error\ndata: {"code":"analysis_in_progress","message":"本业务正在分析"}\n\n',
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      )
      .mockImplementation(async () => response());
    render(
      <StageAnalysisPanel
        accountId="account-one"
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    await screen.findByText("已有分析正在运行，正在等待结果…");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      screen
        .getByRole("region", { name: "节点开始分析" })
        .getAttribute("aria-busy"),
    ).toBe("true");
    await waitFor(() =>
      expect(screen.getByText("本节点编辑：node-report")).toBeTruthy(),
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it("automatically analyzes an available stage without changing task state", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => response());
    render(
      <StrictMode>
        <StageAnalysisPanel
          accountId="account-one"
          record={record}
          stage={stage}
          onUpdate={() => undefined}
        />
      </StrictMode>,
    );
    await waitFor(() =>
      expect(screen.getByText("本节点编辑：node-report")).toBeTruthy(),
    );
    expect(fetcher.mock.calls[0]![0]).toBe(
      "/api/tea/cases/case-one/stages/prepare/analysis/stream",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(
      JSON.parse(fetcher.mock.calls[0]![1]!.body as string).expected_revision,
    ).toBe(1);
  });
  it("does not run a locked stage", () => {
    const fetcher = rs.spyOn(globalThis, "fetch");
    render(
      <StageAnalysisPanel
        accountId="account-one"
        record={record}
        stage={{ ...stage, status: "locked" }}
        onUpdate={() => undefined}
      />,
    );
    expect(fetcher).not.toHaveBeenCalled();
    expect(screen.getByText(/节点解锁后/)).toBeTruthy();
  });
  it("shows actual step progress before the completed response arrives", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(value) {
        controller = value;
      },
    });
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
    );
    render(
      <StageAnalysisPanel
        accountId="account-one"
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    await act(async () =>
      controller.enqueue(
        new TextEncoder().encode(
          `event: step\ndata: ${JSON.stringify({ id: "qa01", label: "问题识别", status: "running", detail: "正在核对本节点的问题范围" })}\n\n`,
        ),
      ),
    );
    expect(screen.getByText("正在核对本节点的问题范围")).toBeTruthy();
    expect(screen.getByText("分析中")).toBeTruthy();
    expect(screen.queryByText("本节点编辑：node-report")).toBeNull();
    await act(async () =>
      controller.enqueue(
        new TextEncoder().encode(
          'event: status\ndata: {"label":"QA-05 条件判断与有界复核"}\n\n',
        ),
      ),
    );
    expect(screen.getByText("正在分析结论与期限…")).toBeTruthy();
    expect(screen.queryByText(/QA-05/)).toBeNull();
    await act(async () => {
      controller.enqueue(
        new TextEncoder().encode(
          `event: done\ndata: ${JSON.stringify({ analysis: result })}\n\n`,
        ),
      );
      controller.close();
    });
    await waitFor(() =>
      expect(screen.getByText("本节点编辑：node-report")).toBeTruthy(),
    );
  });
  it("aborts the old node when switching and rejects results for a different node", async () => {
    let oldSignal: AbortSignal | undefined;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (_path, options) => {
        if (!oldSignal) {
          oldSignal = options!.signal!;
          return await new Promise<Response>((_resolve, reject) =>
            oldSignal!.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            ),
          );
        }
        return response();
      });
    const view = render(
      <StageAnalysisPanel
        accountId="account-one"
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    view.rerender(
      <StageAnalysisPanel
        accountId="account-one"
        record={record}
        stage={{ ...stage, id: "next" }}
        onUpdate={() => undefined}
      />,
    );
    await waitFor(() => expect(oldSignal?.aborted).toBe(true));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("无法读取"),
    );
    expect(screen.queryByText("本节点编辑：node-report")).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it("collapses a completed process and connects its disclosure to the saved steps", async () => {
    const completed = {
      ...result,
      trace: Array.from({ length: 6 }, (_, index) => ({
        id: `qa0${index + 1}`,
        label: `分析阶段 ${index + 1}`,
        status: "completed",
        detail: `已保存的步骤说明 ${index + 1}`,
      })),
    };
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        `event: done\ndata: ${JSON.stringify({ analysis: completed })}\n\n`,
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    render(
      <StageAnalysisPanel
        accountId="completed-owner"
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("本节点编辑：node-report")).toBeTruthy(),
    );
    const trigger = screen.getByRole("button", {
      name: /分析过程.*6\s*\/\s*6/,
    });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("已保存的步骤说明 1")).toBeNull();
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("已保存的步骤说明 1")).toBeTruthy();
    const content = document.getElementById(
      trigger.getAttribute("aria-controls")!,
    );
    expect(content?.contains(screen.getByText("已保存的步骤说明 1"))).toBe(
      true,
    );
  });
  it("keeps partial failures distinct from completed steps", async () => {
    const partial = {
      ...result,
      status: "failed",
      trace: [
        {
          id: "qa01",
          label: "问题识别",
          status: "completed",
          detail: "已核对问题",
        },
        {
          id: "qa02",
          label: "事实梳理",
          status: "failed",
          detail: "资料读取失败，等待重试",
        },
      ],
    };
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        `event: done\ndata: ${JSON.stringify({ analysis: partial })}\n\n`,
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    render(
      <StageAnalysisPanel
        accountId="partial-owner"
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText("本节点编辑：node-report")).toBeTruthy(),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /分析未完成.*1\s*\/\s*6/ }),
    );
    expect(screen.getByText("资料读取失败，等待重试")).toBeTruthy();
    expect(screen.getAllByText("已完成")).toHaveLength(1);
    expect(screen.getAllByText("未执行")).toHaveLength(4);
  });
  it("respects a manual collapse when the next streamed step arrives", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(value) {
            controller = value;
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    render(
      <StageAnalysisPanel
        accountId="stream-owner"
        record={record}
        stage={stage}
        onUpdate={() => undefined}
      />,
    );
    const send = (detail: string) =>
      controller.enqueue(
        new TextEncoder().encode(
          `event: step\ndata: ${JSON.stringify({ id: "qa01", label: "问题识别", status: "running", detail })}\n\n`,
        ),
      );
    await act(async () => send("正在识别业务问题"));
    const trigger = screen.getByRole("button", {
      name: /正在分析.*0\s*\/\s*6/,
    });
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(trigger);
    await act(async () => send("继续核对业务范围"));
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("继续核对业务范围")).toBeNull();
    await act(async () => {
      controller.enqueue(
        new TextEncoder().encode(
          `event: done\ndata: ${JSON.stringify({ analysis: result })}\n\n`,
        ),
      );
      controller.close();
    });
    await waitFor(() =>
      expect(screen.getByText("本节点编辑：node-report")).toBeTruthy(),
    );
  });
});
