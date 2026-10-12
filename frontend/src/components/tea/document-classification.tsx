"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, patch, TeaApiError, teaRequest } from "@/core/tea/api";
import {
  classificationDrafts,
  splitTags,
  type ClassificationDraft,
} from "@/core/tea/collection-drafts";
import { type Document, type DocumentDetail } from "@/core/tea/types";

import { ActionButton, ErrorNotice } from "./shared";

export function DocumentClassificationEditor({
  document,
  ownerId,
  onSaved,
}: {
  document: Document;
  ownerId?: string;
  onSaved: (document: Document) => void;
}) {
  const key = ownerId ? `${ownerId}:${document.id}` : undefined;
  const first = document.classifications?.[0];
  const [draft, setDraft] = useState<ClassificationDraft>(
    () =>
      (key ? classificationDrafts.get(key) : undefined) ?? {
        collection: first?.collection ?? "default",
        level: first?.level ?? "primary",
        tags: (first?.tags ?? document.tags).join(", "),
        expected_revision: first?.revision ?? 0,
      },
  );
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [latest, setLatest] = useState<Document>();
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const dirty = useRef(false);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    function guard(event: BeforeUnloadEvent) {
      if (dirty.current || (key && classificationDrafts.has(key))) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [key]);
  function update(next: ClassificationDraft) {
    setDraft(next);
    dirty.current = true;
    if (key) classificationDrafts.set(key, next);
  }
  function collection(value: string) {
    const existing = document.classifications?.find(
      (item) => item.collection === value,
    );
    update({
      ...draft,
      collection: value,
      expected_revision: existing?.revision ?? 0,
      ...(existing
        ? { level: existing.level, tags: existing.tags.join(", ") }
        : {}),
    });
  }
  async function save() {
    if (request.current) return;
    const abort = new AbortController();
    request.current = abort;
    setBusy(true);
    setError("");
    setConflict(false);
    setLatest(undefined);
    try {
      const next = await patch<Document>(
        `/documents/${encodeURIComponent(document.id)}/classification`,
        { ...draft, tags: splitTags(draft.tags) },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      const saved = next.classifications?.find(
        (item) => item.collection === draft.collection,
      );
      setDraft({
        ...draft,
        expected_revision: saved?.revision ?? draft.expected_revision + 1,
      });
      if (key) classificationDrafts.delete(key);
      dirty.current = false;
      onSaved(next);
    } catch (e) {
      if (!abort.signal.aborted) {
        setError(errorMessage(e));
        setConflict(e instanceof TeaApiError && e.status === 409);
      }
    } finally {
      request.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  async function loadLatest() {
    if (request.current) return;
    const abort = new AbortController();
    request.current = abort;
    setBusy(true);
    try {
      const next = await teaRequest<DocumentDetail>(
        `/documents/${encodeURIComponent(document.id)}?limit=1`,
        { signal: abort.signal },
      );
      if (!abort.signal.aborted) setLatest(next.document);
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      request.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  const latestClassification = latest?.classifications?.find(
    (item) => item.collection === draft.collection,
  );
  function resolveConflict(keep: boolean) {
    if (!latest) return;
    update({
      ...draft,
      expected_revision: latestClassification?.revision ?? 0,
      ...(keep
        ? {}
        : {
            level: latestClassification?.level ?? "primary",
            tags: (latestClassification?.tags ?? []).join(", "),
          }),
    });
    setError("");
    setConflict(false);
    setLatest(undefined);
  }
  return (
    <details className="tea-config-details ta-classification-editor">
      <summary>分级与标签</summary>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <fieldset disabled={busy} className="ta-form-fields">
          <div className="tea-form-row">
            <label className="tea-field">
              <span>采集组</span>
              <Input
                required
                list={`collections-${document.id}`}
                value={draft.collection}
                onChange={(e) => collection(e.target.value)}
              />
              <datalist id={`collections-${document.id}`}>
                {document.classifications?.map((item) => (
                  <option key={item.collection} value={item.collection} />
                ))}
              </datalist>
            </label>
            <label className="tea-field">
              <span>资料级别</span>
              <select
                value={draft.level}
                onChange={(e) =>
                  update({
                    ...draft,
                    level: e.target.value as ClassificationDraft["level"],
                  })
                }
              >
                <option value="primary">一级 · 主资料</option>
                <option value="secondary">二级 · 引用法规</option>
              </select>
            </label>
          </div>
          <label className="tea-field">
            <span>分类标签</span>
            <Input
              value={draft.tags}
              onChange={(e) => update({ ...draft, tags: e.target.value })}
              placeholder="国家, 清单编号, 主题"
            />
          </label>
          {document.classifications?.length ? (
            <div className="ta-collection-tags">
              {document.classifications.map((item) => (
                <span key={item.collection}>
                  {item.collection} ·{" "}
                  {item.level === "primary" ? "一级" : "二级"}
                </span>
              ))}
            </div>
          ) : null}
          {error && <ErrorNotice message={error} />}
          {conflict && !latest && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void loadLatest()}
            >
              核对最新分类
            </Button>
          )}
          {latest && (
            <div className="ta-conflict-review">
              <strong>最新已保存分类</strong>
              <p>
                {draft.collection} ·{" "}
                {latestClassification?.level === "secondary" ? "二级" : "一级"}
              </p>
              <p>
                {latestClassification?.tags.length
                  ? latestClassification.tags.join("、")
                  : "无标签"}
              </p>
              <div className="ta-collection-actions">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => resolveConflict(false)}
                >
                  用最新分类替换输入
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
          <ActionButton type="submit" busy={busy} variant="outline" size="sm">
            保存分级
          </ActionButton>
        </fieldset>
      </form>
    </details>
  );
}
