# synthesize -- Yosys Synthesis

Synthesize HDL designs (Verilog, SystemVerilog, VHDL) using Yosys. Returns resource statistics and inferred module names.

## Index

- [MCP Tool](#mcp-tool)
- [Source Input Modes](#source-input-modes)
- [Language Support](#language-support)
- [Backends](#backends)
- [Supported Targets](#supported-targets)
- [Parameters](#parameters)
- [Filelist Support](#filelist-support)
- [Include Path Resolution](#include-path-resolution)
- [Response](#response)
- [Top Module Validation](#top-module-validation)
- [Usage](#usage)

## MCP Tool

`synthesize`

## Source Input Modes

Provide exactly one:

| Mode | Parameter | Description |
|---|---|---|
| Single file | `code` | HDL source as a string |
| Multi-file | `files` | Dict of `filename -> source code` |
| Disk project | `project_dir` | Path to directory containing HDL files |

`project_dir` is restricted to directories under the server's working directory, `$HOME`, or paths listed in `FPGAZERO_ALLOWED_DIRS`.

## Language Support

| Language | Yosys Read Command | Notes |
|---|---|---|
| `verilog` | `read_verilog file.v` | Default |
| `systemverilog` | `read_verilog -sv file.sv` | Yosys SV subset |
| `vhdl` | `ghdl --std=08 files... -e top` | Requires ghdl-yosys-plugin (in OSS CAD Suite). Entity names are lowercased during import. |

## Backends

| Backend | Description |
|---|---|
| `yosys` | Open-source synthesis via Yosys (default) |
| `litex` | Delegates to LiteX flow (ignores code/top_module) |

## Supported Targets

| Target | Yosys Command | Vendor |
|---|---|---|
| `generic` | `synth` | Technology-independent |
| `ice40` | `synth_ice40` | Lattice iCE40 |
| `ecp5` | `synth_ecp5` | Lattice ECP5 |
| `nexus` | `synth_nexus` | Lattice Nexus |
| `gowin` | `synth_gowin` | Gowin |
| `xilinx` | `synth_xilinx` | Xilinx / AMD |
| `intel` | `synth_intel` | Intel / Altera |

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | `""` | HDL source code (single-file mode) |
| `files` | object | `null` | Multi-file mode: `{"filename": "source code", ...}` |
| `project_dir` | string | `null` | Disk mode: path to directory with HDL files |
| `top_module` | string | *(required)* | Top-level module name |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |
| `target` | string | `"generic"` | FPGA family / synthesis target |
| `backend` | string | `"yosys"` | `yosys` or `litex` |
| `litex_board` | string | `null` | LiteX board target (required if backend=litex) |
| `litex_args` | array | `null` | Extra LiteX CLI args |
| `timeout` | integer | `120` | Timeout in seconds (clamped to 1-3600) |

## Filelist Support

When using `project_dir`, if a `files.f` or `sources.f` file exists in the project root, it is parsed instead of globbing. Supported directives:

```
# Comment lines (# or //)
rtl/top.v
rtl/sub.v
+incdir+rtl/include
+define+SYNTHESIS
-f nested_filelist.f
```

This preserves compile order and supports include paths and defines.

## Include Path Resolution

For Verilog/SystemVerilog, include paths are auto-detected:
- `project_dir` root is always added as `-I`
- Subdirectories containing `.vh` or `.svh` files are added automatically
- `+incdir+` directives from filelists are included
- `+define+` directives are passed as `-D` flags to `read_verilog`

## Response

```json
{
  "success": true,
  "target": "ice40",
  "language": "verilog",
  "top_module": "top",
  "modules": ["top", "sub"],
  "stdout": "...",
  "stderr": "",
  "files": ["top.v", "sub.v"],
  "project_dir": "/path/to/project",
  "source_files": ["rtl/top.v", "rtl/sub.v"]
}
```

`files` and `project_dir`/`source_files` are only present in the respective input modes.

## Top Module Validation

The `top_module` name must be a valid Verilog identifier:
- Starts with a letter, underscore, or `$`
- Contains only letters, digits, underscores, or `$`
- Cannot be empty or contain spaces

## Usage

### MCP (via AI assistant)

> "Synthesize this design for iCE40 and tell me the LUT count."

> "Synthesize the VHDL files in ~/projects/my_fpga for ECP5."

### Python

```python
from tools.synthesize import synthesize

# Single file
result = synthesize(code="module top(...); endmodule", top_module="top")

# Multi-file
result = synthesize(
    files={"top.v": "...", "sub.v": "..."},
    top_module="top",
    target="ice40",
)

# Project on disk
result = synthesize(
    project_dir="/home/user/my_fpga/",
    top_module="top",
    language="systemverilog",
    target="ecp5",
)

# VHDL
result = synthesize(
    code=open("design.vhd").read(),
    top_module="my_entity",
    language="vhdl",
    target="generic",
)
```
