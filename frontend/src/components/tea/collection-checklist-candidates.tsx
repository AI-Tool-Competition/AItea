"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  downloadUrl,
  errorMessage,
  patch,
  safeSourceUrl,
  TeaApiError,
  teaRequest,
} from "@/core/tea/api";
import { type ChecklistCandidate, type ListResult } from "@/core/tea/types";

import { ActionButton, Busy, ErrorNotice } from "./shared";

const PAGE_SIZE = 20;
const statusLabels = {
  pending: "待确认",
  queued: "采集中",
  stored: "已入库",
  excluded: "已排除",
};
const sourceFieldLabels: Record<string, string> = {
  link_text: "来源链接原文标题",
  listed_version: "目录列出版本",
  listed_issue_date: "目录列出发布日期",
  publication_date: "来源公布日期",
  row_description: "来源目录说明",
  id: "来源文书 ID",
  id_theme: "官方目录主题 ID",
  document_type: "来源文种分类",
  document_file_url: "来源原件链接",
  verified_original_url: "校验后的原件链接",
  regulation_number: "文号",
  regulation_type: "文种",
  establishment_date: "制定日期",
  slug: "官方目录标识",
  jns: "文种代码",
  nama_jenis: "官方文种",
  no_peraturan: "文号",
  tahun: "年份",
  tentang: "官方标题",
  teu: "发布机关",
  tgl_di: "制定日期",
  diundangkan: "公布日期",
  bahasa: "来源语言",
  tempat: "地点",
  lokasi: "来源位置",
  subjek: "主题",
  bidang_hukum: "法律领域",
  status: "来源状态标签",
  status_hukum: "来源法律状态标签",
  ln: "国家公报号",
  tln: "国家公报补编号",
  terkait: "相关文件原始记录",
  mengubah: "修改文件原始记录",
  mencabut: "废止文件原始记录",
  diubah: "被修改文件原始记录",
  dicabut: "被废止文件原始记录",
  diubah_sebagian: "部分修改记录",
  dicabut_sebagian: "部分废止记录",
};
const sourceProviderLabels: Record<string, string> = {
  setneg: "Setneg",
  bkpm: "BKPM",
  rcepsec: "RCEP 秘书处",
  ipos: "新加坡知识产权局",
  acra: "新加坡会计与企业管理局",
};
const sourceUnitLabels: Record<string, string> = {
  preamble: "序言",
  chapter: "章节",
  annex: "附件",
  appendix: "附录",
  schedule: "成员减让表/附表",
  protocol: "议定书",
  document_part: "文书组成部分",
  circular: "通告",
  practice_direction: "实务指引",
  registrar_interpretation: "注册局解释",
  guide: "指南",
  manual_section: "工作手册章节",
  form_or_template: "表格/模板",
  document_link: "资料链接",
};
export function ChecklistCandidates({
  basePath,
  refreshKey,
  selected,
  storedSelected,
  disabled,
  onSelection,
  excludeId,
  excludeNote,
  onExclusionDraft,
  onChanged,
}: {
  basePath: string;
  refreshKey: number;
  selected: string[];
  storedSelected: string[];
  disabled: boolean;
  onSelection: (selected: string[], storedIds?: string[]) => void;
  excludeId: string;
  excludeNote: string;
  onExclusionDraft: (id: string, note: string) => void;
  onChanged: () => void;
}) {
  const [status, setStatus] = useState<"pending" | "stored" | "excluded">(
    "pending",
  );
  const [page, setPage] = useState<ListResult<ChecklistCandidate>>({
    items: [],
    total: 0,
  });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const [retry, setRetry] = useState(0);
  const mutation = useRef<AbortController | null>(null);
  useEffect(() => () => mutation.current?.abort(), []);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    teaRequest<ListResult<ChecklistCandidate>>(
      `${basePath}/candidates?status=${status}&limit=${PAGE_SIZE}&offset=${offset}`,
      { signal: abort.signal },
    )
      .then((next) => {
        if (abort.signal.aborted) return;
        const lastOffset =
          Math.max(0, Math.ceil(next.total / PAGE_SIZE) - 1) * PAGE_SIZE;
        if (offset > lastOffset) {
          setOffset(lastOffset);
          return;
        }
        setPage(next);
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [basePath, refreshKey, status, offset, retry]);
  async function changeCandidate(
    candidate: ChecklistCandidate,
    nextStatus: "pending" | "excluded",
  ) {
    if (mutation.current || disabled) return;
    const abort = new AbortController();
    mutation.current = abort;
    setBusy(candidate.id);
    setActionError("");
    try {
      await patch<ChecklistCandidate>(
        `/collection-candidates/${encodeURIComponent(candidate.id)}`,
        {
          expected_revision: candidate.revision,
          status: nextStatus,
          ...(nextStatus === "excluded" ? { note: excludeNote.trim() } : {}),
        },
        abort.signal,
      );
      if (abort.signal.aborted) return;
      onSelection(selected.filter((id) => id !== candidate.id));
      onExclusionDraft("", "");
      onChanged();
    } catch (e) {
      if (abort.signal.aborted) return;
      setActionError(errorMessage(e));
      if (e instanceof TeaApiError && e.status === 409)
        setRetry((value) => value + 1);
    } finally {
      mutation.current = null;
      if (!abort.signal.aborted) setBusy("");
    }
  }
  const pendingPage = page.items.filter(
    (candidate) =>
      candidate.status === "pending" || candidate.status === "stored",
  );
  function select(ids: string[]) {
    const stored = new Set([
      ...storedSelected,
      ...page.items
        .filter((candidate) => candidate.status === "stored")
        .map((candidate) => candidate.id),
    ]);
    onSelection(
      ids,
      ids.filter((id) => stored.has(id)),
    );
  }
  const pageSelected =
    pendingPage.length > 0 &&
    pendingPage.every((candidate) => selected.includes(candidate.id));
  return (
    <section className="ta-checklist-candidates" aria-label="候选资料">
      <div className="ta-checklist-candidates-toolbar">
        <div className="ta-checklist-candidate-tabs" aria-label="候选状态">
          {(["pending", "stored", "excluded"] as const).map((value) => (
            <Button
              key={value}
              size="sm"
              variant={status === value ? "secondary" : "ghost"}
              aria-pressed={status === value}
              onClick={() => {
                setStatus(value);
                setOffset(0);
              }}
            >
              {statusLabels[value]}
            </Button>
          ))}
        </div>
        <Button
          variant="ghost"
          size="sm"
          disabled={loading}
          onClick={() => setRetry((value) => value + 1)}
        >
          刷新候选
        </Button>
      </div>
      {actionError && <ErrorNotice message={actionError} />}
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : loading ? (
        <Busy label="读取候选…" />
      ) : (
        <>
          {status !== "excluded" && pendingPage.length > 0 && (
            <div className="ta-checklist-selection">
              <label className="tea-checkbox">
                <input
                  type="checkbox"
                  checked={pageSelected}
                  disabled={disabled || !!busy}
                  onChange={() =>
                    select(
                      pageSelected
                        ? selected.filter(
                            (id) =>
                              !pendingPage.some(
                                (candidate) => candidate.id === id,
                              ),
                          )
                        : [
                            ...new Set([
                              ...selected,
                              ...pendingPage.map((candidate) => candidate.id),
                            ]),
                          ].slice(0, 200),
                    )
                  }
                />
                选择本页
              </label>
              <span>已选 {selected.length} 项</span>
            </div>
          )}
          <ul className="ta-checklist-candidate-list">
            {page.items.map((candidate) => {
              const href = safeSourceUrl(candidate.url);
              const origin = safeSourceUrl(candidate.discovered_from);
              const title = candidate.title || candidate.url;
              return (
                <li key={candidate.id}>
                  <div className="ta-checklist-candidate-main">
                    {(candidate.status === "pending" ||
                      candidate.status === "stored") && (
                      <input
                        type="checkbox"
                        aria-label={`选择 ${title}`}
                        checked={selected.includes(candidate.id)}
                        disabled={
                          disabled ||
                          !!busy ||
                          (!selected.includes(candidate.id) &&
                            selected.length >= 200)
                        }
                        onChange={(event) =>
                          select(
                            event.target.checked
                              ? [...selected, candidate.id]
                              : selected.filter((id) => id !== candidate.id),
                          )
                        }
                      />
                    )}
                    <div className="ta-checklist-candidate-text">
                      {href ? (
                        <a
                          href={href}
                          title={candidate.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {title}
                        </a>
                      ) : (
                        <strong>{title}</strong>
                      )}
                      {href && title !== candidate.url && (
                        <small>{new URL(href).hostname}</small>
                      )}
                      <div className="ta-checklist-candidate-meta">
                        <span>{statusLabels[candidate.status]}</span>
                        {origin && (
                          <a href={origin} target="_blank" rel="noreferrer">
                            发现来源
                          </a>
                        )}
                        {candidate.note && <span>{candidate.note}</span>}
                      </div>
                      {candidate.source_record && (
                        <details className="ta-checklist-source-record">
                          <summary>
                            {sourceProviderLabels[candidate.source_record.provider]} 来源字段（尚未审核）
                          </summary>
                          {candidate.source_record.structure && (
                            <p>
                              {sourceUnitLabels[
                                candidate.source_record.structure.unit_type
                              ] ?? candidate.source_record.structure.unit_type}
                              {candidate.source_record.structure.number &&
                                ` ${candidate.source_record.structure.number}`}
                              {candidate.source_record.structure.parent_titles.length > 0 &&
                                ` · ${candidate.source_record.structure.parent_titles.join(" › ")}`}
                            </p>
                          )}
                          <dl>
                            {Object.entries(candidate.source_record.fields)
                              .filter(([key, value]) =>
                                Boolean(sourceFieldLabels[key]) &&
                                value !== null &&
                                value !== "",
                              )
                              .map(([key, value]) => (
                                <div key={key}>
                                  <dt>{sourceFieldLabels[key]}</dt>
                                  <dd>{String(value)}</dd>
                                </div>
                              ))}
                          </dl>
                          {candidate.source_record.file?.realName && (
                            <p>原件文件名：{candidate.source_record.file.realName}</p>
                          )}
                          <small>
                            来源页 SHA-256：{candidate.source_record.evidence.sha256}
                          </small>
                        </details>
                      )}
                    </div>
                    <div className="ta-checklist-candidate-actions">
                      {candidate.status === "pending" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={disabled || !!busy}
                          onClick={() =>
                            onExclusionDraft(candidate.id, candidate.note)
                          }
                        >
                          排除
                        </Button>
                      )}
                      {candidate.status === "excluded" && (
                        <ActionButton
                          size="sm"
                          variant="ghost"
                          disabled={disabled || !!busy}
                          busy={busy === candidate.id}
                          onClick={() =>
                            void changeCandidate(candidate, "pending")
                          }
                        >
                          恢复
                        </ActionButton>
                      )}
                      {candidate.document_id && (
                        <Button size="sm" variant="ghost" asChild>
                          <a href={downloadUrl(candidate.document_id)}>原件</a>
                        </Button>
                      )}
                    </div>
                  </div>
                  {excludeId === candidate.id &&
                    candidate.status === "pending" && (
                      <div className="ta-checklist-exclude">
                        <Input
                          aria-label="排除原因"
                          placeholder="排除原因（可选）"
                          value={excludeNote}
                          disabled={!!busy}
                          onChange={(event) =>
                            onExclusionDraft(candidate.id, event.target.value)
                          }
                        />
                        <ActionButton
                          size="sm"
                          variant="outline"
                          disabled={disabled || !!busy}
                          busy={busy === candidate.id}
                          onClick={() =>
                            void changeCandidate(candidate, "excluded")
                          }
                        >
                          确认排除
                        </ActionButton>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!!busy}
                          onClick={() => onExclusionDraft("", "")}
                        >
                          取消
                        </Button>
                      </div>
                    )}
                </li>
              );
            })}
          </ul>
          {!page.items.length && (
            <p className="ta-checklist-no-candidates">
              {status === "pending"
                ? "暂无候选"
                : status === "stored"
                  ? "尚无已入库资料。"
                  : "尚无排除记录。"}
            </p>
          )}
          {page.total > PAGE_SIZE && (
            <div className="ta-pagination">
              <span>
                {offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} /{" "}
                {page.total}
              </span>
              <div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={!offset}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  上一页
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={offset + PAGE_SIZE >= page.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
