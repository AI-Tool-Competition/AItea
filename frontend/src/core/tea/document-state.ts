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
  const detail = document.processing_detail;
  if (detail?.state === "running")
    return {
      label: detail.phase === "embed" ? "正在建立索引" : "处理中",
      tone: "processing",
    };
  if (detail?.state === "ocr_disabled")
    return { label: "识别未启用", tone: "warning" };
  if (detail?.state === "needs_review")
    return { label: "需核对", tone: "warning" };
  if (detail?.state === "failed") return { label: "处理失败", tone: "failed" };
  if (detail?.state === "queued")
    return {
      label: detail.phase === "embed" ? "等待索引" : "等待处理",
      tone: "warning",
    };
  if (detail?.state === "incomplete")
    return { label: "正文待完善", tone: "warning" };
  if (detail?.state === "complete") return { label: "已解析", tone: "ready" };
  if (document.status === "processing")
    return { label: "等待处理", tone: "warning" };
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

// Background status reads must preserve unrelated document metadata.
export function mergeProcessingDocument(
  current: Document,
  next: Document,
): Document {
  return {
    ...current,
    status: next.status,
    error: next.error,
    chunk_count: next.chunk_count,
    ...(next.processing_detail !== undefined && {
      processing_detail: next.processing_detail,
    }),
  };
}

export function documentProcessingMessage(document: Document) {
  switch (document.processing_detail?.state) {
    case "running":
      return document.processing_detail.phase === "embed"
        ? "正在建立检索索引，已有正文可继续查看。"
        : "任务正在执行，已有正文可继续查看。";
    case "ocr_disabled":
      return "文字核验需要图片识别，当前未启用；原件及已有正文已保留。";
    case "needs_review":
      return "部分文字或来源映射需要核对；原件及已有正文已保留。";
    case "failed":
      return "处理未成功；原件及已有正文已保留，请核对处理记录。";
    case "incomplete":
      return "正文尚未完整处理；可查看已有内容或下载原件。";
    case "queued":
      return "等待执行任务；当前没有有效的运行任务，原件及已有正文已保留。";
    default:
      return document.status === "processing"
        ? "等待处理；暂未确认有正在执行的任务，可下载原件查看。"
        : undefined;
  }
}
