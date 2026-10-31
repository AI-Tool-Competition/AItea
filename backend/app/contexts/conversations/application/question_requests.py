"""Replay only the visible question, never the tool arguments or thinking trace."""

from app.contexts.conversations.application.contracts import QuestionRequest


def question_history_text(serialized):
    if not isinstance(serialized, str) or len(serialized.encode()) > 16000:
        return ""
    try:
        request = QuestionRequest.model_validate_json(serialized)
    except ValueError:
        return ""
    lines = ["已向用户提出以下问题（也可自由回答）："]
    for index, question in enumerate(request.questions, 1):
        lines.append(f"{index}、{question.question}（{'可多选' if question.multiple else '单选'}）")
        for option in question.options:
            lines.append(f"- {option.label}" + (f"：{option.description}" if option.description else ""))
    return "\n".join(lines)
