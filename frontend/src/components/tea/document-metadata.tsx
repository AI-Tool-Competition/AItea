"use client";
import { Save, ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import { errorMessage, patch } from "@/core/tea/api";
import { type Document, type DocumentMetadata } from "@/core/tea/types";

import { ActionButton, ErrorNotice } from "./shared";

export function DocumentMetadataEditor({
  document,
  onSaved,
}: {
  document: Document;
  onSaved: (document: Document) => void;
}) {
  const [metadata, setMetadata] = useState<DocumentMetadata>(
    document.metadata ?? {
      kind: "business",
      jurisdiction: "",
      language: "zh",
      published_at: null,
      effective_from: null,
      effective_to: null,
      review_status: "draft",
    },
  );
  const [approve, setApprove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  if (document.role === "secondary") return null;
  if (document.source_scope === "system")
    return (
      <div className="tea-note">
        系统法规总库 · 只读 ·{" "}
        {document.metadata?.review_status === "published" &&
        document.reviewed_at
          ? "已记录审核，请核对适用期间。"
          : "待审核，尚不能作为有效法规引用。"}
      </div>
    );
  async function save() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await patch<Document>(
        `/documents/${document.id}/metadata`,
        {
          ...metadata,
          review_status: approve ? "published" : "draft",
          expected_revision: document.metadata_revision ?? 1,
        },
      );
      onSaved(result);
      setApprove(false);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <details className="tea-config-details">
      <summary>
        <ShieldCheck size={13} className="inline" /> 资料分类与适用性 ·{" "}
        {document.metadata?.review_status === "published" ? "已审核" : "待审核"}
      </summary>
      <div className="tea-note">
        先核对来源和适用日期。待审核资料不进入回答引用；未知日期不代表适用性已获确认。
      </div>
      <div className="tea-form-row">
        <label className="tea-field">
          <span>资料类别</span>
          <select
            value={metadata.kind}
            onChange={(e) =>
              setMetadata({
                ...metadata,
                kind: e.target.value as DocumentMetadata["kind"],
              })
            }
          >
            <option value="law">法律法规</option>
            <option value="guidance">办事指南</option>
            <option value="case">案例</option>
            <option value="business">业务材料</option>
            <option value="demo">演示资料</option>
          </select>
        </label>
        <label className="tea-field">
          <span>法域 / 适用地区</span>
          <Input
            value={metadata.jurisdiction}
            onChange={(e) =>
              setMetadata({ ...metadata, jurisdiction: e.target.value })
            }
            placeholder="例如：印度尼西亚"
          />
        </label>
      </div>
      <div className="tea-form-row">
        <label className="tea-field">
          <span>语言</span>
          <Input
            value={metadata.language}
            onChange={(e) =>
              setMetadata({ ...metadata, language: e.target.value })
            }
            placeholder="zh / id / en"
          />
        </label>
        <label className="tea-field">
          <span>公布日期</span>
          <Input
            type="date"
            value={metadata.published_at ?? ""}
            onChange={(e) =>
              setMetadata({ ...metadata, published_at: e.target.value || null })
            }
          />
        </label>
      </div>
      <div className="tea-form-row">
        <label className="tea-field">
          <span>生效日期 · 可留空</span>
          <Input
            type="date"
            value={metadata.effective_from ?? ""}
            onChange={(e) =>
              setMetadata({
                ...metadata,
                effective_from: e.target.value || null,
              })
            }
          />
        </label>
        <label className="tea-field">
          <span>失效日期 · 可留空</span>
          <Input
            type="date"
            value={metadata.effective_to ?? ""}
            onChange={(e) =>
              setMetadata({ ...metadata, effective_to: e.target.value || null })
            }
          />
        </label>
      </div>
      <label className="tea-confirm">
        <input
          type="checkbox"
          checked={approve}
          onChange={(e) => setApprove(e.target.checked)}
        />
        <span>我已核对以上分类与适用性，保存为已审核资料。</span>
      </label>
      {error && <ErrorNotice message={error} />}
      <ActionButton size="sm" variant="outline" busy={busy} onClick={save}>
        <Save size={13} />
        {approve ? "保存并通过审核" : "保存为待审核"}
      </ActionButton>
    </details>
  );
}
