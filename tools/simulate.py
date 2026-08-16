# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path

from tools.synthesize import _resolve_sources
from tools.workspace import temporary_workspace


def simulate(
    code: str = "",
    testbench: str = "",
    language: str = "verilog",
    timeout: int = 60,
    return_vcd: bool = False,
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
) -> dict:
    """Compile and run an HDL simulation.

    Verilog/SystemVerilog: Icarus Verilog (iverilog + vvp)
    VHDL:                  GHDL (ghdl -a, ghdl -e, ghdl -r)

    Design source input (provide exactly one):
      code:        single HDL source as a string
      files:       dict of filename → source code for multi-file designs
      project_dir: path to a directory containing HDL files on disk

    testbench is always a source string and is required.
    return_vcd: include the raw VCD text in the response (can be large);
                by default only a structured summary is returned.
    """
    if not testbench:
        return {
            "success": False,
            "error": "testbench is required.",
            "error_code": "invalid_input",
        }
    if language == "vhdl":
        return _simulate_vhdl(code, testbench, timeout, return_vcd, files, project_dir)
    return _simulate_verilog(
        code, testbench, language, timeout, return_vcd, files, project_dir
    )


def _simulate_verilog(
    code: str,
    testbench: str,
    language: str,
    timeout: int,
    return_vcd: bool = False,
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
) -> dict:
    """Compile and run a Verilog simulation using Icarus Verilog (iverilog + vvp)."""
    with temporary_workspace("sim_") as tmpdir:
        design_paths, err = _resolve_sources(code, files, project_dir, language, tmpdir)
        if err:
            return {"success": False, "error": err, "error_code": "invalid_input"}

        tb_file = os.path.join(tmpdir, "testbench.v")
        out_file = os.path.join(tmpdir, "sim.vvp")
        with open(tb_file, "w", encoding="utf-8") as f:
            f.write(testbench)

        cmd = ["iverilog", "-g2012", "-o", out_file]
        if project_dir:
            cmd += ["-I", str(Path(project_dir).resolve())]
        cmd += [tb_file, *design_paths]

        try:
            compile_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            if compile_result.returncode != 0:
                return {
                    "success": False,
                    "tool": "iverilog",
                    "stage": "compile",
                    "error": "Compilation failed — see stderr.",
                    "error_code": "syntax_error",
                    "stdout": compile_result.stdout,
                    "stderr": compile_result.stderr,
                }

            run_result = subprocess.run(
                ["vvp", out_file],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                cwd=tmpdir,
            )
            result = {
                "success": run_result.returncode == 0,
                "tool": "iverilog",
                "stage": "run",
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
                "verdict": _parse_verdict(
                    run_result.stdout, run_result.stderr, run_result.returncode
                ),
            }
            result.update(_collect_waveforms(tmpdir, return_vcd))
            return result

        except FileNotFoundError:
            return {
                "success": False,
                "error": "'iverilog'/'vvp' not found. Install OSS CAD Suite.",
                "error_code": "tool_not_found",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Simulation timed out after {timeout} s.",
                "error_code": "timeout",
            }


def _simulate_vhdl(
    code: str,
    testbench: str,
    timeout: int,
    return_vcd: bool = False,
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
) -> dict:
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
            "error_code": "invalid_input",
        }

    with temporary_workspace("sim_vhdl_") as tmpdir:
        design_paths, err = _resolve_sources(code, files, project_dir, "vhdl", tmpdir)
        if err:
            return {"success": False, "error": err, "error_code": "invalid_input"}

        tb_file = os.path.join(tmpdir, "testbench.vhd")
        with open(tb_file, "w", encoding="utf-8") as f:
            f.write(testbench)

        try:
            # Analyze design files (in filelist/glob order for multi-file input)
            for design_file in design_paths:
                analyze_design = subprocess.run(
                    ["ghdl", "-a", "--std=08", "--workdir=" + tmpdir, design_file],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=timeout,
                )
                if analyze_design.returncode != 0:
                    return {
                        "success": False,
                        "tool": "ghdl",
                        "stage": "analyze_design",
                        "file": os.path.basename(design_file),
                        "error": "VHDL analysis of the design failed — see stderr.",
                        "error_code": "syntax_error",
                        "stdout": analyze_design.stdout,
                        "stderr": analyze_design.stderr,
                    }

            # Analyze testbench
            analyze_tb = subprocess.run(
                ["ghdl", "-a", "--std=08", "--workdir=" + tmpdir, tb_file],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            if analyze_tb.returncode != 0:
                return {
                    "success": False,
                    "tool": "ghdl",
                    "stage": "analyze_testbench",
                    "error": "VHDL analysis of the testbench failed — see stderr.",
                    "error_code": "syntax_error",
                    "stdout": analyze_tb.stdout,
                    "stderr": analyze_tb.stderr,
                }

            # Elaborate
            elab = subprocess.run(
                ["ghdl", "-e", "--std=08", "--workdir=" + tmpdir, tb_entity],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                cwd=tmpdir,
            )
            if elab.returncode != 0:
                return {
                    "success": False,
                    "tool": "ghdl",
                    "stage": "elaborate",
                    "error": "VHDL elaboration failed — see stderr.",
                    "error_code": "elaboration_error",
                    "stdout": elab.stdout,
                    "stderr": elab.stderr,
                }

            # Run
            run_result = subprocess.run(
                ["ghdl", "-r", "--std=08", "--workdir=" + tmpdir, tb_entity],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                cwd=tmpdir,
            )
            result = {
                "success": run_result.returncode == 0,
                "tool": "ghdl",
                "stage": "run",
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
                "verdict": _parse_verdict(
                    run_result.stdout, run_result.stderr, run_result.returncode
                ),
            }
            result.update(_collect_waveforms(tmpdir, return_vcd))
            return result

        except FileNotFoundError:
            return {
                "success": False,
                "error": "'ghdl' not found. Install OSS CAD Suite.",
                "error_code": "tool_not_found",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Simulation timed out after {timeout} s.",
                "error_code": "timeout",
            }


def _find_vhdl_entity(code: str) -> str | None:
    """Extract the first entity name from VHDL source."""
    import re

    m = re.search(r"\bentity\s+(\w+)\s+is\b", code, re.IGNORECASE)
    return m.group(1) if m else None


_MAX_VCD_SIZE = 512 * 1024  # 512 KB cap for inline VCD

# Patterns for pass/fail detection in simulation output
_PASS_PATTERNS = re.compile(
    r"\bPASS\b|TEST\s+PASSED|SIMULATION\s+PASSED|All\s+tests\s+passed|UVM_PASS",
    re.IGNORECASE,
)
_FAIL_PATTERNS = re.compile(
    r"\bFAIL\b|TEST\s+FAILED|SIMULATION\s+FAILED|ASSERTION\s+FAILED"
    r"|UVM_ERROR|UVM_FATAL|\$fatal\b|Error:",
    re.IGNORECASE,
)


def _parse_verdict(stdout: str, stderr: str, returncode: int) -> dict:
    """Determine pass/fail from simulation output.

    Returns {"verdict": "pass"|"fail"|"inconclusive", "reason": "..."}.
    """
    combined = stdout + stderr

    fail_match = _FAIL_PATTERNS.search(combined)
    pass_match = _PASS_PATTERNS.search(combined)

    if returncode != 0:
        reason = fail_match.group(0) if fail_match else "non-zero exit code"
        return {"verdict": "fail", "reason": reason}
    if fail_match:
        return {"verdict": "fail", "reason": fail_match.group(0)}
    if pass_match:
        return {"verdict": "pass", "reason": pass_match.group(0)}
    return {
        "verdict": "inconclusive",
        "reason": "no PASS/FAIL pattern detected in output",
    }


def _summarize_vcd(vcd_text: str) -> dict:
    """Extract a lightweight summary from VCD text.

    Returns signal list, total simulation time, and end-of-sim values.
    """
    signals: dict[str, str] = {}  # id -> name
    end_time = ""
    final_values: dict[str, str] = {}  # signal_name -> value

    # Parse signal declarations: $var wire 1 ! clk $end
    var_pat = re.compile(r"\$var\s+\w+\s+\d+\s+(\S+)\s+(\S+)")
    for m in var_pat.finditer(vcd_text):
        sig_id, sig_name = m.groups()
        signals[sig_id] = sig_name

    # Track time and final values
    for line in vcd_text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            end_time = line[1:]
        elif len(line) >= 2 and line[0] in "01xzXZ" and line[1:] in signals:
            final_values[signals[line[1:]]] = line[0]
        elif line.startswith("b") and " " in line:
            val, sid = line.split(" ", 1)
            if sid in signals:
                final_values[signals[sid]] = val

    return {
        "signal_count": len(signals),
        "signals": list(signals.values()),
        "end_time": end_time,
        "final_values": final_values,
    }


def _collect_waveforms(tmpdir: str, include_vcd: bool = False) -> dict:
    """Return a VCD summary (and optionally the raw VCD text) if produced.

    The raw VCD is only included when include_vcd is True — it can be hundreds
    of kilobytes, which floods an AI client's context for no benefit when the
    structured summary suffices.
    """
    extras: dict = {}
    vcd_files = glob.glob(os.path.join(tmpdir, "*.vcd"))
    if vcd_files:
        vcd_path = vcd_files[0]
        size = os.path.getsize(vcd_path)
        if size <= _MAX_VCD_SIZE:
            with open(vcd_path, "r", encoding="utf-8", errors="replace") as f:
                vcd_text = f.read()
            extras["vcd_summary"] = _summarize_vcd(vcd_text)
            if include_vcd:
                extras["vcd"] = vcd_text
        else:
            extras["vcd_truncated"] = True
            extras["vcd_size_kb"] = round(size / 1024, 1)
    return extras
