# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Check which EDA tools are installed and reachable."""
from __future__ import annotations

import shutil
import subprocess


_TOOLS = [
    ("iverilog",              "Icarus Verilog (lint, simulate)"),
    ("vvp",                   "Icarus Verilog runtime"),
    ("yosys",                 "Yosys (synthesis)"),
    ("nextpnr-ice40",        "nextpnr for iCE40"),
    ("nextpnr-ecp5",         "nextpnr for ECP5"),
    ("ghdl",                  "GHDL (VHDL analysis/simulation)"),
    ("verilator",             "Verilator (lint, diagnostics)"),
    ("verible-verilog-lint",  "Verible linter"),
    ("verible-verilog-format","Verible formatter"),
    ("vsg",                   "VHDL Style Guide (formatter)"),
]


def check_tools() -> dict:
    """Return availability and version of each EDA tool."""
    results: list[dict] = []
    for binary, description in _TOOLS:
        entry: dict = {
            "tool": binary,
            "description": description,
            "installed": False,
        }
        path = shutil.which(binary)
        if path:
            entry["installed"] = True
            entry["path"] = path
            entry["version"] = _get_version(binary)
        results.append(entry)

    installed = sum(1 for r in results if r["installed"])
    return {
        "installed": installed,
        "total": len(results),
        "tools": results,
    }


def _get_version(binary: str) -> str:
    """Best-effort version string extraction."""
    # Most tools respond to --version; yosys/iverilog use -V
    best = ""
    for flag in ["--version", "-V", "-v"]:
        try:
            r = subprocess.run(
                [binary, flag],
                capture_output=True, text=True, errors="replace", timeout=5,
            )
            output = (r.stdout + r.stderr).strip()
            if r.returncode == 0 and output:
                for line in output.splitlines():
                    line = line.strip()
                    if line:
                        return line
            # Keep first non-empty output as fallback even if rc != 0
            if not best and output:
                for line in output.splitlines():
                    line = line.strip()
                    if line:
                        best = line
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return best or "unknown"
