# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import re
import subprocess
import tempfile

from tools.synthesize import SYNTH_CMDS, validate_top_module

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


def place_and_route(
    code: str,
    top_module: str,
    target: str,
    device: str,
    package: str = "",
    constraints: str = "",
    timeout: int = 300,
    backend: str = "yosys",
    litex_board: str | None = None,
    litex_args: list[str] | None = None,
) -> dict:
    """Synthesize Verilog with Yosys then place-and-route with nextpnr.

    Common device/package values:
      ice40:  device=hx1k|hx8k|up5k|lp1k  package=tq144|qn84|sg48|cm81
      ecp5:   device=25k|45k|85k           package=CABGA256|CABGA381|CABGA381
      nexus:  device=LIFCL-40-9BG400C      (package embedded in device string)
      gowin:  device=GW1N-UV4LQ144C6/I5   (package embedded in device string)

    constraints: optional PCF (ice40), LPF (ecp5), PDC (nexus), or CST (gowin) text.
    """
    if backend == "litex":
        if not litex_board:
            return {"success": False, "error": "litex_board is required for LiteX backend."}
        from tools.litex import litex_build
        result = litex_build(board=litex_board, args=litex_args or [], timeout=max(timeout, 300))
        result["backend"] = "litex"
        result["note"] = "LiteX backend ignores code/top_module/target/device and runs board build."
        return result

    if target not in NEXTPNR_BIN:
        supported = list(NEXTPNR_BIN.keys())
        return {"error": f"Unsupported PnR target '{target}'. Supported: {supported}"}

    synth_cmd = SYNTH_CMDS.get(target)
    if not synth_cmd:
        return {"error": f"No Yosys synth command for target '{target}'"}
    top_err = validate_top_module(top_module)
    if top_err:
        return {"success": False, "error": top_err}

    with tempfile.TemporaryDirectory() as tmpdir:
        src_file     = os.path.join(tmpdir, "design.v")
        ys_script    = os.path.join(tmpdir, "synth.ys")
        netlist_json = os.path.join(tmpdir, "netlist.json")
        out_file     = os.path.join(tmpdir, f"out{OUTPUT_EXT[target]}")
        cst_file     = os.path.join(tmpdir, f"constraints{CONSTRAINTS_EXT[target]}")

        with open(src_file, "w", encoding="utf-8") as f:
            f.write(code)

        # Forward slashes for Yosys on Windows
        src_yosys     = src_file.replace("\\", "/")
        netlist_yosys = netlist_json.replace("\\", "/")

        script = (
            f"read_verilog {src_yosys}\n"
            f"{synth_cmd} -top {top_module} -json {netlist_yosys}\n"
        )
        with open(ys_script, "w") as f:
            f.write(script)

        # ------------------------------------------------------------------
        # Stage 1: Synthesis
        # ------------------------------------------------------------------
        try:
            synth = subprocess.run(
                ["yosys", "-s", ys_script],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            return {"error": "'yosys' not found. Install OSS CAD Suite."}
        except subprocess.TimeoutExpired:
            return {"error": "Synthesis timed out after 120 s."}

        if synth.returncode != 0:
            return {
                "success": False,
                "stage": "synthesis",
                "stdout": synth.stdout,
                "stderr": synth.stderr,
            }

        if not os.path.exists(netlist_json):
            return {"success": False, "stage": "synthesis",
                    "error": "Yosys did not produce a netlist JSON."}

        if constraints:
            with open(cst_file, "w", encoding="utf-8") as f:
                f.write(constraints)

        # ------------------------------------------------------------------
        # Stage 2: Place and route
        # ------------------------------------------------------------------
        cmd = _build_cmd(
            NEXTPNR_BIN[target], target, device, package,
            netlist_json, out_file, cst_file if constraints else None,
        )

        try:
            pnr = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            return {"error": f"'{NEXTPNR_BIN[target]}' not found. Install OSS CAD Suite."}
        except subprocess.TimeoutExpired:
            return {"error": f"Place and route timed out after {timeout} s."}

        combined_output = pnr.stdout + pnr.stderr

        return {
            "success":     pnr.returncode == 0,
            "stage":       "place_and_route",
            "target":      target,
            "device":      device,
            "package":     package,
            "top_module":  top_module,
            "timing":      _parse_timing(combined_output),
            "utilization": _parse_utilization(combined_output, target),
            "synth_log":   synth.stdout,
            "pnr_stdout":  pnr.stdout,
            "pnr_stderr":  pnr.stderr,
        }


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
