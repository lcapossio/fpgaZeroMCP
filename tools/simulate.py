# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import glob
import os
import subprocess

from tools.workspace import temporary_workspace


def simulate(
    code: str,
    testbench: str,
    language: str = "verilog",
    timeout: int = 60,
) -> dict:
    """Compile and run an HDL simulation.

    Verilog/SystemVerilog: Icarus Verilog (iverilog + vvp)
    VHDL:                  GHDL (ghdl -a, ghdl -e, ghdl -r)
    """
    if language == "vhdl":
        return _simulate_vhdl(code, testbench, timeout)
    return _simulate_verilog(code, testbench, timeout)


def _simulate_verilog(code: str, testbench: str, timeout: int) -> dict:
    """Compile and run a Verilog simulation using Icarus Verilog (iverilog + vvp)."""
    with temporary_workspace("sim_") as tmpdir:
        design_file = os.path.join(tmpdir, "design.v")
        tb_file     = os.path.join(tmpdir, "testbench.v")
        out_file    = os.path.join(tmpdir, "sim.vvp")

        with open(design_file, "w", encoding="utf-8") as f:
            f.write(code)
        with open(tb_file, "w", encoding="utf-8") as f:
            f.write(testbench)

        try:
            compile_result = subprocess.run(
                ["iverilog", "-g2012", "-o", out_file, tb_file, design_file],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "tool": "iverilog",
                    "stage": "compile",
                    "stdout": compile_result.stdout,
                    "stderr": compile_result.stderr,
                }

            run_result = subprocess.run(
                ["vvp", out_file],
                capture_output=True, text=True, errors="replace", timeout=timeout,
                cwd=tmpdir,
            )
            result = {
                "success": run_result.returncode == 0,
                "tool": "iverilog",
                "stage": "run",
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
            }
            result.update(_collect_waveforms(tmpdir))
            return result

        except FileNotFoundError:
            return {"success": False, "error": "'iverilog'/'vvp' not found. Install OSS CAD Suite."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Simulation timed out after {timeout} s."}


def _simulate_vhdl(code: str, testbench: str, timeout: int) -> dict:
    """Compile and run a VHDL simulation using GHDL."""
    # Check for testbench entity before invoking GHDL
    tb_entity = _find_vhdl_entity(testbench)
    if not tb_entity:
        return {
            "success": False,
            "tool": "ghdl",
            "stage": "elaborate",
            "error": "Could not find an entity in the testbench. "
                     "Ensure the testbench contains an 'entity <name> is' declaration.",
        }

    with temporary_workspace("sim_vhdl_") as tmpdir:
        design_file = os.path.join(tmpdir, "design.vhd")
        tb_file     = os.path.join(tmpdir, "testbench.vhd")

        with open(design_file, "w", encoding="utf-8") as f:
            f.write(code)
        with open(tb_file, "w", encoding="utf-8") as f:
            f.write(testbench)

        try:
            # Analyze design
            analyze_design = subprocess.run(
                ["ghdl", "-a", "--std=08", "--workdir=" + tmpdir, design_file],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if analyze_design.returncode != 0:
                return {
                    "success": False,
                    "tool": "ghdl",
                    "stage": "analyze_design",
                    "stdout": analyze_design.stdout,
                    "stderr": analyze_design.stderr,
                }

            # Analyze testbench
            analyze_tb = subprocess.run(
                ["ghdl", "-a", "--std=08", "--workdir=" + tmpdir, tb_file],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if analyze_tb.returncode != 0:
                return {
                    "success": False,
                    "tool": "ghdl",
                    "stage": "analyze_testbench",
                    "stdout": analyze_tb.stdout,
                    "stderr": analyze_tb.stderr,
                }

            # Elaborate
            elab = subprocess.run(
                ["ghdl", "-e", "--std=08", "--workdir=" + tmpdir, tb_entity],
                capture_output=True, text=True, errors="replace", timeout=30,
                cwd=tmpdir,
            )
            if elab.returncode != 0:
                return {
                    "success": False,
                    "tool": "ghdl",
                    "stage": "elaborate",
                    "stdout": elab.stdout,
                    "stderr": elab.stderr,
                }

            # Run
            run_result = subprocess.run(
                ["ghdl", "-r", "--std=08", "--workdir=" + tmpdir, tb_entity],
                capture_output=True, text=True, errors="replace", timeout=timeout,
                cwd=tmpdir,
            )
            result = {
                "success": run_result.returncode == 0,
                "tool": "ghdl",
                "stage": "run",
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
            }
            result.update(_collect_waveforms(tmpdir))
            return result

        except FileNotFoundError:
            return {"success": False, "error": "'ghdl' not found. Install OSS CAD Suite."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Simulation timed out after {timeout} s."}


def _find_vhdl_entity(code: str) -> str | None:
    """Extract the first entity name from VHDL source."""
    import re
    m = re.search(r"\bentity\s+(\w+)\s+is\b", code, re.IGNORECASE)
    return m.group(1) if m else None


_MAX_VCD_SIZE = 512 * 1024  # 512 KB cap for inline VCD


def _collect_waveforms(tmpdir: str) -> dict:
    """Return VCD/FST waveform data if produced by the simulation."""
    extras: dict = {}
    vcd_files = glob.glob(os.path.join(tmpdir, "*.vcd"))
    if vcd_files:
        vcd_path = vcd_files[0]
        size = os.path.getsize(vcd_path)
        if size <= _MAX_VCD_SIZE:
            with open(vcd_path, "r", encoding="utf-8", errors="replace") as f:
                extras["vcd"] = f.read()
        else:
            extras["vcd_truncated"] = True
            extras["vcd_size_kb"] = round(size / 1024, 1)
    return extras
