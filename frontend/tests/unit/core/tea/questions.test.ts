import { expect, it } from "@rstest/core";

import { isQuestionRequest, messageQuestion } from "@/core/tea/questions";

const valid = {
  id: "request",
  questions: [
    {
      question: "出口目的地？",
      options: [
        { label: "新加坡", description: "" },
        { label: "印尼", description: "" },
      ],
      multiple: false,
    },
  ],
};

it("bounds question snapshots and rejects malformed persisted or streamed requests", () => {
  expect(isQuestionRequest(valid)).toBe(true);
  for (const request of [
    null,
    { ...valid, id: "" },
    { ...valid, questions: [] },
    { ...valid, unknown: true },
    {
      ...valid,
      questions: [
        ...valid.questions,
        ...valid.questions,
        ...valid.questions,
        ...valid.questions,
      ],
    },
  ]) {
    expect(isQuestionRequest(request)).toBe(false);
  }
  for (const question of [
    { ...valid.questions[0], multiple: "false" },
    { ...valid.questions[0], question: " " },
    { ...valid.questions[0], options: [{ label: "新加坡", description: "" }] },
    {
      ...valid.questions[0],
      options: [valid.questions[0]!.options[0], valid.questions[0]!.options[0]],
    },
  ])
    expect(isQuestionRequest({ ...valid, questions: [question] })).toBe(false);
  const big = {
    ...valid.questions[0],
    options: Array.from({ length: 6 }, (_, i) => ({
      label: String(i),
      description: "茶".repeat(300),
    })),
  };
  expect(isQuestionRequest({ ...valid, questions: [big, big, big] })).toBe(
    false,
  );
  expect(
    messageQuestion([
      {
        id: "agent",
        label: "",
        detail: "",
        status: "completed",
        question_request: valid,
      },
    ]),
  ).toEqual(valid);
});
