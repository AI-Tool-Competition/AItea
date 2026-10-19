import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import { act, cleanup, renderHook } from "@testing-library/react";

import { mergeProcessingDocument } from "@/core/tea/document-state";
import { type Document } from "@/core/tea/types";
import { useDocumentProcessing } from "@/core/tea/use-document-processing";

const pending = {
  id: "file-1",
  status: "processing",
  metadata_revision: 1,
} as Document;
const ready = { ...pending, status: "ready", chunk_count: 3 } as Document;
const response = (document: Document) =>
  new Response(JSON.stringify({ document }));
function pathOf(input: RequestInfo | URL) {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

beforeEach(() => {
  rs.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});
afterEach(() => {
  cleanup();
  rs.useRealTimers();
  rs.restoreAllMocks();
});

describe("document processing updates", () => {
  it("updates processing files and stops reading once they finish", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response(ready));
    const update = rs.fn();
    const view = renderHook(
      ({ document }) =>
        useDocumentProcessing({
          documents: [document],
          boundary: "alice:case-1",
          active: true,
          onUpdate: update,
        }),
      { initialProps: { document: pending } },
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    expect(update).toHaveBeenCalledWith([ready]);
    expect(pathOf(fetcher.mock.calls[0]![0])).toContain("limit=1");
    view.rerender({ document: ready });
    await act(async () => {
      await rs.advanceTimersByTimeAsync(30_000);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("cancels a previous business read and ignores a late response", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
      signal = options?.signal;
      return new Promise((resolve) => {
        finish = resolve;
      });
    });
    const update = rs.fn();
    const view = renderHook(
      ({ boundary }) =>
        useDocumentProcessing({
          documents: [pending],
          boundary,
          active: true,
          onUpdate: update,
        }),
      { initialProps: { boundary: "alice:first" } },
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    view.rerender({ boundary: "bob:second" });
    expect(signal?.aborted).toBe(true);
    await act(async () => {
      finish(response(ready));
    });
    expect(update).not.toHaveBeenCalled();
  });

  it("bounds each batch and reaches files beyond the first five", async () => {
    const documents = Array.from({ length: 7 }, (_, i) => ({
      ...pending,
      id: `file-${i}`,
    }));
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => {
        const id = pathOf(url).split("/").pop()!.split("?")[0]!;
        return response({ ...pending, id });
      });
    const update = rs.fn();
    renderHook(() =>
      useDocumentProcessing({
        documents,
        boundary: "alice",
        active: true,
        onUpdate: update,
      }),
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    expect(fetcher).toHaveBeenCalledTimes(5);
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    expect(
      fetcher.mock.calls.some(([url]) => pathOf(url).includes("file-6?")),
    ).toBe(true);
  });

  it("pauses after three failed reads and supports explicit recovery", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => new Response("{}", { status: 503 }));
    const update = rs.fn();
    const view = renderHook(() =>
      useDocumentProcessing({
        documents: [pending],
        boundary: "alice",
        active: true,
        onUpdate: update,
      }),
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(60_000);
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(view.result.current.error).toContain("暂时无法更新");
    fetcher.mockResolvedValue(response(ready));
    act(() => view.result.current.retry());
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    expect(update).toHaveBeenLastCalledWith([ready]);
    expect(view.result.current.error).toBe("");
  });

  it("does not poll a closed panel and cancels its in-flight work", async () => {
    let signal: AbortSignal | null | undefined;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (_url, options) => {
        signal = options?.signal;
        return new Promise<Response>(() => undefined);
      });
    const update = rs.fn();
    const view = renderHook(
      ({ active }) =>
        useDocumentProcessing({
          documents: [pending],
          boundary: "alice",
          active,
          onUpdate: update,
        }),
      { initialProps: { active: false } },
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(6000);
    });
    expect(fetcher).not.toHaveBeenCalled();
    view.rerender({ active: true });
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    view.rerender({ active: false });
    expect(signal?.aborted).toBe(true);
  });

  it("preserves newer manual metadata when a processing read resolves", () => {
    const current = {
      ...pending,
      metadata_revision: 3,
      metadata: { kind: "business" },
      reviewed_at: "2026-10-19",
    } as Document;
    expect(mergeProcessingDocument(current, ready)).toEqual({
      ...ready,
      metadata_revision: 3,
      metadata: current.metadata,
      reviewed_at: current.reviewed_at,
    });
  });
});
