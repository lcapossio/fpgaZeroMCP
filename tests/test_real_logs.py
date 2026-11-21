"""build_parser against real, captured tool logs.

The other parser tests use hand-written excerpts, which only prove the
regexes match what the test author expected. These fixtures are real output
(hostname and paths scrubbed), so they catch formats nobody anticipated:
the ``WNS=6.361.`` crash and the phase thrashing both only showed up here.

Fixtures (tests/fixtures/logs/):
  vivado_2025.2_uart_tx_impl.log    -- non-project batch flow, uart_tx on
      xc7a35t: synth_design, opt_design, place_design, route_design,
      report_utilization, report_timing_summary. 10 ns clock, met.
  vivado_2025.2_opt_design_error.log -- the same flow dying in opt_design
      with ERROR [Synth 20-411] (stale .Xil state).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpgazero_mcp.tools.build_parser import BuildLogParser, parse_build_log

LOGS = Path(__file__).parent / "fixtures" / "logs"


def _read(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def impl_log() -> str:
    return _read("vivado_2025.2_uart_tx_impl.log")


@pytest.fixture(scope="module")
def impl(impl_log: str) -> dict:
    return parse_build_log(impl_log)


class TestVivadoImplLog:
    def test_tool(self, impl: dict) -> None:
        assert impl["tool"] == "vivado"

    def test_phase_sequence_follows_commands(self, impl: dict) -> None:
        # One entry per command. Sub-phase names ("... Physical Synthesis",
        # "Post Placement Optimization", "Intermediate Timing Summary") used
        # to bounce this through 36 transitions.
        assert impl["phase_history"] == [
            "vivado_synth",
            "vivado_opt",
            "vivado_place",
            "vivado_route",
            "vivado_timing",
        ]
        assert impl["phase"] == "vivado_timing"

    def test_phase_line_counts_cover_log(self, impl: dict, impl_log: str) -> None:
        detail = impl["phase_detail"]
        assert [d["phase"] for d in detail] == impl["phase_history"]
        assert all(d["lines"] > 0 for d in detail)
        # Lines before the first phase (banner, read_verilog) aren't counted.
        first_phase_line = next(
            i
            for i, line in enumerate(impl_log.splitlines())
            if line.startswith("# synth_design")
        )
        assert sum(d["lines"] for d in detail) == (
            len(impl_log.splitlines()) - first_phase_line
        )

    def test_utilization(self, impl: dict) -> None:
        u = impl["utilization"]
        assert u["luts"] == {"used": 24, "total": 20800, "pct": 0.1}
        assert u["ffs"] == {"used": 26, "total": 41600, "pct": 0.1}
        assert u["brams"]["used"] == 0 and u["brams"]["total"] == 50
        assert u["dsps"] == {"used": 0, "total": 90, "pct": 0.0}
        assert u["ios"]["total"] == 106

    def test_slack_is_final_routed_value(self, impl: dict) -> None:
        # The log has WNS=6.361 (placement estimates) before the routed
        # 6.667; the last value wins.
        assert impl["slack"] == {
            "wns_ns": 6.667,
            "tns_ns": 0.0,
            "whs_ns": 0.168,
            "ths_ns": 0.0,
        }

    def test_counts_and_health(self, impl: dict) -> None:
        assert impl["warnings"] == 16
        assert impl["errors"] == 0
        assert impl["health"]["status"] == "ok"

    @pytest.mark.parametrize("lines_per_feed", [1, 7, 100, 10_000])
    def test_incremental_feed_matches_whole(
        self, impl: dict, impl_log: str, lines_per_feed: int
    ) -> None:
        # build_status feeds whole lines in poll-sized batches.
        lines = impl_log.splitlines(keepends=True)
        p = BuildLogParser()
        for i in range(0, len(lines), lines_per_feed):
            p.feed("".join(lines[i : i + lines_per_feed]))
        assert p.result() == impl


class TestVivadoErrorLog:
    @pytest.fixture(scope="class")
    def result(self) -> dict:
        return parse_build_log(_read("vivado_2025.2_opt_design_error.log"))

    def test_stops_in_failing_command(self, result: dict) -> None:
        assert result["tool"] == "vivado"
        assert result["phase_history"] == ["vivado_synth", "vivado_opt"]
        assert result["phase"] == "vivado_opt"

    def test_error_reported(self, result: dict) -> None:
        assert result["errors"] == 1
        assert result["warnings"] == 1
        assert result["health"]["status"] == "error"
        assert result["utilization"] == {}
