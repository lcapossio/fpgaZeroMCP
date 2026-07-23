# build_manager -- Background Build Management

Start, monitor, and cancel long-running EDA builds in the background.

## Index

- [MCP Tools](#mcp-tools)
- [start_build](#start_build)
- [Command Allowlist](#command-allowlist)
- [build_status](#build_status)
- [list_builds](#list_builds)
- [cancel_build](#cancel_build)
- [cleanup_build_logs](#cleanup_build_logs)
- [File Locations](#file-locations)
- [Internals](#internals)
- [Usage](#usage)

## MCP Tools

| Tool | Description |
|---|---|
| `start_build` | Start a command in the background, returns a `build_id` |
| `build_status` | Check progress: status, elapsed time, parsed build info, recent log |
| `list_builds` | List all tracked builds with status summary |
| `cancel_build` | Kill a running build |
| `cleanup_build_logs` | Delete old build logs to reclaim disk space |

## start_build

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cmd` | array | *(required)* | Command and arguments, e.g. `["yosys", "-s", "synth.ys"]` |
| `label` | string | `""` | Human-readable label |
| `work_dir` | string | `null` | Working directory (default: server's cwd) |

### Response

```json
{
  "success": true,
  "build_id": "a3f2c1b0",
  "label": "ECP5 synthesis",
  "log_path": "no_commit/builds/a3f2c1b0.log",
  "message": "Build started. Check with build_status(build_id='a3f2c1b0')."
}
```

## Command Allowlist

`start_build` only permits EDA-related commands. Arbitrary executables are rejected.

**Allowed tools:**
- Synthesis/PnR: `yosys`, `nextpnr-ice40`, `nextpnr-ecp5`, `nextpnr-nexus`, `nextpnr-gowin`
- Simulation/lint: `ghdl`, `iverilog`, `vvp`, `verilator`, `verible-verilog-lint`
- Formal verification: `sby`
- Programming: `iceprog`, `openFPGALoader`, `ecpprog`
- Python: only `python -m litex_boards.*` or `python -m litex.*` (all Python versions accepted)

**Blocked:** `python -c`, arbitrary scripts, non-EDA binaries.

## build_status

| Parameter | Type | Default | Description |
|---|---|---|---|
| `build_id` | string | *(required)* | Build ID from `start_build` |
| `tail_lines` | integer | `30` | Number of log lines from the end |
| `parse` | boolean | `true` | Parse log for phase/utilization/timing (set false for fast polling) |

### Response

```json
{
  "build_id": "a3f2c1b0",
  "label": "ECP5 synthesis",
  "status": "running",
  "elapsed_s": 142.3,
  "returncode": null,
  "log_path": "no_commit/builds/a3f2c1b0.log",
  "log_size_kb": 245.6,
  "tail": "... last 30 lines ...",
  "build_info": {
    "tool": "nextpnr",
    "phase": "nextpnr_routing",
    "phase_label": "Nextpnr: Routing nets",
    "utilization": { "luts": { "used": 4200, "total": 5280, "pct": 79.5 } },
    "timing": { "fmax_mhz": 142.34, "met": true },
    "health": { "status": "ok", "concerns": ["No issues detected"] },
    "warnings": 2,
    "errors": 0
  }
}
```

The `build_info` field is produced by the [build log parser](build_parser.md). It auto-detects the EDA tool and extracts phases, utilization, timing, congestion, and health.

## list_builds

No parameters. Returns a list of all tracked builds (no full log parsing for speed).

## cancel_build

| Parameter | Type | Default | Description |
|---|---|---|---|
| `build_id` | string | *(required)* | Build ID to cancel |

Sends SIGTERM, waits 5s, then SIGKILL if needed.

## cleanup_build_logs

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_age_days` | integer | `7` | Delete logs older than this many days |
| `max_total_mb` | integer | `500` | Target max total log size in MB |

Deletes old logs first, then trims by size (oldest first).

## File Locations

| What | Location |
|---|---|
| Build logs | `<cwd>/no_commit/builds/<build_id>.log` |
| Build subprocess cwd | `work_dir` parameter, or server's cwd |
| Tool-generated artifacts | Wherever the tool writes them relative to subprocess cwd |

`work_dir` must resolve under the server's current directory, `$HOME`, or a directory listed in `FPGAZERO_ALLOWED_DIRS`.

**Important for Vivado/Quartus:** These tools create large directory trees (`.runs/`, `.cache/`, project files) in their working directory. Always set `work_dir` to a dedicated build directory to avoid polluting the server's cwd.

## Internals

- Build logs are written to `no_commit/builds/<build_id>.log`
- A daemon thread monitors each subprocess and records the exit code
- Large log tail reads use seek-from-end for efficiency
- Thread-safe via `threading.Lock`

## Usage

### MCP (via AI assistant)

> "Start a Yosys synthesis build in the background and check on it every 2 minutes."

> "Cancel build a3f2c1b0."

> "Clean up old build logs."

### Python

```python
from tools.build_manager import BuildManager

mgr = BuildManager()

# Start a build (must be in the allowlist)
result = mgr.start(
    cmd=["yosys", "-s", "synth.ys"],
    label="ECP5 synth",
    work_dir="/path/to/build/dir",
)
build_id = result["build_id"]

# Check status
status = mgr.status(build_id)
print(status["build_info"]["phase_label"])

# Clean up old logs
mgr.cleanup_logs(max_age_days=3)
```
