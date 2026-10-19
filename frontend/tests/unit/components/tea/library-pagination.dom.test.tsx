import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { Library } from "@/components/tea/library";
import { type Document, type DocumentDetail } from "@/core/tea/types";

const first: Document = {
  id: "first",
  title: "完整办理指南",
  filename: "guide.md",
  role: "primary",
  status: "ready",
  mime_type: "text/markdown",
  size: 1000,
  version: 1,
  case_id: null,
  parent_id: null,
  tags: [],
  created_at: "2026-10-11T00:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 101,
};
const second: Document = { ...first, id: "second", title: "新的资料" };

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status });
}

function page(document: Document, offset: number, total = 101): DocumentDetail {
  return {
    document,
    elements: Array.from({ length: Math.min(100, total - offset) }, (_, i) => ({
      element_key: `${document.id}-${offset + i}`,
      kind: "paragraph",
      content_text: `${document.title}第 ${offset + i + 1} 段正文`,
      section_path: ["办理说明"],
      locator: { line_start: offset + i + 1 },
    })),
    chunks: [],
    element_total: total,
    chunk_total: total,
    warnings: [],
  };
}

function requestUrl(input: RequestInfo | URL) {
  const path =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
  return new URL(path, "http://localhost");
}

afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("library parsed-content pagination", () => {
  it("reads beyond paragraph 100 with bounded pages and recovers a failed page without losing the current text", async () => {
    let failNextPage = true;
    const detailRequests: URL[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/tea/library/countries")
        return response({ items: [], total: 0 });
      if (url.pathname === "/api/tea/documents")
        return response({ items: [first], total: 1 });
      detailRequests.push(url);
      const offset = Number(url.searchParams.get("element_offset") ?? 0);
      if (offset === 100 && failNextPage) {
        failNextPage = false;
        return response({ error: { message: "读取下一页失败" } }, 503);
      }
      return response(page(first, offset));
    });
    render(<Library cases={[]} revision={0} onUpload={() => undefined} />);
    fireEvent.click(
      await screen.findByRole("button", { name: /完整办理指南/ }),
    );
    const dialog = within(screen.getByRole("dialog", { name: first.title }));
    await waitFor(() =>
      expect(dialog.getByText("完整办理指南第 1 段正文")).toBeTruthy(),
    );
    expect(dialog.getByText("第 1–100 段 / 共 101 段")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "下一页正文" }));
    await waitFor(() =>
      expect(dialog.getByRole("alert").textContent).toContain("读取下一页失败"),
    );
    expect(dialog.getByText("完整办理指南第 1 段正文")).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(dialog.getByText("完整办理指南第 101 段正文")).toBeTruthy(),
    );
    expect(dialog.queryByText("完整办理指南第 1 段正文")).toBeNull();
    expect(dialog.getByText("第 101–101 段 / 共 101 段")).toBeTruthy();
    expect(
      dialog.getByRole<HTMLButtonElement>("button", { name: "下一页正文" })
        .disabled,
    ).toBe(true);
    fireEvent.click(dialog.getByRole("button", { name: "上一页正文" }));
    await waitFor(() =>
      expect(dialog.getByText("完整办理指南第 1 段正文")).toBeTruthy(),
    );
    expect(
      detailRequests.map((url) => url.searchParams.get("element_offset")),
    ).toEqual(["0", "100", "100", "0"]);
    expect(
      detailRequests.every((url) => url.searchParams.get("limit") === "100"),
    ).toBe(true);
  });

  it("aborts an old page request and starts the newly selected document at its first paragraph", async () => {
    let finishOldPage: ((value: Response) => void) | undefined;
    let oldSignal: AbortSignal | null | undefined;
    const secondOffsets: string[] = [];
    const pending = new Promise<Response>((resolve) => {
      finishOldPage = resolve;
    });
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input, options) => {
      const url = requestUrl(input);
      if (url.pathname === "/api/tea/library/countries")
        return response({ items: [], total: 0 });
      if (url.pathname === "/api/tea/documents")
        return response({ items: [first, second], total: 2 });
      const offset = Number(url.searchParams.get("element_offset") ?? 0);
      if (url.pathname.endsWith("/first") && offset === 100) {
        oldSignal = options?.signal;
        return pending;
      }
      if (url.pathname.endsWith("/second")) {
        secondOffsets.push(url.searchParams.get("element_offset") ?? "");
        return response(page(second, offset, 1));
      }
      return response(page(first, offset));
    });
    render(<Library cases={[]} revision={0} onUpload={() => undefined} />);
    fireEvent.click(
      await screen.findByRole("button", { name: /完整办理指南/ }),
    );
    await waitFor(() =>
      expect(screen.getByText("完整办理指南第 1 段正文")).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "下一页正文" }));
    await waitFor(() => expect(oldSignal).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(oldSignal?.aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /新的资料/ }));
    await waitFor(() =>
      expect(screen.getByText("新的资料第 1 段正文")).toBeTruthy(),
    );
    await act(async () => {
      finishOldPage?.(response(page(first, 100)));
    });
    expect(screen.queryByText("完整办理指南第 101 段正文")).toBeNull();
    expect(screen.getByText("新的资料第 1 段正文")).toBeTruthy();
    expect(secondOffsets).toEqual(["0"]);
  });
});
