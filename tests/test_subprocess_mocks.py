# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Unit tests covering subprocess-invoking code paths via mocks.

Targets modules whose coverage is dominated by `subprocess.run` calls to
external EDA tools: tools/lsp.py, tools/lint.py, tools/litex.py,
tools/simulate.py, tools/pnr.py, registry/github.py.

All subprocess and HTTP calls are stubbed so these tests run without any
EDA toolchain or network connection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture
def scratch_dir():
    """tmp_path substitute — pytest-qt in this env breaks the built-in tmp_path fixture."""
    path = tempfile.mkdtemp(prefix="pytest_scratch_")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _stub_run(monkeypatch, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Patch subprocess.run to always return a fake CompletedProcess."""

    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeProc(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _stub_run_raises(monkeypatch, exc: BaseException):
    """Patch subprocess.run to raise the given exception."""

    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise exc

    monkeypatch.setattr(subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# tools.lsp
# ---------------------------------------------------------------------------


class TestLspDiagnostics:
    def test_verilator_success(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        _stub_run(monkeypatch, returncode=0, stdout="", stderr="")
        r = get_diagnostics("module top; endmodule", language="verilog")
        assert r["success"] is True
        assert r["tool"] == "verilator"
        assert r["diagnostics"] == []

    def test_verilator_parses_error_and_warning(self, monkeypatch) -> None:
        from tools.lsp import _parse_verilator

        # Build a realistic Verilator output snippet
        fname = "/tmp/x.v"
        output = (
            f"%Error-SYNTAX: {fname}:3:10: syntax error, unexpected ';'\n"
            f"%Warning-UNUSED: {fname}:5: Signal is unused: 'foo'\n"
        )
        diags = _parse_verilator(output, fname)
        assert len(diags) == 2
        assert diags[0]["severity"] == "error"
        assert diags[0]["code"] == "SYNTAX"
        assert diags[0]["line"] == 3
        assert diags[0]["col"] == 10
        assert diags[1]["severity"] == "warning"
        assert diags[1]["code"] == "UNUSED"
        # col defaults to 1 when not captured
        assert diags[1]["col"] == 1

    def test_verilator_systemverilog_flag(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        get_diagnostics("module top; endmodule", language="systemverilog")
        # SV mode should use Verilator's --sv switch, not iverilog's -g2012
        assert "--sv" in captured[0]
        assert "-g2012" not in captured[0]

    def test_verilator_missing_falls_back_to_verible(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        calls: list[str] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd[0])
            if cmd[0] == "verilator":
                raise FileNotFoundError
            return _FakeProc(returncode=0)

        # No WSL fallback either — force the verible path
        monkeypatch.setattr("tools.lsp._wsl_has_verilator", lambda: False)
        monkeypatch.setattr(subprocess, "run", fake_run)
        r = get_diagnostics("module top; endmodule", language="verilog")
        # We should have tried verilator first, then verible-verilog-lint
        assert calls == ["verilator", "verible-verilog-lint"]
        assert r["tool"] == "verible-verilog-lint"

    def test_verilator_timeout(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        _stub_run_raises(
            monkeypatch, subprocess.TimeoutExpired(cmd="verilator", timeout=30)
        )
        r = get_diagnostics("module top; endmodule", language="verilog")
        # The lsp module falls back to verible on "error", and verible also times out,
        # so the response ends up as an error dict. Either way there's no success field.
        assert "error" in r or r.get("success") is False

    def test_both_missing_returns_error(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = get_diagnostics("module top; endmodule", language="verilog")
        assert "error" in r
        assert "not found" in r["error"]

    def test_ghdl_for_vhdl(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = get_diagnostics("entity e is end;", language="vhdl")
        assert captured[0][0] == "ghdl"
        assert r["tool"] == "ghdl"

    def test_ghdl_parse_errors(self) -> None:
        from tools.lsp import _parse_ghdl

        fname = "/tmp/x.vhd"
        output = (
            f"{fname}:4:10: error: expected ';'\n{fname}:7:5: warning: unused signal\n"
        )
        diags = _parse_ghdl(output, fname)
        assert len(diags) == 2
        assert diags[0]["severity"] == "error"
        assert diags[1]["severity"] == "warning"

    def test_ghdl_missing(self, monkeypatch) -> None:
        from tools.lsp import get_diagnostics

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = get_diagnostics("entity e is end;", language="vhdl")
        assert "error" in r
        assert "ghdl" in r["error"]

    def test_verible_parse_with_rule(self) -> None:
        from tools.lsp import _parse_verible

        fname = "/tmp/x.v"
        output = f"{fname}:12:3: line too long [line-length]\n"
        diags = _parse_verible(output, fname)
        assert len(diags) == 1
        assert diags[0]["code"] == "line-length"
        assert diags[0]["line"] == 12
        assert diags[0]["col"] == 3


class TestLspFormat:
    def test_verible_format_success(self, monkeypatch) -> None:
        from tools.lsp import format_hdl

        _stub_run(monkeypatch, returncode=0, stdout="module top;\nendmodule\n")
        r = format_hdl("module top;endmodule", language="verilog")
        assert r["success"] is True
        assert r["tool"] == "verible-verilog-format"
        assert r["changed"] is True

    def test_verible_format_unchanged(self, monkeypatch) -> None:
        from tools.lsp import format_hdl

        original = "module top;\nendmodule\n"
        _stub_run(monkeypatch, returncode=0, stdout=original)
        r = format_hdl(original, language="verilog")
        assert r["success"] is True
        assert r["changed"] is False

    def test_verible_format_failure(self, monkeypatch) -> None:
        from tools.lsp import format_hdl

        _stub_run(monkeypatch, returncode=1, stderr="parse error")
        r = format_hdl("module", language="verilog")
        assert r["success"] is False
        assert r["stderr"] == "parse error"

    def test_verible_format_missing(self, monkeypatch) -> None:
        from tools.lsp import format_hdl

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = format_hdl("module top; endmodule", language="verilog")
        assert "error" in r

    def test_verible_format_timeout(self, monkeypatch) -> None:
        from tools.lsp import format_hdl

        _stub_run_raises(
            monkeypatch,
            subprocess.TimeoutExpired(cmd="verible-verilog-format", timeout=30),
        )
        r = format_hdl("module top; endmodule", language="verilog")
        assert "error" in r
        assert "timed out" in r["error"]

    def test_vsg_format_missing(self, monkeypatch) -> None:
        from tools.lsp import format_hdl

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = format_hdl("entity e is end;", language="vhdl")
        assert "error" in r
        assert "vsg" in r["error"]


# ---------------------------------------------------------------------------
# tools.lint
# ---------------------------------------------------------------------------


class TestLintHdlMocked:
    def test_verilog_success(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        _stub_run(monkeypatch, returncode=0)
        r = lint_hdl("module top; endmodule", language="verilog")
        assert r["success"] is True
        assert r["tool"] == "iverilog"
        assert r["message"] == "No errors found."

    def test_verilog_lint_failure(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        _stub_run(monkeypatch, returncode=1, stderr="syntax error")
        r = lint_hdl("module top endmodule", language="verilog")
        assert r["success"] is False
        assert "syntax error" in r["stderr"]
        assert "message" not in r

    def test_systemverilog_adds_g2012(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        lint_hdl("module top; endmodule", language="systemverilog")
        assert "-g2012" in captured[0]

    def test_vhdl_uses_ghdl(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        lint_hdl("entity e is end;", language="vhdl")
        assert captured[0][0] == "ghdl"

    def test_iverilog_missing(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = lint_hdl("module top; endmodule", language="verilog")
        assert r["success"] is False
        assert "iverilog" in r["error"]

    def test_verilator_missing_reports_verilator(self, monkeypatch) -> None:
        import shutil

        from tools.lint import lint_hdl

        # Force the verilator branch
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        # Also force _wsl_has_verilator to False
        monkeypatch.setattr("tools.lint._wsl_has_verilator", lambda: False)
        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = lint_hdl("module top; endmodule", language="verilog", linter="verilator")
        assert "verilator" in r["error"]

    def test_ghdl_missing(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = lint_hdl("entity e is end;", language="vhdl")
        assert "ghdl" in r["error"]

    def test_timeout(self, monkeypatch) -> None:
        from tools.lint import lint_hdl

        _stub_run_raises(
            monkeypatch, subprocess.TimeoutExpired(cmd="iverilog", timeout=30)
        )
        r = lint_hdl("module top; endmodule", language="verilog")
        assert r["success"] is False
        assert "timed out" in r["error"]


class TestLintProjectMocked:
    def test_project_success(self, monkeypatch) -> None:
        from tools.lint import lint_project

        _stub_run(monkeypatch, returncode=0)
        r = lint_project(
            files={"a.v": "module a; endmodule", "b.v": "module b; endmodule"},
            language="verilog",
            top_module="a",
        )
        assert r["success"] is True
        assert set(r["files"]) == {"a.v", "b.v"}

    def test_project_iverilog_adds_top(self, monkeypatch) -> None:
        from tools.lint import lint_project

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        lint_project(files={"a.v": "module a; endmodule"}, top_module="a")
        assert "-s" in captured[0]
        # -s must be followed by the top module name
        idx = captured[0].index("-s")
        assert captured[0][idx + 1] == "a"

    def test_project_systemverilog_adds_top(self, monkeypatch) -> None:
        from tools.lint import lint_project

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        lint_project(
            files={"a.sv": "module a; endmodule"},
            language="systemverilog",
            top_module="a",
        )
        assert "-s" in captured[0]
        assert "-g2012" in captured[0]

    def test_project_vhdl(self, monkeypatch) -> None:
        from tools.lint import lint_project

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        lint_project(files={"a.vhd": "entity a is end;"}, language="vhdl")
        assert captured[0][0] == "ghdl"

    def test_project_tool_missing(self, monkeypatch) -> None:
        from tools.lint import lint_project

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = lint_project(files={"a.v": "module a; endmodule"})
        assert r["success"] is False

    def test_project_timeout(self, monkeypatch) -> None:
        from tools.lint import lint_project

        _stub_run_raises(
            monkeypatch, subprocess.TimeoutExpired(cmd="iverilog", timeout=60)
        )
        r = lint_project(files={"a.v": "module a; endmodule"}, timeout=60)
        assert r["success"] is False
        assert "timed out" in r["error"]

    def test_project_auto_appends_extension(self, monkeypatch) -> None:
        from tools.lint import lint_project

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        # File named without .v suffix should get one appended
        lint_project(files={"topmodule": "module top; endmodule"})
        # The file path passed to iverilog should end with .v
        written_files = [arg for arg in captured[0] if "topmodule" in str(arg)]
        assert any(p.endswith(".v") for p in written_files)


# ---------------------------------------------------------------------------
# tools.litex
# ---------------------------------------------------------------------------


class TestLitexMocked:
    def test_litex_build_runs(self, monkeypatch) -> None:
        from tools.litex import litex_build

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0, stdout="build ok")

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = litex_build(board="arty")
        assert r["success"] is True
        # --build should be appended
        assert "--build" in captured[0]
        # --output-dir should be set
        assert "--output-dir" in captured[0]
        assert "output_dir" in r

    def test_litex_build_respects_existing_build_arg(self, monkeypatch) -> None:
        from tools.litex import litex_build

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        litex_build(board="arty", args=["--build", "--cpu-type=vexriscv"])
        # --build should only appear once (the user-provided one)
        assert captured[0].count("--build") == 1

    def test_litex_soc_strips_build(self, monkeypatch) -> None:
        from tools.litex import litex_soc

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        litex_soc(board="arty", args=["--build"])
        # --build must be stripped, --no-compile added
        assert "--build" not in captured[0]
        assert "--no-compile" in captured[0]

    def test_litex_flow_passthrough(self, monkeypatch) -> None:
        from tools.litex import litex_flow

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        litex_flow(board="arty", args=["--foo", "--bar=1"])
        assert "--foo" in captured[0]
        assert "--bar=1" in captured[0]

    def test_litex_not_found(self, monkeypatch) -> None:
        from tools.litex import litex_build

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = litex_build(board="arty")
        assert r["success"] is False
        assert "not found" in r["error"]

    def test_litex_timeout(self, monkeypatch) -> None:
        from tools.litex import litex_flow

        _stub_run_raises(
            monkeypatch, subprocess.TimeoutExpired(cmd="litex", timeout=600)
        )
        r = litex_flow(board="arty", timeout=600)
        assert r["success"] is False
        assert "timed out" in r["error"]

    def test_litex_build_failure_propagates_returncode(self, monkeypatch) -> None:
        from tools.litex import litex_build

        _stub_run(monkeypatch, returncode=2, stderr="build failed")
        r = litex_build(board="arty")
        assert r["success"] is False
        assert "build failed" in r["stderr"]

    def test_litex_custom_output_dir(self, monkeypatch, scratch_dir) -> None:
        from tools.litex import litex_build

        _stub_run(monkeypatch, returncode=0)
        custom = os.path.join(scratch_dir, "mybuild")
        r = litex_build(board="arty", output_dir=custom)
        assert r["output_dir"] == custom


# ---------------------------------------------------------------------------
# tools.simulate
# ---------------------------------------------------------------------------


class TestSimulateMocked:
    def test_verilog_compile_failure(self, monkeypatch) -> None:
        from tools.simulate import simulate

        _stub_run(monkeypatch, returncode=1, stderr="compile error")
        r = simulate("module top; endmodule", "module tb; endmodule")
        assert r["success"] is False
        assert r["stage"] == "compile"

    def test_verilog_run_success_with_pass_verdict(self, monkeypatch) -> None:
        from tools.simulate import simulate

        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            # First call compiles, second call (vvp) runs and prints PASS
            if cmd[0] == "iverilog":
                return _FakeProc(returncode=0)
            return _FakeProc(returncode=0, stdout="TEST PASSED\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = simulate("module top; endmodule", "module tb; endmodule")
        assert r["success"] is True
        assert r["stage"] == "run"
        assert r["verdict"]["verdict"] == "pass"

    def test_verilog_iverilog_missing(self, monkeypatch) -> None:
        from tools.simulate import simulate

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = simulate("module top; endmodule", "module tb; endmodule")
        assert r["success"] is False
        assert "iverilog" in r["error"]

    def test_verilog_timeout(self, monkeypatch) -> None:
        from tools.simulate import simulate

        _stub_run_raises(monkeypatch, subprocess.TimeoutExpired(cmd="vvp", timeout=60))
        r = simulate("module top; endmodule", "module tb; endmodule", timeout=60)
        assert r["success"] is False
        assert "timed out" in r["error"]

    def test_vhdl_no_entity_in_testbench(self) -> None:
        from tools.simulate import simulate

        r = simulate("entity e is end;", "-- no entity here", language="vhdl")
        assert r["success"] is False
        assert r["stage"] == "elaborate"

    def test_vhdl_analyze_design_fail(self, monkeypatch) -> None:
        from tools.simulate import simulate

        _stub_run(monkeypatch, returncode=1, stderr="bad vhdl")
        r = simulate(
            "entity e is end;",
            "entity tb is end; architecture a of tb is begin end;",
            language="vhdl",
        )
        assert r["success"] is False
        assert r["stage"] == "analyze_design"

    def test_vhdl_analyze_testbench_fail(self, monkeypatch) -> None:
        from tools.simulate import simulate

        calls: list[int] = []

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            # First ghdl -a (design) succeeds; second (testbench) fails
            if len(calls) == 1:
                return _FakeProc(returncode=0)
            return _FakeProc(returncode=1, stderr="bad tb")

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = simulate(
            "entity e is end;",
            "entity tb is end; architecture a of tb is begin end;",
            language="vhdl",
        )
        assert r["success"] is False
        assert r["stage"] == "analyze_testbench"

    def test_vhdl_elaborate_fail(self, monkeypatch) -> None:
        from tools.simulate import simulate

        calls: list[int] = []

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            # Two analyzes succeed; elaborate fails
            if len(calls) <= 2:
                return _FakeProc(returncode=0)
            return _FakeProc(returncode=1, stderr="elab fail")

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = simulate(
            "entity e is end;",
            "entity tb is end; architecture a of tb is begin end;",
            language="vhdl",
        )
        assert r["success"] is False
        assert r["stage"] == "elaborate"

    def test_vhdl_run_success(self, monkeypatch) -> None:
        from tools.simulate import simulate

        _stub_run(monkeypatch, returncode=0, stdout="All tests passed\n")
        r = simulate(
            "entity e is end;",
            "entity tb is end; architecture a of tb is begin end;",
            language="vhdl",
        )
        assert r["success"] is True
        assert r["stage"] == "run"
        assert r["verdict"]["verdict"] == "pass"

    def test_vhdl_ghdl_missing(self, monkeypatch) -> None:
        from tools.simulate import simulate

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = simulate(
            "entity e is end;",
            "entity tb is end; architecture a of tb is begin end;",
            language="vhdl",
        )
        assert r["success"] is False
        assert "ghdl" in r["error"]


# ---------------------------------------------------------------------------
# tools.pnr — hit the synth/pnr subprocess paths with mocks
# ---------------------------------------------------------------------------


class TestPnrMocked:
    def _setup_successful_run(self, monkeypatch) -> list:
        """Mock subprocess.run so that yosys produces a netlist and nextpnr succeeds."""
        captured: list[list[str]] = []

        # yosys writes an actual netlist.json file so the "file exists" check passes;
        # nextpnr writes the output bitstream file.
        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            if cmd[0] == "yosys":
                # Parse the -s <script> argument and find the -json path inside it
                idx = cmd.index("-s")
                script_path = cmd[idx + 1]
                with open(script_path, "r", encoding="utf-8") as f:
                    script = f.read()
                m_json = None
                for line in script.splitlines():
                    if "-json " in line:
                        m_json = line.split("-json ")[1].strip()
                if m_json:
                    # Write minimal netlist with a top module
                    import json as _json

                    with open(m_json, "w", encoding="utf-8") as f:
                        _json.dump({"modules": {"top": {}}}, f)
                return _FakeProc(returncode=0, stdout="yosys done")
            # nextpnr path — find --asc/--textcfg/--fasm/--write output and touch it
            out_flags = ("--asc", "--textcfg", "--fasm", "--write")
            for flag in out_flags:
                if flag in cmd:
                    out_path = cmd[cmd.index(flag) + 1]
                    with open(out_path, "wb") as f:
                        f.write(b"\xde\xad\xbe\xef")
                    break
            return _FakeProc(
                returncode=0,
                stdout="Max frequency for clock 'clk': 142.34 MHz (PASS at 12.00 MHz)\n"
                "ICESTORM_LC:    42/ 5280\n",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        return captured

    def test_pnr_full_pipeline_ice40(self, monkeypatch, scratch_dir) -> None:
        from tools.pnr import place_and_route

        monkeypatch.setenv("FPGAZERO_DATA_DIR", scratch_dir)
        self._setup_successful_run(monkeypatch)
        r = place_and_route(
            code="module top(input clk, output y); assign y = clk; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
            package="tq144",
        )
        assert r["success"] is True
        assert r["target"] == "ice40"
        assert r["timing"]["max_freq_mhz"] == 142.34
        assert r["utilization"]["luts_used"] == 42
        # Bitstream persisted to disk; base64 only on request
        assert "bitstream_b64" not in r
        assert Path(r["bitstream_path"]).read_bytes() == b"\xde\xad\xbe\xef"

    def test_pnr_bitstream_b64_opt_in(self, monkeypatch, scratch_dir) -> None:
        import base64

        from tools.pnr import place_and_route

        monkeypatch.setenv("FPGAZERO_DATA_DIR", scratch_dir)
        self._setup_successful_run(monkeypatch)
        r = place_and_route(
            code="module top(input clk, output y); assign y = clk; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
            package="tq144",
            return_bitstream_b64=True,
        )
        assert r["success"] is True
        assert base64.b64decode(r["bitstream_b64"]) == b"\xde\xad\xbe\xef"

    def test_pnr_board_preset_resolves(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        self._setup_successful_run(monkeypatch)
        r = place_and_route(
            code="module top(input clk, output y); assign y = clk; endmodule",
            top_module="top",
            board="icebreaker",
        )
        # Preset should have filled in target=ice40, device=up5k
        assert r["target"] == "ice40"
        assert r["device"] == "up5k"

    def test_pnr_yosys_missing(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        _stub_run_raises(monkeypatch, FileNotFoundError())
        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
        )
        assert r["success"] is False
        assert "yosys" in r["error"]

    def test_pnr_synth_timeout(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        _stub_run_raises(
            monkeypatch, subprocess.TimeoutExpired(cmd="yosys", timeout=30)
        )
        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
        )
        assert r["success"] is False
        assert "timed out" in r["error"]

    def test_pnr_synth_failure(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        _stub_run(monkeypatch, returncode=1, stderr="synth failed")
        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
        )
        assert r["success"] is False
        assert r["stage"] == "synthesis"

    def test_pnr_no_netlist_produced(self, monkeypatch) -> None:
        """Synth reports success but no netlist.json appears — treat as failure."""
        from tools.pnr import place_and_route

        # yosys returns 0 but never writes the netlist file
        _stub_run(monkeypatch, returncode=0, stdout="yosys done")
        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
        )
        assert r["success"] is False
        assert r.get("stage") == "synthesis"

    def test_pnr_with_nextpnr_args(self, monkeypatch) -> None:
        from tools.pnr import place_and_route

        captured = self._setup_successful_run(monkeypatch)
        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
            nextpnr_args=["--seed", "42"],
        )
        assert r["success"] is True
        # Verify --seed 42 was passed through to nextpnr
        nextpnr_cmd = [c for c in captured if c[0].startswith("nextpnr-")][0]
        assert "--seed" in nextpnr_cmd
        assert "42" in nextpnr_cmd


# ---------------------------------------------------------------------------
# registry.github — mock urlopen
# ---------------------------------------------------------------------------


class _FakeUrlopen:
    """Context manager returning a fake response with read() -> bytes."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


class TestGithubMocked:
    def test_search_repos_happy_path(self, monkeypatch) -> None:
        from registry import github

        fake_json = {
            "items": [
                {
                    "full_name": "alice/uart-core",
                    "description": "A UART",
                    "stargazers_count": 42,
                    "license": {"spdx_id": "MIT"},
                    "topics": ["fpga", "uart"],
                    "default_branch": "main",
                    "html_url": "https://github.com/alice/uart-core",
                },
            ]
        }
        import json as _json

        def fake_urlopen(_req, timeout=None):  # type: ignore[no-untyped-def]
            return _FakeUrlopen(_json.dumps(fake_json).encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        results = github.search_repos("uart", language="verilog")
        assert len(results) == 1
        assert results[0]["repo"] == "alice/uart-core"
        assert results[0]["stars"] == 42
        assert results[0]["license"] == "MIT"

    def test_search_repos_http_error(self, monkeypatch) -> None:
        import urllib.error

        from registry import github

        def fake_urlopen(_req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(
                "url",
                403,
                "forbidden",
                {},
                None,  # type: ignore[arg-type]
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = github.search_repos("uart")
        # Errors are a dict (not a list) so the dispatcher flags isError
        assert isinstance(result, dict)
        assert "error" in result
        assert result["error_code"] == "network_error"

    def test_search_repos_transport_error(self, monkeypatch) -> None:
        import urllib.error

        from registry import github

        def fake_urlopen(_req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.URLError("dns lookup failed")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = github.search_repos("uart")
        assert isinstance(result, dict)
        assert "connection error" in result["error"]
        assert result["error_code"] == "network_error"

    def test_get_dict_rejects_list_response(self, monkeypatch) -> None:
        from registry import github

        def fake_urlopen(_req, timeout=None):  # type: ignore[no-untyped-def]
            return _FakeUrlopen(b"[1,2,3]")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        try:
            github._get_dict("https://example.com/foo")
        except ValueError as e:
            assert "Expected dict" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_request_retries_on_transient_status(self, monkeypatch) -> None:
        import urllib.error

        from registry import github

        # Count how many times urlopen is called
        calls: list[int] = []

        def fake_urlopen(_req, timeout=None):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) < 2:
                raise urllib.error.HTTPError(
                    "url",
                    503,
                    "service unavailable",
                    {},
                    None,  # type: ignore[arg-type]
                )
            return _FakeUrlopen(b'{"ok": true}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        # Avoid real sleep during retries
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda _s: None)
        data = github._get("https://example.com/foo")
        assert data == {"ok": True}
        assert len(calls) == 2

    def test_request_url_params_encoded(self, monkeypatch) -> None:
        from registry import github

        captured: list[str] = []

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            captured.append(req.full_url)
            return _FakeUrlopen(b"{}")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        github._get("https://example.com/foo", params={"q": "a b", "n": 10})
        assert "q=a+b" in captured[0] or "q=a%20b" in captured[0]
        assert "n=10" in captured[0]

    def test_import_core_bad_repo_format(self) -> None:
        from registry import github

        r = github.import_core("not-a-valid-repo")
        assert "error" in r

    def test_import_core_license_rejected(self, monkeypatch) -> None:
        from registry import github
        import json as _json

        # Meta response reports a disallowed license
        def fake_urlopen(_req, timeout=None):  # type: ignore[no-untyped-def]
            return _FakeUrlopen(
                _json.dumps({"license": {"spdx_id": "AGPL-3.0"}}).encode("utf-8")
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        r = github.import_core("alice/some-gpl-core")
        assert "error" in r
        assert "license" in r["error"].lower()
