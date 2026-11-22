# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Incremental build-log parsing (build_status) and JSON-RPC batch handling."""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from fpgazero_mcp.tools.build_manager import BuildRecord
from fpgazero_mcp.tools.build_parser import BuildLogParser, parse_build_log

VIVADO_LOG = """\
****** Vivado v2024.1 (64-bit)
Command: synth_design -top top -part xc7a35tcpg236-1
Starting synth_design
WARNING: [Synth 8-7129] Port a in module b is either unconnected or has no load
INFO: [Synth 8-6157] synthesizing module 'top'
| Slice LUTs                 |  1234 |     0 |     20800 |  5.93 |
| Slice Registers            |  2345 |     0 |     41600 |  5.64 |
Starting Placer Task
Phase 1.1 Placer Initialization
CRITICAL WARNING: [Place 30-1] something odd
Phase 2.3 Global Placement
WARNING: [Place 30-2] another one
Starting Router
Phase 4.1 Global Routing
| Design Timing Summary
| Post Routing | WNS=-0.123 | TNS=-4.500 | WHS=0.051 | THS=0.000 |
ERROR: [Route 35-1] routing failed
write_bitstream -force top.bit
"""

YOSYS_NEXTPNR_LOG = """\
Yosys 0.40 (git sha1 abc)
Executing HIERARCHY pass
Warning: Replacing memory
Executing OPT pass
Executing SYNTH_ICE40 pass
Executing WRITE_JSON pass
Info: Device: ice40
Info: Packing constants..
Info: Placed 120 cells
   ICESTORM_LC:   500/ 1280    39%
   SB_IO:          10/  112     8%
Info: Routing..
Info: Max frequency for clock 'clk': 75.20 MHz (PASS at 12.00 MHz)
ERROR: something broke
"""


def _feed_in_pieces(text: str, rng: random.Random) -> dict:
    """Feed text split at random line boundaries; return the result."""
    lines = text.splitlines(keepends=True)
    parser = BuildLogParser()
    i = 0
    while i < len(lines):
        n = rng.randint(1, 4)
        parser.feed("".join(lines[i : i + n]))
        i += n
    return parser.result()


class TestBuildLogParser:
    @pytest.mark.parametrize("log", [VIVADO_LOG, YOSYS_NEXTPNR_LOG, ""])
    def test_pieces_match_whole(self, log: str) -> None:
        whole = parse_build_log(log)
        rng = random.Random(1234)
        for _ in range(20):
            assert _feed_in_pieces(log, rng) == whole

    def test_result_does_not_disturb_state(self) -> None:
        parser = BuildLogParser()
        lines = VIVADO_LOG.splitlines(keepends=True)
        half = len(lines) // 2
        parser.feed("".join(lines[:half]))
        mid = parser.result()
        # Mutating a handed-out result must not leak into the parser.
        mid["utilization"]["luts"]["used"] = -1
        mid["phase_detail"].clear()
        parser.feed("".join(lines[half:]))
        assert parser.result() == parse_build_log(VIVADO_LOG)
        assert parser.result() == parser.result()

    def test_phase_line_counts(self) -> None:
        log = (
            "Yosys 0.40\n"  # 0: no phase yet
            "Executing HIERARCHY pass\n"  # 1: elaboration starts
            "a\nb\nc\n"
            "Executing OPT pass\n"  # 5: optimization starts
            "d\n"
            "Executing WRITE_JSON pass\n"  # 7: write_output starts
            "e\nf\n"
        )
        detail = parse_build_log(log)["phase_detail"]
        assert detail == [
            {"phase": "yosys_elaboration", "lines": 4},
            {"phase": "yosys_optimization", "lines": 2},
            {"phase": "yosys_write_output", "lines": 3},
        ]

    def test_counts_and_detection(self) -> None:
        r = parse_build_log(VIVADO_LOG)
        assert r["tool"] == "vivado"
        assert r["warnings"] == 2
        assert r["errors"] == 2  # ERROR + CRITICAL WARNING
        assert r["slack"]["wns_ns"] == -0.123

        r = parse_build_log(YOSYS_NEXTPNR_LOG)
        assert r["tool"] == "yosys"
        assert r["tools_detected"] == ["nextpnr", "yosys"]


# ---------------------------------------------------------------------------
# BuildRecord.build_info — parses only new log output on each poll
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_path():
    """pytest.ini disables the built-in tmp_path (-p no:tmpdir)."""
    d = Path(tempfile.mkdtemp(prefix="pytest_incr_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _record(tmp_path: Path) -> tuple[BuildRecord, Path]:
    log = tmp_path / "b.log"
    log.write_bytes(b"")
    rec = BuildRecord(
        build_id="x", label="", cmd=[], log_path=str(log), work_dir=".", start_time=0.0
    )
    return rec, log


def _append(log: Path, text: str) -> None:
    with open(log, "ab") as f:
        f.write(text.encode("utf-8"))


class TestBuildRecordIncremental:
    def test_polls_accumulate_to_whole_log_result(self, tmp_path: Path) -> None:
        rec, log = _record(tmp_path)
        lines = VIVADO_LOG.splitlines(keepends=True)
        for line in lines:
            _append(log, line)
            rec.build_info()
            rec.build_info()  # a poll with nothing new must not double count
        assert rec.build_info() == parse_build_log(VIVADO_LOG)

    def test_partial_line_waits_for_newline_while_running(self, tmp_path: Path) -> None:
        rec, log = _record(tmp_path)
        _append(log, "****** Vivado v2024.1\nWARNING: [X 1-1] half")
        assert rec.build_info()["warnings"] == 0
        _append(log, " a line\nWARNING: [X 1-2] tail without newline")
        assert rec.build_info()["warnings"] == 1
        # Once the build has finished the unterminated last line counts too.
        rec.returncode = 0
        assert rec.build_info()["warnings"] == 2
        assert rec.build_info()["warnings"] == 2

    def test_small_chunks_and_long_lines(self, tmp_path: Path, monkeypatch) -> None:
        # Every VIVADO_LOG line fits in a 128-byte chunk, so each read ends
        # mid-log and must be cut back to a line boundary; the filler line is
        # longer than a chunk and gets parsed in pieces (harmless fragments).
        monkeypatch.setattr(BuildRecord, "_PARSE_CHUNK_BYTES", 128)
        assert max(map(len, VIVADO_LOG.splitlines())) < 128
        rec, log = _record(tmp_path)
        text = "INFO: " + "x" * 300 + "\n" + VIVADO_LOG
        _append(log, text)
        rec.returncode = 0
        info = rec.build_info()
        expected = parse_build_log(text)
        for key in ("tool", "phase", "warnings", "errors", "slack", "utilization"):
            assert info[key] == expected[key], key

    def test_missing_log(self, tmp_path: Path) -> None:
        rec, log = _record(tmp_path)
        log.unlink()
        assert rec.build_info()["tool"] == "unknown"

    def test_large_backlog_keeps_head_and_skips_middle(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(BuildRecord, "_PARSE_MAX_CATCHUP_BYTES", 2000)
        monkeypatch.setattr(BuildRecord, "_PARSE_HEAD_BYTES", 64)
        rec, log = _record(tmp_path)
        filler = "".join(f"WARNING: [Synth 8-{i}] filler line\n" for i in range(500))
        _append(log, "****** Vivado v2024.1 (64-bit)\n" + filler)
        _append(log, "Starting Router\n| WNS=0.250 | TNS=0.000 |\n")

        info = rec.build_info()
        assert info["tool"] == "vivado"  # banner kept from the head
        assert info["phase"] == "vivado_route"  # tail parsed
        assert info["slack"]["wns_ns"] == 0.25
        assert info["log_bytes_skipped"] > 0
        assert info["warnings"] < 500
        # Later polls are incremental again and skip nothing more.
        skipped = info["log_bytes_skipped"]
        _append(log, "WARNING: [Route 1-1] late\n")
        after = rec.build_info()
        assert after["warnings"] == info["warnings"] + 1
        assert after["log_bytes_skipped"] == skipped


# ---------------------------------------------------------------------------
# mcp_lite — batches and non-object messages must not kill the read loop
# ---------------------------------------------------------------------------


def _run(monkeypatch, lines: list[str]) -> list:
    from fpgazero_mcp.mcp_lite import Server

    srv = Server("t")
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    asyncio.run(srv.run())
    return [json.loads(x) for x in out.getvalue().splitlines()]


PING_2 = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})


class TestMessageShapes:
    def test_batch_gets_array_reply(self, monkeypatch) -> None:
        batch = [
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 11, "method": "nope"},
            7,
        ]
        out = _run(monkeypatch, [json.dumps(batch), PING_2])
        arrays = [o for o in out if isinstance(o, list)]
        assert len(arrays) == 1
        by_id = {r["id"]: r for r in arrays[0]}
        assert by_id[10]["result"] == {}
        assert by_id[11]["error"]["code"] == -32601
        assert by_id[None]["error"]["code"] == -32600
        assert len(arrays[0]) == 3  # no entry for the notification
        assert {"jsonrpc": "2.0", "id": 2, "result": {}} in out

    def test_notification_only_batch_sends_nothing(self, monkeypatch) -> None:
        batch = [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
        out = _run(monkeypatch, [json.dumps(batch), PING_2])
        assert out == [{"jsonrpc": "2.0", "id": 2, "result": {}}]

    @pytest.mark.parametrize("raw", ["[]", "5", '"hello"', "null"])
    def test_invalid_shapes_answered_and_loop_survives(self, monkeypatch, raw) -> None:
        out = _run(monkeypatch, [raw, PING_2])
        assert len(out) == 2  # replies are sent from separate tasks: any order
        assert [o["error"]["code"] for o in out if "error" in o] == [-32600]
        assert {"jsonrpc": "2.0", "id": 2, "result": {}} in out


# ---------------------------------------------------------------------------
# Regressions found running a real Vivado build through start_build
# ---------------------------------------------------------------------------


class TestRealVivadoRegressions:
    def test_sentence_ending_period_after_number(self) -> None:
        # Vivado 2025.2 place_design output; [\d.]+ captured "6.361." and
        # float() raised, failing every build_status from then on.
        log = (
            "****** Vivado v2025.2 (64-bit)\n"
            "INFO: [Place 30-746] Post Placement Timing Summary WNS=6.361. "
            "For the most accurate timing information please run report_timing.\n"
            "Fmax: 150. MHz\n"
        )
        r = parse_build_log(log)
        assert r["slack"]["wns_ns"] == 6.361
        assert r["timing"]["fmax_mhz"] == 150.0

    def test_unparseable_number_skips_metric_not_build(self, monkeypatch) -> None:
        import re

        from fpgazero_mcp.tools import build_parser

        monkeypatch.setattr(
            build_parser, "_SLACK_PATTERNS", [("wns_ns", re.compile(r"WNS=(\S+)"))]
        )
        r = parse_build_log("Vivado v1\nWNS=n/a\nWARNING: [X 1-1] still counted\n")
        assert "slack" not in r
        assert r["warnings"] == 1


class TestWindowsLaunchers:
    @pytest.mark.parametrize(
        "cmd0",
        [
            "vivado",
            "vivado.exe",
            "vivado.bat",
            "vivado.CMD",
            os.path.join("some", "dir", "vivado.bat"),
        ],
    )
    def test_allowlist_accepts_windows_suffixes(self, cmd0: str) -> None:
        from fpgazero_mcp.tools.build_manager import _validate_build_cmd

        assert _validate_build_cmd([cmd0, "-mode", "batch"]) is None

    def test_allowlist_still_rejects_others(self) -> None:
        from fpgazero_mcp.tools.build_manager import _validate_build_cmd

        assert _validate_build_cmd(["evil.bat"]) is not None
        assert _validate_build_cmd(["vivado.ps1"]) is not None

    def _fake_bat(self, tmp_path: Path) -> Path:
        bat = tmp_path / "vivado.bat"
        bat.write_text("@echo off\n")
        bat.chmod(0o755)  # shutil.which needs the exec bit on POSIX
        return bat

    @pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is Windows-only")
    def test_batch_file_rejects_cmd_metacharacters(self, tmp_path: Path) -> None:
        from fpgazero_mcp.tools.build_manager import _resolve_executable

        bat = self._fake_bat(tmp_path)
        for arg in ("a&calc", "x|y", "%PATH%", 'q"uote', "a^b", "a>b"):
            _cmd, err = _resolve_executable([str(bat), "-source", arg])
            assert err is not None and "batch file" in err, arg
        run, err = _resolve_executable([str(bat), "-source", r"C:\my dir\b.tcl"])
        assert err is None
        assert run[0].lower().endswith("vivado.bat")

    @pytest.mark.skipif(
        sys.platform != "win32", reason="PATHEXT lookup is Windows-only"
    )
    def test_bare_name_resolves_through_pathext(self, tmp_path: Path) -> None:
        from fpgazero_mcp.tools.build_manager import _resolve_executable

        self._fake_bat(tmp_path)
        run, err = _resolve_executable(["vivado", "-version"], path=str(tmp_path))
        assert err is None
        assert (
            run == [str(tmp_path / "vivado.bat"), "-version"]
            or run[0].lower() == str(tmp_path / "vivado.bat").lower()
        )

    def test_unresolvable_passes_through(self) -> None:
        from fpgazero_mcp.tools.build_manager import _resolve_executable

        run, err = _resolve_executable(["no-such-tool-xyz", "a&b"], path="")
        assert err is None and run == ["no-such-tool-xyz", "a&b"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX lookup")
    def test_posix_command_is_left_to_popen(self, tmp_path: Path) -> None:
        """A relative PATH entry must still be searched from the child's cwd."""
        from fpgazero_mcp.tools.build_manager import _resolve_executable

        tool = tmp_path / "yosys"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        run, err = _resolve_executable(["yosys", "-V"], path=str(tmp_path))
        assert err is None and run == ["yosys", "-V"]
