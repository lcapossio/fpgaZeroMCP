# build_parser -- Build Log Parser

Single-pass parser for EDA tool build logs. Auto-detects the tool and extracts structured build information.

## Index

- [Supported Tools](#supported-tools)
- [Extracted Information](#extracted-information)
  - [Phases](#phases)
  - [Resource Utilization](#resource-utilization)
  - [Timing](#timing)
  - [Congestion](#congestion)
  - [Progress Estimation](#progress-estimation)
  - [Warning / Error Counts](#warning--error-counts)
- [Health Assessment](#health-assessment)
- [Usage](#usage)

## Supported Tools

| Tool | Detection Signature |
|---|---|
| Yosys | `Yosys \d`, `Executing \w+ pass` |
| nextpnr | `nextpnr`, `Info: Pack/Plac/Rout/Device` |
| Vivado | `Vivado `, `synth_design`, `place_design`, `route_design` |
| Quartus | `quartus_`, `Analysis & Synthesis`, `Fitter (` |
| GHDL | `ghdl`, `GHDL \d` |

## Extracted Information

### Phases

Each tool has its own phase sequence:

| Tool | Phases |
|---|---|
| Yosys | read_design, elaboration, optimization, synthesis, mapping, write_output |
| nextpnr | packing, placing, routing, timing, bitstream |
| Vivado | synth, opt, phys_opt, place, route, timing, drc, write |
| Quartus | analysis, synth, fit, place, route, sta, asm, eda, pgm |
| GHDL | analyze, elaborate, simulate |

Phases include line counts for duration estimation.

### Resource Utilization

| Tool | Resources Parsed |
|---|---|
| nextpnr | LUTs, FFs, BRAMs, IOs, DSPs |
| Vivado | Slice LUTs, Slice Registers, Block RAM, DSPs, Bonded IOB, CLB LUTs/Regs (UltraScale), URAM |
| Quartus | Logic Elements, ALMs, ALUTs, Registers, Memory bits, M-K blocks, DSPs, Pins, PLLs, MLABs |

Each resource reports `used`, `total`, and `pct`.

### Timing

| Metric | Tools |
|---|---|
| Fmax (MHz) | nextpnr, Vivado, Quartus |
| Restricted Fmax | Quartus |
| Critical path (ns) | nextpnr |
| WNS / TNS / WHS / THS (ns) | Vivado |
| Setup / Hold slack (ns) | Quartus |
| Met (pass/fail) | nextpnr |

### Congestion

Detected indicators: unrouted nets, routing budget exceeded, congestion level, interconnect usage %.

### Progress Estimation

| Tool | Indicator |
|---|---|
| nextpnr | Placer iteration count, router pass number |
| Vivado | Phase step number (e.g., `3.2`) |
| Quartus | Fitter completion % |

### Warning / Error Counts

Tool-specific patterns avoid false positives:

| Tool | Error Pattern | Warning Pattern |
|---|---|---|
| Yosys | `^ERROR:` | `^Warning:` |
| nextpnr | `^ERROR:` or `Error:` | `^Warning:` or `Warning:` |
| Vivado | `^ERROR:` or `CRITICAL WARNING:` | `^WARNING:` |
| Quartus | `^Error (\d+):` | `^Warning (\d+):` |
| GHDL | `:line:col: error:` | `:line:col: warning:` |

## Health Assessment

| Status | Meaning |
|---|---|
| `ok` | No issues detected |
| `warning` | Utilization > 80%, tight timing margin (WNS < 0.5 ns) |
| `critical` | Utilization > 95%, timing violated (WNS < 0), unrouted nets, Fmax FAIL |
| `error` | Errors found in log |

## Usage

### MCP (via AI assistant)

The parser is used internally by `build_status` -- you don't call it directly via MCP. When you check a build's status, the `build_info` field contains the parsed results.

`build_status` parses the log incrementally: each poll parses only the output written since the previous poll, so polling stays cheap however long the log grows, and warning/error counts cover the whole log. If a build is first polled after it has already written more than 8 MB, the parser reads the head of the log (for tool detection) and the most recent 8 MB, skipping the middle; `build_info.log_bytes_skipped` then reports how much was skipped, and the counts cover only what was parsed.

### Python

```python
from fpgazero_mcp.tools.build_parser import parse_build_log

# Parse a log file
with open("build.log") as f:
    result = parse_build_log(f.read())

print(result["tool"])          # "nextpnr"
print(result["phase"])         # "nextpnr_routing"
print(result["phase_label"])   # "Nextpnr: Routing nets"
print(result["health"])        # {"status": "warning", "concerns": [...]}
print(result["utilization"])   # {"luts": {"used": 4200, "total": 5280, "pct": 79.5}}
print(result["timing"])        # {"fmax_mhz": 142.34, "met": true}
print(result["warnings"])      # 5
print(result["errors"])        # 0

# Check phase history with line counts
for phase in result["phase_detail"]:
    print(f"  {phase['phase']}: {phase['lines']} lines")
```

For a log that is still growing, `BuildLogParser` parses it in pieces. Feed it complete lines only; the result is identical to parsing the whole text at once:

```python
from fpgazero_mcp.tools.build_parser import BuildLogParser

parser = BuildLogParser()
parser.feed(first_lines)
print(parser.result()["phase"])
parser.feed(more_lines)        # only the new output is parsed
print(parser.result()["phase"])
```
