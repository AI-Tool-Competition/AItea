import type { ToolOutputDelta, TraceStep } from "./types";

/** An ordered delta belongs to exactly one running call, never to the answer. */
export function mergeToolDelta(
  steps: readonly TraceStep[],
  delta: ToolOutputDelta,
): TraceStep[] {
  return steps.map((step) => {
    if (
      step.id !== delta.step_id ||
      step.run_id !== delta.run_id ||
      step.status !== "running" ||
      (step.sequence ?? 0) >= delta.sequence ||
      !step.tools?.some(
        (tool) => tool.id === delta.tool_id && tool.status === "running",
      )
    )
      return step;
    return {
      ...step,
      sequence: delta.sequence,
      tools: step.tools.map((tool) => {
        if (tool.id !== delta.tool_id) return tool;
        const output = tool.output_preview ?? {
          text: "",
          items: [],
          notice: "",
          truncated: false,
        };
        const text = Array.from(output.text + delta.text);
        const limit = tool.name === "agent_turn" ? 60000 : 4000;
        return {
          ...tool,
          output_preview: {
            ...output,
            text: text.slice(0, limit).join(""),
            truncated: output.truncated || text.length > limit,
          },
        };
      }),
    };
  });
}

/** Replace a snapshot in its original position; late progress cannot undo completion. */
export function mergeTraceStep(
  steps: readonly TraceStep[],
  incoming: TraceStep,
): TraceStep[] {
  const index = steps.findIndex((step) => step.id === incoming.id);
  if (index < 0) return [...steps, incoming];
  const previous = steps[index]!;
  if (
    previous.run_id &&
    previous.run_id === incoming.run_id &&
    previous.sequence !== undefined &&
    incoming.sequence !== undefined &&
    incoming.sequence <= previous.sequence
  )
    return [...steps];
  return steps.map((step, position) => (position === index ? incoming : step));
}

export function settleTraceSteps(
  steps: readonly TraceStep[],
  status: "failed" | "cancelled",
): TraceStep[] {
  return steps.map((step) => ({
    ...step,
    ...(step.status === "running" ? { status } : {}),
    ...(step.tools
      ? {
          tools: step.tools.map((tool) =>
            tool.status === "running" || tool.status === "retrying"
              ? {
                  ...tool,
                  status,
                  detail:
                    status === "cancelled"
                      ? "已停止，结果尚未完成。"
                      : "连接已中断，结果尚未确认。",
                }
              : tool,
          ),
        }
      : {}),
  }));
}
