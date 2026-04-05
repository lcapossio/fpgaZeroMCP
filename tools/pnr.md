# pnr -- Place and Route

Full synthesis + place-and-route pipeline: Yosys synthesis to JSON netlist, then nextpnr.

## Index

- [MCP Tool](#mcp-tool)
- [Source Input Modes](#source-input-modes)
- [Board Presets](#board-presets)
- [Supported Targets](#supported-targets)
- [Common Device / Package Values](#common-device--package-values)
- [Parameters](#parameters)
- [Constraint Auto-Detection](#constraint-auto-detection)
- [Bitstream Output](#bitstream-output)
- [Response](#response)
- [Pipeline](#pipeline)
- [Timeout Budget](#timeout-budget)
- [Incremental Workflows (work_dir)](#incremental-workflows-work_dir)
- [Usage](#usage)

## MCP Tool

`place_and_route`

## Source Input Modes

Same as [synthesize](synthesize.md#source-input-modes). Provide exactly one of `code`, `files`, or `project_dir`.

## Board Presets

Pass `board` instead of manually specifying `target`/`device`/`package`. Explicit parameters override the preset.

| Board | Target | Device | Package | Clock (MHz) |
|---|---|---|---|---|
| `icebreaker` | ice40 | up5k | sg48 | 12 |
| `icestick` | ice40 | hx1k | tq144 | 12 |
| `tinyfpga_bx` | ice40 | lp8k | cm81 | 16 |
| `ulx3s_25f` | ecp5 | 25k | CABGA381 | 25 |
| `ulx3s_45f` | ecp5 | 45k | CABGA381 | 25 |
| `ulx3s_85f` | ecp5 | 85k | CABGA381 | 25 |
| `orangecrab_r02` | ecp5 | 25k | CSFBGA285 | 48 |
| `colorlight_i5` | ecp5 | 25k | CABGA256 | 25 |
| `tangnano_9k` | gowin | GW1NR-LV9QN88PC6/I5 | -- | 27 |
| `tangnano_20k` | gowin | GW2A-LV18PG256C8/I7 | -- | 27 |

Use `list_boards` to see all presets. When a board has a `clock_mhz`, the timing result includes `target_mhz` and `meets_target: true/false`.

## Supported Targets

| Target | nextpnr Binary | Constraints | Output |
|---|---|---|---|
| `ice40` | `nextpnr-ice40` | `.pcf` | `.asc` |
| `ecp5` | `nextpnr-ecp5` | `.lpf` | `.config` |
| `nexus` | `nextpnr-nexus` | `.pdc` | `.fasm` |
| `gowin` | `nextpnr-gowin` | `.cst` | `_pnr.json` |

## Common Device / Package Values

| Target | Device | Package |
|---|---|---|
| ice40 | `hx1k`, `hx8k`, `up5k`, `lp1k` | `tq144`, `qn84`, `sg48`, `cm81` |
| ecp5 | `25k`, `45k`, `85k` | `CABGA256`, `CABGA381` |
| nexus | `LIFCL-40-9BG400C` | *(embedded in device string)* |
| gowin | `GW1N-UV4LQ144C6/I5` | *(embedded in device string)* |

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `code` | string | `""` | HDL source code (single-file mode) |
| `files` | object | `null` | Multi-file mode: `{"filename": "source", ...}` |
| `project_dir` | string | `null` | Disk mode: path to HDL project directory |
| `top_module` | string | *(required)* | Top-level module name |
| `language` | string | `"verilog"` | `verilog`, `systemverilog`, or `vhdl` |
| `target` | string | `""` | FPGA family (can be omitted if `board` is set) |
| `device` | string | `""` | Device variant (can be omitted if `board` is set) |
| `package` | string | `""` | Package (not needed for nexus/gowin or with `board`) |
| `constraints` | string | `""` | Pin constraints text. If empty, auto-detected from `project_dir`. |
| `board` | string | `null` | Board preset (e.g. `"icebreaker"`) |
| `nextpnr_args` | array | `null` | Extra nextpnr arguments (e.g. `["--seed", "42"]`) |
| `work_dir` | string | `null` | Persistent working directory (see below) |
| `timeout` | integer | `300` | Total timeout in seconds (clamped to 1-3600) |
| `backend` | string | `"yosys"` | `yosys` or `litex` |
| `litex_board` | string | `null` | LiteX board (required if backend=litex) |
| `litex_args` | array | `null` | Extra LiteX CLI args |

## Constraint Auto-Detection

When using `project_dir` and `constraints` is empty, the tool automatically searches for a constraint file matching the target:

- ice40: `*.pcf`
- ecp5: `*.lpf`
- nexus: `*.pdc`
- gowin: `*.cst`

If found, it's used automatically. The `constraints` field in the response reports the source (`"auto-detected: pins.pcf"` or `"provided"` or `"none"`).

## Bitstream Output

On successful PnR, the response includes:
- `bitstream_b64`: base64-encoded bitstream (`.asc`, `.config`, `.fasm`, or PnR JSON)
- `bitstream_ext`: file extension (e.g. `.asc`)

Pass `bitstream_b64` to `program_fpga` to flash the design.

## Response

```json
{
  "success": true,
  "stage": "place_and_route",
  "target": "ice40",
  "device": "up5k",
  "package": "sg48",
  "language": "verilog",
  "top_module": "blinky",
  "board": "icebreaker",
  "constraints": "auto-detected: pins.pcf",
  "timing": {
    "max_freq_mhz": 42.5,
    "critical_path_ns": 23.5,
    "target_mhz": 12.0,
    "meets_target": true
  },
  "utilization": { "luts_used": 42, "luts_total": 5280 },
  "bitstream_b64": "...",
  "bitstream_ext": ".asc",
  "work_dir": "/path/to/persistent/dir",
  "synth_log": "...",
  "pnr_stdout": "...",
  "pnr_stderr": "..."
}
```

## Pipeline

1. **Source resolution** -- Reads from `code`, `files`, or `project_dir` (with filelist and include path support)
2. **Synthesis** -- Yosys reads HDL (Verilog/SV/VHDL), runs `synth_<target>`, writes JSON netlist
3. **Place and route** -- nextpnr reads the netlist, places and routes with constraints
4. **Output parsing** -- Timing (Fmax, critical path) and utilization extracted from nextpnr output
5. **Bitstream** -- Output file base64-encoded and returned

## Timeout Budget

The `timeout` parameter is the total budget for both stages. Synthesis gets up to 1/3 of the total (minimum 30s, capped at the total). Place-and-route gets the remaining time. If synthesis consumes the budget leaving less than 10s for PnR, the tool fails fast with a clear message.

## Incremental Workflows (work_dir)

Pass `work_dir` to keep build artifacts across runs:

```python
# First run: full build
r1 = place_and_route(project_dir="~/my_fpga", top_module="top",
                     board="icebreaker", work_dir="~/my_fpga/build")
# work_dir path returned in response

# Second run: files already in work_dir, re-synthesize with different seed
r2 = place_and_route(project_dir="~/my_fpga", top_module="top",
                     board="icebreaker", work_dir="~/my_fpga/build",
                     nextpnr_args=["--seed", "42"])
```

Without `work_dir`, temp files are deleted after each run.

## Usage

### MCP (via AI assistant)

> "PnR this design for the iCEBreaker board."

> "Run place-and-route on my project at ~/projects/blinky targeting ULX3S 85F."

> "Re-run PnR with seed 42 to try for better timing."

### Python

```python
from tools.pnr import place_and_route

# Board preset (simplest)
result = place_and_route(
    code=open("blinky.v").read(),
    top_module="blinky",
    board="icebreaker",
)
print(result["timing"]["meets_target"])  # True/False vs 12 MHz

# Manual target with constraints
result = place_and_route(
    project_dir="/home/user/my_ecp5_project/",
    top_module="top",
    target="ecp5",
    device="85k",
    package="CABGA381",
    # constraints auto-detected from project dir
)

# With seed and persistent workspace
result = place_and_route(
    code="...", top_module="top",
    board="icebreaker",
    nextpnr_args=["--seed", "5", "--placer", "heap"],
    work_dir="./build/",
)
```
