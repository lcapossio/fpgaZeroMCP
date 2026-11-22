# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for the timing-closure sweep.

The sweep is mocked at the place_and_route boundary: each test supplies a
sequence of canned attempt results, so the loop logic (early exit, best-of-N
selection, netlist reuse, budget guards) is tested without a toolchain.

The nextpnr log fixtures are real nextpnr output lines, including the
timing-failure form, because the whole sweep depends on an attempt that misses
timing still reporting its achieved fmax rather than being dropped.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from fpgazero_mcp.tools import timing as timing_mod
from fpgazero_mcp.tools.timing import (
    MAX_TOTAL_BUDGET_S,
    VIVADO_STRATEGIES,
    _attempt_metric,
    close_timing,
)

# Real nextpnr tail, timing met.
NEXTPNR_PASS = """\
Info: Critical path report for clock 'clk' (posedge -> posedge):
Info: Max frequency for clock 'clk': 68.42 MHz (PASS at 12.00 MHz)
Info: Max delay posedge clk -> <async>: 3.21 ns
Info: Program finished normally.
"""

# Real nextpnr tail when the constraint is missed. --timing-allow-fail is what
# keeps this from being a hard error, and the fmax line is still present.
NEXTPNR_FAIL = """\
Info: Critical path report for clock 'clk' (posedge -> posedge):
Info: Max frequency for clock 'clk': 44.12 MHz (FAIL at 48.00 MHz)
Warning: Max frequency for clock 'clk' is lower than constraint.
Info: Program finished normally.
"""


def _attempt(fmax=None, routed=True, wns=None, **extra):
    """Build a canned place_and_route result."""
    t = {}
    if fmax is not None:
        t["max_freq_mhz"] = fmax
    if wns is not None:
        t["wns_ns"] = wns
    r = {
        "success": routed,
        "routed": routed,
        "timing": t,
        "utilization": {"lut_used": 100},
        "bitstream_path": "/out/design.bin" if routed else None,
    }
    r.update(extra)
    return r


def _mock_pnr(monkeypatch, results):
    """Patch place_and_route to return `results` in order; record call kwargs."""
    calls: list[dict] = []
    seq = list(results)

    def fake(**kwargs):
        calls.append(kwargs)
        target = kwargs.get("target_mhz")
        res = seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]
        res = dict(res)
        fmax = (res.get("timing") or {}).get("max_freq_mhz")
        if target and fmax is not None:
            res["timing"] = {**res["timing"], "target_mhz": target}
            res["timing"]["meets_target"] = fmax >= target
        return res

    monkeypatch.setattr("fpgazero_mcp.tools.pnr.place_and_route", fake, raising=True)
    return calls


@pytest.fixture
def scratch_dir():
    """pytest.ini disables tmp_path (-p no:tmpdir)."""
    path = tempfile.mkdtemp(prefix="pytest_timing_")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --- real nextpnr log parsing --------------------------------------------


class TestNextpnrFailLogIsUsable:
    """A timing-failing attempt must still yield a comparable fmax."""

    def test_fail_log_still_reports_fmax(self) -> None:
        from fpgazero_mcp.tools.pnr import _parse_timing

        t = _parse_timing(NEXTPNR_FAIL)
        assert t["max_freq_mhz"] == 44.12

    def test_pass_log_reports_fmax(self) -> None:
        from fpgazero_mcp.tools.pnr import _parse_timing

        t = _parse_timing(NEXTPNR_PASS)
        assert t["max_freq_mhz"] == 68.42


class TestAttemptMetric:
    def test_unrouted_ranks_below_everything(self) -> None:
        assert _attempt_metric({"routed": False}) < _attempt_metric(
            {"routed": True, "timing": {"wns_ns": -5.0}}
        )

    def test_fmax_preferred_over_wns(self) -> None:
        assert _attempt_metric(
            {"routed": True, "timing": {"max_freq_mhz": 1.0}}
        ) > _attempt_metric({"routed": True, "timing": {"wns_ns": 99.0}})

    def test_higher_fmax_wins(self) -> None:
        lo = {"routed": True, "timing": {"max_freq_mhz": 44.0}}
        hi = {"routed": True, "timing": {"max_freq_mhz": 55.0}}
        assert _attempt_metric(hi) > _attempt_metric(lo)

    def test_higher_wns_wins_when_no_fmax(self) -> None:
        worse = {"routed": True, "timing": {"wns_ns": -1.5}}
        better = {"routed": True, "timing": {"wns_ns": 0.3}}
        assert _attempt_metric(better) > _attempt_metric(worse)


# --- input validation -----------------------------------------------------


class TestValidation:
    def test_litex_rejected(self) -> None:
        r = close_timing(code="x", top_module="top", backend="litex")
        assert r["error_code"] == "invalid_input"
        assert "LiteX" in r["error"]

    def test_budget_guard(self) -> None:
        r = close_timing(
            code="x",
            top_module="top",
            board="icebreaker",
            max_attempts=20,
            timeout=600,
        )
        assert r["error_code"] == "invalid_input"
        assert str(MAX_TOTAL_BUDGET_S) in r["error"]

    def test_seeds_rejected_for_vivado(self) -> None:
        r = close_timing(code="x", top_module="top", target="xilinx", seeds=[1, 2])
        assert r["error_code"] == "invalid_input"
        assert "strategies" in r["error"]

    def test_strategies_rejected_for_nextpnr(self) -> None:
        r = close_timing(
            code="x", top_module="top", board="icebreaker", strategies=["explore"]
        )
        assert r["error_code"] == "invalid_input"
        assert "seeds" in r["error"]

    def test_unknown_strategy_name(self) -> None:
        r = close_timing(
            code="x", top_module="top", target="xilinx", strategies=["turbo"]
        )
        assert r["error_code"] == "invalid_input"
        assert "turbo" in r["error"]


# --- sweep behaviour ------------------------------------------------------


class TestSweep:
    def test_stops_at_first_attempt_meeting_target(self, monkeypatch) -> None:
        calls = _mock_pnr(
            monkeypatch,
            [_attempt(fmax=44.0), _attempt(fmax=50.0), _attempt(fmax=60.0)],
        )
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=3,
        )
        assert len(calls) == 2, "should stop once the target is met"
        assert r["closed"] is True
        assert r["best_attempt"] == 2
        assert r["achieved_mhz"] == 50.0
        assert r["margin_mhz"] == 2.0
        assert "Closed 48.0 MHz on attempt 2" in r["summary"]

    def test_stop_when_met_false_runs_every_attempt(self, monkeypatch) -> None:
        calls = _mock_pnr(
            monkeypatch,
            [_attempt(fmax=50.0), _attempt(fmax=70.0), _attempt(fmax=55.0)],
        )
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=3,
            stop_when_met=False,
        )
        assert len(calls) == 3
        assert r["best_attempt"] == 2
        assert r["achieved_mhz"] == 70.0

    def test_missed_target_returns_best_effort_not_failure(self, monkeypatch) -> None:
        _mock_pnr(
            monkeypatch,
            [_attempt(fmax=44.12), _attempt(fmax=46.0), _attempt(fmax=45.1)],
        )
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=3,
        )
        # Routed but short of target: a usable result, not an error.
        assert r["success"] is True
        assert r["closed"] is False
        assert r["achieved_mhz"] == 46.0
        assert r["margin_mhz"] == -2.0
        assert r["bitstream_path"] == "/out/design.bin"
        assert "Did not close 48.0 MHz" in r["summary"]
        assert "range 44.12-46.0 MHz" in r["summary"]

    def test_all_attempts_recorded_with_their_fmax(self, monkeypatch) -> None:
        _mock_pnr(monkeypatch, [_attempt(fmax=40.0), _attempt(fmax=41.0)])
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=2,
        )
        assert [a["max_freq_mhz"] for a in r["attempts"]] == [40.0, 41.0]
        assert [a["seed"] for a in r["attempts"]] == [1, 2]
        assert all(a["meets_target"] is False for a in r["attempts"])

    def test_no_target_means_closed_is_none(self, monkeypatch) -> None:
        _mock_pnr(monkeypatch, [_attempt(fmax=40.0), _attempt(fmax=61.0)])
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            max_attempts=2,
            stop_when_met=False,
        )
        assert r["closed"] is None
        assert "target_mhz" not in r
        assert r["achieved_mhz"] == 61.0
        assert r["success"] is True

    def test_board_preset_supplies_the_target(self, monkeypatch) -> None:
        calls = _mock_pnr(monkeypatch, [_attempt(fmax=5.0)])
        r = close_timing(code="x", top_module="top", board="icebreaker", max_attempts=1)
        assert r["target_mhz"] == calls[0]["target_mhz"]
        assert r["target_mhz"] > 0

    def test_all_attempts_unrouted_is_a_failure(self, monkeypatch) -> None:
        _mock_pnr(
            monkeypatch,
            [
                _attempt(routed=False, error="routing failed", error_code="pnr_failed"),
                _attempt(routed=False, error="routing failed", error_code="pnr_failed"),
            ],
        )
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=2,
        )
        assert r["success"] is False
        assert r["error_code"] == "pnr_failed"

    def test_aborts_early_on_unrecoverable_error(self, monkeypatch) -> None:
        """A missing toolchain fails identically every time; don't burn the budget."""
        calls = _mock_pnr(
            monkeypatch,
            [
                _attempt(
                    routed=False,
                    error="'nextpnr-ice40' not found.",
                    error_code="tool_not_found",
                )
            ],
        )
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=4,
        )
        assert len(calls) == 1
        assert r["attempts_run"] == 1
        assert r["attempts_planned"] == 4
        assert r["error_code"] == "tool_not_found"

    def test_synthesis_failure_aborts_early(self, monkeypatch) -> None:
        calls = _mock_pnr(
            monkeypatch,
            [_attempt(routed=False, error="bad HDL", error_code="synthesis_failed")],
        )
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=48.0,
            max_attempts=4,
        )
        assert len(calls) == 1


class TestNetlistReuse:
    def test_synthesizes_once_then_reuses(self, monkeypatch) -> None:
        calls = _mock_pnr(
            monkeypatch, [_attempt(fmax=40.0), _attempt(fmax=41.0), _attempt(fmax=42.0)]
        )
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=99.0,
            max_attempts=3,
        )
        assert [c["reuse_netlist"] for c in calls] == [False, True, True]
        # Every attempt must share one work_dir for the reuse to be possible.
        dirs = {c["work_dir"] for c in calls}
        assert len(dirs) == 1
        assert next(iter(dirs))

    def test_explicit_work_dir_is_used_as_given(self, monkeypatch, scratch_dir) -> None:
        calls = _mock_pnr(monkeypatch, [_attempt(fmax=40.0)])
        r = close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=99.0,
            max_attempts=1,
            work_dir=str(scratch_dir),
        )
        assert calls[0]["work_dir"] == str(scratch_dir)
        assert r["work_dir"] == str(scratch_dir)
        assert "work_dir_note" not in r

    def test_supplied_work_dir_attempts_reuse_from_the_first_attempt(
        self, monkeypatch, scratch_dir
    ) -> None:
        """A caller-supplied work_dir may already hold a valid netlist.

        place_and_route verifies the fingerprint and re-synthesizes on a
        mismatch, so asking for reuse on attempt one is safe.
        """
        calls = _mock_pnr(monkeypatch, [_attempt(fmax=40.0), _attempt(fmax=41.0)])
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=999.0,
            max_attempts=2,
            work_dir=str(scratch_dir),
        )
        assert [c["reuse_netlist"] for c in calls] == [True, True]

    def test_sweep_dir_is_outside_the_package(self, monkeypatch) -> None:
        import fpgazero_mcp

        pkg = Path(fpgazero_mcp.__file__).resolve().parent
        calls = _mock_pnr(monkeypatch, [_attempt(fmax=40.0)])
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=99.0,
            max_attempts=1,
        )
        assert not Path(calls[0]["work_dir"]).resolve().is_relative_to(pkg)

    def test_every_attempt_allows_timing_fail(self, monkeypatch) -> None:
        """Otherwise a near miss returns no fmax and the attempts are incomparable."""
        calls = _mock_pnr(monkeypatch, [_attempt(fmax=40.0), _attempt(fmax=41.0)])
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=99.0,
            max_attempts=2,
        )
        assert all(c["allow_timing_fail"] is True for c in calls)


class TestVivadoSweep:
    def test_iterates_strategies_not_seeds(self, monkeypatch) -> None:
        calls = _mock_pnr(
            monkeypatch, [_attempt(wns=-1.0), _attempt(wns=-0.2), _attempt(wns=0.4)]
        )
        r = close_timing(
            code="x",
            top_module="top",
            target="xilinx",
            device="xc7a35tcpg236-1",
            max_attempts=3,
            stop_when_met=False,
        )
        assert [c["place_directive"] for c in calls] == [
            s["place_directive"] for s in VIVADO_STRATEGIES[:3]
        ]
        assert all("seed" not in c for c in calls)
        assert [a["strategy"] for a in r["attempts"]] == [
            s["name"] for s in VIVADO_STRATEGIES[:3]
        ]
        # No fmax available without a clock period: rank on WNS instead.
        assert r["best_attempt"] == 3

    def test_default_attempt_count_is_lower_for_vivado(self, monkeypatch) -> None:
        calls = _mock_pnr(monkeypatch, [_attempt(wns=-1.0)] * 5)
        close_timing(
            code="x",
            top_module="top",
            target="xilinx",
            device="xc7a35tcpg236-1",
            stop_when_met=False,
        )
        assert len(calls) == timing_mod.DEFAULT_ATTEMPTS_VIVADO

    def test_default_attempt_count_for_nextpnr(self, monkeypatch) -> None:
        calls = _mock_pnr(monkeypatch, [_attempt(fmax=1.0)] * 9)
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=999.0,
        )
        assert len(calls) == timing_mod.DEFAULT_ATTEMPTS_NEXTPNR

    def test_explicit_strategy_subset(self, monkeypatch) -> None:
        calls = _mock_pnr(monkeypatch, [_attempt(wns=0.1)] * 2)
        close_timing(
            code="x",
            top_module="top",
            target="xilinx",
            device="xc7a35tcpg236-1",
            strategies=["explore", "default"],
            stop_when_met=False,
        )
        assert [c["route_directive"] for c in calls] == [
            "AggressiveExplore",
            "default",
        ]

    def test_vivado_sweep_does_not_invent_a_work_dir(self, monkeypatch) -> None:
        """Vivado directives change synthesis, so there is no netlist to reuse."""
        calls = _mock_pnr(monkeypatch, [_attempt(wns=0.1)])
        close_timing(
            code="x",
            top_module="top",
            target="xilinx",
            device="xc7a35tcpg236-1",
            max_attempts=1,
        )
        assert calls[0]["work_dir"] is None


class TestProgress:
    def test_progress_reported_per_attempt(self, monkeypatch) -> None:
        _mock_pnr(monkeypatch, [_attempt(fmax=1.0)] * 3)
        seen: list[tuple[float, str]] = []
        close_timing(
            code="x",
            top_module="top",
            target="ice40",
            device="up5k",
            target_mhz=999.0,
            max_attempts=3,
            progress=lambda f, m: seen.append((f, m)),
        )
        assert len(seen) >= 4  # three attempts plus completion
        assert all(0.0 <= f <= 1.0 for f, _ in seen)
        assert seen[-1][0] == 1.0


# --- Vivado TCL directive generation -------------------------------------


class TestVivadoTcl:
    def test_default_directives_emit_the_original_script(self) -> None:
        from fpgazero_mcp.tools.vivado import _generate_tcl

        tcl = _generate_tcl(["/x/a.v"], "verilog", "top", "xc7a35t", None, "/x/a.bit")
        assert "-directive" not in tcl
        assert "phys_opt_design" not in tcl
        assert "place_design\n" in tcl
        assert "route_design\n" in tcl

    def test_directives_are_injected(self) -> None:
        from fpgazero_mcp.tools.vivado import _generate_tcl

        tcl = _generate_tcl(
            ["/x/a.v"],
            "verilog",
            "top",
            "xc7a35t",
            None,
            "/x/a.bit",
            synth_directive="PerformanceOptimized",
            place_directive="Explore",
            route_directive="AggressiveExplore",
            phys_opt=True,
        )
        assert (
            "synth_design -top top -part xc7a35t -directive PerformanceOptimized" in tcl
        )
        assert "place_design -directive Explore" in tcl
        assert "route_design -directive AggressiveExplore" in tcl
        # phys_opt runs between placement and routing.
        assert tcl.index("place_design") < tcl.index("phys_opt_design")
        assert tcl.index("phys_opt_design") < tcl.index("route_design")

    def test_bad_directive_rejected(self, monkeypatch) -> None:
        from fpgazero_mcp.tools.vivado import vivado_place_and_route

        monkeypatch.setattr(shutil, "which", lambda _b: "/usr/bin/vivado")
        r = vivado_place_and_route(
            code="module top; endmodule",
            top_module="top",
            part="xc7a35tcpg236-1",
            route_directive="GoFaster",
        )
        assert r["error_code"] == "invalid_input"
        assert "GoFaster" in r["error"]


class TestPnrSeedArgs:
    def test_seed_and_allow_fail_reach_nextpnr(self, monkeypatch, scratch_dir) -> None:
        """The flags the sweep depends on must actually land on the command line."""
        from fpgazero_mcp.tools import pnr as pnr_mod

        captured: list[list[str]] = []

        class _P:
            def __init__(self, rc=0, out=""):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[0] == "yosys":
                # Produce the netlist the next stage looks for.
                idx = cmd.index("-s")
                script = Path(cmd[idx + 1]).read_text(encoding="utf-8")
                for tok in script.split():
                    if tok.endswith("netlist.json"):
                        Path(tok).write_text("{}", encoding="utf-8")
                return _P(0, "")
            return _P(0, NEXTPNR_FAIL)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", str(scratch_dir))

        r = pnr_mod.place_and_route(
            code="module top(input clk); endmodule",
            top_module="top",
            target="ice40",
            device="up5k",
            package="sg48",
            seed=7,
            allow_timing_fail=True,
            target_mhz=48.0,
            work_dir=str(scratch_dir),
        )
        pnr_cmd = captured[-1]
        assert "--seed" in pnr_cmd and "7" in pnr_cmd
        assert "--timing-allow-fail" in pnr_cmd
        # The FAIL log must still yield a comparable fmax and a verdict.
        assert r["timing"]["max_freq_mhz"] == 44.12
        assert r["timing"]["target_mhz"] == 48.0
        assert r["timing"]["meets_target"] is False
        assert r["seed"] == 7

    def test_reuse_netlist_requires_work_dir(self) -> None:
        from fpgazero_mcp.tools.pnr import place_and_route

        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="up5k",
            reuse_netlist=True,
        )
        assert r["error_code"] == "invalid_input"
        assert "work_dir" in r["error"]

    def test_vivado_directives_rejected_on_nextpnr_target(self) -> None:
        from fpgazero_mcp.tools.pnr import place_and_route

        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="up5k",
            route_directive="Explore",
        )
        assert r["error_code"] == "invalid_input"
        assert "Vivado-only" in r["error"]

    @staticmethod
    def _fake_yosys_runner(captured, pnr_log):
        """subprocess.run stand-in: yosys writes a netlist, nextpnr returns a log."""

        class _P:
            def __init__(self, rc=0, out=""):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[0] == "yosys":
                idx = cmd.index("-s")
                script = Path(cmd[idx + 1]).read_text(encoding="utf-8")
                for tok in script.split():
                    if tok.endswith("netlist.json"):
                        Path(tok).write_text("{}", encoding="utf-8")
                return _P(0, "")
            return _P(0, pnr_log)

        return fake_run

    def _run(self, monkeypatch, scratch_dir, captured, code, **kwargs):
        from fpgazero_mcp.tools import pnr as pnr_mod

        monkeypatch.setattr(
            subprocess, "run", self._fake_yosys_runner(captured, NEXTPNR_PASS)
        )
        monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", str(scratch_dir))
        return pnr_mod.place_and_route(
            code=code,
            top_module="top",
            target="ice40",
            device="up5k",
            package="sg48",
            work_dir=str(scratch_dir),
            **kwargs,
        )

    DESIGN_A = "module top(input clk); endmodule"
    DESIGN_B = "module top(input clk, input rst); endmodule"

    def test_reuse_netlist_skips_yosys(self, monkeypatch, scratch_dir) -> None:
        """After a run that synthesized, a reuse run must not invoke yosys."""
        first: list[list[str]] = []
        self._run(monkeypatch, scratch_dir, first, self.DESIGN_A)
        assert any(c[0] == "yosys" for c in first), "first run should synthesize"

        second: list[list[str]] = []
        r = self._run(
            monkeypatch, scratch_dir, second, self.DESIGN_A, reuse_netlist=True
        )
        assert not any(c[0] == "yosys" for c in second), "yosys should be skipped"
        assert r["netlist_reused"] is True

    def test_reuse_refuses_netlist_it_did_not_produce(
        self, monkeypatch, scratch_dir
    ) -> None:
        """A bare netlist.json has no input fingerprint, so it cannot be trusted."""
        (scratch_dir / "netlist.json").write_text("{}", encoding="utf-8")
        captured: list[list[str]] = []
        r = self._run(
            monkeypatch, scratch_dir, captured, self.DESIGN_A, reuse_netlist=True
        )
        assert any(c[0] == "yosys" for c in captured), "should re-synthesize"
        assert r["netlist_reused"] is False
        assert "fingerprint" in r["netlist_reuse_skipped"]

    def test_reuse_refuses_stale_netlist_after_source_edit(
        self, monkeypatch, scratch_dir
    ) -> None:
        """The footgun: edit the HDL, reuse the old work_dir, get the old netlist."""
        self._run(monkeypatch, scratch_dir, [], self.DESIGN_A)

        captured: list[list[str]] = []
        r = self._run(
            monkeypatch, scratch_dir, captured, self.DESIGN_B, reuse_netlist=True
        )
        assert any(c[0] == "yosys" for c in captured), "edited sources must re-synth"
        assert r["netlist_reused"] is False
        assert r["netlist_reuse_skipped"] == "sources or synthesis options changed"

    def test_reuse_refuses_when_synthesis_options_change(
        self, monkeypatch, scratch_dir
    ) -> None:
        """A different top module means a different netlist, same sources or not."""
        from fpgazero_mcp.tools import pnr as pnr_mod

        self._run(monkeypatch, scratch_dir, [], self.DESIGN_A)

        captured: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run", self._fake_yosys_runner(captured, NEXTPNR_PASS)
        )
        monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", str(scratch_dir))
        r = pnr_mod.place_and_route(
            code=self.DESIGN_A,
            top_module="top",
            target="ice40",
            device="up5k",
            package="sg48",
            work_dir=str(scratch_dir),
            reuse_netlist=True,
            # A different synthesis target changes the generated .ys script.
            nextpnr_args=None,
            language="systemverilog",
        )
        assert any(c[0] == "yosys" for c in captured)
        assert r["netlist_reused"] is False


# --- integration ----------------------------------------------------------


def _have(*bins: str) -> bool:
    return all(shutil.which(b) for b in bins)


# Inputs are registered so there is a clk-to-clk path: without one nextpnr
# prints no "Max frequency" line and a sweep has nothing to rank or close on.
ICE40_DESIGN = """\
module top(input wire clk, input wire [7:0] a, input wire [7:0] b,
           output reg [15:0] y);
  reg [7:0] ra, rb;
  always @(posedge clk) begin
    ra <= a;
    rb <= b;
    y <= ra * rb + {ra[3:0], rb[3:0]};
  end
endmodule
"""

# Only the clock is pinned; nextpnr-ice40 rejects a PCF that leaves IOs
# unconstrained unless told to place them itself.
ICE40_PCF = "set_io clk 35\n"
ICE40_ARGS = ["--pcf-allow-unconstrained"]


@pytest.mark.integration
def test_sweep_runs_real_seeds_and_reuses_netlist() -> None:
    """A real sweep must run every seed and synthesize only once."""
    if not _have("yosys", "nextpnr-ice40"):
        pytest.skip("yosys/nextpnr-ice40 not installed")
    r = close_timing(
        code=ICE40_DESIGN,
        top_module="top",
        target="ice40",
        device="up5k",
        package="sg48",
        constraints=ICE40_PCF,
        nextpnr_args=ICE40_ARGS,
        # Unreachably high, so no attempt short-circuits the sweep.
        target_mhz=5000.0,
        max_attempts=2,
        timeout=300,
    )
    assert r["success"] is True, r.get("error")
    assert r["attempts_run"] == 2
    assert r["closed"] is False
    # Each attempt must report an fmax even though all of them miss the target;
    # this is what --timing-allow-fail buys us.
    assert all(a["max_freq_mhz"] is not None for a in r["attempts"]), r["attempts"]
    assert r["achieved_mhz"] > 0
    # Attempt 1 synthesizes, attempt 2 reuses.
    assert r["attempts"][1].get("netlist_reused") is True


@pytest.mark.integration
def test_sweep_closes_a_trivial_target() -> None:
    """A target the design clears easily must report closed on the first attempt."""
    if not _have("yosys", "nextpnr-ice40"):
        pytest.skip("yosys/nextpnr-ice40 not installed")
    r = close_timing(
        code=ICE40_DESIGN,
        top_module="top",
        target="ice40",
        device="up5k",
        package="sg48",
        constraints=ICE40_PCF,
        nextpnr_args=ICE40_ARGS,
        target_mhz=1.0,
        max_attempts=4,
        timeout=300,
    )
    assert r["closed"] is True, r.get("summary") or r.get("error")
    assert r["attempts_run"] == 1, "should stop at the first passing attempt"
    assert r["bitstream_path"]
