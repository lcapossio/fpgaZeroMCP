# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import re
import subprocess

from tools.workspace import temporary_workspace

SYNTH_CMDS: dict[str, str] = {
    "generic": "synth",
    "ice40":   "synth_ice40",
    "ecp5":    "synth_ecp5",
    "nexus":   "synth_nexus",
    "gowin":   "synth_gowin",
    "xilinx":  "synth_xilinx",
    "intel":   "synth_intel",
}

_TOP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_top_module(name: str) -> str | None:
    if not _TOP_RE.fullmatch(name):
        return (
            "Invalid top_module. Must be a Verilog identifier "
            "(letters/digits/_/$, not starting with a digit)."
        )
    return None


def synthesize(
    code: str,
    top_module: str,
    target: str = "generic",
    backend: str = "yosys",
    litex_board: str | None = None,
    litex_args: list[str] | None = None,
    timeout: int = 120,
) -> dict:
    """Synthesize Verilog using Yosys or LiteX backend."""
    if backend == "litex":
        if not litex_board:
            return {"success": False, "error": "litex_board is required for LiteX backend."}
        from tools.litex import litex_flow
        result = litex_flow(board=litex_board, args=litex_args or [], timeout=max(timeout, 120))
        result["backend"] = "litex"
        result["note"] = "LiteX backend ignores code/top_module and runs board target."
        return result

    synth_cmd = SYNTH_CMDS.get(target, "synth")
    top_err = validate_top_module(top_module)
    if top_err:
        return {"success": False, "error": top_err}

    with temporary_workspace("synth_") as tmpdir:
        src       = os.path.join(tmpdir, "design.v")
        out_json  = os.path.join(tmpdir, "synth.json")
        ys_script = os.path.join(tmpdir, "synth.ys")

        with open(src, "w", encoding="utf-8") as f:
            f.write(code)

        # Yosys expects forward slashes even on Windows
        src_yosys      = src.replace("\\", "/")
        out_json_yosys = out_json.replace("\\", "/")

        # Generic 'synth' doesn't support -json; use write_json separately
        if target == "generic":
            script = (
                f"read_verilog {src_yosys}\n"
                f"{synth_cmd} -top {top_module}\n"
                f"write_json {out_json_yosys}\n"
                f"stat\n"
            )
        else:
            script = (
                f"read_verilog {src_yosys}\n"
                f"{synth_cmd} -top {top_module} -json {out_json_yosys}\n"
                f"stat\n"
            )
        with open(ys_script, "w", encoding="utf-8") as f:
            f.write(script)

        try:
            result = subprocess.run(
                ["yosys", "-s", ys_script],
                capture_output=True, text=True, errors="replace", timeout=timeout,
            )

            modules: list[str] = []
            if os.path.exists(out_json):
                with open(out_json) as f:
                    netlist = json.load(f)
                modules = list(netlist.get("modules", {}).keys())

            return {
                "success": result.returncode == 0,
                "target": target,
                "top_module": top_module,
                "modules": modules,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        except FileNotFoundError:
            return {"success": False, "error": "'yosys' not found. Install it and ensure it is on PATH."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Synthesis timed out after {timeout} s."}
