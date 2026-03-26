# Contributing to fpgaZeroMCP

## What belongs in this repo

This repo is the **MCP server infrastructure** — not an IP core library.

| Belongs here | Does not belong here |
|---|---|
| New MCP tools (`tools/`) | New IP cores |
| Registry engine improvements (`registry/`) | HDL files for common peripherals |
| New synthesis targets | Testbenches for third-party cores |
| FuseSoC / GitHub import improvements | Anything that duplicates what's on GitHub |
| Bug fixes | |
| Documentation | |

**To share an IP core with the community:** publish it on GitHub with the `fpga` topic and an MIT (or compatible) license. Users can then pull it in with `import_github_core`. No PR needed here.

---

## Contributing a tool or registry improvement

### Setup

```bash
git clone https://github.com/lcapossio/fpgaZeroMCP
cd fpgaZeroMCP
pip install -e .
```

Optional but recommended for testing tool wrappers:
- [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases) — iverilog, yosys, nextpnr

### Running the demo

```bash
python example.py
```

### Project structure

```
server.py          MCP server entry point — tool definitions + dispatch
tools/
  lint.py          iverilog / ghdl wrapper
  synthesize.py    Yosys wrapper
  simulate.py      iverilog + vvp wrapper
registry/
  manifest.py      Pydantic schema for core.json
  resolver.py      Loads cores/, serves list/get/generate/import
  github.py        GitHub search + download
  fusesoc.py       FuseSoC CAPI2 parser
cores/
  uart_tx/         Reference core (demonstrates core.json format)
  fifo/            Reference core
```

### Adding a new synthesis target

1. Find the Yosys command name — e.g. `synth_ecp5`, `synth_nexus`
2. Add it to `SYNTH_CMDS` in [tools/synthesize.py](tools/synthesize.py)
3. Add it to the `enum` in the `synthesize` tool schema in [server.py](server.py)
4. Add a row to the synthesis targets table in [README.md](README.md)

### Adding a new MCP tool

1. Implement the logic in `tools/` or `registry/`
2. Add a `types.Tool(...)` entry in `handle_list_tools()` in [server.py](server.py)
3. Add a `case "tool_name":` in `handle_call_tool()` in [server.py](server.py)

### Pull request checklist

- [ ] `python example.py` runs without errors
- [ ] New tools have a JSON schema (`inputSchema`) with descriptions on all fields
- [ ] Required arguments are listed in `"required": [...]`
- [ ] Tool wrappers return `{"error": "..."}` when the underlying binary is missing — never raise unhandled exceptions
- [ ] README updated if the change is user-visible

---

## Sharing an IP core (without opening a PR)

If you have an HDL module you want the community to use:

1. Create a public GitHub repo
2. Add the topic **`fpga`** to the repo (Settings → Topics)
3. Set the license to **MIT** (or Apache-2.0, BSD-2-Clause)
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
