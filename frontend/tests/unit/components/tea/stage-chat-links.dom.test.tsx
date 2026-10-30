import { afterEach, expect, rs, test } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import { StageChat } from "@/components/tea/stage-chat";
import type { Capabilities, CaseDetail } from "@/core/tea/types";

const state = rs.hoisted(() => ({ streaming: false }));
rs.mock("@/components/ai-elements/conversation", () => ({
  Conversation: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ConversationContent: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  ConversationScrollButton: () => null,
}));
rs.mock("@/core/tea/use-stage-chat", () => ({
  useStageChat: () => {
    const content =
      "协会网址：[http://association.example/](http://association.example/)\n\n" +
      "报名入口：[https://expo.example/apply](https://expo.example/apply)\n\n" +
      "[生成文件](/api/tea/documents/generated-file/download)\n\n" +
      "[无效地址](javascript:alert%281%29)";
    const message = {
      id: "answer",
      role: "assistant",
      content,
      trace: [],
      citations: [],
      created_at: "2026-10-29T00:00:00Z",
    };
    const turn = {
      state: "sending",
      text: content,
      request: { client_message_id: "request", content: "展会报名网址" },
      steps: [],
    };
    return {
      messages: state.streaming ? [] : [message],
      turns: state.streaming ? [turn] : [],
      timeline: state.streaming ? [{ turn }] : [{ message }],
      text: "",
      messageKey: () => message.id,
      sending: state.streaming,
    };
  },
}));

afterEach(cleanup);

test.each([false, true])(
  "generated file links stay under /aitea, streaming=%s",
  (streaming) => {
    const previous = process.env.NEXT_PUBLIC_TEA_BASE_PATH;
    process.env.NEXT_PUBLIC_TEA_BASE_PATH = "/aitea";
    try {
      state.streaming = streaming;
      const record = {
        id: "case",
        stages: [{ id: "stage", status: "active", tasks: [] }],
      } as unknown as CaseDetail;
      render(
        <StageChat
          record={record}
          stage={record.stages[0]!}
          accountId="owner"
          taskId=""
          capabilities={{ assistant_mode: "live" } as Capabilities}
          onTask={() => undefined}
          onUpdate={() => undefined}
          onUpload={() => undefined}
        />,
      );
      expect(
        screen.getByRole("link", { name: "生成文件" }).getAttribute("href"),
      ).toBe("/aitea/api/tea/documents/generated-file/download");
      expect(screen.queryByRole("link", { name: "无效地址" })).toBeNull();
    } finally {
      if (previous === undefined) delete process.env.NEXT_PUBLIC_TEA_BASE_PATH;
      else process.env.NEXT_PUBLIC_TEA_BASE_PATH = previous;
    }
  },
);

test.each([false, true])(
  "business URLs are direct links in a real rendered answer, streaming=%s",
  (streaming) => {
    state.streaming = streaming;
    const record = {
      id: "case",
      stages: [{ id: "stage", status: "active", tasks: [] }],
    } as unknown as CaseDetail;
    render(
      <StageChat
        record={record}
        stage={record.stages[0]!}
        accountId="owner"
        taskId=""
        capabilities={{ assistant_mode: "live" } as Capabilities}
        onTask={() => undefined}
        onUpdate={() => undefined}
        onUpload={() => undefined}
      />,
    );
    for (const address of [
      "http://association.example/",
      "https://expo.example/apply",
    ]) {
      const link = screen.getByRole("link", { name: address });
      expect(link.getAttribute("href")).toBe(address);
      expect(link.getAttribute("target")).toBe("_blank");
      expect(screen.queryByRole("button", { name: address })).toBeNull();
    }
    expect(screen.queryByRole("link", { name: "无效地址" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  },
);
