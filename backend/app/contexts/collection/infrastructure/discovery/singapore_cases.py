"""Dedicated, immutable SG-LegalCite CSV acquisition and candidate validation.

The large raw dataset is kept outside document ingestion and RAG. A citation
candidate is never legal approval, and long judgment fields are not exported.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import heapq
import json
import os
import re
import stat
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

from app.shared.domain.values import now
from app.shared.infrastructure.blob_store import BlobStore, _verify_existing
from app.shared.infrastructure.http.download import DownloadError, _download, _validated_url

REPOSITORY = "ShannonLeeYuehErn/SG-LegalCite"
FILENAME = "COMBINED_ALL_CASES_FINAL_V2.csv"
DECLARED_SHA256 = "22994a94441fe8d7ea3d3ad19a43c25d908b23127cef6c3dd2a49bd3e33aad5e"
MAX_DATASET_BYTES = 1024 * 1024 * 1024
MAX_ROWS = 300_000
MAX_FIELD_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 150
MAX_VERIFIED = 50
CASE_PARSER_VERSION = "sg-legalcite-byte-encoding-and-citation-identifiers-v3"

_ALIASES = {
    "year": ("Year",),
    "citing_reference": ("Judgment_Reference", "Full_Reference"),
    "citing_url": ("Judgment_URL", "URL"),
    "facts": ("Fact_Query", "Extract of Facts"),
    "case_name": ("Case Name", "Case_Name"),
    "citing_court_type": ("Court_Type",),
    "citing_case_number": ("Case_Number",),
    "current_court_level": ("Current Court Level",),
    "principle": ("Key Principles Illustrated", "Principle"),
    "issue": ("Issue",),
    "issue_group": ("Issue Group",),
    "cited_case": ("Cited Case",),
    "paragraph": ("Paragraph",),
}
_OPTIONAL = {
    "cited_court_level": ("Court Level",),
    "precedential_weight": ("Precedential Weight (Binding, Comity, or Persuasive)",),
}
_TOPICS = {
    "sale_quality": (
        "sale of goods",
        "merchantable",
        "satisfactory quality",
        "fitness for",
        "supply of goods",
        "defective goods",
    ),
    "contract_payment": ("breach of contract", "contractual", "payment", "damages", "termination"),
    "agency_distribution": ("distribution", "distributor", "agency", "principal and agent", "licensing"),
    "misrepresentation_consumer": ("misrepresentation", "misleading", "consumer", "unfair practice"),
    "trade_marks": ("trade mark", "trademark", "passing off", "counterfeit"),
    "customer_data": ("personal data", "privacy", "marketing"),
    "storage_logistics": ("warehouse", "bailment", "storage", "carriage", "freight", "logistics"),
    "lease_employment": ("lease", "tenancy", "employment", "employee"),
    "commercial_remedies": (
        "injunction",
        "specific performance",
        "restitution",
        "quantum meruit",
        "limitation",
    ),
}
_EXCLUDED_DOCTRINES = re.compile(
    r"\b(?:criminal sentencing|criminal procedure|matrimonial|divorce|child custody|family law|"
    r"securities regulation|listing rules?|banking regulation)\b",
    re.I,
)
_CITATION = re.compile(
    r"\[(?:17|18|19|20)\d{2}\]\s+(?:"
    r"SGCAI?|SGHCF?|SGHCR|UKSC|UKHL|EWCA\s+(?:Civ|Crim)|EWHC|"
    r"(?:\d+\s+)?(?:SLR(?:\s*\(R\))?|AC|QB|Ch|WLR|All\s+ER)"
    r")\s+\d+(?:\s*\(\d+\))?"
)


class DatasetValidationError(ValueError):
    """A source, immutable version, or structured-field check failed."""


def write_json(output: Path, name: str, value: dict) -> None:
    temporary = output / (name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output / name)
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def dataset_metadata(output: Path, *, revision="main", expected_sha256=DECLARED_SHA256, timeout=30) -> dict:
    """Freeze one official repository commit and exact LFS size/hash before I/O."""
    if revision != "main" and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DatasetValidationError("dataset revision must be main or an immutable commit")
    if not _sha(expected_sha256) or not 1 <= timeout <= 90:
        raise DatasetValidationError("dataset metadata bounds are invalid")
    store = BlobStore(output / "metadata", max_bytes=4 * 1024 * 1024)
    proofs = []

    def get(url):
        response = _download(
            url,
            deadline=time.monotonic() + timeout,
            max_bytes=4 * 1024 * 1024,
            accept_compressed_json=True,
            consume=lambda chunks: store.put("SG-LegalCite-metadata", chunks),
        )
        if (
            urlsplit(response["url"]).hostname != "huggingface.co"
            or response["mime_type"] != "application/json"
        ):
            raise DatasetValidationError("dataset metadata response is not the official JSON API")
        blob = response["blob"]
        proofs.append(
            {
                "url": url,
                "content_hash": blob.digest,
                "size": blob.size,
                "path": str(blob.path.relative_to(output)),
            }
        )
        return json.loads(blob.path.read_text(encoding="utf-8"))

    repository = get(f"https://huggingface.co/api/datasets/{REPOSITORY}/revision/{revision}")
    commit = repository.get("sha") if isinstance(repository, dict) else None
    if (
        not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or (revision != "main" and commit != revision)
    ):
        raise DatasetValidationError("repository did not identify the requested immutable commit")
    tree = get(f"https://huggingface.co/api/datasets/{REPOSITORY}/tree/{commit}?recursive=false&expand=false")
    files = (
        [entry for entry in tree if isinstance(entry, dict) and entry.get("path") == FILENAME]
        if isinstance(tree, list)
        else []
    )
    if len(files) != 1:
        raise DatasetValidationError("official repository tree does not identify exactly one requested CSV")
    entry, lfs = files[0], files[0].get("lfs", {})
    size, digest = entry.get("size"), lfs.get("oid") if isinstance(lfs, dict) else None
    if type(size) is not int or not 1 <= size <= MAX_DATASET_BYTES or digest != expected_sha256:
        raise DatasetValidationError("dataset size or immutable SHA256 does not match the reviewed source")
    if lfs.get("size", size) != size:
        raise DatasetValidationError("repository and LFS sizes disagree")
    result = {
        "repository": REPOSITORY,
        "filename": FILENAME,
        "commit": commit,
        "size": size,
        "sha256": digest,
        "source_url": f"https://huggingface.co/datasets/{REPOSITORY}/resolve/{commit}/{FILENAME}?download=true",
        "metadata_proofs": proofs,
        "data_card_license": repository.get("cardData", {}).get("license"),
        "license_review_status": "attribution_and_third_party_judgment_rights_require_separate_review",
    }
    write_json(output, "source.json", result)
    return result


class DatasetRanges:
    """Restartable range parts, each bound to total size, strong ETag and hashes."""

    def __init__(
        self,
        output: Path,
        source: dict,
        *,
        part_bytes=8 * 1024 * 1024,
        workers=4,
        timeout=90,
        time_budget=1800,
    ):
        if not 64 * 1024 <= part_bytes <= 16 * 1024 * 1024 or not 1 <= workers <= 8:
            raise DatasetValidationError("dataset parts require 64 KiB–16 MiB and 1–8 workers")
        if not 1 <= timeout <= 90 or not 1 <= time_budget <= 7200:
            raise DatasetValidationError("dataset time budget is invalid")
        if (
            not _sha(source.get("sha256"))
            or type(source.get("size")) is not int
            or not 1 <= source["size"] <= MAX_DATASET_BYTES
        ):
            raise DatasetValidationError("dataset source size/hash are invalid")
        commit = source.get("commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise DatasetValidationError("dataset source needs an immutable commit")
        expected = f"https://huggingface.co/datasets/{REPOSITORY}/resolve/{commit}/{FILENAME}?download=true"
        if (
            source.get("source_url") != expected
            or source.get("repository") != REPOSITORY
            or source.get("filename") != FILENAME
        ):
            raise DatasetValidationError("dataset source is outside the one reviewed repository file")
        self.output, self.source = output, source
        self.part_bytes, self.workers, self.timeout = part_bytes, workers, timeout
        self.deadline = time.monotonic() + time_budget
        self.store = BlobStore(output / "parts", max_bytes=part_bytes)
        state_file = output / "download.json"
        if state_file.exists():
            fd = os.open(state_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                if (
                    not stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
                    or os.fstat(handle.fileno()).st_size > 2 * 1024 * 1024
                ):
                    raise DatasetValidationError("dataset checkpoint is not a bounded regular file")
                self.state = json.load(handle)
            if self.state.get("source") != source or self.state.get("part_bytes") != part_bytes:
                raise DatasetValidationError("dataset checkpoint source or part size changed")
        else:
            self.state = {
                "version": 1,
                "status": "downloading",
                "source": source,
                "part_bytes": part_bytes,
                "etag": None,
                "parts": {},
                "started_at": now(),
            }

    def checkpoint(self):
        self.state["updated_at"] = now()
        write_json(self.output, "download.json", self.state)

    def part_path(self, part):
        digest = part.get("content_hash")
        if not _sha(digest):
            raise DatasetValidationError("dataset part hash is invalid")
        owner = hashlib.sha256(b"SG-LegalCite-ranges").hexdigest()
        path = self.store.blobs_dir / owner / digest[:2] / digest[2:4] / digest
        if part.get("path") != str(path.relative_to(self.output)):
            raise DatasetValidationError("dataset part path is outside its immutable hash namespace")
        return path

    def fetch(self, start):
        end = min(start + self.part_bytes, self.source["size"]) - 1
        if time.monotonic() >= self.deadline:
            raise DownloadError("数据集下载预算已用完；已验证分段保留以供续跑。", retryable=True)
        fetched = _download(
            self.source["source_url"],
            deadline=min(self.deadline, time.monotonic() + self.timeout),
            max_bytes=self.part_bytes,
            byte_range=(start, end),
            range_total_bytes=self.source["size"],
            range_validator=self.state["etag"],
            consume=lambda chunks: self.store.put("SG-LegalCite-ranges", chunks),
        )
        etag = fetched.get("etag")
        if not isinstance(etag, str) or not re.fullmatch(r'"[\x21\x23-\x7e]{1,1020}"', etag):
            raise DatasetValidationError("dataset range needs a strong ETag")
        if self.state["etag"] is not None and etag != self.state["etag"]:
            raise DatasetValidationError("dataset range belongs to a different immutable source version")
        blob = fetched["blob"]
        if (
            fetched.get("response_status") != 206
            or blob.size != end - start + 1
            or fetched.get("total_size") != self.source["size"]
        ):
            raise DatasetValidationError("dataset range bounds were not preserved")
        return {
            "start": start,
            "end": end,
            "size": blob.size,
            "content_hash": blob.digest,
            "path": str(blob.path.relative_to(self.output)),
            "etag": etag,
            "response_host": urlsplit(fetched["url"]).hostname,
            # Signed CDN redirect queries stay out of checkpoint/export data.
            "response_url_sha256": hashlib.sha256(fetched["url"].encode()).hexdigest(),
        }

    def download(self) -> Path:
        for key, part in self.state["parts"].items():
            start = int(key)
            end = min(start + self.part_bytes, self.source["size"]) - 1
            if (
                start < 0
                or start % self.part_bytes
                or start >= self.source["size"]
                or part.get("start") != start
                or part.get("end") != end
                or part.get("size") != end - start + 1
                or part.get("etag") != self.state["etag"]
            ):
                raise DatasetValidationError("dataset checkpoint has overlapping or changed ranges")
            _verify_existing(self.part_path(part), part["content_hash"], part["size"])
        if "0" not in self.state["parts"]:
            first = self.fetch(0)
            self.state["etag"] = first["etag"]
            self.state["parts"]["0"] = first
            self.checkpoint()
        missing = [
            start
            for start in range(0, self.source["size"], self.part_bytes)
            if str(start) not in self.state["parts"]
        ]
        self.state["status"] = "downloading"
        failures = []
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="tea-case-dataset") as pool:
            futures = {pool.submit(self.fetch, start): start for start in missing}
            for future in as_completed(futures):
                start = futures[future]
                try:
                    self.state["parts"][str(start)] = future.result()
                except (DownloadError, DatasetValidationError) as exc:
                    failures.append({"start": start, "error": str(exc), "error_type": type(exc).__name__})
                self.checkpoint()
        if failures:
            self.state.update(status="download_incomplete", failures=failures[:100])
            self.checkpoint()
            raise DatasetValidationError("dataset download is incomplete; verified parts remain resumable")
        return self.assemble()

    def assemble(self) -> Path:
        directory = self.output / "dataset"
        directory.mkdir(mode=0o700, exist_ok=True)
        destination = directory / (self.source["sha256"] + ".csv")
        if destination.exists():
            _verify_existing(destination, self.source["sha256"], self.source["size"])
        else:
            fd, name = tempfile.mkstemp(prefix="dataset-", suffix=".part", dir=directory)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    digest, size = hashlib.sha256(), 0
                    for start in range(0, self.source["size"], self.part_bytes):
                        part = self.state["parts"].get(str(start))
                        if not part:
                            raise DatasetValidationError("dataset original has missing ranges")
                        path = self.part_path(part)
                        _verify_existing(path, part["content_hash"], part["size"])
                        with path.open("rb") as stream:
                            while block := stream.read(65536):
                                digest.update(block)
                                size += len(block)
                                handle.write(block)
                    if size != self.source["size"] or digest.hexdigest() != self.source["sha256"]:
                        raise DatasetValidationError(
                            "assembled dataset does not match the reviewed full SHA256"
                        )
                    handle.flush()
                    os.fchmod(handle.fileno(), 0o400)
                    os.fsync(handle.fileno())
                os.link(temporary, destination, follow_symlinks=False)
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)
        self.state.update(
            status="full_hash_verified",
            full_hash_verified=True,
            dataset_path=str(destination.relative_to(self.output)),
        )
        self.state.pop("failures", None)
        self.checkpoint()
        return destination


def field_mapping(header: list[str]) -> dict:
    if (
        not 1 <= len(header) <= 40
        or len(set(header)) != len(header)
        or any(not field or len(field) > 200 for field in header)
    ):
        raise DatasetValidationError("CSV header has duplicate, empty or excessive fields")
    mapping = {}
    for key, aliases in {**_ALIASES, **_OPTIONAL}.items():
        present = [alias for alias in aliases if alias in header]
        if len(present) > 1 or (not present and key in _ALIASES):
            raise DatasetValidationError(f"CSV field mapping is missing or ambiguous: {key}")
        mapping[key] = present[0] if present else None
    return mapping


def _business_topics(row, mapping):
    doctrine = " ".join(row[mapping[key]] for key in ("issue", "issue_group"))
    if _EXCLUDED_DOCTRINES.search(doctrine):
        return [], 0
    text = (doctrine + " " + row[mapping["principle"]]).casefold()
    matches = {topic: sum(term in text for term in terms) for topic, terms in _TOPICS.items()}
    return [topic for topic, count in matches.items() if count], sum(matches.values())


def observed_encoding(path: Path) -> tuple[str, dict]:
    """Inspect all source bytes; a data-card codec is not proof of file encoding."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    count = 0
    with path.open("rb") as handle:
        bom = handle.read(3) == codecs.BOM_UTF8
        handle.seek(0)
        while block := handle.read(65536):
            pending_bytes = len(decoder.getstate()[0])
            try:
                decoder.decode(block, final=False)
            except UnicodeDecodeError as exc:
                return "latin-1", {
                    "strict_utf8_valid": False,
                    "invalid_byte_offset": count - pending_bytes + exc.start,
                    "basis": "official_data_card_latin1_after_utf8_validation_rejected",
                }
            count += len(block)
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return "latin-1", {
                "strict_utf8_valid": False,
                "invalid_byte_offset": count - len(decoder.getstate()[0]),
                "basis": "official_data_card_latin1_after_truncated_utf8_sequence",
            }
    return "utf-8-sig" if bom else "utf-8", {
        "strict_utf8_valid": True,
        "bytes_checked": count,
        "utf8_bom": bom,
        "basis": "all_original_bytes_validate_as_utf8_data_card_latin1_is_not_used",
    }


def citation_review_fields(raw_cited: str, context: str, mapping: dict) -> dict:
    """Literal identifiers aid a reviewer; context matches do not establish edges."""
    direct_references = sorted({match[0] for match in _CITATION.finditer(raw_cited)})[:20]
    label = None
    if len(raw_cited) <= 500 and "\n" not in raw_cited and "\r" not in raw_cited:
        if re.fullmatch(
            r"[^;\n]{2,180}\s+v\.?\s+[^;\n]{2,180}\s+(?:\[(?:17|18|19|20)\d{2}\]|\((?:17|18|19|20)\d{2}\))(?:\s+[\w(). /-]{1,80})?",
            raw_cited,
        ):
            label = raw_cited
        elif re.fullmatch(
            r"(?:Re|In re)\s+[^;\n]{2,250}\s+\[(?:17|18|19|20)\d{2}\](?:\s+[\w(). /-]{1,80})?", raw_cited
        ):
            label = raw_cited
    context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    reference_candidates = []
    for match in _CITATION.finditer(context):
        if len(reference_candidates) >= 20:
            break
        reference_candidates.append(
            {
                "reference": match[0],
                "source_field": mapping["paragraph"],
                "field_content_hash": context_hash,
                "character_start": match.start(),
                "character_end": match.end(),
                "target_identity_verified": False,
            }
        )
    paragraph_numbers = []
    for match in re.finditer(r"(?:^|\n)\s*(\d{1,4})\s+[A-Z(\[]", context):
        number = int(match[1])
        if number not in paragraph_numbers:
            paragraph_numbers.append(number)
            if len(paragraph_numbers) >= 20:
                break
    reasons = []
    if len(raw_cited) > 800 or "\n" in raw_cited or "\r" in raw_cited:
        reasons.append("cited_case_contains_long_quote")
    if not label and len(direct_references) != 1:
        reasons.append("cited_target_not_unambiguously_identified")
    return {
        "cited_case_label": label,
        "cited_references": direct_references,
        "context_reference_candidates": reference_candidates,
        "source_context_paragraph_numbers": paragraph_numbers,
        "paragraph_number_basis": "literal_csv_context_markers_pending_original_judgment_check",
        "citation_context_content_hash": context_hash,
        "cited_case_content_hash": hashlib.sha256(raw_cited.encode("utf-8")).hexdigest(),
        "citation_target_status": "pending_manual_identity_and_paragraph_verification",
        "field_hash_encoding": "utf-8_of_decoded_csv_value",
        "quarantine_reasons": reasons,
    }


def validate_csv(path: Path, source: dict, output: Path, *, candidate_limit=MAX_CANDIDATES) -> dict:
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= MAX_CANDIDATES:
        raise DatasetValidationError("manual candidate queue requires at most 150 records")
    _verify_existing(path, source["sha256"], source["size"])
    encoding, encoding_proof = observed_encoding(path)
    previous_limit = csv.field_size_limit(MAX_FIELD_BYTES)
    counts, topic_counts, heap, seen = Counter(), Counter(), [], set()
    judgments = {}
    total = eligible = metadata_mismatch = duplicate = matched_topics = unmatched_topics = 0
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            digest, observed_size = hashlib.sha256(), 0
            if encoding == "utf-8-sig":
                digest.update(codecs.BOM_UTF8)
                observed_size = len(codecs.BOM_UTF8)

            def bounded_lines():
                nonlocal observed_size
                while line := handle.readline(8 * MAX_FIELD_BYTES + 1):
                    if len(line) > 8 * MAX_FIELD_BYTES:
                        raise DatasetValidationError("CSV physical line exceeds its bounded row allowance")
                    raw = line.encode("utf-8" if encoding == "utf-8-sig" else encoding)
                    if len(raw) > 8 * MAX_FIELD_BYTES:
                        raise DatasetValidationError("CSV physical line exceeds its bounded byte allowance")
                    observed_size += len(raw)
                    if observed_size > source["size"]:
                        raise DatasetValidationError("CSV original changed during structured validation")
                    digest.update(raw)
                    yield line

            reader = csv.reader(bounded_lines(), strict=True)
            header = next(reader)
            mapping = field_mapping(header)
            previous_line = reader.line_num
            for number, values in enumerate(reader, 1):
                total += 1
                start_line, end_line = previous_line + 1, reader.line_num
                previous_line = end_line
                if (
                    total > MAX_ROWS
                    or len(values) != len(header)
                    or sum(len(value) for value in values) > 8 * MAX_FIELD_BYTES
                ):
                    raise DatasetValidationError("CSV has excessive rows or inconsistent bounded fields")
                row = dict(zip(header, values, strict=True))
                year_value = row[mapping["year"]].strip()
                if not re.fullmatch(r"(?:19|20)\d{2}", year_value):
                    raise DatasetValidationError("CSV citing Year is not a four-digit year")
                year = int(year_value)
                counts[year] += 1
                judgments.setdefault(year, set()).add(row[mapping["citing_url"]])
                if not 2016 <= year <= 2025:
                    continue
                reference = row[mapping["citing_reference"]].strip()
                url = row[mapping["citing_url"]].strip()
                try:
                    canonical, host = _validated_url(url)
                except DownloadError:
                    metadata_mismatch += 1
                    continue
                url_year = re.search(r"/(\d{4})_SG(?:CAI?|HCF?|HCR)_\d+(?:$|[/?])", canonical)
                citation_year = re.match(r"\[(\d{4})\]", reference)
                if (
                    host != "www.elitigation.sg"
                    or not url_year
                    or not citation_year
                    or int(url_year[1]) != year
                    or int(citation_year[1]) != year
                ):
                    metadata_mismatch += 1
                    continue
                topics, score = _business_topics(row, mapping)
                if not topics:
                    unmatched_topics += 1
                    continue
                matched_topics += 1
                principle = row[mapping["principle"]].strip()
                raw_cited = row[mapping["cited_case"]].strip()
                review = citation_review_fields(raw_cited, row[mapping["paragraph"]], mapping)
                cited_references, cited_hash = review["cited_references"], review["cited_case_content_hash"]
                dedup = hashlib.sha256(
                    (
                        " ".join(principle.casefold().split())
                        + "\n"
                        + canonical
                        + "\n"
                        + ("|".join(cited_references) or cited_hash)
                    ).encode()
                ).hexdigest()
                if dedup in seen:
                    duplicate += 1
                    continue
                seen.add(dedup)
                eligible += 1
                topic_counts.update(topics)
                quarantine = review["quarantine_reasons"]
                facts = row[mapping["facts"]].strip()
                if len(principle) > 8192 or len(facts) > 8192:
                    quarantine.append("candidate_preview_truncated_source_review_required")
                candidate = {
                    "id": dedup,
                    "source_dataset_sha256": source["sha256"],
                    "source_commit": source["commit"],
                    "source_locator": {"csv_record": number, "start_line": start_line, "end_line": end_line},
                    "year": year,
                    "citing_reference": reference[:500],
                    "citing_url": canonical,
                    "case_name": row[mapping["case_name"]][:1000],
                    "citing_court_type": row[mapping["citing_court_type"]][:200],
                    "citing_case_number": row[mapping["citing_case_number"]][:200],
                    "current_court_level": row[mapping["current_court_level"]][:200],
                    "facts_excerpt": facts[:8192],
                    "principle": principle[:8192],
                    "issue": row[mapping["issue"]][:1000],
                    "issue_group": row[mapping["issue_group"]][:1000],
                    "topics": topics,
                    "score": score,
                    **review,
                    "cited_court_level": row[mapping["cited_court_level"]][:200]
                    if mapping["cited_court_level"]
                    else None,
                    "precedential_weight": row[mapping["precedential_weight"]][:200]
                    if mapping["precedential_weight"]
                    else None,
                    "review_status": "quarantined" if quarantine else "candidate_pending_paragraph_review",
                    "quarantine_reasons": quarantine,
                    "rag_eligible": False,
                }
                entry = (score, year, -number, candidate)
                if len(heap) < candidate_limit:
                    heapq.heappush(heap, entry)
                elif entry[:3] > heap[0][:3]:
                    heapq.heapreplace(heap, entry)
            if observed_size != source["size"] or digest.hexdigest() != source["sha256"]:
                raise DatasetValidationError("CSV original hash changed during structured validation")
    except (csv.Error, UnicodeError, StopIteration) as exc:
        raise DatasetValidationError("CSV quoting, encoding or header validation failed") from exc
    finally:
        csv.field_size_limit(previous_limit)
    candidates = [entry[3] for entry in sorted(heap, key=lambda entry: entry[:3], reverse=True)]
    candidate_path = output / "candidates.jsonl"
    candidate_hash, candidate_size = hashlib.sha256(), 0
    fd = os.open(candidate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for candidate in candidates:
            serialized = json.dumps(candidate, ensure_ascii=False, allow_nan=False) + "\n"
            payload = serialized.encode("utf-8")
            candidate_hash.update(payload)
            candidate_size += len(payload)
            handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    result = {
        "status": "csv_verified_candidates_require_manual_review",
        "parser_version": CASE_PARSER_VERSION,
        "source": source,
        "full_hash_verified": True,
        "full_csv_parsed": True,
        "encoding": encoding,
        "encoding_proof": encoding_proof,
        "actual_header": header,
        "column_count": len(header),
        "field_mapping": mapping,
        "rows": total,
        "annual_counts": {str(year): counts[year] for year in sorted(counts)},
        "annual_count_unit": "case_principle_rows",
        "annual_unique_citing_judgments": {str(year): len(judgments[year]) for year in sorted(judgments)},
        "citing_year_range": [2016, 2025],
        "range_row_count": sum(counts[year] for year in range(2016, 2026)),
        "business_candidates_before_queue_limit": eligible,
        "business_topic_matched_before_deduplication": matched_topics,
        "no_included_business_topic_or_excluded_doctrine_rows": unmatched_topics,
        "business_topic_counts": dict(topic_counts),
        "metadata_mismatch_rows": metadata_mismatch,
        "metadata_validation_scope": "all_citing_rows_2016_through_2025_before_business_topic_filter",
        "deduplicated_rows": duplicate,
        "deduplication_basis": "normalized_principle_whitespace_and_case_with_citing_url_and_cited_identifier_or_field_hash",
        "candidate_count": len(candidates),
        "candidate_limit": candidate_limit,
        "quarantined_candidates": sum(
            candidate["review_status"] == "quarantined" for candidate in candidates
        ),
        "verified_propositions": 0,
        "verified_limit": MAX_VERIFIED,
        "verified_target": [20, 30],
        "rag_records": 0,
        "raw_dataset_enters_document_or_vector_index": False,
        "candidate_file": "candidates.jsonl",
        "candidate_file_sha256": candidate_hash.hexdigest(),
        "candidate_file_size": candidate_size,
        "candidate_case_labels": sum(bool(candidate["cited_case_label"]) for candidate in candidates),
        "candidate_context_references": sum(
            bool(candidate["context_reference_candidates"]) for candidate in candidates
        ),
        "raw_long_cited_fields_quarantined": sum(
            "cited_case_contains_long_quote" in candidate["quarantine_reasons"] for candidate in candidates
        ),
        "manual_review_questions": [
            "回查引用判决实际段落，核法律命题与必要裁判理由。",
            "核对被引案件名称及完整引称；上下文匹配仅是待核线索。",
            "核对先例层级、后续处理和六堡茶交易适用性，不以引用次数替代效力判断。",
            "分别确认数据许可署名及第三方判决原文权限，审核后才决定资料用途。",
        ],
        "excluded_export_fields": ["Paragraph", "Cited Case"],
        "completed_at": now(),
    }
    write_json(output, "report.json", result)
    return result
