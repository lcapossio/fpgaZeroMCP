# fpgaZeroMCP

[![CI](https://github.com/lcapossio/fpgaZeroMCP/actions/workflows/ci.yml/badge.svg)](https://github.com/lcapossio/fpgaZeroMCP/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![MCP](https://img.shields.io/badge/MCP-compatible-green)

An open-source [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI assistants a complete FPGA toolchain — lint, simulate, synthesize, place-and-route, program the bitstream, and a live IP core registry backed by GitHub.

Ask your AI to search for cores, pull them in, lint HDL, synthesize a multi-file VHDL or Verilog project from disk, run a simulation, then flash the bitstream to your board — all without leaving your chat window.

## Features

- **Multi-language**: Verilog, SystemVerilog, and VHDL (via ghdl-yosys-plugin)
- **Three input modes**: inline `code` string, multi-file `files` dict, or `project_dir` path on disk
- **Filelist support**: `files.f`/`sources.f` with `+incdir+`, `+define+`, and nested `-f` directives
- **Board presets**: 11 built-in boards (iCEBreaker, ULX3S, TinyFPGA BX, Tang Nano, etc.) — sets target/device/package/clock automatically
- **Constraint auto-detection**: finds `.pcf`/`.lpf`/`.pdc`/`.cst` in your project directory
- **Bitstream programming**: flash via `iceprog` (iCE40) or `openFPGALoader` (ECP5/Gowin/Nexus/Xilinx)
- **Vivado**: batch runs through `start_build`, Xilinx builds through LiteX, structured Vivado log parsing in `build_status`
- **Simulation verdict parsing**: PASS/FAIL/UVM pattern detection with VCD signal summary
- **Background builds**: long-running synthesis/PnR with status polling and a strict EDA-only command allowlist
- **Concurrent requests**: ping, build status, and cancel are answered while a slow tool call is still running; `notifications/cancelled` aborts an in-flight call
- **Machine-readable results**: `structuredContent` on tool results (MCP 2025-06-18) and a uniform `error_code` taxonomy for retry/fallback decisions
- **IP core registry**: live search and import from GitHub with FuseSoC CAPI2 metadata
- **Health check**: discover which OSS CAD Suite tools are installed and reachable

---

## Table of Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [MCP Client Setup](#mcp-client-setup)
- [Tools](#tools)
- [IP Core Registry](#ip-core-registry)
- [Synthesis Targets](#synthesis-targets)
- [LiteX](#litex)
- [Local Core Repositories](#local-core-repositories)
- [Testing](#testing)
- [Environment Variables](#environment-variables)
- [Standalone / Scripting](#standalone--scripting)
- [core.json Schema](#corejson-schema)
- [License](#license)

---

## How it works

```
Your AI assistant  <-->  fpgaZeroMCP (stdio MCP server)  <-->  OSS tools
                                    |
                           cores/   registry on GitHub
                           (uart_tx, fifo + any imported)
```

The MCP server runs as a local subprocess. Your AI calls tools on it over JSON-RPC (stdio). The server shells out to Yosys, nextpnr, iverilog, Verilator, and others from OSS CAD Suite — and can pull open-source FPGA cores directly from GitHub.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | |
| [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) | Bundles iverilog, Yosys, nextpnr, Verilator, Verible, GHDL in one download |
| [LiteX](https://github.com/enjoy-digital/litex) + [litex-boards](https://github.com/litex-hub/litex-boards) | Optional — only needed for LiteX tools |

Add OSS CAD Suite to your `PATH` after installing. All tool wrappers degrade gracefully if a tool is missing.

### GitHub API access

GitHub API requests are unauthenticated by default and subject to rate limits. Set a personal access token to increase limits:

```bash
# Linux/macOS
export GITHUB_TOKEN=ghp_...
```

```powershell
# Windows (PowerShell)
$env:GITHUB_TOKEN = "ghp_..."
```

---

## Installation

```bash
git clone https://github.com/lcapossio/fpgaZeroMCP
cd fpgaZeroMCP
pip install -e .
```

---

## MCP Client Setup

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fpgaZeroMCP": {
      "command": "python",
      "args": ["/path/to/fpgaZeroMCP/server.py"],
      "env": { "PYTHONPATH": "/path/to/fpgaZeroMCP" }
    }
  }
}
```

### VS Code (GitHub Copilot)

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "fpgaZeroMCP": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/fpgaZeroMCP/server.py"],
      "env": { "PYTHONPATH": "/path/to/fpgaZeroMCP" }
    }
  }
}
```

### Cursor / Windsurf

Add to your MCP settings (Settings → MCP Servers):

```json
{
  "fpgaZeroMCP": {
    "command": "python",
    "args": ["/path/to/fpgaZeroMCP/server.py"],
    "env": { "PYTHONPATH": "/path/to/fpgaZeroMCP" }
  }
}
```

### Example prompts

- *"Find me an I2C master core and import it."*
- *"Synthesize the VHDL files in ~/projects/my_fpga and tell me the LUT count."*
- *"PnR my project for the iCEBreaker board, then flash it."*
- *"Run place-and-route with seed 42 to try for better timing."*
- *"Lint this Verilog and fix any errors."*
- *"Simulate this FIFO and tell me whether the testbench passed."*
- *"Format this SystemVerilog file."*
- *"Which OSS CAD Suite tools do I have installed?"*

---

## Tools

### HDL quality

| Tool | Description |
|---|---|
| `lint_hdl` | Syntax/error check via iverilog (V/SV) or GHDL (VHDL) — single file |
| `lint_project` | Lint multiple files together so cross-module references resolve |
| `get_diagnostics` | Structured per-line diagnostics — Verilator → verible fallback (V/SV), GHDL (VHDL) |
| `format_hdl` | Auto-format via verible-verilog-format (V/SV) or vsg (VHDL) |

### Design flow

| Tool | Description |
|---|---|
| `simulate` | Compile and run testbenches — iverilog (V/SV) or GHDL (VHDL). Returns verdict + VCD summary |
| `synthesize` | Yosys synthesis with resource stats. Accepts `code`, `files`, or `project_dir`. Verilog, SV, VHDL |
| `place_and_route` | Yosys + nextpnr in one step. Board presets, constraint auto-detection, bitstream written to disk (`bitstream_path`) |
| `program_fpga` | Flash a bitstream via `iceprog` or `openFPGALoader` |
| `list_boards` | Enumerate built-in board presets (target/device/package/clock) |

### IP core registry

| Tool | Description |
|---|---|
| `list_ip_cores` | Browse the local registry, filter by category |
| `get_ip_core` | Fetch manifest and HDL source for a core |
| `generate_ip` | Get a parameterized instantiation snippet + source files |
| `search_github_cores` | Search GitHub for MIT-licensed FPGA IP repos |
| `import_github_core` | Download a GitHub repo into the local registry |
| `import_fusesoc_core` | Import a local FuseSoC CAPI2 `.core` file |

### LiteX

| Tool | Description |
|---|---|
| `litex_build` | Run a LiteX board target with `--build` |
| `litex_soc` | Generate a LiteX SoC without building gateware |
| `litex_flow` | Run a LiteX board target with fully custom args |

### Build management

| Tool | Description |
|---|---|
| `start_build` | Start a long-running command in the background (allowlisted EDA tools only) |
| `build_status` | Check progress — status, elapsed time, parsed phase/utilization/timing |
| `list_builds` | List all tracked builds (running and finished) |
| `cancel_build` | Kill a running background build |
| `cleanup_build_logs` | Delete old build logs by age and total size |

### Server / registry

| Tool | Description |
|---|---|
| `check_tools` | Report which OSS CAD Suite tools are installed, with paths and versions |
| `reload_registry` | Re-scan core directories without restarting the server |

---

## IP Core Registry

Cores live in `cores/<name>/` — a `core.json` manifest and one or more HDL files. The server auto-discovers them on startup and reloads after any import.

Two reference cores are included (`uart_tx`, `fifo`) to demonstrate the format. **The registry is not meant to grow here** — it is powered by GitHub.

### Getting cores at runtime

```python
# Find a RISC-V softcore
search_github_cores("riscv softcore", language="verilog")

# Pull it in
import_github_core("YosysHQ/picorv32")

# It is now in the local registry
get_ip_core("picorv32")
generate_ip("picorv32", {"COMPRESSED_ISA": 1})
```

The server automatically uses FuseSoC CAPI2 metadata (`.core` files) when found in the repo, giving richer parameter and port information. Only repos with an [allowed license](#allowed-licenses) are accepted.

### Contributing a core

**Do not open PRs adding cores to this repo.** Instead:

1. Publish your HDL repo on GitHub with the `fpga` topic and an MIT license
2. Optionally add a FuseSoC CAPI2 `.core` file for richer metadata
3. Anyone can then `import_github_core("you/your-core")` directly

This keeps the server lean and lets the community grow organically on GitHub.

---

## Synthesis Targets

| Target | Vendor / Family | Full OSS P&R |
|---|---|---|
| `ice40` | Lattice iCE40 | yes — nextpnr-ice40 |
| `ecp5` | Lattice ECP5 | yes — nextpnr-ecp5 |
| `nexus` | Lattice Nexus (CrossLink-NX, CertusPro-NX) | yes — nextpnr-nexus |
| `gowin` | Gowin | yes — nextpnr-gowin |
| `xilinx` | Xilinx / AMD | Synth only |
| `intel` | Intel / Altera | Synth only |
| `generic` | Technology-independent | Netlist only |

Common device/package values for `place_and_route`:

| Target | device | package |
|---|---|---|
| ice40 | `hx1k` `hx8k` `up5k` `lp1k` | `tq144` `qn84` `sg48` `cm81` |
| ecp5 | `25k` `45k` `85k` | `CABGA256` `CABGA381` |
| nexus | `LIFCL-40-9BG400C` | *(embedded in device string)* |
| gowin | `GW1N-UV4LQ144C6/I5` | *(embedded in device string)* |

---

## LiteX

[LiteX](https://github.com/enjoy-digital/litex) is a Python SoC framework that can target many FPGA boards. fpgaZeroMCP exposes three dedicated LiteX tools and also accepts `backend="litex"` in `synthesize` and `place_and_route`.

```python
# Dedicated tools
litex_build(board="arty", args=["--build"])
litex_soc(board="arty", args=["--no-compile"])
litex_flow(board="arty", args=["--build", "--output-dir", "build_arty"])

# As a backend in existing flow tools
synthesize(code="...", top_module="top", backend="litex", litex_board="arty")
place_and_route(code="...", top_module="top", target="ice40", device="hx1k",
                backend="litex", litex_board="arty", litex_args=["--build"])
```

---

## Local Core Repositories

You can point the registry at your own local HDL directories in two ways:

**Environment variable:**

Linux/macOS (colon-separated):
```bash
export USERCORES_PATH=/home/you/my-cores:/home/you/work-cores
```

Windows (semicolon-separated, PowerShell):
```powershell
$env:USERCORES_PATH = "C:\Users\you\my-cores;C:\Users\you\work-cores"
```

**Config file** (`~/.fpgazero_mcp/config.json`):
```json
{
  "core_paths": [
    "/home/you/my-cores",
    "/home/you/work-cores"
  ]
}
```

All paths are scanned on startup alongside the built-in `cores/` directory.

### Allowed licenses

By default, `import_github_core` accepts repos with any of these SPDX licenses:

```
MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, GPL-2.0, GPL-3.0, LGPL-2.1, LGPL-3.0
```

Override with the `FPGAZERO_ALLOWED_LICENSES` environment variable (comma-separated SPDX IDs):

```bash
# Linux/macOS
export FPGAZERO_ALLOWED_LICENSES=MIT
export FPGAZERO_ALLOWED_LICENSES=MIT,Apache-2.0
```

```powershell
# Windows (PowerShell)
$env:FPGAZERO_ALLOWED_LICENSES = "MIT"
$env:FPGAZERO_ALLOWED_LICENSES = "MIT,Apache-2.0"
```

License IDs follow [SPDX notation](https://spdx.org/licenses/). The check is done at import time; `search_github_cores` returns results regardless of license so you can evaluate before importing.

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Some tests require OSS CAD Suite tools on PATH. Tests that need missing tools are skipped automatically.

---

## Reducing Memory Footprint

The server runs as a 1-process-per-session subprocess under stdio transport (this is how MCP clients like Claude Desktop launch it). Each session takes ~60-90 MB RSS idle on Linux, mostly from the Python interpreter and dependencies.

If you run many concurrent MCP sessions, set these environment variables before launching your MCP client:

```bash
# Linux — reduces glibc malloc arena fragmentation (can save 10-20 MB per session)
export MALLOC_ARENA_MAX=2

# Strip bytecode position annotations from tracebacks (saves a few MB)
export PYTHONNODEBUGRANGES=1

# Skip .pyc cache files (no memory impact, avoids disk writes)
export PYTHONDONTWRITEBYTECODE=1
```

These are zero-code changes and fully transparent.

---

## Environment Variables

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | GitHub personal access token — raises API rate limits |
| `USERCORES_PATH` | Extra core search directories (OS path separator delimited) |
| `FPGAZERO_ALLOWED_LICENSES` | Comma-separated SPDX IDs for `import_github_core` (default: `MIT,BSD-2-Clause,BSD-3-Clause,Apache-2.0,ISC,GPL-2.0,GPL-3.0,LGPL-2.1,LGPL-3.0`) |
| `FPGAZERO_TMPDIR` | Override temporary workspace root directory |
| `FPGAZERO_DATA_DIR` | Root for persistent server artifacts — build logs, LiteX output, temp workspaces (default: `<install dir>/no_commit`) |
| `FPGAZERO_ALLOWED_DIRS` | OS pathsep-separated list of extra directories that `project_dir` may read from and `start_build`/`place_and_route` may use as `work_dir` (in addition to cwd and `$HOME`) |

---

## Standalone / Scripting

The Python API can be used directly without an MCP client:

```python
from registry.resolver import CoreRegistry
from tools.lint import lint_hdl

reg = CoreRegistry()

# Import a core from GitHub
reg.import_github_core("ben-marshall/uart")

# Generate a parameterized instantiation
result = reg.generate_ip("uart", {"CLKS_PER_BIT": 868})
print(result["instantiation"])

# Lint some HDL
lint_hdl(open("my_design.v").read())
```

```bash
python example.py   # runs the built-in demo
```

---

## core.json Schema

```json
{
  "name": "my_core",
  "version": "1.0.0",
  "description": "...",
  "author": "you",
  "license": "MIT",
  "language": "verilog",
  "category": "communication",
  "tags": ["spi", "serial"],
  "parameters": {
    "DATA_WIDTH": { "type": "integer", "default": 8, "description": "..." }
  },
  "ports": {
    "clk": { "direction": "input", "width": 1, "description": "System clock" }
  },
  "files": ["my_core.v"]
}
```

---

## Author

**Leonardo Capossio** (bard0) — [hello@bard0.com](mailto:hello@bard0.com)

## License

MIT — see [LICENSE](LICENSE).
