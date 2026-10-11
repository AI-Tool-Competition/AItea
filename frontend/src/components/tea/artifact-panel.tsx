"use client";
import { Check, Download, FileText, History, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  downloadUrl,
  errorMessage,
  patch,
  post,
  teaRequest,
} from "@/core/tea/api";
import { type Artifact, type ListResult } from "@/core/tea/types";

import { ActionButton, dateLabel, Empty, ErrorNotice } from "./shared";

const draftCache = new Map<
  string,
  { title: string; content: string; version: number }
>();
export function clearArtifactDrafts() {
  draftCache.clear();
}

export function ArtifactPanel({
  artifact,
  onSaved,
}: {
  artifact?: Artifact;
  onSaved: (artifact: Artifact) => void | Promise<void>;
}) {
  const [title, setTitle] = useState(
    artifact ? (draftCache.get(artifact.id)?.title ?? artifact.title) : "",
  );
  const [content, setContent] = useState(
    artifact ? (draftCache.get(artifact.id)?.content ?? artifact.content) : "",
  );
  const [baseVersion, setBaseVersion] = useState(
    artifact ? (draftCache.get(artifact.id)?.version ?? artifact.version) : 0,
  );
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const pending = useRef(false);
  const [versions, setVersions] = useState<Artifact[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const dirty =
    !!artifact && (title !== artifact.title || content !== artifact.content);
  useEffect(() => {
    if (artifact && dirty)
      draftCache.set(artifact.id, {
        title,
        content,
        version: baseVersion,
      });
    else if (artifact) draftCache.delete(artifact.id);
  }, [artifact, title, content, dirty, baseVersion]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  if (!artifact)
    return (
      <Empty icon={<FileText size={30} />} title="让资料成为一份好文稿">
        从任务中选择“起草文稿”，在这里编辑、确认和下载。
      </Empty>
    );
  async function action(kind: "save" | "confirm" | "history") {
    if (!artifact || pending.current) return;
    pending.current = true;
    setBusy(kind);
    setError("");
    try {
      if (kind === "history") {
        const result = await teaRequest<ListResult<Artifact>>(
          `/artifacts/${artifact.id}/versions`,
        );
        setVersions(result.items);
        setShowHistory(!showHistory);
      } else {
        const next =
          kind === "save"
            ? await patch<Artifact>(`/artifacts/${artifact.id}`, {
                title,
                content,
                expected_version: baseVersion,
              })
            : await post<Artifact>(`/artifacts/${artifact.id}/confirm`, {
                expected_version: baseVersion,
              });
        draftCache.delete(next.id);
        setBaseVersion(next.version);
        await onSaved(next);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy("");
      pending.current = false;
    }
  }
  return (
    <div className="tea-artifact">
      <div className="tea-artifact-bar">
        <span className="tea-eyebrow">DOCUMENT · V{artifact.version}</span>
        <span
          className={`tea-status ${artifact.confirmed ? "completed" : "pending"}`}
        >
          {dirty ? "有未保存修改" : artifact.confirmed ? "已确认" : "草稿"}
        </span>
      </div>
      <label className="tea-field">
        <span>文稿名称</span>
        <Input
          value={title}
          disabled={!!busy}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <label className="tea-field tea-editor-label">
        <span>正文 · 支持 Markdown</span>
        <textarea
          className="tea-document-editor"
          value={content}
          disabled={!!busy}
          onChange={(e) => setContent(e.target.value)}
        />
      </label>
      {baseVersion !== artifact.version && (
        <div className="tea-note">
          服务器已有第 {artifact.version} 版，你的修改基于第 {baseVersion}{" "}
          版。请先复制自己的内容，再载入最新版本进行核对。
          <button
            type="button"
            onClick={() => {
              draftCache.delete(artifact.id);
              setTitle(artifact.title);
              setContent(artifact.content);
              setBaseVersion(artifact.version);
            }}
          >
            载入最新版本
          </button>
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      <div className="tea-artifact-actions">
        <ActionButton
          busy={busy === "save"}
          disabled={!!busy || !dirty || !title.trim()}
          onClick={() => action("save")}
        >
          <Save size={14} />
          保存版本
        </ActionButton>
        <ActionButton
          variant="outline"
          busy={busy === "confirm"}
          disabled={!!busy || dirty || artifact.confirmed}
          onClick={() => action("confirm")}
        >
          <Check size={14} />
          确认
        </ActionButton>
        <Button asChild variant="outline" size="icon" title="下载文稿">
          <a href={downloadUrl(artifact.id, "artifacts")} aria-label="下载文稿">
            <Download size={15} />
          </a>
        </Button>
      </div>
      <Button
        variant="ghost"
        size="sm"
        disabled={!!busy}
        onClick={() => action("history")}
      >
        <History size={14} />
        版本记录
      </Button>
      {showHistory && (
        <div className="tea-version-list">
          {versions.map((version) => (
            <details key={version.version}>
              <summary>
                第 {version.version} 版{" "}
                <span>{dateLabel(version.updated_at)}</span>
              </summary>
              <pre>{version.content}</pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
