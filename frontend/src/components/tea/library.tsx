"use client";
import {
  ArrowDownToLine,
  ArrowRight,
  AlertCircle,
  BookOpen,
  Check,
  ChevronRight,
  FileText,
  Link2,
  Loader2,
  RefreshCw,
  Search,
  Upload,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

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
  safeSourceUrl,
  searchDocuments,
  teaRequest,
} from "@/core/tea/api";
import { countryLabel, useCollectionCountries } from "@/core/tea/countries";
import {
  documentSize,
  documentState,
  mergeProcessingDocument,
} from "@/core/tea/document-state";
import {
  type CaseSummary,
  type Citation,
  type Document,
  type ListResult,
} from "@/core/tea/types";
import { useDocumentProcessing } from "@/core/tea/use-document-processing";

import { DocumentPreview } from "./document-preview";
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
  cases,
  onDone,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  caseId?: string;
  cases: CaseSummary[];
  onDone: (document: Document) => void;
}) {
  const [scope, setScope] = useState(caseId ?? "");
  const [tags, setTags] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const uploadRequest = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => uploadRequest.current?.abort(), []);
  useEffect(() => {
    if (!open) return;
    setScope(caseId ?? "");
    setFile(null);
    setTags("");
    setError("");
    setDragging(false);
  }, [open, caseId]);
  function chooseFile(next: File | null) {
    if (busy) return;
    setError("");
    setFile(null);
    if (next && !next.size) {
      setError("文件为空，请重新选择。");
      return;
    }
    if (next && next.size > 20 * 1024 * 1024) {
      setError("文件不能超过 20 MB。");
      return;
    }
    if (next && !/\.(pdf|docx|html?|txt|md)$/i.test(next.name)) {
      setError("请选择 PDF、Word、HTML 或文本文件。");
      return;
    }
    setFile(next);
  }
  async function upload() {
    if (!file || submitting.current) return;
    if (file.size > 20 * 1024 * 1024) {
      setError("文件不能超过 20 MB。");
      return;
    }
    const controller = new AbortController();
    uploadRequest.current = controller;
    submitting.current = true;
    setBusy(true);
    setError("");
    const data = new FormData();
    data.append("file", file);
    data.append("role", "primary");
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
    try {
      const document = await teaRequest<Document>("/documents", {
        method: "POST",
        body: data,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      onDone(document);
      onOpenChange(false);
    } catch (e) {
      if (!controller.signal.aborted) setError(errorMessage(e));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      submitting.current = false;
    }
  }
  return (
    <Dialog open={open} onOpenChange={busy ? undefined : onOpenChange}>
      <DialogContent className="tea-theme tea-dialog">
        <DialogHeader>
          <DialogTitle>上传资料</DialogTitle>
          <DialogDescription className="sr-only">
            上传文件并保存到所选资料范围。
          </DialogDescription>
        </DialogHeader>
        <label className="tea-field">
          <span>资料归属</span>
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            disabled={busy}
          >
            <option value="">我的资料</option>
            {cases.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
        <div>
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
        <label
          className={`tea-file-drop tea-upload-drop ${dragging ? "is-dragging" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            if (!busy) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (busy) return;
            if (event.dataTransfer.files.length > 1) {
              setFile(null);
              setError("请一次上传一份资料。");
              return;
            }
            chooseFile(event.dataTransfer.files[0] ?? null);
          }}
        >
          {file ? <FileText size={26} /> : <Upload size={26} />}
          <strong>{file ? file.name : "点击选择或拖入文件"}</strong>
          <span>
            {file
              ? `${documentSize(file.size)} · 点击更换`
              : "PDF、Word、HTML、TXT / Markdown · 最大 20 MB"}
          </span>
          <input
            className="sr-only"
            aria-label="选择上传文件"
            type="file"
            disabled={busy}
            accept=".pdf,.docx,.html,.htm,.txt,.md"
            onChange={(e) => {
              chooseFile(e.target.files?.[0] ?? null);
              e.target.value = "";
            }}
          />
        </label>
        {error && <ErrorNotice message={error} />}
        <ActionButton busy={busy} disabled={!file} onClick={upload}>
          {busy ? "正在上传并处理…" : "上传并保存"}
        </ActionButton>
      </DialogContent>
    </Dialog>
  );
}

export function Library(props: LibraryProps) {
  return (
    <LibraryWorkspace
      key={`${props.ownerId ?? "session"}:${props.initialCaseId ?? "library"}`}
      {...props}
    />
  );
}

interface LibraryProps {
  onUpload: (caseId?: string) => void;
  revision: number;
  cases: CaseSummary[];
  initialCaseId?: string;
  scopeLocked?: boolean;
  businessOnly?: boolean;
  ownerId?: string;
  uploadedDocument?: Document;
  active?: boolean;
  onDocumentsChange?: (documents: Document[]) => void;
}

function LibraryWorkspace({
  onUpload,
  revision,
  cases,
  initialCaseId,
  scopeLocked = false,
  businessOnly = false,
  ownerId,
  uploadedDocument,
  active = true,
  onDocumentsChange,
}: LibraryProps) {
  const [scope, setScope] = useState(initialCaseId ?? "");
  const [sourceScope, setSourceScope] = useState<"personal" | "system">(
    initialCaseId ? "personal" : "system",
  );
  const countries = useCollectionCountries(ownerId, !scope);
  const [countrySelection, setCountrySelection] = useState("");
  const country =
    countrySelection ||
    (sourceScope === "personal" ? "all" : countries.defaultCountry);
  const [docs, setDocs] = useState<Document[]>([]);
  const [total, setTotal] = useState(0);
  const [moreBusy, setMoreBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searchedQuery, setSearchedQuery] = useState("");
  const [searchError, setSearchError] = useState("");
  const [nameQuery, setNameQuery] = useState("");
  const [searchMode, setSearchMode] = useState("name");
  const [hits, setHits] = useState<Citation[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<Document>();
  const [receipt, setReceipt] = useState<Document>();
  const receiptId = receipt?.id;
  const receiptStatus = receipt?.status;
  const receiptError = receipt?.error;
  useEffect(() => {
    if (
      !receiptId ||
      receiptError ||
      !["ready", "stored"].includes(receiptStatus ?? "")
    )
      return;
    const timer = setTimeout(() => setReceipt(undefined), 5000);
    return () => clearTimeout(timer);
  }, [receiptId, receiptStatus, receiptError]);
  const [reload, setReload] = useState(0);
  const requests = useRef<{ search?: AbortController; more?: AbortController }>(
    {},
  );
  const scopeKey = `${ownerId ?? "session"}:${scope}:${sourceScope}:${country}:${nameQuery}`;
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;
  const notifyDocumentsChange = useRef(onDocumentsChange);
  notifyDocumentsChange.current = onDocumentsChange;
  const resetReads = useCallback(() => {
    requests.current.search?.abort();
    requests.current.more?.abort();
    requests.current = {};
    setHits(null);
    setSearchedQuery("");
    setSearchError("");
    setSearching(false);
    setMoreBusy(false);
    setSelected(undefined);
    setReceipt(undefined);
    setDocs([]);
    setTotal(0);
    setError("");
    setLoading(true);
  }, []);
  useEffect(() => {
    const active = requests.current;
    return () => {
      active.search?.abort();
      active.more?.abort();
    };
  }, [scope, country, sourceScope, nameQuery, ownerId]);
  const updateDocuments = useCallback((updates: Document[]) => {
    const byId = new Map(updates.map((item) => [item.id, item]));
    const update = (current: Document) => {
      const next = byId.get(current.id);
      return next ? mergeProcessingDocument(current, next) : current;
    };
    setDocs((current) => current.map(update));
    setSelected((current) => current && update(current));
    setReceipt((current) => current && update(current));
    if (updates.length) notifyDocumentsChange.current?.(updates);
  }, []);
  const processing = useDocumentProcessing({
    documents: [
      ...docs,
      ...(selected ? [selected] : []),
      ...(receipt ? [receipt] : []),
    ],
    boundary: scopeKey,
    active,
    onUpdate: updateDocuments,
  });
  useEffect(() => {
    if (
      !uploadedDocument ||
      (scopeLocked &&
        (uploadedDocument.case_id ?? "") !== (initialCaseId ?? ""))
    )
      return;
    resetReads();
    setScope(uploadedDocument.case_id ?? "");
    setSourceScope("personal");
    setCountrySelection("all");
    setQuery("");
    setNameQuery("");
    setReceipt(uploadedDocument);
  }, [uploadedDocument, initialCaseId, scopeLocked, resetReads]);
  useEffect(() => {
    if (!countries.ready) return;
    const controller = new AbortController();
    setLoading(true);
    const params = new URLSearchParams({
      limit: "100",
      offset: "0",
      source_scope: sourceScope,
    });
    if (scope) params.set("case_id", scope);
    else if (country !== "all") params.set("country", country);
    if (nameQuery) params.set("q", nameQuery);
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
  }, [
    revision,
    reload,
    scope,
    country,
    sourceScope,
    nameQuery,
    countries.ready,
    ownerId,
  ]);
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
        source_scope: sourceScope,
      });
      if (scope) params.set("case_id", scope);
      else if (country !== "all") params.set("country", country);
      if (nameQuery) params.set("q", nameQuery);
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
    if (
      !query.trim() ||
      requests.current.search ||
      !countries.ready ||
      (!scope && country === "all")
    )
      return;
    const abort = new AbortController();
    const requestedScope = scopeKey;
    requests.current.search = abort;
    setSearching(true);
    setSearchError("");
    try {
      const data = await searchDocuments(
        {
          query,
          limit: 8,
          source_scope: sourceScope,
          ...(scope ? { case_id: scope } : { country }),
        },
        abort.signal,
      );
      if (!abort.signal.aborted && currentScope.current === requestedScope) {
        setHits(data.items);
        setSearchedQuery(query.trim());
      }
    } catch (e) {
      if (!abort.signal.aborted) setSearchError(errorMessage(e));
    } finally {
      if (requests.current.search === abort)
        requests.current.search = undefined;
      if (!abort.signal.aborted) setSearching(false);
    }
  }
  return (
    <div className="tea-page tea-library">
      <SectionHeading title={scope ? "业务资料" : "资料库"}>
        <Button size="sm" onClick={() => onUpload(scope || undefined)}>
          <Upload size={15} />
          上传资料
        </Button>
      </SectionHeading>
      {!businessOnly && (
        <div className="tea-form-row" role="group" aria-label="资料来源">
          <Button
            variant={sourceScope === "system" ? "default" : "outline"}
            aria-pressed={sourceScope === "system"}
            onClick={() => {
              if (sourceScope === "system") return;
              resetReads();
              setSourceScope("system");
              setCountrySelection("");
              setQuery("");
              setNameQuery("");
            }}
          >
            {scope ? "对应国家法规" : "法规总库"}
          </Button>
          <Button
            variant={sourceScope === "personal" ? "default" : "outline"}
            aria-pressed={sourceScope === "personal"}
            onClick={() => {
              if (sourceScope === "personal") return;
              resetReads();
              setSourceScope("personal");
              setCountrySelection("");
              setQuery("");
              setNameQuery("");
            }}
          >
            {scope ? "本业务资料" : "我的资料"}
          </Button>
        </div>
      )}
      {!scopeLocked && (
        <label className="tea-field">
          <span>资料范围</span>
          <select
            value={scope}
            onChange={(e) => {
              resetReads();
              setScope(e.target.value);
              setQuery("");
              setNameQuery("");
              setCountrySelection("");
            }}
          >
            <option value="">
              {sourceScope === "system" ? "全部法规" : "账号全部资料"}
            </option>
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
              setQuery("");
              setNameQuery("");
            }}
          >
            {!!scope && <option value="case">按业务目的地</option>}
            {sourceScope === "personal" && !scope && (
              <option value="all">全部国家（含未分国）</option>
            )}
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
        className="tea-library-search"
        onSubmit={(e) => {
          e.preventDefault();
          if (searchMode === "content") {
            void search();
            return;
          }
          resetReads();
          setNameQuery(query.trim());
          setReload((value) => value + 1);
        }}
      >
        <Search size={18} />
        <Input
          aria-label="搜索资料"
          value={query}
          onChange={(e) => {
            requests.current.search?.abort();
            requests.current.search = undefined;
            setSearching(false);
            setSearchError("");
            setQuery(e.target.value);
            setHits(null);
          }}
          maxLength={200}
          placeholder={
            searchMode === "name" ? "搜索文件名称" : "搜索正文关键词"
          }
        />
        <select
          aria-label="搜索方式"
          value={searchMode}
          onChange={(event) => {
            requests.current.search?.abort();
            requests.current.search = undefined;
            setSearching(false);
            setSearchError("");
            setHits(null);
            setSearchMode(event.target.value);
          }}
        >
          <option value="name">文件名</option>
          <option value="content">正文</option>
        </select>
        <ActionButton
          busy={searching}
          size="icon"
          variant="ghost"
          aria-label={searchMode === "name" ? "查找文件" : "检索"}
          title={searchMode === "name" ? "查找文件" : "检索正文"}
          type="submit"
          disabled={
            !countries.ready ||
            (searchMode === "content" &&
              (!query.trim() || (!scope && country === "all")))
          }
        >
          {!searching && <ArrowRight size={16} />}
        </ActionButton>
      </form>
      {!scope && country === "all" && searchMode === "content" && (
        <p className="tea-library-hint">检索正文请先选择国家。</p>
      )}
      {receipt && (
        <div
          className={`tea-upload-receipt ${documentState(receipt).tone}`}
          role="status"
        >
          <DocumentStatusIcon document={receipt} />
          <div>
            <strong>{receipt.title || receipt.filename}</strong>
            <span>{documentState(receipt).label}</span>
          </div>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setSelected(receipt)}
          >
            查看
          </Button>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭上传提示"
            onClick={() => setReceipt(undefined)}
          >
            <X size={14} />
          </Button>
        </div>
      )}
      {processing.error && (
        <ErrorNotice message={processing.error} onRetry={processing.retry} />
      )}
      {countries.error && (
        <ErrorNotice message={countries.error} onRetry={countries.retry} />
      )}
      {error && (
        <ErrorNotice message={error} onRetry={() => setReload((r) => r + 1)} />
      )}
      {searchError && (
        <ErrorNotice message={searchError} onRetry={() => void search()} />
      )}
      {loading ? (
        <Busy />
      ) : error && !docs.length ? null : hits ? (
        <div className="tea-search-results">
          <div className="tea-list-label">
            {searchedQuery} · {hits.length} 条结果
            <Button
              variant="ghost"
              onClick={() => {
                setHits(null);
                setQuery("");
                setNameQuery("");
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
          <div className="tea-library-list-heading">
            <span>{total} 份资料</span>
            <Button
              size="icon"
              variant="ghost"
              aria-label="刷新资料"
              title="刷新资料"
              onClick={() => {
                setReload((value) => value + 1);
                processing.retry();
              }}
            >
              <RefreshCw size={14} />
            </Button>
          </div>
          {docs.length ? (
            <div className="tea-document-list">
              {docs.map((doc) => (
                <button
                  className={`tea-file-row tea-library-file ${receipt?.id === doc.id ? "is-recent" : ""}`}
                  key={doc.id}
                  onClick={() => setSelected(doc)}
                >
                  <span className="tea-file-icon">
                    <FileText size={21} />
                  </span>
                  <div>
                    <strong title={doc.title || doc.filename}>
                      {doc.title || doc.filename}
                    </strong>
                    <p>
                      <span>
                        {doc.filename.split(".").pop()?.toUpperCase()} ·{" "}
                        {documentSize(doc.size)}
                      </span>
                      <span>{dateLabel(doc.created_at)}</span>
                      {!scope && doc.case_id && (
                        <span>
                          {cases.find((item) => item.id === doc.case_id)
                            ?.title ?? "业务资料"}
                        </span>
                      )}
                    </p>
                    <span
                      className={`tea-document-state ${documentState(doc).tone}`}
                    >
                      <DocumentStatusIcon document={doc} />
                      {documentState(doc).label}
                    </span>
                  </div>
                  <ChevronRight size={15} className="tea-file-chevron" />
                </button>
              ))}
            </div>
          ) : (
            <Empty
              icon={<BookOpen size={32} />}
              title={nameQuery ? "没有找到匹配文件" : "暂无资料"}
            >
              {nameQuery ? (
                "调整资料名称或国家筛选后重试。"
              ) : sourceScope === "system" ? (
                "可切换国家查看。"
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onUpload(scope || undefined)}
                >
                  <Upload size={14} />
                  上传资料
                </Button>
              )}
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
            <DialogDescription className="sr-only">
              资料内容预览
            </DialogDescription>
          </DialogHeader>
          {selected && (
            <DocumentPreview
              key={`${ownerId}:${selected.id}`}
              document={selected}
              processing={processing}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function DocumentStatusIcon({ document }: { document: Document }) {
  const state = documentState(document);
  if (state.tone === "processing")
    return <Loader2 size={13} className="tea-spin" />;
  if (state.tone === "failed" || state.tone === "warning")
    return <AlertCircle size={13} />;
  return <Check size={13} />;
}
