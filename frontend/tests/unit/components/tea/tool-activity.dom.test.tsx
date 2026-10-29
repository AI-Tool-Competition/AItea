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

it("shows exact Chinese article lookup as a readable activity", () => {
  render(
    <ToolActivity tools={[{ ...tool, name: "lookup_chinese_law" }]} running />,
  );
  expect(
    screen.getByRole("button", { name: "查询中国法条 · 进行中" }),
  ).toBeTruthy();
});

it("closes preparation on the first answer and closes a reopened panel on completion or stop", () => {
  const view = render(<MessageActivity steps={[]} running />);
  const trigger = screen.getByRole("button", { name: /处理进度/ });
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  view.rerender(<MessageActivity steps={[]} running answering />);
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText("正在回复")).toBeTruthy();
  fireEvent.click(trigger);
  view.rerender(<MessageActivity steps={[]} running answering />);
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  view.rerender(<MessageActivity steps={[]} answering />);
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText("已完成回复")).toBeTruthy();
  view.rerender(<MessageActivity steps={[]} running />);
  view.rerender(<MessageActivity steps={[]} stopped />);
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText("处理已停止")).toBeTruthy();
});

it("fully collapses completed output and expands the next call and retry", () => {
  const view = render(<ToolActivity tools={[tool]} running />);
  const first = screen.getByRole("button", { name: "查找资料 · 进行中" });
  const done = {
    ...tool,
    status: "completed" as const,
    output_preview: {
      text: "已找到 3 份产品资料。",
      items: [],
      notice: "",
      truncated: false,
    },
  };
  const next = { ...tool, id: "next", name: "inspect_image" as const };
  view.rerender(<ToolActivity tools={[done, next]} running />);
  expect(first.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText(done.output_preview.text)).toBeNull();
  const second = screen.getByRole("button", { name: "查看图片 · 进行中" });
  expect(second.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(second);
  expect(second.getAttribute("aria-expanded")).toBe("false");
  view.rerender(
    <ToolActivity
      tools={[done, { ...next, status: "retrying", attempt: 2 }]}
      running
    />,
  );
  expect(second.getAttribute("aria-expanded")).toBe("true");
  view.rerender(<ToolActivity tools={[done, next]} running />);
  expect(second.getAttribute("aria-expanded")).toBe("true");
  view.rerender(
    <ToolActivity tools={[done, { ...next, status: "completed" }]} running />,
  );
  const parent = screen.getByRole("button", { name: /处理进度/ });
  expect(parent.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "查找资料 · 已完成" }));
  view.rerender(
    <ToolActivity
      tools={[done, { ...next, status: "completed" }]}
      running={false}
      answering
    />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(parent);
  expect(
    screen
      .getByRole("button", { name: "查找资料 · 已完成" })
      .getAttribute("aria-expanded"),
  ).toBe("false");
});

it("collapses streaming thoughts at the first answer and never auto-opens later work during the answer", () => {
  const thought: ToolCall = {
    ...tool,
    id: "thought",
    name: "agent_turn",
    output_preview: {
      text: "正在核对实际返回的资料。",
      items: [],
      notice: "",
      truncated: false,
    },
  };
  const view = render(<ToolActivity tools={[thought, tool]} running />);
  const parent = screen.getByRole("button", { name: /处理进度/ });
  expect(screen.getByText(thought.output_preview!.text)).toBeTruthy();
  // The provider may still mark the model/tool as running when text arrives.
  view.rerender(<ToolActivity tools={[thought, tool]} running answering />);
  expect(parent.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText("正在回复")).toBeTruthy();
  expect(screen.queryByText(thought.output_preview!.text)).toBeNull();
  expect(screen.queryByRole("list")).toBeNull();
  const next = { ...tool, id: "later", name: "web_search" as const };
  view.rerender(
    <ToolActivity tools={[thought, tool, next]} running answering />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(parent);
  const row = screen.getByRole("button", { name: "思考摘要 · 进行中" });
  expect(row.getAttribute("aria-expanded")).toBe("false");
  expect(
    screen
      .getByRole("button", { name: "搜索公开信息 · 进行中" })
      .getAttribute("aria-expanded"),
  ).toBe("false");
  fireEvent.click(row);
  const updated = {
    ...thought,
    output_preview: {
      ...thought.output_preview!,
      text: "核对完毕，继续回复。",
    },
  };
  view.rerender(
    <ToolActivity
      tools={[updated, tool, { ...next, status: "retrying", attempt: 2 }]}
      running
      answering
    />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("true");
  expect(row.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText(updated.output_preview.text)).toBeTruthy();
  view.rerender(
    <ToolActivity tools={[updated, tool, next]} running={false} answering />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("false");
});

it("does not reopen manually collapsed progress for text, model rounds, or completed parallel siblings", () => {
  const model: ToolCall = { ...tool, id: "model", name: "agent_turn" };
  const other: ToolCall = { ...tool, id: "image", name: "inspect_image" };
  const view = render(<ToolActivity tools={[model, tool, other]} running />);
  const parent = screen.getByRole("button", { name: /处理进度/ });
  fireEvent.click(parent);
  view.rerender(
    <ToolActivity
      tools={[{ ...model, status: "completed" }, tool, other]}
      running
      answering
    />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("false");
  view.rerender(
    <ToolActivity tools={[{ ...tool, status: "completed" }, other]} running />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("false");
  view.rerender(
    <ToolActivity
      tools={[
        { ...tool, status: "completed" },
        { ...other, status: "completed" },
        { ...model, id: "model-next" },
      ]}
      running
    />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("false");
  view.rerender(
    <ToolActivity
      tools={[
        { ...tool, status: "completed" },
        { ...other, id: "new-image" },
      ]}
      running
    />,
  );
  expect(parent.getAttribute("aria-expanded")).toBe("true");
  const row = screen.getByRole("button", { name: "查看图片 · 进行中" });
  fireEvent.click(row);
  view.rerender(
    <ToolActivity
      tools={[{ ...other, id: "new-image", status: "retrying" }]}
      running
    />,
  );
  expect(row.getAttribute("aria-expanded")).toBe("false");
  view.rerender(
    <ToolActivity
      tools={[{ ...other, id: "new-image", attempt: 2 }]}
      running
    />,
  );
  expect(row.getAttribute("aria-expanded")).toBe("true");
});

it("streams progress through Vercel Task without exposing internal labels or payloads", () => {
  const view = render(<ToolActivity tools={[tool]} running />);
  const trigger = screen.getByRole("button", { name: /处理进度/ });
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("region", { name: /处理进度/ }).id).toBe(
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
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText("已完成 1 项处理")).toBeTruthy();
  fireEvent.click(trigger);
  expect(screen.getByText("1.2 秒")).toBeTruthy();
  expect(screen.queryByText(/retry_after|最多.*次/)).toBeNull();
});

it("respects manual collapse within a phase and follows newly executing tools", () => {
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
    <ToolActivity tools={[{ ...tool, duration_ms: 600 }]} running />,
  );
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  view.rerender(
    <ToolActivity
      tools={[tool, { ...tool, id: "next", name: "inspect_image" }]}
      running
    />,
  );
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("正在处理 2 项工作")).toBeTruthy();
  expect(
    screen
      .getByRole("button", { name: "查找资料 · 进行中" })
      .getAttribute("aria-expanded"),
  ).toBe("true");
  expect(
    screen
      .getByRole("button", { name: "查看图片 · 进行中" })
      .getAttribute("aria-expanded"),
  ).toBe("true");
});

it("never presents disconnected running work as completed", () => {
  render(<ToolActivity tools={[tool]} running={false} />);
  fireEvent.click(screen.getByRole("button", { name: /处理已停止/ }));
  expect(screen.getByText("已停止")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "查找资料 · 已停止" }));
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
  "update_task_brief",
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

it("shows a generic thinking indicator without exposing private internal progress", () => {
  const view = render(<MessageActivity steps={[internal]} running />);
  expect(
    screen
      .getByRole("button", { name: /正在思考/ })
      .getAttribute("aria-expanded"),
  ).toBe("true");
  expect(
    screen.queryByText(/记忆|法律流程|raw-data|agent_turn|智能工具协作/),
  ).toBeNull();
  view.rerender(
    <MessageActivity
      steps={[
        {
          ...internal,
          status: "completed",
          tools: internal.tools!.map((call) => ({
            ...call,
            status: "completed",
          })),
        },
      ]}
    />,
  );
  expect(
    screen
      .getByRole("button", { name: /已完成回复/ })
      .getAttribute("aria-expanded"),
  ).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: /已完成回复/ }));
  expect(
    screen.queryByText(/记忆|法律流程|raw-data|agent_turn|智能工具协作/),
  ).toBeNull();
});

it("expands actual streamed thought summaries and closes them when the next tool starts", () => {
  const thinking: ToolCall = {
    ...tool,
    id: "thinking",
    name: "agent_turn",
    output_preview: { text: "先核对", items: [], notice: "", truncated: false },
  };
  const view = render(<ToolActivity tools={[thinking]} running />);
  const row = screen.getByRole("button", { name: "思考摘要 · 进行中" });
  expect(row.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("先核对")).toBeTruthy();
  const progressed = {
    ...thinking,
    output_preview: {
      ...thinking.output_preview!,
      text: "先核对现有资料，再查阅公开来源。",
    },
  };
  view.rerender(<ToolActivity tools={[progressed]} running />);
  expect(screen.getByText(progressed.output_preview.text)).toBeTruthy();
  view.rerender(
    <ToolActivity
      tools={[{ ...progressed, status: "completed" }, tool]}
      running
    />,
  );
  expect(row.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText(progressed.output_preview.text)).toBeNull();
  expect(
    screen
      .getByRole("button", { name: "查找资料 · 进行中" })
      .getAttribute("aria-expanded"),
  ).toBe("true");
  fireEvent.click(row);
  expect(screen.getByText(progressed.output_preview.text)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "展开结果" })).toBeNull();
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
  expect(screen.getAllByRole("button")).toHaveLength(3);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(
    screen.getByRole("button", { name: "法律分析 · 进行中" }),
  ).toBeTruthy();
  expect(
    screen.queryByText(/记忆|智能工具协作|内部|raw-data|技术记录/),
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

it("displays the entire bounded output with exactly one disclosure and no closed excerpt", () => {
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
  fireEvent.click(screen.getByRole("button", { name: "查找资料 · 已完成" }));
  expect(screen.getByText("主体名称")).toBeTruthy();
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.getByText("待补充")).toBeTruthy();
  expect(screen.getByText("不安全来源")).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "来源原文" }).getAttribute("href"),
  ).toBe("https://example.org/source");
  expect(screen.queryByRole("button", { name: "展开结果" })).toBeNull();
  expect(screen.getByText(text)).toBeTruthy();
  expect(screen.getByText("不安全来源")).toBeTruthy();
  expect(screen.queryByRole("link", { name: "不安全来源" })).toBeNull();
  expect(document.querySelector(".tea-tool-output img")).toBeNull();
  expect(screen.getByText("此处为本次执行结果的节选。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "查找资料 · 已完成" }));
  expect(screen.queryByText(text)).toBeNull();
  expect(screen.queryByText("不安全来源")).toBeNull();
});

it("shows business card preparation without claiming the pending transaction is saved", () => {
  render(
    <ToolActivity
      tools={[
        {
          ...tool,
          name: "update_business_card",
          output_preview: {
            text: "",
            items: [],
            notice: "答复成功后写入，供茶商核对。",
            truncated: false,
          },
        },
      ]}
      running
    />,
  );
  expect(
    screen.getByRole("button", { name: "整理业务卡 · 进行中" }),
  ).toBeTruthy();
  expect(screen.getByText("答复成功后写入，供茶商核对。")).toBeTruthy();
});
