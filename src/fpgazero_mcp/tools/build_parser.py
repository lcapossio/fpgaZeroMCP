# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
Build log parser for EDA tool output (single-pass architecture).

Supported tools (auto-detected):
  - Yosys (open-source synthesis)
  - nextpnr (open-source place-and-route for iCE40, ECP5, Nexus, Gowin)
  - Vivado (Xilinx/AMD synthesis, implementation, timing)
  - Quartus (Intel/Altera synthesis, fitting, timing)
  - GHDL (VHDL analysis, elaboration, simulation)

Scans build logs in a single pass to extract:
  - Detected tool
  - Current build phase with per-phase elapsed line counts
  - Resource utilization (LUTs, FFs, BRAMs, IOs, DSPs, ALMs, ALUTs, MLABs)
  - Timing metrics (Fmax, WNS, TNS, WHS, THS, critical path, restricted Fmax)
  - Placer/router progress estimation
  - Congestion indicators
  - Accurate warning/error counts (tool-specific patterns)
  - Health assessment with actionable concerns
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------

_TOOL_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("yosys", re.compile(r"Yosys \d|yosys>|Executing \w+ pass", re.I)),
    ("nextpnr", re.compile(r"nextpnr|Info: (?:Pack|Plac|Rout|Device)", re.I)),
    (
        "vivado",
        re.compile(
            r"Vivado |synth_design|place_design|route_design|write_bitstream", re.I
        ),
    ),
    ("quartus", re.compile(r"quartus_|Quartus |Analysis & Synthesis|Fitter \(", re.I)),
    ("ghdl", re.compile(r"ghdl|GHDL \d", re.I)),
]


# ---------------------------------------------------------------------------
# Phase patterns per tool
# ---------------------------------------------------------------------------

_PHASES: dict[str, list[tuple[str, re.Pattern]]] = {
    "yosys": [
        (
            "read_design",
            re.compile(r"(?:Parsing|Reading) (?:Verilog|SystemVerilog|VHDL)", re.I),
        ),
        (
            "elaboration",
            re.compile(r"Executing (?:HIERARCHY|PROC)|Generating RTLIL", re.I),
        ),
        (
            "optimization",
            re.compile(r"Executing (?:OPT|TECHMAP|ABC|FLATTEN|MEMORY)", re.I),
        ),
        ("synthesis", re.compile(r"Executing (?:SYNTH_\w+)", re.I)),
        ("mapping", re.compile(r"Executing (?:ABC9?|MAP_\w+|DFFLEGALIZE)", re.I)),
        ("write_output", re.compile(r"Executing (?:WRITE_|write_)", re.I)),
    ],
    "nextpnr": [
        ("packing", re.compile(r"Packing\.\.\.|Info: Packing")),
        ("placing", re.compile(r"Placer|SA placement|HeAP Placer|Info: Plac")),
        ("routing", re.compile(r"Router|Routing\.\.\.|Info: Rout")),
        ("timing", re.compile(r"Max frequency|Info: Max frequency")),
        ("bitstream", re.compile(r"Writing bitstream|icepack|ecppack")),
    ],
    "vivado": [
        (
            "synth",
            re.compile(
                r"synth_design|Starting Synthesis|Phase \d+.*Synthesis|launch_runs.*synth",
                re.I,
            ),
        ),
        ("phys_opt", re.compile(r"phys_opt_design|Physical Optimization", re.I)),
        ("opt", re.compile(r"opt_design|Phase \d+.*Optimization", re.I)),
        (
            "place",
            re.compile(
                r"place_design|Starting Placer|Phase \d+.*Placement|Placer Phase", re.I
            ),
        ),
        (
            "route",
            re.compile(
                r"route_design|Starting Router|Phase \d+.*Routing|Router Phase", re.I
            ),
        ),
        (
            "timing",
            re.compile(
                r"report_timing_summary|Timing Summary|Design Timing Summary", re.I
            ),
        ),
        ("drc", re.compile(r"report_drc|DRC Results", re.I)),
        (
            "write",
            re.compile(r"write_bitstream|\.bit generation|Bitstream Generation", re.I),
        ),
    ],
    "quartus": [
        (
            "analysis",
            re.compile(r"Analysis & Elaboration|quartus_map.*--analysis", re.I),
        ),
        (
            "synth",
            re.compile(r"quartus_map|Quartus.*Synthesis|Analysis & Synthesis", re.I),
        ),
        ("fit", re.compile(r"quartus_fit|Fitter \(|Starting Fitter", re.I)),
        ("place", re.compile(r"Fitter.*Placement|quartus_fit.*placement", re.I)),
        ("route", re.compile(r"Fitter.*Routing|quartus_fit.*routing", re.I)),
        ("sta", re.compile(r"quartus_sta|TimeQuest|Timing Analyzer|Slow.*Model", re.I)),
        (
            "asm",
            re.compile(
                r"quartus_asm|Assembler|Generating.*\.sof|\.pof generation", re.I
            ),
        ),
        ("eda", re.compile(r"quartus_eda|EDA Netlist Writer", re.I)),
        ("pgm", re.compile(r"quartus_pgm|Programmer", re.I)),
    ],
    "ghdl": [
        ("analyze", re.compile(r"ghdl.*-a\b|Analyzing", re.I)),
        ("elaborate", re.compile(r"ghdl.*-e\b|Elaborating", re.I)),
        ("simulate", re.compile(r"ghdl.*-r\b|Simulating|simulation", re.I)),
    ],
}

# Friendly labels — tool prefix added automatically
_PHASE_LABELS: dict[str, dict[str, str]] = {
    "yosys": {
        "read_design": "Reading design files",
        "elaboration": "Elaborating hierarchy",
        "optimization": "Optimizing logic",
        "synthesis": "Synthesizing for target",
        "mapping": "Technology mapping",
        "write_output": "Writing output",
    },
    "nextpnr": {
        "packing": "Packing primitives",
        "placing": "Placing cells",
        "routing": "Routing nets",
        "timing": "Timing analysis",
        "bitstream": "Generating bitstream",
    },
    "vivado": {
        "synth": "Synthesis",
        "opt": "Logic optimization",
        "phys_opt": "Physical optimization",
        "place": "Placement",
        "route": "Routing",
        "timing": "Timing analysis",
        "drc": "Design rule check",
        "write": "Writing bitstream",
    },
    "quartus": {
        "analysis": "Analysis & Elaboration",
        "synth": "Analysis & Synthesis",
        "fit": "Fitter",
        "place": "Fitter — Placement",
        "route": "Fitter — Routing",
        "sta": "Timing Analyzer (STA)",
        "asm": "Assembler",
        "eda": "EDA Netlist Writer",
        "pgm": "Programmer",
    },
    "ghdl": {
        "analyze": "Analyzing",
        "elaborate": "Elaborating",
        "simulate": "Simulating",
    },
}


# ---------------------------------------------------------------------------
# Warning/error patterns per tool (avoids false positives)
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: dict[str, re.Pattern] = {
    "yosys": re.compile(r"^ERROR:", re.M),
    "nextpnr": re.compile(r"^ERROR:|Error:", re.M),
    "vivado": re.compile(r"^ERROR:\s|CRITICAL WARNING:", re.M),
    "quartus": re.compile(r"^Error \(\d+\):", re.M),
    "ghdl": re.compile(r":\d+:\d+:\s*error:", re.I),
}

_WARNING_PATTERNS: dict[str, re.Pattern] = {
    "yosys": re.compile(r"^Warning:", re.M),
    "nextpnr": re.compile(r"^Warning:|Warning:", re.M),
    "vivado": re.compile(r"^WARNING:\s", re.M),
    "quartus": re.compile(r"^Warning \(\d+\):", re.M),
    "ghdl": re.compile(r":\d+:\d+:\s*warning:", re.I),
}

# Fallback for unknown tools
_ERROR_FALLBACK = re.compile(r"^(?:ERROR|Error)[:\s]", re.M)
_WARNING_FALLBACK = re.compile(r"^(?:WARNING|Warning)[:\s]", re.M)


# ---------------------------------------------------------------------------
# Utilization patterns per tool
# ---------------------------------------------------------------------------

_UTIL_NEXTPNR = [
    (
        re.compile(r"(ICESTORM_LC|LUT4|OXIDE_COMB)[:\s]+([\d,]+)\s*/\s*([\d,]+)"),
        "luts",
        3,
    ),
    (
        re.compile(r"(SB_DFF\w*|TRELLIS_FF|OXIDE_FF)[:\s]+([\d,]+)\s*/\s*([\d,]+)"),
        "ffs",
        3,
    ),
    (
        re.compile(r"(SB_RAM\w*|BRAM\w*|EBR\w*|RAMW\w*)[:\s]+([\d,]+)\s*/\s*([\d,]+)"),
        "brams",
        3,
    ),
    (re.compile(r"(SB_IO|TRELLIS_IO|OXIDE_IO)[:\s]+([\d,]+)\s*/\s*([\d,]+)"), "ios", 3),
    (
        re.compile(r"(DSP48\w*|MULT18\w*|MUL18\w*)[:\s]+([\d,]+)\s*/\s*([\d,]+)"),
        "dsps",
        3,
    ),
]


def _vivado_util_row(label_re: str, num: str = r"[\d,]+") -> re.Pattern:
    """Vivado utilization table row, both layouts:
      old:  | Resource | Used | Fixed | Available | Util% |
      new:  | Resource | Used | Fixed | Prohibited | Available | Util% |
    Captures Used and Available; the trailing Util% column anchors the match
    so the optional Prohibited column can't shift what gets captured.
    """
    return re.compile(
        label_re
        + rf"\s*\|\s*({num})\s*\|\s*{num}\s*\|\s*(?:{num}\s*\|\s*)?({num})"
        + r"\s*\|\s*[\d.]+\s*\|"
    )


_UTIL_VIVADO = [
    (_vivado_util_row(r"(?:Slice |CLB )LUTs?"), "luts", 2),
    (_vivado_util_row(r"(?:Slice |CLB )Registers?"), "ffs", 2),
    (_vivado_util_row(r"Block RAM Tile", r"[\d,.]+"), "brams", 2),
    (_vivado_util_row(r"DSPs?"), "dsps", 2),
    (_vivado_util_row(r"Bonded IOB"), "ios", 2),
    (_vivado_util_row(r"URAM"), "urams", 2),
]

_UTIL_QUARTUS = [
    (re.compile(r"Total logic elements?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)"), "les", 2),
    (
        re.compile(r"(?:Total )?ALMs?\s*(?:used)?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)"),
        "alms",
        2,
    ),
    (
        re.compile(
            r"(?:Total )?(?:Adaptive |dedicated )?ALUTs?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)",
            re.I,
        ),
        "aluts",
        2,
    ),
    (
        re.compile(
            r"(?:Total )?(?:dedicated )?registers?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)",
            re.I,
        ),
        "ffs",
        2,
    ),
    (re.compile(r"Total memory bits\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)"), "mem_bits", 2),
    (
        re.compile(
            r"(?:Total )?(?:M\d+K|M\d+|block memory|RAM) blocks?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)",
            re.I,
        ),
        "brams",
        2,
    ),
    (
        re.compile(
            r"(?:Total )?(?:embedded )?(?:DSP|multiplier)\s*(?:block|element)s?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)",
            re.I,
        ),
        "dsps",
        2,
    ),
    (re.compile(r"(?:Total )?pins?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)", re.I), "ios", 2),
    (re.compile(r"(?:Total )?PLLs?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)", re.I), "plls", 2),
    (re.compile(r"MLABs?\s*[;:]\s*([\d,]+)\s*/\s*([\d,]+)", re.I), "mlabs", 2),
]


# ---------------------------------------------------------------------------
# Timing patterns
# ---------------------------------------------------------------------------

_RE_NEXTPNR_FMAX = re.compile(
    r"Max frequency for clock\s+'?(\w+)'?[^:]*:\s*([\d.]+)\s*MHz(?:\s*\((\w+))?"
)
_RE_CRITICAL_PATH = re.compile(r"Critical path[^:]*:\s*([\d.]+)\s*ns")
_RE_VIVADO_FMAX = re.compile(
    r"(?:Fmax|Maximum Frequency)\s*[;:=]\s*([\d.]+)\s*MHz", re.I
)
_RE_QUARTUS_FMAX = re.compile(r"Fmax\s*[;:]\s*([\d.]+)\s*MHz", re.I)
_RE_QUARTUS_RESTRICTED_FMAX = re.compile(
    r"Restricted\s+Fmax\s*[;:]\s*([\d.]+)\s*MHz", re.I
)
_RE_QUARTUS_SETUP_SLACK = re.compile(
    r"(?:Worst.case )?[Ss]etup\s+slack\s*[;:]\s*(-?[\d.]+)", re.I
)
_RE_QUARTUS_HOLD_SLACK = re.compile(
    r"(?:Worst.case )?[Hh]old\s+slack\s*[;:]\s*(-?[\d.]+)", re.I
)

_SLACK_PATTERNS = [
    ("wns_ns", re.compile(r"WNS\s*[\(:=]\s*(-?[\d.]+)\s*(?:ns)?\b", re.I)),
    ("tns_ns", re.compile(r"TNS\s*[\(:=]\s*(-?[\d.]+)\s*(?:ns)?\b", re.I)),
    ("whs_ns", re.compile(r"WHS\s*[\(:=]\s*(-?[\d.]+)\s*(?:ns)?\b", re.I)),
    ("ths_ns", re.compile(r"THS\s*[\(:=]\s*(-?[\d.]+)\s*(?:ns)?\b", re.I)),
    ("wns_ns", re.compile(r"Slack\s+histogram.*?worst\s*=?\s*(-?[\d.]+)", re.I)),
]

_CONGESTION_PATTERNS = [
    ("level", re.compile(r"congestion level\s*[=:]\s*(\w+)", re.I)),
    ("unrouted_nets", re.compile(r"(\d+)\s+unrouted", re.I)),
    ("unrouted_nets", re.compile(r"failed to route (\d+)", re.I)),
    ("budget", re.compile(r"routing budget exceeded", re.I)),
    ("level", re.compile(r"Estimated.*congestion\s*[=:]\s*(\w+)", re.I)),
    ("unrouted_nets", re.compile(r"Routing problems[^:]*:\s*(\d+)", re.I)),
    ("level", re.compile(r"Interconnect usage\s*[;:]\s*([\d.]+)\s*%", re.I)),
]

# Progress estimation
_RE_NEXTPNR_PLACE_ITER = re.compile(r"at t\s*=\s*([\d.e+-]+)", re.I)
_RE_NEXTPNR_ROUTE_PASS = re.compile(r"Pass\s+(\d+)", re.I)
_RE_VIVADO_PHASE_STEP = re.compile(r"Phase\s+(\d+)\.(\d+)", re.I)
_RE_QUARTUS_FIT_PCT = re.compile(r"(\d+)%\s+(?:complete|done)", re.I)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pi(s: str) -> int:
    """Parse int, stripping commas."""
    return int(s.replace(",", ""))


def _pf(s: str) -> float:
    """Parse float, stripping commas."""
    return float(s.replace(",", ""))


def _make_util(used: float, total: float) -> dict:
    pct = round(used / max(total, 0.001) * 100, 1)
    return {
        "used": used,
        "total": total,
        "pct": min(pct, 100.0) if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Single-pass collector
# ---------------------------------------------------------------------------


@dataclass
class _Collector:
    """Accumulates parse results in a single pass over log lines."""

    tool: str = "unknown"
    phase: str = "starting"
    phase_history: list[tuple[str, int]] = field(default_factory=list)
    utilization: dict[str, dict] = field(default_factory=dict)
    timing: dict = field(default_factory=dict)
    slack: dict = field(default_factory=dict)
    congestion: dict = field(default_factory=dict)
    warnings: int = 0
    errors: int = 0
    progress: dict = field(default_factory=dict)
    _phase_start_line: int = 0
    _tools_seen: set = field(default_factory=set)
    _place_iters: int = 0
    _route_passes: int = 0

    def _set_phase(self, name: str, line_no: int) -> None:
        prefixed = f"{self.tool}_{name}" if self.tool not in ("unknown",) else name
        if self.phase_history and self.phase_history[-1][0] == prefixed:
            return
        # Record line count for previous phase
        if self.phase_history:
            prev_name, prev_start = self.phase_history[-1]
            self.phase_history[-1] = (prev_name, line_no - prev_start)
        self.phase_history.append((prefixed, 0))
        self.phase = prefixed
        self._phase_start_line = line_no

    def finalize(self, total_lines: int) -> None:
        """Close out the last phase's line count."""
        if self.phase_history:
            prev_name, prev_start = self.phase_history[-1]
            lines_in_phase = total_lines - self._phase_start_line
            self.phase_history[-1] = (prev_name, lines_in_phase)


def _detect_tool(line: str, seen: set) -> str | None:
    for tool, pat in _TOOL_SIGNATURES:
        if pat.search(line):
            seen.add(tool)
            return tool
    return None


def _process_phase(c: _Collector, line: str, line_no: int) -> None:
    phases = _PHASES.get(c.tool, [])
    # Also check all tools if tool is unknown
    if c.tool == "unknown":
        for tool_phases in _PHASES.values():
            phases = phases + tool_phases

    for phase_name, pat in phases:
        if pat.search(line):
            c._set_phase(phase_name, line_no)
            return


def _process_util(c: _Collector, line: str) -> None:
    patterns: list[tuple[re.Pattern, str, int]] = []
    if c.tool in ("nextpnr", "unknown"):
        patterns += _UTIL_NEXTPNR
    if c.tool in ("vivado", "unknown"):
        patterns += _UTIL_VIVADO
    if c.tool in ("quartus", "unknown"):
        patterns += _UTIL_QUARTUS

    for pat, label, ngroups in patterns:
        m = pat.search(line)
        if m:
            if ngroups == 3:
                c.utilization[label] = _make_util(_pi(m.group(2)), _pi(m.group(3)))
            elif label == "brams" and c.tool == "vivado":
                c.utilization[label] = _make_util(_pf(m.group(1)), _pf(m.group(2)))
            else:
                c.utilization[label] = _make_util(_pi(m.group(1)), _pi(m.group(2)))


def _process_timing(c: _Collector, line: str) -> None:
    # nextpnr Fmax
    m = _RE_NEXTPNR_FMAX.search(line)
    if m:
        c.timing["clock"] = m.group(1)
        c.timing["fmax_mhz"] = float(m.group(2))
        if m.group(3):
            c.timing["met"] = m.group(3).upper() == "PASS"
        return

    # Critical path
    m = _RE_CRITICAL_PATH.search(line)
    if m:
        c.timing["critical_path_ns"] = float(m.group(1))
        return

    # Quartus Restricted Fmax (before regular)
    m = _RE_QUARTUS_RESTRICTED_FMAX.search(line)
    if m:
        c.timing["restricted_fmax_mhz"] = float(m.group(1))
        return

    # Fmax (Vivado or Quartus regular)
    if c.tool in ("vivado", "unknown"):
        m = _RE_VIVADO_FMAX.search(line)
        if m:
            c.timing["fmax_mhz"] = float(m.group(1))
            return
    if c.tool in ("quartus", "unknown"):
        m = _RE_QUARTUS_FMAX.search(line)
        if m:
            c.timing["fmax_mhz"] = float(m.group(1))
            return

    # Quartus setup/hold slack
    m = _RE_QUARTUS_SETUP_SLACK.search(line)
    if m:
        c.timing["setup_slack_ns"] = float(m.group(1))
        return
    m = _RE_QUARTUS_HOLD_SLACK.search(line)
    if m:
        c.timing["hold_slack_ns"] = float(m.group(1))


def _process_slack(c: _Collector, line: str) -> None:
    for key, pat in _SLACK_PATTERNS:
        m = pat.search(line)
        if m:
            c.slack[key] = float(m.group(1))


def _process_congestion(c: _Collector, line: str) -> None:
    for kind, pat in _CONGESTION_PATTERNS:
        m = pat.search(line)
        if m:
            if kind == "unrouted_nets":
                c.congestion["unrouted_nets"] = int(m.group(1))
            elif kind == "budget":
                c.congestion["budget_exceeded"] = True
            elif kind == "level":
                c.congestion["level"] = m.group(1)


def _process_errors_warnings(c: _Collector, line: str) -> None:
    err_pat = _ERROR_PATTERNS.get(c.tool, _ERROR_FALLBACK)
    warn_pat = _WARNING_PATTERNS.get(c.tool, _WARNING_FALLBACK)
    if err_pat.search(line):
        c.errors += 1
    if warn_pat.search(line):
        c.warnings += 1


def _process_progress(c: _Collector, line: str) -> None:
    # nextpnr placer iterations
    if c.tool == "nextpnr" and "placing" in c.phase:
        m = _RE_NEXTPNR_PLACE_ITER.search(line)
        if m:
            c._place_iters += 1
            c.progress["placer_iterations"] = c._place_iters

    # nextpnr router passes
    if c.tool == "nextpnr" and "routing" in c.phase:
        m = _RE_NEXTPNR_ROUTE_PASS.search(line)
        if m:
            c._route_passes = int(m.group(1))
            c.progress["router_pass"] = c._route_passes

    # Vivado phase steps
    if c.tool == "vivado":
        m = _RE_VIVADO_PHASE_STEP.search(line)
        if m:
            c.progress["vivado_phase"] = f"{m.group(1)}.{m.group(2)}"

    # Quartus fitter %
    if c.tool == "quartus":
        m = _RE_QUARTUS_FIT_PCT.search(line)
        if m:
            c.progress["fitter_pct"] = int(m.group(1))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_build_log(log_text: str) -> dict:
    """Parse a build log in a single pass and return structured results.

    Auto-detects the EDA tool and applies only relevant patterns.
    """
    c = _Collector()
    lines = log_text.splitlines()

    for i, line in enumerate(lines):
        # Tool detection (first match sticks, but keep scanning for multi-tool flows)
        detected = _detect_tool(line, c._tools_seen)
        if detected and c.tool == "unknown":
            c.tool = detected

        # Phase
        _process_phase(c, line, i)

        # Metrics (only relevant patterns based on detected tool)
        _process_util(c, line)
        _process_timing(c, line)
        _process_slack(c, line)
        _process_congestion(c, line)
        _process_errors_warnings(c, line)
        _process_progress(c, line)

    c.finalize(len(lines))

    # Build phase history as list of dicts with names and line counts
    phase_names = [name for name, _ in c.phase_history]
    phase_detail = [{"phase": name, "lines": count} for name, count in c.phase_history]

    # Phase label
    tool_labels = _PHASE_LABELS.get(c.tool, {})
    # Strip tool prefix for label lookup
    raw_phase = c.phase
    if c.tool != "unknown" and raw_phase.startswith(c.tool + "_"):
        raw_phase = raw_phase[len(c.tool) + 1 :]
    label = tool_labels.get(raw_phase, c.phase)
    if c.tool not in ("unknown",):
        label = f"{c.tool.capitalize()}: {label}" if label != c.phase else c.phase

    # Health
    health = _assess_health(
        c.phase, c.utilization, c.timing, c.slack, c.congestion, c.errors
    )

    result: dict = {
        "tool": c.tool,
        "phase": c.phase,
        "phase_label": label,
        "phase_history": phase_names,
        "phase_detail": phase_detail,
        "utilization": c.utilization,
        "health": health,
    }
    if c.timing:
        result["timing"] = c.timing
    if c.slack:
        result["slack"] = c.slack
    if c.congestion:
        result["congestion"] = c.congestion
    if c.progress:
        result["progress"] = c.progress
    if len(c._tools_seen) > 1:
        result["tools_detected"] = sorted(c._tools_seen)
    result["warnings"] = c.warnings
    result["errors"] = c.errors

    return result


# ---------------------------------------------------------------------------
# Health assessment
# ---------------------------------------------------------------------------


def _assess_health(
    phase: str,
    utilization: dict,
    timing: dict,
    slack: dict,
    congestion: dict,
    errors: int,
) -> dict:
    """Produce a health summary with status and any concerns."""
    status = "ok"
    concerns: list[str] = []

    if errors > 0:
        status = "error"
        concerns.append(f"{errors} error(s) in log")

    # High utilization
    for res, data in utilization.items():
        pct = data.get("pct", 0)
        if pct > 95:
            status = "critical" if status != "error" else status
            concerns.append(
                f"{res} utilization at {pct}% — nearly full, expect congestion"
            )
        elif pct > 80:
            if status == "ok":
                status = "warning"
            concerns.append(f"{res} utilization at {pct}% — getting tight")

    # Timing — Fmax met flag (nextpnr)
    if timing.get("met") is False:
        status = "critical" if status != "error" else status
        concerns.append(
            f"Timing FAILED — Fmax {timing.get('fmax_mhz', '?')} MHz "
            f"does not meet constraint"
        )

    # Timing — Quartus setup slack
    setup_slack = timing.get("setup_slack_ns")
    if setup_slack is not None and setup_slack < 0:
        status = "critical" if status != "error" else status
        concerns.append(f"Setup slack = {setup_slack} ns — timing violated")
    elif setup_slack is not None and setup_slack < 0.5:
        if status == "ok":
            status = "warning"
        concerns.append(f"Setup slack = {setup_slack} ns — very tight margin")

    # Timing — Quartus hold slack
    hold_slack = timing.get("hold_slack_ns")
    if hold_slack is not None and hold_slack < 0:
        status = "critical" if status != "error" else status
        concerns.append(f"Hold slack = {hold_slack} ns — hold violation")

    # Slack — Vivado WNS
    wns = slack.get("wns_ns")
    if wns is not None and wns < 0:
        status = "critical" if status != "error" else status
        concerns.append(f"WNS = {wns} ns — timing violated")
    elif wns is not None and wns < 0.5:
        if status == "ok":
            status = "warning"
        concerns.append(f"WNS = {wns} ns — very tight margin")

    # Congestion
    unrouted = congestion.get("unrouted_nets", 0)
    if unrouted > 0:
        status = "critical" if status != "error" else status
        concerns.append(f"{unrouted} unrouted net(s) — routing congestion")
    if congestion.get("budget_exceeded"):
        status = "critical" if status != "error" else status
        concerns.append("Routing budget exceeded")

    if not concerns:
        concerns.append("No issues detected")

    return {"status": status, "concerns": concerns}
