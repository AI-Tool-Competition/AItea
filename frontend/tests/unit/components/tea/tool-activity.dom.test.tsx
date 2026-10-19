import { afterEach, expect, it } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ToolActivity } from "@/components/tea/tool-activity";
import type { ToolCall } from "@/core/tea/types";

afterEach(cleanup);
const tool: ToolCall = {
  id: "private-call-id",
  name: "search_documents",
  label: "检索业务资料",
  position: 1,
  status: "running",
  attempt: 1,
  max_attempts: 2,
  detail: "正在查找本业务的原文。",
  duration_ms: 0,
};

it("streams ordered tool states and retains an explicit expansion through completion", () => {
  const view = render(<ToolActivity tools={[tool]} running />);
  const trigger = screen.getByRole("button", { name: /工具调用/ });
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("正在查找本业务的原文。")).toBeTruthy();
  expect(screen.queryByText(/private-call-id|search_documents/)).toBeNull();
  fireEvent.click(trigger);
  fireEvent.click(trigger);
  view.rerender(
    <ToolActivity
      tools={[{ ...tool, status: "retrying", detail: "服务繁忙，稍后重试。" }]}
      running
    />,
  );
  expect(screen.getByText("等待重试")).toBeTruthy();
  view.rerender(
    <ToolActivity
      tools={[
        {
          ...tool,
          status: "completed",
          attempt: 2,
          duration_ms: 1200,
          detail: "找到 3 条依据。",
        },
      ]}
      running={false}
    />,
  );
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText("找到 3 条依据。")).toBeTruthy();
  expect(screen.getByText("1.2 秒")).toBeTruthy();
  expect(screen.getByText("第 2 次尝试 · 最多 2 次")).toBeTruthy();
});

it("never presents disconnected running work as completed", () => {
  render(<ToolActivity tools={[tool]} running={false} />);
  fireEvent.click(screen.getByRole("button", { name: /工具调用/ }));
  expect(screen.getByText("已停止")).toBeTruthy();
  expect(screen.getByText("调用已中断，结果尚未确认。")).toBeTruthy();
  expect(screen.queryByText("已完成")).toBeNull();
});
