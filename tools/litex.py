# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def _build_litex_cmd(board: str, args: list[str] | None) -> list[str]:
    if not board:
        raise ValueError("board is required")
    return [sys.executable, "-m", f"litex_boards.targets.{board}"] + (args or [])


def _run_litex(board: str, args: list[str] | None, timeout: int) -> dict:
    cmd = _build_litex_cmd(board, args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return {
            "success": result.returncode == 0,
            "cmd": cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except FileNotFoundError:
        return {"success": False, "error": "LiteX entrypoint not found."}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"LiteX timed out after {timeout} s."}


def litex_build(
    board: str,
    args: list[str] | None = None,
    output_dir: str | None = None,
    timeout: int = 600,
) -> dict:
    """Run LiteX board target with --build. Returns logs and output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(output_dir) if output_dir else Path(tmpdir) / "build"
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
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(output_dir) if output_dir else Path(tmpdir) / "soc"
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
