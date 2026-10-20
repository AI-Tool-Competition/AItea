"use client";

import { useEffect, useState } from "react";

import { teaRequest } from "./api";
import { type Document, type DocumentDetail } from "./types";

export function useDocumentProcessing({
  documents,
  boundary,
  active,
  onUpdate,
}: {
  documents: Document[];
  boundary: string;
  active: boolean;
  onUpdate: (documents: Document[]) => void;
}) {
  const pending = [
    ...new Set(
      documents
        .filter(
          (item) =>
            item.status === "processing" ||
            item.processing_detail?.state === "running" ||
            item.processing_detail?.state === "queued",
        )
        .map((item) => item.id),
    ),
  ]
    .sort()
    .join(",");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setError("");
    if (!pending || !active) return;
    const ids = pending.split(",");
    let cancelled = false;
    let cursor = 0;
    let failures = 0;
    let controller: AbortController | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      if (window.document.visibilityState === "hidden") {
        timer = setTimeout(() => void read(), 3000);
        return;
      }
      controller = new AbortController();
      const signal = controller.signal;
      deadline = setTimeout(() => controller?.abort(), 12000);
      // At most five bounded summaries per tick, including later list pages.
      const batch = Array.from(
        { length: Math.min(5, ids.length) },
        (_, index) => ids[(cursor + index) % ids.length]!,
      );
      cursor = (cursor + batch.length) % ids.length;
      const results = await Promise.allSettled(
        batch.map((id) =>
          teaRequest<DocumentDetail>(
            `/documents/${encodeURIComponent(id)}?limit=1&include_preview=false`,
            { signal },
          ),
        ),
      );
      clearTimeout(deadline);
      if (cancelled) return;
      onUpdate(
        results.flatMap((result) =>
          result.status === "fulfilled" ? [result.value.document] : [],
        ),
      );
      failures = results.some((result) => result.status === "rejected")
        ? failures + 1
        : 0;
      setError(failures ? "暂时无法更新处理状态，原件已保留。" : "");
      if (failures < 3)
        timer = setTimeout(() => void read(), 3000 * 2 ** failures);
    };
    timer = setTimeout(() => void read(), 3000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      clearTimeout(deadline);
      controller?.abort();
    };
  }, [pending, boundary, active, attempt, onUpdate]);
  return { error, retry: () => setAttempt((value) => value + 1) };
}
