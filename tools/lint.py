# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import subprocess

from tools.workspace import temporary_workspace


def lint_hdl(
    code: str,
    language: str = "verilog",
    top_module: str | None = None,
) -> dict:
    """Lint HDL source using iverilog (Verilog/SV) or ghdl (VHDL)."""
    suffix_map = {
        "verilog": ".v",
        "systemverilog": ".sv",
        "vhdl": ".vhd",
    }
    suffix = suffix_map.get(language, ".v")

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
