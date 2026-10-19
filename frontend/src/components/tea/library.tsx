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
  Paperclip,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  type DocumentDetail,
  type ListResult,
} from "@/core/tea/types";
import { useDocumentProcessing } from "@/core/tea/use-document-processing";

import { DocumentAttachments } from "./admin-library";
import { DocumentHistory } from "./document-history";
import { DocumentMetadataEditor } from "./document-metadata";
import { DocumentRelations } from "./document-relations";
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
  onDone: (document: Document) => void;
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
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const uploadRequest = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => uploadRequest.current?.abort(), []);
  useEffect(() => {
    if (!open) return;
    setScope(caseId ?? "");
    setRole(initialParentId ? "secondary" : "primary");
    setAttachmentCountry(initialCountry ?? "");
    setFile(null);
    setTags("");
    setParent("");
    setDocs([]);
    setError("");
    setDragging(false);
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
        source_scope: "personal",
      });
      if (!scope) params.set("library_only", "true");
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
      if (original?.source_scope === "system") setParent("");
      const options = result.items.filter(
        (document) =>
          document.role === "primary" &&
          document.source_scope !== "system" &&
          document.case_id === (scope || null),
      );
      setDocs(
        original &&
          original.source_scope !== "system" &&
          !options.some((document) => document.id === original.id)
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
    if (
      next &&
      role === "primary" &&
      !/\.(pdf|docx|html?|txt|md)$/i.test(next.name)
    ) {
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
          <DialogDescription>
            主文件解析正文；关联附件仅保存原件。
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
        <div className="tea-form-row">
          <label className="tea-field">
            <span>文件用途</span>
            <select
              value={role}
              onChange={(e) => {
                setRole(e.target.value);
                setFile(null);
                setError("");
              }}
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
            <select
              value={parent}
              onChange={(e) => setParent(e.target.value)}
              disabled={busy}
            >
              <option value="">选择主文件</option>
              {docs.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.title}
                </option>
              ))}
            </select>
          </label>
        )}
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
              : role === "primary"
                ? "PDF、Word、HTML、TXT / Markdown · 最大 20 MB"
                : "原始附件 · 最大 20 MB"}
          </span>
          <input
            key={role}
            className="sr-only"
            aria-label="选择上传文件"
            type="file"
            disabled={busy}
            accept={
              role === "primary" ? ".pdf,.docx,.html,.htm,.txt,.md" : undefined
            }
            onChange={(e) => {
              chooseFile(e.target.files?.[0] ?? null);
              e.target.value = "";
            }}
          />
        </label>
        {error && <ErrorNotice message={error} />}
        <ActionButton
          busy={busy}
          disabled={!file || (role === "secondary" && !parent)}
          onClick={upload}
        >
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
  const [roleFilter, setRoleFilter] = useState("");
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
  const scopeKey = `${ownerId ?? "session"}:${scope}:${sourceScope}:${country}:${nameQuery}:${roleFilter}`;
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
  }, [scope, country, sourceScope, nameQuery, roleFilter, ownerId]);
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
    setRoleFilter("");
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
    if (roleFilter) params.set("role", roleFilter);
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
    roleFilter,
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
      if (roleFilter) params.set("role", roleFilter);
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
            <span>
              {receipt.status === "processing"
                ? "已上传 · 处理中"
                : documentState(receipt).label}
            </span>
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
            <div
              className="tea-library-filters"
              role="group"
              aria-label="文件用途筛选"
            >
              {[
                ["", "全部"],
                ["primary", "主文件"],
                ["secondary", "附件"],
              ].map(([value, label]) => (
                <button
                  key={value}
                  aria-pressed={roleFilter === value}
                  onClick={() => {
                    if (roleFilter === value) return;
                    resetReads();
                    setRoleFilter(value!);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
            <span>{total} 份</span>
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
                    {doc.role === "secondary" ? (
                      <Paperclip size={20} />
                    ) : (
                      <FileText size={21} />
                    )}
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
                      <span>
                        v{doc.version} · {dateLabel(doc.created_at)}
                      </span>
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
              title={
                nameQuery
                  ? "没有找到匹配文件"
                  : roleFilter
                    ? "暂无此类文件"
                    : "暂无资料"
              }
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
            <DialogDescription>{selected?.filename}</DialogDescription>
          </DialogHeader>
          {selected && (
            <LibraryDocumentDetails
              key={`${selected.id}:${selected.version}`}
              document={selected}
              ownerId={ownerId}
              revision={revision}
              onSelect={setSelected}
              onSaved={(next) => {
                setSelected(next);
                setDocs((current) =>
                  current.map((item) => (item.id === next.id ? next : item)),
                );
                setReceipt((current) =>
                  current?.id === next.id ? next : current,
                );
                notifyDocumentsChange.current?.([next]);
              }}
              onUpload={() => {
                setSelected(undefined);
                onUpload(scope || undefined);
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function DocumentStatusIcon({ document }: { document: Document }) {
  if (document.status === "processing")
    return <Loader2 size={13} className="tea-spin" />;
  if (document.status === "failed" || document.error)
    return <AlertCircle size={13} />;
  return <Check size={13} />;
}

function LibraryDocumentDetails({
  document,
  ownerId,
  revision,
  onSelect,
  onSaved,
  onUpload,
}: {
  document: Document;
  ownerId?: string;
  revision: number;
  onSelect: (document: Document) => void;
  onSaved: (document: Document) => void;
  onUpload: () => void;
}) {
  const tabs = [
    ["content", "正文"],
    ["versions", "版本"],
    ["attachments", "附件"],
    ["relations", "法规关联"],
  ] as const;
  return (
    <>
      <div className="tea-document-meta">
        <span>
          {document.source_scope === "system" ? "共享法规 · 只读" : "私人资料"}
        </span>
        <span>版本 {document.version}</span>
        <span>{documentSize(document.size)}</span>
        <Button asChild size="sm" variant="outline">
          <a href={downloadUrl(document.id)}>
            <ArrowDownToLine size={14} />
            下载原文
          </a>
        </Button>
      </div>
      <div
        className={`tea-document-processing ${documentState(document).tone}`}
        role="status"
      >
        <DocumentStatusIcon document={document} />
        <strong>{documentState(document).label}</strong>
        {document.status === "processing" && <span>完成后自动更新</span>}
        {document.status === "failed" && document.source_scope !== "system" && (
          <Button size="sm" variant="outline" onClick={onUpload}>
            重新上传
          </Button>
        )}
      </div>
      {document.error && <ErrorNotice message={document.error} />}
      <DocumentMetadataEditor
        key={`${document.id}-${document.metadata_revision}`}
        document={document}
        onSaved={onSaved}
      />
      <Tabs defaultValue="content">
        <TabsList aria-label="资料内容" className="w-full">
          {tabs.map(([id, label]) => (
            <TabsTrigger key={id} value={id}>
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="content">
          {document.role === "secondary" ? (
            <div className="tea-note">
              该文件为关联附件，仅保存原文，不参与解析或检索。
            </div>
          ) : (
            <DocumentExcerpts
              key={`${document.id}:${document.status}:${document.chunk_count}`}
              documentId={document.id}
            />
          )}
        </TabsContent>
        <TabsContent value="versions">
          <DocumentHistory document={document} onSelect={onSelect} />
        </TabsContent>
        <TabsContent value="attachments">
          <DocumentAttachments
            parent={document}
            revision={revision}
            onSelect={onSelect}
          />
        </TabsContent>
        <TabsContent value="relations">
          <DocumentRelations
            document={document}
            ownerId={ownerId}
            onSelect={onSelect}
          />
        </TabsContent>
      </Tabs>
    </>
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
              <Empty
                title={
                  page.data.document.status === "processing"
                    ? "正在处理正文"
                    : page.data.document.status === "failed"
                      ? "正文解析失败"
                      : "暂无解析内容"
                }
              >
                可下载原始文件查看。
              </Empty>
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
