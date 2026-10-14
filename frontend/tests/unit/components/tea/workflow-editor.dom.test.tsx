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
  it("keeps a required closeout task while allowing later stages to proceed", () => {
    const changed = rs.fn();
    render(<Editor onDraft={changed} />);
    fireEvent.click(screen.getByRole("button", { name: "产品信息" }));
    const inspector = within(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    );
    fireEvent.click(inspector.getByLabelText("完成后才能进入下一阶段"));
    const task = changed.mock.calls.at(-1)?.[0].stages[0].tasks[0];
    expect(task.required).toBe(true);
    expect(task.blocks_next_stage).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "收起流程编辑栏" }));
    fireEvent.click(screen.getByRole("button", { name: "产品信息" }));
    expect(
      inspector.getByLabelText<HTMLInputElement>("完成后才能进入下一阶段")
        .checked,
    ).toBe(false);
    expect(
      inspector.getByLabelText<HTMLInputElement>("业务结案前必须完成").checked,
    ).toBe(true);
  });
  it("edits select options and completion values without losing them on reopen", () => {
    const changed = rs.fn();
    render(<Editor onDraft={changed} />);
    const tree = within(screen.getByRole("region", { name: "流程结构" }));
    fireEvent.click(tree.getByRole("button", { name: "展开任务：产品信息" }));
    fireEvent.click(tree.getByRole("button", { name: "产品名称" }));
    const inspector = within(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    );
    fireEvent.change(inspector.getByLabelText("填写方式"), {
      target: { value: "select" },
    });
    fireEvent.change(inspector.getByLabelText("可选值（每行一项）"), {
      target: { value: "等待外部\n已核验完成\n经核验不适用" },
    });
    fireEvent.change(inspector.getByLabelText(/核验通过值/), {
      target: { value: "已核验完成\n经核验不适用" },
    });
    fireEvent.click(screen.getByRole("button", { name: "收起流程编辑栏" }));
    fireEvent.click(tree.getByRole("button", { name: "产品名称" }));
    expect(
      inspector.getByLabelText<HTMLTextAreaElement>(/核验通过值/).value,
    ).toBe("已核验完成\n经核验不适用");
    expect(
      changed.mock.calls.at(-1)?.[0].stages[0].tasks[0].fields[0].options,
    ).toEqual(["等待外部", "已核验完成", "经核验不适用"]);
    fireEvent.change(inspector.getByLabelText("填写方式"), {
      target: { value: "text" },
    });
    expect(
      changed.mock.calls.at(-1)?.[0].stages[0].tasks[0].fields[0]
        .accepted_values,
    ).toBeUndefined();
  });
  it("opens only for editing, dismisses outside, and preserves unsaved inputs", () => {
    render(<Editor />);
    expect(
      screen.queryByRole("complementary", { name: "流程编辑栏" }),
    ).toBeNull();
    const tree = within(screen.getByRole("region", { name: "流程结构" }));
    const trigger = tree.getByRole("button", { name: "产品信息" });
    trigger.focus();
    fireEvent.click(trigger);
    const inspector = screen.getByRole("complementary", {
      name: "流程编辑栏",
    });
    const input = within(inspector).getByLabelText("任务名称");
    fireEvent.pointerDown(input);
    fireEvent.change(input, { target: { value: "待整理的产品资料" } });
    expect(inspector.getAttribute("aria-hidden")).toBe("false");

    fireEvent.pointerDown(document.body);
    expect(
      screen.queryByRole("complementary", { name: "流程编辑栏" }),
    ).toBeNull();
    const renamed = tree.getByRole("button", { name: "待整理的产品资料" });
    renamed.focus();
    fireEvent.click(renamed);
    expect(
      within(inspector).getByLabelText<HTMLInputElement>("任务名称").value,
    ).toBe("待整理的产品资料");
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(
      screen.queryByRole("complementary", { name: "流程编辑栏" }),
    ).toBeNull();
    expect(document.activeElement).toBe(renamed);
  });

  it("edits a deeply selected field in one inspector and retains it across stage navigation", () => {
    const changed = rs.fn();
    render(<Editor onDraft={changed} />);
    const tree = within(screen.getByRole("region", { name: "流程结构" }));
    fireEvent.click(tree.getByRole("button", { name: "展开任务：产品信息" }));
    fireEvent.click(tree.getByRole("button", { name: "产品名称" }));
    const inspector = within(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    );
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
    fireEvent.click(screen.getByRole("button", { name: "收起流程编辑栏" }));
    expect(tree.getByRole("button", { name: "商品名称" })).toBeTruthy();
  });

  it("keeps task selection when reordering and requires confirmation before removing the task", () => {
    const changed = rs.fn();
    render(<Editor onDraft={changed} />);
    const tree = within(screen.getByRole("region", { name: "流程结构" }));
    fireEvent.click(tree.getByRole("button", { name: "产品信息" }));
    const inspector = within(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    );
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
    fireEvent.pointerDown(screen.getByRole("dialog"));
    expect(
      screen.getByLabelText("流程编辑栏").getAttribute("aria-hidden"),
    ).toBe("false");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.getByRole("complementary", { name: "流程编辑栏" }),
    ).toBeTruthy();
    fireEvent.click(inspector.getByRole("button", { name: "删除任务" }));
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
