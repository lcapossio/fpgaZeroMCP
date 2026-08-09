# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
tools/lsp.py — LSP-like diagnostics and formatting for HDL.

Backends:
  Verilog / SystemVerilog:
    diagnostics  → Verilator (primary) or verible-verilog-lint (fallback)
    formatting   → verible-verilog-format
  VHDL:
    diagnostics  → GHDL
    formatting   → vsg (pip install vsg)

All tools are part of OSS CAD Suite except vsg.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from tools.lint import _win_to_wsl_path, _wsl_has_verilator
from tools.workspace import temporary_workspace

HDL_SUFFIX = {
    "verilog": ".v",
    "systemverilog": ".sv",
    "vhdl": ".vhd",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_diagnostics(code: str, language: str = "verilog") -> dict:
    """Return structured diagnostics (line, col, severity, message) for HDL code.

    Verilog/SV: tries Verilator first, falls back to verible-verilog-lint.
    VHDL:       uses GHDL.
    """
    suffix = HDL_SUFFIX.get(language, ".v")

    with temporary_workspace("diag_") as tmpdir:
        tmpfile = os.path.join(tmpdir, f"diag{suffix}")
        with open(tmpfile, "w", encoding="utf-8") as f:
            f.write(code)

        if language == "vhdl":
            return _ghdl_diagnostics(tmpfile)
        result = _verilator_diagnostics(tmpfile, language)
        if "error" in result:
            # Verilator missing — try Verible
            return _verible_lint(tmpfile)
        return result


def format_hdl(code: str, language: str = "verilog") -> dict:
    """Return formatted HDL code.

    Verilog/SV: verible-verilog-format
    VHDL:       vsg  (pip install vsg)
    """
    suffix = HDL_SUFFIX.get(language, ".v")

    with temporary_workspace("fmt_") as tmpdir:
        tmpfile = os.path.join(tmpdir, f"fmt{suffix}")
        with open(tmpfile, "w", encoding="utf-8") as f:
            f.write(code)

        if language == "vhdl":
            return _vsg_format(tmpfile, code)
        return _verible_format(tmpfile, code)


# ---------------------------------------------------------------------------
# Verilator diagnostics
# ---------------------------------------------------------------------------


def _verilator_diagnostics(tmpfile: str, language: str) -> dict:
    # -g2012 is an iverilog flag; Verilator's SystemVerilog switch is --sv
    flags = ["--sv"] if language == "systemverilog" else []
    # Same WSL fallback as tools.lint: on Windows without a native verilator,
    # run it inside WSL with translated paths.
    use_wsl = not shutil.which("verilator") and _wsl_has_verilator()
    target = _win_to_wsl_path(tmpfile) if use_wsl else tmpfile
    base = ["wsl", "verilator"] if use_wsl else ["verilator"]
    try:
        r = subprocess.run(
            base + ["--lint-only", "--error-limit", "50"] + flags + [target],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        diags = _parse_verilator(r.stdout + r.stderr, target)
        return {
            "success": r.returncode == 0,
            "tool": "verilator",
            "diagnostics": diags,
        }
    except FileNotFoundError:
        return {"error": "'verilator' not found", "error_code": "tool_not_found"}
    except subprocess.TimeoutExpired:
        return {"error": "Verilator timed out after 30 s.", "error_code": "timeout"}


def _parse_verilator(output: str, filename: str) -> list[dict]:
    """Parse Verilator output: %Error-CODE: file:line:col: message"""
    diags: list[dict] = []
    pat = re.compile(
        r"^%(Error|Warning)(?:-(\w+))?:\s*"
        + re.escape(filename)
        + r":(\d+):(?:(\d+):)?\s*(.+)$",
        re.MULTILINE,
    )
    for m in pat.finditer(output):
        level, code, line, col, msg = m.groups()
        diags.append(
            {
                "severity": "error" if level == "Error" else "warning",
                "code": code or "",
                "line": int(line),
                "col": int(col) if col else 1,
                "message": msg.strip(),
                "source": "verilator",
            }
        )
    return diags


# ---------------------------------------------------------------------------
# Verible diagnostics + formatting
# ---------------------------------------------------------------------------


def _verible_lint(tmpfile: str) -> dict:
    try:
        r = subprocess.run(
            ["verible-verilog-lint", tmpfile],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        diags = _parse_verible(r.stdout + r.stderr, tmpfile)
        return {
            "success": r.returncode == 0,
            "tool": "verible-verilog-lint",
            "diagnostics": diags,
        }
    except FileNotFoundError:
        return {
            "error": (
                "'verilator' and 'verible-verilog-lint' not found. "
                "Install OSS CAD Suite."
            ),
            "error_code": "tool_not_found",
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "verible-verilog-lint timed out after 30 s.",
            "error_code": "timeout",
        }


def _parse_verible(output: str, filename: str) -> list[dict]:
    """Parse verible-verilog-lint: file:line:col: message [rule-name]"""
    diags: list[dict] = []
    pat = re.compile(
        re.escape(filename) + r":(\d+):(\d+):\s*(.+?)(?:\s+\[(.+?)\])?$",
        re.MULTILINE,
    )
    for m in pat.finditer(output):
        line, col, msg, rule = m.groups()
        diags.append(
            {
                "severity": "warning",
                "code": rule or "",
                "line": int(line),
                "col": int(col),
                "message": msg.strip(),
                "source": "verible",
            }
        )
    return diags


def _verible_format(tmpfile: str, original: str) -> dict:
    try:
        r = subprocess.run(
            ["verible-verilog-format", tmpfile],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        if r.returncode == 0:
            return {
                "success": True,
                "tool": "verible-verilog-format",
                "formatted": r.stdout,
                "changed": r.stdout.strip() != original.strip(),
            }
        return {
            "success": False,
            "tool": "verible-verilog-format",
            "stderr": r.stderr,
            "formatted": original,
        }
    except FileNotFoundError:
        return {
            "error": "'verible-verilog-format' not found. Install OSS CAD Suite.",
            "error_code": "tool_not_found",
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "verible-verilog-format timed out after 30 s.",
            "error_code": "timeout",
        }


# ---------------------------------------------------------------------------
# GHDL diagnostics
# ---------------------------------------------------------------------------


def _ghdl_diagnostics(tmpfile: str) -> dict:
    # --workdir keeps the GHDL work library (work-obj08.cf) out of the
    # server's cwd and isolates concurrent analyses from each other.
    workdir = os.path.dirname(tmpfile)
    try:
        r = subprocess.run(
            ["ghdl", "-a", "--std=08", "--workdir=" + workdir, tmpfile],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        diags = _parse_ghdl(r.stdout + r.stderr, tmpfile)
        return {
            "success": r.returncode == 0,
            "tool": "ghdl",
            "diagnostics": diags,
        }
    except FileNotFoundError:
        return {
            "error": "'ghdl' not found. Install OSS CAD Suite.",
            "error_code": "tool_not_found",
        }
    except subprocess.TimeoutExpired:
        return {"error": "GHDL timed out after 30 s.", "error_code": "timeout"}


def _parse_ghdl(output: str, filename: str) -> list[dict]:
    """Parse GHDL output: file:line:col: error/warning: message"""
    diags: list[dict] = []
    pat = re.compile(
        re.escape(filename) + r":(\d+):(\d+):\s*(error|warning):\s*(.+)$",
        re.MULTILINE,
    )
    for m in pat.finditer(output):
        line, col, severity, msg = m.groups()
        diags.append(
            {
                "severity": severity,
                "code": "",
                "line": int(line),
                "col": int(col),
                "message": msg.strip(),
                "source": "ghdl",
            }
        )
    return diags


# ---------------------------------------------------------------------------
# vsg (VHDL Style Guide) formatting
# ---------------------------------------------------------------------------


def _vsg_format(tmpfile: str, original: str) -> dict:
    try:
        # vsg --fix edits the file in place
        r = subprocess.run(
            ["vsg", "--fix", "-f", tmpfile],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        formatted = Path(tmpfile).read_text(encoding="utf-8")
        return {
            "success": r.returncode in (0, 1),  # 1 = fixes were applied
            "tool": "vsg",
            "formatted": formatted,
            "changed": formatted.strip() != original.strip(),
        }
    except FileNotFoundError:
        return {
            "error": "'vsg' not found. Install with: pip install vsg",
            "error_code": "tool_not_found",
        }
    except subprocess.TimeoutExpired:
        return {"error": "vsg timed out after 30 s.", "error_code": "timeout"}
