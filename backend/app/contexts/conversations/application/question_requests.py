"""Recognize explicit question requests and replay only their visible content."""

import re

from app.contexts.conversations.application.contracts import QuestionRequest


def question_tool_preference(content):
    """Only direct requests in this user turn constrain tool selection.

    This is a conservative UI preference, not a classifier for missing business
    facts. Other questions remain the model's choice. Quoted material and
    discussion of multiple-choice questions must not activate a tool.
    """
    content = re.sub(r"```[\s\S]*?(?:```|$)", "", content)
    content = re.sub(r"`[^`\n]*`|“[^”\n]*”|‘[^’\n]*’|\"[^\"\n]*\"|'[^'\n]*'", "", content)
    preference = "auto"
    for clause in re.split(r"[，,。；;！？!?\n]", content):
        if clause.lstrip().startswith(">"):
            continue
        if re.search(r"资料|原文|引用|文档|文件|截图|网页|举例|例如|比如|日志|提示词|代码", clause):
            continue
        choice = r"(?:选择题|单选题?|多选题?|选项|点选|勾选)"
        if re.search(
            r"(?:不要|不用|无需|不需要|暂不|先不|别|不做).{0,12}"
            + choice
            + r"|(?:不要|不用|别)(?:再)?(?:提问|问我)",
            clause,
        ):
            preference = "none"
            continue
        if re.search(r"怎么|如何|为什么|为何|什么意思|区别|组件|分析|解答|讲解|解释|翻译|总结", clause):
            continue
        if re.search(r"(?:这|那)道.{0,6}" + choice, clause):
            continue
        if re.search(
            r"(?:做|出|来|给我|帮我|想|要|改成|改为|换成|换为|使用|用|以).{0,12}(?:选择题|单选题?|多选题?)"
            r"|(?:选项.{0,10}(?:让我|供我|给我).{0,6}(?:选|点))"
            r"|(?:让我.{0,8}(?:点选|勾选))"
            r"|(?:可点击.{0,6}选项)"
            r"|(?:(?:用|以|通过).{0,6}选项.{0,6}(?:问我|提问|让我选))",
            clause,
        ):
            preference = "required"
    return preference


def has_text_choices(content):
    """Detect a prose-only invitation to choose, never interpret it as a call.

    The model must generate and validate the real tool arguments on the next
    turn. Ordinary comparisons, instructions and quoted examples stay text.
    """
    content = re.sub(r"```[\s\S]*?(?:```|$)", "", content)
    content = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith(">"))
    if not re.search(
        r"(?:请|你|您).{0,16}(?:选一|选择|选哪|想先|勾选|点选)"
        r"|(?:回复|回答).{0,12}(?:字母|序号|选项)"
        r"|(?:你|您).{0,12}(?:哪个|哪种|哪些).{0,8}[？?]",
        content,
    ):
        return False
    options = re.findall(
        r"(?m)^\s*(?:[-*+]\s+|(?:[-*+]\s*)?(?:\*\*)?[A-Fa-fＡ-Ｆ1-6][.．、)）:：])\S?.+",
        content,
    )
    return len(options) >= 2


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
