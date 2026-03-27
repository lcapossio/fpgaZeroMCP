# simulate — HDL Simulation

Compile and run HDL simulations with design + testbench.

## MCP Tool

`simulate`

## Backends

| Language | Tool | Flow |
|---|---|---|
| Verilog / SystemVerilog | Icarus Verilog | `iverilog -g2012` compile, then `vvp` run |
| VHDL | GHDL | `ghdl -a` (analyze design), `ghdl -a` (analyze testbench), `ghdl -e` (elaborate), `ghdl -r` (run) |

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | *(required)* | HDL design source |
| `testbench` | string | *(required)* | HDL testbench source |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |
| `timeout` | integer | `60` | Simulation timeout in seconds (clamped to 1-3600) |

## Response

```json
{
  "success": true,
  "tool": "iverilog",
  "stage": "run",
  "stdout": "y=0\ny=1\n",
  "stderr": ""
}
```

## VHDL Notes

- The testbench must contain an `entity <name> is` declaration — the entity name is auto-extracted and used for elaboration and run.
- GHDL is invoked with `--std=08` (VHDL-2008).
- All GHDL stages use a shared `--workdir` so compiled units are visible across files.

## Stages

The `stage` field in the response indicates where execution stopped:

| Stage | Meaning |
|---|---|
| `compile` | iverilog compilation failed |
| `run` | Simulation ran (check `success` for pass/fail) |
| `analyze_design` | GHDL failed analyzing the design file |
| `analyze_testbench` | GHDL failed analyzing the testbench |
| `elaborate` | GHDL elaboration failed |
