# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Writable locations for runtime state.

This lives at the package root rather than under tools/ because both tools/ and
registry/ need it, and registry/ must never import from tools/.

Nothing here may resolve relative to __file__: the package is installed into
site-packages, which is the wrong place -- and often not writable -- for build
logs, bitstreams and imported cores.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Base for everything the server writes. Kept beside the existing
#: ~/.fpgazero_mcp/config.json the registry already reads.
_DEFAULT_ROOT = Path.home() / ".fpgazero_mcp" / "work"


def data_root() -> Path:
    """Root directory for persistent server artifacts (logs, build output, temp).

    FPGAZERO_DATA_DIR overrides. The default is under the user's home directory,
    not the install directory and not the process cwd -- MCP clients may launch
    the server from an arbitrary directory, and artifacts must neither be
    scattered into it nor written back into the installed package.
    """
    env = os.environ.get("FPGAZERO_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return _DEFAULT_ROOT


def user_cores_dir() -> Path:
    """Writable destination for cores imported at runtime.

    Separate from the read-only cores bundled inside the package, which would
    otherwise mean `import_github_core` writing into site-packages.
    """
    return data_root() / "cores"
