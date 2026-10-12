"use client";
import { Clock3, Globe2, Plus, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { errorMessage, patch, post, teaRequest } from "@/core/tea/api";
import { sourceSamples } from "@/core/tea/library-samples";
import {
  type CollectionRun,
  type ListResult,
  type Source,
} from "@/core/tea/types";

import {
  CollectionProgress,
  collectionActive,
  collectionStatus,
} from "./collection-run";
import {
  ActionButton,
  Busy,
  dateLabel,
  Empty,
  ErrorNotice,
  SectionHeading,
} from "./shared";
import { SourceForm } from "./source-form";

const PAGE_SIZE = 20;
export function Sources({
  compact = false,
  samples = false,
  ownerId,
}: { compact?: boolean; samples?: boolean; ownerId?: string } = {}) {
  const [sources, setSources] = useState<Source[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [editor, setEditor] = useState<"new" | Source>();
  const [revision, setRevision] = useState(0);
  const pending = useRef(new Map<string, AbortController>());
  const requestIds = useRef(new Map<string, string>());
  useEffect(() => {
    const requests = pending.current;
    return () => {
      requests.forEach((request) => request.abort());
      requests.clear();
    };
  }, []);
  useEffect(() => {
    if (samples) {
      setSources(sourceSamples);
      setTotal(sourceSamples.length);
      setLoading(false);
      return;
    }
    const abort = new AbortController();
    setLoading(true);
    teaRequest<ListResult<Source>>(
      `/sources?limit=${PAGE_SIZE}&offset=${offset}`,
      { signal: abort.signal },
    )
      .then((result) => {
        if (abort.signal.aborted) return;
        setSources(result.items);
        setTotal(result.total);
        setError("");
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [revision, samples, offset, ownerId]);

  async function mutate(source: Source, collect: boolean) {
    if (pending.current.has(source.id) || samples) return;
    const abort = new AbortController();
    pending.current.set(source.id, abort);
    setBusy((current) => new Set(current).add(source.id));
    setError("");
    try {
      if (collect) {
        const requestId =
          requestIds.current.get(source.id) ?? crypto.randomUUID();
        requestIds.current.set(source.id, requestId);
        const run = await post<CollectionRun>(
          `/sources/${encodeURIComponent(source.id)}/runs`,
          { client_request_id: requestId },
          abort.signal,
        );
        if (abort.signal.aborted) return;
        requestIds.current.delete(source.id);
        setSources((all) =>
          all.map((item) =>
            item.id === source.id ? { ...item, latest_run: run } : item,
          ),
        );
      } else {
        const next = await patch<Source>(
          `/sources/${encodeURIComponent(source.id)}`,
          { enabled: !source.enabled, expected_revision: source.revision ?? 1 },
          abort.signal,
        );
        if (!abort.signal.aborted)
          setSources((all) =>
            all.map((item) => (item.id === next.id ? next : item)),
          );
      }
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      pending.current.delete(source.id);
      if (!abort.signal.aborted)
        setBusy((current) => {
          const next = new Set(current);
          next.delete(source.id);
          return next;
        });
    }
  }
  const addButton = !samples && (
    <Button onClick={() => setEditor(editor === "new" ? undefined : "new")}>
      <Plus size={15} />
      {editor === "new" ? "收起添加" : "添加来源"}
    </Button>
  );
  return (
    <div className="tea-page tea-sources">
      {compact ? (
        <div className="ta-source-toolbar">
          <span>{loading ? "读取中…" : `${total} 个来源`}</span>
          {addButton}
        </div>
      ) : (
        <SectionHeading title="来源管理">{addButton}</SectionHeading>
      )}
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((value) => value + 1)}
        />
      )}
      {editor && !samples && (
        <SourceForm
          key={editor === "new" ? "new" : editor.id}
          ownerId={ownerId}
          source={editor === "new" ? undefined : editor}
          onClose={() => setEditor(undefined)}
          onSaved={() => {
            setEditor(undefined);
            setRevision((value) => value + 1);
          }}
        />
      )}
      {loading ? (
        <Busy />
      ) : sources.length ? (
        <div className="tea-source-list">
          {sources.map((source) => (
            <article key={source.id}>
              <div className="tea-source-top">
                <span className="tea-source-icon">
                  <Globe2 size={22} />
                </span>
                <div>
                  <h3>{source.name}</h3>
                  <p>{source.url}</p>
                </div>
                <label className="tea-checkbox">
                  <input
                    type="checkbox"
                    checked={source.enabled}
                    disabled={busy.has(source.id) || samples}
                    onChange={() => void mutate(source, false)}
                  />
                  启用
                </label>
              </div>
              <div className="tea-source-meta">
                <span>
                  <Clock3 size={13} />每 {source.interval_hours} 小时检查
                </span>
                <span>
                  {samples
                    ? "未抓取"
                    : source.last_fetched_at
                      ? `上次 ${dateLabel(source.last_fetched_at)}`
                      : "尚未抓取"}
                </span>
                {!samples && (
                  <>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setEditor(source)}
                    >
                      配置
                    </Button>
                    <ActionButton
                      variant="outline"
                      size="sm"
                      busy={busy.has(source.id)}
                      disabled={
                        busy.has(source.id) ||
                        collectionActive(source.latest_run)
                      }
                      onClick={() => void mutate(source, true)}
                    >
                      <RefreshCw size={13} />
                      立即抓取
                    </ActionButton>
                  </>
                )}
              </div>
              <div className="ta-collection-tags">
                {source.plan && (
                  <span>
                    {source.plan.collection === "default"
                      ? "默认采集组"
                      : source.plan.collection}{" "}
                    ·{" "}
                    {source.plan.level === "secondary"
                      ? "二级法规"
                      : "一级资料"}
                  </span>
                )}
                {source.tags.map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </div>
              {!samples && source.latest_run && (
                <CollectionProgress
                  key={source.latest_run.id}
                  initial={source.latest_run}
                  onChange={(run) =>
                    setSources((all) =>
                      all.map((item) =>
                        item.id === source.id && item.latest_run?.id === run.id
                          ? { ...item, latest_run: run }
                          : item,
                      ),
                    )
                  }
                />
              )}
              {!samples && (
                <SourceHistory
                  sourceId={source.id}
                  latestId={source.latest_run?.id}
                />
              )}
              {source.last_error && !source.latest_run && (
                <ErrorNotice message={source.last_error} />
              )}
            </article>
          ))}
        </div>
      ) : (
        <Empty icon={<Globe2 size={30} />} title="暂无来源">
          添加官方页面或文件地址。
        </Empty>
      )}
      {total > PAGE_SIZE && (
        <div className="ta-pagination">
          <span>
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} / {total}
          </span>
          <div>
            <Button
              size="sm"
              variant="outline"
              disabled={loading || !offset}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              上一页
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={loading || offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function SourceHistory({
  sourceId,
  latestId,
}: {
  sourceId: string;
  latestId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ListResult<CollectionRun>>({
    items: [],
    total: 0,
  });
  const [selected, setSelected] = useState<CollectionRun>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<CollectionRun>>(
      `/sources/${encodeURIComponent(sourceId)}/runs?limit=20&offset=${offset}`,
      { signal: abort.signal },
    )
      .then((next) => {
        if (!abort.signal.aborted) setData(next);
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [open, sourceId, offset, latestId, retry]);
  return (
    <details
      className="tea-config-details ta-run-history"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>抓取记录</summary>
      {loading ? (
        <Busy />
      ) : error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : (
        <>
          <div className="ta-run-history-list">
            {data.items.map((run) => (
              <Button
                key={run.id}
                variant="ghost"
                onClick={() =>
                  setSelected(selected?.id === run.id ? undefined : run)
                }
              >
                {dateLabel(run.created_at, true)} · {collectionStatus(run)} ·
                已存 {run.stored}
              </Button>
            ))}
            {!data.items.length && <p className="ta-hint">暂无记录。</p>}
          </div>
          {data.total > 20 && (
            <div className="ta-pagination">
              <span>
                {offset + 1}–{Math.min(offset + 20, data.total)} / {data.total}
              </span>
              <div>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!offset}
                  onClick={() => setOffset(Math.max(0, offset - 20))}
                >
                  上一页
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={offset + 20 >= data.total}
                  onClick={() => setOffset(offset + 20)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
          {selected && open && (
            <CollectionProgress key={selected.id} initial={selected} />
          )}
        </>
      )}
    </details>
  );
}
