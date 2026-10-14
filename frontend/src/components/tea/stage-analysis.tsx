"use client";

import { RefreshCw, Sparkles, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { processStepLabels } from "@/core/tea/analysis-process";
import { errorMessage, teaRequest } from "@/core/tea/api";
import { legalModules } from "@/core/tea/configuration";
import { subscribeNodeAnalysis } from "@/core/tea/node-analysis";
import type {
  CaseDetail,
  StageAnalysis,
  StageState,
  TraceStep,
} from "@/core/tea/types";

import { AnalysisProcess } from "./analysis-process";
import { LegalWorkbench } from "./legal-workbench";
import { ErrorNotice } from "./shared";

type Props = {
  accountId: string;
  record: CaseDetail;
  stage: StageState;
  onUpdate: (record: CaseDetail) => void;
};
export function StageAnalysisPanel(props: Props) {
  return (
    <NodeAnalysis
      key={JSON.stringify([props.accountId, props.record.id, props.stage.id])}
      {...props}
    />
  );
}
function NodeAnalysis({ accountId, record, stage, onUpdate }: Props) {
  const [analysis, setAnalysis] = useState<StageAnalysis>();
  const [loading, setLoading] = useState(false);
  const [steps, setSteps] = useState<TraceStep[]>([]);
  const [label, setLabel] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [refreshBusy, setRefreshBusy] = useState(false);
  const stop = useRef<() => void>(() => undefined);
  const alive = useRef(false);
  const onUpdateRef = useRef(onUpdate);
  onUpdateRef.current = onUpdate;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (stage.status === "locked") return;
    let current = true;
    setLoading(true);
    setError("");
    setSteps([]);
    setLabel("正在读取本节点的资料与分析记录…");
    const request = subscribeNodeAnalysis(
      accountId,
      record.id,
      stage.id,
      record.revision,
      attempt > 0,
      (event) => {
        if (!current) return;
        if (event.event === "status") {
          const stepNumber = /^QA-(0[1-6])(?:\s|$)/.exec(event.data.label)?.[1];
          setLabel(
            stepNumber
              ? `正在${processStepLabels[`qa${stepNumber}`]}…`
              : event.data.label,
          );
        } else if (event.event === "step")
          setSteps((prior) => [
            ...prior.filter((step) => step.id !== event.data.id),
            event.data,
          ]);
      },
    );
    stop.current = request.stop;
    void request.promise
      .then((result) => {
        if (current) {
          setAnalysis(result);
          setSteps(result.trace);
        }
      })
      .catch((e) => {
        if (current)
          setError(
            e instanceof Error && e.name === "AbortError"
              ? "分析已停止，已保存的内容仍然保留。可重试继续。"
              : errorMessage(e),
          );
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
      request.unsubscribe();
    };
  }, [accountId, record.id, record.revision, stage.id, stage.status, attempt]);
  async function retry() {
    if (refreshBusy || loading) return;
    setRefreshBusy(true);
    setError("");
    try {
      const latest = await teaRequest<CaseDetail>(
        `/cases/${encodeURIComponent(record.id)}`,
      );
      if (!alive.current) return;
      if (latest.revision !== record.revision) onUpdateRef.current(latest);
      setAttempt((value) => value + 1);
    } catch (e) {
      if (alive.current) setError(errorMessage(e));
    } finally {
      if (alive.current) setRefreshBusy(false);
    }
  }
  if (stage.status === "locked")
    return (
      <div className="tea-proactive-locked">
        <Sparkles size={14} />
        节点解锁后，将自动分析当前资料并给出办理建议。
      </div>
    );
  const processSteps = legalModules.map(
    (module) =>
      steps.find((step) => step.id === module.id) ?? {
        id: module.id,
        label: module.title,
        status: "pending" as const,
        detail: "",
      },
  );
  return (
    <section
      className="tea-proactive tea-node-analysis"
      aria-label="节点开始分析"
      aria-busy={loading}
    >
      <header>
        <span className="tea-node-mark" aria-hidden="true">
          <Sparkles size={16} />
        </span>
        <div className="tea-node-identity">
          <strong>{stage.name} · 节点分析</strong>
          <span>
            {analysis
              ? analysis.assistant_mode === "live"
                ? "模型辅助分析"
                : "演示模式 · 未调用大模型"
              : "根据当前业务资料分析"}
          </span>
        </div>
      </header>
      <AnalysisProcess
        steps={processSteps}
        running={loading}
        incomplete={!!error || analysis?.status === "failed"}
      />
      {loading && (
        <div className="tea-node-running">
          <p role="status">{label}</p>
          <Button size="sm" variant="ghost" onClick={() => stop.current()}>
            <Square size={12} />
            停止
          </Button>
        </div>
      )}
      {error && <ErrorNotice message={error} onRetry={() => void retry()} />}
      {analysis && (
        <>
          {analysis.status === "failed" && !error && (
            <ErrorNotice
              message="部分分析步骤未完成，已保存的编辑仍然保留。"
              onRetry={() => void retry()}
            />
          )}
          <LegalWorkbench
            accountId={accountId}
            record={record}
            stageId={stage.id}
            onUpdate={onUpdate}
            embedded
            analysisId={analysis.legal_analysis_id}
            analysisRevision={analysis.legal_revision}
            readOnly={loading || !!error}
          />
        </>
      )}
      <footer>
        <span>分析建议供办理与复核参考。</span>
        <Button
          variant="ghost"
          size="sm"
          disabled={loading || refreshBusy}
          onClick={() => void retry()}
        >
          <RefreshCw size={12} />
          {refreshBusy ? "读取中…" : "检查最新资料"}
        </Button>
      </footer>
    </section>
  );
}
