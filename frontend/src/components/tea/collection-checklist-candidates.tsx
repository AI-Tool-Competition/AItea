"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  downloadUrl,
  errorMessage,
  patch,
  safeSourceUrl,
  TeaApiError,
  teaRequest,
} from "@/core/tea/api";
import { type ChecklistCandidate, type ListResult } from "@/core/tea/types";

import { ActionButton, Busy, ErrorNotice } from "./shared";

const PAGE_SIZE = 20;
const statusLabels = {
  pending: "待确认",
  queued: "采集中",
  stored: "已入库",
  excluded: "已排除",
};
export function ChecklistCandidates({
  basePath,
  refreshKey,
  selected,
  storedSelected,
  disabled,
  onSelection,
  excludeId,
  excludeNote,
  onExclusionDraft,
  onChanged,
}: {
  basePath: string;
  refreshKey: number;
  selected: string[];
  storedSelected: string[];
  disabled: boolean;
  onSelection: (selected: string[], storedIds?: string[]) => void;
  excludeId: string;
  excludeNote: string;
  onExclusionDraft: (id: string, note: string) => void;
  onChanged: () => void;
}) {
  const [status, setStatus] = useState<"pending" | "stored" | "excluded">(
    "pending",
  );
  const [page, setPage] = useState<ListResult<ChecklistCandidate>>({
    items: [],
    total: 0,
  });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const [retry, setRetry] = useState(0);
  const mutation = useRef<AbortController | null>(null);
  useEffect(() => () => mutation.current?.abort(), []);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<ChecklistCandidate>>(
      `${basePath}/candidates?status=${status}&limit=${PAGE_SIZE}&offset=${offset}`,
      { signal: abort.signal },
    )
      .then((next) => {
        if (abort.signal.aborted) return;
        const lastOffset =
          Math.max(0, Math.ceil(next.total / PAGE_SIZE) - 1) * PAGE_SIZE;
        if (offset > lastOffset) {
          setOffset(lastOffset);
          return;
        }
        setPage(next);
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [basePath, refreshKey, status, offset, retry]);
  async function changeCandidate(
    candidate: ChecklistCandidate,
    nextStatus: "pending" | "excluded",
  ) {
    if (mutation.current || disabled) return;
    const abort = new AbortController();
    mutation.current = abort;
    setBusy(candidate.id);
    setActionError("");
    try {
      await patch<ChecklistCandidate>(
        `/collection-candidates/${encodeURIComponent(candidate.id)}`,
        {
          expected_revision: candidate.revision,
          status: nextStatus,
          ...(nextStatus === "excluded" ? { note: excludeNote.trim() } : {}),
        },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      onSelection(selected.filter((id) => id !== candidate.id));
      onExclusionDraft("", "");
      onChanged();
    } catch (e) {
      if (abort.signal.aborted) return;
      setActionError(errorMessage(e));
      if (e instanceof TeaApiError && e.status === 409)
        setRetry((value) => value + 1);
    } finally {
      mutation.current = null;
      if (!abort.signal.aborted) setBusy("");
    }
  }
  const pendingPage = page.items.filter(
    (candidate) =>
      candidate.status === "pending" || candidate.status === "stored",
  );
  function select(ids: string[]) {
    const stored = new Set([
      ...storedSelected,
      ...page.items
        .filter((candidate) => candidate.status === "stored")
        .map((candidate) => candidate.id),
    ]);
    onSelection(
      ids,
      ids.filter((id) => stored.has(id)),
    );
  }
  const pageSelected =
    pendingPage.length > 0 &&
    pendingPage.every((candidate) => selected.includes(candidate.id));
  return (
    <section className="ta-checklist-candidates" aria-label="候选资料">
      <div className="ta-checklist-candidates-toolbar">
        <div className="ta-checklist-candidate-tabs" aria-label="候选状态">
          {(["pending", "stored", "excluded"] as const).map((value) => (
            <Button
              key={value}
              size="sm"
              variant={status === value ? "secondary" : "ghost"}
              aria-pressed={status === value}
              onClick={() => {
                setStatus(value);
                setOffset(0);
              }}
            >
              {statusLabels[value]}
            </Button>
          ))}
        </div>
        <Button
          variant="ghost"
          size="sm"
          disabled={loading}
          onClick={() => setRetry((value) => value + 1)}
        >
          刷新候选
        </Button>
      </div>
      {actionError && <ErrorNotice message={actionError} />}
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : loading ? (
        <Busy label="读取候选…" />
      ) : (
        <>
          {status !== "excluded" && pendingPage.length > 0 && (
            <div className="ta-checklist-selection">
              <label className="tea-checkbox">
                <input
                  type="checkbox"
                  checked={pageSelected}
                  disabled={disabled || !!busy}
                  onChange={() =>
                    select(
                      pageSelected
                        ? selected.filter(
                            (id) =>
                              !pendingPage.some(
                                (candidate) => candidate.id === id,
                              ),
                          )
                        : [
                            ...new Set([
                              ...selected,
                              ...pendingPage.map((candidate) => candidate.id),
                            ]),
                          ].slice(0, 200),
                    )
                  }
                />
                选择本页
              </label>
              <span>已选 {selected.length} / 200</span>
            </div>
          )}
          <ul className="ta-checklist-candidate-list">
            {page.items.map((candidate) => {
              const href = safeSourceUrl(candidate.url);
              const origin = safeSourceUrl(candidate.discovered_from);
              const title = candidate.title || candidate.url;
              return (
                <li key={candidate.id}>
                  <div className="ta-checklist-candidate-main">
                    {(candidate.status === "pending" ||
                      candidate.status === "stored") && (
                      <input
                        type="checkbox"
                        aria-label={`选择 ${title}`}
                        checked={selected.includes(candidate.id)}
                        disabled={
                          disabled ||
                          !!busy ||
                          (!selected.includes(candidate.id) &&
                            selected.length >= 200)
                        }
                        onChange={(event) =>
                          select(
                            event.target.checked
                              ? [...selected, candidate.id]
                              : selected.filter((id) => id !== candidate.id),
                          )
                        }
                      />
                    )}
                    <div className="ta-checklist-candidate-text">
                      {href ? (
                        <a href={href} target="_blank" rel="noreferrer">
                          {title}
                        </a>
                      ) : (
                        <strong>{title}</strong>
                      )}
                      <small>{candidate.url}</small>
                      <div className="ta-checklist-candidate-meta">
                        <span>{statusLabels[candidate.status]}</span>
                        {origin && (
                          <a href={origin} target="_blank" rel="noreferrer">
                            发现来源
                          </a>
                        )}
                        {candidate.note && <span>{candidate.note}</span>}
                      </div>
                    </div>
                    <div className="ta-checklist-candidate-actions">
                      {candidate.status === "pending" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={disabled || !!busy}
                          onClick={() =>
                            onExclusionDraft(candidate.id, candidate.note)
                          }
                        >
                          排除
                        </Button>
                      )}
                      {candidate.status === "excluded" && (
                        <ActionButton
                          size="sm"
                          variant="ghost"
                          disabled={disabled || !!busy}
                          busy={busy === candidate.id}
                          onClick={() =>
                            void changeCandidate(candidate, "pending")
                          }
                        >
                          恢复
                        </ActionButton>
                      )}
                      {candidate.document_id && (
                        <Button size="sm" variant="ghost" asChild>
                          <a href={downloadUrl(candidate.document_id)}>原件</a>
                        </Button>
                      )}
                    </div>
                  </div>
                  {excludeId === candidate.id &&
                    candidate.status === "pending" && (
                      <div className="ta-checklist-exclude">
                        <Input
                          aria-label="排除原因"
                          placeholder="排除原因（可选）"
                          value={excludeNote}
                          disabled={!!busy}
                          onChange={(event) =>
                            onExclusionDraft(candidate.id, event.target.value)
                          }
                        />
                        <ActionButton
                          size="sm"
                          variant="outline"
                          disabled={disabled || !!busy}
                          busy={busy === candidate.id}
                          onClick={() =>
                            void changeCandidate(candidate, "excluded")
                          }
                        >
                          确认排除
                        </ActionButton>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!!busy}
                          onClick={() => onExclusionDraft("", "")}
                        >
                          取消
                        </Button>
                      </div>
                    )}
                </li>
              );
            })}
          </ul>
          {!page.items.length && (
            <p className="ta-checklist-no-candidates">
              {status === "pending"
                ? "暂无待确认候选。可先运行发现，或查看已入库与已排除条目。"
                : status === "stored"
                  ? "尚无已入库资料。"
                  : "尚无排除记录。"}
            </p>
          )}
          {page.total > PAGE_SIZE && (
            <div className="ta-pagination">
              <span>
                {offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} /{" "}
                {page.total}
              </span>
              <div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!offset}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  上一页
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset + PAGE_SIZE >= page.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
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
