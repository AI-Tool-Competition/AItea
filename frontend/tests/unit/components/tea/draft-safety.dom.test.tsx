import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import {
  ArtifactPanel,
  clearArtifactDrafts,
} from "@/components/tea/artifact-panel";
import { TaskPanel, clearTaskDrafts } from "@/components/tea/task-panel";
import {
  type Artifact,
  type CaseDetail,
  type TaskState,
} from "@/core/tea/types";

const artifact: Artifact = {
  id: "draft-one",
  case_id: "case-one",
  task_id: "profile",
  title: "产品信息卡",
  content: "初始内容",
  version: 1,
  confirmed: false,
  created_at: "2026-10-11T00:00:00Z",
  updated_at: "2026-10-11T00:00:00Z",
};
const task: TaskState = {
  id: "profile",
  title: "产品信息",
  description: "核对本批次产品",
  required: true,
  require_document: false,
  require_confirmation: false,
  fields: [{ key: "name", label: "产品名称", type: "text", required: true }],
  status: "pending",
  needs_review: false,
  revision: 1,
  values: { name: "乌龙茶" },
  evidence_ids: [],
  confirmed: false,
  verification: null,
};
const record = {
  id: "case-one",
  stages: [{ id: "stage", tasks: [task] }],
  documents: [],
} as unknown as CaseDetail;

beforeEach(() => {
  clearArtifactDrafts();
  clearTaskDrafts();
});
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

describe("Draft and revision safety", () => {
  it("does not block page navigation when no artifact exists", () => {
    const listener = rs.spyOn(window, "addEventListener");
    render(<ArtifactPanel onSaved={() => undefined} />);
    expect(listener.mock.calls.some((call) => call[0] === "beforeunload")).toBe(
      false,
    );
  });
  it("retains edited artifact text and its original revision across internal navigation", async () => {
    const first = render(
      <ArtifactPanel artifact={artifact} onSaved={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("正文 · 支持 Markdown"), {
      target: { value: "用户尚未保存的文字" },
    });
    first.unmount();
    const fetcher = rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { code: "stale_revision", message: "版本冲突" },
        }),
        { status: 409 },
      ),
    );
    render(
      <ArtifactPanel
        artifact={{ ...artifact, version: 2, content: "另一窗口保存的文字" }}
        onSaved={() => undefined}
      />,
    );
    expect(screen.getByLabelText<HTMLTextAreaElement>("正文 · 支持 Markdown").value).toBe(
      "用户尚未保存的文字",
    );
    fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    expect(
      JSON.parse(fetcher.mock.calls[0]![1]!.body as string).expected_version,
    ).toBe(1);
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("版本冲突"),
    );
    expect(screen.getByLabelText<HTMLTextAreaElement>("正文 · 支持 Markdown").value).toBe(
      "用户尚未保存的文字",
    );
  });
  it("saves changed task inputs before requesting a draft", async () => {
    const saved = {
      ...record,
      stages: [
        {
          id: "stage",
          tasks: [{ ...task, revision: 2, values: { name: "红茶" } }],
        },
      ],
    };
    const fetcher = rs
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify(saved), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(artifact), { status: 200 }),
      );
    const updated = rs.fn();
    const drafted = rs.fn();
    render(
      <TaskPanel
        task={task}
        record={record}
        locked={false}
        onUpdate={updated}
        onArtifact={drafted}
        onUpload={() => undefined}
      />,
    );
    fireEvent.change(screen.getByLabelText(/产品名称/), {
      target: { value: "红茶" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "根据当前资料起草文稿" }),
    );
    await waitFor(() => expect(drafted).toHaveBeenCalledTimes(1));
    expect(fetcher.mock.calls[0]![0]).toBe(
      "/api/tea/cases/case-one/tasks/profile",
    );
    expect(fetcher.mock.calls[0]![1]!.method).toBe("PATCH");
    expect(fetcher.mock.calls[1]![0]).toBe("/api/tea/cases/case-one/artifacts");
    expect(updated).toHaveBeenCalledWith(saved);
  });
  it("preserves task input when switching away and returning", () => {
    const props = {
      task,
      record,
      locked: false,
      onUpdate: () => undefined,
      onArtifact: () => undefined,
      onUpload: () => undefined,
    };
    const first = render(<TaskPanel {...props} />);
    fireEvent.change(screen.getByLabelText(/产品名称/), {
      target: { value: "未保存的茶叶名称" },
    });
    first.unmount();
    render(<TaskPanel {...props} />);
    expect(screen.getByLabelText<HTMLInputElement>(/产品名称/).value).toBe("未保存的茶叶名称");
  });
});
