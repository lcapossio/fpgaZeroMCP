# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import subprocess
import tempfile


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

    with tempfile.NamedTemporaryFile(suffix=suffix, mode="w", delete=False) as f:
        f.write(code)
        tmpfile = f.name

    try:
        if language == "vhdl":
            cmd = ["ghdl", "-a", "--std=08", tmpfile]
        elif language == "systemverilog":
            cmd = ["iverilog", "-g2012", "-tnull", tmpfile]
        else:
            cmd = ["iverilog", "-tnull", tmpfile]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

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

    except FileNotFoundError:
        tool = "ghdl" if language == "vhdl" else "iverilog"
        return {
            "success": False,
            "error": f"'{tool}' not found. Install it and ensure it is on PATH.",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Lint timed out after 30 s."}
    finally:
        os.unlink(tmpfile)
