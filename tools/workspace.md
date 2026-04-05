# workspace -- Temporary Workspace Management

Context manager that creates a temporary directory for EDA tool operations.

## Index

- [Where Build Files Go](#where-build-files-go)
- [Usage](#usage)
- [Directory Selection](#directory-selection)
- [Environment Variables](#environment-variables)
- [Cleanup](#cleanup)

## Where Build Files Go

Different tools store files in different places. This matters when you need to find build artifacts or understand disk usage.

| Tool | Location | Persists? | What's there |
|---|---|---|---|
| `lint_hdl`, `lint_project` | `<cwd>/no_commit/fpgazero_tmp/lint_<uuid>/` | No | Temp HDL files, deleted after lint |
| `synthesize` | `<cwd>/no_commit/fpgazero_tmp/synth_<uuid>/` | No | Yosys scripts, netlist JSON, deleted after synth |
| `simulate` | `<cwd>/no_commit/fpgazero_tmp/sim_<uuid>/` | No | Design + testbench files, VCD waveforms, deleted after sim |
| `place_and_route` (default) | `<cwd>/no_commit/fpgazero_tmp/pnr_<uuid>/` | No | Netlist, bitstream, constraint files, deleted after PnR |
| `place_and_route` (with `work_dir`) | user-specified path | **Yes** | Same as above, but kept for incremental reuse |
| `start_build` | subprocess runs in `work_dir` or `cwd` | **Yes** | Whatever the tool generates (Vivado `.runs/`, `.cache/`, etc.) |
| `start_build` logs | `<cwd>/no_commit/builds/<build_id>.log` | **Yes** | Combined stdout+stderr log. Use `cleanup_build_logs` to purge. |
| `litex_build` | `<cwd>/no_commit/litex_build/<board>/` | **Yes** | Full LiteX build output (gateware, software, reports) |
| `litex_soc` | `<cwd>/no_commit/litex_soc/<board>/` | **Yes** | Generated SoC files without gateware |
| `get_diagnostics`, `format_hdl` | `<cwd>/no_commit/fpgazero_tmp/diag_<uuid>/` | No | Temp file for linter/formatter, deleted immediately |

**Key point:** `<cwd>` is the working directory of the MCP server process, which depends on how it was launched (Claude Desktop, VS Code, Cursor, etc.). If you need predictable paths, set `FPGAZERO_TMPDIR`.

### Vivado / Quartus via start_build

When you run a Vivado or Quartus build through `start_build`, the EDA tool's own artifacts (`.runs/`, `.cache/`, project files) go into the **subprocess working directory** -- either `work_dir` if you set it, or the server's `cwd`. Vivado in particular creates large directory trees. Always set `work_dir` to a dedicated build directory:

```json
{
  "cmd": ["vivado", "-mode", "batch", "-source", "build.tcl"],
  "work_dir": "/home/user/projects/my_fpga/build",
  "label": "Vivado synthesis"
}
```

## Usage

### MCP (via AI assistant)

The workspace is used internally by all tool wrappers (lint, synthesize, simulate, pnr). You don't call it directly via MCP.

### Python

```python
from tools.workspace import temporary_workspace

with temporary_workspace("synth_") as tmpdir:
    # tmpdir is a writable directory path string
    # Write source files, run tools, read results
    with open(f"{tmpdir}/design.v", "w") as f:
        f.write("module top(...); ... endmodule")
    # Run EDA tools in tmpdir
    pass
# Directory is cleaned up automatically on exit
```

## Directory Selection

The workspace tries these locations in order, using the first one that is writable:

1. `FPGAZERO_TMPDIR` environment variable
2. `<cwd>/no_commit/fpgazero_tmp`
3. `<repo_root>/no_commit/fpgazero_tmp`
4. System temp directory (`tempfile.gettempdir()`)

A probe file is written to verify writability before the directory is used.

## Environment Variables

| Variable | Description |
|---|---|
| `FPGAZERO_TMPDIR` | Override the temporary workspace root directory |
| `FPGAZERO_ALLOWED_DIRS` | OS pathsep-separated list of extra directories that `project_dir` is allowed to read from (in addition to cwd and $HOME) |

## Cleanup

The directory and all its contents are removed via `shutil.rmtree` when the context manager exits, even if an exception occurs.

**Persistent files** (build logs, LiteX output, `work_dir` PnR runs) are NOT auto-cleaned. Use `cleanup_build_logs` to purge old build logs, or delete `no_commit/` contents manually.
