"use client";

import { Check, LoaderCircle, Minus, Pause, X } from "lucide-react";

import type {
  DebugEvaluation,
  DebugRun,
  DebugRunSummary,
  TraceStep,
} from "@/core/tea/types";

import { CitationCard } from "./library";
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

const labels: Record<string, string> = {
  query: "检索内容",
  limit: "最多引用段数",
  question: "问题",
  tasks: "任务资料",
  title: "名称",
  label: "填写项",
  values: "实际填写",
  fields: "字段规则",
  required: "必填",
  min: "最小值",
  type: "类型",
  placeholder: "填写提示",
  require_document: "需要关联材料",
  require_confirmation: "需要人工确认",
  confirmed: "已人工确认",
  citations: "原文依据",
  excerpt: "原文片段",
  checks: "检查结果",
  reason: "说明",
  status: "状态",
  mode: "检索方式",
  content: "内容",
  notice: "说明",
  case_context: "业务资料",
  stage: "办理节点",
  origin: "出发地",
  destination: "目的地",
  test_input: "样例问题",
  previous_step_output: "上一步的建议",
  instructions: "整体分析要求",
  step_instructions: "本步分析要求",
  language: "输出语言",
  instructions_applied: "分析要求已用于模型",
  document_version: "资料版本",
  metadata: "资料信息",
  truncated: "内容已按上限截取",
  summary: "摘要",
  task_title: "任务",
  filename: "文件名",
  reviewed_at: "审核时间",
  classification: "分类",
  jurisdiction: "适用地区",
  valid_from: "生效日期",
  valid_until: "失效日期",
  review_status: "审核状态",
  reference_status: "适用状态",
  source_url: "来源",
  document_ids: "限定资料",
  evidence_ids: "关联材料",
  evidence_refs: "依据",
  score: "相关程度",
};
const hidden = new Set([
  "id",
  "task_id",
  "document_id",
  "key",
  "metadata_revision",
  "locator",
  "content_hash",
  "family_id",
  "effective_version_id",
]);
const valueLabels: Record<string, string> = {
  passed: "通过",
  missing: "待补充",
  conflict: "有冲突",
  lexical: "关键词检索",
  hybrid: "混合检索",
  number: "数字",
  text: "短文字",
  textarea: "多行文字",
  approved: "已审核",
  draft: "待审核",
  active: "可用",
  superseded: "已被替代",
  law: "法规",
  guidance: "指南",
  manual: "人工确认",
};
function ReadableData({
  value,
  depth = 0,
}: {
  value: unknown;
  depth?: number;
}) {
  if (value === null || value === undefined)
    return <span className="ta-hint">未设置</span>;
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (typeof value === "string" || typeof value === "number")
    return (
      <span className="ta-debug-value">
        {valueLabels[String(value)] ??
          (value === "" ? "未填写" : String(value))}
      </span>
    );
  if (Array.isArray(value))
    return value.length ? (
      <div className="ta-debug-data-list">
        {value.map((item, index) => (
          <div key={index}>
            <ReadableData value={item} depth={depth + 1} />
          </div>
        ))}
      </div>
    ) : (
      <span className="ta-hint">无</span>
    );
  if (typeof value !== "object") return <span>未设置</span>;
  const record = value as Record<string, unknown>;
  if (depth > 5) return <span className="ta-hint">详见原始数据</span>;
  if (
    typeof record.label === "string" &&
    typeof record.status === "string" &&
    ["passed", "missing", "conflict"].includes(record.status)
  ) {
    return (
      <div className="ta-debug-check-row" data-status={record.status}>
        <strong>{record.label}</strong>
        <span>{valueLabels[record.status]}</span>
        <small>
          {typeof record.task_title === "string" && `${record.task_title} · `}
          {typeof record.reason === "string" && record.reason}
        </small>
      </div>
    );
  }
  if (
    Array.isArray(record.fields) &&
    record.values &&
    typeof record.values === "object"
  ) {
    const fields = record.fields as Array<{
      key: string;
      label: string;
      required?: boolean;
      min?: number;
    }>;
    const values = record.values as Record<string, unknown>;
    return (
      <div className="ta-debug-task-data">
        <strong>
          {typeof record.title === "string" ? record.title : "任务"}
        </strong>
        <dl>
          {fields.map((field) => (
            <div key={field.key}>
              <dt>
                {field.label}
                {field.required ? "（必填）" : ""}
                {field.min !== null && field.min !== undefined
                  ? ` ≥ ${field.min}`
                  : ""}
              </dt>
              <dd>
                <ReadableData value={values[field.key] ?? "未填写"} />
              </dd>
            </div>
          ))}
        </dl>
        {record.require_document === true && (
          <p className="ta-hint">需要关联材料</p>
        )}
        {record.require_confirmation === true && (
          <p className="ta-hint">需要人工确认</p>
        )}
      </div>
    );
  }
  const entries = Object.entries(record).filter(([key]) => !hidden.has(key));
  return (
    <dl className="ta-debug-data">
      {entries.map(([key, item]) => (
        <div key={key}>
          <dt>{labels[key] ?? key}</dt>
          <dd>
            {["document_ids", "evidence_ids", "evidence_refs"].includes(key) ? (
              <span>
                {Array.isArray(item) ? `${item.length} 份` : "当前资料库"}
              </span>
            ) : (
              <ReadableData value={item} depth={depth + 1} />
            )}
          </dd>
        </div>
      ))}
    </dl>
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
    agent(run)
      .steps.filter((step) => step.enabled)
      .map((step) => step.label)
      .join(" → ");
  add("步骤顺序", stepNames(previous), stepNames(current));
  for (const step of agent(current).steps) {
    const old = agent(previous).steps.find((item) => item.id === step.id);
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
