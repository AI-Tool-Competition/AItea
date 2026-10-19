import { expect, it } from "@rstest/core";

import { mergeTraceStep, settleTraceSteps } from "@/core/tea/trace";
import type { ToolCall, TraceStep } from "@/core/tea/types";

const tool: ToolCall = {
  id: "run:1",
  name: "search_documents",
  label: "检索资料",
  position: 1,
  status: "running",
  attempt: 1,
  max_attempts: 2,
  detail: "正在检索",
  duration_ms: 0,
};
const step: TraceStep = {
  id: "search",
  label: "查找依据",
  status: "running",
  detail: "",
  run_id: "run",
  sequence: 1,
  tools: [tool],
};

it("keeps interleaved progress in its first position and ignores stale snapshots", () => {
  const other = { ...step, id: "check", sequence: 2 };
  const done: TraceStep = {
    ...step,
    status: "completed",
    sequence: 4,
    tools: [{ ...tool, status: "completed" }],
  };
  const result = mergeTraceStep([step, other], done);
  expect(result.map((item) => item.id)).toEqual(["search", "check"]);
  expect(mergeTraceStep(result, { ...step, sequence: 3 })).toEqual(result);
  expect(mergeTraceStep(result, done)).toEqual(result);
});

it("stops only unfinished tools and preserves completed evidence", () => {
  const result = settleTraceSteps(
    [
      {
        ...step,
        tools: [
          { ...tool, status: "completed" },
          { ...tool, id: "run:2", status: "retrying" },
        ],
      },
    ],
    "cancelled",
  );
  expect(result[0]?.status).toBe("cancelled");
  expect(result[0]?.tools?.map((item) => item.status)).toEqual([
    "completed",
    "cancelled",
  ]);
  expect(step.status).toBe("running");
});
