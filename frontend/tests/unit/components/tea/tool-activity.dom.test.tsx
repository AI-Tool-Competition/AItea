import { afterEach, expect, it } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MessageActivity, ToolActivity } from "@/components/tea/tool-activity";
import type { ToolCall, TraceStep } from "@/core/tea/types";

afterEach(cleanup);
const tool: ToolCall = {
  id: "private-call-id",
  name: "search_documents",
  label: "检索业务资料",
  position: 1,
  status: "running",
  attempt: 1,
  max_attempts: 2,
  detail: "RAGFlow internal provider detail",
  duration_ms: 0,
};

it("streams progress through Vercel Task without exposing internal labels or payloads", () => {
  const view = render(<ToolActivity tools={[tool]} running />);
  const trigger = screen.getByRole("button", { name: /处理进度/ });
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("region").id).toBe(
    trigger.getAttribute("aria-controls"),
  );
  expect(screen.getByText("正在查找资料")).toBeTruthy();
  expect(screen.getByText("进行中")).toBeTruthy();
  expect(
    screen.queryByText(/private-call-id|search_documents|RAGFlow|检索业务资料/),
  ).toBeNull();
  fireEvent.click(trigger);
  fireEvent.click(trigger);
  view.rerender(
    <ToolActivity
      tools={[{ ...tool, status: "retrying", detail: "retry_after=3" }]}
      running
    />,
  );
  expect(screen.getByText("正在重试")).toBeTruthy();
  view.rerender(
    <ToolActivity
      tools={[{ ...tool, status: "completed", attempt: 2, duration_ms: 1200 }]}
      running={false}
    />,
  );
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("已完成 1 项处理")).toBeTruthy();
  expect(screen.getByText("1.2 秒")).toBeTruthy();
  expect(screen.queryByText(/retry_after|最多.*次/)).toBeNull();
});

it("collapses completed work by default and respects a manual collapse while streaming", () => {
  const view = render(<ToolActivity tools={[tool]} running />);
  view.rerender(
    <ToolActivity tools={[{ ...tool, status: "completed" }]} running={false} />,
  );
  const trigger = screen.getByRole("button", { name: /处理进度/ });
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("list")).toBeNull();
  view.rerender(<ToolActivity tools={[tool]} running />);
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(trigger);
  view.rerender(
    <ToolActivity
      tools={[tool, { ...tool, id: "next", name: "inspect_image" }]}
      running
    />,
  );
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText("正在处理 2 项工作")).toBeTruthy();
});

it("never presents disconnected running work as completed", () => {
  render(<ToolActivity tools={[tool]} running={false} />);
  fireEvent.click(screen.getByRole("button", { name: /处理已停止/ }));
  expect(screen.getByText("已停止")).toBeTruthy();
  expect(screen.getByText("处理已停止，结果尚未确认。")).toBeTruthy();
  expect(screen.queryByText("已完成")).toBeNull();
});

it("keeps failures and cached work distinguishable without leaking raw errors", () => {
  render(
    <ToolActivity
      tools={[
        { ...tool, id: "cached", status: "completed", cached: true },
        {
          ...tool,
          id: "failed",
          name: "web_search",
          status: "failed",
          detail: "secret-provider-error",
        },
      ]}
      running={false}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /部分处理未完成/ }));
  expect(screen.getByText("沿用结果")).toBeTruthy();
  expect(screen.getByText("未完成")).toBeTruthy();
  expect(screen.queryByText("secret-provider-error")).toBeNull();
});

const hiddenNames: ToolCall["name"][] = [
  "agent_turn",
  "discover_tools",
  "load_skill",
  "recall_memory",
  "remember_memory",
  "update_plan",
  "read_tool_result",
  "compact_context",
];
const internal: TraceStep = {
  id: "agent",
  label: "智能工具协作",
  status: "running",
  detail: "记忆与法律流程",
  input_data: { private: "raw-data" },
  tools: hiddenNames.map((name, position) => ({
    ...tool,
    id: name,
    name,
    label: "记忆",
    position,
  })),
};

it("hides internal-only progress in live and historical messages", () => {
  const view = render(<MessageActivity steps={[internal]} running />);
  expect(view.container.textContent).toBe("");
  view.rerender(
    <MessageActivity steps={[{ ...internal, status: "completed" }]} />,
  );
  expect(view.container.textContent).toBe("");
});

it("flattens real calls and hides memory and legal substeps even when expanded", () => {
  render(
    <MessageActivity
      steps={[
        {
          ...internal,
          tools: [
            ...internal.tools!,
            tool,
            { ...tool, id: "legal", name: "analyze_legal", label: "法律分析" },
          ],
        },
        {
          id: "qa01",
          label: "内部分析阶段",
          status: "completed",
          detail: "内部过程",
          output_data: { raw: "private" },
        },
      ]}
      running
    />,
  );
  expect(screen.getAllByRole("button")).toHaveLength(1);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByText("核对适用依据")).toBeTruthy();
  expect(
    screen.queryByText(/记忆|法律分析|智能工具协作|内部|raw-data|技术记录/),
  ).toBeNull();
});

it("reports a failed answer even when all visible calls already completed", () => {
  render(
    <MessageActivity
      steps={[
        {
          ...internal,
          tools: [{ ...tool, status: "completed" }],
        },
      ]}
      incomplete
    />,
  );
  expect(screen.getByText("部分处理未完成")).toBeTruthy();
  expect(screen.queryByText("已完成 1 项处理")).toBeNull();
});

it("displays actual tool output as safe text and sources, with expandable excerpts", () => {
  const text = "原".repeat(179) + "😀" + "这是实际返回内容。".repeat(25);
  render(
    <ToolActivity
      tools={[
        {
          ...tool,
          status: "completed",
          output_preview: {
            text,
            notice: "尚需核对原件。",
            truncated: true,
            items: [
              {
                label: "主体名称",
                text: "请补充实际进口主体。",
                status: "missing",
              },
              {
                label: "来源原文",
                text: "实际返回的来源片段",
                url: "https://example.org/source",
              },
              {
                label: "不安全来源",
                text: '<img src="x" onerror="alert(1)">',
                url: "javascript:alert(1)",
              },
            ],
          },
        },
      ]}
      running
    />,
  );
  expect(screen.getByText("主体名称")).toBeTruthy();
  expect(screen.getByText("原".repeat(179) + "😀…")).toBeTruthy();
  expect(screen.getByText("待补充")).toBeTruthy();
  expect(screen.queryByText("不安全来源")).toBeNull();
  expect(
    screen.getByRole("link", { name: "来源原文" }).getAttribute("href"),
  ).toBe("https://example.org/source");
  fireEvent.click(screen.getByRole("button", { name: "展开结果" }));
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.getByText("不安全来源")).toBeTruthy();
  expect(screen.queryByRole("link", { name: "不安全来源" })).toBeNull();
  expect(document.querySelector(".tea-tool-output img")).toBeNull();
  expect(screen.getByText("此处为本次执行结果的节选。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "收起结果" }));
  expect(screen.queryByText("不安全来源")).toBeNull();
});
