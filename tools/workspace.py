# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def data_root() -> Path:
    """Root directory for persistent server artifacts (logs, build output, temp).

    FPGAZERO_DATA_DIR overrides; the default is <package>/no_commit rather than
    the process cwd — MCP clients may launch the server from an arbitrary
    directory, and artifacts must not be scattered into it.
    """
    env = os.environ.get("FPGAZERO_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "no_commit"


def _candidate_tmp_roots() -> list[Path]:
    roots: list[Path] = []

    env_root = os.environ.get("FPGAZERO_TMPDIR", "").strip()
    if env_root:
        roots.append(Path(env_root))

    # Prefer a package-local scratch directory when possible. This avoids
    # flaky temp-directory behavior on some Windows environments.
    roots.append(data_root() / "fpgazero_tmp")

    roots.append(Path(tempfile.gettempdir()))
    return roots


@contextmanager
def temporary_workspace(prefix: str) -> Iterator[str]:
    last_error: Exception | None = None

    for root in _candidate_tmp_roots():
        tmpdir: Path | None = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            tmpdir = root / f"{prefix}{uuid4().hex}"
            tmpdir.mkdir()

            # Verify the directory is actually writable before we hand it off.
            probe = tmpdir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            last_error = exc
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            continue

        try:
            yield str(tmpdir)
            return
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    if last_error is None:
        raise OSError("Could not create a temporary workspace.")
    raise OSError(f"Could not create a temporary workspace: {last_error}")
