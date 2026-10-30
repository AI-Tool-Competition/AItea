"""Explicit read-only legal-pack provider shared by agents and legal analysis.

The administrator-built SQLite index is a data asset, not a RAGFlow dataset.
Pack retrieval never grants publication or current-effectivity approval.
"""

import hashlib
import json
import math
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from datetime import date
from urllib.parse import urlsplit

from app.configuration.paths import InstancePaths
from app.contexts.documents.infrastructure.country_clauses import ClauseSemantic
from app.contexts.documents.infrastructure.country_semantic import CountrySemantic
from app.contexts.documents.infrastructure.providers.reranking import rerank_texts


@lru_cache(maxsize=8)
def _catalog(path, modified, size):
    """Cache the delivered catalog, with stat identity preventing stale reuse."""
    if size > 32 * 1024 * 1024:
        return {}
    try:
        with open(path, encoding="utf-8-sig") as stream:
            documents = [json.loads(line) for line in stream if line.strip()]
        return {item["document_id"]: item for item in documents}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


@lru_cache(maxsize=8)
def _relations(path, modified, size):
    if size > 8 * 1024 * 1024:
        return []
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError, TypeError):
        return []


@lru_cache(maxsize=8)
def _source_metadata(path, modified, size):
    if size > 1024 * 1024:
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value["documents"] if value.get("schema_version") == 1 else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


@lru_cache(maxsize=256)
def _source_heading(path, modified, country, document_id, source_hash):
    with sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT id,text FROM records WHERE country=? AND document_id=? AND source_hash=? ORDER BY rowid LIMIT 1",
            (country, document_id, source_hash),
        ).fetchone()
    if not row:
        return None
    heading = re.sub(r"\s+", "", row[1].strip().splitlines()[0]).strip("《》")
    if re.fullmatch(
        r"[\u3400-\u9fffA-Za-z0-9（）()·—-]{3,140}(?:法|办法|条例|规定|决定|解释|标准|公告|规范|通则)",
        heading,
    ):
        return heading, row[0]
    return None


@lru_cache(maxsize=32)
def _source_scopes(path, modified, country, document_id, source_hash):
    """Transcribe chapter/section context from the source's actual record order."""
    scopes, active = {}, {}
    with sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT id,article,text FROM records WHERE country=? AND document_id=? AND source_hash=? ORDER BY rowid",
            (country, document_id, source_hash),
        )
        for identifier, article, text in rows:
            heading = (
                re.fullmatch(r"第[一二三四五六七八九十百零〇\d]+([编章节])\s*(.{2,70})", text.strip())
                if not article
                else None
            )
            if heading:
                level = "编章节".index(heading[1])
                active = {key: value for key, value in active.items() if key < level}
                active[level] = (heading[2].strip(), identifier)
            scopes[identifier] = tuple(active[key] for key in sorted(active))
    return scopes


class CountryPacks:
    def __init__(self, data_dir, *, metadata_path=None):
        self.path = Path(data_dir) / "legal-library" / "index.sqlite3"
        self.metadata_path = (
            Path(metadata_path)
            if metadata_path
            else InstancePaths.resolve().catalogs / "legal-source-metadata.json"
        )
        self.semantic = CountrySemantic(data_dir)
        self.clauses = ClauseSemantic(data_dir)

    def document_metadata(self, row):
        metadata = self._document_metadata(row)
        try:
            stat = self.metadata_path.stat()
            documents = _source_metadata(str(self.metadata_path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            documents = []
        for entry in documents:
            if (
                entry.get("country") == row["country"]
                and entry.get("document_id") == row["document_id"]
                and (not entry.get("source_hash") or entry["source_hash"] == row["source_hash"])
            ):
                metadata.update(entry.get("metadata", {}))
        return metadata

    def _document_metadata(self, row):
        if row["country"] != "CN":
            if row["country"] != "SG":
                return {}
            # Display labels are transcribed from the stored source headings;
            # document IDs remain stable internal references.
            titles = {
                "MOLEG-FOOD-REG": "Food Regulations（食品条例；韩国法制处收录文本）",
                "GAZ-S92-2025": "Food (Amendment) Regulations 2025（S 92/2025）",
                "SFA-LABEL-GUIDE": "A Guide to Food Labelling and Advertisements（SFA指引）",
                "SFA-LABEL": "Labelling Requirements for Food（SFA指引）",
                "SFA-IMPORT": "What You Need to Know for Import of Food for Commercial Sale（SFA指引）",
                "CUSTOMS-IMPORT": "Import Procedures Overview（Singapore Customs指引）",
                "LAWS-FOOD": "Sale of Food Act 1973（非官方转载）",
                "LAWS-CUSTOMS": "Customs Act 1960（非官方转载）",
            }
            host = (urlsplit(row["source_url"] or "").hostname or "").casefold()
            return {
                "catalog_title": titles.get(row["document_id"]),
                "source_authority": "official"
                if host.endswith(".gov.sg")
                else ("foreign_official_reproduction" if host.endswith(".go.kr") else "non_official"),
            }
        path = self.path.parent / "source-packs/CN/茶渡中国法律资料/数据/法规与标准目录.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return {}
        doc = _catalog(str(path), stat.st_mtime_ns, stat.st_size).get(row["document_id"], {})
        if not row["source_hash"] or doc.get("original_file_hash") != row["source_hash"]:
            return {}
        metadata = {
            "catalog_version_id": doc.get("version_id"),
            "catalog_title": doc.get("title"),
            "declared_version_date": doc.get("version_date"),
            "issuing_authority": doc.get("issuing_authority"),
            "full_text_available": doc.get("full_text_available"),
        }
        title = metadata.get("catalog_title") or ""
        page_title = bool(re.search(r"法规[网库]|政策法规库|政策库|门户|首页", title))
        if page_title:
            metadata.update(source_page_title=title, catalog_title=None, title_status="unresolved")
        if (
            page_title
            or not title
            or re.match(r"第[一二三四五六七八九十百零〇\d]+条|[（(]?\d{4}\s*年|办事指南$|下载", title)
        ):
            try:
                heading = _source_heading(
                    str(self.path),
                    self.path.stat().st_mtime_ns,
                    row["country"],
                    row["document_id"],
                    row["source_hash"],
                )
            except (OSError, sqlite3.Error, IndexError):
                heading = None
            if heading:
                metadata.update(
                    catalog_title=heading[0],
                    catalog_title_original=title,
                    title_evidence_record_id=heading[1],
                    title_status="source_heading",
                )
        for target, candidates in (
            ("published_at", ("publication_date",)),
            ("effective_from", ("version_effective_date", "effective_date")),
            ("effective_to", ("end_date",)),
        ):
            for key in candidates:
                value = doc.get(key)
                if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    metadata[target] = value
                    break
        # Retain the date's source text without promoting its review status.
        metadata["date_evidence"] = [
            item
            for item in (doc.get("effective_date_evidence") or [])[:4]
            if isinstance(item, dict) and len(json.dumps(item, ensure_ascii=False)) <= 1200
        ]
        relation_path = path.parent / "修改废止替代线索.jsonl"
        try:
            stat = relation_path.stat()
            relations = _relations(str(relation_path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            relations = []
        related = [
            r
            for r in relations
            if row["document_id"] in (r.get("source_document_id"), r.get("target_document_id"))
        ]
        metadata["version_relations"] = related[:8]
        replacements = [
            r["effective_date"]
            for r in related
            if r.get("target_document_id") == row["document_id"]
            and r.get("target_version_resolved")
            and r.get("relation_type") == "新版本施行，旧版本区间结束"
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("effective_date")))
        ]
        if replacements:
            metadata["superseded_at"] = min(replacements)
            metadata["superseded_by"] = [
                r["source_document_id"]
                for r in related
                if r.get("target_document_id") == row["document_id"]
                and r.get("effective_date") == metadata["superseded_at"]
                and r.get("target_version_resolved")
                and r.get("relation_type") == "新版本施行，旧版本区间结束"
            ]
        return metadata

    def status(self):
        if not self.path.is_file():
            return {"status": "not_configured", "countries": {}}
        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                metadata = dict(conn.execute("SELECT key,value FROM metadata"))
            counts = {c: int(metadata.get(f"record_count_{c}", 0)) for c in ("CN", "ID", "SG")}
            return {
                "status": "ready",
                "countries": counts,
                "fingerprint": metadata["fingerprint"],
                "semantic": self.semantic.status(metadata["fingerprint"]),
                "clause_semantic": self.clauses.status(metadata["fingerprint"]),
            }
        except (sqlite3.Error, ValueError, KeyError):
            return {"status": "invalid_index", "countries": {}}

    def later_version(self, country, title, version_date, as_of):
        """Resolve an explicitly dated official snapshot against catalog versions."""
        if not self.path.is_file() or not version_date:
            return None

        def name(value):
            return re.split(r"[（(]|\s+[-|—]\s+", value, maxsplit=1)[0].strip().strip("《》")

        found = []
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM records WHERE country=? AND rowid IN "
                "(SELECT min(rowid) FROM records WHERE country=? GROUP BY document_id)",
                (country, country),
            )
            for row in rows:
                meta = self.document_metadata(row)
                if name(meta.get("catalog_title") or row["title"]) != name(title):
                    continue
                revision, start = meta.get("declared_version_date"), meta.get("effective_from")
                if revision and start and version_date < revision and start <= str(as_of):
                    found.append(
                        {
                            "document_id": row["document_id"],
                            "effective_from": start,
                            "declared_version_date": revision,
                            "source_url": row["source_url"],
                        }
                    )
        return max(found, key=lambda item: item["declared_version_date"]) if found else None

    def search(self, country, query, limit, as_of=None):
        state = self.status()
        if not state["countries"].get(country):
            return None
        # Expand ordinary expressions into searchable legal vocabulary. These
        # are lexical equivalents, never a mapping from facts to legal effects.
        equivalents = (
            ("货款", "价款"),
            ("买方", "买受人"),
            ("卖方", "出卖人"),
            ("逾期", "迟延"),
            ("交货", "交付"),
        )
        concepts = list(
            dict.fromkeys(
                term for group in equivalents if any(term in query for term in group) for term in group
            )
        )
        if country in {"SG", "ID"}:
            for zh, english in (
                ("食品", "food"),
                ("标签", "label labelling"),
                ("英文", "English"),
                ("进口", "import"),
                ("出口", "export"),
                ("许可", "permit licence"),
                ("注册", "registration register"),
                ("备案", "registration"),
                ("生产", "manufactur"),
                ("预包装", "prepacked"),
                ("销售", "sale sell"),
                ("零售", "retail"),
                ("样品", "sample"),
                ("免费", "free"),
                ("定义", "interpretation definition"),
            ):
                if zh in query:
                    concepts.extend(english.casefold().split())
        for term in ("支付", "违约", "责任", "备案", "登记", "取消", "标签", "进口", "出口"):
            if term in query:
                concepts.append(term)
        goals = re.findall(r"(?:能否|能不能|可否|是否|还要|还需|能要求)([^？?。；;]+)", query)
        focus_query = goals[-1] if goals else query

        def tokens(text):
            found = re.findall(r"[a-z0-9]{2,}", text.casefold())
            for run in re.findall(r"[\u3400-\u9fff]+", text):
                found.extend(run[i : i + 2] for i in range(len(run) - 1))
            return found

        focus_words = list(dict.fromkeys(tokens(focus_query)))
        for ordinary, legal in equivalents:
            if ordinary in focus_query:
                focus_words.extend(tokens(legal))
        focus_words = list(dict.fromkeys(focus_words))
        words = list(dict.fromkeys(tokens(" ".join(concepts)) + focus_words + tokens(query)))[:48]
        result = {"items": [], "mode": "local_pack", "reranked": False}
        if not words:
            return result | {"warning": "请提供具体法规名称、条号或法律概念。"}
        quoted = ['"' + word + '"' for word in words]
        focus = quoted[:12]
        pairs = [f"({a} AND {b})" for i, a in enumerate(focus) for b in focus[i + 1 :]]
        expressions = [" OR ".join(pairs)] if pairs else [" OR ".join(quoted)]
        if concepts:
            expressions.insert(0, " OR ".join('"' + term + '"' for term in concepts))
        clause_hits, clause_status = self.clauses.search(country, query, state["fingerprint"])
        clause_available = bool(clause_hits) and clause_status in {"ready", "partial"}
        matches = {}
        for hit in clause_hits:
            matches.setdefault(hit["record_id"], hit)
        if clause_status == "ready":
            semantic, semantic_status = [], "ready"
        else:
            semantic, semantic_status = self.semantic.search(country, query, state["fingerprint"])
        semantic_scores = {(document, version): score for document, version, score in semantic}
        if semantic_status == "ready" or clause_available:
            result["mode"] = "local_pack_vector"
            result["reranked"] = True
        result["semantic_status"] = semantic_status
        result["clause_semantic_status"] = clause_status
        result["semantic_granularity"] = "clause" if clause_available else "document_profile"
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            exact_rows = self._exact_rows(conn, country, query)
            rows = list(exact_rows)
            if matches:
                placeholders = ",".join("?" for _ in matches)
                rows.extend(
                    conn.execute(
                        f"SELECT * FROM records WHERE country=? AND id IN ({placeholders})",
                        (country, *matches),
                    ).fetchall()
                )
            for expression in expressions:
                lexical = conn.execute(
                    """SELECT r.*,bm25(record_search,5,1,3,0.4) ranking
                    FROM record_search JOIN records r ON r.rowid=record_search.rowid
                    WHERE record_search MATCH ? AND r.country=? ORDER BY ranking LIMIT 160""",
                    (expression, country),
                ).fetchall()
                rows.extend(lexical)
            if semantic:
                clauses = " OR ".join("(document_id=? AND source_hash=?)" for _ in semantic)
                params = [value for document, version, _ in semantic for value in (document, version)]
                rows.extend(
                    conn.execute(
                        f"SELECT * FROM records WHERE country=? AND ({clauses})", (country, *params)
                    ).fetchall()
                )
        exact_ids = {row["id"] for row in exact_rows}
        at = str(as_of or date.today())
        # A statute's title may mention both directions while the requested
        # conduct concerns only one. Rank the operative body for that conduct;
        # shared provisions remain candidates and explicit article requests win.
        conduct = re.sub(r"《[^》]*》", "", query)
        directions = {term for term in ("进口", "出口") if term in conduct.replace("进出口", "")}

        def direction_rank(row):
            if country != "CN" or len(directions) != 1:
                return 0
            body = (row["text"] + row["translation"]).replace("进出口", "")
            requested = next(iter(directions))
            opposite = "出口" if requested == "进口" else "进口"
            return int(requested in body) - int(opposite in body)

        document_metadata = {}
        source_scopes = {}
        for row in rows:
            document_metadata.setdefault(row["document_id"], None)
            if document_metadata[row["document_id"]] is None:
                document_metadata[row["document_id"]] = self.document_metadata(row)
            if row["document_id"] not in source_scopes:
                source_scopes[row["document_id"]] = _source_scopes(
                    str(self.path),
                    self.path.stat().st_mtime_ns,
                    country,
                    row["document_id"],
                    row["source_hash"],
                )

        def period_rank(row):
            meta = document_metadata[row["document_id"]]
            start, end, replaced = (
                meta.get("effective_from"),
                meta.get("effective_to"),
                meta.get("superseded_at"),
            )
            if (start and start > at) or (end and end < at) or (replaced and replaced <= at):
                return -1
            return 1 if start else 0

        def ranking(row):
            body = (row["text"] + row["translation"]).casefold()
            substantive = (
                len(body.strip()) >= 30 or (bool(row["article"]) and len(body.strip()) >= 12)
            ) and row["source_type"] not in {
                "heading",
                "navigation",
                "link",
            }
            operative = bool(row["article"]) and "explanation" not in row["unit_id"]
            direct_rule = operative and bool(re.search(r"应当|不得|必须|所称.{0,60}是指", body))
            case_requested = bool(re.search(r"案例|判决|裁判|case\s+law", query, re.I))
            case_penalty = 0 if case_requested or not re.search(r"案例|判决", row["title"]) else 4
            # Length normalization prevents a long judgment or table of contents
            # from winning merely by repeating every word in a user's narrative.
            body_score = sum(word in body for word in words) / math.sqrt(max(1, len(body) / 180))
            concept_score = 2 * sum(term in body for term in concepts)
            goal_score = 3 * sum(word in body for word in focus_words) / math.sqrt(max(1, len(body) / 180))
            title_score = 0.5 * sum(word in row["title"].casefold() for word in words)
            headings = source_scopes.get(row["document_id"], {}).get(row["id"], ())
            scope_score = sum(
                24 + 3 * sum(word in label for word in words)
                for label, _ in headings
                if len(label) >= 4 and label in query
            )
            domestic_penalty = (
                14 if "国内" in query and re.search(r"国际|联合国|涉外|CISG", row["title"], re.I) else 0
            )
            return (
                self._requested_article(query) == row["article"] if self._requested_article(query) else False,
                substantive,
                row["id"] in exact_ids,
                self._requested_article(query) == row["article"] if self._requested_article(query) else False,
                period_rank(row) >= 0,
                direction_rank(row),
                not row["id"].startswith("SG:guidance:"),
                body_score
                + goal_score
                + concept_score
                + title_score
                + scope_score
                + (10 if operative else 0)
                + (2 if direct_rule else 0)
                - case_penalty
                - domestic_penalty
                + 6 * semantic_scores.get((row["document_id"], row["source_hash"]), 0),
                period_rank(row),
                operative,
            )

        rows = list(
            {
                row["id"]: row for row in rows if row["source_type"] not in {"heading", "navigation", "link"}
            }.values()
        )
        rows.sort(key=ranking, reverse=True)
        if matches and clause_status == "ready":
            lexical_ranks = {row["id"]: i + 1 for i, row in enumerate(rows)}
            semantic_ranks = {record_id: i + 1 for i, record_id in enumerate(matches)}
            # Reciprocal rank fusion prevents lexical repetition or a vector's
            # score scale from suppressing the other retrieval method.
            # A partial index contributes recall above, but cannot rank the
            # unindexed provisions. Fusing those incomplete ranks would favour
            # whichever clauses happened to have been embedded first.
            rows.sort(
                key=lambda row: (
                    self._requested_article(query) == row["article"]
                    if self._requested_article(query)
                    else False,
                    len((row["text"] + row["translation"]).strip()) >= 30,
                    row["id"] in exact_ids,
                    self._requested_article(query) == row["article"]
                    if self._requested_article(query)
                    else False,
                    period_rank(row) >= 0,
                    direction_rank(row),
                    not row["id"].startswith("SG:guidance:"),
                    1 / (40 + lexical_ranks[row["id"]])
                    + (1.25 / (40 + semantic_ranks[row["id"]]) if row["id"] in semantic_ranks else 0),
                    period_rank(row),
                ),
                reverse=True,
            )
        # Re-rank complete source records before imposing a per-document cap.
        # Document-profile vectors alone cannot select clauses in a long code.
        pool = [r for r in rows if 0 < len(r["text"] or r["translation"]) <= 18000][:16]
        try:
            ranks = rerank_texts(
                query,
                [
                    "\n".join(
                        [
                            r["title"],
                            *(label for label, _ in source_scopes.get(r["document_id"], {}).get(r["id"], ())),
                            (r["text"] or r["translation"])[:1000],
                        ]
                    )
                    for r in pool
                ],
            )
            if ranks is not None:
                scores = {pool[r["index"]]["id"]: r["score"] for r in ranks}
                original_order = {r["id"]: i for i, r in enumerate(rows)}
                rows.sort(
                    key=lambda r: (
                        self._requested_article(query) == r["article"]
                        if self._requested_article(query)
                        else False,
                        r["id"] in exact_ids,
                        period_rank(r) >= 0,
                        direction_rank(r),
                        scores.get(r["id"], -1),
                        -original_order[r["id"]],
                    ),
                    reverse=True,
                )
                result["clause_reranker"] = "ready"
            else:
                result["clause_reranker"] = "not_configured"
        except ValueError:
            result["clause_reranker"] = "unavailable"
        # Regulations can retain a condition after the enabling act changes.
        # Follow saved textual references and closely named procedures, without
        # another vector/model call or any precomputed legal conclusion.
        related = []
        if country == "CN" and limit >= 6:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                related = self._related_rows(conn, country, query, rows[:12])
            for row in related:
                document_metadata.setdefault(row["document_id"], self.document_metadata(row))
            groups = {}
            for row in related:
                if row["source_type"] not in {"heading", "navigation", "link", "publication_preamble"}:
                    title = document_metadata[row["document_id"]].get("catalog_title") or row["title"]
                    family = re.sub(r"[（(].*?[）)]|\s+", "", title)
                    groups.setdefault(family, []).append(row)
            additions = []
            query_phrases = {
                run[i : i + 6] for run in re.findall(r"[\u3400-\u9fff]+", query) for i in range(len(run) - 5)
            }

            def dependency_rank(group):
                versions = {
                    document_metadata[r["document_id"]].get("effective_from")
                    for r in group
                    if document_metadata[r["document_id"]].get("effective_from")
                }
                return (
                    any(
                        re.search(
                            r"关于.*(?:修改|废止).*决定",
                            document_metadata[r["document_id"]].get("catalog_title") or r["title"],
                        )
                        for r in group
                    ),
                    len(versions) > 1,
                    max(ranking(r) for r in group),
                )

            for group in sorted(groups.values(), key=dependency_rank, reverse=True):
                # Keep the latest applicable text in a family, plus distinct
                # amendment decisions. Old versions remain in the main recall.
                current = max(
                    group,
                    key=lambda r: (
                        period_rank(r),
                        document_metadata[r["document_id"]].get("declared_version_date") or "",
                    ),
                )["document_id"]

                def related_rank(row):
                    text = re.sub(r"\s+", "", row["text"])
                    title = document_metadata[row["document_id"]].get("catalog_title") or row["title"]
                    return (
                        bool(re.search(r"关于.*(?:修改|废止).*决定", title))
                        and bool(re.search(r"删去|删除|修改为|废止", text)),
                        sum(phrase in text for phrase in query_phrases),
                        bool(re.search(r"所称.{0,50}是指|是指", text)),
                        ranking(row),
                    )

                selected = sorted(
                    (r for r in group if r["document_id"] == current), key=related_rank, reverse=True
                )
                if not selected:
                    continue
                additions.append(selected[0])
                definition = next(
                    (r for r in selected if re.search(r"本(?:法|条例|办法|规定)所称.{0,80}是指", r["text"])),
                    None,
                )
                if definition and definition["id"] != selected[0]["id"]:
                    additions.append(definition)
                contextual = next(
                    (r for r in selected if re.search(r"本法自.{0,60}施行|本决定自.{0,60}施行", r["text"])),
                    None,
                )
                if contextual and contextual["id"] != selected[0]["id"]:
                    additions.append(contextual)
            # Reserve bounded space for dependency evidence before the usual
            # per-document cap. A broad lexical hit cannot crowd it out.
            rows[min(2, len(rows)) : min(2, len(rows))] = additions[: min(6, limit // 2)]
        related_ids = {r["id"] for r in related}
        # Keep the commencement/transition clause of a named law alongside the
        # relevant provisions, including when it occurs at the end of a long act.
        commencement = [
            r
            for r in exact_rows
            if re.search(r"本法自.{0,60}施行|本条例自.{0,60}施行|commencement|transitional", r["text"], re.I)
            and r["source_type"] not in {"heading", "navigation", "link"}
            and len(r["text"]) < 1800
        ]
        if commencement:
            selected = sorted(
                commencement,
                key=lambda r: (
                    period_rank(r),
                    document_metadata[r["document_id"]].get("declared_version_date") or "",
                ),
                reverse=True,
            )[0]
            rows.insert(min(2, len(rows)), selected)
        # A numbered request still needs the decision that changed that act.
        # These short operative units are often near the end, after navigation.
        changes = [
            r
            for r in exact_rows
            if re.search(
                r"关于(?:修改|废止)|修正案|Amendment",
                self.document_metadata(r).get("catalog_title") or r["title"],
                re.I,
            )
            and re.search(
                r"删去|删除|修改为|废止|施行|commencement|amend|insert|delete|substitut|omit|repeal",
                r["text"],
                re.I,
            )
            and r["source_type"] not in {"heading", "navigation", "link", "publication_preamble"}
            and len(r["text"]) < 1800
        ]
        for changed in reversed(changes[:4]):
            rows.insert(min(1, len(rows)), changed)
        # An old article number may now regulate a different subject. Preserve
        # the applicable act's subject definition before the per-document cap,
        # alongside its commencement and the historical amending decision.
        definitions = [
            r
            for r in [*exact_rows, *related]
            if re.search(r"本(?:法|条例|办法|规定)所称.{0,80}是指", r["text"]) and period_rank(r) >= 0
        ]
        for definition in reversed(
            sorted(
                definitions,
                key=lambda r: (
                    period_rank(r),
                    document_metadata[r["document_id"]].get("declared_version_date") or "",
                    ranking(r),
                ),
                reverse=True,
            )[:2]
        ):
            rows.insert(0, definition)
        seen, document_counts = set(), {}
        for row in rows:
            if row["status"] == "source_location_unverified":
                continue
            document_key = row["document_id"], row["source_hash"]
            identity = (*document_key, row["article"] or row["unit_id"])
            document_limit = 8 if row["id"] in exact_ids else 4
            if identity in seen or document_counts.get(document_key, 0) >= document_limit:
                continue
            seen.add(identity)
            document_counts[document_key] = document_counts.get(document_key, 0) + 1
            match = matches.get(row["id"])
            if match and match["source_hash"] != row["source_hash"]:
                continue
            item = self._item(row, state["fingerprint"], words, full=False, match=match)
            item["metadata"]["semantic_document_score"] = semantic_scores.get(document_key)
            item["metadata"]["retrieval_date_state"] = period_rank(row)
            item["metadata"]["exact_match"] = row["id"] in exact_ids
            if row["id"] in related_ids:
                item["metadata"]["retrieval_relation"] = "referenced_or_related_rule"
            if match:
                item["metadata"]["semantic_clause_match"] = match
            result["items"].append(item)
            if len(result["items"]) >= limit:
                break
        return result | {
            "warning": (
                "部分已完成条款语义、文件级语义与全文词项混合检索；条款索引尚未全量覆盖，未命中不代表没有规定。"
                if clause_available and clause_status == "partial"
                else "条款语义与全文词项混合检索；适用时间见来源及版本记录。"
                if clause_status == "ready"
                else "文件级语义与全文词项混合检索；适用时间见来源及版本记录。"
                if result["mode"] == "local_pack_vector"
                else "本地资料包全文词项检索；适用时间见来源及版本记录。"
            )
        }

    def lookup_chinese_articles(self, law, numbers):
        """Exact law/article fallback, without vector or reranking requests."""
        if not self.path.is_file():
            return []
        numbers = [number for number in numbers if number]
        if not numbers:
            return []
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM records WHERE country='CN' AND article IN ("
                + ",".join("?" for _ in numbers)
                + ") ORDER BY rowid",
                numbers,
            ).fetchall()
        items = []
        for row in rows:
            title = self.document_metadata(row).get("catalog_title") or row["title"]
            # Match the full name and retain separately recorded versions.
            if title != law and not title.startswith((law + "（", law + "(")):
                continue
            item = self.read("CN", row["id"])
            if item and item.get("metadata", {}).get("context_complete"):
                items.append(item)
            if len(items) >= 12:
                break
        return items

    def read(self, country, record_id):
        """Read a saved record under an explicit country; never resolve a path."""
        state = self.status()
        if not state["countries"].get(country) or not isinstance(record_id, str) or len(record_id) > 1000:
            return None
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM records WHERE country=? AND id=?", (country, record_id)
            ).fetchone()
            context_units = []
            if row and country == "SG" and row["title"] and row["source_type"] in {"paragraph", "table_row"}:
                section = conn.execute(
                    "SELECT id,text FROM records WHERE country=? AND document_id=? AND source_hash=? AND title=? AND source_type IN ('paragraph','table_row') ORDER BY rowid",
                    (country, row["document_id"], row["source_hash"], row["title"]),
                ).fetchall()
                if row["source_type"] == "table_row" and len("\n".join(r["text"] for r in section)) > 5000:
                    position = next(i for i, r in enumerate(section) if r["id"] == row["id"])
                    # Retain complete neighbouring rows, including contrasting
                    # categories and exceptions. Never cut a row to fit.
                    section = section[max(0, position - 1) : position + 2]
                text = "\n".join(r["text"] for r in section)
                if len(section) > 1 and len(text) <= 5000:
                    row = dict(row)
                    row["text"] = text
                    context_units = [r["id"] for r in section]
        return (
            self._item(row, state["fingerprint"], [], full=True, context_units=context_units) if row else None
        )

    def neighboring_records(self, country, record_id):
        """Return adjacent original units from the same immutable instrument."""
        if not self.path.is_file() or not record_id:
            return []
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            source = conn.execute(
                "SELECT rowid,* FROM records WHERE country=? AND id=?", (country, record_id)
            ).fetchone()
            if not source:
                return []
            identifiers = []
            for comparison, ordering in (("<", "DESC"), (">", "ASC")):
                row = conn.execute(
                    f"SELECT id FROM records WHERE country=? AND document_id=? AND source_hash=? AND rowid {comparison} ? AND source_type NOT IN ('heading','navigation','link','publication_preamble') ORDER BY rowid {ordering} LIMIT 1",
                    (country, source["document_id"], source["source_hash"], source["rowid"]),
                ).fetchone()
                if row:
                    identifiers.append(row["id"])
        items = []
        for identifier in identifiers:
            item = self.read(country, identifier)
            if item:
                item["metadata"].update(
                    retrieval_relation="adjacent_source_unit", adjacent_to_record_id=record_id
                )
                items.append(item)
        return items

    def referenced_articles(self, country, record_id, *, limit=4):
        """Follow explicit internal article references in the same immutable source."""
        if not self.path.is_file() or not record_id:
            return []
        found, seen, pending = [], {record_id}, [record_id]
        with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            for _ in range(2):
                next_ids = []
                for identifier in pending:
                    source = conn.execute(
                        "SELECT * FROM records WHERE country=? AND id=?", (country, identifier)
                    ).fetchone()
                    if not source:
                        continue
                    text = source["text"]
                    for number in self.article_references(text):
                        targets = conn.execute(
                            "SELECT id FROM records WHERE country=? AND document_id=? AND source_hash=? AND article=? ORDER BY rowid",
                            (country, source["document_id"], source["source_hash"], number),
                        )
                        for target in targets:
                            if target["id"] in seen:
                                continue
                            seen.add(target["id"])
                            item = self.read(country, target["id"])
                            if item:
                                item["metadata"]["retrieval_relation"] = "explicit_article_reference"
                                item["metadata"]["referenced_by_record_id"] = identifier
                                found.append(item)
                                next_ids.append(target["id"])
                                if len(found) >= limit:
                                    return found
                pending = next_ids
        return found

    @classmethod
    def article_references(cls, text):
        references = re.findall(
            r"sebagaimana\s+dimaksud(?:\s+dalam)?\s+Pasal\s+\d+\b|本(?:法|条例|办法|规定)第[一二三四五六七八九十百零〇\d]+条",
            text,
            re.I,
        )
        return list(dict.fromkeys(cls._requested_article(reference) for reference in references))

    @staticmethod
    def _requested_article(query):
        article = re.search(
            r"(?:第\s*([\d一二三四五六七八九十百零〇]+)\s*条|(?:pasal|article|section|regulation)\s+(\d+))",
            query,
            re.I,
        )
        number = next(group for group in article.groups() if group) if article else None
        if number and not number.isdigit():
            digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
            digits["〇"] = 0
            if "百" in number:
                head, number = number.split("百", 1)
                hundreds = digits.get(head, 1) * 100
            else:
                hundreds = 0
            if "十" in number:
                head, tail = number.split("十", 1)
                number = str(hundreds + digits.get(head, 1) * 10 + digits.get(tail, 0))
            else:
                number = str(hundreds + digits.get(number[-1:] or "零", 0))
        return number

    def _exact_rows(self, conn, country, query):
        documents = conn.execute(
            "SELECT * FROM records WHERE country=? AND rowid IN "
            "(SELECT min(rowid) FROM records WHERE country=? GROUP BY document_id)",
            (country, country),
        ).fetchall()
        metadata = {r["document_id"]: self.document_metadata(r) for r in documents}
        references = re.findall(r"(?:PP|UU|Perpres|Permendag|Peraturan)\s*(\d+)\s*/\s*(\d{4})", query, re.I)
        references += [(n, y) for y, n in re.findall(r"(\d{4})\s*年?第\s*(\d+)\s*号", query)]
        named = re.findall(r"《([^》]{2,100})》", query)
        named = [re.sub(r"[（(].*?[）)]", "", name).removeprefix("中华人民共和国") for name in named]
        aliases = {"SG": {"食品条例": "Food Regulations", "食品销售法": "Sale of Food Act"}}
        named.extend(english for chinese, english in aliases.get(country, {}).items() if chinese in query)
        if not named:
            for row in documents:
                title = metadata[row["document_id"]].get("catalog_title") or row["title"]
                short = re.sub(r"[（(].*?[）)]", "", title).removeprefix("中华人民共和国")
                # Some imported PDF blocks use the issuing authority as their
                # title. A regulator's name is not an exact statute match.
                norm_title = country != "CN" or re.search(
                    r"法$|法典$|办法$|条例$|规定$|决定$|解释$|标准$|公告$|规范$|通则$|指引$|指南$|第\d+号",
                    short,
                )
                if norm_title and len(short) >= 3 and short.casefold() in query.casefold():
                    named.append(short)
        numbers = re.findall(r"第\s*(\d+)\s*号令", query)
        terms = list(dict.fromkeys(named + [y for _, y in references] + numbers))
        if not terms:
            return []
        candidates = conn.execute(
            "SELECT * FROM records WHERE country=? AND (" + " OR ".join("title LIKE ?" for _ in terms) + ")",
            (country, *("%" + term + "%" for term in terms)),
        ).fetchall()
        if named:
            # PDF blocks can have an empty extracted title. Resolve a named
            # source using its catalog/heading, then rank its complete body.
            ids = [
                r["document_id"]
                for r in documents
                if any(
                    name.casefold()
                    in re.sub(
                        r"\([^)]*Amendment[^)]*\)\s*",
                        "",
                        str(metadata[r["document_id"]].get("catalog_title") or r["title"]),
                        flags=re.I,
                    ).casefold()
                    for name in named
                )
            ]
            if ids:
                candidates.extend(
                    conn.execute(
                        "SELECT * FROM records WHERE country=? AND document_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        (country, *ids),
                    ).fetchall()
                )

        def matches_name(name, title):
            # Short instrument names such as 民法典 and 海关法 must identify
            # the instrument itself, rather than every commentary citing it.
            if country == "CN" and len(name) <= 3:
                return re.sub(r"[（(].*?[）)]", "", title).removeprefix("中华人民共和国") == name
            return name.casefold() in title.casefold()

        rows = []
        for row in candidates:
            if row["document_id"] not in metadata:
                metadata[row["document_id"]] = self.document_metadata(row)
            title = metadata[row["document_id"]].get("catalog_title") or row["title"]
            family_title = re.sub(r"\([^)]*Amendment[^)]*\)\s*", "", title, flags=re.I)
            identity = re.search(r"(\d{4})年第\s*(\d+)\s*号", title)
            if identity and (identity[2], identity[1]) in references:
                rows.append(row)
            elif any(re.search(rf"\b{re.escape(n)}\s*/\s*{re.escape(y)}\b", title) for n, y in references):
                rows.append(row)
            elif any(
                (matches_name(name, title) or matches_name(name, family_title))
                and not re.search(r"^第[一二三四五六七八九十\d]+条", title)
                for name in named
            ):
                rows.append(row)
            elif any(re.search(rf"第?\s*{re.escape(n)}\s*号令?", title) for n in numbers):
                rows.append(row)
        # Extracted PDF titles may differ between blocks. A matched immutable
        # document brings in its remaining body, including late-numbered rules.
        if rows:
            ids = list(dict.fromkeys(r["document_id"] for r in rows))
            rows = conn.execute(
                "SELECT * FROM records WHERE country=? AND document_id IN ("
                + ",".join("?" for _ in ids)
                + ")",
                (country, *ids),
            ).fetchall()
        return rows

    def _related_rows(self, conn, country, query, seeds):
        """Two bounded hops over source titles/references, not legal answers."""

        def normalize(text):
            return re.sub(r"\s+", "", text)

        def subject_text(text):
            # Shared publishing authorities identify provenance, not a legal
            # dependency. Otherwise 中华人民共和国 or 全国人民代表大会
            # connects unrelated amendment decisions to almost every query.
            return re.sub(
                r"中华人民共和国|全国人民代表大会常务委员会|全国人民代表大会|最高人民法院|最高人民检察院|国务院|海关总署",
                "",
                normalize(text),
            )

        matches, formal_titles = [], []
        seed_ids = list(dict.fromkeys(r["document_id"] for r in seeds))[:6]
        # A broad business query may not name a procedure retained in a lower
        # regulation. Follow the procedure actually recalled, including notices
        # that changed it, before the caller spends its source-context budget.
        reference_text = normalize(query) + "\n" + "\n".join(normalize(r["text"])[:1800] for r in seeds)
        subject_reference = subject_text(reference_text)
        reference_phrases = {subject_reference[i : i + 6] for i in range(len(subject_reference) - 5)}
        # Resolve an unnamed procedure by a shared phrase in its formal title.
        # Six Han characters avoids broad terms such as food/import/registration.
        for row in conn.execute(
            "SELECT * FROM records WHERE country=? AND rowid IN "
            "(SELECT min(rowid) FROM records WHERE country=? GROUP BY document_id)",
            (country, country),
        ):
            title = self.document_metadata(row).get("catalog_title") or row["title"]
            base = re.sub(r"[（(].*?[）)]|\s+", "", title).removeprefix("中华人民共和国")
            if re.search(r"(?:法|条例|办法|规定|细则)$", base):
                formal_titles.append((base, title))
            runs = re.findall(r"[\u3400-\u9fff]+", subject_text(base))
            changing = bool(re.search(r"取消|停止办理|废止|修订|修改", base))
            # A recalled rule can name a procedure without book-title marks.
            # Follow that name as well as express citations; otherwise the
            # enabling law and later amendments are invisible to broad queries.
            shared = 0
            for run in runs:
                for i in range(len(run) - 5):
                    if run[i : i + 6] not in reference_phrases:
                        continue
                    size = 6
                    while size < min(len(run) - i, 30) and run[i : i + size + 1] in subject_reference:
                        size += 1
                    shared = max(shared, size)
            if shared and (changing or re.search(r"(?:法|条例|办法|规定|细则)$", base)):
                matches.append((shared, changing, title))

        # Formal references can be broken across PDF lines. Read the beginning
        # of each selected source and follow its express enabling-law citation.
        def references(document_ids):
            found = []
            for identifier in document_ids[:12]:
                text = "".join(
                    r[0]
                    for r in conn.execute(
                        "SELECT text FROM records WHERE country=? AND document_id=? ORDER BY rowid LIMIT 8",
                        (country, identifier),
                    )
                )
                text = normalize(text)[:6000]
                found.extend(re.findall(r"《([^》]{2,100})》", text))
                found.extend(title for base, title in formal_titles if len(base) >= 3 and base in text)
            return found

        # Long specific procedure names outrank generic shared regulator names.
        # Express references take priority over incidental title overlap.
        ordered_matches = sorted(matches, reverse=True)
        names = list(
            dict.fromkeys(
                [
                    *(m[2] for m in ordered_matches if m[0] >= 8),
                    *references(seed_ids),
                    *(m[2] for m in ordered_matches),
                ]
            )
        )[:12]
        rows = self._exact_rows(conn, country, " ".join(f"《{name}》" for name in names)) if names else []
        parents = list(dict.fromkeys(r["document_id"] for r in rows))
        titles = {
            r["document_id"]: self.document_metadata(r).get("catalog_title") or r["title"] for r in rows
        }
        parents.sort(
            key=lambda identifier: next(
                (i for i, name in enumerate(names) if name in titles[identifier]), len(names)
            )
        )
        parents = parents[:12]
        more = [name for name in references(parents) if name not in names][:8]
        if more:
            rows.extend(self._exact_rows(conn, country, " ".join(f"《{name}》" for name in more)))
        return list({r["id"]: r for r in rows}.values())

    def _item(self, row, fingerprint, words, *, full, match=None, context_units=None):
        country = row["country"]
        metadata = self.document_metadata(row)
        # Keep the printed hierarchy with the original provision. A matching
        # article number or a high retrieval score does not establish relevance.
        headings = _source_scopes(
            str(self.path), self.path.stat().st_mtime_ns, country, row["document_id"], row["source_hash"]
        ).get(row["id"], ())
        title = (
            "法规原文（名称待核对）"
            if metadata.get("title_status") == "unresolved"
            else metadata.get("catalog_title") or row["title"] or row["document_id"]
        )
        guidance = bool(re.search(r"指南|办理说明|办事说明|Layanan", title, re.I))
        original, translated = row["text"], row["translation"]
        original_limit, translated_limit = (20000, 10000) if full else (2200, 1200)
        excerpt = self._excerpt(original or translated, words, original_limit)
        if match and not full and match["text_kind"] == "text":
            start = max(0, match["start"] - 100)
            excerpt = original[start : start + original_limit]
        if original and translated:
            excerpt += "\n中文整理（非官方译文）：\n" + self._excerpt(translated, words, translated_limit)
        content_hash = hashlib.sha256((original + "\0" + translated).encode()).hexdigest()
        source_version = row["source_hash"] or fingerprint
        document_identity = row["document_id"]
        if row["id"].startswith("SG:guidance:"):
            document_identity += ":chinese-summary"
        document_id = "pack-" + country + "-" + hashlib.sha256(document_identity.encode()).hexdigest()[:24]
        return {
            "document_id": document_id,
            "document_version": int(hashlib.sha256(source_version.encode()).hexdigest()[:7], 16) + 1,
            "title": title[:1000],
            "excerpt": excerpt,
            "country": country,
            "source_url": row["source_url"] or None,
            "locator": {
                "unit_id": row["unit_id"],
                "article": None if guidance else row["article"] or None,
                **({"section": row["article"]} if guidance and row["article"] else {}),
                "source_hash": source_version,
                **(
                    {
                        "headings": [
                            {"title": label, "record_id": identifier} for label, identifier in headings
                        ]
                    }
                    if headings
                    else {}
                ),
                **({"context_record_ids": context_units} if context_units else {}),
            },
            "chunk_id": hashlib.sha256(row["id"].encode()).hexdigest(),
            "chunk_hash": content_hash,
            "metadata_revision": 0,
            "metadata": {
                "kind": (
                    "case"
                    if "判决" in row["source_type"]
                    else "law"
                    if re.search(r"规范类型：.*(?:法律|法规|条例|规章|司法解释|条约)", row["source_type"])
                    else "guidance"
                ),
                "review_status": "draft",
                "jurisdiction": country,
                "language": row["language"],
                "published_at": None,
                "effective_from": None,
                "effective_to": None,
                "source_type": row["source_type"],
                "evidence_store": "country_pack",
                "content_role": "secondary_summary"
                if row["id"].startswith("SG:guidance:")
                or (
                    country == "SG"
                    and (urlsplit(row["source_url"] or "").hostname or "").casefold() == "laws.sg"
                )
                else "source_text",
                "index_fingerprint": fingerprint,
                "source_record_id": row["id"],
                "effectivity": "unreviewed",
                "source_location_verified": (
                    row["status"] != "source_location_unverified" if country == "CN" else None
                ),
                "context_complete": (
                    full
                    or not (
                        country == "SG" and row["title"] and row["source_type"] in {"paragraph", "table_row"}
                    )
                )
                and len(original or translated) <= original_limit
                and (not original or len(translated) <= translated_limit),
                "source_characters": len(original),
                "translation_characters": len(translated),
                **metadata,
            },
        }

    @staticmethod
    def _excerpt(text, words, limit):
        if len(text) <= limit:
            return text
        positions = [text.casefold().find(word) for word in words if word in text.casefold()]
        start = max(0, min(positions) - 150) if positions else 0
        return text[start : start + limit]
