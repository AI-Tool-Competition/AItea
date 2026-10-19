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
import { DocumentChooser } from "@/components/tea/document-chooser";
import { Library, UploadDialog } from "@/components/tea/library";
import { type CaseSummary, type Document } from "@/core/tea/types";

const shared: Document = {
  id: "shared",
  title: "共享食品法规",
  filename: "law.txt",
  source_scope: "system",
  country: "ID",
  role: "primary",
  status: "ready",
  mime_type: "text/plain",
  size: 20,
  version: 1,
  case_id: null,
  parent_id: null,
  tags: [],
  created_at: "2026-10-16T00:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 1,
};
const privateDocument: Document = {
  ...shared,
  id: "private",
  title: "我的未分国资料",
  source_scope: "personal",
  country: null,
};
const caseDocument: Document = {
  ...privateDocument,
  id: "case-file",
  title: "我的业务材料",
  case_id: "case-1",
};
const cases = [{ id: "case-1", title: "茶叶出口业务" } as CaseSummary];
function response(body: unknown) {
  return new Response(JSON.stringify(body));
}
function urlOf(input: RequestInfo | URL) {
  return new URL(
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url,
    "http://localhost",
  );
}
function defaultRead(input: RequestInfo | URL) {
  const url = urlOf(input);
  if (url.pathname.endsWith("/library/countries"))
    return response({
      items: [{ country: "ID" }, { country: "SG" }],
      total: 2,
    });
  if (url.pathname.endsWith("/documents"))
    return response({
      items:
        url.searchParams.get("source_scope") === "personal"
          ? [privateDocument, caseDocument]
          : [shared],
      total: url.searchParams.get("source_scope") === "personal" ? 2 : 1,
    });
  return response({
    document: shared,
    elements: [],
    chunks: [],
    element_total: 0,
    chunk_total: 0,
    warnings: [],
  });
}
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("shared regulations and isolated private library", () => {
  it("keeps private uploads discoverable across countries and businesses, with directory search in the same scope", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => defaultRead(input));
    const onUpload = rs.fn();
    render(
      <Library
        ownerId="alice"
        cases={cases}
        revision={0}
        onUpload={onUpload}
      />,
    );
    await screen.findByRole("button", { name: /共享食品法规/ });
    expect(
      screen
        .getByRole("button", { name: "法规总库" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "上传到我的资料" }));
    expect(onUpload).toHaveBeenCalledWith(undefined);
    fireEvent.click(screen.getByRole("button", { name: "我的资料" }));
    await screen.findByRole("button", { name: /我的未分国资料/ });
    expect(
      screen.getByRole("button", { name: /我的业务材料/ }).textContent,
    ).toContain("茶叶出口业务");
    expect(screen.getByLabelText<HTMLSelectElement>("资料国家").value).toBe(
      "all",
    );
    expect(screen.queryByRole("button", { name: /共享食品法规/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "合同" },
    });
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "检索" }).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "查找文件" }));
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          ([input]) => urlOf(input).searchParams.get("q") === "合同",
        ),
      ).toBe(true),
    );
    const privateReads = fetcher.mock.calls
      .map(([input]) => urlOf(input))
      .filter((url) => url.searchParams.get("source_scope") === "personal");
    expect(privateReads.length).toBeGreaterThan(0);
    expect(
      privateReads.every(
        (url) =>
          !url.searchParams.has("country") && !url.searchParams.has("case_id"),
      ),
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("资料国家"), {
      target: { value: "SG" },
    });
    expect(screen.getByLabelText<HTMLInputElement>("搜索资料").value).toBe("");
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          ([input]) => urlOf(input).searchParams.get("country") === "SG",
        ),
      ).toBe(true),
    );
  });

  it("distinguishes an empty filename filter from an empty private library and recovers after clearing it", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = urlOf(input);
      if (url.searchParams.get("q") === "alice")
        return response({ items: [], total: 0 });
      return defaultRead(input);
    });
    render(
      <Library
        ownerId="bob"
        cases={[]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /共享食品法规/ });
    fireEvent.click(screen.getByRole("button", { name: "我的资料" }));
    await screen.findByRole("button", { name: /我的未分国资料/ });
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "alice" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查找文件" }));
    await screen.findByRole("heading", { name: "没有找到匹配文件" });
    expect(screen.getByText("调整资料名称或国家筛选后重试。")).toBeTruthy();
    expect(screen.queryByText(/上传第一份私人资料/)).toBeNull();
    expect(
      screen.queryByRole("heading", { name: "资料库准备好了" }),
    ).toBeNull();
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查找文件" }));
    await screen.findByRole("button", { name: /我的未分国资料/ });
    expect(
      screen.queryByRole("heading", { name: "没有找到匹配文件" }),
    ).toBeNull();
  });

  it("aborts a shared search and ignores its delayed response after switching to private files", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    let submitted: Record<string, unknown> = {};
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input, options) => {
      if (urlOf(input).pathname.endsWith("/search")) {
        submitted = JSON.parse(options?.body as string) as Record<
          string,
          unknown
        >;
        signal = options?.signal;
        return new Promise((resolve) => {
          finish = resolve;
        });
      }
      return defaultRead(input);
    });
    render(
      <Library
        ownerId="alice"
        cases={[]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /共享食品法规/ });
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "食品" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await waitFor(() => expect(submitted.source_scope).toBe("system"));
    expect(submitted.country).toBe("ID");
    fireEvent.click(screen.getByRole("button", { name: "我的资料" }));
    expect(signal?.aborted).toBe(true);
    await screen.findByRole("button", { name: /我的未分国资料/ });
    await act(async () =>
      finish(
        response({
          items: [
            { document_id: "old", title: "迟到法规", excerpt: "不应出现" },
          ],
        }),
      ),
    );
    expect(screen.queryByText("迟到法规")).toBeNull();
  });

  it("uses the business destination for shared regulations and clears the selected private file", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => defaultRead(input));
    render(
      <Library
        ownerId="alice"
        cases={cases}
        initialCaseId="case-1"
        scopeLocked
        revision={0}
        onUpload={() => undefined}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /我的业务材料/ }),
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "对应国家法规" }));
    await screen.findByRole("button", { name: /共享食品法规/ });
    const reads = fetcher.mock.calls
      .map(([input]) => urlOf(input))
      .filter((url) => url.searchParams.get("source_scope") === "system");
    expect(reads.length).toBeGreaterThan(0);
    expect(
      reads.every(
        (url) =>
          url.searchParams.get("case_id") === "case-1" &&
          !url.searchParams.has("country"),
      ),
    ).toBe(true);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resets private scope and selected details when the account changes", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input, options) => {
      if (urlOf(input).pathname.endsWith("/private")) {
        signal = options?.signal;
        return new Promise((resolve) => {
          finish = resolve;
        });
      }
      return defaultRead(input);
    });
    const view = render(
      <Library
        ownerId="alice"
        cases={[]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /共享食品法规/ });
    fireEvent.click(screen.getByRole("button", { name: "我的资料" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /我的未分国资料/ }),
    );
    await waitFor(() => expect(signal).toBeTruthy());
    view.rerender(
      <Library
        ownerId="bob"
        cases={[]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    expect(signal?.aborted).toBe(true);
    await screen.findByRole("button", { name: /共享食品法规/ });
    await act(async () =>
      finish(
        response({
          document: privateDocument,
          elements: [{ content_text: "Alice的私人正文" }],
          element_total: 1,
          chunks: [],
          chunk_total: 0,
          warnings: [],
        }),
      ),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("Alice的私人正文")).toBeNull();
  });

  it("returns the uploaded private document and opens it in the private library", async () => {
    const done = rs.fn();
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input, options) =>
      options?.method === "POST"
        ? response(privateDocument)
        : defaultRead(input),
    );
    const { unmount } = render(
      <UploadDialog
        open
        onOpenChange={() => undefined}
        cases={[]}
        onDone={done}
      />,
    );
    expect(
      screen.getByRole("option", { name: "我的资料 · 仅当前账号" }),
    ).toBeTruthy();
    const file = new File(["hello"], "private.txt", { type: "text/plain" });
    fireEvent.change(document.body.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并保存" }));
    await waitFor(() => expect(done).toHaveBeenCalledWith(privateDocument));
    unmount();
    const library = render(
      <Library
        ownerId="alice"
        uploadedDocument={privateDocument}
        cases={[]}
        revision={1}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("dialog", { name: privateDocument.title });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen
        .getByRole("button", { name: "我的资料" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    library.rerender(
      <Library
        ownerId="alice"
        uploadedDocument={caseDocument}
        cases={cases}
        revision={2}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("dialog", { name: caseDocument.title });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen
        .getByRole("button", { name: "本业务资料" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("cancels an upload on account unmount and does not deliver its late result", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    const done = rs.fn();
    const close = rs.fn();
    rs.spyOn(globalThis, "fetch").mockImplementation(
      async (_input, options) => {
        signal = options?.signal;
        return new Promise((resolve) => {
          finish = resolve;
        });
      },
    );
    const { unmount } = render(
      <UploadDialog open onOpenChange={close} cases={[]} onDone={done} />,
    );
    fireEvent.change(document.body.querySelector('input[type="file"]')!, {
      target: { files: [new File(["hello"], "private.txt")] },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并保存" }));
    await waitFor(() => expect(signal).toBeTruthy());
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(response(privateDocument)));
    expect(done).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });

  it("uses the same ownership split in the management directory", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => defaultRead(input));
    render(
      <AdminLibrary
        ownerId="alice"
        cases={cases}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByText(shared.title);
    fireEvent.click(screen.getByRole("button", { name: "我的资料" }));
    await screen.findByText(privateDocument.title);
    expect(screen.getByLabelText<HTMLSelectElement>("资料国家").value).toBe(
      "all",
    );
    expect(
      fetcher.mock.calls.some(([input]) => {
        const url = urlOf(input);
        return (
          url.searchParams.get("source_scope") === "personal" &&
          !url.searchParams.has("country")
        );
      }),
    ).toBe(true);
  });

  it("resets chooser selection, search, pagination and delayed candidates when its boundary changes", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | null | undefined;
    const onChange = rs.fn();
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, options) => {
        const url = urlOf(input);
        if (url.searchParams.get("offset") === "20") {
          signal = options?.signal;
          return new Promise((resolve) => {
            finish = resolve;
          });
        }
        return response({
          items: [
            url.searchParams.get("source_scope") === "system"
              ? shared
              : privateDocument,
          ],
          total: 40,
        });
      });
    const view = render(
      <DocumentChooser
        ownerId="alice"
        sourceScope="personal"
        selected={[privateDocument.id]}
        onChange={onChange}
      />,
    );
    await screen.findByRole("checkbox", {
      name: new RegExp(privateDocument.title),
    });
    fireEvent.change(screen.getByLabelText("搜索关联法规"), {
      target: { value: "合同" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查找" }));
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(
          ([input]) => urlOf(input).searchParams.get("q") === "合同",
        ),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(signal).toBeTruthy());
    view.rerender(
      <DocumentChooser
        ownerId="alice"
        sourceScope="system"
        selected={[privateDocument.id]}
        onChange={onChange}
      />,
    );
    expect(signal?.aborted).toBe(true);
    await screen.findByText(shared.title);
    expect(onChange).toHaveBeenCalledWith([]);
    expect(screen.getByLabelText<HTMLInputElement>("搜索关联法规").value).toBe(
      "",
    );
    expect(screen.queryByRole("button", { name: /移除.*private/ })).toBeNull();
    await act(async () =>
      finish(
        response({
          items: [{ ...privateDocument, title: "迟到私人候选" }],
          total: 40,
        }),
      ),
    );
    expect(screen.queryByText("迟到私人候选")).toBeNull();
    const last = urlOf(fetcher.mock.calls.at(-1)![0]);
    expect(last.searchParams.get("offset")).toBe("0");
    expect(last.searchParams.has("q")).toBe(false);
  });
});
