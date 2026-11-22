# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Timing-closure sweep: run place-and-route repeatedly and keep the best result.

`place_and_route` is a single shot. When it comes back below the clock target
there is nothing in the toolchain that says what to try next, so the client has
to hand-roll a loop, re-paying full synthesis each time and inventing its own
comparison of the results.

This runs that loop properly. On nextpnr targets it varies the placer seed
against a netlist synthesized once; on Xilinx parts it varies Vivado strategy
directives (which do change synthesis, so each attempt is a full run). Attempts
stop as soon as the target is met, and the best attempt's bitstream is the one
returned.
"""

from __future__ import annotations

import os
import shutil
from typing import Callable
from uuid import uuid4

from fpgazero_mcp.paths import data_root
from fpgazero_mcp.tools.boards import get_board_preset
from fpgazero_mcp.tools.errors import err, INVALID_INPUT
from fpgazero_mcp.tools.progress import make_reporter

#: Hard ceiling on max_attempts * timeout. A sweep that cannot finish inside an
#: MCP client's patience is worse than a clear refusal up front.
MAX_TOTAL_BUDGET_S = 3600

#: Vivado iterations cost minutes each, nextpnr seeds seconds. Defaulting both
#: to the same attempt count would make a Xilinx sweep a 40-minute surprise.
DEFAULT_ATTEMPTS_NEXTPNR = 4
DEFAULT_ATTEMPTS_VIVADO = 3

#: Ordered Vivado strategies, cheapest first. Each later entry asks Vivado for
#: more effort, so stopping at the first one that closes is also the fastest.
VIVADO_STRATEGIES: tuple[dict, ...] = (
    {
        "name": "default",
        "synth_directive": "default",
        "place_directive": "default",
        "route_directive": "default",
        "phys_opt": False,
    },
    {
        "name": "performance",
        "synth_directive": "PerformanceOptimized",
        "place_directive": "ExtraTimingOpt",
        "route_directive": "Explore",
        "phys_opt": True,
    },
    {
        "name": "explore",
        "synth_directive": "PerformanceOptimized",
        "place_directive": "Explore",
        "route_directive": "AggressiveExplore",
        "phys_opt": True,
    },
    {
        "name": "no_timing_relaxation",
        "synth_directive": "AlternateRoutability",
        "place_directive": "ExtraPostPlacementOpt",
        "route_directive": "NoTimingRelaxation",
        "phys_opt": True,
    },
)


def _attempt_metric(attempt: dict) -> tuple:
    """Sort key for picking the best attempt; larger is better.

    fmax is the comparable number when available. Vivado only yields one when
    the XDC gave a clock period, so fall back to worst-negative-slack, which is
    always present once routing finished.
    """
    if not attempt.get("routed"):
        return (0, float("-inf"))
    timing = attempt.get("timing") or {}
    fmax = timing.get("max_freq_mhz")
    if fmax is not None:
        return (2, float(fmax))
    wns = timing.get("wns_ns")
    if wns is not None:
        return (1, float(wns))
    return (1, float("-inf"))


def _over_budget(attempts: int, timeout: int) -> dict:
    return err(
        INVALID_INPUT,
        f"{attempts} attempts x {timeout} s timeout exceeds the "
        f"{MAX_TOTAL_BUDGET_S} s sweep budget. Lower max_attempts or timeout.",
    )


def _summarize(attempt_no: int, label: dict, result: dict) -> dict:
    """Compact per-attempt record. Full logs stay with the best attempt only."""
    timing = result.get("timing") or {}
    summary: dict = {
        "attempt": attempt_no,
        **label,
        "routed": bool(result.get("routed")),
        "timing": timing,
        "utilization": result.get("utilization") or {},
    }
    if timing.get("max_freq_mhz") is not None:
        summary["max_freq_mhz"] = timing["max_freq_mhz"]
    if timing.get("meets_target") is not None:
        summary["meets_target"] = timing["meets_target"]
    if result.get("error"):
        summary["error"] = result["error"]
        summary["error_code"] = result.get("error_code")
    if result.get("netlist_reused"):
        summary["netlist_reused"] = True
    return summary


def close_timing(
    code: str = "",
    top_module: str = "",
    target: str = "",
    device: str = "",
    package: str = "",
    constraints: str = "",
    language: str = "verilog",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    board: str | None = None,
    target_mhz: float | None = None,
    max_attempts: int = 0,
    seeds: list[int] | None = None,
    strategies: list[str] | None = None,
    stop_when_met: bool = True,
    nextpnr_args: list[str] | None = None,
    work_dir: str | None = None,
    timeout: int = 300,
    backend: str = "yosys",
    return_bitstream_b64: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Sweep place-and-route configurations until the clock target is met.

    Source input (provide exactly one): code, files, or project_dir -- same as
    place_and_route, which this calls for each attempt.

    target_mhz: the clock to close on. Defaults to the board preset's clock.
                Without either, the sweep still runs and reports the best fmax
                found, but cannot say whether timing "closed".
    max_attempts: number of configurations to try. Defaults to 4 for nextpnr
                and 3 for Vivado, whose iterations cost minutes each.
    seeds: explicit nextpnr placer seeds; defaults to 1..max_attempts.
    strategies: explicit Vivado strategy names from VIVADO_STRATEGIES.
    stop_when_met: stop at the first attempt that meets the target (default).
                Set False to run every attempt and report the full spread.
    timeout: seconds *per attempt*, not for the whole sweep.

    Returns a dict where "success" means at least one attempt routed, and
    "closed" means the target was met (None when no target was known). An
    attempt that routes but misses timing is a result, not a failure: its
    bitstream is still returned so the design can be programmed and measured.
    """
    report = make_reporter(progress)

    if backend == "litex":
        return err(
            INVALID_INPUT,
            "close_timing does not support the LiteX backend; LiteX owns its "
            "own build flow. Use place_and_route with backend='litex'.",
        )

    preset = get_board_preset(board) if board else None
    if preset:
        target = target or preset["target"]
        device = device or preset["device"]
        package = package or preset.get("package", "")

    is_vivado = backend == "vivado" or target == "xilinx"

    effective_target = target_mhz
    if effective_target is None and preset and preset.get("clock_mhz"):
        effective_target = preset["clock_mhz"]

    # ---- build the attempt plan -----------------------------------------
    plan: list[dict]
    if is_vivado:
        if seeds:
            return err(
                INVALID_INPUT,
                "seeds apply to nextpnr targets. For Xilinx parts use "
                "`strategies` (Vivado has no placer-seed equivalent here).",
            )
        by_name = {s["name"]: s for s in VIVADO_STRATEGIES}
        if strategies:
            unknown = [n for n in strategies if n not in by_name]
            if unknown:
                return err(
                    INVALID_INPUT,
                    f"Unknown strategy name(s): {', '.join(unknown)}. "
                    f"Available: {', '.join(by_name)}.",
                )
            chosen = [by_name[n] for n in strategies]
        else:
            chosen = list(VIVADO_STRATEGIES)
        limit = max_attempts or min(DEFAULT_ATTEMPTS_VIVADO, len(chosen))
        plan = [dict(s) for s in chosen[:limit]]
    else:
        if strategies:
            return err(
                INVALID_INPUT,
                "strategies apply to Xilinx/Vivado targets. For nextpnr "
                "targets use `seeds`.",
            )
        if seeds:
            limit = max_attempts or len(seeds)
            plan = [{"seed": int(s)} for s in seeds[:limit]]
        else:
            limit = max_attempts or DEFAULT_ATTEMPTS_NEXTPNR
            # Check the budget before building the list: max_attempts is
            # caller-supplied and could be enormous.
            if limit * timeout > MAX_TOTAL_BUDGET_S:
                return _over_budget(limit, timeout)
            plan = [{"seed": n} for n in range(1, limit + 1)]

    if not plan:
        return err(INVALID_INPUT, "No attempts to run (max_attempts resolved to 0).")
    if len(plan) * timeout > MAX_TOTAL_BUDGET_S:
        return _over_budget(len(plan), timeout)

    # ---- a shared work_dir lets nextpnr attempts reuse one netlist -------
    sweep_dir = work_dir
    owns_dir = False
    if not is_vivado and sweep_dir is None:
        sweep_dir = str(data_root() / "timing" / uuid4().hex)
        os.makedirs(sweep_dir, exist_ok=True)
        owns_dir = True

    from fpgazero_mcp.tools.pnr import place_and_route

    attempts: list[dict] = []
    results: list[dict] = []
    closed = False
    best_metric: tuple | None = None

    for i, label in enumerate(plan):
        report(i / len(plan), f"timing attempt {i + 1}/{len(plan)}: {label}")

        kwargs: dict = dict(
            code=code,
            top_module=top_module,
            target=target,
            device=device,
            package=package,
            constraints=constraints,
            language=language,
            files=files,
            project_dir=project_dir,
            board=board,
            nextpnr_args=nextpnr_args,
            work_dir=sweep_dir,
            timeout=timeout,
            backend=backend,
            target_mhz=effective_target,
            allow_timing_fail=True,
            return_bitstream_b64=False,
        )
        if is_vivado:
            kwargs.update(
                synth_directive=label["synth_directive"],
                place_directive=label["place_directive"],
                route_directive=label["route_directive"],
                phys_opt=label["phys_opt"],
            )
            record_label = {"strategy": label["name"]}
        else:
            kwargs["seed"] = label["seed"]
            # The netlist does not depend on the placer seed, so synthesize on
            # the first attempt and reuse it for the rest. A caller-supplied
            # work_dir may already hold a usable netlist, so try to reuse from
            # attempt one too: place_and_route verifies the netlist's recorded
            # input fingerprint and re-synthesizes on any mismatch.
            kwargs["reuse_netlist"] = i > 0 or bool(work_dir)
            record_label = {"seed": label["seed"]}

        result = place_and_route(**kwargs)
        results.append(result)
        summary = _summarize(i + 1, record_label, result)
        attempts.append(summary)

        # Every attempt writes the same output file in the shared work_dir, so
        # a later, worse attempt would overwrite the bitstream of the winner.
        # Keep a copy of the best so far (same ranking, first wins on ties, as
        # the final pick below).
        if sweep_dir and result.get("bitstream_path"):
            metric = _attempt_metric(summary)
            if best_metric is None or metric > best_metric:
                src = result["bitstream_path"]
                dst = os.path.join(sweep_dir, "best" + os.path.splitext(src)[1])
                try:
                    shutil.copyfile(src, dst)
                except OSError:
                    pass
                else:
                    result["bitstream_path"] = dst
                    best_metric = metric

        # A toolchain that is absent or a design that will not synthesize will
        # fail identically on every attempt; stop rather than burn the budget.
        if result.get("error_code") in {
            "tool_not_found",
            "invalid_input",
            "path_security",
            "synthesis_failed",
        }:
            break

        # A run can log a passing fmax and still fail to route; that attempt
        # produced nothing usable, so it must not close (or stop) the sweep.
        if summary["routed"] and summary.get("meets_target"):
            closed = True
            if stop_when_met:
                break

    report(1.0, "sweep complete")

    best_idx = max(range(len(results)), key=lambda i: _attempt_metric(attempts[i]))
    best_result = results[best_idx]
    best_summary = attempts[best_idx]
    routed_any = any(a["routed"] for a in attempts)

    out: dict = {
        "success": routed_any,
        "stage": "close_timing",
        "backend": "vivado" if is_vivado else "yosys",
        "target": target,
        "device": device,
        "top_module": top_module,
        "attempts_run": len(attempts),
        "attempts_planned": len(plan),
        "attempts": attempts,
        "best_attempt": best_summary["attempt"],
        "best": {k: v for k, v in best_summary.items() if k != "attempt"},
        "closed": closed if effective_target else None,
    }
    if effective_target:
        out["target_mhz"] = effective_target
    if board:
        out["board"] = board
    if sweep_dir:
        out["work_dir"] = sweep_dir
        if owns_dir:
            out["work_dir_note"] = (
                "Created by this sweep; reuse it as work_dir to skip synthesis."
            )

    achieved = (best_summary.get("timing") or {}).get("max_freq_mhz")
    if achieved is not None:
        out["achieved_mhz"] = achieved
        if effective_target:
            out["margin_mhz"] = round(achieved - effective_target, 2)

    # Carry the usable outputs of the winning attempt.
    for key in ("bitstream_path", "bitstream_ext", "constraints", "utilization"):
        if best_result.get(key) is not None:
            out[key] = best_result[key]
    if return_bitstream_b64 and best_result.get("bitstream_path"):
        import base64

        try:
            with open(best_result["bitstream_path"], "rb") as bf:
                out["bitstream_b64"] = base64.b64encode(bf.read()).decode("ascii")
        except OSError:
            pass

    if not routed_any:
        out["error"] = best_result.get("error", "No attempt completed place-and-route.")
        out["error_code"] = best_result.get("error_code", "pnr_failed")
    elif effective_target and not closed:
        spread: list[float] = []
        for a in attempts:
            value = a.get("max_freq_mhz")
            if value is not None:
                spread.append(float(value))
        out["summary"] = (
            f"Did not close {effective_target} MHz in {len(attempts)} attempt(s); "
            f"best was {achieved} MHz"
            + (f" (range {min(spread)}-{max(spread)} MHz). " if spread else ". ")
            + (
                "Try more strategies, or reduce the clock."
                if is_vivado
                else "Try more seeds, pipeline the critical path, or reduce the clock."
            )
        )
    elif closed:
        out["summary"] = (
            f"Closed {effective_target} MHz on attempt {best_summary['attempt']} "
            f"at {achieved} MHz."
        )

    return out
