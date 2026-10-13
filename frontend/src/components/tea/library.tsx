"use client";
import {
  ArrowDownToLine,
  BookOpen,
  FileText,
  Link2,
  Search,
  Upload,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  downloadUrl,
  errorMessage,
  post,
  safeSourceUrl,
  teaRequest,
} from "@/core/tea/api";
import { countryLabel, useCollectionCountries } from "@/core/tea/countries";
import {
  type CaseSummary,
  type Citation,
  type Document,
  type DocumentDetail,
  type ListResult,
  type SearchResult,
} from "@/core/tea/types";

import { DocumentMetadataEditor } from "./document-metadata";
import {
  ActionButton,
  Busy,
  dateLabel,
  Empty,
  ErrorNotice,
  SectionHeading,
} from "./shared";

export function CitationCard({ citation }: { citation: Citation }) {
  const url = safeSourceUrl(citation.source_url);
  const locator = citation.locator ?? {};
  const page = locator.page ?? locator.page_number ?? locator.page_no;
  const lineStart = locator.line_start;
  const lineEnd = locator.line_end;
  const section = locator.section_path ?? locator.section;
  const readable = (value: unknown) =>
    typeof value === "string" || typeof value === "number" ? String(value) : "";
  const locations = [
    readable(page) ? `第 ${readable(page)} 页` : "",
    readable(lineStart)
      ? `第 ${readable(lineStart)}${readable(lineEnd) && lineEnd !== lineStart ? `–${readable(lineEnd)}` : ""} 行`
      : "",
    Array.isArray(section)
      ? section.filter((value) => typeof value === "string").join(" / ")
      : readable(section),
  ].filter(Boolean);
  const kinds = {
    law: "法律法规",
    guidance: "办事指南",
    case: "案例",
    business: "业务材料",
    demo: "演示资料",
  };
  const kind = citation.metadata?.kind;
  return (
    <div className="tea-citation">
      <div>
        <BookOpen size={16} />
        <strong>{citation.title}</strong>
        {citation.document_version && <span>v{citation.document_version}</span>}
      </div>
      <small>
        {kind ? kinds[kind] : "参考资料"}
        {locations.length ? ` · ${locations.join(" · ")}` : ""}
      </small>
      <p>{citation.excerpt}</p>
      {citation.metadata && (
        <small>
          {citation.metadata.effective_from || citation.metadata.effective_to
            ? `适用日期：${citation.metadata.effective_from ?? "起始日期待核对"} 至 ${citation.metadata.effective_to ?? "结束日期未注明"}`
            : kind === "law"
              ? "生效日期尚未记录，请核对原文"
              : null}
        </small>
      )}
      <footer>
        <a href={downloadUrl(citation.document_id)}>
          <ArrowDownToLine size={13} />
          原始文件
        </a>
        {url && (
          <a href={url} target="_blank" rel="noreferrer">
            <Link2 size={13} />
            来源网站
          </a>
        )}
      </footer>
    </div>
  );
}

export function UploadDialog({
  open,
  onOpenChange,
  caseId,
  initialParentId,
  initialCountry,
  cases,
  onDone,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  caseId?: string;
  initialParentId?: string;
  initialCountry?: string;
  cases: CaseSummary[];
  onDone: () => void;
}) {
  const [scope, setScope] = useState(caseId ?? "");
  const [role, setRole] = useState("primary");
  const [parent, setParent] = useState("");
  const [tags, setTags] = useState("");
  const [docs, setDocs] = useState<Document[]>([]);
  const [attachmentCountry, setAttachmentCountry] = useState(
    initialCountry ?? "",
  );
  const countries = useCollectionCountries(
    undefined,
    open && role === "secondary" && !scope && !initialCountry,
  );
  const parentCountry = attachmentCountry || countries.defaultCountry;
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  useEffect(() => {
    if (!open) return;
    setScope(caseId ?? "");
    setRole(initialParentId ? "secondary" : "primary");
    setAttachmentCountry(initialCountry ?? "");
    setFile(null);
    setError("");
  }, [open, caseId, initialParentId, initialCountry]);
  useEffect(() => {
    if (
      !open ||
      role !== "secondary" ||
      (!scope && !initialCountry && !countries.ready)
    )
      return;
    const controller = new AbortController();
    setParent(
      scope === (caseId ?? "") &&
        (!initialCountry || parentCountry === initialCountry)
        ? (initialParentId ?? "")
        : "",
    );
    setDocs([]);
    async function readParents() {
      const original =
        initialParentId && scope === (caseId ?? "")
          ? (
              await teaRequest<DocumentDetail>(
                `/documents/${encodeURIComponent(initialParentId)}?limit=1`,
                { signal: controller.signal },
              )
            ).document
          : undefined;
      if (controller.signal.aborted) return;
      const params = new URLSearchParams({
        role: "primary",
        limit: "100",
        offset: "0",
      });
      if (scope) params.set("case_id", scope);
      else
        params.set(
          "country",
          original ? (original.country ?? "unassigned") : parentCountry,
        );
      const result = await teaRequest<ListResult<Document>>(
        `/documents?${params}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const options = result.items.filter(
        (document) =>
          document.role === "primary" && document.case_id === (scope || null),
      );
      setDocs(
        original && !options.some((document) => document.id === original.id)
          ? [original, ...options]
          : options,
      );
    }
    void readParents().catch((e) => {
      if (!controller.signal.aborted) setError(errorMessage(e));
    });
    return () => controller.abort();
  }, [
    scope,
    open,
    role,
    initialParentId,
    initialCountry,
    caseId,
    parentCountry,
    countries.ready,
  ]);
  async function upload() {
    if (!file || submitting.current) return;
    if (file.size > 20 * 1024 * 1024) {
      setError("文件不能超过 20 MB。");
      return;
    }
    submitting.current = true;
    setBusy(true);
    setError("");
    const data = new FormData();
    data.append("file", file);
    data.append("role", role);
    data.append(
      "tags",
      JSON.stringify(
        tags
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
      ),
    );
    if (scope) data.append("case_id", scope);
    if (role === "secondary") data.append("parent_id", parent);
    try {
      await teaRequest<Document>("/documents", { method: "POST", body: data });
      onDone();
      onOpenChange(false);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }
  return (
    <Dialog open={open} onOpenChange={busy ? undefined : onOpenChange}>
      <DialogContent className="tea-theme tea-dialog">
        <DialogHeader>
          <DialogTitle>上传资料</DialogTitle>
          <DialogDescription>
            主文件解析正文；关联附件保存原件。
          </DialogDescription>
        </DialogHeader>
        <label className="tea-field">
          <span>资料归属</span>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            disabled={busy}
          >
            <option value="">公共资料库 · 当前账号</option>
            {cases.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
        <div className="tea-form-row">
          <label className="tea-field">
            <span>文件用途</span>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              disabled={busy}
            >
              <option value="primary">主文件 · 解析与检索</option>
              <option value="secondary">关联附件 · 仅保存</option>
            </select>
          </label>
          <label className="tea-field">
            <span>标签</span>
            <Input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="用逗号分隔"
              disabled={busy}
            />
          </label>
        </div>
        {role === "secondary" && !scope && !initialParentId && (
          <label className="tea-field">
            <span>主文件国家</span>
            <select
              value={countries.ready ? parentCountry : ""}
              disabled={busy || !countries.ready}
              onChange={(event) => setAttachmentCountry(event.target.value)}
            >
              {!countries.ready && <option value="">读取国家…</option>}
              {countries.countries.map((code) => (
                <option key={code} value={code}>
                  {countryLabel(code)}
                </option>
              ))}
              <option value="unassigned">未分国</option>
            </select>
          </label>
        )}
        {role === "secondary" && countries.error && (
          <ErrorNotice message={countries.error} onRetry={countries.retry} />
        )}
        {role === "secondary" && (
          <label className="tea-field">
            <span>关联到主文件</span>
            <select value={parent} onChange={(e) => setParent(e.target.value)}>
              <option value="">选择主文件</option>
              {docs.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="tea-file-drop">
          <Upload size={26} />
          <strong>{file ? file.name : "选择一份资料"}</strong>
          <span>PDF、Word、HTML、TXT / Markdown · 最大 20 MB</span>
          <input
            type="file"
            disabled={busy}
            accept=".pdf,.docx,.html,.htm,.txt,.md"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        {error && <ErrorNotice message={error} />}
        <ActionButton
          busy={busy}
          disabled={!file || (role === "secondary" && !parent)}
          onClick={upload}
        >
          上传并保存
        </ActionButton>
      </DialogContent>
    </Dialog>
  );
}

export function Library({
  onUpload,
  revision,
  cases,
  initialCaseId,
  scopeLocked = false,
  ownerId,
}: {
  onUpload: (caseId?: string) => void;
  revision: number;
  cases: CaseSummary[];
  initialCaseId?: string;
  scopeLocked?: boolean;
  ownerId?: string;
}) {
  const [scope, setScope] = useState(initialCaseId ?? "");
  const countries = useCollectionCountries(ownerId, !scope);
  const [countrySelection, setCountrySelection] = useState("");
  const country = countrySelection || countries.defaultCountry;
  const [docs, setDocs] = useState<Document[]>([]);
  const [total, setTotal] = useState(0);
  const [moreBusy, setMoreBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Citation[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<Document>();
  const [reload, setReload] = useState(0);
  const requests = useRef<{ search?: AbortController; more?: AbortController }>(
    {},
  );
  const scopeKey = `${ownerId ?? "session"}:${scope}:${country}`;
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;
  function resetReads() {
    requests.current.search?.abort();
    requests.current.more?.abort();
    requests.current = {};
    setHits(null);
    setSearching(false);
    setMoreBusy(false);
    setSelected(undefined);
  }
  useEffect(() => {
    const active = requests.current;
    return () => {
      active.search?.abort();
      active.more?.abort();
    };
  }, [scope, country, ownerId]);
  useEffect(() => {
    if (!countries.ready) return;
    const controller = new AbortController();
    setLoading(true);
    const params = new URLSearchParams({ limit: "100", offset: "0" });
    if (scope) params.set("case_id", scope);
    else params.set("country", country);
    teaRequest<ListResult<Document>>(`/documents?${params}`, {
      signal: controller.signal,
    })
      .then((data) => {
        if (controller.signal.aborted) return;
        setDocs(data.items);
        setTotal(data.total);
        setError("");
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision, reload, scope, country, countries.ready, ownerId]);
  async function loadMore() {
    if (requests.current.more || !countries.ready) return;
    const abort = new AbortController();
    const requestedScope = scopeKey;
    requests.current.more = abort;
    setMoreBusy(true);
    try {
      const params = new URLSearchParams({
        limit: "100",
        offset: String(docs.length),
      });
      if (scope) params.set("case_id", scope);
      else params.set("country", country);
      const data = await teaRequest<ListResult<Document>>(
        `/documents?${params}`,
        { signal: abort.signal },
      );
      if (abort.signal.aborted || currentScope.current !== requestedScope)
        return;
      setDocs((current) => [
        ...new Map(
          [...current, ...data.items].map((doc) => [doc.id, doc]),
        ).values(),
      ]);
      setTotal(data.total);
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      if (requests.current.more === abort) requests.current.more = undefined;
      if (!abort.signal.aborted) setMoreBusy(false);
    }
  }
  async function search() {
    if (!query.trim() || requests.current.search || !countries.ready) return;
    const abort = new AbortController();
    const requestedScope = scopeKey;
    requests.current.search = abort;
    setSearching(true);
    setError("");
    try {
      const data = await post<SearchResult>(
        "/search",
        { query, limit: 8, ...(scope ? { case_id: scope } : { country }) },
        abort.signal,
      );
      if (!abort.signal.aborted && currentScope.current === requestedScope)
        setHits(data.items);
    } catch (e) {
      if (!abort.signal.aborted) setError(errorMessage(e));
    } finally {
      if (requests.current.search === abort)
        requests.current.search = undefined;
      if (!abort.signal.aborted) setSearching(false);
    }
  }
  return (
    <div className="tea-page tea-library">
      <SectionHeading title={scopeLocked && !scope ? "公共资料库" : "资料库"}>
        <Button onClick={() => onUpload(scope || undefined)}>
          <Upload size={15} />
          上传资料
        </Button>
      </SectionHeading>
      {!scopeLocked && (
        <label className="tea-field">
          <span>资料范围</span>
          <select
            value={scope}
            onChange={(e) => {
              resetReads();
              setScope(e.target.value);
              setQuery("");
            }}
          >
            <option value="">账号公共资料库</option>
            {cases.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
      )}
      {(!scopeLocked || !scope) && (
        <label className="tea-field">
          <span>资料国家</span>
          <select
            aria-label="资料国家"
            value={scope ? "case" : countries.ready ? country : ""}
            disabled={!!scope || !countries.ready}
            onChange={(event) => {
              resetReads();
              setCountrySelection(event.target.value);
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
        </label>
      )}
      <form
        className="tea-search-bar"
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <Search size={18} />
        <Input
          aria-label="搜索资料"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            if (!e.target.value) setHits(null);
          }}
          placeholder="搜索法规条款、材料内容或关键词…"
        />
        <ActionButton
          busy={searching}
          type="submit"
          disabled={!query.trim() || !countries.ready}
        >
          检索
        </ActionButton>
      </form>
      {countries.error && (
        <ErrorNotice message={countries.error} onRetry={countries.retry} />
      )}
      {error && (
        <ErrorNotice message={error} onRetry={() => setReload((r) => r + 1)} />
      )}
      {loading ? (
        <Busy />
      ) : hits ? (
        <div className="tea-search-results">
          <div className="tea-list-label">
            找到 {hits.length} 条相关内容
            <Button
              variant="ghost"
              onClick={() => {
                setHits(null);
                setQuery("");
              }}
            >
              返回文件列表
            </Button>
          </div>
          {hits.length ? (
            hits.map((h, i) => (
              <CitationCard citation={h} key={`${h.document_id}-${i}`} />
            ))
          ) : (
            <Empty title="没有找到相关依据">
              试试更准确的条款名或关键词，或上传相关资料。
            </Empty>
          )}
        </div>
      ) : (
        <>
          <div className="tea-list-label">
            资料目录 <span>{total} 份文件</span>
          </div>
          {docs.length ? (
            <div className="tea-document-list">
              {docs.map((doc) => (
                <button
                  className="tea-file-row"
                  key={doc.id}
                  onClick={() => setSelected(doc)}
                >
                  <span className="tea-file-icon">
                    <FileText size={21} />
                  </span>
                  <div>
                    <strong>{doc.title || doc.filename}</strong>
                    <p>
                      {doc.role === "primary" ? "主文件" : "关联附件"} · v
                      {doc.version} · {dateLabel(doc.created_at)}
                    </p>
                  </div>
                  <span
                    className={`tea-status ${doc.status === "ready" ? "completed" : doc.status === "failed" ? "needs_input" : "pending"}`}
                  >
                    {doc.status === "ready"
                      ? `${doc.chunk_count} 个片段`
                      : doc.status === "stored"
                        ? "原文已保存"
                        : doc.status === "failed"
                          ? "解析失败"
                          : "处理中"}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <Empty icon={<BookOpen size={32} />} title="资料库准备好了">
              上传第一份资料，让后续问答有据可查。
            </Empty>
          )}
        </>
      )}
      {hits === null && docs.length < total && (
        <ActionButton variant="outline" busy={moreBusy} onClick={loadMore}>
          加载更多资料（已显示 {docs.length} / {total}）
        </ActionButton>
      )}
      <Dialog
        open={!!selected}
        onOpenChange={(open) => {
          if (!open) setSelected(undefined);
        }}
      >
        <DialogContent className="tea-theme tea-dialog tea-document-dialog">
          <DialogHeader>
            <DialogTitle>{selected?.title}</DialogTitle>
            <DialogDescription>
              原始文件与解析内容保留版本关联。
            </DialogDescription>
          </DialogHeader>
          {selected && (
            <>
              <div className="tea-document-meta">
                <span>版本 {selected.version}</span>
                <span>{(selected.size / 1024).toFixed(1)} KB</span>
                <Button asChild size="sm" variant="outline">
                  <a href={downloadUrl(selected.id)}>
                    <ArrowDownToLine size={14} />
                    下载原文
                  </a>
                </Button>
              </div>
              {selected.error && <ErrorNotice message={selected.error} />}
              <DocumentMetadataEditor
                key={`${selected.id}-${selected.metadata_revision}`}
                document={selected}
                onSaved={(next) => {
                  setSelected(next);
                  setDocs((current) =>
                    current.map((doc) => (doc.id === next.id ? next : doc)),
                  );
                }}
              />
              {selected.role === "secondary" ? (
                <div className="tea-note">
                  该文件为关联附件，仅保存原文，不参与解析或检索。
                </div>
              ) : (
                <DocumentExcerpts
                  key={`${selected.id}:${selected.version}`}
                  documentId={selected.id}
                />
              )}
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

const DOCUMENT_PAGE_SIZE = 100;

function DocumentExcerpts({ documentId }: { documentId: string }) {
  const [page, setPage] = useState<{ offset: number; data: DocumentDetail }>();
  const [request, setRequest] = useState({ offset: 0, attempt: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<DocumentDetail>(
      `/documents/${encodeURIComponent(documentId)}?element_offset=${request.offset}&chunk_offset=0&limit=${DOCUMENT_PAGE_SIZE}`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (!controller.signal.aborted)
          setPage({ offset: request.offset, data });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [documentId, request]);
  return (
    <>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() =>
            setRequest((current) => ({
              ...current,
              attempt: current.attempt + 1,
            }))
          }
        />
      )}
      {loading && <Busy label="读取解析内容…" />}
      {page && (
        <>
          <div className="tea-list-label" role="status">
            {page.data.elements.length
              ? `第 ${page.offset + 1}–${page.offset + page.data.elements.length} 段 / 共 ${page.data.element_total} 段`
              : `共 ${page.data.element_total} 段`}
          </div>
          <div
            className="tea-document-excerpts"
            key={page.offset}
            aria-busy={loading}
          >
            {page.data.elements.map((element, i) => (
              <article key={element.element_key ?? element.id ?? i}>
                <small>
                  {Array.isArray(element.section_path)
                    ? element.section_path.join(" / ")
                    : typeof element.kind === "string"
                      ? element.kind
                      : "正文"}
                </small>
                <p>
                  {typeof element.content_text === "string"
                    ? element.content_text
                    : typeof element.text === "string"
                      ? element.text
                      : ""}
                </p>
              </article>
            ))}
            {!page.data.elements.length && (
              <Empty title="暂无解析内容">可下载原始文件查看。</Empty>
            )}
          </div>
          {page.data.element_total > DOCUMENT_PAGE_SIZE && (
            <div className="tea-form-row">
              <Button
                variant="outline"
                size="sm"
                disabled={loading || page.offset === 0}
                onClick={() =>
                  setRequest({
                    offset: Math.max(0, page.offset - DOCUMENT_PAGE_SIZE),
                    attempt: 0,
                  })
                }
              >
                上一页正文
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={
                  loading ||
                  page.offset + page.data.elements.length >=
                    page.data.element_total
                }
                onClick={() =>
                  setRequest({
                    offset: page.offset + DOCUMENT_PAGE_SIZE,
                    attempt: 0,
                  })
                }
              >
                下一页正文
              </Button>
            </div>
          )}
        </>
      )}
    </>
  );
}
