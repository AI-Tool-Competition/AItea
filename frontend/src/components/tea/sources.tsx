"use client";
import { ChevronRight, Globe2, Plus, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  errorMessage,
  patch,
  post,
  safeSourceUrl,
  teaRequest,
} from "@/core/tea/api";
import { sourceSamples } from "@/core/tea/library-samples";
import {
  type CollectionRun,
  type ListResult,
  type Source,
} from "@/core/tea/types";

import { CollectionChecklists } from "./collection-checklists";
import {
  CollectionProgress,
  collectionActive,
  collectionStatus,
  currentProgress,
} from "./collection-run";
import {
  ActionButton,
  Busy,
  dateLabel,
  Empty,
  ErrorNotice,
  SectionHeading,
} from "./shared";
import { SourceDialog } from "./source-dialog";
import { SourceForm } from "./source-form";

const PAGE_SIZE = 20;
export function Sources(
  props: { compact?: boolean; samples?: boolean; ownerId?: string } = {},
) {
  if (props.samples)
    return (
      <CustomSources key={`samples:${props.ownerId ?? "session"}`} {...props} />
    );
  return <SourceWorkspace key={props.ownerId ?? "session"} {...props} />;
}
function SourceWorkspace({
  compact = false,
  ownerId,
}: {
  compact?: boolean;
  ownerId?: string;
}) {
  const [tab, setTab] = useState<"checklists" | "custom">("checklists");
  return (
    <div className="tea-page tea-sources">
      {!compact && <SectionHeading title="来源管理" />}
      <div className="ta-checklist-tabs" aria-label="来源类型">
        <Button
          variant={tab === "checklists" ? "secondary" : "ghost"}
          aria-pressed={tab === "checklists"}
          onClick={() => setTab("checklists")}
        >
          采集清单
        </Button>
        <Button
          variant={tab === "custom" ? "secondary" : "ghost"}
          aria-pressed={tab === "custom"}
          onClick={() => setTab("custom")}
        >
          自定义来源
        </Button>
      </div>
      {tab === "checklists" ? (
        <CollectionChecklists ownerId={ownerId} />
      ) : (
        <CustomSources compact ownerId={ownerId} />
      )}
    </div>
  );
}
export function CustomSources({
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
  const [selected, setSelected] = useState("");
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
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true);
    function load() {
      teaRequest<ListResult<Source>>(
        `/sources?limit=${PAGE_SIZE}&offset=${offset}`,
        { signal: abort.signal },
      )
        .then((result) => {
          if (abort.signal.aborted) return;
          setSources((current) =>
            result.items.map((next) => {
              const previous = current.find((item) => item.id === next.id);
              if (!previous) return next;
              if ((previous.revision ?? 1) > (next.revision ?? 1))
                return previous;
              const latest = previous.latest_run;
              if (
                latest &&
                (!next.latest_run ||
                  (latest.id === next.latest_run.id
                    ? !currentProgress(next.latest_run, latest)
                    : latest.created_at > next.latest_run.created_at))
              )
                return { ...next, latest_run: latest };
              return next;
            }),
          );
          setTotal(result.total);
          setError("");
          if (
            result.items.some(
              (source) =>
                collectionActive(source.latest_run) ||
                (source.latest_run?.processing ?? 0) > 0,
            )
          )
            timer = setTimeout(load, 3000);
        })
        .catch((e) => {
          if (!abort.signal.aborted) setError(errorMessage(e));
        })
        .finally(() => {
          if (!abort.signal.aborted) setLoading(false);
        });
    }
    load();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [revision, samples, offset, ownerId]);

  async function mutate(source: Source, collect: boolean) {
    if (pending.current.has(source.id) || samples || source.checklist) return;
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
        setSelected(source.id);
        setRevision((value) => value + 1);
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
    <Button onClick={() => setEditor("new")}>
      <Plus size={15} />
      添加来源
    </Button>
  );
  const selectedSource = sources.find((source) => source.id === selected);
  return (
    <div className="tea-page tea-sources tea-custom-sources">
      {compact ? (
        <div className="ta-source-toolbar">
          <span>
            {loading ? "读取中…" : error ? "来源" : `${total} 个来源`}
          </span>
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
      <SourceDialog
        title={editor === "new" ? "添加来源" : "配置来源"}
        open={!!editor && !samples}
        onClose={() => setEditor(undefined)}
      >
        {editor && (
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
      </SourceDialog>
      {loading ? (
        <Busy />
      ) : sources.length ? (
        <div className="tea-source-list">
          <div
            className="tea-source-table-head tea-custom-source-head"
            aria-hidden="true"
          >
            <span>来源</span>
            <span>最近采集</span>
            <span>状态</span>
            <span />
          </div>
          {sources.map((source) => (
            <article key={source.id}>
              <button
                className="tea-source-name"
                aria-label={`查看 ${source.name}`}
                aria-haspopup="dialog"
                onClick={() => setSelected(source.id)}
              >
                <strong>{source.name}</strong>
                <span>
                  {safeSourceUrl(source.url)
                    ? new URL(source.url).hostname
                    : source.url}
                </span>
              </button>
              <span className="tea-source-last-fetched">
                {source.last_fetched_at
                  ? dateLabel(source.last_fetched_at)
                  : "—"}
              </span>
              <span
                className={`tea-source-status${source.last_error || source.latest_run?.status === "partial" ? "has-error" : ""}`}
              >
                {source.latest_run
                  ? collectionStatus(source.latest_run)
                  : source.last_error
                    ? "需处理"
                    : source.enabled
                      ? "已启用"
                      : "已停用"}
              </span>
              <div className="tea-source-row-actions">
                {!samples && !source.checklist && (
                  <ActionButton
                    variant="ghost"
                    size="sm"
                    busy={busy.has(source.id)}
                    disabled={
                      busy.has(source.id) || collectionActive(source.latest_run)
                    }
                    onClick={() => void mutate(source, true)}
                  >
                    <RefreshCw size={13} />
                    立即抓取
                  </ActionButton>
                )}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`来源详情 ${source.name}`}
                  onClick={() => setSelected(source.id)}
                >
                  <ChevronRight size={16} />
                </Button>
              </div>
            </article>
          ))}
        </div>
      ) : !error ? (
        <Empty icon={<Globe2 size={30} />} title="暂无来源">
          添加官方页面或文件地址。
        </Empty>
      ) : null}
      <SourceDialog
        title={selectedSource?.name ?? "来源详情"}
        open={!!selectedSource}
        onClose={() => setSelected("")}
      >
        {selectedSource && (
          <div className="tea-source-detail">
            {safeSourceUrl(selectedSource.url) && (
              <a
                className="tea-source-address"
                href={safeSourceUrl(selectedSource.url)}
                target="_blank"
                rel="noreferrer"
              >
                {selectedSource.url}
              </a>
            )}
            <div className="tea-source-settings-line">
              <span>每 {selectedSource.interval_hours} 小时采集</span>
              <label className="tea-checkbox">
                <input
                  type="checkbox"
                  checked={selectedSource.enabled}
                  disabled={
                    busy.has(selectedSource.id) ||
                    samples ||
                    !!selectedSource.checklist
                  }
                  onChange={() => void mutate(selectedSource, false)}
                />
                启用
              </label>
              {!samples && !selectedSource.checklist && (
                <ActionButton
                  variant="outline"
                  size="sm"
                  busy={busy.has(selectedSource.id)}
                  disabled={
                    busy.has(selectedSource.id) ||
                    collectionActive(selectedSource.latest_run)
                  }
                  onClick={() => void mutate(selectedSource, true)}
                >
                  立即抓取
                </ActionButton>
              )}
              {!samples && !selectedSource.checklist && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setSelected("");
                    setEditor(selectedSource);
                  }}
                >
                  配置
                </Button>
              )}
            </div>
            {!samples && selectedSource.latest_run && (
              <CollectionProgress
                key={selectedSource.latest_run.id}
                initial={selectedSource.latest_run}
                onChange={(run) =>
                  setSources((all) =>
                    all.map((source) =>
                      source.id === selectedSource.id &&
                      source.latest_run?.id === run.id
                        ? { ...source, latest_run: run }
                        : source,
                    ),
                  )
                }
              />
            )}
            {!samples && (
              <SourceHistory
                sourceId={selectedSource.id}
                latestId={selectedSource.latest_run?.id}
              />
            )}
            {selectedSource.last_error && !selectedSource.latest_run && (
              <ErrorNotice message={selectedSource.last_error} />
            )}
          </div>
        )}
      </SourceDialog>
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
                {run.kind === "discovery"
                  ? `已读 ${run.pages ?? 0} 页`
                  : `已存 ${run.stored}`}
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
