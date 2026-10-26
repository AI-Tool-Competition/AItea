import type { AgentMemoryEntry } from "./types";

export interface MemoryDraft {
  content: string;
  kind: AgentMemoryEntry["kind"];
  editing: AgentMemoryEntry | null;
}
const drafts = new Map<string, MemoryDraft>();
export const getMemoryDraft = (scope: string): MemoryDraft =>
  drafts.get(scope) ?? { content: "", kind: "note", editing: null };
export const saveMemoryDraft = (scope: string, draft: MemoryDraft) => {
  if (draft.content || draft.editing) drafts.set(scope, draft);
  else drafts.delete(scope);
};
export const clearMemoryDrafts = () => drafts.clear();
