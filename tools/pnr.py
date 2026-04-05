# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import glob as _glob
import os
import re
import subprocess
from pathlib import Path

from tools.boards import get_board_preset, BOARD_PRESETS
from tools.synthesize import SYNTH_CMDS, validate_top_module, _resolve_sources, _yosys_read_cmds
from tools.workspace import temporary_workspace

# nextpnr binary per target
NEXTPNR_BIN = {
    "ice40": "nextpnr-ice40",
    "ecp5":  "nextpnr-ecp5",
    "nexus": "nextpnr-nexus",
    "gowin": "nextpnr-gowin",
}

# Constraints file extension per target
CONSTRAINTS_EXT = {
    "ice40": ".pcf",
    "ecp5":  ".lpf",
    "nexus": ".pdc",
    "gowin": ".cst",
}

# nextpnr output file extension per target
OUTPUT_EXT = {
    "ice40": ".asc",
    "ecp5":  ".config",
    "nexus": ".fasm",
    "gowin": "_pnr.json",
}


def _find_constraints(project_dir: str, target: str) -> str | None:
    """Auto-detect a constraint file in project_dir for the given target.

    Returns the file path if found, else None.
    """
    ext = CONSTRAINTS_EXT.get(target)
    if not ext:
        return None
    matches = _glob.glob(os.path.join(project_dir, f"**/*{ext}"), recursive=True)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer files in root over subdirectories, then shortest path
        matches.sort(key=lambda p: (p.count(os.sep), len(p)))
        return matches[0]
    return None


def place_and_route(
    code: str = "",
    top_module: str = "",
    target: str = "",
    device: str = "",
    package: str = "",
    constraints: str = "",
    language: str = "verilog",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    board: str | None = None,
    nextpnr_args: list[str] | None = None,
    work_dir: str | None = None,
    timeout: int = 300,
    backend: str = "yosys",
    litex_board: str | None = None,
    litex_args: list[str] | None = None,
) -> dict:
    """Synthesize HDL with Yosys then place-and-route with nextpnr.

    Source input (provide exactly one):
      code:        single HDL source as a string
      files:       dict of filename → source code for multi-file designs
      project_dir: path to a directory containing HDL files on disk

    language: "verilog" (default), "systemverilog", or "vhdl".
    board: optional board preset (e.g. "icebreaker") — sets target/device/package/clock.
    nextpnr_args: extra arguments passed to nextpnr (e.g. ["--seed", "42"]).
    work_dir: path to a persistent working directory. If provided, files and
              synthesis artifacts are kept across runs for incremental workflows.
              The path is returned in the response for reuse.

    constraints: optional PCF/LPF/PDC/CST text. If omitted and project_dir is
                 used, auto-detects constraint files from the project directory.
    """
    if backend == "litex":
        if not litex_board:
            return {"success": False, "error": "litex_board is required for LiteX backend."}
        from tools.litex import litex_build
        result = litex_build(board=litex_board, args=litex_args or [], timeout=max(timeout, 300))
        result["backend"] = "litex"
        result["note"] = "LiteX backend ignores code/top_module/target/device and runs board build."
        return result

    # Apply board preset (explicit params override preset values)
    preset = get_board_preset(board) if board else None
    if preset:
        target = target or preset["target"]
        device = device or preset["device"]
        package = package or preset.get("package", "")

    if not target or target not in NEXTPNR_BIN:
        supported = list(NEXTPNR_BIN.keys())
        return {"success": False, "error": f"Unsupported PnR target '{target}'. Supported: {supported}"}

    synth_cmd = SYNTH_CMDS.get(target)
    if not synth_cmd:
        return {"success": False, "error": f"No Yosys synth command for target '{target}'"}
    if not top_module:
        return {"success": False, "error": "top_module is required."}
    top_err = validate_top_module(top_module)
    if top_err:
        return {"success": False, "error": top_err}

    # Determine whether to use a persistent work_dir or a temp workspace
    use_persistent = bool(work_dir)
    if use_persistent:
        os.makedirs(work_dir, exist_ok=True)  # type: ignore[arg-type]
        tmpdir = work_dir  # type: ignore[assignment]

    def _run_in(tmpdir: str) -> dict:
        src_paths, err = _resolve_sources(code, files, project_dir, language, tmpdir)
        if err:
            return {"success": False, "error": err}

        netlist_json = os.path.join(tmpdir, "netlist.json")
        ys_script    = os.path.join(tmpdir, "synth.ys")
        out_file     = os.path.join(tmpdir, f"out{OUTPUT_EXT[target]}")
        cst_file     = os.path.join(tmpdir, f"constraints{CONSTRAINTS_EXT[target]}")

        # Auto-detect include directories from project_dir
        include_dirs: list[str] = []
        if project_dir:
            resolved_proj = str(Path(project_dir).resolve())
            include_dirs.append(resolved_proj)
            for root, _dirs, fnames in os.walk(resolved_proj):
                if any(f.endswith((".vh", ".svh")) for f in fnames):
                    if root != resolved_proj:
                        include_dirs.append(root)

        # _yosys_read_cmds handles VHDL elaborate in a single ghdl invocation
        read_cmds = _yosys_read_cmds(src_paths, language, top_module, include_dirs)
        netlist_yosys = netlist_json.replace("\\", "/")

        # ghdl-yosys-plugin lowercases VHDL entity names during import
        yosys_top = top_module.lower() if language == "vhdl" else top_module

        script = (
            f"{read_cmds}"
            f"{synth_cmd} -top {yosys_top} -json {netlist_yosys}\n"
        )
        with open(ys_script, "w", encoding="utf-8") as f:
            f.write(script)

        # ------------------------------------------------------------------
        # Stage 1: Synthesis
        # ------------------------------------------------------------------
        import time as _time
        deadline = _time.monotonic() + timeout
        synth_timeout = min(max(timeout // 3, 30), timeout)
        try:
            synth = subprocess.run(
                ["yosys", "-s", ys_script],
                capture_output=True, text=True, errors="replace", timeout=synth_timeout,
            )
        except FileNotFoundError:
            return {"success": False, "error": "'yosys' not found. Install OSS CAD Suite."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Synthesis timed out after {synth_timeout} s."}

        if synth.returncode != 0:
            return {
                "success": False,
                "stage": "synthesis",
                "stdout": synth.stdout,
                "stderr": synth.stderr,
            }

        if not os.path.exists(netlist_json):
            return {"success": False, "stage": "synthesis",
                    "error": "Yosys did not produce a netlist JSON.",
                    "stdout": synth.stdout, "stderr": synth.stderr}

        # Resolve constraints: explicit string > auto-detect from project_dir
        effective_cst: str | None = None
        cst_source = "none"
        if constraints:
            with open(cst_file, "w", encoding="utf-8") as f:
                f.write(constraints)
            effective_cst = cst_file
            cst_source = "provided"
        elif project_dir:
            auto_cst = _find_constraints(project_dir, target)
            if auto_cst:
                effective_cst = auto_cst
                cst_source = f"auto-detected: {os.path.basename(auto_cst)}"

        # ------------------------------------------------------------------
        # Stage 2: Place and route
        # ------------------------------------------------------------------
        cmd = _build_cmd(
            NEXTPNR_BIN[target], target, device, package,
            netlist_json, out_file, effective_cst,
        )
        if nextpnr_args:
            cmd.extend(nextpnr_args)

        pnr_timeout = int(deadline - _time.monotonic())
        if pnr_timeout < 10:
            return {
                "success": False,
                "stage": "place_and_route",
                "error": (
                    f"Synthesis used most of the {timeout} s budget; "
                    f"only {max(pnr_timeout, 0)} s remain for PnR. "
                    "Increase timeout or simplify the design."
                ),
                "synth_log": synth.stdout,
            }
        try:
            pnr = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=pnr_timeout,
            )
        except FileNotFoundError:
            return {"success": False, "error": f"'{NEXTPNR_BIN[target]}' not found. Install OSS CAD Suite."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Place and route timed out after {pnr_timeout} s."}

        combined_output = pnr.stdout + pnr.stderr

        result: dict = {
            "success":     pnr.returncode == 0,
            "stage":       "place_and_route",
            "target":      target,
            "device":      device,
            "package":     package,
            "language":    language,
            "top_module":  top_module,
            "constraints": cst_source,
            "timing":      _parse_timing(combined_output),
            "utilization": _parse_utilization(combined_output, target),
            "synth_log":   synth.stdout,
            "pnr_stdout":  pnr.stdout,
            "pnr_stderr":  pnr.stderr,
        }

        # Evaluate timing against board clock target
        if preset and preset.get("clock_mhz"):
            target_mhz = preset["clock_mhz"]
            fmax = result["timing"].get("max_freq_mhz")
            result["timing"]["target_mhz"] = target_mhz
            if fmax is not None:
                result["timing"]["meets_target"] = fmax >= target_mhz

        if board:
            result["board"] = board
        if use_persistent:
            result["work_dir"] = tmpdir
        if nextpnr_args:
            result["nextpnr_args"] = nextpnr_args

        # Include bitstream/config output if PnR succeeded
        if pnr.returncode == 0 and os.path.exists(out_file):
            with open(out_file, "rb") as f:
                result["bitstream_b64"] = base64.b64encode(f.read()).decode("ascii")
            result["bitstream_ext"] = OUTPUT_EXT[target]

        return result

    if use_persistent:
        return _run_in(tmpdir)
    else:
        with temporary_workspace("pnr_") as tmpdir:
            return _run_in(tmpdir)


def _build_cmd(
    binary: str, target: str, device: str, package: str,
    netlist: str, out_file: str, constraints: str | None,
) -> list[str]:
    cmd = [binary]

    if target == "ice40":
        cmd += [f"--{device}"]
        if package:
            cmd += ["--package", package]
        cmd += ["--json", netlist, "--asc", out_file]
        if constraints:
            cmd += ["--pcf", constraints]

    elif target == "ecp5":
        cmd += [f"--{device}"]
        if package:
            cmd += ["--package", package]
        cmd += ["--json", netlist, "--textcfg", out_file]
        if constraints:
            cmd += ["--lpf", constraints]

    elif target == "nexus":
        cmd += ["--device", device, "--json", netlist, "--fasm", out_file]
        if constraints:
            cmd += ["--pdc", constraints]

    elif target == "gowin":
        cmd += ["--device", device, "--json", netlist, "--write", out_file]
        if constraints:
            cmd += ["--cst", constraints]

    return cmd


def _parse_timing(output: str) -> dict:
    timing: dict = {}

    # "Max frequency for clock 'clk': 142.34 MHz (PASS at 12.00 MHz)"
    m = re.search(r"Max frequency for clock[^:]*:\s*([\d.]+)\s*MHz", output)
    if m:
        timing["max_freq_mhz"] = float(m.group(1))

    # "Critical path: 7.03 ns"
    m = re.search(r"[Cc]ritical path[^:]*:\s*([\d.]+)\s*ns", output)
    if m:
        timing["critical_path_ns"] = float(m.group(1))

    return timing


def _parse_utilization(output: str, target: str) -> dict:
    util: dict = {}

    patterns = {
        "ice40": [
            (r"ICESTORM_LC[:\s]+([\d]+)/\s*([\d]+)",   "luts"),
            (r"SB_IO[:\s]+([\d]+)/\s*([\d]+)",          "ios"),
            (r"SB_RAM40_4K[:\s]+([\d]+)/\s*([\d]+)",    "brams"),
        ],
        "ecp5": [
            (r"LUT4[:\s]+([\d]+)/\s*([\d]+)",           "luts"),
            (r"TRELLIS_IO[:\s]+([\d]+)/\s*([\d]+)",     "ios"),
            (r"TRELLIS_RAMW[:\s]+([\d]+)/\s*([\d]+)",   "brams"),
        ],
        "nexus": [
            (r"OXIDE_COMB[:\s]+([\d]+)/\s*([\d]+)",     "luts"),
            (r"OXIDE_FF[:\s]+([\d]+)/\s*([\d]+)",       "ffs"),
        ],
        "gowin": [
            (r"LUT[:\s]+([\d]+)/\s*([\d]+)",            "luts"),
            (r"FF[:\s]+([\d]+)/\s*([\d]+)",             "ffs"),
        ],
    }

    for pattern, label in patterns.get(target, []):
        m = re.search(pattern, output)
        if m:
            util[f"{label}_used"]  = int(m.group(1))
            util[f"{label}_total"] = int(m.group(2))

    return util
