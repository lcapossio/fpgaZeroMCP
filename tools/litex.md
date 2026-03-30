# litex -- LiteX SoC Framework

Wrappers for LiteX board targets. Runs LiteX as `python -m litex_boards.targets.<board>`.

## Index

- [MCP Tools](#mcp-tools)
- [Board Name Validation](#board-name-validation)
- [litex_build](#litex_build)
- [litex_soc](#litex_soc)
- [litex_flow](#litex_flow)
- [Response](#response-all-tools)
- [Prerequisites](#prerequisites)
- [Usage](#usage)

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

## Usage

### MCP (via AI assistant)

> "Build a LiteX SoC for the Digilent Arty board."

> "Generate the SoC for lattice_ecp5_evn without building gateware."

### Python

```python
from tools.litex import litex_build, litex_soc, litex_flow

# Build for Arty
result = litex_build("digilent_arty", args=["--cpu-type=vexriscv"])
print(result["output_dir"])

# Generate SoC only (no gateware build)
result = litex_soc("digilent_arty")

# Custom flow
result = litex_flow("digilent_arty", args=["--build", "--with-ethernet"])
```
