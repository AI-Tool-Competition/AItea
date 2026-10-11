"use client";

import { BookOpen, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { errorMessage, post, TeaApiError, teaRequest } from "@/core/tea/api";
import {
  type CaseDetail,
  type StageAnalysis,
  type StageState,
} from "@/core/tea/types";

import { CitationCard } from "./library";
import { Busy, ErrorNotice, Trace } from "./shared";

// Only in-flight requests are shared. A remount reads the persisted server
// result; an obsolete component never applies its response to the new stage.
const requests = new Map<string, Promise<StageAnalysis>>();
function analyze(
  caseId: string,
  stageId: string,
  revision: number,
  retry: boolean,
) {
  const key = `${caseId}:${stageId}:${revision}:${retry}`;
  const existing = requests.get(key);
  if (existing) return existing;
  const request = post<StageAnalysis>(
    `/cases/${encodeURIComponent(caseId)}/stages/${encodeURIComponent(stageId)}/analysis`,
    { expected_revision: revision, retry },
  ).finally(() => {
    if (requests.get(key) === request) requests.delete(key);
  });
  requests.set(key, request);
  return request;
}
export function StageAnalysisPanel({
  record,
  stage,
  onUpdate,
}: {
  record: CaseDetail;
  stage: StageState;
  onUpdate: (record: CaseDetail) => void;
}) {
  const [analysis, setAnalysis] = useState<StageAnalysis>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [refreshBusy, setRefreshBusy] = useState(false);
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
    if (stage.status === "locked") {
      setAnalysis(undefined);
      setLoading(false);
      return;
    }
    let current = true;
    setLoading(true);
    setError("");
    setAnalysis(undefined);
    analyze(record.id, stage.id, record.revision, attempt > 0)
      .then((result) => {
        if (current) setAnalysis(result);
      })
      .catch((e) => {
        if (current)
          setError(
            e instanceof TeaApiError && e.code === "analysis_in_progress"
              ? "这个节点正在另一处分析。稍后点击重试即可读取结果。"
              : errorMessage(e),
          );
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [record.id, record.revision, stage.id, stage.status, attempt]);
  async function retry() {
    if (refreshBusy) return;
    setRefreshBusy(true);
    setError("");
    try {
      const current = await teaRequest<CaseDetail>(
        `/cases/${encodeURIComponent(record.id)}`,
      );
      if (!alive.current) return;
      if (current.revision !== record.revision) onUpdateRef.current(current);
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
  return (
    <section className="tea-proactive" aria-label="节点开始分析">
      <header>
        <Sparkles size={16} />
        <strong>开始前，先理清这一步</strong>
        <span>
          {analysis?.assistant_mode === "live" ? "模型分析" : "规则分析"}
        </span>
      </header>
      {loading ? (
        <Busy label="正在分析本节点资料与待办事项…" />
      ) : error ? (
        <ErrorNotice message={error} onRetry={() => void retry()} />
      ) : analysis ? (
        <>
          <div className="tea-proactive-result">
            <MessageResponse>{analysis.output}</MessageResponse>
          </div>
          {analysis.status === "failed" && (
            <ErrorNotice
              message="部分分析步骤未完成，可以重试。"
              onRetry={() => void retry()}
            />
          )}
          <Trace steps={analysis.trace} />
          {analysis.citations.length > 0 && (
            <details className="tea-message-citations">
              <summary>
                <BookOpen size={13} />
                查看 {analysis.citations.length} 项依据
              </summary>
              {analysis.citations.map((citation, index) => (
                <CitationCard
                  citation={citation}
                  key={`${citation.document_id}:${index}`}
                />
              ))}
            </details>
          )}
          <footer>
            <span>
              {analysis.assistant_mode === "demo"
                ? "演示模式 · 未调用大模型"
                : "建议与依据已保留"}
            </span>
            <Button
              variant="ghost"
              size="sm"
              disabled={refreshBusy}
              onClick={() => void retry()}
            >
              <RefreshCw size={12} />
              {refreshBusy ? "读取中…" : "检查最新资料"}
            </Button>
          </footer>
        </>
      ) : null}
    </section>
  );
}
