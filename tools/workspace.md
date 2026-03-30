# workspace -- Temporary Workspace Management

Context manager that creates a temporary directory for EDA tool operations.

## Index

- [Usage](#usage)
- [Directory Selection](#directory-selection)
- [Environment Variable](#environment-variable)
- [Cleanup](#cleanup)

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

## Environment Variable

| Variable | Description |
|---|---|
| `FPGAZERO_TMPDIR` | Override the temporary workspace root directory |

## Cleanup

The directory and all its contents are removed via `shutil.rmtree` when the context manager exits, even if an exception occurs.
