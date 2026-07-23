# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import functools
import json
import logging

from mcp_lite import CallToolResult, Server, TextContent, Tool

logger = logging.getLogger("fpgaZeroMCP")

app = Server("fpgaZeroMCP", version="0.3.0")


# Lazy singletons — CoreRegistry does disk I/O and BuildManager allocates a
# lock on construction. Deferring these means sessions that never touch cores
# or background builds pay zero cost for them.
#
# NOTE: tool modules are also imported lazily inside handle_call_tool. This
# shifts failures from server boot to first tool call — a deliberate tradeoff
# for faster start and lower memory footprint per session.
@functools.cache
def _registry():
    from registry.resolver import CoreRegistry

    return CoreRegistry()


@functools.cache
def _builds():
    from tools.build_manager import BuildManager

    return BuildManager()


_MAX_TIMEOUT = 3600  # 1 hour hard cap


def _clamp_timeout(value: int, default: int) -> int:
    """Clamp timeout to a sane range [1, _MAX_TIMEOUT]."""
    if not isinstance(value, int) or value < 1:
        return default
    return min(value, _MAX_TIMEOUT)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


@app.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="lint_hdl",
            description=(
                "Lint HDL source code using iverilog/verilator (Verilog/SystemVerilog) or ghdl (VHDL). "
                "Use linter='verilator' to enable -Wall checks including multidriven net detection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "HDL source code to lint",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "top_module": {
                        "type": "string",
                        "description": "Top-level module name (optional)",
                    },
                    "linter": {
                        "type": "string",
                        "enum": ["iverilog", "verilator"],
                        "default": "iverilog",
                        "description": "Linter backend for Verilog/SV. 'verilator' enables -Wall (multidriven nets, etc.)",
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="lint_project",
            description=(
                "Lint multiple HDL files together so cross-module references resolve. "
                "Pass a dict of filename-to-source pairs. All files are compiled in one invocation "
                "of iverilog/verilator (Verilog/SystemVerilog) or ghdl (VHDL). "
                "Use linter='verilator' to enable -Wall checks including multidriven net detection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "object",
                        "description": (
                            "Mapping of filename to source code, "
                            'e.g. {"uart_tx.v": "module uart_tx...", "top.v": "module top..."}'
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "top_module": {
                        "type": "string",
                        "description": "Top-level module name (optional)",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 60,
                        "description": "Timeout in seconds",
                    },
                    "linter": {
                        "type": "string",
                        "enum": ["iverilog", "verilator"],
                        "default": "iverilog",
                        "description": "Linter backend for Verilog/SV. 'verilator' enables -Wall (multidriven nets, etc.)",
                    },
                },
                "required": ["files"],
            },
        ),
        Tool(
            name="synthesize",
            description=(
                "Synthesize HDL (Verilog, SystemVerilog, or VHDL) using Yosys or run LiteX backend. "
                "Provide source as: code (single string), files (dict of filename→source), "
                "or project_dir (path to HDL files on disk). "
                "Returns structured resource statistics (stats field: wires, cells, "
                "cells_by_type, ...) and the list of inferred modules. "
                "Logs are truncated on success; full logs are kept on failure. "
                "Supported targets: generic, ice40, ecp5, gowin, xilinx, intel."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "HDL source code (single-file mode)",
                    },
                    "files": {
                        "type": "object",
                        "description": "Multi-file mode: mapping of filename to source code",
                        "additionalProperties": {"type": "string"},
                    },
                    "project_dir": {
                        "type": "string",
                        "description": "Disk mode: path to directory containing HDL source files",
                    },
                    "top_module": {
                        "type": "string",
                        "description": "Name of the top-level module",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "target": {
                        "type": "string",
                        "enum": [
                            "generic",
                            "ice40",
                            "ecp5",
                            "nexus",
                            "gowin",
                            "xilinx",
                            "intel",
                        ],
                        "default": "generic",
                        "description": "FPGA family / synthesis target",
                    },
                    "backend": {
                        "type": "string",
                        "enum": ["yosys", "litex"],
                        "default": "yosys",
                        "description": "Synthesis backend",
                    },
                    "litex_board": {
                        "type": "string",
                        "description": "LiteX board target (required if backend=litex)",
                    },
                    "litex_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra LiteX CLI args (backend=litex)",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 120,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["top_module"],
            },
        ),
        Tool(
            name="place_and_route",
            description=(
                "Synthesize HDL with Yosys then place-and-route with nextpnr in one step. "
                "Provide source as: code (single string), files (dict of filename→source), "
                "or project_dir (path to HDL files on disk). "
                "If backend=litex, runs LiteX build and ignores HDL inputs. "
                "Returns max frequency, critical path, resource utilization, and "
                "bitstream_path — a file on disk ready for program_fpga. "
                "Set return_bitstream_b64=true to also get the bitstream inline. "
                "Supported targets: ice40, ecp5, nexus, gowin.\n"
                "Common device/package values:\n"
                "  ice40: device=hx1k|hx8k|up5k|lp1k  package=tq144|qn84|sg48|cm81\n"
                "  ecp5:  device=25k|45k|85k            package=CABGA256|CABGA381\n"
                "  nexus: device=LIFCL-40-9BG400C       (package embedded in device string)\n"
                "  gowin: device=GW1N-UV4LQ144C6/I5     (package embedded in device string)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "HDL source code (single-file mode)",
                    },
                    "files": {
                        "type": "object",
                        "description": "Multi-file mode: mapping of filename to source code",
                        "additionalProperties": {"type": "string"},
                    },
                    "project_dir": {
                        "type": "string",
                        "description": "Disk mode: path to directory containing HDL source files",
                    },
                    "top_module": {
                        "type": "string",
                        "description": "Top-level module name",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["ice40", "ecp5", "nexus", "gowin"],
                        "description": "FPGA family",
                    },
                    "device": {
                        "type": "string",
                        "description": "Device variant, e.g. 'hx1k', '25k', 'LIFCL-40-9BG400C'",
                    },
                    "package": {
                        "type": "string",
                        "description": "Package, e.g. 'tq144', 'CABGA256' (not needed for nexus/gowin)",
                    },
                    "constraints": {
                        "type": "string",
                        "description": "Optional pin constraints (PCF/LPF/PDC/CST text)",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 300,
                        "description": "PnR timeout in seconds",
                    },
                    "backend": {
                        "type": "string",
                        "enum": ["yosys", "litex"],
                        "default": "yosys",
                        "description": "PnR backend",
                    },
                    "litex_board": {
                        "type": "string",
                        "description": "LiteX board target (required if backend=litex)",
                    },
                    "litex_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra LiteX CLI args (backend=litex)",
                    },
                    "board": {
                        "type": "string",
                        "description": (
                            "Board preset (e.g. 'icebreaker', 'ulx3s_85f'). "
                            "Sets target/device/package/clock automatically."
                        ),
                    },
                    "nextpnr_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra nextpnr arguments (e.g. ['--seed', '42', '--placer', 'heap'])",
                    },
                    "work_dir": {
                        "type": "string",
                        "description": "Persistent working directory for incremental runs. Returned in response for reuse.",
                    },
                    "return_bitstream_b64": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include the bitstream as base64 in the response. "
                            "Default: only bitstream_path (file on disk) is returned."
                        ),
                    },
                },
                "required": ["top_module"],
            },
        ),
        Tool(
            name="simulate",
            description=(
                "Compile and simulate HDL using Icarus Verilog (iverilog + vvp) or GHDL (VHDL). "
                "Provide the design source and a separate testbench. "
                "Returns all $display/$monitor output (Verilog) or report output (VHDL), "
                "a pass/fail verdict, and a structured VCD waveform summary. "
                "Set return_vcd=true to include the raw VCD text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "HDL design source"},
                    "testbench": {
                        "type": "string",
                        "description": "HDL testbench source",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 60,
                        "description": "Timeout in seconds",
                    },
                    "return_vcd": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include raw VCD waveform text in the response "
                            "(large). Default: structured summary only."
                        ),
                    },
                },
                "required": ["code", "testbench"],
            },
        ),
        Tool(
            name="list_ip_cores",
            description="List all available IP cores in the registry. Optionally filter by category.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category, e.g. 'communication' or 'memory'",
                    },
                },
            },
        ),
        Tool(
            name="get_ip_core",
            description="Fetch the full manifest and HDL source files for a named IP core.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Core name, e.g. 'uart_tx' or 'fifo'",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="search_github_cores",
            description=(
                "Search GitHub for open-source FPGA IP cores (MIT, BSD, Apache, GPL, etc). "
                "Returns repo names, star counts, descriptions and topics. "
                "Use import_github_core to download a result into the local registry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms, e.g. 'uart verilog' or 'riscv softcore'",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "description": "Filter by HDL language (optional)",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="import_github_core",
            description=(
                "Download an open-source GitHub repository and add it to the local IP core registry. "
                "Automatically uses FuseSoC CAPI2 metadata (.core file) if one exists in the repo. "
                "After import, the core is immediately available via get_ip_core and generate_ip."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "GitHub repo in 'owner/repo' format, e.g. 'ultraembedded/core_uart'",
                    },
                    "subdir": {
                        "type": "string",
                        "description": "Subdirectory within the repo to scope HDL search (for monorepos)",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Branch, tag, or commit SHA (default: repo's default branch)",
                    },
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="import_fusesoc_core",
            description=(
                "Import a local FuseSoC CAPI2 .core file into the registry. "
                "HDL files referenced in the .core file must exist in the same directory. "
                "Useful when you already have FuseSoC cores checked out locally."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the .core file",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="generate_ip",
            description=(
                "Generate a parameterized instance of an IP core. "
                "Returns the HDL source files and a ready-to-paste Verilog instantiation snippet "
                "with the requested parameter values applied."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Core name, e.g. 'uart_tx' or 'fifo'",
                    },
                    "parameters": {
                        "type": "object",
                        "description": 'Parameter overrides, e.g. {"CLKS_PER_BIT": 434}',
                        "additionalProperties": True,
                    },
                    "instance_name": {
                        "type": "string",
                        "description": "Verilog instance name (default: <core_name>_inst)",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="get_diagnostics",
            description=(
                "Return structured lint diagnostics (line, column, severity, message) for HDL source. "
                "Verilog/SystemVerilog: uses Verilator (primary) with verible-verilog-lint as fallback. "
                "VHDL: uses GHDL. "
                "All tools are part of OSS CAD Suite."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "HDL source code"},
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="format_hdl",
            description=(
                "Format HDL source code and return the result. "
                "Verilog/SystemVerilog: uses verible-verilog-format. "
                "VHDL: uses vsg (pip install vsg). "
                "Returns the formatted code and whether it changed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "HDL source code to format",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="litex_build",
            description=(
                "Run LiteX board target with --build. "
                "Returns logs and output directory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "board": {"type": "string", "description": "LiteX board target"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra LiteX CLI args",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Optional output directory",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 600,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["board"],
            },
        ),
        Tool(
            name="litex_soc",
            description=(
                "Generate LiteX SoC without building gateware. "
                "Returns logs and output directory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "board": {"type": "string", "description": "LiteX board target"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra LiteX CLI args",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Optional output directory",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 300,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["board"],
            },
        ),
        Tool(
            name="litex_flow",
            description="Run a generic LiteX board target with caller-provided args.",
            inputSchema={
                "type": "object",
                "properties": {
                    "board": {"type": "string", "description": "LiteX board target"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra LiteX CLI args",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 600,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["board"],
            },
        ),
        Tool(
            name="start_build",
            description=(
                "Start a long-running build command in the background. "
                "Returns a build_id to check progress with build_status. "
                "Use for synthesis, place-and-route, LiteX builds, or any command that takes minutes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Command and arguments, e.g. ['yosys', '-s', 'synth.ys']",
                    },
                    "label": {
                        "type": "string",
                        "description": "Human-readable label for this build (optional)",
                    },
                    "work_dir": {
                        "type": "string",
                        "description": "Working directory for the build (optional, defaults to project root)",
                    },
                },
                "required": ["cmd"],
            },
        ),
        Tool(
            name="build_status",
            description=(
                "Check the progress of a background build. "
                "Returns status (running/success/failed), elapsed time, and recent log output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "build_id": {
                        "type": "string",
                        "description": "Build ID returned by start_build",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "default": 30,
                        "description": "Number of log lines to return from the end",
                    },
                    "parse": {
                        "type": "boolean",
                        "default": True,
                        "description": "Parse log for build phase/utilization/timing (set false for fast polling)",
                    },
                },
                "required": ["build_id"],
            },
        ),
        Tool(
            name="list_builds",
            description="List all tracked builds (running and finished) with status summary.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cancel_build",
            description="Cancel a running background build.",
            inputSchema={
                "type": "object",
                "properties": {
                    "build_id": {"type": "string", "description": "Build ID to cancel"},
                },
                "required": ["build_id"],
            },
        ),
        Tool(
            name="check_tools",
            description=(
                "Check which EDA tools are installed and reachable. "
                "Returns tool name, path, and version for each detected binary."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="reload_registry",
            description=(
                "Re-scan all core directories and rebuild the IP core cache. "
                "Call after adding cores to disk or editing config.json."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cleanup_build_logs",
            description=(
                "Delete old build logs to reclaim disk space. "
                "Removes logs older than max_age_days, then trims oldest until under max_total_mb."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_age_days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Delete logs older than this many days",
                    },
                    "max_total_mb": {
                        "type": "integer",
                        "default": 500,
                        "description": "Target max total log size in MB",
                    },
                },
            },
        ),
        Tool(
            name="program_fpga",
            description=(
                "Flash a bitstream to an FPGA board using iceprog (ice40) or openFPGALoader (ecp5/gowin/nexus). "
                "Provide bitstream as base64 (from place_and_route output) or a file path on disk."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["ice40", "ecp5", "nexus", "gowin"],
                        "description": "FPGA family (used to select default programmer)",
                    },
                    "bitstream_b64": {
                        "type": "string",
                        "description": "Base64-encoded bitstream (from place_and_route bitstream_b64 field)",
                    },
                    "bitstream_path": {
                        "type": "string",
                        "description": "Path to bitstream file on disk",
                    },
                    "programmer": {
                        "type": "string",
                        "enum": ["iceprog", "openFPGALoader"],
                        "description": "Programmer tool (auto-detected from target if omitted)",
                    },
                    "board": {
                        "type": "string",
                        "description": "openFPGALoader --board flag (e.g. 'ulx3s', 'tangnano9k')",
                    },
                    "timeout": {
                        "type": "integer",
                        "default": 60,
                        "description": "Timeout in seconds",
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="list_boards",
            description="List all known FPGA board presets with target, device, package, and clock frequency.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


@app.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> CallToolResult:
    try:
        result: dict | list
        match name:
            case "lint_hdl":
                from tools.lint import lint_hdl

                result = await asyncio.to_thread(
                    lint_hdl,
                    code=arguments["code"],
                    language=arguments.get("language", "verilog"),
                    top_module=arguments.get("top_module"),
                    linter=arguments.get("linter", "iverilog"),
                )
            case "lint_project":
                from tools.lint import lint_project

                result = await asyncio.to_thread(
                    lint_project,
                    files=arguments["files"],
                    language=arguments.get("language", "verilog"),
                    top_module=arguments.get("top_module"),
                    timeout=_clamp_timeout(arguments.get("timeout", 60), 60),
                    linter=arguments.get("linter", "iverilog"),
                )
            case "synthesize":
                from tools.synthesize import synthesize

                result = await asyncio.to_thread(
                    synthesize,
                    code=arguments.get("code", ""),
                    top_module=arguments["top_module"],
                    target=arguments.get("target", "generic"),
                    language=arguments.get("language", "verilog"),
                    files=arguments.get("files"),
                    project_dir=arguments.get("project_dir"),
                    backend=arguments.get("backend", "yosys"),
                    litex_board=arguments.get("litex_board"),
                    litex_args=arguments.get("litex_args"),
                    timeout=_clamp_timeout(arguments.get("timeout", 120), 120),
                )
            case "place_and_route":
                from tools.pnr import place_and_route

                result = await asyncio.to_thread(
                    place_and_route,
                    code=arguments.get("code", ""),
                    top_module=arguments["top_module"],
                    target=arguments.get("target", ""),
                    device=arguments.get("device", ""),
                    package=arguments.get("package", ""),
                    constraints=arguments.get("constraints", ""),
                    language=arguments.get("language", "verilog"),
                    files=arguments.get("files"),
                    project_dir=arguments.get("project_dir"),
                    board=arguments.get("board"),
                    nextpnr_args=arguments.get("nextpnr_args"),
                    work_dir=arguments.get("work_dir"),
                    timeout=_clamp_timeout(arguments.get("timeout", 300), 300),
                    backend=arguments.get("backend", "yosys"),
                    litex_board=arguments.get("litex_board"),
                    litex_args=arguments.get("litex_args"),
                    return_bitstream_b64=arguments.get("return_bitstream_b64", False),
                )
            case "simulate":
                from tools.simulate import simulate

                result = await asyncio.to_thread(
                    simulate,
                    code=arguments["code"],
                    testbench=arguments["testbench"],
                    language=arguments.get("language", "verilog"),
                    timeout=_clamp_timeout(arguments.get("timeout", 60), 60),
                    return_vcd=arguments.get("return_vcd", False),
                )
            case "search_github_cores":
                from registry.github import search_repos

                result = await asyncio.to_thread(  # type: ignore[arg-type]
                    search_repos,
                    query=arguments["query"],
                    language=arguments.get("language"),
                    max_results=arguments.get("max_results", 10),
                )
            case "import_github_core":
                result = await asyncio.to_thread(
                    _registry().import_github_core,
                    owner_repo=arguments["repo"],
                    subdir=arguments.get("subdir", ""),
                    ref=arguments.get("ref", ""),
                )
            case "import_fusesoc_core":
                result = await asyncio.to_thread(
                    _registry().import_fusesoc_core, path=arguments["path"]
                )
            case "list_ip_cores":
                result = await asyncio.to_thread(  # type: ignore[arg-type]
                    _registry().list_cores, category=arguments.get("category")
                )
            case "get_ip_core":
                result = await asyncio.to_thread(
                    _registry().get_core, arguments["name"]
                )
            case "generate_ip":
                result = await asyncio.to_thread(
                    _registry().generate_ip,
                    name=arguments["name"],
                    parameters=arguments.get("parameters", {}),
                    instance_name=arguments.get("instance_name"),
                )
            case "get_diagnostics":
                from tools.lsp import get_diagnostics

                result = await asyncio.to_thread(
                    get_diagnostics,
                    code=arguments["code"],
                    language=arguments.get("language", "verilog"),
                )
            case "format_hdl":
                from tools.lsp import format_hdl

                result = await asyncio.to_thread(
                    format_hdl,
                    code=arguments["code"],
                    language=arguments.get("language", "verilog"),
                )
            case "litex_build":
                from tools.litex import litex_build

                result = await asyncio.to_thread(
                    litex_build,
                    board=arguments["board"],
                    args=arguments.get("args"),
                    output_dir=arguments.get("output_dir"),
                    timeout=_clamp_timeout(arguments.get("timeout", 600), 600),
                )
            case "litex_soc":
                from tools.litex import litex_soc

                result = await asyncio.to_thread(
                    litex_soc,
                    board=arguments["board"],
                    args=arguments.get("args"),
                    output_dir=arguments.get("output_dir"),
                    timeout=_clamp_timeout(arguments.get("timeout", 300), 300),
                )
            case "litex_flow":
                from tools.litex import litex_flow

                result = await asyncio.to_thread(
                    litex_flow,
                    board=arguments["board"],
                    args=arguments.get("args"),
                    timeout=_clamp_timeout(arguments.get("timeout", 600), 600),
                )
            case "start_build":
                result = _builds().start(
                    cmd=arguments["cmd"],
                    label=arguments.get("label", ""),
                    work_dir=arguments.get("work_dir"),
                )
            case "build_status":
                result = _builds().status(
                    build_id=arguments["build_id"],
                    tail_lines=arguments.get("tail_lines", 30),
                    parse=arguments.get("parse", True),
                )
            case "list_builds":
                result = _builds().list_builds()
            case "cancel_build":
                result = _builds().cancel(build_id=arguments["build_id"])
            case "check_tools":
                from tools.healthcheck import check_tools

                result = await asyncio.to_thread(check_tools)
            case "reload_registry":
                result = _registry().reload()
            case "cleanup_build_logs":
                from tools.build_manager import BuildManager

                result = BuildManager.cleanup_logs(
                    max_age_days=arguments.get("max_age_days", 7),
                    max_total_mb=arguments.get("max_total_mb", 500),
                )
            case "program_fpga":
                from tools.program import program_fpga

                result = await asyncio.to_thread(
                    program_fpga,
                    target=arguments["target"],
                    bitstream_b64=arguments.get("bitstream_b64", ""),
                    bitstream_path=arguments.get("bitstream_path", ""),
                    programmer=arguments.get("programmer", ""),
                    board=arguments.get("board", ""),
                    timeout=_clamp_timeout(arguments.get("timeout", 60), 60),
                )
            case "list_boards":
                from tools.boards import list_boards

                result = list_boards()
            case _:
                result = {"error": f"Unknown tool: '{name}'"}

        text = json.dumps(result, indent=2)
        is_err = isinstance(result, dict) and "error" in result
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            isError=is_err,
        )

    except KeyError as exc:
        logger.warning("Tool '%s' missing required argument: %s", name, exc)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"Missing required argument: {exc}"}),
                )
            ],
            isError=True,
        )
    except Exception as exc:
        logger.exception("Unhandled error in tool '%s'", name)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"error": str(exc)}))],
            isError=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
