# litex — LiteX SoC Framework

Wrappers for LiteX board targets. Runs LiteX as `python -m litex_boards.targets.<board>`.

## MCP Tools

| Tool | Description |
|---|---|
| `litex_build` | Run a board target with `--build` |
| `litex_soc` | Generate SoC without building gateware (`--no-compile`) |
| `litex_flow` | Generic runner with caller-provided args |

## Board Name Validation

Board names must match `^[A-Za-z_][A-Za-z0-9_.]*$` (valid Python module path). Examples:
- `digilent_arty`
- `lattice_ecp5_evn`
- `vendor.board`

Rejected: `bad-board`, `board; rm -rf /`, empty strings.

## litex_build

| Parameter | Type | Default | Description |
|---|---|---|---|
| `board` | string | *(required)* | LiteX board target |
| `args` | array | `null` | Extra CLI args |
| `output_dir` | string | `null` | Output directory (default: `no_commit/litex_build/<board>`) |
| `timeout` | integer | `600` | Timeout in seconds |

Automatically adds `--build` and `--output-dir` if not already present.

## litex_soc

| Parameter | Type | Default | Description |
|---|---|---|---|
| `board` | string | *(required)* | LiteX board target |
| `args` | array | `null` | Extra CLI args |
| `output_dir` | string | `null` | Output directory (default: `no_commit/litex_soc/<board>`) |
| `timeout` | integer | `300` | Timeout in seconds |

Strips `--build` if present, adds `--no-compile`.

## litex_flow

| Parameter | Type | Default | Description |
|---|---|---|---|
| `board` | string | *(required)* | LiteX board target |
| `args` | array | `null` | CLI args (passed as-is) |
| `timeout` | integer | `600` | Timeout in seconds |

## Response (all tools)

```json
{
  "success": true,
  "cmd": ["python", "-m", "litex_boards.targets.digilent_arty", "--build"],
  "stdout": "...",
  "stderr": "",
  "output_dir": "/path/to/output"
}
```

## Prerequisites

Requires [LiteX](https://github.com/enjoy-digital/litex) and [litex-boards](https://github.com/litex-hub/litex-boards) installed.
