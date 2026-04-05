# simulate -- HDL Simulation

Compile and run HDL simulations with design + testbench.

## Index

- [MCP Tool](#mcp-tool)
- [Backends](#backends)
- [Parameters](#parameters)
- [Response](#response)
- [Verdict Parsing](#verdict-parsing)
- [VCD Waveform Output](#vcd-waveform-output)
- [VHDL Notes](#vhdl-notes)
- [Stages](#stages)
- [Usage](#usage)

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
  "stdout": "TEST PASSED\n",
  "stderr": "",
  "verdict": {
    "verdict": "pass",
    "reason": "TEST PASSED"
  },
  "vcd": "...",
  "vcd_summary": {
    "signal_count": 4,
    "signals": ["clk", "rst", "data_in", "data_out"],
    "end_time": "1000",
    "final_values": {"clk": "0", "rst": "0", "data_in": "1", "data_out": "1"}
  }
}
```

## Verdict Parsing

The `verdict` field scans simulation output for common pass/fail patterns:

| Verdict | Condition |
|---|---|
| `"fail"` | Non-zero exit code, or output contains: `FAIL`, `TEST FAILED`, `ASSERTION FAILED`, `UVM_ERROR`, `UVM_FATAL`, `$fatal`, `Error:` |
| `"pass"` | Exit code 0 and output contains: `PASS`, `TEST PASSED`, `SIMULATION PASSED`, `All tests passed`, `UVM_PASS` |
| `"inconclusive"` | Exit code 0 but no recognized pass/fail pattern |

The `reason` field shows the matched pattern or "non-zero exit code".

## VCD Waveform Output

If the simulation produces a `.vcd` file (via `$dumpfile`/`$dumpvars`), it is returned:

- **`vcd`**: full VCD text (if under 512 KB)
- **`vcd_summary`**: structured summary with signal list, end time, and final signal values
- **`vcd_truncated` / `vcd_size_kb`**: returned instead of `vcd` if the file exceeds 512 KB

The summary lets the AI reason about simulation results without parsing raw VCD.

## VHDL Notes

- The testbench must contain an `entity <name> is` declaration -- the entity name is auto-extracted and used for elaboration and run.
- GHDL is invoked with `--std=08` (VHDL-2008).
- All GHDL stages use a shared `--workdir` so compiled units are visible across files.

## Stages

The `stage` field in the response indicates where execution stopped:

| Stage | Meaning |
|---|---|
| `compile` | iverilog compilation failed |
| `run` | Simulation ran (check `success` and `verdict` for result) |
| `analyze_design` | GHDL failed analyzing the design file |
| `analyze_testbench` | GHDL failed analyzing the testbench |
| `elaborate` | GHDL elaboration failed |

## Usage

### MCP (via AI assistant)

> "Simulate this FIFO with a testbench that writes 4 bytes then reads them back."

> "Did the simulation pass or fail?"

### Python

```python
from tools.simulate import simulate

result = simulate(design_code, tb_code, timeout=30)
print(result["verdict"]["verdict"])  # "pass", "fail", or "inconclusive"

# Check waveform signals
if "vcd_summary" in result:
    print(result["vcd_summary"]["signals"])
    print(result["vcd_summary"]["final_values"])
```
