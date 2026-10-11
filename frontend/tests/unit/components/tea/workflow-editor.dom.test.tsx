import { afterEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { useState } from "react";

import { WorkflowEditor } from "@/components/tea/workflow-editor";
import { type Template } from "@/core/tea/types";

const template: Template = {
  id: "tea",
  version: 1,
  title: "茶叶出口准备",
  description: "准备说明",
  origin: "中国",
  destination: "印度尼西亚",
  is_demo: true,
  modes: [{ id: "a", name: "模式 A", description: "适用情况", enabled: true }],
  stages: [
    {
      id: "prepare",
      name: "资料准备",
      actor: "企业",
      description: "补齐信息",
      tasks: [
        {
          id: "product",
          title: "产品信息",
          description: "填写产品资料",
          required: true,
          require_document: false,
          require_confirmation: true,
          fields: [
            { key: "name", label: "产品名称", type: "text", required: true },
            {
              key: "weight",
              label: "净含量",
              type: "number",
              required: true,
              min: 0,
            },
          ],
        },
        {
          id: "contact",
          title: "联系信息",
          description: "填写联系人",
          required: true,
          require_document: false,
          require_confirmation: true,
          fields: [],
        },
      ],
    },
    {
      id: "delivery",
      name: "材料交付",
      actor: "接收主体",
      description: "交付目标",
      tasks: [],
    },
  ],
  agent: { language: "中文", instructions: "", retrieval_limit: 5, steps: [] },
};

afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
});

function Editor({
  onDraft = () => undefined,
}: {
  onDraft?: (draft: Template) => void;
}) {
  const [draft, setDraft] = useState(template);
  const [stage, setStage] = useState("prepare");
  return (
    <>
      <button onClick={() => setStage("delivery")}>导航到材料交付</button>
      <WorkflowEditor
        draft={draft}
        onChange={(value) => {
          setDraft(value);
          onDraft(value);
        }}
        stageId={stage}
        onStage={setStage}
        onAnalyze={() => undefined}
      />
    </>
  );
}

describe("workflow editor", () => {
  it("edits a deeply selected field in one inspector and retains it across stage navigation", () => {
    const changed = rs.fn();
    render(<Editor onDraft={changed} />);
    const tree = within(screen.getByRole("region", { name: "流程结构" }));
    const inspector = within(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    );
    expect(inspector.queryByLabelText("显示名称")).toBeNull();
    fireEvent.click(tree.getByRole("button", { name: "展开任务：产品信息" }));
    fireEvent.click(tree.getByRole("button", { name: "产品名称" }));
    expect(inspector.queryByLabelText("节点名称")).toBeNull();
    fireEvent.change(inspector.getByLabelText("显示名称"), {
      target: { value: "商品名称" },
    });
    expect(
      changed.mock.calls.at(-1)?.[0].stages[0].tasks[0].fields[0].label,
    ).toBe("商品名称");
    fireEvent.click(screen.getByRole("button", { name: "导航到材料交付" }));
    expect(inspector.getByLabelText<HTMLInputElement>("节点名称").value).toBe(
      "材料交付",
    );
    expect(inspector.queryByLabelText("显示名称")).toBeNull();
    fireEvent.click(tree.getByRole("button", { name: "资料准备" }));
    fireEvent.click(tree.getByRole("button", { name: "商品名称" }));
    expect(inspector.getByLabelText<HTMLInputElement>("显示名称").value).toBe(
      "商品名称",
    );
    fireEvent.click(screen.getByRole("button", { name: "返回流程列表" }));
    expect(tree.getByRole("button", { name: "商品名称" })).toBeTruthy();
  });

  it("keeps task selection when reordering and requires confirmation before removing the task", () => {
    const changed = rs.fn();
    render(<Editor onDraft={changed} />);
    const tree = within(screen.getByRole("region", { name: "流程结构" }));
    const inspector = within(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    );
    fireEvent.click(tree.getByRole("button", { name: "产品信息" }));
    fireEvent.click(inspector.getByRole("button", { name: "任务后移" }));
    expect(inspector.getByLabelText<HTMLInputElement>("任务名称").value).toBe(
      "产品信息",
    );
    expect(
      changed.mock.calls
        .at(-1)?.[0]
        .stages[0].tasks.map((task: { id: string }) => task.id),
    ).toEqual(["contact", "product"]);
    fireEvent.click(inspector.getByRole("button", { name: "删除任务" }));
    expect(changed.mock.calls.at(-1)?.[0].stages[0].tasks).toHaveLength(2);
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "确认",
      }),
    );
    expect(tree.queryByRole("button", { name: "产品信息" })).toBeNull();
    expect(inspector.getByLabelText<HTMLInputElement>("节点名称").value).toBe(
      "资料准备",
    );
    expect(changed.mock.calls.at(-1)?.[0].stages[0].tasks[0].id).toBe(
      "contact",
    );
  });
});
