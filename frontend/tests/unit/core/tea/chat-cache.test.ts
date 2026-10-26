import { afterEach, expect, it, rs } from "@rstest/core";

import {
  cacheChatHistory,
  clearChatHistory,
  mergeChatHistory,
  readChatHistory,
} from "@/core/tea/chat-cache";
import type { Message } from "@/core/tea/types";

const message = (id: string) =>
  ({
    id,
    role: "assistant",
    content: id,
    created_at: id,
    trace: [],
    citations: [],
  }) as unknown as Message;
afterEach(() => {
  clearChatHistory();
  rs.restoreAllMocks();
});

it("bounds cached histories without losing pagination totals and isolates scopes", () => {
  cacheChatHistory("owner:conversation", {
    items: Array.from({ length: 230 }, (_, i) =>
      message(String(i).padStart(3, "0")),
    ),
    total: 400,
  });
  expect(readChatHistory("other-owner:conversation")).toBeUndefined();
  expect(readChatHistory("owner:conversation")?.items).toHaveLength(200);
  expect(readChatHistory("owner:conversation")?.total).toBe(400);
  clearChatHistory();
  expect(readChatHistory("owner:conversation")).toBeUndefined();
});

it("evicts the least recently used history and expires old cache entries", () => {
  for (let i = 0; i < 20; i++)
    cacheChatHistory(String(i), { items: [message(String(i))], total: 1 });
  readChatHistory("0");
  cacheChatHistory("new", { items: [], total: 0 });
  expect(readChatHistory("1")).toBeUndefined();
  expect(readChatHistory("0")).toBeTruthy();
  const now = Date.now();
  rs.spyOn(Date, "now").mockReturnValue(now + 31 * 60_000);
  expect(readChatHistory("0")).toBeUndefined();
});

it("does not cache an oversized history and merges late pages without dropping newer messages", () => {
  cacheChatHistory("large", {
    items: [{ ...message("a"), content: "x".repeat(9 * 1024 * 1024) }],
    total: 1,
  });
  expect(readChatHistory("large")).toBeUndefined();
  const merged = mergeChatHistory(
    { items: [message("b"), message("c")], total: 3 },
    { items: [message("a"), message("b")], total: 2 },
  );
  expect(merged.items.map((item) => item.id)).toEqual(["a", "b", "c"]);
  expect(merged.total).toBe(3);
});
