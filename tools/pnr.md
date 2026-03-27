# pnr — Place and Route

Full synthesis + place-and-route pipeline: Yosys synthesis to JSON netlist, then nextpnr.

## MCP Tool

`place_and_route`

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
| `code` | string | *(required)* | Verilog source code |
| `top_module` | string | *(required)* | Top-level module name |
| `target` | string | *(required)* | FPGA family |
| `device` | string | *(required)* | Device variant |
| `package` | string | `""` | Package (not needed for nexus/gowin) |
| `constraints` | string | `""` | Pin constraints text (PCF/LPF/PDC/CST) |
| `timeout` | integer | `300` | PnR timeout in seconds (clamped to 1-3600) |
| `backend` | string | `"yosys"` | `yosys` or `litex` |
| `litex_board` | string | `null` | LiteX board (required if backend=litex) |
| `litex_args` | array | `null` | Extra LiteX CLI args |

## Response

```json
{
  "success": true,
  "stage": "place_and_route",
  "target": "ice40",
  "device": "hx1k",
  "package": "tq144",
  "top_module": "top",
  "timing": {
    "max_freq_mhz": 142.34,
    "critical_path_ns": 7.03
  },
  "utilization": {
    "luts_used": 42,
    "luts_total": 1280,
    "ios_used": 3,
    "ios_total": 206
  },
  "synth_log": "...",
  "pnr_stdout": "...",
  "pnr_stderr": "..."
}
```

## Pipeline

1. **Synthesis** — Yosys reads Verilog, runs `synth_<target>`, writes JSON netlist
2. **Place and route** — nextpnr reads the netlist, places and routes with optional constraints
3. **Output parsing** — Timing (Fmax, critical path) and utilization extracted from nextpnr output
