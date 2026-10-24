"""Shared physical-page bound for file-backed archive processing.

Uploading bytes retains its separate 300-page bound. This limit does not
increase per-window, text, result, image, memory, or CPU budgets.
"""

ARCHIVE_MAX_PAGES = 10_000
