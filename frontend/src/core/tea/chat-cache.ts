import type { ListResult, Message } from "./types";

const entries = new Map<
  string,
  { history: ListResult<Message>; savedAt: number; bytes: number }
>();
const MAX_CONVERSATIONS = 20;
const MAX_BYTES = 8 * 1024 * 1024;
const MAX_AGE = 30 * 60_000;

/** Private, tab-local cache. The server remains the durable history source. */
export function readChatHistory(scope: string) {
  const entry = entries.get(scope);
  if (!entry) return undefined;
  entries.delete(scope);
  if (Date.now() - entry.savedAt > MAX_AGE) return undefined;
  entries.set(scope, entry);
  return entry.history;
}

export function cacheChatHistory(scope: string, history: ListResult<Message>) {
  const value = { items: history.items.slice(-200), total: history.total };
  const bytes = new TextEncoder().encode(JSON.stringify(value)).byteLength;
  entries.delete(scope);
  if (bytes > MAX_BYTES) return;
  entries.set(scope, { history: value, savedAt: Date.now(), bytes });
  let total = [...entries.values()].reduce(
    (sum, entry) => sum + entry.bytes,
    0,
  );
  for (const [key, entry] of entries) {
    if (entries.size <= MAX_CONVERSATIONS && total <= MAX_BYTES) break;
    entries.delete(key);
    total -= entry.bytes;
  }
}

export function clearChatHistory() {
  entries.clear();
}

export function mergeChatHistory(
  previous: ListResult<Message>,
  incoming: ListResult<Message>,
): ListResult<Message> {
  const items = [
    ...new Map(
      [...previous.items, ...incoming.items].map((item) => [item.id, item]),
    ).values(),
  ].sort(
    (a, b) =>
      a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id),
  );
  return {
    items,
    total: Math.max(previous.total, incoming.total, items.length),
  };
}
