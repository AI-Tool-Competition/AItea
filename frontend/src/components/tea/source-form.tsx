"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  errorMessage,
  patch,
  post,
  TeaApiError,
  teaRequest,
} from "@/core/tea/api";
import {
  sourceDrafts,
  splitTags,
  type SourceDraft,
} from "@/core/tea/collection-drafts";
import { type CollectionPlan, type Source } from "@/core/tea/types";

import { DocumentChooser } from "./document-chooser";
import { ActionButton, ErrorNotice } from "./shared";

const defaultPlan: CollectionPlan = {
  collection: "default",
  level: "primary",
  seed_urls: [],
  primary_document_ids: [],
  discover_attachments: true,
  max_items: 200,
};

export function SourceForm({
  source,
  ownerId,
  onSaved,
  onClose,
}: {
  source?: Source;
  ownerId?: string;
  onSaved: (source: Source) => void;
  onClose: () => void;
}) {
  const key = ownerId ? `${ownerId}:${source?.id ?? "new"}` : undefined;
  const [draft, setDraft] = useState<SourceDraft>(
    () =>
      (key ? sourceDrafts.get(key) : undefined) ?? {
        name: source?.name ?? "",
        url: source?.url ?? "",
        interval_hours: source?.interval_hours ?? 24,
        tags: source?.tags.join(", ") ?? "",
        plan: source?.plan ?? { ...defaultPlan },
        seed_text: source?.plan?.seed_urls.join("\n") ?? "",
        expected_revision: source?.revision ?? (source ? 1 : 0),
      },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [latest, setLatest] = useState<Source>();
  const pending = useRef<AbortController | null>(null);
  const dirty = useRef(false);
  useEffect(() => () => pending.current?.abort(), []);
  function update(next: SourceDraft) {
    dirty.current = true;
    setDraft(next);
    if (key) sourceDrafts.set(key, next);
  }
  useEffect(() => {
    function guard(event: BeforeUnloadEvent) {
      if (dirty.current || (key && sourceDrafts.has(key))) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [key]);
  function plan(next: Partial<CollectionPlan>) {
    update({ ...draft, plan: { ...draft.plan, ...next } });
  }
  async function save() {
    if (pending.current) return;
    if (
      draft.plan.level === "secondary" &&
      !draft.plan.primary_document_ids.length
    ) {
      setError("请选择关联的一级资料。");
      return;
    }
    const seedUrls = [
      ...new Set(
        draft.seed_text
          .split(/\r?\n/)
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    ];
    if (
      seedUrls.some((value) => {
        try {
          return new URL(value).protocol !== "https:";
        } catch {
          return true;
        }
      })
    ) {
      setError("补充地址须为 HTTPS 地址。");
      return;
    }
    const abort = new AbortController();
    pending.current = abort;
    setBusy(true);
    setError("");
    setConflict(false);
    setLatest(undefined);
    const payload = {
      name: draft.name,
      url: draft.url,
      interval_hours: draft.interval_hours,
      tags: splitTags(draft.tags),
      plan: {
        ...draft.plan,
        seed_urls: seedUrls,
        primary_document_ids:
          draft.plan.level === "secondary"
            ? draft.plan.primary_document_ids
            : [],
      },
    };
    try {
      const next = source
        ? await patch<Source>(
            `/sources/${encodeURIComponent(source.id)}`,
            { ...payload, expected_revision: draft.expected_revision },
            abort.signal,
          )
        : await post<Source>(
            "/sources",
            { ...payload, enabled: true },
            abort.signal,
          );
      if (abort.signal.aborted) return;
      if (key) sourceDrafts.delete(key);
      dirty.current = false;
      onSaved(next);
    } catch (e) {
      if (!abort.signal.aborted) {
        setError(errorMessage(e));
        setConflict(e instanceof TeaApiError && e.status === 409);
      }
    } finally {
      pending.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  async function loadLatest() {
    if (!source || pending.current) return;
    const abort = new AbortController();
    pending.current = abort;
    setBusy(true);
    try {
      const next = await teaRequest<Source>(
        `/sources/${encodeURIComponent(source.id)}`,
        { signal: abort.signal },
      );
      if (!abort.signal.aborted) setLatest(next);
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      pending.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  function resolveConflict(keep: boolean) {
    if (!latest) return;
    update(
      keep
        ? { ...draft, expected_revision: latest.revision ?? 1 }
        : {
            name: latest.name,
            url: latest.url,
            interval_hours: latest.interval_hours,
            tags: latest.tags.join(", "),
            plan: latest.plan ?? { ...defaultPlan },
            seed_text: latest.plan?.seed_urls.join("\n") ?? "",
            expected_revision: latest.revision ?? 1,
          },
    );
    setError("");
    setConflict(false);
    setLatest(undefined);
  }
  return (
    <form
      className="tea-source-form"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <fieldset disabled={busy} className="ta-form-fields">
        <div className="tea-form-row">
          <label className="tea-field">
            <span>来源名称</span>
            <Input
              required
              value={draft.name}
              onChange={(e) => update({ ...draft, name: e.target.value })}
            />
          </label>
          <label className="tea-field">
            <span>检查间隔 · 小时</span>
            <Input
              required
              type="number"
              min={1}
              max={8760}
              value={draft.interval_hours}
              onChange={(e) =>
                update({ ...draft, interval_hours: Number(e.target.value) })
              }
            />
          </label>
        </div>
        <label className="tea-field">
          <span>公开 HTTPS 地址</span>
          <Input
            required
            type="url"
            pattern="https://.*"
            value={draft.url}
            onChange={(e) => update({ ...draft, url: e.target.value })}
            placeholder="https://…"
          />
        </label>
        <div className="tea-form-row">
          <label className="tea-field">
            <span>采集组</span>
            <Input
              required
              value={draft.plan.collection}
              onChange={(e) => plan({ collection: e.target.value })}
              placeholder="ID / SG"
            />
          </label>
          <label className="tea-field">
            <span>资料级别</span>
            <select
              value={draft.plan.level}
              onChange={(e) =>
                plan({ level: e.target.value as CollectionPlan["level"] })
              }
            >
              <option value="primary">一级 · 主资料</option>
              <option value="secondary">二级 · 引用法规</option>
            </select>
          </label>
        </div>
        {draft.plan.level === "secondary" && (
          <fieldset className="ta-collection-fieldset">
            <legend>关联一级资料</legend>
            <DocumentChooser
              ownerId={ownerId}
              sourceScope="personal"
              primaryOnly
              selected={draft.plan.primary_document_ids}
              onChange={(ids) => plan({ primary_document_ids: ids })}
            />
          </fieldset>
        )}
        <label className="tea-field">
          <span>标签</span>
          <Input
            value={draft.tags}
            onChange={(e) => update({ ...draft, tags: e.target.value })}
            placeholder="国家, 清单编号, 主题"
          />
        </label>
        <details className="tea-config-details">
          <summary>批量与附件</summary>
          <label className="tea-field">
            <span>补充地址 · 每行一个</span>
            <Textarea
              rows={4}
              value={draft.seed_text}
              onChange={(e) => update({ ...draft, seed_text: e.target.value })}
              placeholder="https://…"
            />
          </label>
          <div className="tea-form-row">
            <label className="tea-field">
              <span>单次文件上限</span>
              <Input
                required
                type="number"
                min={1}
                max={1000}
                value={draft.plan.max_items}
                onChange={(e) => plan({ max_items: Number(e.target.value) })}
              />
            </label>
            <label className="tea-checkbox">
              <input
                type="checkbox"
                checked={draft.plan.discover_attachments}
                onChange={(e) =>
                  plan({ discover_attachments: e.target.checked })
                }
              />
              保存页面中的同站附件
            </label>
          </div>
        </details>
        {error && <ErrorNotice message={error} />}
        {conflict && !latest && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void loadLatest()}
          >
            核对最新配置
          </Button>
        )}
        {latest && (
          <div className="ta-conflict-review">
            <strong>最新已保存配置</strong>
            <pre>
              {JSON.stringify(
                {
                  名称: latest.name,
                  地址: latest.url,
                  检查间隔: latest.interval_hours,
                  标签: latest.tags,
                  采集组: latest.plan?.collection,
                  级别: latest.plan?.level === "secondary" ? "二级" : "一级",
                  补充地址: latest.plan?.seed_urls,
                  主法版本: latest.plan?.primary_document_ids,
                  文件上限: latest.plan?.max_items,
                  保存附件: latest.plan?.discover_attachments,
                },
                null,
                2,
              )}
            </pre>
            <div className="ta-collection-actions">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => resolveConflict(false)}
              >
                用最新配置替换输入
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => resolveConflict(true)}
              >
                核对后保留我的修改
              </Button>
            </div>
          </div>
        )}
        <div className="ta-collection-actions">
          <ActionButton busy={busy} type="submit">
            保存来源
          </ActionButton>
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={onClose}
          >
            收起
          </Button>
        </div>
      </fieldset>
    </form>
  );
}
