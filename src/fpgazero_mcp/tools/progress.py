# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Progress reporting helper shared by long-running tools.

Tools accept an optional ``progress`` callback (fraction 0..1, message) and
invoke it at phase boundaries. The server wires it to MCP
``notifications/progress``; a raising callback must never break the tool run.
"""

from __future__ import annotations

from typing import Callable

ProgressFn = Callable[[float, str], None]


def make_reporter(progress: ProgressFn | None) -> ProgressFn:
    """Wrap an optional progress callback into a safe no-throw reporter."""

    def report(fraction: float, message: str) -> None:
        if progress is None:
            return
        try:
            progress(fraction, message)
        except Exception:
            pass

    return report
