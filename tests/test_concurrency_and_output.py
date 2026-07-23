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
