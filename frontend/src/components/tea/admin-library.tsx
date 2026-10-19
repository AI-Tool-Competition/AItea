"use client";

import {
  ArrowLeft,
  ChevronRight,
  FileText,
  RefreshCw,
  Search,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, teaRequest } from "@/core/tea/api";
import { countryLabel, useCollectionCountries } from "@/core/tea/countries";
import {
  documentSize,
  documentState,
  mergeProcessingDocument,
} from "@/core/tea/document-state";
import { librarySamples } from "@/core/tea/library-samples";
import { type Document, type ListResult } from "@/core/tea/types";
import { useDocumentProcessing } from "@/core/tea/use-document-processing";

import { DocumentPreview } from "./document-preview";
import { Busy, Empty, ErrorNotice } from "./shared";

const PAGE_SIZE = 20;
interface AdminLibraryProps {
  samples?: boolean;
  ownerId?: string;
}

export function AdminLibrary(props: AdminLibraryProps) {
  return (
    <LawLibrary
      key={`${props.ownerId ?? "session"}:${!!props.samples}`}
      {...props}
    />
  );
}

function LawLibrary({ samples = false, ownerId }: AdminLibraryProps) {
  const countries = useCollectionCountries(ownerId, !samples);
  const [countrySelection, setCountrySelection] = useState("");
  const country = countrySelection || countries.defaultCountry;
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ListResult<Document>>({
    items: [],
    total: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<Document>();
  useEffect(() => {
    if (samples) {
      const items = librarySamples
        .map((item) => item.document)
        .filter((item) => item.title.includes(search));
      setData({
        items: items.slice(offset, offset + PAGE_SIZE),
        total: items.length,
      });
      setLoading(false);
      return;
    }
    if (!countries.ready) return;
    const abort = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      source_scope: "system",
      country,
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (search) params.set("q", search);
    teaRequest<ListResult<Document>>(`/documents?${params}`, {
      signal: abort.signal,
    })
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
  }, [country, countries.ready, offset, search, retry, samples]);
  const updateDocuments = useCallback((updates: Document[]) => {
    const byId = new Map(updates.map((document) => [document.id, document]));
    setData((current) => ({
      ...current,
      items: current.items.map((item) =>
        byId.has(item.id)
          ? mergeProcessingDocument(item, byId.get(item.id)!)
          : item,
      ),
    }));
    setSelected((current) =>
      current && byId.has(current.id)
        ? mergeProcessingDocument(current, byId.get(current.id)!)
        : current,
    );
  }, []);
  const processing = useDocumentProcessing({
    documents: selected ? [selected] : data.items,
    boundary: `${ownerId}:system:${country}`,
    active: !samples,
    onUpdate: updateDocuments,
  });
  if (selected)
    return (
      <div className="ta-law-preview">
        <Button
          variant="ghost"
          size="sm"
          className="ta-law-back"
          onClick={() => {
            setSelected(undefined);
            setRetry((value) => value + 1);
          }}
        >
          <ArrowLeft size={15} />
          返回法律总库
        </Button>
        <header className="ta-law-preview-heading">
          <h2>{selected.title}</h2>
        </header>
        <DocumentPreview
          key={selected.id}
          document={selected}
          processing={processing}
          sample={
            samples
              ? librarySamples.find((item) => item.document.id === selected.id)
              : undefined
          }
        />
      </div>
    );
  return (
    <div className="ta-law-library">
      <div className="ta-law-heading">
        <h2>法律总库</h2>
        <span>{!loading && `${data.total} 份资料`}</span>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="刷新法律总库"
          disabled={loading}
          onClick={() => setRetry((value) => value + 1)}
        >
          <RefreshCw size={15} />
        </Button>
      </div>
      <div className="ta-library-toolbar">
        <form
          className="ta-file-search"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(query.trim());
            setOffset(0);
          }}
        >
          <Search size={16} />
          <Input
            aria-label="搜索资料名称"
            placeholder="搜索文件名称"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Button type="submit" variant="ghost" size="sm">
            搜索
          </Button>
        </form>
        {!samples && (
          <select
            className="ta-country-filter"
            aria-label="资料国家"
            value={country}
            disabled={!countries.ready}
            onChange={(event) => {
              setCountrySelection(event.target.value);
              setOffset(0);
              setData({ items: [], total: 0 });
            }}
          >
            {!countries.ready && <option value="">读取国家…</option>}
            {countries.countries.map((code) => (
              <option value={code} key={code}>
                {countryLabel(code)}
              </option>
            ))}
            <option value="unassigned">未分国</option>
          </select>
        )}
      </div>
      {countries.error && (
        <ErrorNotice message={countries.error} onRetry={countries.retry} />
      )}
      {processing.error && (
        <ErrorNotice message={processing.error} onRetry={processing.retry} />
      )}
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      )}
      {loading ? (
        <Busy label="读取资料…" />
      ) : data.items.length ? (
        <>
          <div className="ta-law-table-wrap">
            <table className="ta-law-table">
              <thead>
                <tr>
                  <th>文件名称</th>
                  <th>国家</th>
                  <th>状态</th>
                  <th>
                    <span className="sr-only">操作</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((document) => (
                  <tr key={document.id}>
                    <td>
                      <button
                        className="ta-law-file"
                        onClick={() => setSelected(document)}
                      >
                        <FileText size={20} strokeWidth={1.5} />
                        <span>
                          <strong>{document.title}</strong>
                          <small>
                            {document.filename.split(".").at(-1)?.toUpperCase()}{" "}
                            · {documentSize(document.size)}
                          </small>
                        </span>
                      </button>
                    </td>
                    <td className="ta-law-country">
                      {countryLabel(
                        document.country ?? (samples ? "ID" : "unassigned"),
                      )}
                    </td>
                    <td>
                      <span
                        className={`tea-preview-status ${documentState(document).tone}`}
                      >
                        {documentState(document).label}
                      </span>
                    </td>
                    <td>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setSelected(document)}
                        aria-label={`预览 ${document.title}`}
                      >
                        预览
                        <ChevronRight size={14} />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.total > PAGE_SIZE && (
            <div className="ta-pagination">
              <span>
                {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} /{" "}
                {data.total}
              </span>
              <div>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={loading || offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  上一页
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={loading || offset + PAGE_SIZE >= data.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </>
      ) : (
        !error && <Empty title={search ? "没有找到匹配文件" : "暂无资料"} />
      )}
    </div>
  );
}
