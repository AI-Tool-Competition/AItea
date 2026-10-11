"use client";

import {
  CheckCircle2,
  ChevronRight,
  FlaskConical,
  Play,
  RotateCcw,
  Square,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage, teaRequest } from "@/core/tea/api";
import {
  type DebugResult,
  type Template,
  type TraceStep,
} from "@/core/tea/types";

import { CitationCard } from "./library";
import { ActionButton, Checks, ErrorNotice } from "./shared";

export function ConfigurationDebugger({
  draft,
  stageId,
  stepId,
  onTrace,
}: {
  draft: Template;
  stageId: string;
  stepId?: string;
  onTrace: (steps: TraceStep[]) => void;
}) {
  const stage =
    draft.stages.find((item) => item.id === stageId) ?? draft.stages[0]!;
  const [input, setInput] = useState(
    "请分析当前资料，告诉我还缺什么，以及下一步应该做什么。",
  );
  const [values, setValues] = useState<
    Record<string, Record<string, string | number>>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{
    data: DebugResult;
    signature: string;
  }>();
  const [tab, setTab] = useState<"output" | "trace" | "checks" | "citations">(
    "output",
  );
  const active = useRef<AbortController | null>(null);
  const signature = JSON.stringify([draft, stage.id, input, values]);
  const latest = useRef(signature);
  latest.current = signature;
  useEffect(() => () => active.current?.abort(), []);
  const currentResult =
    result?.signature === signature ? result.data : undefined;
  function changeValues(taskId: string, field: string, value: string | number) {
    setValues((current) => ({
      ...current,
      [taskId]: { ...current[taskId], [field]: value },
    }));
    onTrace([]);
  }
  async function run(stopAtStep = false) {
    if (active.current) return;
    const abort = new AbortController();
    active.current = abort;
    setBusy(true);
    setError("");
    onTrace([]);
    const submitted = signature;
    try {
      const data = await teaRequest<DebugResult>("/templates/debug", {
        method: "POST",
        signal: abort.signal,
        body: JSON.stringify({
          template: draft,
          stage_id: stage.id,
          input,
          task_values: Object.fromEntries(
            stage.tasks.map((task) => [
              task.id,
              Object.fromEntries(
                Object.entries(values[task.id] ?? {}).filter(([key]) =>
                  task.fields.some((field) => field.key === key),
                ),
              ),
            ]),
          ),
          ...(stopAtStep && stepId ? { stop_after_step_id: stepId } : {}),
        }),
      });
      if (abort.signal.aborted) return;
      if (latest.current !== submitted) {
        setError("配置或样例已变化，本次结果已过期。请重新试运行。");
        return;
      }
      setResult({ data, signature: submitted });
      onTrace(data.trace);
      setTab("output");
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      if (!abort.signal.aborted) setBusy(false);
      active.current = null;
    }
  }
  return (
    <section className="ta-debugger" aria-label="配置试运行">
      <div className="ta-debug-heading">
        <div>
          <FlaskConical size={18} />
          <h3>试运行</h3>
          <span className="ta-badge">独立测试</span>
        </div>
        <p>使用当前草稿与样例资料，不修改实际业务。</p>
      </div>
      <div className="ta-debug-grid">
        <fieldset disabled={busy} className="ta-debug-input">
          <label className="tea-field">
            <span>测试节点</span>
            <Input readOnly value={stage.name} />
          </label>
          <label className="tea-field">
            <span>给助手的测试问题</span>
            <Textarea
              maxLength={20000}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                onTrace([]);
              }}
            />
          </label>
          <details className="ta-sample-fields">
            <summary>
              填写样例资料{" "}
              <small>
                {stage.tasks.reduce((sum, task) => sum + task.fields.length, 0)}{" "}
                项
              </small>
            </summary>
            <p className="ta-hint">
              留空可以测试缺项提示。材料关联和人工确认仍由实际业务核验。
            </p>
            {stage.tasks.map((task) => (
              <div key={task.id}>
                <h4>{task.title}</h4>
                {task.fields.map((field) => (
                  <label className="tea-field" key={field.key}>
                    <span>
                      {field.label}
                      {field.required ? " · 必填" : ""}
                    </span>
                    {field.type === "textarea" ? (
                      <Textarea
                        value={values[task.id]?.[field.key] ?? ""}
                        placeholder={field.placeholder ?? "测试用示例"}
                        onChange={(e) =>
                          changeValues(task.id, field.key, e.target.value)
                        }
                      />
                    ) : (
                      <Input
                        type={field.type === "number" ? "number" : "text"}
                        value={values[task.id]?.[field.key] ?? ""}
                        placeholder={field.placeholder ?? "测试用示例"}
                        onChange={(e) =>
                          changeValues(
                            task.id,
                            field.key,
                            field.type === "number" && e.target.value !== ""
                              ? Number(e.target.value)
                              : e.target.value,
                          )
                        }
                      />
                    )}
                  </label>
                ))}
              </div>
            ))}
          </details>
          <div className="ta-debug-actions">
            <ActionButton busy={busy} onClick={() => run()}>
              <Play size={14} />
              {busy ? "正在分析…" : "运行整个节点"}
            </ActionButton>
            {stepId && (
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => run(true)}
              >
                <Square size={12} />
                运行到选中步骤
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              aria-label="清空测试资料"
              onClick={() => {
                setValues({});
                setResult(undefined);
                onTrace([]);
              }}
            >
              <RotateCcw size={14} />
            </Button>
          </div>
        </fieldset>
        <div className="ta-debug-output" aria-live="polite">
          {error && <ErrorNotice message={error} onRetry={() => run()} />}
          {result && !currentResult && (
            <div className="tea-note">
              配置或样例已修改，请重新试运行以查看最新结果。
            </div>
          )}
          {currentResult ? (
            <>
              <div className="ta-result-mode">
                <span className="ta-dot" />
                {currentResult.assistant_mode === "live"
                  ? "真实模型"
                  : "规则演示 · 未调用大模型"}
                <span>
                  {
                    currentResult.trace.filter(
                      (step) => step.status === "completed",
                    ).length
                  }{" "}
                  步已执行
                </span>
              </div>
              <div
                className="ta-result-tabs"
                role="tablist"
                aria-label="试运行结果"
              >
                {(
                  [
                    ["output", "分析结果"],
                    ["trace", "执行过程"],
                    ["checks", `检查项 ${currentResult.checks.length}`],
                    [
                      "citations",
                      `引用 ${currentResult.citations?.length ?? 0}`,
                    ],
                  ] as const
                ).map(([id, label]) => (
                  <button
                    role="tab"
                    aria-selected={tab === id}
                    key={id}
                    onClick={() => setTab(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div
                className="ta-result-body"
                role="tabpanel"
                aria-label="试运行详情"
              >
                {tab === "output" ? (
                  <p className="ta-output-text">{currentResult.output}</p>
                ) : tab === "checks" ? (
                  currentResult.checks.length ? (
                    <Checks checks={currentResult.checks} />
                  ) : (
                    <p className="ta-hint">本次未执行检查步骤。</p>
                  )
                ) : tab === "citations" ? (
                  currentResult.citations?.length ? (
                    currentResult.citations.map((citation, i) => (
                      <CitationCard
                        key={`${citation.document_id}-${i}`}
                        citation={citation}
                      />
                    ))
                  ) : (
                    <p className="ta-hint">
                      本次没有找到可引用的原文。可检查资料是否已审核、在适用期内，或调整检索关注点。
                    </p>
                  )
                ) : (
                  <div className="ta-execution-list">
                    {currentResult.trace.map((step, i) => (
                      <details key={step.id} open={step.status === "failed"}>
                        <summary>
                          <span className={`ta-execution-icon ${step.status}`}>
                            {step.status === "failed" ? (
                              <XCircle size={16} />
                            ) : step.status === "completed" ? (
                              <CheckCircle2 size={16} />
                            ) : (
                              <ChevronRight size={16} />
                            )}
                          </span>
                          <span>
                            <strong>{step.label}</strong>
                            <small>{step.detail}</small>
                          </span>
                          <em>{String(i + 1).padStart(2, "0")}</em>
                        </summary>
                        <div>
                          {step.input && (
                            <p>
                              <b>输入</b>
                              {step.input}
                            </p>
                          )}
                          {step.output && (
                            <p>
                              <b>输出</b>
                              {step.output}
                            </p>
                          )}
                          <p>{step.detail}</p>
                        </div>
                      </details>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : !busy && !error ? (
            <div className="ta-debug-empty">
              <FlaskConical size={30} strokeWidth={1.2} />
              <h4>先试一遍，再交给办理人。</h4>
              <p>选择节点、填写样例，查看分析结果、缺项提示和引用依据。</p>
              <div>
                <span>样例资料</span>
                <ChevronRight size={13} />
                <span>逐步分析</span>
                <ChevronRight size={13} />
                <span>结果与依据</span>
              </div>
            </div>
          ) : busy ? (
            <div className="ta-debug-empty">
              <FlaskConical size={30} />
              <h4>正在运行实际配置…</h4>
              <p>完成后会显示每一步的结果；这期间可以继续查看流程。</p>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
