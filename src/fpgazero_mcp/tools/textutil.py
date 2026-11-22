# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Text helpers for keeping tool responses compact for AI clients."""

from __future__ import annotations


def truncate_log(text: str, head: int = 20, tail: int = 60) -> tuple[str, bool]:
    """Keep the first `head` and last `tail` lines of a long log.

    EDA tool logs front-load banner/config lines and end with the results
    that matter, so head+tail preserves the useful parts. Returns
    (possibly-truncated text, whether truncation happened).
    """
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text, False
    omitted = len(lines) - head - tail
    kept = lines[:head] + [f"... [{omitted} lines omitted] ..."] + lines[-tail:]
    return "\n".join(kept), True
