"""Conservative evidence for a scope-only OCR retry, not legal accuracy proof."""

import re
from collections import Counter
from difflib import SequenceMatcher

POLICY = "physical-page-scope-content-and-cells-v2"
MAX_SIMILARITY_CHAR_PAIRS = 1_000_000


def _similarity(left, right):
    # Similarity is diagnostic only. Never spend quadratic work on a large
    # page or report an approximate value as the exact historical metric.
    if left == right:
        return 1.0, "identical"
    if len(left) * len(right) > MAX_SIMILARITY_CHAR_PAIRS:
        return None, "omitted_work_limit"
    return SequenceMatcher(None, left, right, autojunk=False).ratio(), "sequence_matcher"


def _row(line):
    line = line.strip()
    cells, start, escaped, pipes = [], 0, False, []
    for index, character in enumerate(line):
        if character == "|" and not escaped:
            pipes.append(index)
            cells.append(line[start:index])
            start = index + 1
        escaped = character == "\\" and not escaped
    if not pipes:
        return None
    cells.append(line[start:])
    # Remove exactly the external delimiters, never the empty edge cells.
    if pipes[0] == 0:
        cells = cells[1:]
    if pipes[-1] == len(line) - 1:
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def _semantic(text):
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}[ \t]+", "", text)
    # Remove paired presentation markers only; retain signs, separators,
    # case, punctuation and word boundaries that may change legal meaning.
    for _ in range(2):
        text = re.sub(r"(?<!\w)(\*{1,2}|_{1,2}|`)(?=\S)([^\n]*?\S)\1(?!\w)", r"\2", text)
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'}))
    text = " ".join(text.replace("\\|", "|").split())
    # Reflow commonly inserts a space after a visible hyphen or slash. Keep
    # the punctuation itself, and keep spaces between two words significant.
    return re.sub(r"\s*([/-])\s*", r"\1", text)


def _structure(text):
    blocks, rows, prose = [], [], []

    def flush_table():
        if rows:
            blocks.append(("table", list(rows)))
            rows.clear()

    def flush_prose():
        value = _semantic("\n".join(prose))
        if value:
            blocks.append(("prose", value))
        prose.clear()

    for line in text.splitlines():
        cells = _row(line)
        if cells is None:
            flush_table()
            prose.append(line)
        else:
            flush_prose()
            if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            rows.append([_semantic(cell) for cell in cells])
    flush_table()
    flush_prose()
    return blocks


def compare_page(before, after):
    old, new = before.get("text", ""), after.get("text", "")

    def letters(text):
        return "".join(
            char.casefold() for char in re.sub(r"<br\s*/?>", " ", text, flags=re.I) if char.isalnum()
        )

    left, right = letters(old), letters(new)
    similarity, similarity_basis = _similarity(left, right)
    old_tokens, new_tokens = re.findall(r"\d+", old), re.findall(r"\d+", new)
    old_blocks, new_blocks = _structure(old), _structure(new)
    tables = [[value for kind, value in blocks if kind == "table"] for blocks in (old_blocks, new_blocks)]
    rectangular = all(
        bool(table) and bool(table[0]) and all(len(row) == len(table[0]) for row in table)
        for document_tables in tables
        for table in document_tables
    )
    reasons = []
    for accepted, reason in (
        (left == right, "scope_retry_letters_digits_changed"),
        (old_tokens == new_tokens, "scope_retry_digit_order_changed"),
        (tables[0] == tables[1], "scope_retry_table_cells_changed"),
        (rectangular, "scope_retry_table_shape_ambiguous"),
        (old_blocks == new_blocks, "scope_retry_content_or_structure_changed"),
    ):
        if not accepted:
            reasons.append(reason)
    return {
        "comparison_policy": POLICY,
        "old_text_characters": len(old),
        "new_text_characters": len(new),
        "normalized_letters_digits_similarity": similarity,
        "normalized_letters_digits_similarity_basis": similarity_basis,
        "normalized_letters_digits_identical": left == right,
        "digit_sequence_unchanged": old_tokens == new_tokens,
        "digit_tokens_added": dict(Counter(new_tokens) - Counter(old_tokens)),
        "digit_tokens_removed": dict(Counter(old_tokens) - Counter(new_tokens)),
        "table_cells_unchanged": tables[0] == tables[1],
        "table_rows_rectangular": rectangular,
        "semantic_content_unchanged": old_blocks == new_blocks,
        "review_reasons": reasons,
        "scope_only_change_accepted": not reasons,
    }
