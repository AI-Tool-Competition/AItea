"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, post, teaRequest } from "@/core/tea/api";
import {
  relationDrafts,
  type RelationDraft,
} from "@/core/tea/collection-drafts";
import { countryLabel } from "@/core/tea/countries";
import {
  type Document,
  type DocumentDetail,
  type LawRelation,
  type ListResult,
} from "@/core/tea/types";

import { DocumentChooser } from "./document-chooser";
import { ActionButton, Busy, ErrorNotice } from "./shared";

function RelationLocator({ value }: { value: string }) {
  let evidence: Record<string, unknown> | undefined;
  try {
    const parsed: unknown = JSON.parse(value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed))
      evidence = parsed as Record<string, unknown>;
  } catch {
    // Manually entered locations are plain text.
  }
  if (!evidence) return value ? <small>{value}</small> : null;
  return (
    <>
      {typeof evidence.locator === "string" && (
        <small>{evidence.locator}</small>
      )}
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer">查看来源与版本记录</summary>
        <pre className="mt-2 break-all whitespace-pre-wrap">
          {JSON.stringify(evidence, null, 2)}
        </pre>
      </details>
    </>
  );
}

export function DocumentRelations({
  document,
  onSelect,
  ownerId,
}: {
  document: Document;
  onSelect: (document: Document) => void;
  ownerId?: string;
}) {
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ListResult<LawRelation>>({
    items: [],
    total: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const key = ownerId ? `${ownerId}:${document.id}` : undefined;
  const [adding, setAdding] = useState(
    () => !!(key && relationDrafts.has(key)),
  );
  const [draft, setDraft] = useState<RelationDraft>(() => {
    const saved = key ? relationDrafts.get(key) : undefined;
    const fixed = document.country ? `country:${document.country}` : undefined;
    if (saved)
      return fixed && saved.collection !== fixed
        ? { ...saved, collection: fixed, target: [] }
        : saved;
    return {
      target: [],
      collection:
        fixed ?? document.classifications?.[0]?.collection ?? "default",
      relation: "cites",
      locator: "",
    };
  });
  const { target, relation, locator } = draft;
  const collection = document.country
    ? `country:${document.country}`
    : draft.collection;
  const dirty = useRef(false);
  function update(next: Partial<RelationDraft>) {
    const value = { ...draft, ...next };
    setDraft(value);
    dirty.current = true;
    if (key) relationDrafts.set(key, value);
  }
  useEffect(() => {
    function guard(event: BeforeUnloadEvent) {
      if (dirty.current || (key && relationDrafts.has(key))) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [key]);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const requests = useRef<{ open?: AbortController; save?: AbortController }>(
    {},
  );
  const secondary =
    (document.classifications?.find((item) => item.collection === collection)
      ?.level ?? document.legal_level) === "secondary";
  useEffect(() => {
    const current = requests.current;
    return () => {
      current.open?.abort();
      current.save?.abort();
    };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<LawRelation>>(
      `/documents/${encodeURIComponent(document.id)}/relations?limit=20&offset=${offset}`,
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
  }, [document.id, offset, retry]);
  async function open(id: string) {
    requests.current.open?.abort();
    const abort = new AbortController();
    requests.current.open = abort;
    setError("");
    try {
      const next = await teaRequest<DocumentDetail>(
        `/documents/${encodeURIComponent(id)}?limit=1`,
        { signal: abort.signal },
      );
      if (!abort.signal.aborted) onSelect(next.document);
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    }
  }
  async function save() {
    if (requests.current.save || !target[0]) return;
    const abort = new AbortController();
    requests.current.save = abort;
    setBusy(true);
    setSaveError("");
    try {
      await post<LawRelation>(
        `/documents/${encodeURIComponent(secondary ? target[0] : document.id)}/relations`,
        {
          collection,
          target_document_id: secondary ? document.id : target[0],
          relation,
          locator,
        },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      setDraft({ ...draft, target: [], locator: "" });
      dirty.current = false;
      if (key) relationDrafts.delete(key);
      setAdding(false);
      setOffset(0);
      setRetry((value) => value + 1);
    } catch (e) {
      if (!abort.signal.aborted) setSaveError(errorMessage(e));
    } finally {
      requests.current.save = undefined;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  return (
    <div className="ta-relation-list">
      <div className="ta-subheading">
        <span>{loading ? "读取中…" : `${data.total} 条关联`}</span>
        {document.source_scope !== "system" && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setAdding(!adding)}
          >
            {adding ? "收起" : "添加关联"}
          </Button>
        )}
      </div>
      {adding && document.source_scope !== "system" && (
        <form
          className="ta-relation-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <fieldset disabled={busy} className="ta-form-fields">
            <div className="tea-form-row">
              <label className="tea-field">
                <span>关联采集组</span>
                <Input
                  required
                  readOnly={!!document.country}
                  value={collection}
                  onChange={(e) =>
                    update({ collection: e.target.value, target: [] })
                  }
                />
              </label>
              <label className="tea-field">
                <span>关系</span>
                <select
                  value={relation}
                  onChange={(e) =>
                    update({
                      relation: e.target.value as LawRelation["relation"],
                    })
                  }
                >
                  <option value="cites">引用</option>
                  <option value="related">相关</option>
                </select>
              </label>
            </div>
            <fieldset className="ta-collection-fieldset">
              <legend>
                {secondary ? "引用本法规的一级资料" : "引用或关联的法规"}
              </legend>
              <DocumentChooser
                ownerId={ownerId}
                sourceScope="personal"
                selected={target}
                onChange={(target) => update({ target })}
                primaryOnly={secondary}
                excludeId={document.id}
                scope={document.case_id}
                country={
                  document.country ??
                  (document.case_id ? undefined : "unassigned")
                }
                collection={document.country ? collection : undefined}
                multiple={false}
              />
            </fieldset>
            <label className="tea-field">
              <span>原文定位</span>
              <Input
                value={locator}
                onChange={(e) => update({ locator: e.target.value })}
                placeholder="例如：第十二条第 2 款"
              />
            </label>
            {saveError && <ErrorNotice message={saveError} />}
            <ActionButton
              size="sm"
              variant="outline"
              type="submit"
              busy={busy}
              disabled={!target.length}
            >
              保存关联
            </ActionButton>
          </fieldset>
        </form>
      )}
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : loading ? (
        <Busy />
      ) : data.items.length ? (
        data.items.map((item) => {
          const incoming = item.target_document_id === document.id;
          return (
            <article key={item.id} className="ta-relation-row">
              <span className="ta-badge shrink-0 whitespace-nowrap">
                {item.relation === "cites"
                  ? incoming
                    ? "被引用"
                    : "引用"
                  : "相关"}
              </span>
              <div>
                <button
                  onClick={() =>
                    void open(
                      incoming
                        ? item.source_document_id
                        : item.target_document_id,
                    )
                  }
                >
                  {incoming ? item.source_title : item.target_title} · v
                  {incoming ? item.source_version : item.target_version}
                </button>
                <small>
                  {document.country ? countryLabel(document.country) : "资料库"}{" "}
                  · 本文 v{incoming ? item.target_version : item.source_version}
                </small>
                <RelationLocator value={item.locator} />
              </div>
            </article>
          );
        })
      ) : (
        <p className="ta-hint">暂无法规关联。</p>
      )}
      {data.total > 20 && (
        <div className="ta-pagination">
          <span>
            {offset + 1}–{Math.min(offset + 20, data.total)} / {data.total}
          </span>
          <div>
            <Button
              size="sm"
              variant="outline"
              disabled={loading || !offset}
              onClick={() => setOffset(Math.max(0, offset - 20))}
            >
              上一页
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={loading || offset + 20 >= data.total}
              onClick={() => setOffset(offset + 20)}
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
