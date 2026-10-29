import { afterEach, expect, it } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { clearTaskDrafts, TaskPanel } from "@/components/tea/task-panel";
import type { CaseDetail, TaskState } from "@/core/tea/types";

const task: TaskState = {
  id: "product",
  title: "产品信息",
  description: "本批产品",
  required: true,
  fields: [{ key: "name", label: "产品名称", type: "text", required: true }],
  require_document: false,
  require_confirmation: true,
  status: "pending",
  needs_review: false,
  revision: 1,
  values: {},
  evidence_ids: [],
  confirmed: false,
  verification: null,
};
const record = {
  id: "case",
  documents: [],
  stages: [{ tasks: [task] }],
} as unknown as CaseDetail;
const props = {
  record,
  locked: false,
  onUpdate: () => undefined,
  onArtifact: () => undefined,
  onUpload: () => undefined,
};
const filled: TaskState = {
  ...task,
  revision: 2,
  values: { name: "六堡茶" },
  field_sources: {
    name: {
      kind: "user_statement",
      quote: "我卖的是六堡茶",
      source_message_id: "message",
      agent_run_id: "run",
      updated_at: "2026-10-29",
    },
  },
};
afterEach(() => {
  cleanup();
  clearTaskDrafts();
});

it("refreshes the card after chat and shows its original statement without changing the input label", () => {
  const view = render(<TaskPanel {...props} task={task} />);
  view.rerender(<TaskPanel {...props} task={filled} />);
  expect(screen.getByLabelText<HTMLInputElement>("产品名称 *").value).toBe(
    "六堡茶",
  );
  expect(screen.getByText("茶渡根据对话填写 · 待核对")).toBeTruthy();
  expect(screen.getByText("来源：我卖的是六堡茶")).toBeTruthy();
  expect(
    screen.getByText("我确认以上信息和关联材料适用于本批次业务。"),
  ).toBeTruthy();
});

it("preserves an unsaved merchant edit when chat updates the same card", () => {
  const view = render(<TaskPanel {...props} task={task} />);
  fireEvent.change(screen.getByLabelText("产品名称 *"), {
    target: { value: "红茶" },
  });
  view.rerender(<TaskPanel {...props} task={filled} />);
  expect(screen.getByLabelText<HTMLInputElement>("产品名称 *").value).toBe(
    "红茶",
  );
  expect(screen.getByText(/你的未保存输入已保留/)).toBeTruthy();
  expect(screen.queryByText("茶渡根据对话填写 · 待核对")).toBeNull();
  fireEvent.click(
    screen.getByRole("button", { name: "放弃本地修改，载入最新资料" }),
  );
  expect(screen.getByLabelText<HTMLInputElement>("产品名称 *").value).toBe(
    "六堡茶",
  );
  expect(screen.getByText("茶渡根据对话填写 · 待核对")).toBeTruthy();
});
