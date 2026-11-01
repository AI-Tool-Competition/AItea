import { z } from "zod";

import type { QuestionRequest, TraceStep } from "./types";

const text = (limit: number, required = false) =>
  z
    .string()
    .refine(
      (value) =>
        Array.from(value).length <= limit && (!required || !!value.trim()),
    );
const option = z
  .object({ label: text(120, true), description: text(300) })
  .strict();
const question = z
  .object({
    question: text(500, true),
    options: z.array(option).min(2).max(6),
    multiple: z.boolean(),
  })
  .strict()
  .refine(
    ({ options }) =>
      new Set(options.map(({ label }) => label.trim().toLowerCase())).size ===
      options.length,
  );
const request: z.ZodType<QuestionRequest> = z
  .object({ id: text(100, true), questions: z.array(question).min(1).max(6) })
  .strict()
  .refine(
    (value) =>
      new TextEncoder().encode(JSON.stringify(value)).byteLength <= 16000,
  );

export function isQuestionRequest(value: unknown): value is QuestionRequest {
  return request.safeParse(value).success;
}

export function messageQuestion(steps: TraceStep[]) {
  return steps.map((step) => step.question_request).find(isQuestionRequest);
}

export interface QuestionAnswer {
  selectedValues: readonly string[];
  text: string;
}
export type QuestionDraft = { index: number; answers: QuestionAnswer[] };
// Drafts live only in this tab, scoped to account/case/stage/conversation/request.
// They survive navigation and are cleared by the same logout path as chat drafts.
const drafts = new Map<string, QuestionDraft>();
export function readQuestionDraft(scope: string) {
  return drafts.get(scope);
}
export function cacheQuestionDraft(scope: string, draft: QuestionDraft) {
  drafts.delete(scope);
  drafts.set(scope, draft);
  while (drafts.size > 60) drafts.delete(drafts.keys().next().value!);
}
export function clearQuestionDrafts(scope?: string) {
  if (scope) drafts.delete(scope);
  else drafts.clear();
}

/** Send ordinary user text, never a command or a claim that a task is complete. */
export function formatQuestionAnswer(
  value: QuestionRequest,
  answers: QuestionAnswer[],
) {
  return value.questions
    .map((item, index) => {
      const answer = answers[index];
      const selected = item.options
        .filter((option) => answer?.selectedValues.includes(option.label))
        .map((option) => option.label);
      const freeform = answer?.text.trim();
      return `${index + 1}、${item.question}\n${[selected.join("、"), freeform]
        .filter(Boolean)
        .join("\n")}`;
    })
    .join("\n\n");
}
