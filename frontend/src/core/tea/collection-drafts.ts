import {
  type Classification,
  type CollectionPlan,
  type LawRelation,
} from "./types";

export interface SourceDraft {
  name: string;
  url: string;
  interval_hours: number;
  tags: string;
  plan: CollectionPlan;
  seed_text: string;
  expected_revision: number;
}
export interface ClassificationDraft {
  collection: string;
  level: Classification["level"];
  tags: string;
  expected_revision: number;
}
export interface RelationDraft {
  target: string[];
  collection: string;
  relation: LawRelation["relation"];
  locator: string;
}
// Account-scoped memory only. Logout and session expiry clear private drafts.
export const sourceDrafts = new Map<string, SourceDraft>();
export const classificationDrafts = new Map<string, ClassificationDraft>();
export const relationDrafts = new Map<string, RelationDraft>();
export function clearCollectionDrafts() {
  sourceDrafts.clear();
  classificationDrafts.clear();
  relationDrafts.clear();
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
