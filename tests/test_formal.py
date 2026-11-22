# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for the SymbiYosys formal verification backend.

The mocked tests feed the parser real sby console transcripts (captured from
sby 0.40+) rather than invented strings, so a change in sby's output format
shows up as a test failure rather than as a silently empty result dict.
"""

import shutil
import subprocess

import pytest

from fpgazero_mcp.tools import proctree

from fpgazero_mcp.tools.formal import (
    ENGINES,
    MODES,
    _parse_sby_log,
    _sby_config,
    _sby_script,
    formal_verify,
)

# --- real sby transcripts -------------------------------------------------

SBY_PASS = """\
SBY 13:37:00 [demo] Removing directory 'demo'.
SBY 13:37:00 [demo] Copy 'design.sv' to 'demo/src/design.sv'.
SBY 13:37:00 [demo] engine_0: smtbmc yices
SBY 13:37:00 [demo] base: starting process "cd demo/src; yosys -ql ../model/design.log"
SBY 13:37:01 [demo] base: finished (returncode=0)
SBY 13:37:01 [demo] engine_0: ##   0:00:00  Solving for step 19..
SBY 13:37:02 [demo] engine_0: ##   0:00:00  Status: passed
SBY 13:37:02 [demo] engine_0: finished (returncode=0)
SBY 13:37:02 [demo] engine_0: Status returned by engine: PASS
SBY 13:37:02 [demo] summary: Elapsed clock time [H:MM:SS (secs)]: 0:00:02 (2)
SBY 13:37:02 [demo] summary: engine_0 (smtbmc yices) returned PASS
SBY 13:37:02 [demo] DONE (PASS, rc=0)
"""

SBY_FAIL = """\
SBY 13:37:00 [demo] Removing directory 'demo'.
SBY 13:37:00 [demo] engine_0: smtbmc yices
SBY 13:37:02 [demo] engine_0: ##   0:00:00  Solving for step 3..
SBY 13:37:02 [demo] engine_0: ##   0:00:00  Assert failed in top: design.sv:14.7-14.32 ($assert$design.sv:14$1)
SBY 13:37:02 [demo] engine_0: ##   0:00:00  Writing trace to VCD file: engine_0/trace.vcd
SBY 13:37:02 [demo] engine_0: ##   0:00:00  Status: failed
SBY 13:37:02 [demo] engine_0: finished (returncode=1)
SBY 13:37:02 [demo] engine_0: Status returned by engine: FAIL
SBY 13:37:02 [demo] summary: engine_0 (smtbmc yices) returned FAIL
SBY 13:37:02 [demo] summary: counterexample trace: demo/engine_0/trace.vcd
SBY 13:37:02 [demo] DONE (FAIL, rc=2)
"""

SBY_UNKNOWN = """\
SBY 13:37:00 [demo] engine_0: smtbmc yices
SBY 13:37:05 [demo] engine_0: ##   0:00:03  Trying induction in step 20..
SBY 13:37:05 [demo] engine_0: Status returned by engine: UNKNOWN
SBY 13:37:05 [demo] summary: engine_0 (smtbmc yices) returned UNKNOWN
SBY 13:37:05 [demo] DONE (UNKNOWN, rc=4)
"""

SBY_ERROR = """\
SBY 13:37:00 [demo] base: starting process "cd demo/src; yosys -ql ../model/design.log"
ERROR: syntax error, unexpected TOK_ID, expecting ';'
SBY 13:37:00 [demo] base: finished (returncode=1)
SBY 13:37:00 [demo] base: task failed. ERROR.
SBY 13:37:00 [demo] DONE (ERROR, rc=16)
"""

SBY_COVER = """\
SBY 13:37:00 [demo] engine_0: smtbmc yices
SBY 13:37:01 [demo] engine_0: ##   0:00:00  Reached cover statement at design.sv:20.4-20.19 in step 4.
SBY 13:37:01 [demo] engine_0: ##   0:00:00  Writing trace to VCD file: engine_0/trace0.vcd
SBY 13:37:01 [demo] summary: engine_0 (smtbmc yices) returned PASS
SBY 13:37:01 [demo] summary: trace: demo/engine_0/trace0.vcd
SBY 13:37:01 [demo] DONE (PASS, rc=0)
"""

DESIGN = """\
module top(input wire clk, input wire a, output wire y);
  assign y = a;
  always @(posedge clk) assert (y == a);
endmodule
"""


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _mock_sby(monkeypatch, stdout, returncode=0):
    def fake_run(cmd, **kwargs):
        return _Proc(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(proctree, "run_tree", fake_run)


# --- log parsing ----------------------------------------------------------


class TestParseSbyLog:
    def test_pass(self) -> None:
        r = _parse_sby_log(SBY_PASS, 0)
        assert r["status"] == "PASS"
        assert r["engines"] == [{"engine": "smtbmc yices", "result": "PASS"}]
        assert r["failed_assertions"] == []
        assert "counterexample_trace" not in r

    def test_fail_extracts_assertion_and_trace(self) -> None:
        r = _parse_sby_log(SBY_FAIL, 2)
        assert r["status"] == "FAIL"
        assert r["failed_assertions"] == [
            {
                "module": "top",
                "location": "design.sv:14.7-14.32 ($assert$design.sv:14$1)",
            }
        ]
        assert r["counterexample_trace"] == "demo/engine_0/trace.vcd"

    def test_unknown(self) -> None:
        r = _parse_sby_log(SBY_UNKNOWN, 4)
        assert r["status"] == "UNKNOWN"

    def test_error_collects_yosys_messages(self) -> None:
        r = _parse_sby_log(SBY_ERROR, 16)
        assert r["status"] == "ERROR"
        assert r["errors"] == ["syntax error, unexpected TOK_ID, expecting ';'"]

    def test_cover_trace_without_counterexample_label(self) -> None:
        r = _parse_sby_log(SBY_COVER, 0)
        assert r["status"] == "PASS"
        assert r["counterexample_trace"] == "demo/engine_0/trace0.vcd"

    def test_no_done_line_falls_back_to_returncode(self) -> None:
        assert _parse_sby_log("", 0)["status"] == "PASS"
        assert _parse_sby_log("", 1)["status"] == "ERROR"


# --- .sby file generation -------------------------------------------------


class TestSbyGeneration:
    def test_verilog_script_uses_read_formal(self) -> None:
        script = _sby_script(["/tmp/x/design.v"], "verilog", "top")
        assert "read -formal design.v" in script
        assert "prep -top top" in script

    def test_systemverilog_adds_sv_flag(self) -> None:
        script = _sby_script(["/tmp/x/design.sv"], "systemverilog", "top")
        assert "read -formal -sv design.sv" in script

    def test_includes_and_defines(self) -> None:
        script = _sby_script(
            ["/tmp/x/design.v"], "verilog", "top", ["/inc/dir"], ["FORMAL", "W=8"]
        )
        assert "-I/inc/dir" in script
        assert "-DFORMAL" in script
        assert "-DW=8" in script

    def test_vhdl_uses_ghdl_and_lowercases_top(self) -> None:
        script = _sby_script(["/tmp/x/design.vhd"], "vhdl", "TopEnt")
        assert "ghdl --std=08 design.vhd -e topent" in script
        assert "prep -top topent" in script

    def test_config_sections(self) -> None:
        cfg = _sby_config(
            "prep -top top\n", ["a.v", "b.v"], "prove", 30, "abc pdr", True
        )
        assert "[options]" in cfg
        assert "mode prove" in cfg
        assert "depth 30" in cfg
        assert "multiclock on" in cfg
        assert "[engines]\nabc pdr" in cfg
        assert "[files]\na.v\nb.v" in cfg

    def test_multiclock_off_by_default(self) -> None:
        cfg = _sby_config("prep -top top\n", ["a.v"], "bmc", 20, "smtbmc", False)
        assert "multiclock" not in cfg


# --- input validation -----------------------------------------------------


class TestValidation:
    def test_bad_mode(self) -> None:
        r = formal_verify(code=DESIGN, mode="nope")
        assert r["error_code"] == "invalid_input"
        assert "nope" in r["error"]

    def test_bad_engine(self) -> None:
        r = formal_verify(code=DESIGN, engine="magic")
        assert r["error_code"] == "invalid_input"

    @pytest.mark.parametrize("depth", [0, -5, 20001, "twenty"])
    def test_bad_depth(self, depth) -> None:
        r = formal_verify(code=DESIGN, depth=depth)
        assert r["error_code"] == "invalid_input"
        assert "depth" in r["error"]

    def test_bad_top_module(self) -> None:
        r = formal_verify(code=DESIGN, top_module="9bad-name")
        assert r["error_code"] == "invalid_input"

    def test_no_source(self) -> None:
        r = formal_verify()
        assert r["error_code"] == "invalid_input"

    def test_duplicate_basenames_rejected(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_PASS)
        # sby stages [files] by basename, so two a/x.v and b/x.v would collide.
        r = formal_verify(files={"a/x.v": DESIGN, "b/x.v": DESIGN})
        assert r["error_code"] == "invalid_input"
        assert "basename" in r["error"]

    def test_all_declared_modes_and_engines_accepted(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_PASS)
        for mode in MODES:
            for engine in ENGINES:
                r = formal_verify(code=DESIGN, mode=mode, engine=engine)
                # sby's live mode only runs on aiger suprove.
                valid = mode != "live" or engine == "aiger-suprove"
                rejected = r.get("error_code") == "invalid_input"
                assert rejected is not valid, (mode, engine)


# --- end-to-end behaviour with a mocked sby -------------------------------


class TestFormalVerify:
    def test_bmc_pass_is_bounded_not_proved(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_PASS)
        r = formal_verify(code=DESIGN, mode="bmc", depth=20)
        assert r["success"] is True
        assert r["status"] == "PASS"
        assert r["bounded"] is True
        assert r["proved"] is False
        assert "bounded result" in r["summary"]

    def test_prove_pass_is_proved(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_PASS)
        r = formal_verify(code=DESIGN, mode="prove")
        assert r["success"] is True
        assert r["proved"] is True
        assert "bounded" not in r

    def test_fail_reports_assertion(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_FAIL, returncode=2)
        r = formal_verify(code=DESIGN, top_module="top")
        assert r["success"] is False
        assert r["error_code"] == "formal_failed"
        assert "design.sv:14.7-14.32" in r["error"]
        assert r["counterexample_trace"] == "demo/engine_0/trace.vcd"

    def test_unknown_suggests_next_step(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_UNKNOWN, returncode=4)
        r = formal_verify(code=DESIGN, mode="prove")
        assert r["success"] is False
        assert r["error_code"] == "formal_failed"
        assert "abc-pdr" in r["error"]

    def test_error_status(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_ERROR, returncode=16)
        r = formal_verify(code=DESIGN)
        assert r["success"] is False
        assert r["status"] == "ERROR"
        assert r["errors"]

    def test_engine_string_is_reported(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_PASS)
        r = formal_verify(code=DESIGN, engine="abc-pdr")
        assert r["engine"] == "abc pdr"

    def test_sby_not_installed(self, monkeypatch) -> None:
        def boom(cmd, **kwargs):
            raise FileNotFoundError()

        monkeypatch.setattr(proctree, "run_tree", boom)
        r = formal_verify(code=DESIGN)
        assert r["error_code"] == "tool_not_found"
        assert "sby" in r["error"]

    def test_timeout(self, monkeypatch) -> None:
        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd="sby", timeout=1)

        monkeypatch.setattr(proctree, "run_tree", boom)
        r = formal_verify(code=DESIGN, timeout=1)
        assert r["error_code"] == "timeout"

    def test_progress_callback_invoked(self, monkeypatch) -> None:
        _mock_sby(monkeypatch, SBY_PASS)
        seen: list[tuple[float, str]] = []
        formal_verify(code=DESIGN, progress=lambda f, m: seen.append((f, m)))
        assert seen
        assert all(0.0 <= f <= 1.0 for f, _ in seen)

    def test_generated_sby_file_contents(self, monkeypatch) -> None:
        """The .sby handed to sby must be complete and reference the staged source."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            cwd = kwargs["cwd"]
            with open(f"{cwd}/design.sby", "r", encoding="utf-8") as f:
                captured["sby"] = f.read()
            captured["cmd"] = cmd
            return _Proc(stdout=SBY_PASS, returncode=0)

        monkeypatch.setattr(proctree, "run_tree", fake_run)
        r = formal_verify(code=DESIGN, mode="bmc", depth=7, top_module="top")
        assert r["success"] is True
        assert captured["cmd"][0] == "sby"
        assert "-f" in captured["cmd"]
        cfg = captured["sby"]
        assert "mode bmc" in cfg
        assert "depth 7" in cfg
        assert "read -formal design.v" in cfg
        assert cfg.rstrip().endswith("design.v")


# --- integration ----------------------------------------------------------


def _have_sby() -> bool:
    return bool(shutil.which("sby") and shutil.which("yosys"))


PROVABLE = """\
module top(input wire clk, input wire rst, input wire [3:0] d, output reg [3:0] q);
  always @(posedge clk) q <= rst ? 4'd0 : d;
  always @(posedge clk) if (!rst) assert (q <= 4'd15);
endmodule
"""

FALSIFIABLE = """\
module top(input wire clk, input wire rst, input wire [3:0] d, output reg [3:0] q);
  always @(posedge clk) q <= rst ? 4'd0 : d;
  always @(posedge clk) if (!rst) assert (q < 4'd8);
endmodule
"""


@pytest.mark.integration
def test_formal_bmc_passes_on_true_property() -> None:
    if not _have_sby():
        pytest.skip("sby/yosys not installed")
    r = formal_verify(code=PROVABLE, top_module="top", mode="bmc", depth=10)
    assert r["status"] == "PASS", r.get("log")
    assert r["success"] is True
    assert r["bounded"] is True


@pytest.mark.integration
def test_formal_bmc_finds_counterexample() -> None:
    if not _have_sby():
        pytest.skip("sby/yosys not installed")
    r = formal_verify(code=FALSIFIABLE, top_module="top", mode="bmc", depth=10)
    assert r["status"] == "FAIL", r.get("log")
    assert r["success"] is False
    assert r["error_code"] == "formal_failed"
    # The parser must recover the assertion location from real sby output.
    assert r["failed_assertions"], r.get("log")


@pytest.mark.integration
def test_formal_prove_reports_proved() -> None:
    if not _have_sby():
        pytest.skip("sby/yosys not installed")
    r = formal_verify(code=PROVABLE, top_module="top", mode="prove", depth=10)
    assert r["status"] == "PASS", r.get("log")
    assert r["proved"] is True
