"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, teaRequest } from "@/core/tea/api";
import { type Document, type ListResult } from "@/core/tea/types";

import { Busy, ErrorNotice } from "./shared";

export function DocumentChooser({
  selected,
  onChange,
  scope,
  collection,
  country,
  primaryOnly = false,
  excludeId,
  multiple = true,
  sourceScope,
}: {
  selected: string[];
  onChange: (ids: string[]) => void;
  scope?: string | null;
  collection?: string;
  country?: string;
  primaryOnly?: boolean;
  excludeId?: string;
  multiple?: boolean;
  sourceScope?: "personal" | "system";
}) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ListResult<Document>>({
    items: [],
    total: 0,
  });
  const [names, setNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      role: "primary",
      limit: "20",
      offset: String(offset),
    });
    if (scope) params.set("case_id", scope);
    if (collection) params.set("collection", collection);
    if (country) params.set("country", country);
    if (sourceScope) params.set("source_scope", sourceScope);
    if (primaryOnly) params.set("legal_level", "primary");
    if (search) params.set("q", search);
    teaRequest<ListResult<Document>>(`/documents?${params}`, {
      signal: abort.signal,
    })
      .then((data) => {
        if (abort.signal.aborted) return;
        setPage(data);
        setNames((previous) =>
          Object.fromEntries([
            ...Object.entries(previous),
            ...data.items.map((doc) => [
              doc.id,
              `${doc.title} · v${doc.version}`,
            ]),
          ]),
        );
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [
    scope,
    collection,
    country,
    search,
    offset,
    retry,
    primaryOnly,
    sourceScope,
  ]);
  const options = page.items.filter(
    (doc) =>
      doc.id !== excludeId &&
      (!primaryOnly || doc.legal_level !== "secondary") &&
      (!country ||
        (country === "unassigned" ? !doc.country : doc.country === country)),
  );
  return (
    <div className="ta-document-chooser">
      <div className="ta-picker-search">
        <Input
          aria-label={primaryOnly ? "搜索一级资料" : "搜索关联法规"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="名称"
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              setSearch(query.trim());
              setOffset(0);
            }
          }}
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            setSearch(query.trim());
            setOffset(0);
          }}
        >
          查找
        </Button>
      </div>
      {selected.length > 0 && (
        <div className="ta-selected-documents">
          {selected.map((id) => (
            <button
              type="button"
              key={id}
              onClick={() => onChange(selected.filter((value) => value !== id))}
              aria-label={`移除 ${names[id] ?? id}`}
            >
              {names[id] ?? id}
              <span aria-hidden="true"> ×</span>
            </button>
          ))}
        </div>
      )}
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : loading ? (
        <Busy />
      ) : (
        <div className="ta-picker-options">
          {options.map((doc) => (
            <label className="tea-checkbox" key={doc.id}>
              <input
                type="checkbox"
                checked={selected.includes(doc.id)}
                onChange={(e) =>
                  onChange(
                    e.target.checked
                      ? multiple
                        ? [...selected, doc.id]
                        : [doc.id]
                      : selected.filter((id) => id !== doc.id),
                  )
                }
              />
              <span>
                {doc.title}
                <small>
                  v{doc.version}
                  {doc.legal_level === "secondary" ? " · 二级" : " · 一级"}
                </small>
              </span>
            </label>
          ))}
          {!options.length && <p className="ta-hint">本页无可选资料。</p>}
        </div>
      )}
      {page.total > 20 && (
        <div className="ta-pagination">
          <span>
            {offset + 1}–{Math.min(offset + 20, page.total)} / {page.total}
          </span>
          <div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={loading || !offset}
              onClick={() => setOffset(Math.max(0, offset - 20))}
            >
              上一页
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={loading || offset + 20 >= page.total}
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
