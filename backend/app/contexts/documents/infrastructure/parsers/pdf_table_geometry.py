"""Painted rectangular table geometry, including explicitly merged cells.

Every boundary segment must be either wholly present or wholly absent between
adjacent axis positions. Missing segments join cells only when the resulting
region is rectangular and its entire perimeter is painted. Partial borders,
interior dividers in a merged region and independent tables remain ambiguous.
"""


def _coverage(intervals, start, end):
    relevant = [(a, b) for a, b in intervals if b > start + 1.5 and a < end - 1.5]
    if not relevant:
        return False
    cursor = start
    for left, right in sorted(relevant):
        if left > cursor + 1.5:
            return None
        cursor = max(cursor, right)
    return True if cursor >= end - 1.5 else None


def rectangular_regions(xs, ys, horizontal, vertical):
    """Return a region partition or None; never infer an unpainted perimeter."""
    rows, columns = len(ys) - 1, len(xs) - 1
    if not 2 <= rows <= 400 or not 2 <= columns <= 64:
        return None
    horizontal_edges = [
        [
            _coverage([(a, b) for p, a, b in horizontal if abs(p - y) <= 0.8], left, right)
            for left, right in zip(xs, xs[1:])
        ]
        for y in ys
    ]
    vertical_edges = [
        [_coverage([(a, b) for p, a, b in vertical if abs(p - x) <= 0.8], top, bottom) for x in xs]
        for top, bottom in zip(ys, ys[1:])
    ]
    if (
        any(value is None for edges in (*horizontal_edges, *vertical_edges) for value in edges)
        or not all(horizontal_edges[0])
        or not all(horizontal_edges[-1])
        or not all(edges[0] and edges[-1] for edges in vertical_edges)
    ):
        return None
    parents = list(range(rows * columns))

    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def join(left, right):
        parents[find(left)] = find(right)

    for row in range(rows):
        for column in range(columns):
            cell = row * columns + column
            if column + 1 < columns and not vertical_edges[row][column + 1]:
                join(cell, cell + 1)
            if row + 1 < rows and not horizontal_edges[row + 1][column]:
                join(cell, cell + columns)
    groups = {}
    for row in range(rows):
        for column in range(columns):
            groups.setdefault(find(row * columns + column), []).append((row, column))
    regions, owners = [], [[None] * columns for _ in range(rows)]
    for coordinates in groups.values():
        first_row = min(row for row, _ in coordinates)
        last_row = max(row for row, _ in coordinates)
        first_column = min(column for _, column in coordinates)
        last_column = max(column for _, column in coordinates)
        if len(coordinates) != (last_row - first_row + 1) * (last_column - first_column + 1):
            return None
        for row in range(first_row, last_row + 1):
            for column in range(first_column, last_column + 1):
                if (
                    column < last_column
                    and vertical_edges[row][column + 1]
                    or row < last_row
                    and horizontal_edges[row + 1][column]
                ):
                    return None
            if not vertical_edges[row][first_column] or not vertical_edges[row][last_column + 1]:
                return None
        for column in range(first_column, last_column + 1):
            if not horizontal_edges[first_row][column] or not horizontal_edges[last_row + 1][column]:
                return None
        region = {
            "row": first_row,
            "column": first_column,
            "rowspan": last_row - first_row + 1,
            "colspan": last_column - first_column + 1,
            "bounds": [xs[first_column], ys[first_row], xs[last_column + 1], ys[last_row + 1]],
        }
        owner = len(regions)
        regions.append(region)
        for row, column in coordinates:
            owners[row][column] = owner
    return regions, owners
