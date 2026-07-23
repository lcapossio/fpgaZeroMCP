# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for concurrent MCP dispatch and token-efficient tool output."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys

import pytest


# ---------------------------------------------------------------------------
# mcp_lite — concurrent request handling
# ---------------------------------------------------------------------------


def _run_server_with_input(monkeypatch, srv, lines: list[str]) -> list[str]:
    """Feed newline-delimited requests to srv.run() and return output lines."""
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    asyncio.run(srv.run())
    return stdout.getvalue().splitlines()


class TestConcurrentDispatch:
    def test_slow_tool_call_does_not_block_ping(self, monkeypatch) -> None:
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("t")

        @srv.call_tool()
        async def _ct(name: str, args: dict) -> CallToolResult:
            await asyncio.sleep(0.3)
            return CallToolResult(content=[TextContent(type="text", text="done")])

        out = _run_server_with_input(
            monkeypatch,
            srv,
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "slow", "arguments": {}},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
            ],
        )
        ids = [json.loads(line)["id"] for line in out]
        assert sorted(ids) == [1, 2, 3]
        # ping (id 3) must be answered while the slow call (id 2) is running
        assert ids.index(3) < ids.index(2)

    def test_in_flight_requests_finish_on_eof(self, monkeypatch) -> None:
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("t")

        @srv.call_tool()
        async def _ct(name: str, args: dict) -> CallToolResult:
            await asyncio.sleep(0.1)
            return CallToolResult(content=[TextContent(type="text", text="late")])

        out = _run_server_with_input(
            monkeypatch,
            srv,
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "x", "arguments": {}},
                    }
                ),
            ],
        )
        # EOF arrives immediately after the request; the response must
        # still be written before run() returns.
        assert json.loads(out[0])["id"] == 7

    def test_malformed_json_gets_parse_error(self, monkeypatch) -> None:
        from mcp_lite import Server

        srv = Server("t")
        out = _run_server_with_input(monkeypatch, srv, ["{this is not json"])
        resp = json.loads(out[0])
        assert resp["error"]["code"] == -32700
        assert resp["id"] is None


# ---------------------------------------------------------------------------
# synthesize — yosys stat parsing
# ---------------------------------------------------------------------------


_YOSYS_STAT_OUTPUT = """\
2.49. Printing statistics.

=== top ===

   Number of wires:                 14
   Number of wire bits:             38
   Number of public wires:           5
   Number of memories:               0
   Number of cells:                 12
     SB_CARRY                        4
     SB_DFF                          4
     SB_LUT4                         4

2.50. Executing JSON backend.
"""


class TestParseYosysStats:
    def test_parses_counts_and_cell_types(self) -> None:
        from tools.synthesize import parse_yosys_stats

        stats = parse_yosys_stats(_YOSYS_STAT_OUTPUT)
        assert stats["wires"] == 14
        assert stats["wire_bits"] == 38
        assert stats["cells"] == 12
        assert stats["cells_by_type"] == {"SB_CARRY": 4, "SB_DFF": 4, "SB_LUT4": 4}

    def test_uses_last_module_block(self) -> None:
        from tools.synthesize import parse_yosys_stats

        two_blocks = _YOSYS_STAT_OUTPUT.replace(
            "=== top ===",
            "=== sub ===\n\n   Number of cells:                  3\n\n=== top ===",
        )
        stats = parse_yosys_stats(two_blocks)
        assert stats["cells"] == 12

    def test_empty_output(self) -> None:
        from tools.synthesize import parse_yosys_stats

        assert parse_yosys_stats("") == {}


# ---------------------------------------------------------------------------
# textutil — log truncation
# ---------------------------------------------------------------------------


class TestTruncateLog:
    def test_short_log_untouched(self) -> None:
        from tools.textutil import truncate_log

        text = "\n".join(f"line {i}" for i in range(50))
        out, truncated = truncate_log(text)
        assert out == text
        assert truncated is False

    def test_long_log_keeps_head_and_tail(self) -> None:
        from tools.textutil import truncate_log

        text = "\n".join(f"line {i}" for i in range(500))
        out, truncated = truncate_log(text, head=20, tail=60)
        assert truncated is True
        lines = out.splitlines()
        assert lines[0] == "line 0"
        assert lines[-1] == "line 499"
        assert "[420 lines omitted]" in lines[20]
        assert len(lines) == 81


# ---------------------------------------------------------------------------
# simulate — VCD summary by default, raw VCD opt-in (real iverilog)
# ---------------------------------------------------------------------------


_TB_WITH_VCD = """\
module tb;
  reg clk = 0;
  always #1 clk = ~clk;
  initial begin
    $dumpfile("wave.vcd");
    $dumpvars(0, tb);
    #10 $display("TEST PASSED");
    $finish;
  end
endmodule
"""


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="iverilog/vvp not installed",
)
class TestSimulateVcdOutput:
    def test_summary_only_by_default(self) -> None:
        from tools.simulate import simulate

        r = simulate("module dummy; endmodule", _TB_WITH_VCD)
        assert r["success"] is True
        assert r["verdict"]["verdict"] == "pass"
        assert "vcd_summary" in r
        assert "clk" in r["vcd_summary"]["signals"]
        assert "vcd" not in r

    def test_raw_vcd_on_request(self) -> None:
        from tools.simulate import simulate

        r = simulate("module dummy; endmodule", _TB_WITH_VCD, return_vcd=True)
        assert r["success"] is True
        assert "$var" in r["vcd"]
