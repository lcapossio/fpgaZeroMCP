# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Shared test configuration.

Test scratch directories are created with tempfile.mkdtemp(), which lands in
/tmp on Linux/macOS — outside the roots (cwd, $HOME) that project_dir/work_dir
validation allows. Whitelist the system temp dir for the whole test session so
those tests exercise the tools rather than the path guard. (On Windows the
temp dir is already under $HOME, so this is a no-op in effect.)
"""

from __future__ import annotations

import os
import tempfile

_tmp = tempfile.gettempdir()
_existing = os.environ.get("FPGAZERO_ALLOWED_DIRS", "")
if _tmp not in _existing.split(os.pathsep):
    os.environ["FPGAZERO_ALLOWED_DIRS"] = (
        f"{_existing}{os.pathsep}{_tmp}" if _existing else _tmp
    )
