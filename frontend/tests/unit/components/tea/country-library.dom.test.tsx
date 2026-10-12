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
import { DocumentClassificationEditor } from "@/components/tea/document-classification";
import { DocumentRelations } from "@/components/tea/document-relations";
import { Library, UploadDialog } from "@/components/tea/library";
import {
  clearCollectionDrafts,
  relationDrafts,
  classificationDrafts,
} from "@/core/tea/collection-drafts";
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
  if (path.endsWith("/collection-plans")) return response(plans);
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
    render(<AdminLibrary cases={[]} revision={0} onUpload={() => undefined} />);
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
  it("uses the case country when switching the admin library from public unassigned materials", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => read(url));
    render(
      <AdminLibrary
        cases={[{ id: "case-1", title: "印尼出口业务" } as CaseSummary]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByText("印尼法规");
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
    fireEvent.change(screen.getByLabelText("资料范围"), {
      target: { value: "case-1" },
    });
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([url]) =>
          pathOf(url).includes("case_id=case-1"),
        ),
      ).toBe(true),
    );
    const caseReads = fetcher.mock.calls.filter(([url]) =>
      pathOf(url).includes("case_id=case-1"),
    );
    expect(
      caseReads.every(
        ([url]) =>
          !new URL(pathOf(url), "http://localhost").searchParams.has("country"),
      ),
    ).toBe(true);
    const country = screen.getByLabelText<HTMLSelectElement>("资料国家");
    expect(country.disabled).toBe(true);
    expect(country.selectedOptions[0]?.textContent).toBe("按业务目的地");
    fireEvent.change(screen.getByLabelText("资料范围"), {
      target: { value: "" },
    });
    expect(country.disabled).toBe(false);
    expect(country.value).toBe("unassigned");
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
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await waitFor(() => expect(submitted.case_id).toBe("case-1"));
    expect(submitted.country).toBeUndefined();
  });
  it("loads attachment parents from the original parent's country", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => read(url));
    render(
      <UploadDialog
        open
        onOpenChange={() => undefined}
        initialParentId="law"
        initialCountry="ID"
        cases={[]}
        onDone={() => undefined}
      />,
    );
    await screen.findByRole("option", { name: "印尼法规" });
    const parentReads = fetcher.mock.calls.filter(([url]) =>
      pathOf(url).includes("/documents?"),
    );
    expect(parentReads.length).toBeGreaterThan(0);
    expect(
      parentReads.every(
        ([url]) =>
          new URL(pathOf(url), "http://localhost").searchParams.get(
            "country",
          ) === "ID",
      ),
    ).toBe(true);
    expect(screen.getByLabelText<HTMLSelectElement>("关联到主文件").value).toBe(
      "law",
    );
  });

  it("locks manual relationships to the document's immutable country", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => read(url));
    render(<DocumentRelations document={law} onSelect={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "添加关联" }));
    expect(screen.getByLabelText<HTMLInputElement>("关联采集组").readOnly).toBe(
      true,
    );
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([url]) =>
          pathOf(url).includes("collection=country%3AID"),
        ),
      ).toBe(true),
    );
  });
  it("keeps unassigned case materials selectable within the case country boundary", async () => {
    const current = {
      ...law,
      country: null,
      case_id: "case-1",
      classifications: [],
    };
    const candidate = {
      ...current,
      id: "case-reference",
      title: "业务内未分国参考",
    };
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) => {
        const path = pathOf(url);
        if (path.includes("/documents?")) {
          const params = new URL(path, "http://localhost").searchParams;
          if (params.has("country"))
            return response({ detail: "资料国家与业务目的地不一致。" }, 422);
          return response({ items: [candidate], total: 1 });
        }
        return read(url);
      });
    render(<DocumentRelations document={current} onSelect={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "添加关联" }));
    fireEvent.click(
      await screen.findByRole("checkbox", { name: /业务内未分国参考/ }),
    );
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "保存关联" })
        .disabled,
    ).toBe(false);
    expect(
      fetcher.mock.calls.some(([url]) =>
        pathOf(url).includes("case_id=case-1"),
      ),
    ).toBe(true);
    expect(
      fetcher.mock.calls.some(([url]) =>
        pathOf(url).includes("country=unassigned"),
      ),
    ).toBe(false);
  });
  it("drops a stale relationship target after a document acquires a country", async () => {
    relationDrafts.set("owner:law", {
      target: ["foreign-law"],
      collection: "country:SG",
      relation: "cites",
      locator: "第十二条",
    });
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => read(url));
    render(
      <DocumentRelations
        document={law}
        ownerId="owner"
        onSelect={() => undefined}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "移除 foreign-law" }),
    ).toBeNull();
    expect(screen.getByLabelText<HTMLInputElement>("原文定位").value).toBe(
      "第十二条",
    );
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "保存关联" })
        .disabled,
    ).toBe(true);
  });

  it("retains classification edits but submits the revision of its fixed country", async () => {
    classificationDrafts.set("owner:law", {
      collection: "country:SG",
      level: "primary",
      tags: "未保存标签",
      expected_revision: 7,
    });
    const writes: Record<string, unknown>[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "PATCH") {
        writes.push(
          JSON.parse(options.body as string) as Record<string, unknown>,
        );
        return response(law);
      }
      return read(url);
    });
    render(
      <DocumentClassificationEditor
        document={law}
        ownerId="owner"
        onSaved={() => undefined}
      />,
    );
    fireEvent.click(screen.getByText("分级与标签"));
    fireEvent.click(screen.getByRole("button", { name: "保存分级" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      collection: "country:ID",
      tags: ["未保存标签"],
      expected_revision: 1,
    });
  });

  it("locks a country's classification and lets an unassigned upload explicitly join the country", async () => {
    const writes: Record<string, unknown>[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "PATCH") {
        writes.push(
          JSON.parse(options.body as string) as Record<string, unknown>,
        );
        return response(law);
      }
      return read(url);
    });
    const view = render(
      <DocumentClassificationEditor document={law} onSaved={() => undefined} />,
    );
    fireEvent.click(screen.getByText("分级与标签"));
    expect(screen.getByLabelText<HTMLInputElement>("采集组").readOnly).toBe(
      true,
    );
    view.unmount();
    render(
      <DocumentClassificationEditor
        document={{ ...law, country: null, classifications: [] }}
        onSaved={() => undefined}
      />,
    );
    fireEvent.click(screen.getByText("分级与标签"));
    fireEvent.click(await screen.findByRole("button", { name: "归入印尼" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      collection: "country:ID",
      expected_revision: 0,
      tags: ["法律"],
    });
  });
});
