"use client";

import { History, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { errorMessage, post, teaRequest } from "@/core/tea/api";
import type { GuideRun, ListResult } from "@/core/tea/types";

import { AnalysisProcess } from "./analysis-process";
import { LegalResultView } from "./legal-result";
import { Busy, dateLabel, ErrorNotice } from "./shared";

export function GuideHistory({
  caseId,
  stageId,
  revision,
}: {
  caseId: string;
  stageId: string;
  revision: number;
}) {
  const [opened, setOpened] = useState(false);
  const [history, setHistory] = useState<ListResult<GuideRun>>({
    items: [],
    total: 0,
  });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(0);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!opened) return;
    const abort = new AbortController();
    let reading = false;
    const read = async () => {
      if (reading) return;
      reading = true;
      setLoading(true);
      try {
        const result = await teaRequest<ListResult<GuideRun>>(
          `/cases/${encodeURIComponent(caseId)}/stages/${encodeURIComponent(stageId)}/guides?limit=10&offset=${page * 10}`,
          { signal: abort.signal },
        );
        if (!abort.signal.aborted) {
          setHistory(result);
          setError("");
        }
      } catch (reason) {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      } finally {
        reading = false;
        if (!abort.signal.aborted) setLoading(false);
      }
    };
    void read();
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") void read();
    }, 5000);
    return () => {
      abort.abort();
      clearInterval(timer);
    };
  }, [caseId, stageId, revision, opened, page, attempt]);
  async function retry(id: string) {
    try {
      await post(
        `/cases/${encodeURIComponent(caseId)}/stages/${encodeURIComponent(stageId)}/guides/${encodeURIComponent(id)}/retry`,
        { client_request_id: crypto.randomUUID() },
      );
      setPage(0);
      setAttempt((value) => value + 1);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }
  return (
    <details
      className="tea-guide-history"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary>
        <History size={14} />
        <span>分析报告与更新</span>
        <small>每次分析独立保留</small>
      </summary>
      <div className="tea-guide-history-content">
        {error && (
          <ErrorNotice
            message={error}
            onRetry={() => setAttempt((value) => value + 1)}
          />
        )}
        {!history.items.length ? (
          loading ? (
            <Busy label="读取分析记录…" />
          ) : (
            <p className="tea-small-note">
              先在引导会话中介绍情况；准备好后开始分析，报告会保存在这里。
            </p>
          )
        ) : (
          history.items.map((run) => (
            <details className="tea-guide-record" key={run.id}>
              <summary>
                <span>{run.title}</span>
                <small>
                  {dateLabel(run.created_at)} ·{" "}
                  {run.status === "completed"
                    ? "已完成"
                    : run.status === "queued"
                      ? "等待更新"
                      : run.status === "running"
                        ? "分析中"
                        : "未完成"}
                </small>
              </summary>
              <AnalysisProcess
                steps={run.trace}
                running={run.status === "running"}
                incomplete={run.status === "failed"}
              />
              {run.result && (
                <>
                  <h4>分析结论</h4>
                  <LegalResultView result={run.result} compact />
                  <details>
                    <summary>完整报告与原文依据</summary>
                    <LegalResultView result={run.result} showTrace={false} />
                  </details>
                </>
              )}
              {run.error && <p role="status">{run.error}</p>}
              {run.status === "failed" && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void retry(run.id)}
                >
                  <RefreshCw size={13} />
                  重试分析
                </Button>
              )}
            </details>
          ))
        )}
        {history.total > 10 && (
          <nav aria-label="引导报告分页" className="tea-legal-pagination">
            <Button
              size="sm"
              variant="ghost"
              disabled={page === 0 || loading}
              onClick={() => setPage((value) => value - 1)}
            >
              较新记录
            </Button>
            <span>
              {page + 1} / {Math.ceil(history.total / 10)}
            </span>
            <Button
              size="sm"
              variant="ghost"
              disabled={(page + 1) * 10 >= history.total || loading}
              onClick={() => setPage((value) => value + 1)}
            >
              更早记录
            </Button>
          </nav>
        )}
      </div>
    </details>
  );
}
