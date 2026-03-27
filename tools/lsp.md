# lsp — Diagnostics and Formatting

LSP-like structured diagnostics and auto-formatting for HDL source code.

## MCP Tools

| Tool | Description |
|---|---|
| `get_diagnostics` | Structured per-line diagnostics (errors, warnings) |
| `format_hdl` | Auto-format HDL source code |

## Backends

### Diagnostics

| Language | Primary | Fallback |
|---|---|---|
| Verilog | Verilator (`--lint-only --error-limit 50`) | `verible-verilog-lint` |
| SystemVerilog | Verilator (`-g2012 --lint-only`) | `verible-verilog-lint` |
| VHDL | GHDL (`-a --std=08`) | *(none)* |

### Formatting

| Language | Tool |
|---|---|
| Verilog / SystemVerilog | `verible-verilog-format` |
| VHDL | `vsg --fix` (pip install vsg) |

## get_diagnostics

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | *(required)* | HDL source code |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |

### Response

```json
{
  "success": true,
  "tool": "verilator",
  "diagnostics": [
    {
      "severity": "warning",
      "code": "UNUSEDSIGNAL",
      "line": 5,
      "col": 10,
      "message": "Signal is not used: 'x'",
      "source": "verilator"
    }
  ]
}
```

## format_hdl

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | *(required)* | HDL source code to format |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |

### Response

```json
{
  "success": true,
  "tool": "verible-verilog-format",
  "formatted": "module top(\n    input wire a,\n    output wire y\n);\n  assign y = a;\nendmodule\n",
  "changed": true
}
```
