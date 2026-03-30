# lsp -- Diagnostics and Formatting

LSP-like structured diagnostics and auto-formatting for HDL source code.

## Index

- [MCP Tools](#mcp-tools)
- [Backends](#backends)
- [get_diagnostics](#get_diagnostics)
- [format_hdl](#format_hdl)
- [Usage](#usage)

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

## Usage

### MCP (via AI assistant)

> "Get diagnostics for this Verilog module."

> "Format this SystemVerilog file."

### Python

```python
from tools.lsp import get_diagnostics, format_hdl

# Diagnostics
result = get_diagnostics("module top; wire x = 1'b1; endmodule\n")
for diag in result["diagnostics"]:
    print(f"  line {diag['line']}: [{diag['severity']}] {diag['message']}")

# Formatting
result = format_hdl("module top(input wire a,output wire y);assign y=a;endmodule\n")
if result["changed"]:
    print(result["formatted"])
```
