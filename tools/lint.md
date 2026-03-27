# lint — HDL Linting

Syntax and error checking for Verilog, SystemVerilog, and VHDL source code.

## Tools

| MCP Tool | Description |
|---|---|
| `lint_hdl` | Lint a single HDL file |
| `lint_project` | Lint multiple files together (cross-module references resolve) |

## Backends

| Language | Tool | Flags |
|---|---|---|
| Verilog | `iverilog` | `-tnull` (syntax only, no output) |
| SystemVerilog | `iverilog` | `-g2012 -tnull` |
| VHDL | `ghdl` | `-a --std=08` |

## lint_hdl

Lint a single source string.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | *(required)* | HDL source code |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |
| `top_module` | string | `null` | Top-level module name (optional) |

### Response

```json
{
  "success": true,
  "tool": "iverilog",
  "language": "verilog",
  "stdout": "",
  "stderr": "",
  "message": "No errors found."
}
```

## lint_project

Lint multiple files in a single invocation so cross-module references resolve.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `files` | object | *(required)* | Mapping of filename to source code |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |
| `top_module` | string | `null` | Top-level module name (passed as `-s` to iverilog) |
| `timeout` | integer | `60` | Timeout in seconds |

### Example

```json
{
  "files": {
    "sub.v": "module sub(input wire a, output wire y); assign y = ~a; endmodule",
    "top.v": "module top(input wire a, output wire y); sub u0(.a(a), .y(y)); endmodule"
  },
  "top_module": "top"
}
```

### Response

```json
{
  "success": true,
  "tool": "iverilog",
  "language": "verilog",
  "files": ["sub.v", "top.v"],
  "stdout": "",
  "stderr": "",
  "message": "No errors found."
}
```
