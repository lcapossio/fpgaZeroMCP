# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import json

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from registry.resolver import CoreRegistry
from tools.lint import lint_hdl, lint_project
from tools.litex import litex_build, litex_flow, litex_soc
from tools.lsp import format_hdl, get_diagnostics
from tools.pnr import place_and_route
from tools.simulate import simulate
from tools.synthesize import synthesize
from tools.build_manager import BuildManager

app = Server("fpgaZeroMCP")
registry = CoreRegistry()
builds = BuildManager()

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
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="lint_hdl",
            description=(
                "Lint HDL source code using iverilog/verilator (Verilog/SystemVerilog) or ghdl (VHDL). "
                "Use linter='verilator' to enable -Wall checks including multidriven net detection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code":     {"type": "string", "description": "HDL source code to lint"},
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "top_module": {"type": "string", "description": "Top-level module name (optional)"},
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
        types.Tool(
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
                    "top_module": {"type": "string", "description": "Top-level module name (optional)"},
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
        types.Tool(
            name="synthesize",
            description=(
                "Synthesize Verilog HDL using Yosys or run LiteX backend. "
                "Returns resource statistics and the list of inferred modules. "
                "Supported targets: generic, ice40, ecp5, gowin, xilinx, intel."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code":       {"type": "string", "description": "Verilog source code"},
                    "top_module": {"type": "string", "description": "Name of the top-level module"},
                    "target": {
                        "type": "string",
                        "enum": ["generic", "ice40", "ecp5", "nexus", "gowin", "xilinx", "intel"],
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
                "required": ["code", "top_module"],
            },
        ),
        types.Tool(
            name="place_and_route",
            description=(
                "Synthesize Verilog with Yosys then place-and-route with nextpnr in one step. "
                "If backend=litex, runs LiteX build and ignores Verilog inputs. "
                "Returns max frequency, critical path, resource utilization, and full logs. "
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
                    "code":        {"type": "string", "description": "Verilog source code"},
                    "top_module":  {"type": "string", "description": "Top-level module name"},
                    "target":      {
                        "type": "string",
                        "enum": ["ice40", "ecp5", "nexus", "gowin"],
                        "description": "FPGA family",
                    },
                    "device":      {"type": "string", "description": "Device variant, e.g. 'hx1k', '25k', 'LIFCL-40-9BG400C'"},
                    "package":     {"type": "string", "description": "Package, e.g. 'tq144', 'CABGA256' (not needed for nexus/gowin)"},
                    "constraints": {"type": "string", "description": "Optional pin constraints (PCF/LPF/PDC/CST text)"},
                    "timeout":     {"type": "integer", "default": 300, "description": "PnR timeout in seconds"},
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
                },
                "required": ["code", "top_module", "target", "device"],
            },
        ),
        types.Tool(
            name="simulate",
            description=(
                "Compile and simulate HDL using Icarus Verilog (iverilog + vvp) or GHDL (VHDL). "
                "Provide the design source and a separate testbench. "
                "Returns all $display/$monitor output (Verilog) or report output (VHDL) and any runtime errors."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code":      {"type": "string", "description": "HDL design source"},
                    "testbench": {"type": "string", "description": "HDL testbench source"},
                    "language": {
                        "type": "string",
                        "enum": ["verilog", "systemverilog", "vhdl"],
                        "default": "verilog",
                        "description": "HDL language variant",
                    },
                    "timeout":   {"type": "integer", "default": 60, "description": "Timeout in seconds"},
                },
                "required": ["code", "testbench"],
            },
        ),
        types.Tool(
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
        types.Tool(
            name="get_ip_core",
            description="Fetch the full manifest and HDL source files for a named IP core.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Core name, e.g. 'uart_tx' or 'fifo'"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
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
        types.Tool(
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
        types.Tool(
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
        types.Tool(
            name="generate_ip",
            description=(
                "Generate a parameterized instance of an IP core. "
                "Returns the HDL source files and a ready-to-paste Verilog instantiation snippet "
                "with the requested parameter values applied."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Core name, e.g. 'uart_tx' or 'fifo'"},
                    "parameters": {
                        "type": "object",
                        "description": "Parameter overrides, e.g. {\"CLKS_PER_BIT\": 434}",
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
        types.Tool(
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
        types.Tool(
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
                    "code": {"type": "string", "description": "HDL source code to format"},
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
        types.Tool(
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
        types.Tool(
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
        types.Tool(
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
        types.Tool(
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
        types.Tool(
            name="build_status",
            description=(
                "Check the progress of a background build. "
                "Returns status (running/success/failed), elapsed time, and recent log output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "build_id": {"type": "string", "description": "Build ID returned by start_build"},
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
        types.Tool(
            name="list_builds",
            description="List all tracked builds (running and finished) with status summary.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
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
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> types.CallToolResult:
    try:
        match name:
            case "lint_hdl":
                result = await asyncio.to_thread(
                    lint_hdl,
                    code=arguments["code"],
                    language=arguments.get("language", "verilog"),
                    top_module=arguments.get("top_module"),
                )
            case "lint_project":
                result = await asyncio.to_thread(
                    lint_project,
                    files=arguments["files"],
                    language=arguments.get("language", "verilog"),
                    top_module=arguments.get("top_module"),
                    timeout=_clamp_timeout(arguments.get("timeout", 60), 60),
                )
            case "synthesize":
                result = await asyncio.to_thread(
                    synthesize,
                    code=arguments["code"],
                    top_module=arguments["top_module"],
                    target=arguments.get("target", "generic"),
                    backend=arguments.get("backend", "yosys"),
                    litex_board=arguments.get("litex_board"),
                    litex_args=arguments.get("litex_args"),
                    timeout=_clamp_timeout(arguments.get("timeout", 120), 120),
                )
            case "place_and_route":
                result = await asyncio.to_thread(
                    place_and_route,
                    code=arguments["code"],
                    top_module=arguments["top_module"],
                    target=arguments["target"],
                    device=arguments["device"],
                    package=arguments.get("package", ""),
                    constraints=arguments.get("constraints", ""),
                    timeout=_clamp_timeout(arguments.get("timeout", 300), 300),
                    backend=arguments.get("backend", "yosys"),
                    litex_board=arguments.get("litex_board"),
                    litex_args=arguments.get("litex_args"),
                )
            case "simulate":
                result = await asyncio.to_thread(
                    simulate,
                    code=arguments["code"],
                    testbench=arguments["testbench"],
                    language=arguments.get("language", "verilog"),
                    timeout=_clamp_timeout(arguments.get("timeout", 60), 60),
                )
            case "search_github_cores":
                from registry.github import search_repos
                result = await asyncio.to_thread(
                    search_repos,
                    query=arguments["query"],
                    language=arguments.get("language"),
                    max_results=arguments.get("max_results", 10),
                )
            case "import_github_core":
                result = await asyncio.to_thread(
                    registry.import_github_core,
                    owner_repo=arguments["repo"],
                    subdir=arguments.get("subdir", ""),
                    ref=arguments.get("ref", ""),
                )
            case "import_fusesoc_core":
                result = await asyncio.to_thread(
                    registry.import_fusesoc_core, path=arguments["path"]
                )
            case "list_ip_cores":
                result = await asyncio.to_thread(
                    registry.list_cores, category=arguments.get("category")
                )
            case "get_ip_core":
                result = await asyncio.to_thread(
                    registry.get_core, arguments["name"]
                )
            case "generate_ip":
                result = await asyncio.to_thread(
                    registry.generate_ip,
                    name=arguments["name"],
                    parameters=arguments.get("parameters", {}),
                    instance_name=arguments.get("instance_name"),
                )
            case "get_diagnostics":
                result = await asyncio.to_thread(
                    get_diagnostics,
                    code=arguments["code"],
                    language=arguments.get("language", "verilog"),
                )
            case "format_hdl":
                result = await asyncio.to_thread(
                    format_hdl,
                    code=arguments["code"],
                    language=arguments.get("language", "verilog"),
                )
            case "litex_build":
                result = await asyncio.to_thread(
                    litex_build,
                    board=arguments["board"],
                    args=arguments.get("args"),
                    output_dir=arguments.get("output_dir"),
                    timeout=_clamp_timeout(arguments.get("timeout", 600), 600),
                )
            case "litex_soc":
                result = await asyncio.to_thread(
                    litex_soc,
                    board=arguments["board"],
                    args=arguments.get("args"),
                    output_dir=arguments.get("output_dir"),
                    timeout=_clamp_timeout(arguments.get("timeout", 300), 300),
                )
            case "litex_flow":
                result = await asyncio.to_thread(
                    litex_flow,
                    board=arguments["board"],
                    args=arguments.get("args"),
                    timeout=_clamp_timeout(arguments.get("timeout", 600), 600),
                )
            case "start_build":
                result = builds.start(
                    cmd=arguments["cmd"],
                    label=arguments.get("label", ""),
                    work_dir=arguments.get("work_dir"),
                )
            case "build_status":
                result = builds.status(
                    build_id=arguments["build_id"],
                    tail_lines=arguments.get("tail_lines", 30),
                    parse=arguments.get("parse", True),
                )
            case "list_builds":
                result = builds.list_builds()
            case "cancel_build":
                result = builds.cancel(build_id=arguments["build_id"])
            case _:
                result = {"error": f"Unknown tool: '{name}'"}

        text = json.dumps(result, indent=2)
        is_err = isinstance(result, dict) and "error" in result
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=is_err,
        )

    except KeyError as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"error": f"Missing required argument: {exc}"}))],
            isError=True,
        )
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"error": str(exc)}))],
            isError=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
