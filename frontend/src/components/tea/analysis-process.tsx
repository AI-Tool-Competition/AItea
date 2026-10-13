"use client";

// Reuses the retained copy of tmp/deer-flow/frontend/src/components/ai-elements/
// chain-of-thought.tsx. License: frontend/DEERFLOW_LICENSE.
import {
  Check,
  Circle,
  CircleAlert,
  History,
  ListChecks,
  LoaderCircle,
  Minus,
  Square,
} from "lucide-react";
import { useEffect, useId, useState } from "react";

import {
  ChainOfThought,
  ChainOfThoughtContent,
  ChainOfThoughtHeader,
  ChainOfThoughtStep,
} from "@/components/ai-elements/chain-of-thought";
import type { TraceStep } from "@/core/tea/types";

type ProcessStep = Omit<TraceStep, "status"> & {
  status: TraceStep["status"] | "pending";
};
const stepLabels: Record<string, string> = {
  qa01: "理解业务问题",
  qa02: "梳理事实与缺项",
  qa03: "确认适用规则",
  qa04: "查找与核对依据",
  qa05: "分析结论与期限",
  qa06: "整理报告与下一步",
};
const stepIcons = {
  completed: Check,
  cached: History,
  skipped: Minus,
  failed: CircleAlert,
  cancelled: Square,
  pending: Circle,
  running: LoaderCircle,
};

export function AnalysisProcess({
  steps,
  running = false,
  incomplete = false,
  title = "分析过程",
}: {
  steps: readonly ProcessStep[];
  running?: boolean;
  incomplete?: boolean;
  title?: string;
}) {
  const id = useId();
  const [expanded, setExpanded] = useState<boolean | null>(null);
  useEffect(() => setExpanded(null), [running]);
  if (!steps.length) return null;
  const resolved = steps.filter((step) =>
    ["completed", "cached", "skipped"].includes(step.status),
  ).length;
  const failed = incomplete || steps.some((step) => step.status === "failed");
  const cancelled = steps.some((step) => step.status === "cancelled");
  const heading = running
    ? "正在分析"
    : failed
      ? "分析未完成"
      : cancelled
        ? "分析已停止"
        : title;
  const statuses = {
    pending: running ? "等待中" : "未执行",
    running: running ? "分析中" : "已中断",
    completed: "已完成",
    cached: "沿用结果",
    skipped: "无需执行",
    failed: "未完成",
    cancelled: "已停止",
  };
  return (
    <ChainOfThought
      className="tea-analysis-process"
      open={expanded ?? running}
      onOpenChange={setExpanded}
    >
      <ChainOfThoughtHeader
        id={id + "-trigger"}
        aria-controls={id + "-content"}
        className="tea-analysis-trigger"
        icon={
          running ? (
            <LoaderCircle className="tea-spin size-4" />
          ) : failed ? (
            <CircleAlert className="size-4" />
          ) : (
            <ListChecks className="size-4" />
          )
        }
      >
        <span className="tea-analysis-heading">
          <span>{heading}</span>
          <span className="tea-analysis-count">
            {resolved} / {steps.length} 步
          </span>
        </span>
      </ChainOfThoughtHeader>
      <ChainOfThoughtContent
        id={id + "-content"}
        role="region"
        aria-labelledby={id + "-trigger"}
        className="tea-analysis-content"
      >
        <div className="tea-analysis-steps" role="list" aria-label="分析步骤">
          {steps.map((step) => {
            const Icon = stepIcons[step.status];
            const input = step.input_data ?? step.input;
            const output = step.output_data ?? step.output;
            return (
              <ChainOfThoughtStep
                key={step.id}
                role="listitem"
                className="tea-analysis-step"
                data-state={step.status}
                status={
                  step.status === "pending"
                    ? "pending"
                    : step.status === "running" && running
                      ? "active"
                      : "complete"
                }
                icon={
                  <Icon
                    className={
                      step.status === "running" && running
                        ? "tea-spin size-4"
                        : "size-4"
                    }
                  />
                }
                label={
                  <div className="tea-analysis-step-heading">
                    <strong title={step.label}>
                      {Object.hasOwn(stepLabels, step.id)
                        ? stepLabels[step.id]
                        : step.label}
                    </strong>
                    <span>{statuses[step.status]}</span>
                  </div>
                }
                description={
                  step.detail ? (
                    <p className="tea-analysis-step-detail">{step.detail}</p>
                  ) : undefined
                }
              >
                {(input !== undefined || output !== undefined) && (
                  <details className="tea-analysis-records">
                    <summary>查看输入与结果</summary>
                    {input !== undefined && (
                      <div>
                        <strong>输入记录</strong>
                        <pre>
                          {typeof input === "string"
                            ? input
                            : JSON.stringify(input, null, 2)}
                        </pre>
                      </div>
                    )}
                    {output !== undefined && (
                      <div>
                        <strong>结果记录</strong>
                        <pre>
                          {typeof output === "string"
                            ? output
                            : JSON.stringify(output, null, 2)}
                        </pre>
                      </div>
                    )}
                  </details>
                )}
              </ChainOfThoughtStep>
            );
          })}
        </div>
      </ChainOfThoughtContent>
    </ChainOfThought>
  );
}
