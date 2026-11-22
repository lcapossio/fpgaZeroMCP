# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from fpgazero_mcp.tools.progress import make_reporter
from fpgazero_mcp.tools.sources import (
    parse_filelist,
    resolve_sources,
    validate_top_module,
    yosys_read_cmds,
)
from fpgazero_mcp.tools.textutil import truncate_log
from fpgazero_mcp.tools.workspace import temporary_workspace

SYNTH_CMDS: dict[str, str] = {
    "generic": "synth",
    "ice40": "synth_ice40",
    "ecp5": "synth_ecp5",
    "nexus": "synth_nexus",
    "gowin": "synth_gowin",
    "xilinx": "synth_xilinx",
    "intel": "synth_intel",
}

# "   Number of cells:                 12" — inside a yosys `stat` block
_STAT_NUM_RE = re.compile(r"^\s+Number of ([\w ]+?):\s+(\d+)\s*$", re.MULTILINE)
# "     SB_LUT4                         4" — per-cell-type count line
_STAT_CELL_RE = re.compile(r"^\s{4,}(\$?[A-Za-z_$][\w$.:]*)\s+(\d+)\s*$", re.MULTILINE)
_STAT_BLOCK_RE = re.compile(r"^===\s+(.+?)\s+===$", re.MULTILINE)


def parse_yosys_stats(stdout: str) -> dict:
    """Extract structured resource counts from yosys `stat` output.

    Uses the last `=== <module> ===` block of the last statistics section —
    for hierarchical designs yosys prints the design totals last.
    """
    idx = stdout.rfind("Printing statistics")
    section = stdout[idx:] if idx != -1 else stdout

    blocks = list(_STAT_BLOCK_RE.finditer(section))
    if blocks:
        section = section[blocks[-1].end() :]

    stats: dict = {}
    for m in _STAT_NUM_RE.finditer(section):
        key = m.group(1).strip().lower().replace(" ", "_")
        stats[key] = int(m.group(2))

    cells: dict[str, int] = {}
    for m in _STAT_CELL_RE.finditer(section):
        name = m.group(1)
        if name.startswith("Number"):
            continue
        cells[name] = int(m.group(2))
    if cells:
        stats["cells_by_type"] = cells
    return stats


def synthesize(
    code: str = "",
    top_module: str = "",
    target: str = "generic",
    language: str = "verilog",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    backend: str = "yosys",
    litex_board: str | None = None,
    litex_args: list[str] | None = None,
    timeout: int = 120,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Synthesize HDL using Yosys or LiteX backend.

    Source input (provide exactly one):
      code:        single HDL source as a string
      files:       dict of filename → source code for multi-file designs
      project_dir: path to a directory containing HDL files on disk

    language: "verilog" (default), "systemverilog", or "vhdl".
    progress: optional callback (fraction 0..1, message) invoked at phase
              boundaries; exceptions from it are swallowed.
    """
    report = make_reporter(progress)
    if backend == "litex":
        if not litex_board:
            return {
                "success": False,
                "error": "litex_board is required for LiteX backend.",
                "error_code": "invalid_input",
            }
        from fpgazero_mcp.tools.litex import litex_flow

        litex_result = litex_flow(
            board=litex_board,
            args=litex_args or [],
            timeout=max(timeout, 120),
            progress=progress,
        )
        litex_result["backend"] = "litex"
        litex_result["note"] = (
            "LiteX backend ignores code/top_module and runs board target."
        )
        return litex_result

    synth_cmd = SYNTH_CMDS.get(target, "synth")
    if top_module:
        top_err = validate_top_module(top_module)
        if top_err:
            return {"success": False, "error": top_err, "error_code": "invalid_input"}
    else:
        return {
            "success": False,
            "error": "top_module is required.",
            "error_code": "invalid_input",
        }

    with temporary_workspace("synth_") as tmpdir:
        src_paths, err = resolve_sources(code, files, project_dir, language, tmpdir)
        if err:
            return {"success": False, "error": err, "error_code": "invalid_input"}
        report(0.1, "sources resolved")

        out_json = os.path.join(tmpdir, "synth.json")
        ys_script = os.path.join(tmpdir, "synth.ys")
        out_json_yosys = out_json.replace("\\", "/")

        # Auto-detect include directories and defines from project_dir
        include_dirs: list[str] = []
        defines: list[str] = []
        if project_dir:
            resolved_proj = str(Path(project_dir).resolve())
            include_dirs.append(resolved_proj)
            # Check filelist for +incdir+ and +define+
            for filelist_name in ("files.f", "sources.f"):
                filelist_path = os.path.join(project_dir, filelist_name)
                if os.path.isfile(filelist_path):
                    _, fl_incdirs, fl_defines = parse_filelist(
                        filelist_path, project_dir
                    )
                    include_dirs.extend(fl_incdirs)
                    defines.extend(fl_defines)
                    break
            # Also add subdirectories containing headers
            for root, _dirs, fnames in os.walk(resolved_proj):
                if any(f.endswith((".vh", ".svh")) for f in fnames):
                    if root not in include_dirs:
                        include_dirs.append(root)

        # yosys_read_cmds handles VHDL elaborate in a single ghdl invocation
        read_cmds = yosys_read_cmds(
            src_paths, language, top_module, include_dirs, defines
        )

        # ghdl-yosys-plugin lowercases VHDL entity names during import
        yosys_top = top_module.lower() if language == "vhdl" else top_module

        # Generic 'synth' doesn't support -json; use write_json separately
        if target == "generic":
            script = (
                f"{read_cmds}"
                f"{synth_cmd} -top {yosys_top}\n"
                f"write_json {out_json_yosys}\n"
                f"stat\n"
            )
        else:
            script = (
                f"{read_cmds}"
                f"{synth_cmd} -top {yosys_top} -json {out_json_yosys}\n"
                f"stat\n"
            )
        with open(ys_script, "w", encoding="utf-8") as f:
            f.write(script)

        try:
            report(0.2, "running yosys synthesis")
            result = subprocess.run(
                ["yosys", "-s", ys_script],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            report(0.9, "yosys finished, parsing results")

            modules: list[str] = []
            if os.path.exists(out_json):
                with open(out_json) as f:
                    netlist = json.load(f)
                modules = list(netlist.get("modules", {}).keys())

            success = result.returncode == 0
            # Keep the full log on failure (errors matter); truncate on success
            # so a routine synthesis doesn't flood the AI client's context.
            stdout_text, stdout_truncated = (
                truncate_log(result.stdout) if success else (result.stdout, False)
            )

            output: dict = {
                "success": success,
                "target": target,
                "language": language,
                "top_module": top_module,
                "modules": modules,
                "stats": parse_yosys_stats(result.stdout) if success else {},
                "stdout": stdout_text,
                "stderr": result.stderr,
            }
            if stdout_truncated:
                output["stdout_truncated"] = True
            if not success:
                output["error"] = "Synthesis failed — see stdout/stderr."
                output["error_code"] = "synthesis_failed"
            if files:
                output["files"] = list(files.keys())
            if project_dir:
                resolved_dir = str(Path(project_dir).resolve())
                output["project_dir"] = project_dir
                output["source_files"] = [
                    os.path.relpath(p, resolved_dir) for p in src_paths
                ]
            report(1.0, "synthesis complete")
            return output

        except FileNotFoundError:
            return {
                "success": False,
                "error": "'yosys' not found. Install it and ensure it is on PATH.",
                "error_code": "tool_not_found",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Synthesis timed out after {timeout} s.",
                "error_code": "timeout",
            }
