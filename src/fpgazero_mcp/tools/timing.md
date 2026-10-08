# timing.py — timing-closure sweep

`place_and_route` is a single shot. When it comes back under the clock target,
nothing in the toolchain says what to try next, so a client ends up hand-rolling
a loop — re-paying full synthesis on every iteration and inventing its own
comparison of the results. `close_timing` is that loop, done properly.

## Entry point

```python
from fpgazero_mcp.tools.timing import close_timing

close_timing(
    code=...,              # or files={...} / project_dir="..."
    top_module="top",
    board="icebreaker",    # or target/device/package
    target_mhz=None,       # defaults to the board preset's clock
    max_attempts=0,        # 0 = per-backend default (4 nextpnr, 3 Vivado)
    seeds=None,            # nextpnr placer seeds
    strategies=None,       # Vivado strategy names
    stop_when_met=True,
    timeout=300,           # PER ATTEMPT
)
```

## What varies per backend

| Backend | Knob | Cost per attempt | Synthesis |
|---|---|---|---|
| nextpnr | `--seed N` | seconds | **once**, then reused |
| Vivado | strategy directives | minutes | every attempt |

Placer seeds do not change the netlist, so the nextpnr path synthesizes on the
first attempt and passes `reuse_netlist=True` for the rest, against one shared
`work_dir`. Vivado strategies include a `synth_design -directive`, which does
change the netlist, so there is nothing to reuse — which is also why the default
attempt count is lower there.

## Vivado strategies

Ordered cheapest-first, so stopping at the first one that closes is also the
fastest route to a result.

| Name | synth | place | route | phys_opt |
|---|---|---|---|---|
| `default` | — | — | — | no |
| `performance` | PerformanceOptimized | ExtraTimingOpt | Explore | yes |
| `explore` | PerformanceOptimized | Explore | AggressiveExplore | yes |
| `no_timing_relaxation` | AlternateRoutability | ExtraPostPlacementOpt | NoTimingRelaxation | yes |

Directive strings are interpolated into generated TCL, so they are allowlisted
in `vivado.py` (`SYNTH_DIRECTIVES`, `PLACE_DIRECTIVES`, `ROUTE_DIRECTIVES`)
rather than accepted as free text.

## `--timing-allow-fail` is load-bearing

Each attempt runs with `allow_timing_fail=True`. Without it nextpnr treats a
missed clock constraint as a hard error, and a near miss would return no fmax at
all — making the attempts incomparable and the whole sweep pointless. With it,
an attempt that routes but misses timing still reports its achieved fmax and
writes a bitstream.

This is why `success` and `closed` are separate:

- `success` — at least one attempt routed
- `closed` — the target was met (`None` when no target was known)

A design that routes at 46 MHz against a 48 MHz target is `success: True,
closed: False`. Its bitstream is still returned, because it can be programmed
and measured.

## Best-attempt selection

`_attempt_metric` ranks on fmax when available, falling back to worst-negative
slack (Vivado only derives an fmax when the XDC supplied a clock period), and
ranks any unrouted attempt below every routed one.

## Budget guard

`max_attempts × timeout` must stay within `MAX_TOTAL_BUDGET_S` (3600). A sweep
that cannot finish inside an MCP client's patience is refused up front rather
than discovered by timing out. `timeout` is **per attempt** — the tool schema
says so explicitly, because the natural reading is otherwise.

## Early abort

A missing toolchain, bad input, a path-security rejection or a synthesis failure
will fail identically on every attempt, so the sweep stops at the first one
instead of burning the budget. `attempts_run` vs `attempts_planned` shows it
happened.

## Return shape

```python
{
  "success": True,
  "stage": "close_timing",
  "backend": "yosys",
  "closed": False,
  "target_mhz": 48.0,
  "achieved_mhz": 46.0,
  "margin_mhz": -2.0,
  "attempts_run": 4,
  "attempts_planned": 4,
  "attempts": [
    {"attempt": 1, "seed": 1, "routed": True, "max_freq_mhz": 44.12,
     "meets_target": False, "timing": {...}, "utilization": {...}},
    ...
  ],
  "best_attempt": 2,
  "best": {...},
  "bitstream_path": ".../bitstreams/....bin",
  "work_dir": "~/.fpgazero_mcp/work/timing/<hex>",
  "summary": "Did not close 48.0 MHz in 4 attempt(s); best was 46.0 MHz "
             "(range 44.12-46.0 MHz). Try more seeds, pipeline the critical "
             "path, or reduce the clock.",
}
```

Per-attempt records are deliberately compact — timing, utilization and the
label only. Full logs stay on the single `place_and_route` result for the best
attempt.

## Failure modes

| Condition | `error_code` |
|---|---|
| `backend="litex"`, seeds on Xilinx, strategies on nextpnr, unknown strategy, budget exceeded | `invalid_input` |
| no attempt routed | propagated from the best attempt (`pnr_failed`, `tool_not_found`, …) |
