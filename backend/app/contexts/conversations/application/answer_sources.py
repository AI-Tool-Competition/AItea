"""Carry retrieved source handles through composition into readable citations.

This is presentation, not a second legal review. It cannot validate a model's
interpretation or turn an unverified source into a current legal obligation.
"""

import copy
import hashlib
import re
from urllib.parse import quote, urlsplit


class AnswerSources:
    def __init__(self, messages=()):
        self.sources = {}
        self.handles = {}
        self.current = set()
        for message in messages:
            if message.get("role") == "tool":
                try:
                    import json

                    self.collect(json.loads(message["content"]), historical=True)
                except (ValueError, TypeError):
                    continue

    def collect(self, value, *, historical=False):
        evidence = value.get("result", value)
        rows = evidence.get("citations", evidence.get("items", evidence.get("sources", [])))
        for row in rows:
            if not isinstance(row, dict):
                continue
            row = self.identify(row)
            identifier = row.get("id")
            if (
                not isinstance(identifier, str)
                or not identifier
                or row.get("metadata", {}).get("content_role") == "secondary_summary"
            ):
                continue
            if identifier not in self.handles:
                saved = row.get("reference") if historical else None
                if saved and re.fullmatch(r"S\d+", saved) and saved not in self.sources:
                    self.handles[identifier] = saved
                else:
                    number = max((int(h[1:]) for h in self.sources), default=0) + 1
                    self.handles[identifier] = f"S{number}"
            self.sources[self.handles[identifier]] = copy.deepcopy(row)
            if not historical:
                self.current.add(self.handles[identifier])

    def project(self, material):
        shared_metadata = {}

        def visit(value):
            if isinstance(value, list):
                return [visit(item) for item in value]
            if not isinstance(value, dict):
                return value
            row = self.identify(value)
            result = {key: visit(item) for key, item in row.items()}
            if isinstance(row.get("id"), str) and row["id"] in self.handles:
                result["reference"] = self.handles[row["id"]]
                # Hashes and index bookkeeping remain in the original receipt.
                # Full provisions, locators and all legal/version metadata stay.
                for key in ("chunk_hash", "metadata_revision"):
                    result.pop(key, None)
                meta = result.get("metadata", {})
                result["record_id"] = meta.get("source_record_id")
                for key in (
                    "index_fingerprint",
                    "evidence_store",
                    "source_characters",
                    "translation_characters",
                    "source_record_id",
                ):
                    meta.pop(key, None)
                # Repeated dates and amendment evidence are sent once per
                # document version, without removing any text or condition.
                identity = (row.get("document_id"), row.get("document_version"))
                if identity[0]:
                    repeated = {
                        key: meta[key]
                        for key in (
                            "published_at",
                            "declared_version_date",
                            "effective_from",
                            "effective_to",
                            "superseded_at",
                            "superseded_by",
                            "date_evidence",
                            "version_relations",
                            "issuing_authority",
                            "catalog_title",
                            "catalog_version_id",
                        )
                        if key in meta
                    }
                    first = shared_metadata.get(identity)
                    if first and first[1] == repeated:
                        for key in repeated:
                            meta.pop(key)
                        meta["same_version_metadata_as"] = first[0]
                    else:
                        shared_metadata[identity] = (result["reference"], repeated)
            return result

        return visit(material)

    @staticmethod
    def identify(row):
        # Tool schemas may define url/document_id as property objects. Those
        # definitions are capabilities, not retrieved source records.
        if not row.get("id") and isinstance(row.get("url"), str) and row["url"]:
            return {
                **row,
                "id": "web-" + hashlib.sha256(row["url"].encode()).hexdigest()[:24],
                "source_url": row["url"],
                "metadata": {**row.get("metadata", {}), "content_role": "search_snippet"},
            }
        if not row.get("id") and isinstance(row.get("document_id"), str) and row["document_id"]:
            identity = str(
                (row["document_id"], row.get("document_version"), row.get("chunk_id") or row.get("locator"))
            )
            return {**row, "id": "doc-" + hashlib.sha256(identity.encode()).hexdigest()[:24]}
        return row

    @staticmethod
    def link(source):
        url = source.get("source_url") or ""
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        title = re.sub(r"\s+", " ", source.get("title") or "原文来源").strip()
        article = source.get("locator", {}).get("article")
        if article:
            title += f"（第{article}条）" if str(article).isdigit() else f"（{article}）"
        if source.get("metadata", {}).get("content_role") == "search_snippet":
            title += "（搜索摘要）"
        elif source.get("metadata", {}).get("source_type") == "third_party_article_api":
            title += "（第三方法条接口）"
        title = re.sub(r"([\\\[\]])", r"\\\1", title)
        return f"[{title}]({quote(url, safe=':/?&=#%+@,;~._-')})"

    @staticmethod
    def normalize(text):
        text = re.sub(r"【(S\d+)】", r"[\1]", text)
        text = re.sub(
            r"\[(S\d+(?:\s*[,，、]\s*S\d+)+)\]",
            lambda m: "".join(f"[{h}]" for h in re.findall(r"S\d+", m[1])),
            text,
        )
        text = re.sub(r"\[\[(S\d+)\]\]\([^\s)]*(?:\([^)]*\)[^\s)]*)*\)", r"[\1]", text)
        text = re.sub(r"\[(S\d+)\]\([^\s)]*(?:\([^)]*\)[^\s)]*)*\)", r"[\1]", text)
        return re.sub(r"(?<![A-Za-z0-9_/\[])S\d+(?![A-Za-z0-9_\]])", lambda m: f"[{m[0]}]", text)

    def used(self, text):
        return list(
            dict.fromkeys(h for h in re.findall(r"\[(S\d+)\]", self.normalize(text)) if h in self.sources)
        )

    def unknown(self, text):
        return set(re.findall(r"\[(S\d+)\]", self.normalize(text))) - self.sources.keys()

    def render(self, text):
        def reference(match):
            source = self.sources.get(match[1])
            return self.link(source) if source and self.link(source) else "（该处尚未取得对应原文链接）"

        # Only model-selected evidence is attached. Unused candidates remain in
        # tool receipts; their existence cannot establish support for an answer.
        text = self.normalize(text)

        def unique(match):
            seen, refs = set(), []
            for handle in re.findall(r"\[(S\d+)\]", match[0]):
                source = self.sources.get(handle)
                identity = self.link(source) if source else handle
                if identity not in seen:
                    refs.append(f"[{handle}]")
                    seen.add(identity)
            return "".join(refs)

        text = re.sub(r"(?:\[S\d+\][ \t]*){2,}", unique, text)
        return re.sub(r"\[(S\d+)\]", reference, text)
