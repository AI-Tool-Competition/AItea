"use client";

import { ExternalLink } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { errorMessage, post, safeSourceUrl, TeaApiError } from "@/core/tea/api";
import {
  checklistDrafts,
  type ChecklistDraft,
} from "@/core/tea/collection-drafts";
import {
  type ChecklistItem,
  type ChecklistItemState,
  type CollectionRun,
} from "@/core/tea/types";

import { ChecklistCandidates } from "./collection-checklist-candidates";
import { CollectionProgress, collectionActive } from "./collection-run";
import { DocumentChooser } from "./document-chooser";
import { ActionButton, ErrorNotice } from "./shared";

const purposeLabels = {
  catalog: "目录",
  detail: "详情",
  document: "正文 / 原件",
  reference: "示范 / 参考",
};
const limitLabels: Record<string, string> = {
  max_pages: "目录页上限",
  max_candidates: "候选上限",
  max_items: "文件上限",
  max_depth: "发现层数",
  target_count: "目标数量",
  min_count: "最少数量",
  target_min: "目标最少数量",
  target_max: "目标最多数量",
};

export function RequirementList({
  title,
  items,
}: {
  title?: string;
  items: string[];
}) {
  if (!items.length) return null;
  return (
    <div className="ta-checklist-requirement">
      {title && <h5>{title}</h5>}
      <ul>
        {items.map((value, index) => (
          <li key={index}>{value}</li>
        ))}
      </ul>
    </div>
  );
}
export function ChecklistItemDetail({
  planId,
  collection,
  item,
  state,
  ownerId,
  onRefresh,
}: {
  planId: string;
  collection: string;
  item: ChecklistItem;
  state?: ChecklistItemState;
  ownerId?: string;
  onRefresh: () => void;
}) {
  const key = `${ownerId ?? "session"}:${planId}:${item.id}`;
  const [draft, setDraft] = useState<ChecklistDraft>(
    () =>
      checklistDrafts.get(key) ?? {
        selected: [],
        level: "",
        primaryIds: [],
        confirmed: false,
        excludeId: "",
        excludeNote: "",
      },
  );
  const [localRun, setLocalRun] = useState<CollectionRun>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"discover" | "collect" | "">("");
  const [candidateRefresh, setCandidateRefresh] = useState(0);
  const mutation = useRef<AbortController | null>(null);
  const lastProgress = useRef("");
  useEffect(() => () => mutation.current?.abort(), []);
  const updateDraft = (next: ChecklistDraft) => {
    checklistDrafts.set(key, next);
    setDraft(next);
  };
  const run =
    localRun &&
    (!state?.latest_run || localRun.created_at >= state.latest_run.created_at)
      ? localRun
      : state?.latest_run;
  const active = collectionActive(run);
  const conditionMet = !item.conditional || draft.confirmed;
  const blocked = !!busy || active;
  const basePath = `/collection-plans/${encodeURIComponent(planId)}/items/${encodeURIComponent(item.id)}`;
  function refresh() {
    setCandidateRefresh((value) => value + 1);
    onRefresh();
  }
  async function start(action: "discover" | "collect") {
    if (mutation.current || !conditionMet || active) return;
    if (
      action === "collect" &&
      (!draft.selected.length ||
        !draft.level ||
        (draft.level === "secondary" && !draft.primaryIds.length))
    )
      return;
    const payload = {
      ...(item.conditional ? { confirm_condition: draft.confirmed } : {}),
      ...(action === "collect"
        ? {
            candidate_ids: draft.selected,
            level: draft.level,
            primary_document_ids:
              draft.level === "secondary" ? draft.primaryIds : [],
          }
        : {}),
    };
    const signature = `${action}:${JSON.stringify(payload)}`;
    const request =
      draft.request?.signature === signature
        ? draft.request
        : { signature, id: crypto.randomUUID() };
    updateDraft({ ...draft, request });
    const abort = new AbortController();
    mutation.current = abort;
    setBusy(action);
    setError("");
    try {
      const next = await post<CollectionRun>(
        `${basePath}/${action}`,
        { ...payload, client_request_id: request.id },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      setLocalRun(next);
      updateDraft({
        ...draft,
        request: undefined,
        selected: action === "collect" ? [] : draft.selected,
      });
      refresh();
    } catch (e) {
      if (abort.signal.aborted) return;
      setError(errorMessage(e));
      // An uncertain response reuses the same key; a rejected payload may be corrected.
      if (e instanceof TeaApiError && e.status >= 400 && e.status < 500)
        updateDraft({ ...draft, request: undefined });
      if (e instanceof TeaApiError && e.status === 409) refresh();
    } finally {
      mutation.current = null;
      if (!abort.signal.aborted) setBusy("");
    }
  }
  return (
    <div className="ta-checklist-item-detail">
      <ul className="ta-checklist-entries" aria-label="来源地址">
        {item.entries.map((entry, index) => {
          const href = safeSourceUrl(entry.url);
          return (
            <li key={`${entry.url}:${index}`}>
              <span>{purposeLabels[entry.purpose]}</span>
              {href ? (
                <a
                  href={href}
                  title={entry.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {new URL(href).hostname}
                  <ExternalLink size={12} />
                </a>
              ) : (
                <strong>来源链接不可用</strong>
              )}
            </li>
          );
        })}
      </ul>
      {item.execution === "dataset_review" ? (
        <div className="ta-checklist-review-only">
          <strong>候选命题需核验</strong>
          <p>按清单逐条核对原文、出处与适用范围，再确认可用资料。</p>
        </div>
      ) : (
        <>
          {item.conditional && (
            <label className="tea-checkbox ta-checklist-condition">
              <input
                type="checkbox"
                checked={draft.confirmed}
                disabled={!!busy}
                onChange={(event) =>
                  updateDraft({ ...draft, confirmed: event.target.checked })
                }
              />
              <span>已确认：{item.trigger}</span>
            </label>
          )}
          <div className="ta-checklist-discover">
            <p className="ta-hint">先发现候选，再选择入库。</p>
            <ActionButton
              variant="outline"
              busy={busy === "discover"}
              disabled={blocked || !conditionMet}
              onClick={() => void start("discover")}
            >
              {(state?.counts.remaining_pages ?? 0) > 0
                ? "继续发现"
                : "发现候选"}
            </ActionButton>
          </div>
          {(error || state?.last_error) && (
            <ErrorNotice message={error || state!.last_error!} />
          )}
          {run && (
            <CollectionProgress
              key={run.id}
              initial={run}
              onChange={(next) => {
                setLocalRun(next);
                const stamp = `${next.id}:${next.status}:${next.stored}:${next.pages ?? 0}:${next.processed}`;
                if (lastProgress.current !== stamp) {
                  lastProgress.current = stamp;
                  refresh();
                }
              }}
            />
          )}
          <ChecklistCandidates
            key={key}
            basePath={basePath}
            refreshKey={candidateRefresh}
            selected={draft.selected}
            storedSelected={draft.storedSelected ?? []}
            disabled={!!busy}
            onSelection={(selected, storedSelected) =>
              updateDraft({
                ...draft,
                selected,
                storedSelected:
                  storedSelected ??
                  (draft.storedSelected ?? []).filter((id) =>
                    selected.includes(id),
                  ),
              })
            }
            excludeId={draft.excludeId}
            excludeNote={draft.excludeNote}
            onExclusionDraft={(excludeId, excludeNote) =>
              updateDraft({ ...draft, excludeId, excludeNote })
            }
            onChanged={refresh}
          />
          {draft.selected.length > 0 && (
            <div className="ta-checklist-collect">
              <div className="ta-checklist-collect-top">
                <label className="tea-field">
                  <span>本次资料级别</span>
                  <select
                    aria-label="本次资料级别"
                    value={draft.level}
                    disabled={!!busy}
                    onChange={(event) =>
                      updateDraft({
                        ...draft,
                        level: event.target.value as ChecklistDraft["level"],
                      })
                    }
                  >
                    <option value="">选择级别</option>
                    <option value="primary">一级 · 主法规</option>
                    <option value="secondary">二级 · 引用或关联法规</option>
                  </select>
                </label>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!!busy}
                  onClick={() => updateDraft({ ...draft, selected: [] })}
                >
                  清空选择
                </Button>
              </div>
              {draft.level === "secondary" && (
                <div className="ta-checklist-primary">
                  <h5>关联本国一级法规</h5>
                  <DocumentChooser
                    ownerId={ownerId}
                    sourceScope="personal"
                    collection={collection}
                    primaryOnly
                    selected={draft.primaryIds}
                    onChange={(primaryIds) =>
                      updateDraft({ ...draft, primaryIds })
                    }
                  />
                </div>
              )}
              <ActionButton
                busy={busy === "collect"}
                disabled={
                  blocked ||
                  !conditionMet ||
                  !draft.level ||
                  (draft.level === "secondary" && !draft.primaryIds.length)
                }
                onClick={() => void start("collect")}
              >
                {draft.selected.every((id) =>
                  draft.storedSelected?.includes(id),
                )
                  ? `检查新版本（${draft.selected.length} 项）`
                  : `采集所选 ${draft.selected.length} 项`}
              </ActionButton>
            </div>
          )}
        </>
      )}
      <details className="tea-config-details ta-checklist-requirements">
        <summary>采集要求</summary>
        <RequirementList title="采集范围" items={item.scope_include} />
        <RequirementList
          title="来源说明"
          items={item.entries.map(
            (entry) => `${entry.url}${entry.note ? `：${entry.note}` : ""}`,
          )}
        />
        <RequirementList title="排除范围" items={item.scope_exclude} />
        <RequirementList title="采集步骤" items={item.steps} />
        <RequirementList title="保留字段" items={item.fields} />
        <RequirementList title="版本规则" items={item.version_rules} />
        <RequirementList title="关联规则" items={item.relation_rules} />
        <RequirementList title="验收要求" items={item.acceptance} />
        <div className="ta-checklist-requirement">
          <h5>文档原文</h5>
          <p className="ta-checklist-verbatim">{item.verbatim_requirements}</p>
        </div>
        <div className="ta-checklist-limits">
          {Object.entries(item.limits)
            .filter(
              ([name, value]) =>
                name in limitLabels &&
                (typeof value === "string" || typeof value === "number"),
            )
            .map(([name, value]) => (
              <span key={name}>
                {limitLabels[name]}：{String(value)}
              </span>
            ))}
        </div>
      </details>
    </div>
  );
}
