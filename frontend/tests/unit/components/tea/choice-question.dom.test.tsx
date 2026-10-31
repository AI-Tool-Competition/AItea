import { afterEach, expect, it, rs } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ChoiceQuestions } from "@/components/tea/choice-question";
import { clearQuestionDrafts } from "@/core/tea/questions";
import type { QuestionRequest } from "@/core/tea/types";
import { clearChatDrafts } from "@/core/tea/use-stage-chat";

const request: QuestionRequest = {
  id: "request",
  questions: [
    {
      question: "目的地是哪里？",
      options: [
        { label: "新加坡", description: "当地进口商清关" },
        { label: "印度尼西亚", description: "进口方待确认" },
      ],
      multiple: false,
    },
    {
      question: "已经准备了哪些资料？",
      options: [
        { label: "产品清单", description: "" },
        { label: "采购合同", description: "" },
      ],
      multiple: true,
    },
  ],
};
const props = {
  request,
  scope: "owner/case/stage/chat/request",
  disabled: false,
};

afterEach(() => {
  cleanup();
  clearQuestionDrafts();
});

it("collects single and multiple choices through Vercel Question and sends one readable answer", () => {
  const onAnswer = rs.fn(() => true);
  render(<ChoiceQuestions {...props} onAnswer={onAnswer} />);
  expect(
    screen.getByRole<HTMLButtonElement>("button", { name: "下一题" }).disabled,
  ).toBe(true);
  const first = screen.getByRole("radio", { name: "新加坡" });
  const second = screen.getByRole("radio", { name: "印度尼西亚" });
  fireEvent.click(second);
  fireEvent.click(first);
  expect(second.getAttribute("aria-checked")).toBe("false");
  expect(first.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "下一题" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "产品清单" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "采购合同" }));
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "  报价尚未确定  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "上一题" }));
  expect(
    screen.getByRole("radio", { name: "新加坡" }).getAttribute("aria-checked"),
  ).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "下一题" }));
  const form = screen.getByRole("form", { name: "已经准备了哪些资料？" });
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect(onAnswer).toHaveBeenCalledTimes(1);
  expect(onAnswer).toHaveBeenCalledWith(
    "1、目的地是哪里？\n新加坡\n\n2、已经准备了哪些资料？\n产品清单、采购合同\n报价尚未确定",
  );
  expect(screen.queryByRole("form")).toBeNull();
  expect(screen.getByText("查看此前的问题")).toBeTruthy();
});

it("supports a freeform-only answer and keeps the draft when sending is temporarily refused", () => {
  const onAnswer = rs.fn(() => false);
  render(
    <ChoiceQuestions
      {...props}
      request={{ ...request, questions: request.questions.slice(0, 1) }}
      onAnswer={onAnswer}
    />,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "   " } });
  expect(
    screen.getByRole<HTMLButtonElement>("button", { name: "发送回答" })
      .disabled,
  ).toBe(true);
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "  马来西亚，还没有确定渠道  " },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送回答" }));
  expect(onAnswer).toHaveBeenCalledWith(
    "1、目的地是哪里？\n马来西亚，还没有确定渠道",
  );
  expect(screen.getByRole<HTMLTextAreaElement>("textbox").value).toBe(
    "  马来西亚，还没有确定渠道  ",
  );
  onAnswer.mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "发送回答" }));
  expect(onAnswer).toHaveBeenCalledTimes(2);
});

it("retains unsent choices on navigation, isolates scopes and clears them on logout", () => {
  const view = render(<ChoiceQuestions {...props} />);
  fireEvent.click(screen.getByRole("radio", { name: "新加坡" }));
  view.unmount();
  const other = render(
    <ChoiceQuestions
      {...props}
      scope="another-owner/case/stage/chat/request"
    />,
  );
  expect(
    screen.getByRole("radio", { name: "新加坡" }).getAttribute("aria-checked"),
  ).toBe("false");
  other.unmount();
  const restored = render(<ChoiceQuestions {...props} />);
  expect(
    screen.getByRole("radio", { name: "新加坡" }).getAttribute("aria-checked"),
  ).toBe("true");
  restored.unmount();
  clearChatDrafts();
  render(<ChoiceQuestions {...props} />);
  expect(
    screen.getByRole("radio", { name: "新加坡" }).getAttribute("aria-checked"),
  ).toBe("false");
});

it("disables pending questions, shows history without actions and supports arrow-key selection", () => {
  const onAnswer = rs.fn(() => true);
  const view = render(
    <ChoiceQuestions {...props} disabled onAnswer={onAnswer} />,
  );
  fireEvent.submit(screen.getByRole("form"));
  expect(onAnswer).not.toHaveBeenCalled();
  expect(
    screen.getByRole<HTMLButtonElement>("radio", { name: "新加坡" }).disabled,
  ).toBe(true);
  view.rerender(<ChoiceQuestions {...props} onAnswer={onAnswer} />);
  const first = screen.getByRole("radio", { name: "新加坡" });
  first.focus();
  fireEvent.keyDown(first, { key: "ArrowDown" });
  expect(
    screen
      .getByRole("radio", { name: "印度尼西亚" })
      .getAttribute("aria-checked"),
  ).toBe("true");
  view.rerender(<ChoiceQuestions {...props} historical onAnswer={onAnswer} />);
  expect(screen.queryByRole("form")).toBeNull();
  expect(screen.queryByRole("radio")).toBeNull();
  expect(screen.getByText("查看此前的问题")).toBeTruthy();
});
