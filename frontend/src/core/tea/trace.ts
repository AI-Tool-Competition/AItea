import type { TraceStep } from "./types";

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
