# synthesize — Yosys Synthesis

Synthesize Verilog designs using Yosys. Returns resource statistics and inferred module names.

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
