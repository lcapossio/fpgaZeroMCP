# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from tools.workspace import temporary_workspace


_SUFFIX_MAP = {
    "verilog": ".v",
    "systemverilog": ".sv",
    "vhdl": ".vhd",
}


def _win_to_wsl_path(path: str) -> str:
    """Convert a Windows absolute path to its WSL /mnt/<drive>/... equivalent."""
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        rest = p[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return p


def _wsl_has_verilator() -> bool:
    """Return True if WSL is available and has verilator installed."""
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["wsl", "which", "verilator"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _verilator_cmd(
    files: list[str],
    language: str,
    top_module: str | None,
) -> list[str]:
    """Build a verilator command, falling back to WSL on Windows if needed."""
    use_wsl = not shutil.which("verilator") and _wsl_has_verilator()

    base_flags = ["--lint-only", "-Wall", "-Wno-DECLFILENAME"]
    if language == "systemverilog":
        base_flags.append("--sv")
    if top_module:
        base_flags += ["--top-module", top_module]

    if use_wsl:
        wsl_files = [_win_to_wsl_path(f) for f in files]
        cmd = ["wsl", "verilator"] + base_flags + wsl_files
    else:
        cmd = ["verilator"] + base_flags + files

    return cmd


def lint_hdl(
    code: str,
    language: str = "verilog",
    top_module: str | None = None,
    linter: str = "iverilog",
) -> dict:
    """Lint HDL source using iverilog/verilator (Verilog/SV) or ghdl (VHDL).

    linter: "iverilog" (default) or "verilator" — only applies to Verilog/SV.
    Verilator enables -Wall which includes MULTIDRIVEN net detection.
    """
    suffix = _SUFFIX_MAP.get(language, ".v")

    with temporary_workspace("lint_") as tmpdir:
        tmpfile = os.path.join(tmpdir, f"lint{suffix}")
        with open(tmpfile, "w", encoding="utf-8") as f:
            f.write(code)

        if language == "vhdl":
            cmd = ["ghdl", "-a", "--std=08", tmpfile]
        elif linter == "verilator":
            cmd = _verilator_cmd([tmpfile], language, top_module)
        elif language == "systemverilog":
            cmd = ["iverilog", "-g2012", "-tnull", tmpfile]
        else:
            cmd = ["iverilog", "-tnull", tmpfile]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=30
            )
        except FileNotFoundError:
            if language == "vhdl":
                tool = "ghdl"
            elif linter == "verilator":
                tool = "verilator"
            else:
                tool = "iverilog"
            return {
                "success": False,
                "error": f"'{tool}' not found. Install it and ensure it is on PATH.",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Lint timed out after 30 s."}

        output = {
            "success": result.returncode == 0,
            "tool": "verilator" if linter == "verilator" else cmd[0],
            "language": language,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.returncode == 0:
            output["message"] = "No errors found."
        return output


def lint_project(
    files: dict[str, str],
    language: str = "verilog",
    top_module: str | None = None,
    timeout: int = 60,
    linter: str = "iverilog",
) -> dict:
    """Lint multiple HDL files together so cross-module references resolve.

    files: mapping of filename → source code, e.g. {"uart_tx.v": "...", "top.v": "..."}.
    linter: "iverilog" (default) or "verilator" — only applies to Verilog/SV.
    Verilator enables -Wall which includes MULTIDRIVEN net detection.
    """
    if not files:
        return {"success": False, "error": "No files provided."}

    suffix = _SUFFIX_MAP.get(language, ".v")

    with temporary_workspace("lint_proj_") as tmpdir:
        written: list[str] = []
        for fname, code in files.items():
            # Ensure correct extension if not already present
            if not any(fname.lower().endswith(ext) for ext in _SUFFIX_MAP.values()):
                fname = fname + suffix
            fpath = os.path.join(tmpdir, fname)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            written.append(fpath)

        if language == "vhdl":
            cmd = ["ghdl", "-a", "--std=08", "--workdir=" + tmpdir] + written
        elif linter == "verilator":
            cmd = _verilator_cmd(written, language, top_module)
        elif language == "systemverilog":
            cmd = ["iverilog", "-g2012", "-tnull"]
            if top_module:
                cmd += ["-s", top_module]
            cmd += written
        else:
            cmd = ["iverilog", "-tnull"]
            if top_module:
                cmd += ["-s", top_module]
            cmd += written

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=timeout
            )
        except FileNotFoundError:
            if language == "vhdl":
                tool = "ghdl"
            elif linter == "verilator":
                tool = "verilator"
            else:
                tool = "iverilog"
            return {
                "success": False,
                "error": f"'{tool}' not found. Install OSS CAD Suite.",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Lint timed out after {timeout} s."}

        output = {
            "success": result.returncode == 0,
            "tool": "verilator" if linter == "verilator" else cmd[0],
            "language": language,
            "files": list(files.keys()),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.returncode == 0:
            output["message"] = "No errors found."
        return output
