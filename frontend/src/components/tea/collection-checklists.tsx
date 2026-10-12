"use client";

import { ChevronDown, ChevronRight, ListChecks } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
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

const coverageLabels: Record<ChecklistItemState["coverage"], string> = {
  not_started: "未开始",
  in_progress: "采集中",
  needs_review: "待核验",
  blocked: "需处理",
};

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
      <div className="ta-checklist-countries" aria-label="国家清单">
        {plans.map((plan) => (
          <Button
            key={plan.id}
            variant={selected === plan.id ? "secondary" : "ghost"}
            aria-pressed={selected === plan.id}
            onClick={() => setSelected(plan.id)}
          >
            {countryLabel(plan.country)}
            <span>{plan.item_count} 项</span>
          </Button>
        ))}
      </div>
      {selected && (
        <ChecklistPlan key={selected} id={selected} ownerId={ownerId} />
      )}
    </div>
  );
}

function ChecklistPlan({ id, ownerId }: { id: string; ownerId?: string }) {
  const [data, setData] = useState<CollectionBlueprintDetail>();
  const [open, setOpen] = useState("");
  const [query, setQuery] = useState("");
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
      await post<{ created: number; existing: number }>(
        `/collection-plans/${encodeURIComponent(id)}/configure`,
        { expected_version: data.plan.version },
        abort.signal,
      );
      if (abort.signal.aborted) return;
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
    return error ? (
      <ErrorNotice
        message={error}
        onRetry={() => setRefresh((value) => value + 1)}
      />
    ) : (
      <Busy />
    );
  const { plan, items: states } = data;
  const statesById = new Map(states.map((state) => [state.item_id, state]));
  const needle = query.trim().toLocaleLowerCase();
  const visible = plan.items.filter(
    (item) =>
      !needle ||
      [item.id, item.title, ...item.tags]
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle),
  );
  const configured = states.filter((state) => state.source_id).length;
  const onRefresh = () => setRefresh((value) => value + 1);
  return (
    <section className="ta-checklist-plan" aria-label={plan.title}>
      <div className="ta-checklist-header">
        <div>
          <h3>{plan.title}</h3>
          <p>
            {plan.chapters.length} 章 · {plan.items.length} 项 · 已配置{" "}
            {configured} 项
          </p>
        </div>
        <div className="ta-checklist-header-actions">
          <ActionButton
            variant="outline"
            busy={configuring}
            disabled={discovering}
            onClick={() => void configure()}
          >
            配置本清单
          </ActionButton>
          <ActionButton
            busy={discovering}
            disabled={configuring}
            onClick={() => void discoverAll()}
          >
            发现全部
          </ActionButton>
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
      <details className="tea-config-details ta-checklist-requirements">
        <summary>清单说明与适用范围</summary>
        <p>{plan.description}</p>
        <RequirementList title="通用要求" items={plan.principles} />
        {plan.channels.map((channel) => (
          <section key={channel.id}>
            <RequirementList title={channel.title} items={channel.questions} />
            <div className="ta-checklist-channel-items">
              {channel.item_ids.map((code) => (
                <Button
                  key={code}
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setQuery("");
                    setOpen(code);
                  }}
                >
                  {code}
                </Button>
              ))}
            </div>
          </section>
        ))}
        <p className="ta-hint">
          依据：{plan.source_document.filename} · {plan.source_document.date} ·
          第 {plan.version} 版
        </p>
      </details>
      <div className="ta-checklist-filter">
        <Input
          aria-label="搜索采集条目"
          placeholder="搜索编号、名称或标签"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <span>{visible.length} 项</span>
      </div>
      <div className="ta-checklist-chapters">
        {plan.chapters.map((chapter) => {
          const chapterItems = visible.filter(
            (item) => item.chapter_id === chapter.id,
          );
          if (!chapterItems.length) return null;
          return (
            <section className="ta-checklist-chapter" key={chapter.id}>
              <div className="ta-checklist-chapter-title">
                <span>{chapter.id}</span>
                <h4>{chapter.title}</h4>
                <small>{chapterItems.length} 项</small>
              </div>
              {chapter.notes.length > 0 && (
                <details className="ta-checklist-chapter-notes">
                  <summary>本章要求</summary>
                  <RequirementList items={chapter.notes} />
                </details>
              )}
              <div className="ta-checklist-rows">
                {chapterItems.map((item) => {
                  const state = statesById.get(item.id);
                  const expanded = open === item.id;
                  const label = state
                    ? coverageLabels[state.coverage]
                    : "未开始";
                  return (
                    <article
                      className={`ta-checklist-row${expanded ? "is-open" : ""}`}
                      key={item.id}
                    >
                      <button
                        type="button"
                        className="ta-checklist-row-toggle"
                        aria-expanded={expanded}
                        aria-label={`${item.id} ${item.title}`}
                        onClick={() => setOpen(expanded ? "" : item.id)}
                      >
                        <span className="ta-checklist-row-chevron">
                          {expanded ? (
                            <ChevronDown size={16} />
                          ) : (
                            <ChevronRight size={16} />
                          )}
                        </span>
                        <span className="ta-checklist-item-code">
                          {item.id}
                        </span>
                        <span className="ta-checklist-item-title">
                          <strong>{item.title}</strong>
                          {item.conditional && <small>按条件采集</small>}
                        </span>
                        <span className="ta-checklist-row-count">
                          候选 {state?.counts.candidates ?? 0} · 已存{" "}
                          {state?.counts.stored ?? 0}
                        </span>
                        <span
                          className={`ta-checklist-coverage ${state?.coverage ?? "not_started"}`}
                        >
                          {label}
                        </span>
                      </button>
                      {expanded && (
                        <ChecklistItemDetail
                          key={item.id}
                          planId={id}
                          collection={`country:${plan.country}`}
                          item={item}
                          state={state}
                          ownerId={ownerId}
                          onRefresh={onRefresh}
                        />
                      )}
                    </article>
                  );
                })}
              </div>
            </section>
          );
        })}
        {!visible.length && <Empty title="没有匹配的条目" />}
      </div>
    </section>
  );
}
