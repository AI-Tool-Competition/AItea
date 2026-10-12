"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  downloadUrl,
  errorMessage,
  post,
  TeaApiError,
  teaRequest,
} from "@/core/tea/api";
import { type CollectionRun, type CollectionRunDetail } from "@/core/tea/types";

import { ActionButton, ErrorNotice } from "./shared";

export function collectionActive(run?: CollectionRun | null) {
  return run?.status === "queued" || run?.status === "running";
}
export function collectionStatus(run: CollectionRun) {
  if (run.status === "running" && !run.pending && run.processing > 0)
    return "解析中";
  return {
    queued: "已排队",
    running: "抓取中",
    completed: "已完成",
    partial: "部分失败",
    cancelled: "已取消",
  }[run.status];
}

function currentProgress(next: CollectionRun, current: CollectionRun) {
  // The API emits UTC ISO timestamps; compare without losing sub-millisecond precision.
  return (
    next.revision > current.revision ||
    (next.revision === current.revision &&
      next.updated_at >= current.updated_at)
  );
}

export function CollectionProgress({
  initial,
  onChange,
}: {
  initial: CollectionRun;
  onChange?: (run: CollectionRun) => void;
}) {
  const [run, setRun] = useState(initial);
  const [detail, setDetail] = useState<CollectionRunDetail>();
  const [expanded, setExpanded] = useState(false);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const change = useRef(onChange);
  change.current = onChange;
  const latestRun = useRef(initial);
  const mutation = useRef<AbortController | null>(null);
  useEffect(() => () => mutation.current?.abort(), []);
  useEffect(() => {
    if (currentProgress(initial, latestRun.current)) {
      latestRun.current = initial;
      setRun(initial);
    }
  }, [initial]);
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const accept = (next: CollectionRun) => {
      if (abort.signal.aborted || !currentProgress(next, latestRun.current))
        return false;
      latestRun.current = next;
      setRun(next);
      change.current?.(next);
      return true;
    };
    async function poll() {
      try {
        const next = await teaRequest<CollectionRunDetail>(
          `/collection-runs/${encodeURIComponent(initial.id)}?limit=${expanded ? 20 : 1}&offset=${expanded ? offset : 0}`,
          { signal: abort.signal },
        );
        if (abort.signal.aborted) return;
        if (accept(next.run)) setDetail(next);
        failures = 0;
        setError("");
        if (
          collectionActive(latestRun.current) ||
          latestRun.current.processing > 0
        )
          timer = setTimeout(() => void poll(), 2000);
      } catch (e) {
        if (abort.signal.aborted) return;
        setError(errorMessage(e));
        failures += 1;
        if (
          failures < 5 &&
          !(e instanceof TeaApiError && [401, 403, 404].includes(e.status))
        )
          timer = setTimeout(
            () => void poll(),
            Math.min(30_000, 2000 * 2 ** failures),
          );
      }
    }
    void poll();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [initial.id, expanded, offset, refresh]);

  async function act(action: "cancel" | "retry") {
    if (mutation.current) return;
    const abort = new AbortController();
    mutation.current = abort;
    setBusy(action);
    setError("");
    setActionError("");
    try {
      const next = await post<CollectionRun>(
        `/collection-runs/${encodeURIComponent(run.id)}/${action}`,
        { expected_revision: run.revision },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      if (currentProgress(next, latestRun.current)) {
        latestRun.current = next;
        setRun(next);
        change.current?.(next);
      }
      setRefresh((value) => value + 1);
    } catch (e) {
      if (!abort.signal.aborted) {
        setActionError(errorMessage(e));
        // A conflict refreshes the server state; never repeat the mutation automatically.
        if (e instanceof TeaApiError && e.status === 409)
          setRefresh((value) => value + 1);
      }
    } finally {
      if (!abort.signal.aborted) setBusy("");
      mutation.current = null;
    }
  }

  return (
    <section className="ta-collection-progress" aria-label="采集进度">
      <div className="ta-collection-counts" role="status">
        <strong>{collectionStatus(run)}</strong>
        <span>
          已存 {run.stored} / {run.total}
        </span>
        <span>已处理 {run.processed}</span>
        {run.pending > 0 && <span>待抓 {run.pending}</span>}
        {run.failed > 0 && (
          <span className="ta-collection-failed">抓取失败 {run.failed}</span>
        )}
        {run.processing_failed > 0 && (
          <span className="ta-collection-failed">
            解析失败 {run.processing_failed}
          </span>
        )}
      </div>
      <progress
        className="ta-collection-bar"
        aria-label="已存文件进度"
        max={Math.max(1, run.total)}
        value={run.stored}
      />
      <div className="ta-collection-actions">
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起明细" : "查看明细"}
        </Button>
        {collectionActive(run) && (
          <ActionButton
            size="sm"
            variant="outline"
            disabled={!!busy}
            busy={busy === "cancel"}
            onClick={() => void act("cancel")}
          >
            取消任务
          </ActionButton>
        )}
        {!collectionActive(run) &&
          (run.failed > 0 || run.processing_failed > 0) && (
            <ActionButton
              size="sm"
              variant="outline"
              disabled={!!busy}
              busy={busy === "retry"}
              onClick={() => void act("retry")}
            >
              重试失败项
            </ActionButton>
          )}
      </div>
      {(actionError || error || run.error) && (
        <ErrorNotice
          message={actionError || error || run.error!}
          onRetry={() => {
            setActionError("");
            setRefresh((value) => value + 1);
          }}
        />
      )}
      {expanded && detail && (
        <>
          <ul className="ta-collection-items">
            {detail.items.map((item) => (
              <li key={item.id}>
                <div>
                  <span>{item.url}</span>
                  <small>
                    {
                      (
                        {
                          queued: "待抓取",
                          fetching: "抓取中",
                          stored: "已存原件",
                          failed: "失败",
                          cancelled: "已取消",
                        } as const
                      )[item.status]
                    }{" "}
                    ·{" "}
                    {item.level === "attachment"
                      ? "附件"
                      : item.level === "secondary"
                        ? "二级法规"
                        : "一级资料"}
                    {item.attempts > 1 ? ` · 尝试 ${item.attempts} 次` : ""}
                    {item.status === "stored" &&
                      item.processing_status &&
                      ` · ${item.processing_status === "completed" ? "已解析" : item.processing_status === "failed" ? "解析失败" : item.processing_status === "queued" ? "待解析" : "解析中"}`}
                  </small>
                  {(item.error ?? item.processing_error) && (
                    <p className="ta-collection-failed">
                      {item.error ?? item.processing_error}
                    </p>
                  )}
                </div>
                {item.document_id && (
                  <Button asChild variant="ghost" size="sm">
                    <a href={downloadUrl(item.document_id)}>原件</a>
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {detail.total > 20 && (
            <div className="ta-pagination">
              <span>
                {offset + 1}–{Math.min(offset + 20, detail.total)} /{" "}
                {detail.total}
              </span>
              <div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!offset}
                  onClick={() => setOffset(Math.max(0, offset - 20))}
                >
                  上一页
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset + 20 >= detail.total}
                  onClick={() => setOffset(offset + 20)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
