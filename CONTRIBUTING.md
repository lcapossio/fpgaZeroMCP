# Contributing to fpgaZeroMCP

## What belongs in this repo

This repo is the **MCP server infrastructure** — not an IP core library.

| Belongs here | Does not belong here |
|---|---|
| New MCP tools (`src/fpgazero_mcp/tools/`) | New IP cores |
| Registry engine improvements (`src/fpgazero_mcp/registry/`) | HDL files for common peripherals |
| New synthesis targets | Testbenches for third-party cores |
| FuseSoC / GitHub import improvements | Anything that duplicates what's on GitHub |
| Bug fixes | |
| Documentation | |

**To share an IP core with the community:** publish it on GitHub with the `fpga` topic and a compatible open-source license. Users can then pull it in with `import_github_core`. No PR needed here.

---

## Contributing a tool or registry improvement

### Setup

```bash
git clone https://github.com/lcapossio/fpgaZeroMCP
cd fpgaZeroMCP
pip install -e ".[dev]"
```

Optional but recommended for testing tool wrappers:
- [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) — iverilog, yosys, nextpnr, verilator, ghdl

### Running the demo

```bash
python example.py
```

### Running tests

```bash
# Unit tests (no EDA tools required)
pytest tests/ -v -m "not integration"

# Integration tests (requires OSS CAD Suite on PATH)
pytest tests/ -v -m "integration"

# All tests
pytest tests/ -v
```

### Project structure

```
src/fpgazero_mcp/      The one installed package — nothing else goes on sys.path
  server.py            MCP server entry point — tool definitions + dispatch
  mcp_lite.py          Minimal stdio JSON-RPC MCP implementation
  tools/
    sources.py         Shared source resolution, path validation, Yosys read cmds
    lint.py            iverilog / ghdl linting (single file and multi-file)
    synthesize.py      Yosys synthesis wrapper
    simulate.py        iverilog + vvp / GHDL simulation
    formal.py          SymbiYosys formal verification (bmc / prove / cover)
    pnr.py             Yosys + nextpnr place-and-route pipeline
    timing.py          Timing-closure sweep over seeds / Vivado directives
    vivado.py          Vivado batch place-and-route backend
    lsp.py             Verilator / Verible / GHDL diagnostics + formatting
    litex.py           LiteX SoC framework wrappers
    build_manager.py   Background build management (start/status/cancel)
    build_parser.py    Single-pass build log parser (Yosys/nextpnr/Vivado/Quartus/GHDL)
    workspace.py       Temporary workspace context manager
    proctree.py        Run/terminate a tool's whole process tree (timeouts, cancel)
    *.md               Per-tool documentation
  registry/
    manifest.py        Dataclass schema and validation for core.json
    resolver.py        Loads cores/, serves list/get/generate/import
    github.py          GitHub search + download with retry logic
    fusesoc.py         FuseSoC CAPI2 parser
  paths.py             Writable locations for runtime state (never inside the package)
  cores/
    uart_tx/           Reference core (demonstrates core.json format)
    fifo/              Reference core
tests/
  test_error_paths.py  Unit tests for error handling, validation, parsers
  test_integration_tools.py  Integration tests (require EDA tools on PATH)
  test_registry_basic.py     Registry load/list/get/generate tests
  test_server_dispatch.py    MCP dispatch tests
  test_github_import.py      GitHub import license/path tests
  test_formal.py             Formal verification: sby log parsing + .sby generation
  test_timing_closure.py     Timing-closure sweep: attempt planning, best-of-N, reuse
  test_paths.py              Runtime state stays out of the installed package
  test_real_logs.py          build_parser against real captured tool logs
  test_review_fixes.py       Regressions from the v0.8 pre-merge review
  fixtures/logs/             Real tool output (hostnames and paths scrubbed)
```

When a parser misreads real tool output, add the log (or the relevant
excerpt) to `tests/fixtures/logs/` with the tool version in the file name,
and assert the values a person reading the log would expect. Replace user
paths and hostnames before committing it.

### Adding a new synthesis target

1. Find the Yosys command name — e.g. `synth_ecp5`, `synth_nexus`
2. Add it to `SYNTH_CMDS` in [synthesize.py](src/fpgazero_mcp/tools/synthesize.py)
3. Add it to the `enum` in the `synthesize` tool schema in [server.py](src/fpgazero_mcp/server.py)
4. Add a row to the synthesis targets table in [README.md](README.md)

### Adding a new MCP tool

1. Implement the logic in `src/fpgazero_mcp/tools/` or `src/fpgazero_mcp/registry/`
2. Add a `types.Tool(...)` entry in `handle_list_tools()` in [server.py](src/fpgazero_mcp/server.py)
3. Add a `case "tool_name":` in `handle_call_tool()` in [server.py](src/fpgazero_mcp/server.py)
4. Add documentation in `src/fpgazero_mcp/tools/<name>.md`
5. Add tests in `tests/`

### Pull request checklist

- [ ] `python example.py` runs without errors
- [ ] `pytest tests/ -v -m "not integration"` passes
- [ ] New tools have a JSON schema (`inputSchema`) with descriptions on all fields
- [ ] Required arguments are listed in `"required": [...]`
- [ ] Tool wrappers return `{"error": "..."}` when the underlying binary is missing — never raise unhandled exceptions
- [ ] README updated if the change is user-visible
- [ ] Tool documentation added/updated in `src/fpgazero_mcp/tools/*.md`

---

## Sharing an IP core (without opening a PR)

If you have an HDL module you want the community to use:

1. Create a public GitHub repo
2. Add the topic **`fpga`** to the repo (Settings > Topics)
3. Set the license to **MIT**, **BSD-2-Clause**, **Apache-2.0**, or another [compatible license](#allowed-licenses)
4. Optionally add a FuseSoC CAPI2 `.core` file — this gives users richer parameter and port metadata automatically

Once done, anyone running fpgaZeroMCP can pull your core in with:

```
import_github_core("you/your-repo")
```

### FuseSoC .core file (optional but recommended)

```yaml
CAPI=2:
name: ::my_uart:1.0.0
description: My UART transmitter

filesets:
  rtl:
    files:
      - rtl/my_uart.v
    file_type: verilogSource

parameters:
  CLKS_PER_BIT:
    datatype: int
    default: 868
    description: Clock cycles per UART bit (clk_hz / baud_rate)

targets:
  default:
    filesets: [rtl]
```

---

## Code style

- Python 3.11+, no external deps beyond what's in `pyproject.toml`
- Type hints on all public functions
- Tool wrappers must be side-effect free except for filesystem writes inside temp dirs
- `registry/` must never import from `tools/` (one-way dependency)
- Shared helpers between backends belong in `tools/sources.py`, not imported privately out of a sibling module
