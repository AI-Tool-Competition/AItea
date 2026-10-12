"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { errorMessage, teaRequest } from "@/core/tea/api";
import { type Document, type ListResult } from "@/core/tea/types";

import { Busy, dateLabel, ErrorNotice } from "./shared";

export function DocumentHistory({
  document,
  onSelect,
}: {
  document: Document;
  onSelect: (document: Document) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ListResult<Document>>({
    items: [],
    total: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<Document>>(
      `/documents/${encodeURIComponent(document.id)}/versions?limit=20&offset=${offset}`,
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
  return (
    <div className="ta-version-list">
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : loading ? (
        <Busy />
      ) : (
        data.items.map((version) => (
          <article key={version.id}>
            <div>
              <strong>
                v{version.version}
                {version.id === document.id ? " · 当前查看" : ""}
              </strong>
              <span>{dateLabel(version.created_at, true)}</span>
              <small>
                {version.metadata?.review_status === "published"
                  ? "已审核"
                  : "待审核"}
                {version.metadata?.effective_from
                  ? ` · 生效 ${version.metadata.effective_from}`
                  : ""}
                {version.metadata?.effective_to
                  ? ` · 失效 ${version.metadata.effective_to}`
                  : ""}
              </small>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={version.id === document.id}
              onClick={() => onSelect(version)}
            >
              查看 v{version.version}
            </Button>
          </article>
        ))
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
