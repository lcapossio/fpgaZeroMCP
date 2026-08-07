# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Check which EDA tools are installed and reachable."""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor


_TOOLS = [
    ("iverilog", "Icarus Verilog (lint, simulate)"),
    ("vvp", "Icarus Verilog runtime"),
    ("yosys", "Yosys (synthesis)"),
    ("nextpnr-ice40", "nextpnr for iCE40"),
    ("nextpnr-ecp5", "nextpnr for ECP5"),
    ("nextpnr-nexus", "nextpnr for Nexus"),
    ("nextpnr-gowin", "nextpnr for Gowin"),
    ("ghdl", "GHDL (VHDL analysis/simulation)"),
    ("verilator", "Verilator (lint, diagnostics)"),
    ("verible-verilog-lint", "Verible linter"),
    ("verible-verilog-format", "Verible formatter"),
    ("vsg", "VHDL Style Guide (formatter)"),
    ("sby", "SymbiYosys (formal verification)"),
    ("iceprog", "iCE40 programmer"),
    ("openFPGALoader", "Universal FPGA programmer"),
    ("ecpprog", "ECP5 programmer"),
    ("vivado", "AMD/Xilinx Vivado (vendor flow, via start_build/LiteX)"),
]


def check_tools() -> dict:
    """Return availability and version of each EDA tool.

    Probes run in parallel — each tool needs a `which` plus up to three
    version subprocesses, which serially takes tens of seconds when many
    tools are missing or slow to answer.
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_probe_tool, _TOOLS))

    results.append(_probe_litex())

    installed = sum(1 for r in results if r["installed"])
    return {
        "installed": installed,
        "total": len(results),
        "tools": results,
    }


def _probe_tool(tool: tuple[str, str]) -> dict:
    binary, description = tool
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
    return entry


def _probe_litex() -> dict:
    """LiteX is a Python package, not a binary — probe it via import metadata."""
    entry: dict = {
        "tool": "litex",
        "description": "LiteX SoC builder (Python package)",
        "installed": False,
    }
    try:
        import importlib.metadata
        import importlib.util

        if importlib.util.find_spec("litex") is not None:
            entry["installed"] = True
            try:
                entry["version"] = importlib.metadata.version("litex")
            except importlib.metadata.PackageNotFoundError:
                entry["version"] = "unknown"
    except Exception:
        pass
    return entry


def _get_version(binary: str) -> str:
    """Best-effort version string extraction."""
    # Most tools respond to --version; yosys/iverilog use -V
    best = ""
    for flag in ["--version", "-V", "-v"]:
        try:
            r = subprocess.run(
                [binary, flag],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
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
