# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""First-class Vivado place-and-route backend.

Generates a non-project batch TCL script (read sources -> synth_design ->
opt/place/route -> reports -> write_bitstream), runs ``vivado -mode batch``,
and parses the log into the same structured shape as the nextpnr backend.
No hand-written TCL required.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from typing import Callable
from uuid import uuid4

from tools.build_parser import parse_build_log
from tools.progress import make_reporter
from tools.synthesize import (
    _resolve_sources,
    validate_path_in_roots,
    validate_top_module,
)
from tools.textutil import truncate_log
from tools.workspace import data_root, temporary_workspace

# Xilinx part numbers: letters, digits, dash (e.g. xc7a35ticsg324-1L)
_PART_RE = re.compile(r"^[A-Za-z0-9\-]+$")

# create_clock -period <ns> in XDC text
_RE_XDC_PERIOD = re.compile(r"create_clock\s+[^\n]*-period\s+([\d.]+)")

# route_design inline summary: "Estimated Timing Summary | WNS=0.087 | TNS=0.000"
_RE_WNS_INLINE = re.compile(r"\bWNS\s*=\s*(-?[\d.]+)")
_RE_TNS_INLINE = re.compile(r"\bTNS\s*=\s*(-?[\d.]+)")

# report_timing_summary table:
#     WNS(ns)      TNS(ns)  TNS Failing Endpoints ... WHS(ns)      THS(ns) ...
#     -------      -------  --------------------- ... -------      ------- ...
#       0.123        0.000                      0 ...   0.045        0.000 ...
_RE_TIMING_TABLE = re.compile(
    r"WNS\(ns\)\s+TNS\(ns\).*?\n[\s-]+\n\s*"
    r"(-?[\d.]+)\s+(-?[\d.]+)\s+\d+\s+\d+\s+(-?[\d.]+)\s+(-?[\d.]+)",
    re.S,
)


def _tcl_path(path: str) -> str:
    """Format a filesystem path for TCL: forward slashes, brace-quoted."""
    return "{" + path.replace("\\", "/") + "}"


def _generate_tcl(
    src_paths: list[str],
    language: str,
    top_module: str,
    part: str,
    xdc_file: str | None,
    bit_file: str,
) -> str:
    """Build the non-project batch flow script."""
    lines: list[str] = []
    if language == "vhdl":
        for p in src_paths:
            lines.append(f"read_vhdl -vhdl2008 {_tcl_path(p)}")
    else:
        flag = "-sv " if language == "systemverilog" else ""
        for p in src_paths:
            lines.append(f"read_verilog {flag}{_tcl_path(p)}")
    if xdc_file:
        lines.append(f"read_xdc {_tcl_path(xdc_file)}")
    lines += [
        f"synth_design -top {top_module} -part {part}",
        "opt_design",
        "place_design",
        "route_design",
        "report_utilization",
        "report_timing_summary -no_detailed_paths",
        f"write_bitstream -force {_tcl_path(bit_file)}",
    ]
    return "\n".join(lines) + "\n"


def _parse_timing(log: str, clock_period_ns: float | None) -> dict:
    """Extract WNS/TNS/WHS/THS; derive fmax when the clock period is known."""
    timing: dict = {}
    m = _RE_TIMING_TABLE.search(log)
    if m:
        timing["wns_ns"] = float(m.group(1))
        timing["tns_ns"] = float(m.group(2))
        timing["whs_ns"] = float(m.group(3))
        timing["ths_ns"] = float(m.group(4))
    else:
        m = _RE_WNS_INLINE.search(log)
        if m:
            timing["wns_ns"] = float(m.group(1))
        m = _RE_TNS_INLINE.search(log)
        if m:
            timing["tns_ns"] = float(m.group(1))

    wns = timing.get("wns_ns")
    if clock_period_ns and wns is not None and clock_period_ns > wns:
        timing["period_ns"] = clock_period_ns
        timing["max_freq_mhz"] = round(1000.0 / (clock_period_ns - wns), 2)
        timing["meets_timing"] = wns >= 0
    return timing


def _flatten_utilization(nested: dict) -> dict:
    """Convert parse_build_log's {res: {used, total, pct}} to the flat
    {res_used, res_total} shape the nextpnr backend returns."""
    flat: dict = {}
    for res, data in nested.items():
        flat[f"{res}_used"] = data.get("used")
        flat[f"{res}_total"] = data.get("total")
    return flat


def _find_xdc(project_dir: str) -> str | None:
    """Auto-detect a single .xdc constraint file in project_dir."""
    import glob as _glob

    matches = _glob.glob(os.path.join(project_dir, "**/*.xdc"), recursive=True)
    if not matches:
        return None
    # Prefer files in root over subdirectories, then shortest path
    matches.sort(key=lambda p: (p.count(os.sep), len(p)))
    return matches[0]


def vivado_place_and_route(
    code: str = "",
    top_module: str = "",
    part: str = "",
    constraints: str = "",
    language: str = "verilog",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    work_dir: str | None = None,
    timeout: int = 600,
    return_bitstream_b64: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Run the full Vivado RTL-to-bitstream flow on the given sources.

    part: full Xilinx part number, e.g. "xc7a35tcpg236-1".
    constraints: XDC text; if omitted and project_dir is used, auto-detects
                 a .xdc file from the project directory.
    """
    report = make_reporter(progress)

    if not top_module:
        return {
            "success": False,
            "error": "top_module is required.",
            "error_code": "invalid_input",
        }
    top_err = validate_top_module(top_module)
    if top_err:
        return {"success": False, "error": top_err, "error_code": "invalid_input"}
    if not part:
        return {
            "success": False,
            "error": (
                "device (Xilinx part number, e.g. 'xc7a35tcpg236-1') is "
                "required for the Vivado backend. Use a board preset "
                "(e.g. board='basys3') to set it automatically."
            ),
            "error_code": "invalid_input",
        }
    if not _PART_RE.fullmatch(part):
        return {
            "success": False,
            "error": f"Invalid Xilinx part number: '{part}'",
            "error_code": "invalid_input",
        }

    vivado_bin = shutil.which("vivado")
    if not vivado_bin:
        return {
            "success": False,
            "error": (
                "'vivado' not found on PATH. Install Vivado and source its "
                "settings script (settings64.sh / settings64.bat)."
            ),
            "error_code": "tool_not_found",
        }

    use_persistent = bool(work_dir)
    if use_persistent:
        assert work_dir is not None
        path_err = validate_path_in_roots(work_dir, "work_dir")
        if path_err:
            return {"success": False, "error": path_err, "error_code": "path_security"}
        os.makedirs(work_dir, exist_ok=True)

    def _run_in(tmpdir: str) -> dict:
        src_paths, err = _resolve_sources(code, files, project_dir, language, tmpdir)
        if err:
            return {"success": False, "error": err, "error_code": "invalid_input"}
        report(0.05, "sources resolved")

        bit_file = os.path.join(tmpdir, "out.bit")
        tcl_file = os.path.join(tmpdir, "build.tcl")

        # Resolve constraints: explicit XDC text > auto-detect from project_dir
        xdc_file: str | None = None
        cst_source = "none"
        if constraints:
            xdc_file = os.path.join(tmpdir, "constraints.xdc")
            with open(xdc_file, "w", encoding="utf-8") as f:
                f.write(constraints)
            cst_source = "provided"
        elif project_dir:
            auto_xdc = _find_xdc(project_dir)
            if auto_xdc:
                xdc_file = auto_xdc
                cst_source = f"auto-detected: {os.path.basename(auto_xdc)}"

        clock_period_ns: float | None = None
        if xdc_file and os.path.isfile(xdc_file):
            with open(xdc_file, "r", encoding="utf-8", errors="replace") as f:
                m = _RE_XDC_PERIOD.search(f.read())
            if m:
                clock_period_ns = float(m.group(1))

        with open(tcl_file, "w", encoding="utf-8") as f:
            f.write(
                _generate_tcl(src_paths, language, top_module, part, xdc_file, bit_file)
            )

        report(0.1, "running vivado batch flow (synth, place, route, bitstream)")
        try:
            proc = subprocess.run(
                [
                    vivado_bin,
                    "-mode",
                    "batch",
                    "-nolog",
                    "-nojournal",
                    "-source",
                    tcl_file,
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Vivado flow timed out after {timeout} s.",
                "error_code": "timeout",
            }

        report(0.9, "vivado finished, collecting results")
        log = proc.stdout + "\n" + proc.stderr
        parsed = parse_build_log(log)
        bit_exists = os.path.exists(bit_file)
        ok = proc.returncode == 0 and bit_exists

        log_text, log_truncated = (
            truncate_log(proc.stdout)
            if ok
            else (
                proc.stdout,
                False,
            )
        )

        result: dict = {
            "success": ok,
            "stage": "place_and_route",
            "backend": "vivado",
            "target": "xilinx",
            "device": part,
            "language": language,
            "top_module": top_module,
            "constraints": cst_source,
            "timing": _parse_timing(log, clock_period_ns),
            "utilization": _flatten_utilization(parsed.get("utilization", {})),
            "phases": parsed.get("phase_history", []),
            "warnings": parsed.get("warnings", 0),
            "errors": parsed.get("errors", 0),
            "vivado_log": log_text,
            "stderr": proc.stderr,
        }
        if log_truncated:
            result["logs_truncated"] = True
        if not ok:
            result["error"] = "Vivado flow failed — see vivado_log/stderr."
            result["error_code"] = "pnr_failed"
        if files:
            result["files"] = list(files.keys())
        if project_dir:
            result["project_dir"] = project_dir
        if use_persistent:
            result["work_dir"] = tmpdir

        # Persist the bitstream and return its path (feeds straight into
        # program_fpga's bitstream_path).
        if ok:
            if use_persistent:
                bitstream_path = bit_file
            else:
                bs_dir = data_root() / "bitstreams"
                bs_dir.mkdir(parents=True, exist_ok=True)
                bs_name = f"{top_module}_xilinx_{uuid4().hex[:8]}.bit"
                bitstream_path = str(bs_dir / bs_name)
                shutil.copyfile(bit_file, bitstream_path)
            result["bitstream_path"] = bitstream_path
            result["bitstream_ext"] = ".bit"
            if return_bitstream_b64:
                with open(bit_file, "rb") as bf:
                    result["bitstream_b64"] = base64.b64encode(bf.read()).decode(
                        "ascii"
                    )
            report(1.0, "vivado flow complete")

        return result

    if use_persistent:
        assert work_dir is not None
        return _run_in(work_dir)
    with temporary_workspace("vivado_") as tmpdir:
        return _run_in(tmpdir)
