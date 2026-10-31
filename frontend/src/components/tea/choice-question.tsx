"use client";

import "./choice-question.css";

import { ArrowLeft, ArrowUp, Check, Circle } from "lucide-react";
import { useId, useRef, useState } from "react";

import {
  Question,
  QuestionActions,
  QuestionDescription,
  QuestionInput,
  QuestionOption,
  QuestionOptions,
  QuestionPrompt,
  QuestionSubmit,
} from "@/components/ai-elements";
import { Button } from "@/components/ui/button";
import {
  cacheQuestionDraft,
  clearQuestionDrafts,
  formatQuestionAnswer,
  readQuestionDraft,
  type QuestionDraft,
} from "@/core/tea/questions";
import type { QuestionRequest } from "@/core/tea/types";

export function ChoiceQuestions({
  request,
  scope,
  disabled,
  historical = false,
  onAnswer,
}: {
  request: QuestionRequest;
  scope: string;
  disabled: boolean;
  historical?: boolean;
  onAnswer?: (content: string) => boolean;
}) {
  const promptId = useId();
  const [draft, setDraft] = useState<QuestionDraft>(
    () =>
      readQuestionDraft(scope) ?? {
        index: 0,
        answers: request.questions.map(() => ({
          selectedValues: [],
          text: "",
        })),
      },
  );
  const submitted = useRef(false);
  const [sent, setSent] = useState(false);
  const item = request.questions[draft.index]!;
  const answer = draft.answers[draft.index]!;
  const last = draft.index === request.questions.length - 1;

  function update(next: QuestionDraft) {
    cacheQuestionDraft(scope, next);
    setDraft(next);
  }

  if (historical || sent) {
    return (
      <details className="tea-question-history">
        <summary>查看此前的问题</summary>
        {request.questions.map((question) => (
          <div key={question.question}>
            <p>{question.question}</p>
            <ul>
              {question.options.map((option) => (
                <li key={option.label}>
                  {option.label}
                  {option.description && `：${option.description}`}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </details>
    );
  }

  return (
    <Question
      aria-labelledby={promptId}
      className="tea-question-form rounded-none border-0 bg-transparent p-0"
      disabled={disabled}
      selectionMode={item.multiple ? "multiple" : "single"}
      value={answer}
      onValueChange={(value) => {
        const answers = [...draft.answers];
        answers[draft.index] = value;
        update({ ...draft, answers });
      }}
      onSubmit={() => {
        if (disabled || submitted.current) return;
        if (!last) {
          update({ ...draft, index: draft.index + 1 });
          return;
        }
        submitted.current = true;
        if (onAnswer?.(formatQuestionAnswer(request, draft.answers))) {
          clearQuestionDrafts(scope);
          setSent(true);
        } else submitted.current = false;
      }}
    >
      <div className="tea-question-heading" aria-live="polite">
        <QuestionPrompt id={promptId}>{item.question}</QuestionPrompt>
        {request.questions.length > 1 && (
          <span aria-label="问题进度">
            {draft.index + 1} / {request.questions.length}
          </span>
        )}
      </div>
      <QuestionDescription>
        {item.multiple
          ? "可以多选，也可以直接补充你的情况。"
          : "请选择一项，也可以直接填写你的情况。"}
      </QuestionDescription>
      <QuestionOptions
        aria-labelledby={promptId}
        className="tea-question-options"
        onKeyDown={(event) => {
          if (
            item.multiple ||
            ![
              "ArrowDown",
              "ArrowUp",
              "ArrowLeft",
              "ArrowRight",
              "Home",
              "End",
            ].includes(event.key)
          )
            return;
          const options = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>(
              '[role="radio"]:not(:disabled)',
            ),
          );
          const current = options.indexOf(event.target as HTMLButtonElement);
          if (current < 0) return;
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? options.length - 1
                : (current +
                    (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) +
                    options.length) %
                  options.length;
          options[next]?.focus();
          if (options[next]?.getAttribute("aria-checked") !== "true")
            options[next]?.click();
        }}
      >
        {item.options.map((option, index) => {
          const selected = answer.selectedValues.includes(option.label);
          return (
            <QuestionOption
              key={option.label}
              value={option.label}
              variant="ghost"
              className="tea-question-option"
              aria-label={option.label}
              aria-describedby={
                option.description ? `${promptId}-${index}` : undefined
              }
            >
              <span className="tea-question-mark" aria-hidden="true">
                {selected ? <Check size={15} /> : <Circle size={15} />}
              </span>
              <span>
                <span className="tea-question-label">{option.label}</span>
                {option.description && (
                  <span
                    className="tea-question-description"
                    id={`${promptId}-${index}`}
                  >
                    {option.description}
                  </span>
                )}
              </span>
            </QuestionOption>
          );
        })}
      </QuestionOptions>
      <QuestionInput
        aria-label="其他情况或补充说明"
        placeholder="其他情况或补充说明…"
        className="tea-question-input"
        rows={2}
        maxLength={2000}
      />
      <QuestionActions className="tea-question-actions">
        {draft.index > 0 && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={disabled}
            onClick={() => update({ ...draft, index: draft.index - 1 })}
          >
            <ArrowLeft size={14} />
            上一题
          </Button>
        )}
        <QuestionSubmit size="sm">
          {last ? "发送回答" : "下一题"}
          {last && <ArrowUp size={14} />}
        </QuestionSubmit>
      </QuestionActions>
    </Question>
  );
}
