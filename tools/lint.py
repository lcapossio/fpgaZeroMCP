# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import subprocess

from tools.workspace import temporary_workspace


_SUFFIX_MAP = {
    "verilog": ".v",
    "systemverilog": ".sv",
    "vhdl": ".vhd",
}


def lint_hdl(
    code: str,
    language: str = "verilog",
    top_module: str | None = None,
) -> dict:
    """Lint HDL source using iverilog (Verilog/SV) or ghdl (VHDL)."""
    suffix = _SUFFIX_MAP.get(language, ".v")

    with temporary_workspace("lint_") as tmpdir:
        tmpfile = os.path.join(tmpdir, f"lint{suffix}")
        with open(tmpfile, "w", encoding="utf-8") as f:
            f.write(code)

        if language == "vhdl":
            cmd = ["ghdl", "-a", "--std=08", tmpfile]
        elif language == "systemverilog":
            cmd = ["iverilog", "-g2012", "-tnull", tmpfile]
        else:
            cmd = ["iverilog", "-tnull", tmpfile]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            tool = "ghdl" if language == "vhdl" else "iverilog"
            return {
                "success": False,
                "error": f"'{tool}' not found. Install it and ensure it is on PATH.",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Lint timed out after 30 s."}

        output = {
            "success": result.returncode == 0,
            "tool": cmd[0],
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
) -> dict:
    """Lint multiple HDL files together so cross-module references resolve.

    files: mapping of filename → source code, e.g. {"uart_tx.v": "...", "top.v": "..."}.
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            tool = "ghdl" if language == "vhdl" else "iverilog"
            return {
                "success": False,
                "error": f"'{tool}' not found. Install OSS CAD Suite.",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Lint timed out after {timeout} s."}

        output = {
            "success": result.returncode == 0,
            "tool": cmd[0],
            "language": language,
            "files": list(files.keys()),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.returncode == 0:
            output["message"] = "No errors found."
        return output
