"use client";

import { ChevronRight, ListChecks, MoreHorizontal, Search } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { errorMessage, post, TeaApiError, teaRequest } from "@/core/tea/api";
import { checklistPlanRequests } from "@/core/tea/collection-drafts";
import { countryLabel } from "@/core/tea/countries";
import {
  type CollectionBlueprintDetail,
  type CollectionBlueprintSummary,
  type CollectionRun,
  type ChecklistItemState,
  type ListResult,
} from "@/core/tea/types";

import {
  ChecklistItemDetail,
  RequirementList,
} from "./collection-checklist-item";
import { collectionActive } from "./collection-run";
import { ActionButton, Busy, Empty, ErrorNotice } from "./shared";
import { SourceDialog } from "./source-dialog";

const PAGE_SIZE = 20;

const coverageLabels: Record<ChecklistItemState["coverage"], string> = {
  not_started: "未开始",
  in_progress: "采集中",
  needs_review: "待核验",
  blocked: "需处理",
};

function requirementItems(value: string | string[] | undefined): string[] {
  if (Array.isArray(value)) return value;
  if (!value?.trim()) return [];
  return value
    .split(/[、，,；;]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function CollectionChecklists({ ownerId }: { ownerId?: string }) {
  const [plans, setPlans] = useState<CollectionBlueprintSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<CollectionBlueprintSummary>>("/collection-plans", {
      signal: abort.signal,
    })
      .then((result) => {
        if (abort.signal.aborted) return;
        setPlans(result.items);
        setSelected((current) =>
          result.items.some((plan) => plan.id === current)
            ? current
            : (result.items[0]?.id ?? ""),
        );
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [retry]);
  if (loading && !plans.length) return <Busy />;
  if (error)
    return (
      <ErrorNotice
        message={error}
        onRetry={() => setRetry((value) => value + 1)}
      />
    );
  if (!plans.length)
    return <Empty icon={<ListChecks size={28} />} title="暂无采集清单" />;
  return (
    <div className="ta-checklists">
      {selected && (
        <ChecklistPlan
          key={selected}
          id={selected}
          ownerId={ownerId}
          countryControl={
            <select
              aria-label="国家清单"
              value={selected}
              onChange={(event) => setSelected(event.target.value)}
            >
              {plans.map((plan) => (
                <option key={plan.id} value={plan.id}>
                  {countryLabel(plan.country)}
                </option>
              ))}
            </select>
          }
        />
      )}
    </div>
  );
}

function ChecklistPlan({
  id,
  ownerId,
  countryControl,
}: {
  id: string;
  ownerId?: string;
  countryControl: ReactNode;
}) {
  const [data, setData] = useState<CollectionBlueprintDetail>();
  const [open, setOpen] = useState("");
  const [query, setQuery] = useState("");
  const [chapter, setChapter] = useState("");
  const [offset, setOffset] = useState(0);
  const [requirementsOpen, setRequirementsOpen] = useState(false);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [configuring, setConfiguring] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [skipped, setSkipped] = useState<{ item_id: string; reason: string }[]>(
    [],
  );
  const [lastAction, setLastAction] = useState<"configure" | "discover">(
    "configure",
  );
  const [refresh, setRefresh] = useState(0);
  const mutation = useRef<AbortController | null>(null);
  useEffect(() => () => mutation.current?.abort(), []);
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    async function load() {
      try {
        const next = await teaRequest<CollectionBlueprintDetail>(
          `/collection-plans/${encodeURIComponent(id)}`,
          { signal: abort.signal },
        );
        if (abort.signal.aborted) return;
        setData(next);
        setError("");
        failures = 0;
        if (
          next.items.some(
            (item) =>
              collectionActive(item.latest_run) ||
              (item.latest_run?.processing ?? 0) > 0,
          )
        )
          timer = setTimeout(() => void load(), 3000);
      } catch (e) {
        if (abort.signal.aborted) return;
        setError(errorMessage(e));
        failures += 1;
        if (failures < 3)
          timer = setTimeout(() => void load(), 4000 * failures);
      }
    }
    void load();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [id, refresh]);
  async function configure() {
    if (!data || mutation.current) return;
    const abort = new AbortController();
    mutation.current = abort;
    setConfiguring(true);
    setLastAction("configure");
    setActionError("");
    setNotice("");
    try {
      const result = await post<{ created: number; existing: number }>(
        `/collection-plans/${encodeURIComponent(id)}/configure`,
        { expected_version: data.plan.version },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      setNotice(`已配置 ${result.created + result.existing} 项`);
      setRefresh((value) => value + 1);
    } catch (e) {
      if (!abort.signal.aborted) setActionError(errorMessage(e));
    } finally {
      if (!abort.signal.aborted) setConfiguring(false);
      mutation.current = null;
    }
  }
  async function discoverAll() {
    if (!data || mutation.current) return;
    const key = `${ownerId ?? "session"}:${id}`;
    const requestId = checklistPlanRequests.get(key) ?? crypto.randomUUID();
    checklistPlanRequests.set(key, requestId);
    const abort = new AbortController();
    mutation.current = abort;
    setDiscovering(true);
    setLastAction("discover");
    setActionError("");
    setNotice("");
    setSkipped([]);
    try {
      const result = await post<{
        runs: CollectionRun[];
        started: number;
        skipped: { item_id: string; reason: string }[];
      }>(
        `/collection-plans/${encodeURIComponent(id)}/discover`,
        { client_request_id: requestId },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      checklistPlanRequests.delete(key);
      setNotice(
        `已启动 ${result.started} 项，跳过 ${result.skipped.length} 项`,
      );
      setSkipped(result.skipped);
      setRefresh((value) => value + 1);
    } catch (e) {
      if (abort.signal.aborted) return;
      setActionError(errorMessage(e));
      if (e instanceof TeaApiError && e.status >= 400 && e.status < 500)
        checklistPlanRequests.delete(key);
    } finally {
      mutation.current = null;
      if (!abort.signal.aborted) setDiscovering(false);
    }
  }
  if (!data)
    return (
      <>
        <div className="tea-source-filters tea-source-loading">
          {countryControl}
        </div>
        {error ? (
          <ErrorNotice
            message={error}
            onRetry={() => setRefresh((value) => value + 1)}
          />
        ) : (
          <Busy />
        )}
      </>
    );
  const { plan, items: states } = data;
  const statesById = new Map(states.map((state) => [state.item_id, state]));
  const needle = query.trim().toLocaleLowerCase();
  const visible = plan.items.filter(
    (item) =>
      (!chapter || item.chapter_id === chapter) &&
      (!needle ||
        [item.id, item.title, ...item.tags]
          .join(" ")
          .toLocaleLowerCase()
          .includes(needle)),
  );
  const pageOffset = Math.min(
    offset,
    Math.max(0, Math.ceil(visible.length / PAGE_SIZE) - 1) * PAGE_SIZE,
  );
  const openedItem = plan.items.find((item) => item.id === open);
  const onRefresh = () => setRefresh((value) => value + 1);
  return (
    <section className="ta-checklist-plan" aria-label={plan.title}>
      <div className="tea-source-filters">
        <label className="tea-source-search">
          <Search size={16} aria-hidden="true" />
          <Input
            aria-label="搜索采集条目"
            placeholder="搜索条目"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setOffset(0);
            }}
          />
        </label>
        {countryControl}
        <select
          aria-label="筛选章节"
          value={chapter}
          onChange={(event) => {
            setChapter(event.target.value);
            setOffset(0);
          }}
        >
          <option value="">全部分类</option>
          {plan.chapters.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
        </select>
        <div className="tea-source-toolbar-actions">
          <ActionButton
            busy={discovering}
            disabled={configuring}
            onClick={() => void discoverAll()}
          >
            发现全部
          </ActionButton>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="清单操作"
                disabled={configuring || discovering}
              >
                <MoreHorizontal size={18} />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="tea-theme">
              <DropdownMenuItem onSelect={() => void configure()}>
                配置本清单
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setRequirementsOpen(true)}>
                清单说明
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onRefresh}>刷新列表</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {notice && (
        <p className="ta-checklist-notice" role="status">
          {notice}
        </p>
      )}
      {skipped.length > 0 && (
        <details className="tea-config-details ta-checklist-requirements">
          <summary>跳过项</summary>
          <ul>
            {skipped.map((item) => (
              <li key={item.item_id}>
                {item.item_id} · {item.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
      {actionError && (
        <ErrorNotice
          message={actionError}
          onRetry={() =>
            void (lastAction === "discover" ? discoverAll() : configure())
          }
        />
      )}
      {error && <ErrorNotice message={error} onRetry={onRefresh} />}
      <div
        className="tea-source-table-head ta-checklist-table-head"
        aria-hidden="true"
      >
        <span>采集条目</span>
        <span>分类</span>
        <span>已入库</span>
        <span>状态</span>
        <span />
      </div>
      <div className="ta-checklist-rows">
        {visible.slice(pageOffset, pageOffset + PAGE_SIZE).map((item) => {
          const state = statesById.get(item.id);
          return (
            <article className="ta-checklist-row" key={item.id}>
              <button
                type="button"
                className="ta-checklist-row-toggle"
                aria-haspopup="dialog"
                aria-label={`${item.id} ${item.title}`}
                onClick={() => setOpen(item.id)}
              >
                <span className="ta-checklist-item-title">
                  <span className="ta-checklist-item-code">{item.id}</span>
                  <strong>{item.title}</strong>
                </span>
                <span className="ta-checklist-item-category">
                  {
                    plan.chapters.find(
                      (chapter) => chapter.id === item.chapter_id,
                    )?.title
                  }
                </span>
                <span className="ta-checklist-row-count">
                  {state && state.counts.stored > 0 ? state.counts.stored : "—"}
                </span>
                <span
                  className={`ta-checklist-coverage ${state?.coverage ?? "not_started"}`}
                >
                  {state ? coverageLabels[state.coverage] : "未开始"}
                </span>
                <ChevronRight
                  className="tea-source-row-chevron"
                  size={15}
                  aria-hidden="true"
                />
              </button>
            </article>
          );
        })}
        {!visible.length && <Empty title="没有匹配的条目" />}
      </div>
      <div className="ta-pagination tea-source-pagination">
        <span>{visible.length} 项</span>
        {visible.length > PAGE_SIZE && (
          <div>
            <Button
              size="sm"
              variant="ghost"
              disabled={!pageOffset}
              onClick={() => setOffset(Math.max(0, pageOffset - PAGE_SIZE))}
            >
              上一页
            </Button>
            <span>
              {Math.floor(pageOffset / PAGE_SIZE) + 1} /{" "}
              {Math.ceil(visible.length / PAGE_SIZE)}
            </span>
            <Button
              size="sm"
              variant="ghost"
              disabled={pageOffset + PAGE_SIZE >= visible.length}
              onClick={() => setOffset(pageOffset + PAGE_SIZE)}
            >
              下一页
            </Button>
          </div>
        )}
      </div>
      <SourceDialog
        title={openedItem ? `${openedItem.id} ${openedItem.title}` : "采集条目"}
        open={!!openedItem}
        onClose={() => setOpen("")}
      >
        {openedItem && (
          <ChecklistItemDetail
            key={openedItem.id}
            planId={id}
            collection={`country:${plan.country}`}
            item={openedItem}
            state={statesById.get(openedItem.id)}
            ownerId={ownerId}
            onRefresh={onRefresh}
          />
        )}
      </SourceDialog>
      <SourceDialog
        title="清单说明"
        open={requirementsOpen}
        onClose={() => setRequirementsOpen(false)}
      >
        <div className="ta-checklist-requirements">
          <p>{plan.description}</p>
          <RequirementList title="通用要求" items={plan.principles} />
          {plan.chapters.map((chapter) => (
            <RequirementList
              key={chapter.id}
              title={chapter.title}
              items={chapter.notes}
            />
          ))}
          {plan.channels.map((channel, index) => {
            const title = channel.title ?? channel.name ?? `渠道 ${index + 1}`;
            return (
              <section key={channel.id ?? channel.name ?? `${title}-${index}`}>
                <RequirementList
                  title={title}
                  items={requirementItems(channel.questions)}
                />
                {channel.sources && (
                  <RequirementList
                    title="资料来源"
                    items={requirementItems(channel.sources)}
                  />
                )}
                {!!channel.item_ids?.length && (
                  <div className="ta-checklist-channel-items">
                    {channel.item_ids.map((code) => (
                      <Button
                        key={code}
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setRequirementsOpen(false);
                          setOpen(code);
                        }}
                      >
                        {code}
                      </Button>
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      </SourceDialog>
    </section>
  );
}
