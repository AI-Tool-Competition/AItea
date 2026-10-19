import { type Document } from "./types";

export function documentSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function documentState(document: Document) {
  if (document.role === "secondary" || document.status === "stored")
    return {
      label: "原件已保存",
      tone: "stored",
    };
  if (document.status === "processing")
    return {
      label: "处理中",
      tone: "processing",
    };
  if (document.status === "failed")
    return {
      label: "解析失败",
      tone: "failed",
    };
  return {
    label: document.error ? "解析有提示" : "已解析",
    tone: document.error ? "warning" : "ready",
  };
}

// Background status reads must preserve the metadata editor's revision and draft.
export function mergeProcessingDocument(
  current: Document,
  next: Document,
): Document {
  return {
    ...current,
    status: next.status,
    error: next.error,
    chunk_count: next.chunk_count,
  };
}
