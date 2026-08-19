# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for v0.7.0: first-class Vivado place-and-route backend."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def scratch_dir():
    """tmp_path substitute — pytest-qt in this env breaks the built-in fixture."""
    path = tempfile.mkdtemp(prefix="pytest_v070_")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


# Realistic Vivado log excerpts for parser tests
_VIVADO_LOG = """\
****** Vivado v2024.1 (64-bit)
Command: synth_design -top blink -part xc7a35tcpg236-1
Starting Synthesis
Phase 1.1 Core Generation And Design Setup
place_design
Phase 4.1 Global Placement
route_design
INFO: [Route 35-57] Estimated Timing Summary | WNS=0.087 | TNS=0.000 |
report_utilization
+----------------------------+------+-------+------------+-----------+-------+
|          Site Type         | Used | Fixed | Prohibited | Available | Util% |
+----------------------------+------+-------+------------+-----------+-------+
| Slice LUTs                 |   12 |     0 |          0 |     20800 |  0.06 |
| Slice Registers            |    8 |     0 |          0 |     41600 |  0.02 |
| Bonded IOB                 |    3 |     0 |          0 |       106 |  2.83 |
+----------------------------+------+-------+------------+-----------+-------+
report_timing_summary
Design Timing Summary
| WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)
    -------      -------  ---------------------  -------------------      -------      -------
      2.145        0.000                      0                   10        0.112        0.000
write_bitstream -force out.bit
Bitstream Generation completed
"""


def _completed(cmd, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# TCL generation
# ---------------------------------------------------------------------------


class TestTclGeneration:
    def test_verilog_flow(self) -> None:
        from tools.vivado import _generate_tcl

        tcl = _generate_tcl(
            ["C:\\work\\a.v", "C:\\work\\b.v"],
            "verilog",
            "top",
            "xc7a35tcpg236-1",
            "C:\\work\\pins.xdc",
            "C:\\work\\out.bit",
        )
        assert "read_verilog {C:/work/a.v}" in tcl
        assert "read_verilog {C:/work/b.v}" in tcl
        assert "-sv" not in tcl
        assert "read_xdc {C:/work/pins.xdc}" in tcl
        assert "synth_design -top top -part xc7a35tcpg236-1" in tcl
        assert "opt_design" in tcl
        assert "place_design" in tcl
        assert "route_design" in tcl
        assert "report_utilization" in tcl
        assert "report_timing_summary" in tcl
        assert "write_bitstream -force {C:/work/out.bit}" in tcl

    def test_systemverilog_uses_sv_flag(self) -> None:
        from tools.vivado import _generate_tcl

        tcl = _generate_tcl(["/w/a.sv"], "systemverilog", "t", "xc7a35t", None, "/w/o")
        assert "read_verilog -sv {/w/a.sv}" in tcl

    def test_vhdl_uses_read_vhdl_2008(self) -> None:
        from tools.vivado import _generate_tcl

        tcl = _generate_tcl(["/w/a.vhd"], "vhdl", "t", "xc7a35t", None, "/w/o")
        assert "read_vhdl -vhdl2008 {/w/a.vhd}" in tcl
        assert "read_verilog" not in tcl

    def test_no_xdc_line_without_constraints(self) -> None:
        from tools.vivado import _generate_tcl

        tcl = _generate_tcl(["/w/a.v"], "verilog", "t", "xc7a35t", None, "/w/o")
        assert "read_xdc" not in tcl


# ---------------------------------------------------------------------------
# Timing parsing
# ---------------------------------------------------------------------------


class TestVivadoTimingParse:
    def test_timing_summary_table(self) -> None:
        from tools.vivado import _parse_timing

        t = _parse_timing(_VIVADO_LOG, clock_period_ns=10.0)
        assert t["wns_ns"] == 2.145
        assert t["tns_ns"] == 0.0
        assert t["whs_ns"] == 0.112
        assert t["ths_ns"] == 0.0
        # fmax = 1000 / (10.0 - 2.145)
        assert t["max_freq_mhz"] == pytest.approx(127.31, abs=0.01)
        assert t["meets_timing"] is True

    def test_inline_route_summary(self) -> None:
        from tools.vivado import _parse_timing

        log = "route_design\nINFO: Estimated Timing Summary | WNS=-0.35 | TNS=-1.2 |"
        t = _parse_timing(log, clock_period_ns=5.0)
        assert t["wns_ns"] == -0.35
        assert t["tns_ns"] == -1.2
        assert t["meets_timing"] is False
        assert t["max_freq_mhz"] == pytest.approx(1000.0 / 5.35, abs=0.01)

    def test_no_period_no_fmax(self) -> None:
        from tools.vivado import _parse_timing

        t = _parse_timing(_VIVADO_LOG, clock_period_ns=None)
        assert "max_freq_mhz" not in t
        assert t["wns_ns"] == 2.145

    def test_empty_log(self) -> None:
        from tools.vivado import _parse_timing

        assert _parse_timing("", clock_period_ns=10.0) == {}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestVivadoValidation:
    def test_missing_top_module(self) -> None:
        from tools.vivado import vivado_place_and_route

        result = vivado_place_and_route(code="module t; endmodule", part="xc7a35t")
        assert result["success"] is False
        assert result["error_code"] == "invalid_input"

    def test_missing_part(self) -> None:
        from tools.vivado import vivado_place_and_route

        result = vivado_place_and_route(code="module t; endmodule", top_module="t")
        assert result["success"] is False
        assert result["error_code"] == "invalid_input"
        assert "part number" in result["error"]

    def test_invalid_part_rejected(self) -> None:
        from tools.vivado import vivado_place_and_route

        result = vivado_place_and_route(
            code="module t; endmodule", top_module="t", part="xc7a35t; exec rm"
        )
        assert result["success"] is False
        assert result["error_code"] == "invalid_input"

    def test_vivado_not_found(self, monkeypatch) -> None:
        import tools.vivado as vv

        monkeypatch.setattr(vv.shutil, "which", lambda _cmd: None)
        result = vv.vivado_place_and_route(
            code="module t; endmodule", top_module="t", part="xc7a35tcpg236-1"
        )
        assert result["success"] is False
        assert result["error_code"] == "tool_not_found"


# ---------------------------------------------------------------------------
# place_and_route routing to the Vivado backend
# ---------------------------------------------------------------------------


class TestPnrVivadoRouting:
    def _capture(self, monkeypatch) -> list[dict]:
        import tools.vivado as vv

        calls: list[dict] = []

        def fake(**kwargs):
            calls.append(kwargs)
            return {"success": True, "backend": "vivado", "timing": {}}

        monkeypatch.setattr(vv, "vivado_place_and_route", fake)
        return calls

    def test_target_xilinx_routes_to_vivado(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        calls = self._capture(monkeypatch)
        result = place_and_route(
            code="module t; endmodule",
            top_module="t",
            target="xilinx",
            device="xc7a35tcpg236-1",
        )
        assert result["success"] is True
        assert len(calls) == 1
        assert calls[0]["part"] == "xc7a35tcpg236-1"

    def test_backend_vivado_routes_without_target(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        calls = self._capture(monkeypatch)
        place_and_route(
            code="module t; endmodule",
            top_module="t",
            backend="vivado",
            device="xc7a100tcsg324-1",
        )
        assert len(calls) == 1

    def test_board_preset_sets_part_and_target(self, monkeypatch) -> None:
        import tools.vivado as vv
        from tools.pnr import place_and_route

        calls: list[dict] = []

        def fake(**kwargs):
            calls.append(kwargs)
            return {"success": True, "timing": {"max_freq_mhz": 150.0}}

        monkeypatch.setattr(vv, "vivado_place_and_route", fake)
        result = place_and_route(
            code="module t; endmodule", top_module="t", board="basys3"
        )
        assert calls[0]["part"] == "xc7a35tcpg236-1"
        assert result["board"] == "basys3"
        assert result["timing"]["target_mhz"] == 100.0
        assert result["timing"]["meets_target"] is True

    def test_non_xilinx_targets_unchanged(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        calls = self._capture(monkeypatch)
        result = place_and_route(
            code="module t; endmodule", top_module="t", target="bogus"
        )
        assert result["success"] is False
        assert calls == []  # vivado backend not invoked
        assert "xilinx" in result["error"]


# ---------------------------------------------------------------------------
# Full mocked flow
# ---------------------------------------------------------------------------


class TestVivadoMockedFlow:
    def test_end_to_end_mocked(self, monkeypatch, scratch_dir) -> None:
        import tools.vivado as vv

        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir / "data"))
        monkeypatch.setattr(vv.shutil, "which", lambda _cmd: "vivado")

        seen_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            tcl_file = cmd[-1]
            tmpdir = os.path.dirname(tcl_file)
            with open(os.path.join(tmpdir, "out.bit"), "wb") as f:
                f.write(b"\x00\xff\x00bitstream")
            return _completed(cmd, stdout=_VIVADO_LOG)

        monkeypatch.setattr(vv.subprocess, "run", fake_run)

        progress_msgs: list[tuple[float, str]] = []
        result = vv.vivado_place_and_route(
            code="module blink(); endmodule",
            top_module="blink",
            part="xc7a35tcpg236-1",
            constraints="create_clock -period 10.000 [get_ports clk]",
            progress=lambda frac, msg: progress_msgs.append((frac, msg)),
        )

        assert result["success"] is True
        assert result["backend"] == "vivado"
        assert result["target"] == "xilinx"
        assert result["constraints"] == "provided"
        # Batch-mode invocation
        cmd = seen_cmds[0]
        assert cmd[1:5] == ["-mode", "batch", "-nolog", "-nojournal"]
        # Utilization parsed from the log (flat shape like the nextpnr backend)
        assert result["utilization"]["luts_used"] == 12
        assert result["utilization"]["luts_total"] == 20800
        assert result["utilization"]["ffs_used"] == 8
        # Timing: table WNS, fmax from XDC clock period
        assert result["timing"]["wns_ns"] == 2.145
        assert result["timing"]["max_freq_mhz"] == pytest.approx(127.31, abs=0.01)
        # Bitstream persisted for program_fpga
        assert os.path.exists(result["bitstream_path"])
        assert result["bitstream_ext"] == ".bit"
        # Progress reported through phase boundaries
        fracs = [f for f, _ in progress_msgs]
        assert fracs == sorted(fracs)
        assert fracs[-1] == 1.0

    def test_failed_run_returns_pnr_failed(self, monkeypatch) -> None:
        import tools.vivado as vv

        monkeypatch.setattr(vv.shutil, "which", lambda _cmd: "vivado")
        monkeypatch.setattr(
            vv.subprocess,
            "run",
            lambda cmd, **kw: _completed(
                cmd, stdout="ERROR: [Synth 8-439] module not found", returncode=1
            ),
        )
        result = vv.vivado_place_and_route(
            code="module t; endmodule", top_module="t", part="xc7a35tcpg236-1"
        )
        assert result["success"] is False
        assert result["error_code"] == "pnr_failed"
        assert result["errors"] >= 1

    def test_timeout(self, monkeypatch) -> None:
        import tools.vivado as vv

        monkeypatch.setattr(vv.shutil, "which", lambda _cmd: "vivado")

        def raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(vv.subprocess, "run", raise_timeout)
        result = vv.vivado_place_and_route(
            code="module t; endmodule",
            top_module="t",
            part="xc7a35tcpg236-1",
            timeout=5,
        )
        assert result["error_code"] == "timeout"


# ---------------------------------------------------------------------------
# XDC auto-detection
# ---------------------------------------------------------------------------


class TestXdcAutoDetect:
    def test_prefers_root_over_subdir(self, scratch_dir) -> None:
        from tools.vivado import _find_xdc

        sub = scratch_dir / "constrs"
        sub.mkdir()
        (sub / "deep.xdc").write_text("# sub", encoding="utf-8")
        (scratch_dir / "top.xdc").write_text("# root", encoding="utf-8")
        found = _find_xdc(str(scratch_dir))
        assert found is not None
        assert os.path.basename(found) == "top.xdc"

    def test_none_when_absent(self, scratch_dir) -> None:
        from tools.vivado import _find_xdc

        assert _find_xdc(str(scratch_dir)) is None


# ---------------------------------------------------------------------------
# Xilinx board presets
# ---------------------------------------------------------------------------


class TestXilinxBoards:
    def test_basys3_preset(self) -> None:
        from tools.boards import get_board_preset

        preset = get_board_preset("basys3")
        assert preset is not None
        assert preset["target"] == "xilinx"
        assert preset["device"] == "xc7a35tcpg236-1"
        assert preset["clock_mhz"] == 100.0

    def test_dash_normalization(self) -> None:
        from tools.boards import get_board_preset

        assert get_board_preset("arty-a7-35") is not None

    def test_listed(self) -> None:
        from tools.boards import list_boards

        names = {b["board"] for b in list_boards()}
        assert {"arty_a7_35", "arty_a7_100", "basys3", "nexys_a7_100"} <= names


# ---------------------------------------------------------------------------
# build_parser: inline WNS= from route_design
# ---------------------------------------------------------------------------


class TestBuildParserInlineWns:
    def test_wns_equals_form(self) -> None:
        from tools.build_parser import parse_build_log

        log = (
            "route_design\n"
            "INFO: [Route 35-57] Estimated Timing Summary | WNS=0.087 | TNS=0.000 |\n"
        )
        parsed = parse_build_log(log)
        assert parsed["tool"] == "vivado"
        assert parsed["slack"]["wns_ns"] == 0.087
        assert parsed["slack"]["tns_ns"] == 0.0


# ---------------------------------------------------------------------------
# server schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_place_and_route_schema_advertises_vivado(self) -> None:
        import server

        tools = asyncio.run(server.handle_list_tools())
        pnr = next(t for t in tools if t.name == "place_and_route")
        props = pnr.inputSchema["properties"]
        assert "vivado" in props["backend"]["enum"]
        assert "xilinx" in props["target"]["enum"]
