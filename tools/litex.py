# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from tools.workspace import data_root, temporary_workspace

_BOARD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _build_litex_cmd(board: str, args: list[str] | None) -> list[str]:
    if not board:
        raise ValueError("board is required")
    if not _BOARD_RE.fullmatch(board):
        raise ValueError(
            f"Invalid board name '{board}'. "
            "Must be a valid Python module path (letters, digits, underscores, dots)."
        )
    return [sys.executable, "-m", f"litex_boards.targets.{board}"] + (args or [])


def _run_litex(board: str, args: list[str] | None, timeout: int) -> dict:
    cmd = _build_litex_cmd(board, args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout
        )
        return {
            "success": result.returncode == 0,
            "cmd": cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "error": "LiteX entrypoint not found.",
            "error_code": "tool_not_found",
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"LiteX timed out after {timeout} s.",
            "error_code": "timeout",
        }


def litex_build(
    board: str,
    args: list[str] | None = None,
    output_dir: str | None = None,
    timeout: int = 600,
) -> dict:
    """Run LiteX board target with --build. Returns logs and output directory."""
    with temporary_workspace("litex_build_"):
        if output_dir:
            out_dir = Path(output_dir)
        else:
            # Use a persistent directory so build artifacts survive temp cleanup
            out_dir = data_root() / "litex_build" / board
        out_dir.mkdir(parents=True, exist_ok=True)

        build_args = list(args or [])
        if "--build" not in build_args:
            build_args.append("--build")
        if "--output-dir" not in build_args:
            build_args += ["--output-dir", str(out_dir)]

        result = _run_litex(board, build_args, timeout)
        result["output_dir"] = str(out_dir)
        return result


def litex_soc(
    board: str,
    args: list[str] | None = None,
    output_dir: str | None = None,
    timeout: int = 300,
) -> dict:
    """Generate LiteX SoC without building gateware."""
    with temporary_workspace("litex_soc_"):
        if output_dir:
            out_dir = Path(output_dir)
        else:
            # Use a persistent directory so SoC artifacts survive temp cleanup
            out_dir = data_root() / "litex_soc" / board
        out_dir.mkdir(parents=True, exist_ok=True)

        soc_args = list(args or [])
        # Ensure we don't accidentally build
        if "--build" in soc_args:
            soc_args = [a for a in soc_args if a != "--build"]
        if "--no-compile" not in soc_args:
            soc_args.append("--no-compile")
        if "--output-dir" not in soc_args:
            soc_args += ["--output-dir", str(out_dir)]

        result = _run_litex(board, soc_args, timeout)
        result["output_dir"] = str(out_dir)
        return result


def litex_flow(
    board: str,
    args: list[str] | None = None,
    timeout: int = 600,
) -> dict:
    """Generic LiteX flow runner with caller-provided args."""
    return _run_litex(board, args or [], timeout)
