# build_manager — Background Build Management

Start, monitor, and cancel long-running EDA builds in the background.

## MCP Tools

| Tool | Description |
|---|---|
| `start_build` | Start a command in the background, returns a `build_id` |
| `build_status` | Check progress — status, elapsed time, parsed build info, recent log |
| `list_builds` | List all tracked builds with status summary |
| `cancel_build` | Kill a running build |

## start_build

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cmd` | array | *(required)* | Command and arguments, e.g. `["yosys", "-s", "synth.ys"]` |
| `label` | string | `""` | Human-readable label |
| `work_dir` | string | `null` | Working directory (default: project root) |

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

## build_status

| Parameter | Type | Default | Description |
|---|---|---|---|
| `build_id` | string | *(required)* | Build ID from `start_build` |
| `tail_lines` | integer | `30` | Number of log lines from the end |

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

## Internals

- Build logs are written to `no_commit/builds/<build_id>.log`
- A daemon thread monitors each subprocess and records the exit code
- Large log tail reads use seek-from-end for efficiency
- Thread-safe via `threading.Lock`
