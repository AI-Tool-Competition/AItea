import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AdminLibrary } from "@/components/tea/admin-library";
import { Library } from "@/components/tea/library";
import { clearCollectionDrafts } from "@/core/tea/collection-drafts";
import { type CaseSummary, type Document } from "@/core/tea/types";

const law = {
  id: "law",
  country: "ID",
  title: "印尼法规",
  filename: "law.txt",
  role: "primary",
  legal_level: "primary",
  status: "ready",
  mime_type: "text/plain",
  size: 30,
  version: 1,
  case_id: null,
  parent_id: null,
  tags: ["法律"],
  created_at: "2026-10-12T01:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 1,
  classifications: [
    { collection: "country:ID", level: "primary", tags: ["法律"], revision: 1 },
  ],
} as Document;
const plans = {
  items: [{ id: "id-plan", country: "ID", title: "印尼清单", item_count: 29 }],
  total: 1,
};
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}
function pathOf(url: RequestInfo | URL) {
  return typeof url === "string"
    ? url
    : url instanceof URL
      ? url.href
      : url.url;
}
function read(url: RequestInfo | URL) {
  const path = pathOf(url);
  if (path.endsWith("/library/countries")) return response(plans);
  if (path.includes("/relations")) return response({ items: [], total: 0 });
  if (path.includes("/documents/law")) return response({ document: law });
  return response({ items: [law], total: 1 });
}
afterEach(() => {
  cleanup();
  clearCollectionDrafts();
  rs.restoreAllMocks();
});

describe("country boundaries in document management", () => {
  it("defaults public lists to the configured country and explicitly separates unassigned documents", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => read(url));
    render(<AdminLibrary />);
    await screen.findByText("印尼法规");
    expect(screen.getByLabelText<HTMLSelectElement>("资料国家").value).toBe(
      "ID",
    );
    const documentReads = fetcher.mock.calls.filter(([url]) =>
      pathOf(url).includes("/documents?"),
    );
    expect(documentReads.length).toBeGreaterThan(0);
    expect(
      documentReads.every(
        ([url]) =>
          new URL(pathOf(url), "http://localhost").searchParams.get(
            "country",
          ) === "ID",
      ),
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("资料国家"), {
      target: { value: "unassigned" },
    });
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([url]) =>
          pathOf(url).includes("country=unassigned"),
        ),
      ).toBe(true),
    );
  });
  it("sends an explicit country for RAG and ignores an old country's delayed response", async () => {
    let signal: AbortSignal | null | undefined;
    let finish!: (result: Response) => void;
    let submitted: Record<string, unknown> = {};
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "POST") {
        submitted = JSON.parse(options.body as string) as Record<
          string,
          unknown
        >;
        signal = options.signal;
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      return read(url);
    });
    render(<Library cases={[]} revision={0} onUpload={() => undefined} />);
    await screen.findByRole("button", { name: /印尼法规/ });
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "进口规则" },
    });
    fireEvent.change(screen.getByLabelText("搜索方式"), {
      target: { value: "content" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await waitFor(() => expect(signal).toBeTruthy());
    expect(submitted.country).toBe("ID");
    fireEvent.change(screen.getByLabelText("资料国家"), {
      target: { value: "unassigned" },
    });
    expect(signal?.aborted).toBe(true);
    await act(async () =>
      finish(
        response({
          items: [
            { document_id: "old", title: "过时检索结果", excerpt: "不应显示" },
          ],
        }),
      ),
    );
    expect(screen.queryByText("过时检索结果")).toBeNull();
  });
  it("lets the server determine a case's country instead of submitting an unrelated selector", async () => {
    let submitted: Record<string, unknown> = {};
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "POST") {
        submitted = JSON.parse(options.body as string) as Record<
          string,
          unknown
        >;
        return response({ items: [] });
      }
      return read(url);
    });
    render(
      <Library
        cases={[{ id: "case-1", title: "出口业务" } as CaseSummary]}
        initialCaseId="case-1"
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /印尼法规/ });
    expect(screen.getByLabelText<HTMLSelectElement>("资料国家").disabled).toBe(
      true,
    );
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "条款" },
    });
    fireEvent.change(screen.getByLabelText("搜索方式"), {
      target: { value: "content" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await waitFor(() => expect(submitted.case_id).toBe("case-1"));
    expect(submitted.country).toBeUndefined();
  });
});
