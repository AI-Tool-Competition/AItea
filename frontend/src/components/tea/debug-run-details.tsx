"use client";

import { Check, LoaderCircle, Minus, Pause, X } from "lucide-react";

import { analysisSteps } from "@/core/tea/configuration";
import { countryLabel } from "@/core/tea/countries";
import type {
  DebugEvaluation,
  DebugRun,
  DebugRunSummary,
  TraceStep,
} from "@/core/tea/types";

import { CitationCard } from "./library";
import { ReadableData, valueLabels } from "./readable-data";
import { Checks } from "./shared";

const stepLabels: Record<TraceStep["status"], string> = {
  running: "运行中",
  completed: "已执行",
  cached: "沿用上次",
  failed: "失败",
  skipped: "未执行",
  cancelled: "已停止",
};
export function runLabel(status: DebugRun["status"]) {
  return {
    completed: "执行完成",
    paused: "已暂停",
    failed: "执行失败",
    cancelled: "已停止",
  }[status];
}
export function evaluationLabel(
  value: DebugEvaluation[] | DebugRunSummary["evaluation_status"],
) {
  const status =
    typeof value === "string"
      ? value
      : !value.length
        ? "none"
        : value.some((item) => item.status === "failed")
          ? "failed"
          : value.some((item) => item.status === "manual")
            ? "manual"
            : "passed";
  return {
    none: "未设置预期",
    failed: "未符合预期",
    manual: "待人工确认",
    passed: "自动检查通过",
  }[status];
}
export function DebugRunDetails({
  result,
  trace,
  selectedId,
  selectedLabel,
  onStep,
  liveOutput,
  running,
}: {
  result: DebugRun | null;
  trace: TraceStep[];
  selectedId?: string;
  selectedLabel?: string;
  onStep: (id: string) => void;
  liveOutput: string;
  running: boolean;
}) {
  const selected = selectedId
    ? trace.find((item) => item.id === selectedId)
    : (trace.find((item) => item.status === "failed") ?? trace.at(-1));
  return (
    <div className="ta-debug-results">
      <div className="ta-debug-step-list">
        {trace.map((step) => (
          <button
            key={step.id}
            aria-pressed={selected?.id === step.id}
            onClick={() => onStep(step.id)}
          >
            <StepIcon status={step.status} />
            <span>{step.label}</span>
            <small>
              {stepLabels[step.status]}
              {step.duration_ms !== undefined && step.status !== "cached"
                ? ` · ${(step.duration_ms / 1000).toFixed(1)} 秒`
                : ""}
            </small>
          </button>
        ))}
      </div>
      {selectedId && !selected && (
        <section className="ta-debug-step-detail">
          <strong>{selectedLabel ?? "选中步骤"}</strong>
          <p className="ta-hint">
            {selectedLabel ? "此步骤尚未运行。" : "这次运行没有该步骤的记录。"}
          </p>
        </section>
      )}
      {selected && (
        <section className="ta-debug-step-detail">
          <div className="ta-debug-section-heading">
            <strong>{selected.label}</strong>
            <span>{stepLabels[selected.status]}</span>
          </div>
          <p className="ta-hint">{selected.detail}</p>
          <details open>
            <summary>收到的输入</summary>
            {selected.input_data ? (
              <ReadableData value={selected.input_data} />
            ) : (
              <p className="ta-output-text">
                {selected.input === ""
                  ? "此步骤尚未收到输入。"
                  : (selected.input ?? "此步骤尚未收到输入。")}
              </p>
            )}
          </details>
          <details open>
            <summary>产生的结果</summary>
            {selected.output_data ? (
              <ReadableData value={selected.output_data} />
            ) : (
              <p className="ta-output-text">
                {selected.output === ""
                  ? "暂无输出。"
                  : (selected.output ?? "暂无输出。")}
              </p>
            )}
          </details>
          {(selected.input_data !== undefined ||
            selected.output_data !== undefined) && (
            <details>
              <summary>原始数据</summary>
              <pre className="ta-debug-raw">
                {JSON.stringify(
                  { input: selected.input_data, output: selected.output_data },
                  null,
                  2,
                )}
              </pre>
            </details>
          )}
        </section>
      )}
      {liveOutput && (
        <section>
          <h4>正在生成</h4>
          <p className="ta-output-text">{liveOutput}</p>
        </section>
      )}
      {result && !running && (
        <>
          <div className="ta-debug-result-status">
            <span>{runLabel(result.status)}</span>
            <span>
              {result.assistant_mode === "live" ? "实际模型" : "演示规则"}
            </span>
          </div>
          {result.error && (
            <p role="alert" className="ta-debug-notice">
              {result.error.message}
            </p>
          )}
          <section className="ta-debug-evaluations">
            <h4>预期检查 · {evaluationLabel(result.evaluations)}</h4>
            {result.evaluations.map((item) => (
              <div key={item.id} data-status={item.status}>
                <StepIcon
                  status={
                    item.status === "passed"
                      ? "completed"
                      : item.status === "failed"
                        ? "failed"
                        : "skipped"
                  }
                />
                <div>
                  <strong>{item.label}</strong>
                  <p>{item.actual}</p>
                  {item.status === "manual" && <small>待人工确认</small>}
                </div>
              </div>
            ))}
          </section>
          <details className="ta-debug-disclosure" open>
            <summary>最终建议</summary>
            <p className="ta-output-text">{result.output}</p>
          </details>
          {!!result.checks.length && (
            <details className="ta-debug-disclosure">
              <summary>全部检查 · {result.checks.length} 项</summary>
              <Checks checks={result.checks} />
            </details>
          )}
          {!!result.citations.length && (
            <details className="ta-debug-disclosure">
              <summary>原文依据 · {result.citations.length} 处</summary>
              {result.citations.map((citation, index) => (
                <CitationCard
                  key={`${citation.document_id}:${index}`}
                  citation={citation}
                />
              ))}
            </details>
          )}
        </>
      )}
    </div>
  );
}
function StepIcon({ status }: { status: TraceStep["status"] }) {
  const Icon =
    status === "running"
      ? LoaderCircle
      : status === "failed"
        ? X
        : status === "completed" || status === "cached"
          ? Check
          : status === "cancelled"
            ? Pause
            : Minus;
  return (
    <Icon
      size={14}
      aria-label={stepLabels[status]}
      className={status === "running" ? "ta-running-icon" : undefined}
    />
  );
}

export function DebugRunComparison({
  current,
  previous,
}: {
  current: DebugRun | null;
  previous: DebugRun | null;
}) {
  if (!current || !previous)
    return (
      <p className="ta-hint">完成两次试运行后，在这里查看修改前后的变化。</p>
    );
  const changes: Array<{ label: string; before: string; after: string }> = [];
  const add = (label: string, before: string, after: string) => {
    if (before !== after) changes.push({ label, before, after });
  };
  add("测试问题", previous.request.input, current.request.input);
  add(
    "目的地",
    previous.request.template.destination,
    current.request.template.destination,
  );
  const country = (run: DebugRun) =>
    run.country === undefined
      ? "旧记录未标注"
      : countryLabel(run.country ?? "unassigned");
  add("资料国家", country(previous), country(current));
  const agent = (run: DebugRun) =>
    run.request.template.stages.find((stage) => stage.id === run.stage_id)
      ?.analysis ?? run.request.template.agent;
  add(
    "整体分析要求",
    agent(previous).instructions,
    agent(current).instructions,
  );
  add("输出语言", agent(previous).language, agent(current).language);
  add(
    "检索段数",
    String(agent(previous).retrieval_limit),
    String(agent(current).retrieval_limit),
  );
  const stepNames = (run: DebugRun) =>
    analysisSteps(agent(run))
      .filter((step) => step.enabled)
      .map((step) => step.label)
      .join(" → ");
  add("步骤顺序", stepNames(previous), stepNames(current));
  const legalDescription = (run: DebugRun) => {
    const policy = agent(run).legal;
    if (!policy) return "未配置";
    return (
      `${policy.enabled ? "已启用" : "未启用"}；额外法域：${policy.jurisdictions.join("、") || "无"}；每组追问 ${policy.question_limit} 项，最多 ${policy.max_rounds} 轮，检索 ${policy.retrieval_limit} 段\n` +
      policy.rules
        .map(
          (rule) =>
            `${rule.title}（${rule.module.toUpperCase()}，${rule.enabled ? "启用" : "停用"}，${rule.execution === "manual" ? "人工审核" : "模型分析"}）\n领域：${rule.domains.join("、") || "通用"}；国家：${rule.jurisdictions.join("、") || "通用"}；触发：${rule.condition}\n分析：${rule.instructions}\n事实：${rule.required_facts.map((fact) => `${fact.label}：${fact.purpose}${fact.decisive ? "（关键）" : ""}`).join("；")}\n输出：${rule.output_requirements}；${rule.review_required ? "需复核" : "无额外复核"}；来源：${rule.source_reference}`,
        )
        .join("\n\n")
    );
  };
  add("法律分析规则", legalDescription(previous), legalDescription(current));
  for (const step of analysisSteps(agent(current))) {
    const old = analysisSteps(agent(previous)).find(
      (item) => item.id === step.id,
    );
    add(
      `${step.label} · 分析要求`,
      old?.instructions ?? "",
      step.instructions ?? "",
    );
  }
  for (const stage of current.request.template.stages.filter(
    (item) => item.id === current.stage_id,
  ))
    for (const task of stage.tasks)
      for (const field of task.fields)
        add(
          `${task.title} · ${field.label}`,
          String(
            previous.request.task_values[task.id]?.[field.key] ?? "未填写",
          ),
          String(current.request.task_values[task.id]?.[field.key] ?? "未填写"),
        );
  const beforeChecks = new Map(
    previous.checks.map((item) => [`${item.task_id}:${item.id}`, item]),
  );
  for (const item of current.checks) {
    const old = beforeChecks.get(`${item.task_id}:${item.id}`);
    add(
      `${item.task_title ?? "检查"} · ${item.label}`,
      old ? `${valueLabels[old.status]}：${old.reason}` : "未执行",
      `${valueLabels[item.status]}：${item.reason}`,
    );
    beforeChecks.delete(`${item.task_id}:${item.id}`);
  }
  for (const old of beforeChecks.values())
    add(
      `检查 · ${old.label}`,
      `${valueLabels[old.status]}：${old.reason}`,
      "未执行",
    );
  const citations = (run: DebugRun) =>
    run.citations
      .map(
        (item) =>
          `${item.title}（第 ${item.document_version ?? 1} 版）\n${item.excerpt}`,
      )
      .join("\n\n") || "无";
  add("引用依据", citations(previous), citations(current));
  add("最终建议", previous.output, current.output);
  return (
    <div className="ta-debug-comparison">
      <div className="ta-debug-comparison-status">
        <span>上次：{evaluationLabel(previous.evaluations)}</span>
        <span>本次：{evaluationLabel(current.evaluations)}</span>
      </div>
      <p className="ta-hint">
        {changes.length
          ? `${changes.length} 处变化`
          : "输入、检查与输出未发现变化。"}{" "}
        · 两次运行均保留历史
      </p>
      {changes.map((change, index) => (
        <details open key={`${change.label}:${index}`}>
          <summary>{change.label}</summary>
          <div className="ta-debug-before">
            <small>上次</small>
            <p>{change.before || "未填写"}</p>
          </div>
          <div className="ta-debug-after">
            <small>本次</small>
            <p>{change.after || "未填写"}</p>
          </div>
        </details>
      ))}
    </div>
  );
}
