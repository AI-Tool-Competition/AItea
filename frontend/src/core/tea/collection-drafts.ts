import { type CollectionPlan } from "./types";

export interface SourceDraft {
  name: string;
  url: string;
  interval_hours: number;
  tags: string;
  plan: CollectionPlan;
  seed_text: string;
  expected_revision: number;
}
export interface ChecklistDraft {
  selected: string[];
  storedSelected?: string[];
  level: "" | "primary" | "secondary";
  primaryIds: string[];
  confirmed: boolean;
  excludeId: string;
  excludeNote: string;
  request?: { signature: string; id: string };
}
export const checklistDrafts = new Map<string, ChecklistDraft>();
export const checklistPlanRequests = new Map<string, string>();
// Account-scoped memory only. Logout and session expiry clear private drafts.
export const sourceDrafts = new Map<string, SourceDraft>();
export function clearCollectionDrafts() {
  checklistDrafts.clear();
  checklistPlanRequests.clear();
  sourceDrafts.clear();
}
export function splitTags(value: string) {
  return [
    ...new Set(
      value
        .split(/[,，\n]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}
