import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AgentMemory } from "@/components/tea/agent-memory";
import { ChatSkills } from "@/components/tea/chat-skills";
import { clearMemoryDrafts } from "@/core/tea/memory-drafts";

const props = {
  accountId: "owner",
  caseId: "case",
  stageId: "stage",
  conversationId: "chat",
};
const note = {
  id: "m1",
  content: "包装需要核对",
  kind: "note",
  conversation_id: "chat",
  revision: 1,
  stale: true,
  source_message_id: null,
  updated_at: "2026-10-26",
};

beforeEach(() => {
  clearMemoryDrafts();
});
afterEach(() => {
  cleanup();
  rs.restoreAllMocks();
  clearMemoryDrafts();
});

describe("agent memory", () => {
  it("retains unsaved input on conflicts, supports revision-aware edit and deletion", async () => {
    let conflict = true;
    let removed = false;
    let current = note;
    const requests: RequestInit[] = [];
    rs.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => {
      if (options?.method === "PATCH") {
        requests.push(options);
        if (conflict) {
          current = { ...note, content: "另一处修改后的内容", revision: 2 };
          return new Response(
            JSON.stringify({
              error: {
                code: "memory_conflict",
                message: "记忆已变化，请刷新后重试",
              },
            }),
            { status: 409 },
          );
        }
        return new Response(JSON.stringify({ ...note, revision: 2 }));
      }
      if (options?.method === "DELETE") {
        removed = true;
        return new Response(JSON.stringify({ deleted: true }));
      }
      return new Response(
        JSON.stringify({
          items: removed ? [] : [current],
          total: removed ? 0 : 1,
        }),
      );
    });
    render(<AgentMemory {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    await screen.findByText("包装需要核对");
    expect(screen.getByText("业务资料已变化，待核对")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "编辑记忆：包装需要核对" }),
    );
    const input = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "编辑记忆",
    });
    fireEvent.change(input, { target: { value: "核对新包装" } });
    fireEvent.click(screen.getByRole("button", { name: "保存记忆" }));
    await screen.findByRole("alert");
    expect(input.value).toBe("核对新包装");
    expect(typeof requests[0]?.body).toBe("string");
    expect(JSON.parse(requests[0]?.body as string).expected_revision).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "刷新列表" }));
    await screen.findByText("另一处修改后的内容");
    expect(input.value).toBe("核对新包装");
    conflict = false;
    fireEvent.click(screen.getByRole("button", { name: "保存记忆" }));
    await waitFor(() =>
      expect(
        screen.getByRole<HTMLTextAreaElement>("textbox", { name: "新增记忆" })
          .value,
      ).toBe(""),
    );
    expect(JSON.parse(requests[1]?.body as string).expected_revision).toBe(2);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "删除记忆：另一处修改后的内容",
      }),
    );
    await screen.findByText(/还没有保存的记忆/);
  });

  it("keeps drafts across navigation and clears them on account logout", async () => {
    rs.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 })),
    );
    const view = render(<AgentMemory {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    fireEvent.change(screen.getByRole("textbox", { name: "新增记忆" }), {
      target: { value: "未保存内容" },
    });
    view.unmount();
    const restored = render(<AgentMemory {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    expect(
      screen.getByRole<HTMLTextAreaElement>("textbox", { name: "新增记忆" })
        .value,
    ).toBe("未保存内容");
    restored.unmount();
    clearMemoryDrafts();
    render(<AgentMemory {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    expect(
      screen.getByRole<HTMLTextAreaElement>("textbox", { name: "新增记忆" })
        .value,
    ).toBe("");
  });

  it("rejects malformed catalog responses without crashing chat", async () => {
    rs.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response(JSON.stringify({ items: [], total: 0 })),
    );
    render(
      <ChatSkills
        caseId="case"
        stageId="stage"
        skills={[]}
        onSkills={() => undefined}
        legal={false}
        onLegal={() => undefined}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: "智能调用" }), {
      button: 0,
      ctrlKey: false,
    });
    await screen.findByText("组合提示加载失败，点击重试");
    expect(screen.getByText("图片核对")).toBeTruthy();
  });
});
