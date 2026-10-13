import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import { type Template } from "@/core/tea/types";
import {
  clearConfigurationDrafts,
  importConfigurationDraft,
  localConfigurationDrafts,
  useConfigurationDraft,
} from "@/core/tea/use-configuration-draft";

const template: Template = {
  id: "tea",
  version: 1,
  title: "原模板",
  description: "",
  origin: "中国",
  destination: "印度尼西亚",
  is_demo: true,
  modes: [{ id: "a", name: "模式 A", description: "", enabled: true }],
  stages: [
    { id: "prepare", name: "准备", actor: "企业", description: "", tasks: [] },
  ],
  agent: { language: "中文", instructions: "", retrieval_limit: 5, steps: [] },
};
const reply = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
beforeEach(() => clearConfigurationDrafts());
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("configuration drafts", () => {
  it("keeps a newly imported configuration editable before its first server save", async () => {
    const imported = { ...template, id: "new_workflow", title: "新业务" };
    importConfigurationDraft("owner-a", imported, null);
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        reply({ error: { code: "not_found", message: "配置不存在" } }, 404),
      )
      .mockResolvedValueOnce(
        reply({ template: imported, revision: 1, updated_at: null }),
      );
    const { result } = renderHook(() =>
      useConfigurationDraft("owner-a", imported),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.loadError).toBe("");
    expect(result.current.dirty).toBe(true);
    expect(
      localConfigurationDrafts("owner-a").map((item) => item.id),
    ).toContain("new_workflow");
    expect(localConfigurationDrafts("owner-b")).toEqual([]);
    await act(async () => {
      await result.current.save();
    });
    expect(result.current.dirty).toBe(false);
    expect(
      JSON.parse(fetcher.mock.calls[1]![1]!.body as string).expected_revision,
    ).toBe(0);
  });
  it("preserves edits and their expected revision after a save conflict", async () => {
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(reply({ template, revision: 3, updated_at: null }))
      .mockResolvedValueOnce(
        reply(
          {
            error: { code: "stale_revision", message: "另一窗口已保存新草稿" },
          },
          409,
        ),
      );
    const { result } = renderHook(() =>
      useConfigurationDraft("owner-a", template),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.update({ ...template, title: "未保存输入" }));
    await act(async () => {
      try {
        await result.current.save();
      } catch {
        /* surfaced by caller */
      }
    });
    expect(result.current.draft.title).toBe("未保存输入");
    expect(result.current.revision).toBe(3);
    expect(
      JSON.parse(fetcher.mock.calls[1]![1]!.body as string).expected_revision,
    ).toBe(3);
  });
  it("restores unsaved input across page navigation without leaking to another owner", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(async () =>
      reply({ template: null, revision: 0, updated_at: null }),
    );
    const first = renderHook(() => useConfigurationDraft("owner-a", template));
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    act(() =>
      first.result.current.update({ ...template, title: "法务正在编辑" }),
    );
    first.unmount();
    const second = renderHook(() => useConfigurationDraft("owner-a", template));
    await waitFor(() => expect(second.result.current.loading).toBe(false));
    expect(second.result.current.draft.title).toBe("法务正在编辑");
    const other = renderHook(() => useConfigurationDraft("owner-b", template));
    await waitFor(() => expect(other.result.current.loading).toBe(false));
    expect(other.result.current.draft.title).toBe("原模板");
  });
});

it("saves legal rule cards with the template and restores their exact instructions", async () => {
  const legalTemplate: Template = {
    ...template,
    agent: {
      ...template.agent,
      legal: {
        enabled: true,
        jurisdictions: ["SG"],
        question_limit: 3,
        max_rounds: 2,
        retrieval_limit: 5,
        rules: [
          {
            id: "exceptions",
            title: "例外审查",
            module: "qa05",
            enabled: true,
            execution: "manual",
            domains: ["contract"],
            jurisdictions: ["CN"],
            condition: "涉及违约",
            instructions: "区分例外与抗辩",
            required_facts: [],
            output_requirements: "附原文引用",
            review_required: true,
            source_reference: "法务规则第二章",
          },
        ],
      },
    },
  };
  let saved: Template | null = null;
  rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "PUT") {
      saved = (JSON.parse(options.body as string) as { template: Template })
        .template;
      return reply({ template: saved, revision: 1, updated_at: null });
    }
    return reply({
      template: saved,
      revision: saved ? 1 : 0,
      updated_at: null,
    });
  });
  const first = renderHook(() => useConfigurationDraft("lawyer", template));
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  act(() => first.result.current.update(legalTemplate));
  await act(async () => {
    await first.result.current.save();
  });
  first.unmount();
  clearConfigurationDrafts();
  const next = renderHook(() => useConfigurationDraft("lawyer", template));
  await waitFor(() => expect(next.result.current.loading).toBe(false));
  expect(next.result.current.draft.agent.legal).toEqual(
    legalTemplate.agent.legal,
  );
});
