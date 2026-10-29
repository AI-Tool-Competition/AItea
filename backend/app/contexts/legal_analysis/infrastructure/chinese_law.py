"""Exact article acquisition, adapted from the supplied chinese-law-verifier.

The public API is a third-party text provider, not an official version registry.
Only law names and article numbers leave the application, never case facts.
"""

import hashlib
import json
import re
from urllib.parse import urlencode

from app.shared.domain.values import now
from app.shared.infrastructure.http.download import _download

ENDPOINT = "https://lawapi.lslby.com/api/v1/article"
_DIGITS = "零一二三四五六七八九"


def article_number(value):
    """Normalize bounded numeric article labels; never infer a law or its rule."""
    value = str(value).strip()
    match = re.fullmatch(r"(?:第)?([0-9]{1,4})(?:条)?(之[一二三四五六七八九十]+)?", value)
    if match:
        number, suffix = int(match[1]), match[2] or ""
        if not 1 <= number <= 9999:
            raise ValueError("条号超出范围")
        result, zero = "", False
        for unit, label in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
            digit, number = divmod(number, unit)
            if digit:
                result += ("零" if zero else "") + _DIGITS[digit] + label
                zero = False
            elif result and number:
                zero = True
        if result.startswith("一十"):
            result = result[1:]
        return "第" + result + "条" + suffix
    if not re.fullmatch(r"第[一二三四五六七八九十百千零〇]{1,16}条(?:之[一二三四五六七八九十]+)?", value):
        raise ValueError("请提供具体条号，例如第五百七十九条或579")
    return value


def article_index(value):
    label = article_number(value)
    number = re.fullmatch(r"第(.+)条", label)
    if not number:  # Added articles have their own identity; never use the base.
        return None
    total, digit = 0, 0
    for char in number[1]:
        if char in _DIGITS or char == "〇":
            digit = 0 if char == "〇" else _DIGITS.index(char)
        else:
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return str(total + digit)


def fetch_article(law, article, *, deadline, check):
    check()
    url = ENDPOINT + "?" + urlencode({"law": law, "article": article})

    def consume(chunks):
        parts = []
        for chunk in chunks:
            check()
            parts.append(chunk)
        return b"".join(parts)

    response = _download(
        url,
        deadline=deadline,
        required_host="lawapi.lslby.com",
        max_bytes=64000,
        accept_compressed_json=True,
        consume=consume,
    )
    if response["mime_type"] != "application/json":
        raise ValueError("第三方法条接口未返回JSON")
    payload = json.loads(response.get("blob") or response.get("content") or b"")
    data = payload.get("data")
    if payload.get("code") != 200 or payload.get("law_name") != law or not isinstance(data, dict):
        raise ValueError("第三方法条接口未返回匹配的法规")
    content = data.get("content")
    if (
        data.get("article_number") != article
        or not isinstance(content, str)
        or len(content) > 20000
        or not re.match(re.escape(article) + r"(?:\s|[　：:])", content)
    ):
        raise ValueError("第三方法条接口未返回匹配的完整条文")
    check()
    digest = hashlib.sha256(content.encode()).hexdigest()
    return {
        "id": "cn-api-" + digest[:24],
        "document_id": "cn-api-" + hashlib.sha256(law.encode()).hexdigest()[:24],
        "document_version": int(digest[:7], 16) + 1,
        "title": law,
        "excerpt": content,
        "country": "CN",
        "source_url": url,
        "locator": {"article": article, **{key: data.get(key) for key in ("part", "chapter", "section")}},
        "metadata": {
            "content_role": "source_text",
            "source_type": "third_party_article_api",
            "provider": "李伯阳律师公益法条接口",
            "retrieved_at": now(),
            "context_complete": True,
            "scope_context_complete": False,
            "published_at": None,
            "effective_from": None,
            "effective_to": None,
            "version_status": "unknown",
            "official_source_verified": False,
        },
    }
