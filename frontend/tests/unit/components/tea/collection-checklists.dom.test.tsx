import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { Sources } from "@/components/tea/sources";
import { clearCollectionDrafts } from "@/core/tea/collection-drafts";

const item = {
  id: "1.1",
  chapter_id: "1",
  title: "主法规采集",
  kind: "law",
  conditional: false,
  trigger: "",
  priority: 1,
  entries: [
    {
      url: "https://example.com/catalog",
      purpose: "catalog",
      note: "官方目录",
    },
  ],
  tags: ["ID", "1.1"],
  scope_include: ["现行与历史法规正文"],
  scope_exclude: ["新闻"],
  steps: ["遍历目录后核对原文"],
  fields: ["名称", "发布日期"],
  version_rules: ["保留旧版"],
  relation_rules: ["按主法规及引用关系分级"],
  acceptance: ["原件与条文均可追溯"],
  verbatim_requirements: "完整保存条文",
  execution: "discovery",
  adapter: "html",
  limits: { max_pages: 30, max_candidates: 200 },
};
const plan = {
  id: "indonesia",
  version: 1,
  country: "ID",
  title: "印尼法律采集清单",
  description: "按清单逐项整理法规。",
  source_document: {
    filename: "印尼采集清单.docx",
    date: "2026-10-09",
    sha256: "abc",
  },
  item_count: 2,
  chapter_count: 1,
  principles: ["先核对范围，再确认版本。"],
  chapters: [{ id: "1", title: "国家立法", notes: ["本章覆盖法案与法规。"] }],
  channels: [],
  limits: {},
  items: [
    item,
    {
      ...item,
      id: "1.2",
      title: "条件性法规",
      conditional: true,
      trigger: "业务涉及进口包装",
      entries: [
        {
          url: "https://example.com/example.pdf",
          purpose: "reference",
          note: "仅作示范",
        },
      ],
    },
  ],
};
const state = (code = "1.1") => ({
  item_id: code,
  source_id: null,
  latest_run: null,
  counts: {
    pages: 0,
    candidates: 1,
    pending: 1,
    stored: 0,
    parsed: 0,
    reviewed: 0,
    excluded: 0,
  },
  coverage: "not_started",
  last_error: null,
});
const candidate = {
  id: "candidate-1",
  url: "https://example.com/law",
  title: "待核对法规",
  discovered_from: "https://example.com/catalog",
  status: "pending",
  document_id: null,
  revision: 1,
  note: "",
  created_at: "2026-10-12T01:00:00Z",
};
const run = {
  id: "run-1",
  source_id: "source-1",
  status: "queued",
  revision: 1,
  created_at: "2026-10-12T01:00:00Z",
  updated_at: "2026-10-12T01:00:00Z",
  error: null,
  total: 1,
  stored: 0,
  pending: 1,
  failed: 0,
  processing: 0,
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
function reads(url: RequestInfo | URL) {
  const path = pathOf(url);
  if (path.endsWith("/collection-plans"))
    return response({
      items: [
        plan,
        { ...plan, id: "singapore", country: "SG", title: "新加坡采集清单" },
      ],
      total: 2,
    });
  if (path.includes("/candidates?"))
    return response({ items: [candidate], total: 1 });
  if (path.includes("/collection-runs/"))
    return response({
      run: { ...run, status: "completed", stored: 1, pending: 0 },
      items: [],
      total: 1,
    });
  if (path.includes("/collection-plans/"))
    return response({ plan, items: [state(), state("1.2")] });
  if (path.includes("/documents?"))
    return response({
      items: [
        {
          id: "primary-a",
          title: "主法规甲",
          version: 3,
          legal_level: "primary",
        },
      ],
      total: 1,
    });
  return response({ items: [], total: 0 });
}
async function openItem(name = "1.1 主法规采集") {
  fireEvent.click(await screen.findByRole("button", { name }));
}
afterEach(() => {
  cleanup();
  clearCollectionDrafts();
  rs.restoreAllMocks();
});

describe("document-driven collection checklists", () => {
  it("shows each country and chapter before sources exist, and configures the selected version", async () => {
    let configured = false;
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) => {
        if (options?.method === "POST") {
          configured = true;
          return response({ created: 2, existing: 0 });
        }
        if (configured && pathOf(url).endsWith("/collection-plans/indonesia"))
          return response({
            plan,
            items: [state(), state("1.2")].map((item) => ({
              ...item,
              source_id: `source-${item.item_id}`,
            })),
          });
        return reads(url);
      });
    render(<Sources compact ownerId="owner-one" />);
    expect(await screen.findByRole("button", { name: /印尼/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /新加坡/ })).toBeTruthy();
    expect(await screen.findByText("国家立法")).toBeTruthy();
    expect(screen.getByRole("button", { name: "1.2 条件性法规" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "配置本清单" }));
    await screen.findByText(/章 · 2 项 · 已配置 2 项/);
    const submit = fetcher.mock.calls.find(
      ([, options]) => options?.method === "POST",
    )!;
    expect(pathOf(submit[0])).toBe(
      "/api/tea/collection-plans/indonesia/configure",
    );
    expect(JSON.parse(submit[1]!.body as string)).toEqual({
      expected_version: 1,
    });
  });
  it("starts the country plan's discovery batch and preserves its idempotency key on retry", async () => {
    const writes: { url: string; body: Record<string, unknown> }[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "POST") {
        writes.push({
          url: pathOf(url),
          body: JSON.parse(options.body as string) as Record<string, unknown>,
        });
        if (writes.length === 1) throw new TypeError("network lost");
        return response(
          {
            runs: [run],
            started: 1,
            skipped: [{ item_id: "1.2", reason: "等待业务条件确认" }],
          },
          202,
        );
      }
      return reads(url);
    });
    render(<Sources compact ownerId="owner-one" />);
    fireEvent.click(await screen.findByRole("button", { name: "发现全部" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "发现全部" }));
    await screen.findByText("已启动 1 项，跳过 1 项");
    expect(writes[0]!.url).toBe("/api/tea/collection-plans/indonesia/discover");
    expect(writes[1]!.body.client_request_id).toBe(
      writes[0]!.body.client_request_id,
    );
  });

  it("discovers directory pages separately and never labels them as stored law text", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) =>
        options?.method === "POST"
          ? response({ ...run, kind: "discovery", pages: 0 }, 202)
          : pathOf(url).includes("/collection-runs/")
            ? response({
                run: {
                  ...run,
                  kind: "discovery",
                  status: "completed",
                  pages: 1,
                  pending: 0,
                },
                items: [],
                total: 1,
              })
            : reads(url),
      );
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    expect(screen.getByText("目录")).toBeTruthy();
    expect(screen.getByText("目录只用于发现候选，确认后才入库。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发现候选" }));
    await screen.findByText("已读 1 / 1 页");
    expect(screen.queryByText("已存 1 / 1")).toBeNull();
    const submit = fetcher.mock.calls.find(
      ([, options]) => options?.method === "POST",
    )!;
    expect(pathOf(submit[0])).toContain("/items/1.1/discover");
  });
  it("requires an explicit business condition before starting a conditional item", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) =>
        options?.method === "POST" ? response(run, 202) : reads(url),
      );
    render(<Sources compact ownerId="owner-one" />);
    await openItem("1.2 条件性法规");
    expect(screen.getByText("示范 / 参考")).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "发现候选" })
        .disabled,
    ).toBe(true);
    fireEvent.click(screen.getByLabelText("已确认：业务涉及进口包装"));
    fireEvent.click(screen.getByRole("button", { name: "发现候选" }));
    await waitFor(() =>
      expect(fetcher.mock.calls.some(([, o]) => o?.method === "POST")).toBe(
        true,
      ),
    );
    const submit = fetcher.mock.calls.find(
      ([, options]) => options?.method === "POST",
    )!;
    expect(JSON.parse(submit[1]!.body as string).confirm_condition).toBe(true);
  });
  it("collects only selected candidates with an explicit secondary mapping", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url, options) =>
        options?.method === "POST" ? response(run, 202) : reads(url),
      );
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    fireEvent.click(await screen.findByLabelText("选择 待核对法规"));
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "采集所选 1 项" })
        .disabled,
    ).toBe(true);
    fireEvent.change(screen.getByLabelText("本次资料级别"), {
      target: { value: "secondary" },
    });
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "采集所选 1 项" })
        .disabled,
    ).toBe(true);
    fireEvent.click(await screen.findByLabelText(/主法规甲/));
    fireEvent.click(screen.getByRole("button", { name: "采集所选 1 项" }));
    await waitFor(() =>
      expect(fetcher.mock.calls.some(([, o]) => o?.method === "POST")).toBe(
        true,
      ),
    );
    const submit = fetcher.mock.calls.find(
      ([, options]) => options?.method === "POST",
    )!;
    expect(pathOf(submit[0])).toContain("/items/1.1/collect");
    expect(JSON.parse(submit[1]!.body as string)).toMatchObject({
      candidate_ids: ["candidate-1"],
      level: "secondary",
      primary_document_ids: ["primary-a"],
    });
    expect(
      fetcher.mock.calls.some(([url]) =>
        pathOf(url).includes("collection=country%3AID"),
      ),
    ).toBe(true);
  });
  it("checks an already stored original for a new version through the same bounded collection API", async () => {
    const writes: Record<string, unknown>[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "POST") {
        writes.push(
          JSON.parse(options.body as string) as Record<string, unknown>,
        );
        return response(run, 202);
      }
      if (pathOf(url).includes("status=stored"))
        return response({
          items: [
            { ...candidate, status: "stored", document_id: "old-version" },
          ],
          total: 1,
        });
      return reads(url);
    });
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    fireEvent.click(screen.getByRole("button", { name: "已入库" }));
    fireEvent.click(await screen.findByLabelText("选择 待核对法规"));
    fireEvent.change(screen.getByLabelText("本次资料级别"), {
      target: { value: "primary" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检查新版本（1 项）" }));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({
      candidate_ids: ["candidate-1"],
      level: "primary",
    });
  });

  it("retains selection and its idempotency key after uncertain submission", async () => {
    const submissions: Record<string, unknown>[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "POST") {
        submissions.push(
          JSON.parse(options.body as string) as Record<string, unknown>,
        );
        if (submissions.length === 1) throw new TypeError("connection lost");
        return response(run, 202);
      }
      return reads(url);
    });
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    fireEvent.click(await screen.findByLabelText("选择 待核对法规"));
    fireEvent.change(screen.getByLabelText("本次资料级别"), {
      target: { value: "primary" },
    });
    fireEvent.click(screen.getByRole("button", { name: "采集所选 1 项" }));
    await screen.findByRole("alert");
    expect(
      screen.getByLabelText<HTMLInputElement>("选择 待核对法规").checked,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "采集所选 1 项" }));
    await waitFor(() => expect(submissions).toHaveLength(2));
    expect(submissions[0]!.client_request_id).toBe(
      submissions[1]!.client_request_id,
    );
  });
  it("aborts old reads and isolates selections when the account changes", async () => {
    let pendingSignal: AbortSignal | null | undefined;
    let finish!: (value: Response) => void;
    let candidateReads = 0;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (pathOf(url).includes("/candidates?")) {
        candidateReads += 1;
        if (candidateReads === 1) {
          pendingSignal = options?.signal;
          return new Promise<Response>((resolve) => {
            finish = resolve;
          });
        }
        return response({ items: [], total: 0 });
      }
      return reads(url);
    });
    const view = render(<Sources compact ownerId="owner-one" />);
    await openItem();
    await waitFor(() => expect(pendingSignal).toBeTruthy());
    view.rerender(<Sources compact ownerId="owner-two" />);
    expect(pendingSignal?.aborted).toBe(true);
    await openItem();
    await act(async () => {
      finish(response({ items: [candidate], total: 1 }));
    });
    expect(screen.queryByLabelText("选择 待核对法规")).toBeNull();
  });
  it("returns to the last available page after excluding the only candidate on page two", async () => {
    let excluded = false;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (options?.method === "PATCH") {
        excluded = true;
        return response({ ...candidate, status: "excluded", revision: 2 });
      }
      if (pathOf(url).includes("/candidates?")) {
        const offset = Number(
          new URL(pathOf(url), "http://localhost").searchParams.get("offset"),
        );
        return response({
          items: offset
            ? excluded
              ? []
              : [candidate]
            : [{ ...candidate, id: "first-page", title: "第一页法规" }],
          total: excluded ? 20 : 21,
        });
      }
      return reads(url);
    });
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    await screen.findByLabelText("选择 第一页法规");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByLabelText("选择 待核对法规");
    fireEvent.click(screen.getByRole("button", { name: "排除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认排除" }));
    expect(await screen.findByLabelText("选择 第一页法规")).toBeTruthy();
  });

  it("keeps a completed discovery batch resumable when frontier pages remain", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      pathOf(url).endsWith("/collection-plans/indonesia")
        ? response({
            plan,
            items: [
              {
                ...state(),
                counts: { ...state().counts, pages: 40, remaining_pages: 12 },
              },
            ],
          })
        : reads(url),
    );
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    expect(screen.getByText("待续页面 12")).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "继续发现" })
        .disabled,
    ).toBe(false);
  });

  it("keeps dataset review requirements visible without generic collection actions", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      pathOf(url).endsWith("/collection-plans/indonesia")
        ? response({
            plan: {
              ...plan,
              items: [{ ...item, execution: "dataset_review" }],
            },
            items: [state()],
          })
        : reads(url),
    );
    render(<Sources compact ownerId="owner-one" />);
    await openItem();
    expect(screen.getByText("候选命题需核验")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "发现候选" })).toBeNull();
    expect(screen.queryByRole("button", { name: /采集所选/ })).toBeNull();
  });
  it("keeps bound source plans read-only in the custom source list", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      pathOf(url).includes("/sources?")
        ? response({
            items: [
              {
                id: "bound-source",
                name: "绑定来源",
                url: "https://example.com/list",
                enabled: false,
                interval_hours: 24,
                tags: [],
                checklist: {
                  plan_id: "indonesia",
                  item_id: "1.1",
                  country: "ID",
                  version: 1,
                },
                status: "idle",
                last_error: null,
                last_fetched_at: null,
              },
            ],
            total: 1,
          })
        : reads(url),
    );
    render(<Sources compact ownerId="owner-one" />);
    fireEvent.click(screen.getByRole("button", { name: "自定义来源" }));
    await screen.findByText("绑定来源");
    expect(screen.queryByRole("button", { name: "配置" })).toBeNull();
    expect(screen.queryByRole("button", { name: "立即抓取" })).toBeNull();
    expect(screen.getByLabelText<HTMLInputElement>("启用").disabled).toBe(true);
  });

  it("never requests checklist APIs in the read-only samples view", async () => {
    const fetcher = rs.spyOn(globalThis, "fetch");
    render(<Sources compact samples ownerId="owner-one" />);
    await screen.findByText("产品标签资料 · 开发样例");
    expect(fetcher).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "配置本清单" })).toBeNull();
  });
});
