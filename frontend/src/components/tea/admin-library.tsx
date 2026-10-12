"use client";

import {
  ArrowDownToLine,
  ArrowLeft,
  BookOpen,
  ChevronDown,
  ChevronRight,
  File,
  FileText,
  Layers,
  Link2,
  Paperclip,
  Search,
  Upload,
} from "lucide-react";
import { Fragment, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  downloadUrl,
  errorMessage,
  safeSourceUrl,
  teaRequest,
} from "@/core/tea/api";
import { countryLabel, useCollectionCountries } from "@/core/tea/countries";
import { librarySamples } from "@/core/tea/library-samples";
import {
  type CaseSummary,
  type Document,
  type DocumentDetail,
  type DocumentPart,
  type ListResult,
} from "@/core/tea/types";

import { DocumentClassificationEditor } from "./document-classification";
import { DocumentHistory } from "./document-history";
import { DocumentMetadataEditor } from "./document-metadata";
import { DocumentRelations } from "./document-relations";
import { Busy, dateLabel, Empty, ErrorNotice } from "./shared";

const PAGE_SIZE = 20;
const PART_SIZE = 40;
export function AdminLibrary({
  cases,
  revision,
  onUpload,
  samples = false,
  ownerId,
}: {
  cases: CaseSummary[];
  revision: number;
  onUpload: (scope?: string, parent?: string, country?: string) => void;
  samples?: boolean;
  ownerId?: string;
}) {
  const countries = useCollectionCountries(ownerId, !samples);
  const [countrySelection, setCountrySelection] = useState("");
  const country = countrySelection || countries.defaultCountry;
  const [scope, setScope] = useState("");
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
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (samples) {
      const items = librarySamples
        .map((item) => item.document)
        .filter(
          (item) => item.role === "primary" && item.title.includes(search),
        );
      setData({
        items: items.slice(offset, offset + PAGE_SIZE),
        total: items.length,
      });
      setLoading(false);
      setError("");
      return;
    }
    if (!countries.ready) return;
    const abort = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      role: "primary",
      limit: String(PAGE_SIZE),
      offset: String(offset),
    });
    if (scope) params.set("case_id", scope);
    else params.set("country", country);
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
  }, [
    scope,
    country,
    countries.ready,
    offset,
    search,
    revision,
    retry,
    samples,
  ]);
  if (selected)
    return (
      <DocumentInspector
        key={selected.id}
        document={selected}
        samples={samples}
        ownerId={ownerId}
        revision={revision}
        onBack={() => {
          setSelected(undefined);
          setRetry((value) => value + 1);
        }}
        onSelect={setSelected}
        onUpload={() =>
          onUpload(
            selected.case_id ?? undefined,
            selected.id,
            selected.country ?? "unassigned",
          )
        }
        onSaved={(next) => {
          setSelected(next);
          setData((current) => ({
            ...current,
            items: current.items.map((item) =>
              item.id === next.id ? next : item,
            ),
          }));
        }}
      />
    );
  return (
    <div className="ta-library">
      <div className="ta-library-toolbar">
        <form
          className="ta-file-search"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(query.trim());
            setOffset(0);
          }}
        >
          <Search size={16} />
          <Input
            aria-label="搜索资料名称"
            placeholder="搜索资料名称…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Button type="submit" variant="ghost" size="sm">
            搜索
          </Button>
        </form>
        {!samples && (
          <select
            className="ta-country-filter"
            aria-label="资料国家"
            value={scope ? "case" : countries.ready ? country : ""}
            disabled={!!scope || !countries.ready}
            onChange={(event) => {
              setCountrySelection(event.target.value);
              setOffset(0);
              setExpanded(new Set());
            }}
          >
            {!!scope && <option value="case">按业务目的地</option>}
            {!countries.ready && <option value="">读取国家…</option>}
            {countries.countries.map((code) => (
              <option value={code} key={code}>
                {countryLabel(code)}
              </option>
            ))}
            <option value="unassigned">未分国</option>
          </select>
        )}
        {!samples && (
          <select
            aria-label="资料范围"
            value={scope}
            onChange={(e) => {
              setScope(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">公共资料</option>
            {cases.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        )}
        {!samples && (
          <Button onClick={() => onUpload(scope || undefined)}>
            <Upload size={14} />
            上传资料
          </Button>
        )}
      </div>
      {countries.error && (
        <ErrorNotice message={countries.error} onRetry={countries.retry} />
      )}
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      )}
      <div className="ta-library-table">
        <div className="ta-table-heading">
          <span>
            {loading
              ? "读取中…"
              : `${data.total} 份${samples ? "开发样例" : "资料"}`}
          </span>
          {search && (
            <button
              onClick={() => {
                setSearch("");
                setQuery("");
                setOffset(0);
              }}
            >
              清除筛选
            </button>
          )}
        </div>
        {loading ? (
          <Busy label="读取资料目录…" />
        ) : error ? null : data.items.length ? (
          <div className="ta-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>文件名称</th>
                  <th>解析状态</th>
                  <th>资料审核</th>
                  <th>更新时间</th>
                  <th>
                    <span className="sr-only">操作</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((doc) => (
                  <Fragment key={doc.id}>
                    <tr>
                      <td>
                        <div className="ta-file-name">
                          <button
                            className="ta-expand"
                            aria-label={`${expanded.has(doc.id) ? "收起" : "展开"}${doc.title}的附件`}
                            aria-expanded={expanded.has(doc.id)}
                            onClick={() =>
                              setExpanded((current) => {
                                const next = new Set(current);
                                if (next.has(doc.id)) next.delete(doc.id);
                                else next.add(doc.id);
                                return next;
                              })
                            }
                          >
                            {expanded.has(doc.id) ? (
                              <ChevronDown size={16} />
                            ) : (
                              <ChevronRight size={16} />
                            )}
                          </button>
                          <span className="ta-file-symbol">
                            <FileText size={19} />
                          </span>
                          <button
                            className="ta-file-title"
                            onClick={() => setSelected(doc)}
                          >
                            <strong>{doc.title}</strong>
                            <small>
                              {documentLevel(doc)} ·{" "}
                              {countryLabel(doc.country ?? "unassigned")} · v
                              {doc.version} · {sizeLabel(doc.size)}
                            </small>
                          </button>
                        </div>
                      </td>
                      <td>
                        <DocumentStatus document={doc} />
                      </td>
                      <td>
                        <span
                          className={`ta-review-state ${doc.metadata?.review_status === "published" ? "reviewed" : ""}`}
                        >
                          {doc.metadata?.review_status === "published"
                            ? "已审核"
                            : "待审核"}
                        </span>
                      </td>
                      <td className="ta-date-cell">
                        {dateLabel(doc.created_at)}
                      </td>
                      <td>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setSelected(doc)}
                        >
                          查看拆解
                          <ChevronRight size={13} />
                        </Button>
                      </td>
                    </tr>
                    {expanded.has(doc.id) && (
                      <tr className="ta-children-row">
                        <td colSpan={5}>
                          <Attachments
                            parent={doc}
                            samples={samples}
                            revision={revision}
                            onSelect={setSelected}
                            onUpload={
                              samples
                                ? undefined
                                : () =>
                                    onUpload(
                                      scope || undefined,
                                      doc.id,
                                      doc.country ?? "unassigned",
                                    )
                            }
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            icon={<BookOpen size={30} />}
            title={search ? "无匹配资料" : "暂无资料"}
          >
            {search
              ? "更换关键词或清除筛选。"
              : "上传资料后查看正文与关联文件。"}
          </Empty>
        )}
        <Pagination
          offset={offset}
          limit={PAGE_SIZE}
          total={data.total}
          busy={loading}
          onChange={setOffset}
        />
      </div>
    </div>
  );
}
function sizeLabel(size: number) {
  return size >= 1024 * 1024
    ? `${(size / 1024 / 1024).toFixed(1)} MB`
    : `${(size / 1024).toFixed(1)} KB`;
}
function documentLevel(document: Document) {
  return document.role === "secondary"
    ? "附件"
    : document.legal_level === "secondary"
      ? "二级法规"
      : "一级资料";
}
function DocumentStatus({ document }: { document: Document }) {
  return (
    <span className={`ta-badge ta-document-status ${document.status}`}>
      <span className="ta-dot" />
      {document.status === "ready"
        ? document.legal_level === "secondary"
          ? "已解析"
          : `${document.chunk_count} 个检索片段`
        : document.status === "stored"
          ? "原件已保存"
          : document.status === "failed"
            ? "解析失败"
            : "处理中"}
    </span>
  );
}
function Pagination({
  offset,
  limit,
  total,
  busy,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  busy: boolean;
  onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  return (
    <div className="ta-pagination">
      <span>
        {total
          ? `第 ${offset + 1}–${Math.min(offset + limit, total)} 项，共 ${total} 项`
          : "暂无记录"}
      </span>
      <div>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}
function Attachments({
  parent,
  revision,
  onSelect,
  onUpload,
  samples = false,
}: {
  parent: Document;
  revision: number;
  onSelect: (doc: Document) => void;
  onUpload?: () => void;
  samples?: boolean;
}) {
  const [data, setData] = useState<ListResult<Document>>({
    items: [],
    total: 0,
  });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (samples) {
      const items = librarySamples
        .map((item) => item.document)
        .filter((item) => item.parent_id === parent.id);
      setData({ items: items.slice(offset, offset + 10), total: items.length });
      setLoading(false);
      return;
    }
    const abort = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      parent_id: parent.id,
      role: "secondary",
      limit: "10",
      offset: String(offset),
    });
    if (parent.case_id) params.set("case_id", parent.case_id);
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
  }, [parent.id, parent.case_id, offset, retry, revision, samples]);
  return (
    <div className="ta-secondary-list">
      <div className="ta-subheading">
        <h4>
          <Paperclip size={14} />
          附件
        </h4>
        {onUpload && (
          <Button variant="ghost" size="sm" onClick={onUpload}>
            添加关联附件
          </Button>
        )}
      </div>
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : loading ? (
        <Busy />
      ) : data.items.length ? (
        data.items.map((doc) => (
          <button
            key={doc.id}
            className="ta-secondary-file"
            onClick={() => onSelect(doc)}
          >
            <File size={16} />
            <strong>{doc.title}</strong>
            <span>原件 · {sizeLabel(doc.size)}</span>
            <ChevronRight size={14} />
          </button>
        ))
      ) : (
        <p className="ta-hint">尚未添加附件。</p>
      )}
      {data.total > 10 && (
        <Pagination
          offset={offset}
          limit={10}
          total={data.total}
          busy={loading}
          onChange={setOffset}
        />
      )}
    </div>
  );
}

function DocumentInspector({
  document,
  revision,
  onBack,
  onSelect,
  onUpload,
  onSaved,
  samples,
  ownerId,
}: {
  document: Document;
  revision: number;
  onBack: () => void;
  onSelect: (doc: Document) => void;
  onUpload: () => void;
  onSaved: (doc: Document) => void;
  samples: boolean;
  ownerId?: string;
}) {
  const [data, setData] = useState<DocumentDetail>();
  const [tab, setTab] = useState<
    "elements" | "chunks" | "attachments" | "versions" | "relations"
  >("elements");
  const [offsets, setOffsets] = useState({ elements: 0, chunks: 0 });
  const [selectedPart, setSelectedPart] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [parentError, setParentError] = useState("");
  const parentRequest = useRef<AbortController | null>(null);
  useEffect(() => () => parentRequest.current?.abort(), []);
  useEffect(() => {
    if (samples) {
      setData(librarySamples.find((item) => item.document.id === document.id));
      setLoading(false);
      return;
    }
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true);
    setError("");
    function read() {
      void teaRequest<DocumentDetail>(
        `/documents/${encodeURIComponent(document.id)}?element_offset=${offsets.elements}&chunk_offset=${offsets.chunks}&limit=${PART_SIZE}`,
        { signal: abort.signal },
      )
        .then((next) => {
          if (abort.signal.aborted) return;
          setData(next);
          if (
            next.document.status === "processing" ||
            (next.document.status === "stored" &&
              next.document.role === "primary")
          )
            timer = setTimeout(read, 2000);
        })
        .catch((e) => {
          if (!abort.signal.aborted) setError(errorMessage(e));
        })
        .finally(() => {
          if (!abort.signal.aborted) setLoading(false);
        });
    }
    read();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [document.id, offsets, retry, samples, revision]);
  const parts =
    tab === "chunks" ? (data?.chunks ?? []) : (data?.elements ?? []);
  const part = parts[selectedPart] ?? parts[0];
  const source = safeSourceUrl(document.source_url);
  const parsedDocument = data?.document ?? document;
  return (
    <div className="ta-document-detail">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft size={14} />
        返回资料目录
      </Button>
      <div className="ta-document-title">
        <span className="ta-file-symbol">
          <FileText size={25} />
        </span>
        <div>
          <div>
            <span className="ta-badge">{documentLevel(document)}</span>
            {!samples && (
              <span className="ta-badge">
                {countryLabel(document.country ?? "unassigned")}
              </span>
            )}
            <span className="ta-template-version">v{document.version}</span>
          </div>
          <h2>{document.title}</h2>
          <p>
            {document.filename} · {sizeLabel(document.size)} ·{" "}
            {dateLabel(document.created_at)}
          </p>
        </div>
        {!samples && (
          <Button asChild variant="outline" size="sm">
            <a href={downloadUrl(document.id)}>
              <ArrowDownToLine size={14} />
              下载原件
            </a>
          </Button>
        )}
      </div>
      {parsedDocument.error && <ErrorNotice message={parsedDocument.error} />}
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      )}
      {source && (
        <a
          className="ta-source-link"
          href={source}
          target="_blank"
          rel="noreferrer"
        >
          <Link2 size={13} />
          查看原始来源
        </a>
      )}
      {document.tags.length > 0 && (
        <div className="ta-collection-tags">
          {document.tags.map((tag) => (
            <span key={tag}>{tag}</span>
          ))}
        </div>
      )}
      {document.role === "secondary" ? (
        <div className="ta-secondary-detail">
          <Paperclip size={30} />
          <h3>关联原件</h3>
          <p>{samples ? "开发样例" : "仅保存原件"}</p>
          {document.parent_id && (
            <Button
              variant="outline"
              onClick={async () => {
                if (samples) {
                  const parent = librarySamples.find(
                    (item) => item.document.id === document.parent_id,
                  );
                  if (parent) onSelect(parent.document);
                  return;
                }
                parentRequest.current?.abort();
                const abort = new AbortController();
                parentRequest.current = abort;
                setParentError("");
                try {
                  const parent = await teaRequest<DocumentDetail>(
                    `/documents/${document.parent_id}`,
                    { signal: abort.signal },
                  );
                  if (!abort.signal.aborted) onSelect(parent.document);
                } catch (e) {
                  if (!abort.signal.aborted) setParentError(errorMessage(e));
                }
              }}
            >
              查看所属资料
            </Button>
          )}
          {parentError && <ErrorNotice message={parentError} />}
          {!samples && (
            <details className="tea-config-details">
              <summary>历史版本</summary>
              <DocumentHistory document={document} onSelect={onSelect} />
            </details>
          )}
        </div>
      ) : (
        <>
          <div className="ta-document-summary">
            <div>
              <small>解析状态</small>
              <DocumentStatus document={parsedDocument} />
            </div>
            <div>
              <small>结构元素</small>
              <strong>
                {data?.element_total ?? "—"}
                <span> 段</span>
              </strong>
            </div>
            <div>
              <small>
                {document.legal_level === "secondary" ? "文本片段" : "检索片段"}
              </small>
              <strong>
                {data?.chunk_total ?? "—"}
                <span> 段</span>
              </strong>
            </div>
            <div>
              <small>资料版本</small>
              <strong>v{document.version}</strong>
            </div>
          </div>
          {!samples && (
            <DocumentClassificationEditor
              document={document}
              ownerId={ownerId}
              onSaved={onSaved}
            />
          )}
          {!samples && (
            <DocumentMetadataEditor
              key={`${document.id}:${document.metadata_revision}`}
              document={document}
              onSaved={onSaved}
            />
          )}
          {data?.warnings?.map((warning, index) => (
            <div className="tea-note" key={index}>
              {warning}
            </div>
          ))}
          <div
            className="ta-document-tabs"
            role="tablist"
            aria-label="资料拆解视图"
          >
            {(
              [
                ["elements", "结构化拆解"],
                [
                  "chunks",
                  document.legal_level === "secondary"
                    ? "文本片段"
                    : "检索片段",
                ],
                ["attachments", "附件"],
                ["relations", "法规关联"],
                ["versions", "版本"],
              ] as const
            )
              .filter(
                ([id]) => !samples || (id !== "relations" && id !== "versions"),
              )
              .map(([id, name]) => (
                <button
                  key={id}
                  role="tab"
                  aria-selected={tab === id}
                  onClick={() => {
                    setTab(id);
                    setSelectedPart(0);
                  }}
                >
                  {name}
                </button>
              ))}
          </div>
          {tab === "attachments" ? (
            <Attachments
              parent={document}
              samples={samples}
              revision={revision}
              onSelect={onSelect}
              onUpload={samples ? undefined : onUpload}
            />
          ) : tab === "versions" ? (
            <DocumentHistory document={document} onSelect={onSelect} />
          ) : tab === "relations" ? (
            <DocumentRelations
              document={document}
              ownerId={ownerId}
              onSelect={onSelect}
            />
          ) : loading ? (
            <Busy label="读取结构化拆解…" />
          ) : (
            !error && (
              <>
                <div className="ta-structure-layout">
                  <aside aria-label="本页资料结构">
                    <div className="ta-subheading">
                      <h3>本页结构</h3>
                      <span>{parts.length} 段</span>
                    </div>
                    {parts.map((item, index) => (
                      <button
                        className={selectedPart === index ? "active" : ""}
                        key={item.element_key ?? item.id ?? index}
                        onClick={() => setSelectedPart(index)}
                      >
                        <span>
                          {String(offsets[tab] + index + 1).padStart(2, "0")}
                        </span>
                        <div>
                          <strong>
                            {item.section_path?.length
                              ? item.section_path.join(" / ")
                              : kindLabel(item.kind)}
                          </strong>
                          <small>
                            {(item.content_text ?? item.text ?? "").slice(
                              0,
                              50,
                            )}
                          </small>
                        </div>
                      </button>
                    ))}
                  </aside>
                  <div className="ta-structured-content">
                    {part ? (
                      <>
                        <div className="ta-part-heading">
                          <span className="ta-badge">
                            {tab === "chunks"
                              ? document.legal_level === "secondary"
                                ? "文本片段"
                                : "检索片段"
                              : kindLabel(part.kind)}
                          </span>
                          <span>{locatorLabel(part)}</span>
                        </div>
                        <h3>
                          {part.section_path?.length
                            ? part.section_path.join(" / ")
                            : "正文内容"}
                        </h3>
                        <p>
                          {part.content_text ?? part.text ?? "无可显示正文"}
                        </p>
                        <footer>
                          <FileText size={13} />
                          {document.title} · v{document.version} ·{" "}
                          {locatorLabel(part)}
                        </footer>
                      </>
                    ) : (
                      <Empty
                        icon={<Layers size={28} />}
                        title={
                          parsedDocument.status === "failed"
                            ? "解析失败"
                            : parsedDocument.status === "processing" ||
                                parsedDocument.status === "stored"
                              ? "解析中"
                              : "暂无正文"
                        }
                      >
                        {parsedDocument.status === "processing" ||
                        parsedDocument.status === "stored"
                          ? "原件已保存。"
                          : "可下载原件核对。"}
                      </Empty>
                    )}
                  </div>
                </div>
                <Pagination
                  offset={offsets[tab]}
                  limit={PART_SIZE}
                  total={
                    tab === "chunks"
                      ? (data?.chunk_total ?? 0)
                      : (data?.element_total ?? 0)
                  }
                  busy={loading}
                  onChange={(offset) => {
                    setOffsets((current) => ({ ...current, [tab]: offset }));
                    setSelectedPart(0);
                  }}
                />
              </>
            )
          )}
        </>
      )}
    </div>
  );
}
function kindLabel(kind?: string) {
  return (
    (
      {
        heading: "标题",
        paragraph: "正文段落",
        text: "正文段落",
        table: "表格",
        list: "列表",
        list_item: "列表项",
        image: "图片",
      } as Record<string, string>
    )[kind ?? ""] ?? "正文内容"
  );
}
function locatorLabel(part: DocumentPart) {
  const locator = part.locator ?? {};
  const page = locator.page ?? locator.page_number ?? locator.page_no;
  const start = locator.line_start;
  const end = locator.line_end;
  const values = [
    typeof page === "number" || typeof page === "string"
      ? `原文第 ${page} 页`
      : "",
    typeof start === "number"
      ? `第 ${start}${typeof end === "number" && end !== start ? `–${end}` : ""} 行`
      : "",
    typeof locator.paragraph_index === "number"
      ? `第 ${locator.paragraph_index + 1} 段`
      : "",
  ];
  return values.filter(Boolean).join(" · ") || "原文未提供页码或行号";
}
