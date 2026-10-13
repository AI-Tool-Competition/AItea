import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { LegalWorkbench } from "@/components/tea/legal-workbench";
import {
  type CaseDetail,
  type LegalAnalysis,
  type LegalFact,
} from "@/core/tea/types";
import { clearLegalDrafts } from "@/core/tea/use-legal-workbench";

const fact: LegalFact = {
  key: "delivery",
  label: "交付日期",
  value: "2026-10-01",
  source: "用户说明",
  confirmed: false,
  extraction_confidence: "unknown",
  evidence_status: "unverified",
  evidence_refs: [],
  conflicts: [],
  revision: 1,
};
const analysis: LegalAnalysis = {
  id: "analysis-one",
  case_id: "case-one",
  stage_id: "prepare",
  question: "交付期限？",
  revision: 1,
  status: "needs_input",
  created_at: "2026-10-12T00:00:00Z",
  updated_at: "2026-10-12T00:00:00Z",
  affected_issue_ids: [],
  error: null,
  sections: [
    {
      id: "report",
      issue_id: "issue",
      title: "交付建议",
      content: "人工保留段落",
      generated_content: "旧建议",
      suggested_content: "新分析建议",
      edited: true,
      needs_review: true,
    },
  ],
  result: {
    question: "交付期限？",
    issues: [],
    facts: [fact],
    followups: [],
    sections: [],
    trace: [],
    gaps: [],
    assistant_mode: "demo",
    output: "待补充",
    citations: [],
  },
};
const record = {
  id: "case-one",
  revision: 1,
  documents: [],
  template: { agent: {} },
  stages: [{ id: "prepare", status: "active" }],
} as unknown as CaseDetail;
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
const history = { facts: [fact], analyses: [analysis], total: 1 };
function mockApi(selected = analysis) {
  return rs
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (path, options) => {
      if (options?.method === "PATCH")
        return response(
          { error: { code: "stale_revision", message: "事实已有新版本" } },
          409,
        );
      if (
        (typeof path === "string"
          ? path
          : path instanceof URL
            ? path.href
            : path.url
        ).includes("/analyses/")
      )
        return response(selected);
      return response(history);
    });
}
beforeEach(() => clearLegalDrafts());
afterEach(() => {
  cleanup();
  clearLegalDrafts();
  rs.restoreAllMocks();
});
describe("legal analysis workbench", () => {
  it("retains a fact draft after a revision conflict and separates confirmation from evidence review", async () => {
    const fetcher = mockApi();
    render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    await waitFor(() => expect(screen.getByLabelText("交付日期")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("交付日期"), {
      target: { value: "2026-10-09" },
    });
    fireEvent.click(screen.getByLabelText("我确认这项事实"));
    fireEvent.click(screen.getByRole("button", { name: "保存事实：交付日期" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("输入已保留"),
    );
    expect(screen.getByLabelText<HTMLInputElement>("交付日期").value).toBe(
      "2026-10-09",
    );
    const request = fetcher.mock.calls.find(
      ([, options]) => options?.method === "PATCH",
    )!;
    expect(JSON.parse(request[1]!.body as string)).toMatchObject({
      expected_revision: 1,
      value: "2026-10-09",
      confirmed: true,
      evidence_status: "unverified",
    });
  });
  it("groups follow-up facts into three questions without discarding later questions", async () => {
    const selected = structuredClone(analysis);
    selected.result.followups = [1, 2, 3, 4].map((n) => ({
      key: `fact_${n}`,
      label: `补充事实 ${n}`,
      purpose: "确认条件",
      decisive: true,
    }));
    mockApi(selected);
    render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("补充事实 1")).toBeTruthy(),
    );
    expect(screen.getByLabelText("补充事实 3")).toBeTruthy();
    expect(screen.queryByLabelText("补充事实 4")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "下一组问题" }));
    expect(screen.getByLabelText("补充事实 4")).toBeTruthy();
  });
  it("preserves manual report edits when a refreshed analysis offers a new suggestion", async () => {
    const refreshed = structuredClone(analysis);
    refreshed.revision = 2;
    refreshed.sections[0]!.suggested_content = "重新分析后的建议";
    mockApi();
    render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("报告段落：交付建议")).toBeTruthy(),
    );
    fireEvent.change(screen.getByLabelText("报告段落：交付建议"), {
      target: { value: "尚未保存的人工判断" },
    });
    rs.mocked(globalThis.fetch).mockImplementation(async (path, options) =>
      options?.method === "POST"
        ? new Response(
            `event: done\ndata: ${JSON.stringify({ analysis: refreshed })}\n\n`,
            { headers: { "Content-Type": "text/event-stream" } },
          )
        : response(
            (typeof path === "string"
              ? path
              : path instanceof URL
                ? path.href
                : path.url
            ).includes("/analyses/")
              ? refreshed
              : history,
          ),
    );
    fireEvent.click(screen.getByRole("button", { name: "重新分析受影响问题" }));
    await waitFor(() =>
      expect(screen.getByText("重新分析后的建议")).toBeTruthy(),
    );
    expect(
      screen.getByLabelText<HTMLTextAreaElement>("报告段落：交付建议").value,
    ).toBe("尚未保存的人工判断");
    expect(
      screen
        .getByRole("button", {
          name: "接受建议：交付建议",
        })
        .hasAttribute("disabled"),
    ).toBe(true);
  });
  it("fences late responses on account and case changes and preserves only the matching question draft", async () => {
    let resolveOld!: (value: Response) => void;
    let oldSignal: AbortSignal | undefined;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (path, options) => {
      if (
        (typeof path === "string"
          ? path
          : path instanceof URL
            ? path.href
            : path.url
        ).includes("case-one")
      ) {
        oldSignal = options?.signal ?? undefined;
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      }
      return response({ facts: [], analyses: [], total: 0 });
    });
    const view = render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    fireEvent.change(screen.getByLabelText("法律分析问题"), {
      target: { value: "我的私人问题" },
    });
    view.rerender(
      <LegalWorkbench
        accountId="other"
        record={{ ...record, id: "case-two" }}
        stageId="prepare"
      />,
    );
    await act(async () =>
      resolveOld(
        response({
          facts: [{ ...fact, label: "旧账号秘密" }],
          analyses: [],
          total: 0,
        }),
      ),
    );
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.queryByLabelText("旧账号秘密")).toBeNull();
    expect(
      screen.getByLabelText<HTMLTextAreaElement>("法律分析问题").value,
    ).toBe("");
    view.rerender(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    expect(
      screen.getByLabelText<HTMLTextAreaElement>("法律分析问题").value,
    ).toBe("我的私人问题");
    view.unmount();
    clearLegalDrafts();
    render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    expect(
      screen.getByLabelText<HTMLTextAreaElement>("法律分析问题").value,
    ).toBe("");
  });
});

it("keeps a newly added fact draft when the panel unmounts and opens again", async () => {
  mockApi();
  const view = render(
    <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
  );
  await waitFor(() => expect(screen.getByLabelText("交付日期")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("事实名称"), {
    target: { value: "选择的仲裁机构" },
  });
  fireEvent.click(screen.getByRole("button", { name: "添加事实" }));
  fireEvent.change(screen.getByLabelText("选择的仲裁机构"), {
    target: { value: "尚待双方确认" },
  });
  view.unmount();
  render(
    <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
  );
  expect(screen.getByLabelText<HTMLInputElement>("选择的仲裁机构").value).toBe(
    "尚待双方确认",
  );
});

it.each(["account", "case", "stage"])(
  "ignores old reads after only the %s changes",
  async (scope) => {
    let resolveOld!: (value: Response) => void;
    let oldSignal: AbortSignal | undefined;
    let requests = 0;
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_path, options) => {
      requests++;
      if (requests === 1) {
        oldSignal = options?.signal ?? undefined;
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      }
      return response({ facts: [], analyses: [], total: 0 });
    });
    const view = render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    view.rerender(
      <LegalWorkbench
        accountId={scope === "account" ? "other" : "owner"}
        record={scope === "case" ? { ...record, id: "case-two" } : record}
        stageId={scope === "stage" ? "next" : "prepare"}
      />,
    );
    await act(async () =>
      resolveOld(
        response({
          facts: [{ ...fact, label: "已离开的范围" }],
          analyses: [],
          total: 0,
        }),
      ),
    );
    expect(oldSignal?.aborted).toBe(true);
    expect(screen.queryByLabelText("已离开的范围")).toBeNull();
  },
);

it("refreshes the shared case revision after saving a legal fact", async () => {
  const onUpdate = rs.fn();
  const refreshed = { ...record, revision: 2 };
  rs.spyOn(globalThis, "fetch").mockImplementation(async (path, options) => {
    if (options?.method === "PATCH")
      return response({
        fact: { ...fact, value: "2026-10-09", revision: 2 },
        affected_analysis_ids: [analysis.id],
      });
    if (path === "/api/tea/cases/case-one") return response(refreshed);
    if (typeof path === "string" && path.includes("/analyses/"))
      return response(analysis);
    return response(history);
  });
  render(
    <LegalWorkbench
      accountId="owner"
      record={record}
      stageId="prepare"
      onUpdate={onUpdate}
    />,
  );
  await waitFor(() => expect(screen.getByLabelText("交付日期")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("交付日期"), {
    target: { value: "2026-10-09" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存事实：交付日期" }));
  await waitFor(() => expect(onUpdate).toHaveBeenCalledWith(refreshed));
});

it.each([
  {
    name: "a replacement section ID",
    id: "replacement",
    title: "新争点建议",
    issue_id: "new-issue",
  },
  {
    name: "the same ID with a new title",
    id: "report",
    title: "重新分类的争点",
    issue_id: "issue",
  },
  {
    name: "the same ID assigned to a new issue",
    id: "report",
    title: "交付建议",
    issue_id: "new-issue",
  },
])(
  "retains the original unsaved section separately after $name",
  async ({ id, title, issue_id }) => {
    let server = structuredClone(analysis);
    const patches: unknown[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (path, options) => {
      if (options?.method === "PATCH") {
        const input = JSON.parse(options.body as string) as {
          content: string;
          expected_revision: number;
        };
        patches.push(input);
        server = {
          ...server,
          revision: server.revision + 1,
          sections: server.sections.map((section) => ({
            ...section,
            content: input.content,
          })),
        };
        return response(server);
      }
      return response(
        typeof path === "string" && path.includes("/analyses/")
          ? server
          : { ...history, analyses: [server] },
      );
    });
    const view = render(
      <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("报告段落：交付建议")).toBeTruthy(),
    );
    fireEvent.change(screen.getByLabelText("报告段落：交付建议"), {
      target: { value: "原争点尚未保存的判断" },
    });
    server = {
      ...server,
      revision: 2,
      sections: [
        {
          ...server.sections[0]!,
          id,
          title,
          issue_id,
          content: "新争点的服务器文本",
          suggested_content: null,
        },
      ],
    };
    view.rerender(
      <LegalWorkbench
        accountId="owner"
        record={{ ...record, revision: 2 }}
        stageId="prepare"
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByLabelText<HTMLTextAreaElement>(`报告段落：${title}`).value,
      ).toBe("新争点的服务器文本"),
    );
    expect(
      screen.getByLabelText<HTMLTextAreaElement>("原争点草稿：交付建议").value,
    ).toBe("原争点尚未保存的判断");
    expect(patches).toHaveLength(0);
    fireEvent.change(screen.getByLabelText(`报告段落：${title}`), {
      target: { value: "给新争点的独立意见" },
    });
    fireEvent.click(screen.getByRole("button", { name: `保存段落：${title}` }));
    await waitFor(() =>
      expect(patches).toEqual([
        { content: "给新争点的独立意见", expected_revision: 2 },
      ]),
    );
    view.unmount();
    render(
      <LegalWorkbench
        accountId="owner"
        record={{ ...record, revision: 2 }}
        stageId="prepare"
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByLabelText<HTMLTextAreaElement>("原争点草稿：交付建议")
          .value,
      ).toBe("原争点尚未保存的判断"),
    );
    expect(
      screen.getByLabelText<HTMLTextAreaElement>(`报告段落：${title}`).value,
    ).toBe("给新争点的独立意见");
  },
);

it("keeps an unsaved section accessible when the refreshed report contains no sections", async () => {
  let server = structuredClone(analysis);
  rs.spyOn(globalThis, "fetch").mockImplementation(async (path) =>
    response(
      typeof path === "string" && path.includes("/analyses/")
        ? server
        : { ...history, analyses: [server] },
    ),
  );
  const view = render(
    <LegalWorkbench accountId="owner" record={record} stageId="prepare" />,
  );
  await waitFor(() =>
    expect(screen.getByLabelText("报告段落：交付建议")).toBeTruthy(),
  );
  fireEvent.change(screen.getByLabelText("报告段落：交付建议"), {
    target: { value: "报告为空也保留这段输入" },
  });
  server = { ...server, revision: 2, sections: [] };
  view.rerender(
    <LegalWorkbench
      accountId="owner"
      record={{ ...record, revision: 2 }}
      stageId="prepare"
    />,
  );
  await waitFor(() =>
    expect(screen.queryByLabelText("报告段落：交付建议")).toBeNull(),
  );
  const retained =
    screen.getByLabelText<HTMLTextAreaElement>("原争点草稿：交付建议");
  expect(retained.value).toBe("报告为空也保留这段输入");
  fireEvent.focus(retained);
  expect(retained.selectionEnd).toBe(retained.value.length);
  fireEvent.click(screen.getByRole("button", { name: "丢弃草稿：交付建议" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.getByLabelText("原争点草稿：交付建议")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "丢弃草稿：交付建议" }));
  fireEvent.click(screen.getByRole("button", { name: "确认" }));
  await waitFor(() =>
    expect(screen.queryByLabelText("原争点草稿：交付建议")).toBeNull(),
  );
});
