# formal.py — SymbiYosys formal verification

Exposes `sby` as a first-class tool. Simulation answers "does this design behave
for the stimulus I wrote?"; formal verification answers "does any reachable
state violate this property?" — which is the check an AI-driven flow can run
without having to invent good stimulus first.

## Entry point

```python
from fpgazero_mcp.tools.formal import formal_verify

formal_verify(
    code=...,            # or files={...} / project_dir="..."
    top_module="top",
    language="verilog",  # verilog | systemverilog | vhdl
    mode="bmc",          # bmc | prove | cover | live
    depth=20,
    engine="smtbmc",     # key of ENGINES
    multiclock=False,
    include_dirs=None,
    defines=None,
    timeout=300,
)
```

Source input uses the shared `tools/sources.py` resolver, so `code`, `files` and
`project_dir` (including `files.f` filelists) behave exactly as they do for
`synthesize`, `place_and_route` and `simulate`.

## Modes

| Mode | Question answered | Result meaning |
|---|---|---|
| `bmc` | Does any assertion fail within `depth` cycles? | PASS is **bounded** — no counterexample up to `depth` |
| `prove` | Does any assertion fail in any reachable state? | PASS sets `proved: true` — unbounded, by k-induction |
| `cover` | Is each `cover()` statement reachable? | PASS means all were reached within `depth` |
| `live` | Do liveness properties hold? | bounded |

`bmc` is the default because it always terminates with a usable answer. `prove`
can return `UNKNOWN` when induction fails even though the property is true —
that usually means the design needs a strengthening invariant, and
`engine="abc-pdr"` is often dramatically faster for it.

## Why not `yosys_read_cmds`

The synthesis frontend discards `assert`/`assume`/`cover` cells and
`$anyconst`/`$anyseq`. Formal needs `read -formal`, so `_sby_script` builds its
own read commands rather than reusing the shared synthesis ones.

## Generated task file

```
[options]
mode bmc
depth 20

[engines]
smtbmc

[script]
read -formal design.v
prep -top top

[files]
design.v
```

sby stages `[files]` entries **by basename** into its own work directory, so
sources are copied flat into the workspace and a duplicate basename across the
design is rejected up front with `invalid_input`.

`engine` is a key into `ENGINES` rather than free text, because its value is
written verbatim into the generated task file.

## Return shape

```python
{
  "success": False,
  "tool": "sby",
  "status": "FAIL",           # PASS | FAIL | UNKNOWN | ERROR
  "mode": "bmc",
  "depth": 20,
  "engine": "smtbmc",
  "bounded": True,            # present for bmc/cover/live
  "engines": [{"engine": "smtbmc yices", "result": "FAIL"}],
  "failed_assertions": [{"module": "top", "location": "design.sv:14.7-14.32 (...)"}],
  "counterexample_trace": "demo/engine_0/trace.vcd",
  "error": "Property violated in 'top'. First failure: ...",
  "error_code": "formal_failed",
  "log": "...",
  "log_truncated": False,
}
```

A PASS in a bounded mode reports `proved: false` and says so in `summary`, so a
client does not mistake "no counterexample in 20 cycles" for a proof.

## Failure modes

| Condition | `error_code` |
|---|---|
| bad mode/engine/depth/top_module, no or duplicate sources | `invalid_input` |
| `sby` not on PATH | `tool_not_found` |
| exceeded `timeout` | `timeout` |
| FAIL, UNKNOWN, or sby task error | `formal_failed` |
