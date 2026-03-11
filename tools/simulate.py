# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import subprocess
import tempfile


def simulate(code: str, testbench: str, timeout: int = 60) -> dict:
    """Compile and run a Verilog simulation using Icarus Verilog (iverilog + vvp)."""
    with tempfile.TemporaryDirectory() as tmpdir:
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
                capture_output=True, text=True, timeout=30,
            )
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "stage": "compile",
                    "stdout": compile_result.stdout,
                    "stderr": compile_result.stderr,
                }

            run_result = subprocess.run(
                ["vvp", out_file],
                capture_output=True, text=True, timeout=timeout,
            )
            return {
                "success": run_result.returncode == 0,
                "stage": "run",
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
            }

        except FileNotFoundError:
            return {"success": False, "error": "'iverilog'/'vvp' not found. Install iverilog and ensure it is on PATH."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Simulation timed out after {timeout} s."}
