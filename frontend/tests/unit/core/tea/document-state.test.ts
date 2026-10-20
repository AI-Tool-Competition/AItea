import { describe, expect, it } from "@rstest/core";

import {
  documentProcessingMessage,
  documentState,
  mergeProcessingDocument,
} from "@/core/tea/document-state";
import { type Document } from "@/core/tea/types";

const pending = { status: "processing", role: "primary" } as Document;

describe("honest document progress", () => {
  it("does not infer active execution from persisted processing", () => {
    expect(documentState(pending)).toEqual({
      label: "等待处理",
      tone: "warning",
    });
    expect(documentProcessingMessage(pending)).toContain("暂未确认");
  });

  it.each([
    ["running", "处理中", "processing"],
    ["queued", "等待处理", "warning"],
    ["ocr_disabled", "识别未启用", "warning"],
    ["needs_review", "需核对", "warning"],
    ["failed", "处理失败", "failed"],
    ["incomplete", "正文待完善", "warning"],
  ] as const)(
    "shows %s independently of persisted status",
    (state, label, tone) => {
      expect(
        documentState({
          ...pending,
          processing_detail: {
            state,
            phase: "parse",
            retry_after_seconds: null,
          },
        }),
      ).toEqual({ label, tone });
    },
  );

  it("distinguishes embedding work from text parsing", () => {
    const document = {
      ...pending,
      status: "ready",
      processing_detail: {
        state: "queued",
        phase: "embed",
        retry_after_seconds: 30,
      },
    } as Document;
    expect(documentState(document).label).toBe("等待索引");
    expect(
      documentState({
        ...document,
        processing_detail: {
          ...document.processing_detail!,
          state: "running",
        },
      }).label,
    ).toBe("正在建立索引");
  });

  it("updates progress while preserving manually edited metadata", () => {
    const current = { ...pending, metadata_revision: 3 };
    const next = {
      ...pending,
      metadata_revision: 1,
      processing_detail: {
        state: "ocr_disabled",
        phase: "parse",
        retry_after_seconds: null,
      },
    } as Document;
    expect(mergeProcessingDocument(current, next)).toEqual({
      ...current,
      error: next.error,
      chunk_count: next.chunk_count,
      processing_detail: next.processing_detail,
    });
  });

  it("keeps confirmed complete text readable despite a historical parser notice", () => {
    expect(
      documentState({
        ...pending,
        status: "ready",
        error: "旧解析器提示",
        processing_detail: {
          state: "complete",
          phase: "parse",
          retry_after_seconds: null,
        },
      }),
    ).toEqual({ label: "已解析", tone: "ready" });
  });
});
