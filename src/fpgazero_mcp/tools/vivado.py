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

from fpgazero_mcp.tools import proctree
from fpgazero_mcp.tools.build_parser import parse_build_log
from fpgazero_mcp.tools.progress import make_reporter
from fpgazero_mcp.tools.sources import (
    resolve_sources,
    validate_path_in_roots,
    validate_top_module,
)
from fpgazero_mcp.tools.textutil import truncate_log
from fpgazero_mcp.paths import data_root
from fpgazero_mcp.tools.workspace import temporary_workspace

# Xilinx part numbers: letters, digits, dash (e.g. xc7a35ticsg324-1L)
_PART_RE = re.compile(r"^[A-Za-z0-9\-]+$")

# Directives are interpolated into the generated TCL, so they are restricted to
# a known-good set rather than accepting arbitrary text. These are the ones that
# matter for timing closure; Vivado accepts more.
SYNTH_DIRECTIVES = (
    "default",
    "PerformanceOptimized",
    "AreaOptimized_high",
    "AlternateRoutability",
    "RuntimeOptimized",
)
PLACE_DIRECTIVES = (
    "default",
    "Explore",
    "ExtraTimingOpt",
    "ExtraPostPlacementOpt",
    "WLDrivenBlockPlacement",
    "RuntimeOptimized",
)
ROUTE_DIRECTIVES = (
    "default",
    "Explore",
    "AggressiveExplore",
    "NoTimingRelaxation",
    "MoreGlobalIterations",
    "HigherDelayCost",
    "RuntimeOptimized",
)

# create_clock -period <ns> in XDC text
_RE_XDC_PERIOD = re.compile(r"create_clock\s+[^\n]*-period\s+([\d.]+)")

# A number that can't swallow a sentence-ending period: Vivado prints
# "Post Placement Timing Summary WNS=6.361. For the most ...", and a
# [\d.]+ capture there makes float() raise.
_NUM = r"-?(?:\d+(?:\.\d*)?|\.\d+)"

# route_design inline summary: "Estimated Timing Summary | WNS=0.087 | TNS=0.000"
# Several appear per run (placement estimates, then routing); the last wins.
_RE_WNS_INLINE = re.compile(r"\bWNS\s*=\s*(" + _NUM + ")")
_RE_TNS_INLINE = re.compile(r"\bTNS\s*=\s*(" + _NUM + ")")

# Printed by Vivado once routing finishes, before any later step can fail.
_RE_ROUTE_DONE = re.compile(r"^route_design completed successfully", re.M)

# report_timing_summary table:
#     WNS(ns)      TNS(ns)  TNS Failing Endpoints ... WHS(ns)      THS(ns) ...
#     -------      -------  --------------------- ... -------      ------- ...
#       0.123        0.000                      0 ...   0.045        0.000 ...
_RE_TIMING_TABLE = re.compile(
    r"WNS\(ns\)\s+TNS\(ns\).*?\n[\s-]+\n\s*"
    r"(-?[\d.]+)\s+(-?[\d.]+)\s+\d+\s+\d+\s+(-?[\d.]+)\s+(-?[\d.]+)",
    re.S,
)


_TCL_BARE_WORD = re.compile(r"[A-Za-z0-9_.:/+-]+")
_TCL_ESCAPE = frozenset(' \t"$[]{}\\;')


def _tcl_word(s: str) -> str:
    """Quote s as one literal Tcl word: no variable, command or backslash
    substitution, no word or command splitting.

    Brace quoting is literal but cannot hold an unbalanced brace: a source
    file named 'a}; exec ...; #.v' would close the quote and run the rest
    as Tcl. Such strings are backslash-escaped character by character
    instead. Control characters are refused outright (backslash-newline is
    a line continuation in Tcl, so it cannot be escaped that way).
    """
    if any(ord(c) < 0x20 or c == "\x7f" for c in s):
        raise ValueError(f"control character in Tcl argument: {s!r}")
    if _TCL_BARE_WORD.fullmatch(s):
        return s
    if not any(c in s for c in "{}\\"):
        return "{" + s + "}"
    return "".join("\\" + c if c in _TCL_ESCAPE else c for c in s)


def _tcl_path(path: str) -> str:
    """Format a filesystem path for Tcl: forward slashes, quoted literally
    (brace-quoted whenever braces can hold it)."""
    p = path.replace("\\", "/")
    word = _tcl_word(p)
    return "{" + p + "}" if word == p else word


def _generate_tcl(
    src_paths: list[str],
    language: str,
    top_module: str,
    part: str,
    xdc_file: str | None,
    bit_file: str,
    synth_directive: str = "default",
    place_directive: str = "default",
    route_directive: str = "default",
    phys_opt: bool = False,
) -> str:
    """Build the non-project batch flow script.

    The *_directive arguments steer Vivado effort/strategy; "default" omits the
    flag entirely, so an unsteered call emits exactly the original script.
    phys_opt inserts a physical-optimization pass after placement, the usual
    next lever once placement alone will not close timing.
    """
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

    def _flag(directive: str) -> str:
        return "" if directive == "default" else f" -directive {directive}"

    # top_module may legally contain '$' (Verilog identifier), which Tcl
    # would substitute; part and directives are allowlist-checked.
    lines.append(
        f"synth_design -top {_tcl_word(top_module)} -part {part}"
        f"{_flag(synth_directive)}"
    )
    lines.append("opt_design")
    lines.append(f"place_design{_flag(place_directive)}")
    if phys_opt:
        lines.append("phys_opt_design")
    lines.append(f"route_design{_flag(route_directive)}")
    lines += [
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
        wns_all = _RE_WNS_INLINE.findall(log)
        if wns_all:
            timing["wns_ns"] = float(wns_all[-1])
        tns_all = _RE_TNS_INLINE.findall(log)
        if tns_all:
            timing["tns_ns"] = float(tns_all[-1])

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
    synth_directive: str = "default",
    place_directive: str = "default",
    route_directive: str = "default",
    phys_opt: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Run the full Vivado RTL-to-bitstream flow on the given sources.

    part: full Xilinx part number, e.g. "xc7a35tcpg236-1".
    constraints: XDC text; if omitted and project_dir is used, auto-detects
                 a .xdc file from the project directory.

    synth_directive / place_directive / route_directive: Vivado strategy
                 directives, each one of the corresponding *_DIRECTIVES tuple.
                 "default" leaves the flag off. Sweeping these is how
                 `close_timing` attacks a timing failure on Xilinx parts.
    phys_opt: run phys_opt_design after placement.
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

    for name, value, allowed in (
        ("synth_directive", synth_directive, SYNTH_DIRECTIVES),
        ("place_directive", place_directive, PLACE_DIRECTIVES),
        ("route_directive", route_directive, ROUTE_DIRECTIVES),
    ):
        if value not in allowed:
            return {
                "success": False,
                "error": (
                    f"Invalid {name} '{value}'. Must be one of: {', '.join(allowed)}."
                ),
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
        src_paths, err = resolve_sources(code, files, project_dir, language, tmpdir)
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

        try:
            script = _generate_tcl(
                src_paths,
                language,
                top_module,
                part,
                xdc_file,
                bit_file,
                synth_directive,
                place_directive,
                route_directive,
                phys_opt,
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_code": "invalid_input"}
        with open(tcl_file, "w", encoding="utf-8") as f:
            f.write(script)

        report(0.1, "running vivado batch flow (synth, place, route, bitstream)")
        try:
            # run_tree, not subprocess.run: on Windows vivado is vivado.bat,
            # so a plain timeout would kill cmd.exe and leave Vivado running
            # (and run() blocked on its output pipe until it finished).
            proc = proctree.run_tree(
                [
                    vivado_bin,
                    "-mode",
                    "batch",
                    "-nolog",
                    "-nojournal",
                    "-source",
                    tcl_file,
                ],
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
            # Same meaning as the nextpnr backend's "routed": routing finished,
            # whether or not a bitstream followed (write_bitstream can still
            # fail on DRC). close_timing ranks attempts on it.
            "routed": ok or bool(_RE_ROUTE_DONE.search(proc.stdout)),
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
