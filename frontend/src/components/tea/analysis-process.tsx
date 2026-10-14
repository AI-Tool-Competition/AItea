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
import {
  presentProcessStep,
  processStepLabels,
  type ProcessFinding,
  type ProcessSection,
  type ProcessStep,
} from "@/core/tea/analysis-process";

import { ReadableData } from "./readable-data";

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
            const hasOutput =
              typeof output === "string"
                ? !!output.trim()
                : !!output && Object.keys(output).length > 0;
            const view = presentProcessStep(step, steps);
            return (
              <ChainOfThoughtStep
                key={step.id}
                role="listitem"
                className="tea-analysis-step"
                data-state={step.status}
                data-step-id={step.id}
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
                    <strong>
                      {Object.hasOwn(processStepLabels, step.id)
                        ? processStepLabels[step.id]
                        : step.label}
                    </strong>
                    <span>{statuses[step.status]}</span>
                  </div>
                }
                description={
                  view.description ? (
                    <p className="tea-analysis-step-detail">
                      {view.description}
                    </p>
                  ) : undefined
                }
              >
                {view.warning && (
                  <p className="tea-analysis-attention">{view.warning}</p>
                )}
                {(input !== undefined || output !== undefined) && (
                  <details className="tea-analysis-records">
                    <summary>查看本步发现</summary>
                    <div className="tea-analysis-findings">
                      {view.context.length > 0 && (
                        <ul
                          className="tea-analysis-context"
                          aria-label="分析范围"
                        >
                          {view.context.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      )}
                      {view.sections.length ? (
                        view.sections.map((section) => (
                          <FindingSection
                            key={section.title}
                            section={section}
                          />
                        ))
                      ) : (
                        <>
                          {input !== undefined &&
                            !(
                              Object.hasOwn(processStepLabels, step.id) &&
                              view.context.length
                            ) && (
                              <section>
                                <h4>参考信息</h4>
                                <ReadableData value={input} />
                              </section>
                            )}
                          {hasOutput ? (
                            <section>
                              <h4>本步结果</h4>
                              <ReadableData value={output} />
                            </section>
                          ) : (
                            <p className="tea-analysis-empty">
                              {step.status === "running" && running
                                ? "正在整理，本步完成后会显示结果。"
                                : "本步暂无可展示的结果。"}
                            </p>
                          )}
                        </>
                      )}
                      <TechnicalRecords
                        step={step}
                        input={input}
                        output={output}
                      />
                    </div>
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

function Finding({ item }: { item: ProcessFinding }) {
  return (
    <li className="tea-analysis-finding">
      <strong>{item.title}</strong>
      {item.details?.map((detail, index) => (
        <p key={index}>{detail}</p>
      ))}
      {!!item.attention?.length && (
        <div className="tea-analysis-attention">
          <span>待核实</span>
          <ul>
            {item.attention.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

function FindingSection({ section }: { section: ProcessSection }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const visible = expanded ? section.items : section.items.slice(0, 3);
  return (
    <section>
      <h4>
        {section.title}
        <span>{section.items.length} 项</span>
      </h4>
      {section.items.length ? (
        <ol id={id} className="tea-analysis-finding-list">
          {visible.map((item, index) => (
            <Finding key={index} item={item} />
          ))}
        </ol>
      ) : (
        <p className="tea-analysis-empty">{section.empty}</p>
      )}
      {section.items.length > 3 && (
        <button
          type="button"
          className="tea-analysis-more"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded
            ? "收起更多内容"
            : `展开其余 ${section.items.length - 3} 项`}
        </button>
      )}
    </section>
  );
}

function TechnicalRecords({
  step,
  input,
  output,
}: {
  step: ProcessStep;
  input: unknown;
  output: unknown;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="tea-analysis-technical"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>技术记录</summary>
      {open && (
        <div>
          <p>保留本步的运行说明与原始数据，供核对或排查问题。</p>
          {step.detail && <p>{step.detail}</p>}
          {input !== undefined && (
            <div>
              <strong>原始输入</strong>
              <pre>
                {typeof input === "string"
                  ? input
                  : JSON.stringify(input, null, 2)}
              </pre>
            </div>
          )}
          {output !== undefined && (
            <div>
              <strong>原始结果</strong>
              <pre>
                {typeof output === "string"
                  ? output
                  : JSON.stringify(output, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </details>
  );
}
