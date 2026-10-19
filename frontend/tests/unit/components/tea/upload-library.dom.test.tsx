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

import { Library, UploadDialog } from "@/components/tea/library";
import { type Document } from "@/core/tea/types";

const file: Document = {
  id: "material",
  title: "产品说明",
  filename: "product.md",
  role: "primary",
  status: "processing",
  mime_type: "text/markdown",
  size: 230,
  version: 1,
  case_id: "case-1",
  parent_id: null,
  tags: [],
  created_at: "2026-10-19T00:00:00Z",
  source_url: null,
  error: null,
  chunk_count: 0,
};
function response(value: unknown) {
  return new Response(JSON.stringify(value));
}
function pathOf(input: RequestInfo | URL) {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}
afterEach(() => {
  cleanup();
  rs.useRealTimers();
  rs.restoreAllMocks();
});

describe("upload and library journey", () => {
  it("updates the list, upload receipt and open preview when processing completes", async () => {
    const done = {
      ...file,
      status: "ready",
      chunk_count: 1,
      metadata_revision: 2,
    } as Document;
    let processed = false;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      if (pathOf(url).includes("/documents?"))
        return response({ items: [file], total: 1 });
      const document = processed ? done : file;
      return response({
        document,
        elements: processed ? [{ content_text: "产品说明正文" }] : [],
        element_total: processed ? 1 : 0,
        chunks: [],
        chunk_total: 0,
        warnings: [],
      });
    });
    rs.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    render(
      <Library
        initialCaseId="case-1"
        scopeLocked
        businessOnly
        cases={[]}
        revision={1}
        uploadedDocument={file}
        onUpload={() => undefined}
      />,
    );
    await act(async () => {
      await rs.advanceTimersByTimeAsync(0);
    });
    expect(
      screen.getByRole("button", { name: /产品说明.*处理中/ }),
    ).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    await act(async () => {
      await rs.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("heading", { name: "正在处理正文" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("法域 / 适用地区"), {
      target: { value: "未保存的地区" },
    });
    processed = true;
    await act(async () => {
      await rs.advanceTimersByTimeAsync(3000);
    });
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("已解析")).toBeTruthy();
    expect(dialog.getByText("产品说明正文")).toBeTruthy();
    expect(
      screen.getByLabelText<HTMLInputElement>("法域 / 适用地区").value,
    ).toBe("未保存的地区");
    expect(document.querySelector(".tea-library-file")?.textContent).toContain(
      "已解析",
    );
    expect(
      document.querySelector(".tea-upload-receipt")?.textContent,
    ).toContain("已解析");
  });

  it("keeps a server-backed attachment filter and does not erase an active filter's results", async () => {
    const reads: string[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      reads.push(pathOf(url));
      return response({
        items: [{ ...file, status: "stored", role: "secondary" }],
        total: 1,
      });
    });
    render(
      <Library
        initialCaseId="case-1"
        scopeLocked
        businessOnly
        cases={[]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /产品说明/ });
    fireEvent.click(screen.getByRole("button", { name: "附件" }));
    await screen.findByRole("button", { name: /产品说明/ });
    expect(reads.at(-1)).toContain("role=secondary");
    const count = reads.length;
    fireEvent.click(screen.getByRole("button", { name: "附件" }));
    expect(screen.getByRole("button", { name: /产品说明/ })).toBeTruthy();
    expect(reads.length).toBe(count);
    expect(screen.queryByText(/资料仅当前账号可见/)).toBeNull();
  });

  it("retries a failed content search without replacing it with a directory refresh", async () => {
    let searches = 0;
    let directoryReads = 0;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (pathOf(input).endsWith("/search")) {
        searches += 1;
        return searches === 1
          ? new Response(
              JSON.stringify({ error: { message: "检索暂时不可用" } }),
              { status: 503 },
            )
          : response({ items: [] });
      }
      directoryReads += 1;
      return response({ items: [{ ...file, status: "ready" }], total: 1 });
    });
    render(
      <Library
        initialCaseId="case-1"
        scopeLocked
        businessOnly
        cases={[]}
        revision={0}
        onUpload={() => undefined}
      />,
    );
    await screen.findByRole("button", { name: /产品说明/ });
    fireEvent.change(screen.getByLabelText("搜索方式"), {
      target: { value: "content" },
    });
    fireEvent.change(screen.getByLabelText("搜索资料"), {
      target: { value: "产品" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));
    await screen.findByText("检索暂时不可用");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("heading", { name: "没有找到相关依据" });
    expect(searches).toBe(2);
    expect(directoryReads).toBe(1);
    expect(screen.getByLabelText<HTMLInputElement>("搜索资料").value).toBe(
      "产品",
    );
  });

  it("validates a drop and preserves the selected file and tags after a failed upload", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "暂时无法保存" } }), {
        status: 503,
      }),
    );
    const done = rs.fn();
    render(
      <UploadDialog
        open
        cases={[]}
        onOpenChange={() => undefined}
        onDone={done}
      />,
    );
    const drop = screen.getByText("点击选择或拖入文件").closest("label")!;
    fireEvent.drop(drop, {
      dataTransfer: { files: [new File([], "empty.md")] },
    });
    expect(screen.getByRole("alert").textContent).toContain("文件为空");
    fireEvent.drop(drop, {
      dataTransfer: { files: [new File(["hello"], "product.md")] },
    });
    fireEvent.change(screen.getByLabelText("标签"), {
      target: { value: "产品,出口" },
    });
    fireEvent.click(screen.getByRole("button", { name: "上传并保存" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("暂时无法保存"),
    );
    expect(screen.getByText("product.md")).toBeTruthy();
    expect(screen.getByLabelText<HTMLInputElement>("标签").value).toBe(
      "产品,出口",
    );
    expect(done).not.toHaveBeenCalled();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "上传并保存" })
        .disabled,
    ).toBe(false);
  });
});
