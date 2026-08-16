# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for v0.6.0: MCP progress notifications and multi-file simulate."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def scratch_dir():
    """tmp_path substitute — pytest-qt in this env breaks the built-in fixture."""
    path = tempfile.mkdtemp(prefix="pytest_v060_")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _completed(cmd, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# mcp_lite: send_progress + progressToken plumbing
# ---------------------------------------------------------------------------


class TestSendProgress:
    def test_noop_without_token(self, monkeypatch) -> None:
        from mcp_lite import Server

        srv = Server("t")
        sent: list[dict] = []
        monkeypatch.setattr(srv, "_send", sent.append)
        srv.send_progress(0.5, total=1.0, message="halfway")
        assert sent == []

    def test_emits_notification_with_token(self, monkeypatch) -> None:
        from mcp_lite import Server, current_progress_token

        srv = Server("t")
        sent: list[dict] = []
        monkeypatch.setattr(srv, "_send", sent.append)
        tok = current_progress_token.set("tok-1")
        try:
            srv.send_progress(0.5, total=1.0, message="halfway")
        finally:
            current_progress_token.reset(tok)
        assert len(sent) == 1
        notif = sent[0]
        assert notif["method"] == "notifications/progress"
        assert "id" not in notif
        assert notif["params"] == {
            "progressToken": "tok-1",
            "progress": 0.5,
            "total": 1.0,
            "message": "halfway",
        }

    def test_optional_fields_omitted(self, monkeypatch) -> None:
        from mcp_lite import Server, current_progress_token

        srv = Server("t")
        sent: list[dict] = []
        monkeypatch.setattr(srv, "_send", sent.append)
        tok = current_progress_token.set(7)
        try:
            srv.send_progress(0.25)
        finally:
            current_progress_token.reset(tok)
        assert sent[0]["params"] == {"progressToken": 7, "progress": 0.25}

    def test_token_extracted_from_tools_call_meta(self, monkeypatch) -> None:
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("t")
        sent: list[dict] = []
        monkeypatch.setattr(srv, "_send", sent.append)

        @srv.call_tool()
        async def _ct(name: str, args: dict) -> CallToolResult:
            srv.send_progress(0.5, total=1.0, message="working")
            return CallToolResult(content=[TextContent(type="text", text="ok")])

        resp = asyncio.run(
            srv._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "x",
                        "arguments": {},
                        "_meta": {"progressToken": "abc"},
                    },
                }
            )
        )
        assert resp is not None
        assert len(sent) == 1
        assert sent[0]["params"]["progressToken"] == "abc"

    def test_no_meta_means_no_notifications(self, monkeypatch) -> None:
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("t")
        sent: list[dict] = []
        monkeypatch.setattr(srv, "_send", sent.append)

        @srv.call_tool()
        async def _ct(name: str, args: dict) -> CallToolResult:
            srv.send_progress(0.5)
            return CallToolResult(content=[TextContent(type="text", text="ok")])

        asyncio.run(
            srv._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "x", "arguments": {}},
                }
            )
        )
        assert sent == []


# ---------------------------------------------------------------------------
# tools/progress.py reporter
# ---------------------------------------------------------------------------


class TestMakeReporter:
    def test_none_callback_is_noop(self) -> None:
        from tools.progress import make_reporter

        make_reporter(None)(0.5, "msg")  # must not raise

    def test_swallows_callback_exceptions(self) -> None:
        from tools.progress import make_reporter

        def bad(_frac: float, _msg: str) -> None:
            raise RuntimeError("boom")

        make_reporter(bad)(0.5, "msg")  # must not raise

    def test_forwards_calls(self) -> None:
        from tools.progress import make_reporter

        calls: list[tuple[float, str]] = []
        make_reporter(lambda f, m: calls.append((f, m)))(0.5, "msg")
        assert calls == [(0.5, "msg")]


# ---------------------------------------------------------------------------
# progress at tool phase boundaries (subprocess mocked)
# ---------------------------------------------------------------------------


class TestToolProgress:
    def test_synthesize_reports_phases(self, monkeypatch) -> None:
        import tools.synthesize as synth_mod

        monkeypatch.setattr(
            synth_mod.subprocess, "run", lambda cmd, **kw: _completed(cmd)
        )
        calls: list[tuple[float, str]] = []
        result = synth_mod.synthesize(
            code="module t; endmodule",
            top_module="t",
            progress=lambda f, m: calls.append((f, m)),
        )
        assert result["success"] is True
        fractions = [f for f, _ in calls]
        assert fractions == sorted(fractions)
        assert fractions[0] < 1.0
        assert fractions[-1] == 1.0
        assert all(m for _, m in calls)

    def test_place_and_route_reports_phases(self, monkeypatch) -> None:
        import tools.pnr as pnr_mod

        def fake_run(cmd, **kw):
            if cmd[0] == "yosys":
                # Create the netlist the script asks for so PnR proceeds
                script = Path(cmd[2]).read_text(encoding="utf-8")
                m = re.search(r"-json (\S+)", script)
                assert m
                Path(m.group(1)).write_text('{"modules": {}}', encoding="utf-8")
            return _completed(cmd)

        monkeypatch.setattr(pnr_mod.subprocess, "run", fake_run)
        calls: list[tuple[float, str]] = []
        result = pnr_mod.place_and_route(
            code="module t; endmodule",
            top_module="t",
            target="ice40",
            device="up5k",
            package="sg48",
            progress=lambda f, m: calls.append((f, m)),
        )
        assert result["success"] is True
        fractions = [f for f, _ in calls]
        assert fractions == sorted(fractions)
        assert len(fractions) >= 4
        assert fractions[-1] == 1.0

    def test_raising_progress_does_not_break_tool(self, monkeypatch) -> None:
        import tools.synthesize as synth_mod

        monkeypatch.setattr(
            synth_mod.subprocess, "run", lambda cmd, **kw: _completed(cmd)
        )

        def bad(_f: float, _m: str) -> None:
            raise RuntimeError("client went away")

        result = synth_mod.synthesize(
            code="module t; endmodule", top_module="t", progress=bad
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# simulate: files / project_dir input modes
# ---------------------------------------------------------------------------


class TestSimulateMultiFile:
    def test_testbench_required(self) -> None:
        from tools.simulate import simulate

        result = simulate(code="module t; endmodule")
        assert result["success"] is False
        assert result["error_code"] == "invalid_input"

    def test_no_design_source_rejected(self) -> None:
        from tools.simulate import simulate

        result = simulate(testbench="module tb; endmodule")
        assert result["success"] is False
        assert result["error_code"] == "invalid_input"

    def test_multiple_modes_rejected(self) -> None:
        from tools.simulate import simulate

        result = simulate(
            code="module t; endmodule",
            files={"a.v": "module a; endmodule"},
            testbench="module tb; endmodule",
        )
        assert result["success"] is False
        assert result["error_code"] == "invalid_input"

    def test_files_mode_passes_all_sources_to_iverilog(self, monkeypatch) -> None:
        import tools.simulate as sim_mod

        cmds: list[list[str]] = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)
            return _completed(cmd, stdout="PASS")

        monkeypatch.setattr(sim_mod.subprocess, "run", fake_run)
        result = sim_mod.simulate(
            files={
                "counter.v": "module counter; endmodule",
                "top.v": "module top; endmodule",
            },
            testbench='module tb; initial $display("PASS"); endmodule',
        )
        assert result["success"] is True
        compile_cmd = cmds[0]
        assert compile_cmd[0] == "iverilog"
        basenames = [Path(a).name for a in compile_cmd]
        assert "counter.v" in basenames
        assert "top.v" in basenames
        assert "testbench.v" in basenames

    def test_project_dir_mode_adds_include_dir(self, monkeypatch, scratch_dir) -> None:
        import tools.simulate as sim_mod

        (scratch_dir / "top.v").write_text("module top; endmodule", encoding="utf-8")
        cmds: list[list[str]] = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)
            return _completed(cmd)

        monkeypatch.setattr(sim_mod.subprocess, "run", fake_run)
        result = sim_mod.simulate(
            project_dir=str(scratch_dir),
            testbench="module tb; endmodule",
        )
        assert result["success"] is True
        compile_cmd = cmds[0]
        assert "-I" in compile_cmd
        basenames = [Path(a).name for a in compile_cmd]
        assert "top.v" in basenames

    def test_vhdl_files_mode_analyzes_each_design_file(self, monkeypatch) -> None:
        import tools.simulate as sim_mod

        cmds: list[list[str]] = []

        def fake_run(cmd, **kw):
            cmds.append(cmd)
            return _completed(cmd)

        monkeypatch.setattr(sim_mod.subprocess, "run", fake_run)
        result = sim_mod.simulate(
            language="vhdl",
            files={
                "a.vhd": "entity a is end a;",
                "b.vhd": "entity b is end b;",
            },
            testbench="entity tb is end tb;",
        )
        assert result["success"] is True
        analyzed = [
            Path(cmd[-1]).name for cmd in cmds if cmd[0] == "ghdl" and cmd[1] == "-a"
        ]
        assert "a.vhd" in analyzed
        assert "b.vhd" in analyzed
        assert "testbench.vhd" in analyzed

    def test_schema_requires_only_testbench(self) -> None:
        import server

        tools = asyncio.run(server.handle_list_tools())
        sim = next(t for t in tools if t.name == "simulate")
        assert sim.inputSchema["required"] == ["testbench"]
        props = sim.inputSchema["properties"]
        assert "files" in props
        assert "project_dir" in props


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="iverilog not installed")
class TestSimulateMultiFileLive:
    def test_two_file_design_simulates(self) -> None:
        from tools.simulate import simulate

        result = simulate(
            files={
                "inv.v": "module inv(input a, output y); assign y = ~a; endmodule",
                "top.v": (
                    "module top(input a, output y);\n  inv u0(.a(a), .y(y));\nendmodule"
                ),
            },
            testbench=(
                "module tb;\n"
                "  reg a; wire y;\n"
                "  top dut(.a(a), .y(y));\n"
                "  initial begin\n"
                "    a = 1'b0; #1;\n"
                '    if (y === 1\'b1) $display("PASS"); else $display("FAIL");\n'
                "    $finish;\n"
                "  end\n"
                "endmodule"
            ),
        )
        assert result["success"] is True
        assert result["verdict"]["verdict"] == "pass"
