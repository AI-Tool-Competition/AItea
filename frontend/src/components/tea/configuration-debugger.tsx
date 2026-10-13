"use client";

import { FlaskConical, Play, Square } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { analysisSteps } from "@/core/tea/configuration";
import type { Template } from "@/core/tea/types";
import type {
  DebugRunMode,
  WorkflowDebugger,
} from "@/core/tea/use-workflow-debugger";

import { ConfirmAction } from "./admin-shared";
import {
  DebugRunComparison,
  DebugRunDetails,
  evaluationLabel,
  runLabel,
} from "./debug-run-details";
import { DebugSampleEditor } from "./debug-sample-editor";
import { ErrorNotice } from "./shared";

export function ConfigurationDebugger({
  debug,
  draft,
  stageId,
  stepId,
  onStep,
  onEdit,
}: {
  debug: WorkflowDebugger;
  draft: Template;
  stageId: string;
  stepId?: string;
  onStep: (id: string) => void;
  onEdit: (id: string) => void;
}) {
  const [tab, setTab] = useState<"sample" | "result" | "compare" | "history">(
    "sample",
  );
  const [choice, setChoice] = useState("");
  const stage = draft.stages.find((item) => item.id === stageId)!;
  const agent = stage.analysis ?? draft.agent;
  const steps = analysisSteps(agent);
  const legalCountries = agent.legal?.enabled
    ? [
        ...new Set([
          draft.destination,
          draft.origin,
          ...agent.legal.jurisdictions,
        ]),
      ]
    : undefined;
  const selectedTrace = stepId
    ? debug.trace.find((item) => item.id === stepId)
    : (debug.trace.find((item) => item.status === "failed") ??
      debug.trace.at(-1));
  const detailStepId = stepId ?? selectedTrace?.id ?? steps[0]?.id;
  const currentStep = steps.find((item) => item.id === detailStepId);
  const runnableStepId = currentStep?.id;
  const editableStepId =
    tab === "result"
      ? runnableStepId
      : steps.find((item) => item.id === (stepId ?? steps[0]?.id))?.id;
  const locked = debug.running || debug.working;
  useEffect(() => {
    if (stepId && debug.trace.length) setTab("result");
  }, [stepId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setChoice(debug.sampleId ?? "");
  }, [debug.sampleId]);
  function run(mode: DebugRunMode) {
    if (mode !== "full" && !runnableStepId) return;
    setTab("result");
    void debug.run(mode, mode === "full" ? undefined : runnableStepId);
  }
  return (
    <div className="ta-debug-workspace">
      <div className="ta-debug-title">
        <strong>试运行</strong>
        <Button
          variant="ghost"
          size="sm"
          disabled={!editableStepId}
          onClick={() => editableStepId && onEdit(editableStepId)}
        >
          编辑步骤
        </Button>
      </div>
      <div className="ta-debug-controls">
        <select
          aria-label="选择测试样例"
          value={choice}
          disabled={locked || debug.loading}
          onChange={(event) => setChoice(event.target.value)}
        >
          <option value="">临时样例</option>
          {debug.samples.map((item) => (
            <option value={item.id} key={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        {choice !== (debug.sampleId ?? "") &&
          (debug.sampleDirty ? (
            <ConfirmAction
              title="载入另一份样例？"
              description="当前未保存的测试输入会被替换。"
              disabled={locked}
              onConfirm={() =>
                choice ? void debug.loadSample(choice) : debug.newSample()
              }
            >
              载入
            </ConfirmAction>
          ) : (
            <Button
              size="sm"
              variant="outline"
              disabled={locked}
              onClick={() =>
                choice ? void debug.loadSample(choice) : debug.newSample()
              }
            >
              载入
            </Button>
          ))}
        {debug.running ? (
          <Button size="sm" variant="outline" onClick={debug.stop}>
            <Square size={13} />
            停止
          </Button>
        ) : (
          <Button
            size="sm"
            onClick={() => run("full")}
            disabled={locked || debug.loading}
          >
            <Play size={13} />
            试运行
          </Button>
        )}
      </div>
      <div className="ta-debug-tabs" role="tablist" aria-label="调试内容">
        {(
          [
            ["sample", "样例"],
            ["result", "结果"],
            ["compare", "对比"],
            ["history", "记录"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            aria-controls={`debug-panel-${id}`}
            id={`debug-tab-${id}`}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div
        className="ta-inspector-body ta-debug-body"
        role="tabpanel"
        id={`debug-panel-${tab}`}
        aria-labelledby={`debug-tab-${tab}`}
      >
        {debug.error && (
          <ErrorNotice
            message={debug.error}
            onRetry={() => void debug.refresh()}
          />
        )}
        {debug.notice && (
          <p className="ta-debug-notice" role="status">
            {debug.notice}
          </p>
        )}
        {debug.running && (
          <p className="ta-debug-progress" role="status">
            <span className="ta-dot" />
            {debug.progress || "正在运行…"}
          </p>
        )}
        {tab === "sample" && (
          <DebugSampleEditor
            key={JSON.stringify(legalCountries ?? draft.destination)}
            debug={debug}
            stage={stage}
            destination={draft.destination}
            countries={legalCountries}
          />
        )}
        {tab === "result" && (
          <>
            {runnableStepId && (
              <div className="ta-debug-step-actions">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={locked}
                  onClick={() => run("to")}
                >
                  运行到这里
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={locked || !debug.result}
                  onClick={() => run("step")}
                >
                  只试这一步
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={locked || !debug.result}
                  onClick={() => run("continue")}
                >
                  从这里继续
                </Button>
              </div>
            )}
            {selectedTrace && !runnableStepId && (
              <p className="ta-hint">
                此步骤已不在当前配置中，历史输入与结果仍可查看。
              </p>
            )}
            {debug.stale && !debug.running && (
              <p className="ta-debug-notice">
                配置或输入已有变化，下面保留的是上次结果。重试时会检查哪些步骤可以复用。
              </p>
            )}
            {debug.result || debug.trace.length || debug.running ? (
              <DebugRunDetails
                result={debug.result}
                trace={debug.trace}
                selectedId={detailStepId}
                selectedLabel={currentStep?.label}
                onStep={onStep}
                liveOutput={debug.liveOutput}
                running={debug.running}
              />
            ) : (
              <div className="ta-debug-placeholder">
                <FlaskConical size={26} strokeWidth={1.4} />
                <p>选一份样例，开始试运行。</p>
              </div>
            )}
          </>
        )}
        {tab === "compare" && (
          <>
            <label className="tea-field">
              <span>对比记录</span>
              <select
                aria-label="对比记录"
                disabled={locked}
                value={debug.comparison?.id ?? ""}
                onChange={(event) => {
                  if (event.target.value)
                    void debug.loadRun(event.target.value, true);
                }}
              >
                <option value="">选择一条历史记录</option>
                {debug.comparison &&
                  !debug.history.some(
                    (item) => item.id === debug.comparison?.id,
                  ) && <option value={debug.comparison.id}>上次结果</option>}
                {debug.history
                  .filter((item) => item.id !== debug.result?.id)
                  .map((item) => (
                    <option value={item.id} key={item.id}>
                      {formatTime(item.created_at)} · {runLabel(item.status)}
                    </option>
                  ))}
              </select>
            </label>
            <DebugRunComparison
              current={debug.result}
              previous={debug.comparison}
            />
          </>
        )}
        {tab === "history" && (
          <>
            <div className="ta-debug-section-heading">
              <strong>当前节点的样例</strong>
              <Button
                size="sm"
                variant="outline"
                disabled={locked || !debug.sampleTotal}
                onClick={() => void debug.runBatch()}
              >
                批量测试
              </Button>
            </div>
            {debug.batchTotal > 0 && (
              <div className="ta-debug-batch">
                <p role="status">
                  已完成 {debug.batch.length} / {debug.batchTotal}
                </p>
                {debug.batch.map((item) => (
                  <div key={item.id}>
                    <strong>{item.name}</strong>
                    <span>
                      {item.error ??
                        (item.result
                          ? `${runLabel(item.result.status)} · ${evaluationLabel(item.result.evaluations)}`
                          : "等待运行")}
                    </span>
                    {item.result && (
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={locked}
                        onClick={() => {
                          void debug.loadRun(item.result!.id);
                          setTab("result");
                        }}
                      >
                        查看
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
            <p className="ta-hint">
              {debug.sampleTotal} 个已保存样例 · {debug.historyTotal} 次运行
            </p>
            {debug.sampleTotal > debug.samples.length && (
              <Button
                variant="ghost"
                size="sm"
                disabled={locked}
                onClick={() => void debug.refresh("samples")}
              >
                载入更多样例
              </Button>
            )}
            <div className="ta-debug-history">
              {debug.history.map((item) => (
                <div key={item.id}>
                  <div>
                    <strong>{formatTime(item.created_at)}</strong>
                    <span>
                      {runLabel(item.status)} ·{" "}
                      {evaluationLabel(item.evaluation_status)}
                    </span>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={locked}
                    onClick={() => {
                      void debug.loadRun(item.id);
                      setTab("result");
                    }}
                  >
                    查看
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={
                      locked || !debug.result || item.id === debug.result.id
                    }
                    onClick={() => {
                      void debug.loadRun(item.id, true);
                      setTab("compare");
                    }}
                  >
                    对比
                  </Button>
                </div>
              ))}
            </div>
            {!debug.history.length && !debug.loading && (
              <p className="ta-hint">暂无运行记录。</p>
            )}
            {debug.historyTotal > debug.history.length && (
              <Button
                size="sm"
                variant="ghost"
                disabled={locked}
                onClick={() => void debug.refresh("history")}
              >
                更多记录
              </Button>
            )}
            {debug.result && (
              <ConfirmAction
                title="载入这次运行的测试输入？"
                description="当前测试输入会被替换，使用正在编辑的配置重新测试。"
                disabled={locked}
                onConfirm={() => {
                  debug.restoreInputs();
                  setTab("sample");
                }}
              >
                使用这次运行的输入
              </ConfirmAction>
            )}
          </>
        )}
      </div>
    </div>
  );
}
function formatTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
