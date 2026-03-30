# synthesize -- Yosys Synthesis

Synthesize Verilog designs using Yosys. Returns resource statistics and inferred module names.

## Index

- [MCP Tool](#mcp-tool)
- [Backends](#backends)
- [Supported Targets](#supported-targets)
- [Parameters](#parameters)
- [Response](#response)
- [Top Module Validation](#top-module-validation)
- [Usage](#usage)

## MCP Tool

`synthesize`

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
| `code` | string | *(required)* | Verilog source code |
| `top_module` | string | *(required)* | Top-level module name (validated as a legal Verilog identifier) |
| `target` | string | `"generic"` | FPGA family / synthesis target |
| `backend` | string | `"yosys"` | `yosys` or `litex` |
| `litex_board` | string | `null` | LiteX board target (required if backend=litex) |
| `litex_args` | array | `null` | Extra LiteX CLI args |
| `timeout` | integer | `120` | Timeout in seconds (clamped to 1-3600) |

## Response

```json
{
  "success": true,
  "target": "ice40",
  "top_module": "top",
  "modules": ["top", "sub"],
  "stdout": "...",
  "stderr": ""
}
```

## Top Module Validation

The `top_module` name must be a valid Verilog identifier:
- Starts with a letter, underscore, or `$`
- Contains only letters, digits, underscores, or `$`
- Cannot be empty or contain spaces

## Usage

### MCP (via AI assistant)

> "Synthesize this design for iCE40 and tell me the LUT count."

> "Run generic synthesis on my counter module."

### Python

```python
from tools.synthesize import synthesize

# Generic synthesis
result = synthesize(
    code=open("counter.v").read(),
    top_module="counter",
    target="generic",
)
print(result["success"])  # True
print(result["modules"])  # ["counter"]

# Target-specific
result = synthesize(
    code=open("blinky.v").read(),
    top_module="blinky",
    target="ice40",
    timeout=180,
)
```
