import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { CollectionProgress } from "@/components/tea/collection-run";
import { CustomSources as Sources } from "@/components/tea/sources";
import { clearCollectionDrafts } from "@/core/tea/collection-drafts";
import { type CollectionRun } from "@/core/tea/types";

const source = {
  id: "source-1",
  name: "法规库",
  url: "https://example.com/law",
  enabled: true,
  interval_hours: 24,
  tags: ["ID", "1.3"],
  last_fetched_at: null,
  status: "idle",
  last_error: null,
  revision: 1,
  plan: {
    collection: "ID",
    level: "primary",
    seed_urls: [],
    primary_document_ids: [],
    discover_attachments: true,
    max_items: 200,
  },
};
const run: CollectionRun = {
  id: "run-1",
  source_id: source.id,
  status: "running",
  revision: 2,
  created_at: "2026-10-12T00:00:00Z",
  updated_at: "2026-10-12T00:00:00Z",
  error: null,
  total: 3,
  stored: 1,
  failed: 0,
  pending: 1,
  processing: 1,
  processed: 0,
  processing_failed: 0,
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
afterEach(() => {
  cleanup();
  clearCollectionDrafts();
  rs.restoreAllMocks();
});

describe("collection source operations", () => {
  it("polls stored counts and stops reading after the run finishes", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ run, items: [], total: 3 }))
      .mockResolvedValueOnce(
        response({
          run: {
            ...run,
            status: "completed",
            updated_at: "2026-10-12T00:00:02Z",
            stored: 3,
            processed: 3,
            processing: 0,
            pending: 0,
          },
          items: [],
          total: 3,
        }),
      );
    render(<CollectionProgress initial={run} />);
    expect(screen.getByText("已存 1 / 3")).toBeTruthy();
    expect(
      await screen.findByText("已存 3 / 3", {}, { timeout: 3500 }),
    ).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 2300));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("retains newer progress when an older source snapshot arrives at the same revision", async () => {
    const completed: CollectionRun = {
      ...run,
      status: "completed",
      updated_at: "2026-10-12T00:00:02Z",
      stored: 3,
      processed: 3,
      processing: 0,
      pending: 0,
    };
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ run: completed, items: [], total: 3 }),
    );
    const view = render(<CollectionProgress initial={run} />);
    await screen.findByText("已存 3 / 3");
    view.rerender(<CollectionProgress initial={{ ...run }} />);
    expect(screen.getByText("已存 3 / 3")).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "取消任务" })).toBeNull();

    // A new control generation takes precedence even if its timestamp is earlier.
    view.rerender(
      <CollectionProgress
        initial={{ ...run, revision: run.revision + 1, status: "queued" }}
      />,
    );
    expect(screen.getByText("已存 1 / 3")).toBeTruthy();
    expect(screen.getByText("已排队")).toBeTruthy();
  });

  it("ignores a delayed progress read after a newer source snapshot", async () => {
    let finishRead!: (result: Response) => void;
    const pendingRead = new Promise<Response>((resolve) => {
      finishRead = resolve;
    });
    rs.spyOn(globalThis, "fetch").mockReturnValue(pendingRead);
    const onChange = rs.fn();
    const view = render(
      <CollectionProgress initial={run} onChange={onChange} />,
    );
    view.rerender(
      <CollectionProgress
        initial={{ ...run, stored: 2, updated_at: "2026-10-12T00:00:02Z" }}
        onChange={onChange}
      />,
    );
    await act(async () => {
      finishRead(
        response({
          run: { ...run, updated_at: "2026-10-12T00:00:01Z" },
          items: [],
          total: 3,
        }),
      );
    });
    expect(screen.getByText("已存 2 / 3")).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("retains polled progress when the cancellation acknowledgement arrives late", async () => {
    let finishRead!: (result: Response) => void;
    let finishCancel!: (result: Response) => void;
    const pendingRead = new Promise<Response>((resolve) => {
      finishRead = resolve;
    });
    const pendingCancel = new Promise<Response>((resolve) => {
      finishCancel = resolve;
    });
    const cancelled: CollectionRun = {
      ...run,
      revision: run.revision + 1,
      status: "cancelled",
      updated_at: "2026-10-12T00:00:02Z",
      stored: 2,
      pending: 0,
    };
    let reads = 0;
    rs.spyOn(globalThis, "fetch").mockImplementation((_url, options) => {
      if (options?.method === "POST") return pendingCancel;
      reads += 1;
      return reads === 1 ? pendingRead : new Promise<Response>(() => undefined);
    });
    const onChange = rs.fn();
    render(<CollectionProgress initial={run} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    await act(async () => {
      finishRead(response({ run: cancelled, items: [], total: 3 }));
    });
    await screen.findByText("已存 2 / 3");
    await act(async () => {
      finishCancel(
        response({
          ...cancelled,
          stored: 1,
          updated_at: "2026-10-12T00:00:01Z",
        }),
      );
    });
    expect(screen.getByText("已存 2 / 3")).toBeTruthy();
    expect(screen.getByText("已取消")).toBeTruthy();
    expect(onChange.mock.calls.every(([next]) => next.stored === 2)).toBe(true);
  });

  it("loads changed source settings for explicit review while retaining the draft", async () => {
    let attempts = 0;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        if (options?.method === "PATCH") {
          attempts += 1;
          return attempts === 1
            ? response(
                {
                  error: { code: "stale_revision", message: "来源版本已更新" },
                },
                409,
              )
            : response({ ...source, name: "我的来源名称", revision: 3 });
        }
        if (pathOf(url).endsWith("/sources/source-1"))
          return response({
            ...source,
            name: "另一窗口的来源名称",
            revision: 2,
          });
        return response({ items: [source], total: 1 });
      });
    render(<Sources compact />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    fireEvent.change(screen.getByLabelText("来源名称"), {
      target: { value: "我的来源名称" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存来源" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "核对最新配置" }),
    );
    await screen.findByText(/另一窗口的来源名称/);
    expect(screen.getByLabelText<HTMLInputElement>("来源名称").value).toBe(
      "我的来源名称",
    );
    fireEvent.click(screen.getByRole("button", { name: "核对后保留我的修改" }));
    fireEvent.click(screen.getByRole("button", { name: "保存来源" }));
    await waitFor(() => expect(attempts).toBe(2));
    const writes = fetcher.mock.calls.filter(
      ([, options]) => options?.method === "PATCH",
    );
    expect(JSON.parse(writes[1]![1]!.body as string)).toMatchObject({
      name: "我的来源名称",
      expected_revision: 2,
    });
  });
  it("keeps conflict feedback and refreshes progress without repeating cancellation", async () => {
    let cancelled = false;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        if (options?.method === "POST") {
          cancelled = true;
          return response(
            {
              error: {
                code: "stale_revision",
                message: "任务进度已更新，请核对后重试",
              },
            },
            409,
          );
        }
        if (pathOf(url).includes("/collection-runs/"))
          return response({
            run: {
              ...run,
              revision: cancelled ? 4 : 2,
              stored: cancelled ? 2 : 1,
            },
            items: [],
            total: 3,
          });
        return response({ items: [{ ...source, latest_run: run }], total: 1 });
      });
    render(<Sources compact />);
    await screen.findByText("已存 1 / 3");
    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    await screen.findByText("已存 2 / 3");
    expect(screen.getByText("任务进度已更新，请核对后重试")).toBeTruthy();
    const posts = fetcher.mock.calls.filter(
      ([, options]) => options?.method === "POST",
    );
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0]![1]!.body as string).expected_revision).toBe(2);
  });

  it("maps a secondary source to multiple primary versions without submitting while selecting", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        if (options?.method === "PATCH")
          return response({ ...source, revision: 2 });
        if (pathOf(url).includes("/documents?"))
          return response({
            items: [
              {
                id: "primary-a",
                title: "主法甲",
                version: 3,
                legal_level: "primary",
              },
              {
                id: "primary-b",
                title: "主法乙",
                version: 1,
                legal_level: "primary",
              },
            ],
            total: 2,
          });
        return response({ items: [source], total: 1 });
      });
    render(<Sources compact />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    fireEvent.change(screen.getByLabelText("资料级别"), {
      target: { value: "secondary" },
    });
    fireEvent.click(await screen.findByLabelText(/主法甲/));
    fireEvent.click(screen.getByLabelText(/主法乙/));
    expect(
      fetcher.mock.calls.filter(([, options]) => options?.method === "PATCH"),
    ).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "保存来源" }));
    await waitFor(() =>
      expect(
        fetcher.mock.calls.filter(([, options]) => options?.method === "PATCH"),
      ).toHaveLength(1),
    );
    const payload = JSON.parse(
      fetcher.mock.calls.find(([, options]) => options?.method === "PATCH")![1]!
        .body as string,
    );
    expect(payload).toMatchObject({
      expected_revision: 1,
      plan: {
        level: "secondary",
        collection: "ID",
        primary_document_ids: ["primary-a", "primary-b"],
      },
    });
    expect(
      fetcher.mock.calls.some(([url]) =>
        pathOf(url).includes("legal_level=primary"),
      ),
    ).toBe(true);
  });

  it("retains unsaved source plans only within their account", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response({ items: [], total: 0 }),
    );
    const first = render(<Sources compact ownerId="one" />);
    fireEvent.click(await screen.findByRole("button", { name: "添加来源" }));
    fireEvent.change(screen.getByLabelText("来源名称"), {
      target: { value: "未保存来源" },
    });
    first.unmount();
    const second = render(<Sources compact ownerId="two" />);
    fireEvent.click(await screen.findByRole("button", { name: "添加来源" }));
    expect(screen.getByLabelText<HTMLInputElement>("来源名称").value).toBe("");
    second.unmount();
    render(<Sources compact ownerId="one" />);
    fireEvent.click(await screen.findByRole("button", { name: "添加来源" }));
    expect(screen.getByLabelText<HTMLInputElement>("来源名称").value).toBe(
      "未保存来源",
    );
  });
  it("submits a durable run and shows stored files while collection is still running", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        const path = pathOf(url);
        if (path.includes("/collection-runs/"))
          return response({ run, items: [], total: 3 });
        if (options?.method === "POST")
          return response({ ...run, status: "queued" }, 202);
        return response({ items: [source], total: 1 });
      });
    render(<Sources compact />);
    fireEvent.click(await screen.findByRole("button", { name: "立即抓取" }));
    await screen.findByText("已存 1 / 3");
    expect(screen.getByRole("button", { name: "取消任务" })).toBeTruthy();
    expect(
      fetcher.mock.calls.some(([url]) => pathOf(url).endsWith("/fetch")),
    ).toBe(false);
    const submit = fetcher.mock.calls.find(
      ([, options]) => options?.method === "POST",
    );
    expect(pathOf(submit![0])).toBe("/api/tea/sources/source-1/runs");
    expect(
      JSON.parse(submit![1]!.body as string).client_request_id,
    ).toBeTruthy();
  });

  it("reuses the idempotency key after an uncertain submission result", async () => {
    const requests: string[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "POST") {
        requests.push(
          JSON.parse(options.body as string).client_request_id as string,
        );
        if (requests.length === 1) throw new TypeError("network lost");
        return response(run, 202);
      }
      if (pathOf(url).includes("/collection-runs/"))
        return response({ run, items: [], total: 3 });
      return response({ items: [source], total: 1 });
    });
    render(<Sources compact />);
    fireEvent.click(await screen.findByRole("button", { name: "立即抓取" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "立即抓取" }));
    await screen.findByText("已存 1 / 3");
    expect(requests).toHaveLength(2);
    expect(requests[0]).toBe(requests[1]);
  });

  it("aborts outstanding progress requests when the account view unmounts", async () => {
    let progressSignal: AbortSignal | null | undefined;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (pathOf(url).includes("/collection-runs/")) {
        progressSignal = options?.signal;
        return new Promise<Response>(() => undefined);
      }
      return response({ items: [{ ...source, latest_run: run }], total: 1 });
    });
    const view = render(<Sources compact />);
    await waitFor(() => expect(progressSignal).toBeTruthy());
    view.unmount();
    expect(progressSignal?.aborted).toBe(true);
  });

  it("keeps a collection plan draft after a failed save", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) =>
      options?.method === "POST"
        ? response({ error: { code: "failed", message: "保存失败" } }, 503)
        : response({ items: [], total: 0 }),
    );
    render(<Sources compact />);
    fireEvent.click(await screen.findByRole("button", { name: "添加来源" }));
    fireEvent.change(screen.getByLabelText("来源名称"), {
      target: { value: "官方批次" },
    });
    fireEvent.change(screen.getByLabelText("公开 HTTPS 地址"), {
      target: { value: "https://example.com/list" },
    });
    fireEvent.change(screen.getByLabelText("采集组"), {
      target: { value: "ID" },
    });
    fireEvent.change(screen.getByLabelText("补充地址 · 每行一个"), {
      target: { value: "https://example.com/law-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存来源" }));
    await screen.findByText("保存失败");
    expect(screen.getByLabelText<HTMLInputElement>("来源名称").value).toBe(
      "官方批次",
    );
    expect(
      screen.getByLabelText<HTMLTextAreaElement>("补充地址 · 每行一个").value,
    ).toBe("https://example.com/law-2");
  });
});
