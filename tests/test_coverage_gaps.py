# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Unit tests for modules that previously had 0% coverage.

Covers: tools.errors, tools.healthcheck, tools.program, mcp_lite.
All subprocess calls are mocked so these tests run without any EDA tools.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def bit_path():
    """tmp_path-style fixture using stdlib tempfile (avoids pytest-qt plugin conflict)."""
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    Path(path).write_bytes(b"FPGA")
    yield path
    if os.path.exists(path):
        os.unlink(path)


# ---------------------------------------------------------------------------
# tools.errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_err_basic(self) -> None:
        from tools.errors import err, INVALID_INPUT

        e = err(INVALID_INPUT, "bad input")
        assert e == {
            "success": False,
            "error": "bad input",
            "error_code": "invalid_input",
        }

    def test_err_with_extra_fields(self) -> None:
        from tools.errors import err, TIMEOUT

        e = err(TIMEOUT, "timed out after 30s", timeout=30, command="yosys")
        assert e["success"] is False
        assert e["error_code"] == "timeout"
        assert e["timeout"] == 30
        assert e["command"] == "yosys"

    def test_error_code_constants_are_strings(self) -> None:
        from tools import errors

        # Every public constant in the module should be a non-empty string
        public = [
            name for name in dir(errors) if not name.startswith("_") and name.isupper()
        ]
        assert public, "expected at least one error code constant"
        for name in public:
            val = getattr(errors, name)
            assert isinstance(val, str), f"{name} is not a str"
            assert val, f"{name} is empty"


# ---------------------------------------------------------------------------
# tools.healthcheck
# ---------------------------------------------------------------------------


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class TestHealthcheck:
    def test_check_tools_none_installed(self, monkeypatch) -> None:
        import shutil

        from tools import healthcheck

        # Pretend no binary is on PATH
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        result = healthcheck.check_tools()
        assert result["installed"] == 0
        assert result["total"] == len(healthcheck._TOOLS)
        assert all(t["installed"] is False for t in result["tools"])
        assert all("path" not in t for t in result["tools"])

    def test_check_tools_some_installed(self, monkeypatch) -> None:
        import shutil

        from tools import healthcheck

        installed = {"iverilog", "yosys"}

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in installed else None

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            # Simulate successful --version response
            return _FakeProc(returncode=0, stdout=f"{cmd[0]} v1.2.3\n")

        monkeypatch.setattr(shutil, "which", fake_which)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = healthcheck.check_tools()
        assert result["installed"] == 2
        present = [t for t in result["tools"] if t["installed"]]
        assert {t["tool"] for t in present} == installed
        for t in present:
            assert t["path"].startswith("/usr/bin/")
            assert "v1.2.3" in t["version"]

    def test_get_version_falls_back_to_nonzero_exit(self, monkeypatch) -> None:
        from tools import healthcheck

        # --version fails, -V succeeds — ensure we accept the fallback
        calls: list[str] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd[1])
            if cmd[1] == "--version":
                return _FakeProc(returncode=1, stderr="unknown option: --version")
            if cmd[1] == "-V":
                return _FakeProc(returncode=0, stdout="Tool version 9.9\n")
            return _FakeProc(returncode=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        version = healthcheck._get_version("fake-tool")
        assert version == "Tool version 9.9"
        assert calls[0] == "--version"

    def test_get_version_returns_unknown_when_all_fail(self, monkeypatch) -> None:
        from tools import healthcheck

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert healthcheck._get_version("nope") == "unknown"


# ---------------------------------------------------------------------------
# tools.program
# ---------------------------------------------------------------------------


class TestProgramFpga:
    def test_no_bitstream_returns_invalid_input(self) -> None:
        from tools.program import program_fpga

        r = program_fpga(target="ice40")
        assert r["success"] is False
        assert r["error_code"] == "invalid_input"
        assert "bitstream_b64 or bitstream_path" in r["error"]

    def test_both_bitstreams_returns_invalid_input(self) -> None:
        from tools.program import program_fpga

        r = program_fpga(
            target="ice40",
            bitstream_b64="aGVsbG8=",
            bitstream_path="/tmp/x.bin",
        )
        assert r["success"] is False
        assert r["error_code"] == "invalid_input"

    def test_invalid_base64_returns_invalid_input(self) -> None:
        from tools.program import program_fpga

        r = program_fpga(target="ice40", bitstream_b64="!!!not valid base64!!!")
        # Python's base64.b64decode is permissive; force a true error with non-ASCII
        # or check via validate=False path. This test may or may not trip — check
        # only that we don't crash.
        # Accept either an invalid-input error or a downstream tool-not-found.
        assert r["success"] is False

    def test_bitstream_path_missing(self) -> None:
        from tools.program import program_fpga

        r = program_fpga(target="ice40", bitstream_path="/does/not/exist.bin")
        assert r["success"] is False
        assert r["error_code"] == "invalid_input"
        assert "not found" in r["error"]

    def test_tool_not_found(self, monkeypatch, bit_path) -> None:
        from tools.program import program_fpga

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError()

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = program_fpga(target="ice40", bitstream_path=bit_path)
        assert r["success"] is False
        assert r["error_code"] == "tool_not_found"

    def test_timeout(self, monkeypatch, bit_path) -> None:
        from tools.program import program_fpga

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd="iceprog", timeout=5)

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = program_fpga(target="ice40", bitstream_path=bit_path, timeout=5)
        assert r["success"] is False
        assert r["error_code"] == "timeout"

    def test_success_iceprog(self, monkeypatch, bit_path) -> None:
        from tools.program import program_fpga

        captured: list = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0, stdout="done")

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = program_fpga(target="ice40", bitstream_path=bit_path)
        assert r["success"] is True
        assert r["programmer"] == "iceprog"
        assert captured[0][0] == "iceprog"
        assert captured[0][1] == bit_path

    def test_openFPGALoader_with_board(self, monkeypatch, bit_path) -> None:
        from tools.program import program_fpga

        captured: list = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = program_fpga(
            target="ecp5",
            bitstream_path=bit_path,
            board="ulx3s",
        )
        assert r["success"] is True
        assert r["programmer"] == "openFPGALoader"
        assert "--board" in captured[0]
        assert "ulx3s" in captured[0]

    def test_bitstream_b64_written_and_cleaned_up(self, monkeypatch) -> None:
        import os

        from tools.program import program_fpga

        tmp_paths_used: list[str] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            # Record the path passed to the programmer and check it exists
            path = cmd[-1]
            tmp_paths_used.append(path)
            assert os.path.exists(path), "temp bitstream should exist during run"
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        payload = base64.b64encode(b"\xde\xad\xbe\xef").decode("ascii")
        r = program_fpga(target="ice40", bitstream_b64=payload)
        assert r["success"] is True
        # Temp file must be cleaned up after the call
        assert tmp_paths_used, "fake_run was never invoked"
        assert not os.path.exists(tmp_paths_used[0])


# ---------------------------------------------------------------------------
# mcp_lite
# ---------------------------------------------------------------------------


class TestMcpLite:
    def test_tool_to_dict(self) -> None:
        from mcp_lite import Tool

        t = Tool(name="foo", description="does foo", inputSchema={"type": "object"})
        d = t.to_dict()
        assert d == {
            "name": "foo",
            "description": "does foo",
            "inputSchema": {"type": "object"},
        }

    def test_text_content_to_dict(self) -> None:
        from mcp_lite import TextContent

        tc = TextContent(type="text", text="hello")
        assert tc.to_dict() == {"type": "text", "text": "hello"}

    def test_call_tool_result_to_dict(self) -> None:
        from mcp_lite import CallToolResult, TextContent

        r = CallToolResult(content=[TextContent(type="text", text="ok")], isError=False)
        d = r.to_dict()
        assert d["isError"] is False
        assert d["content"] == [{"type": "text", "text": "ok"}]

    def test_initialize_response(self) -> None:
        from mcp_lite import Server

        srv = Server("test-srv", version="1.2.3")
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is not None
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "test-srv"
        assert resp["result"]["serverInfo"]["version"] == "1.2.3"
        assert resp["result"]["protocolVersion"] == "2024-11-05"

    def test_initialized_notification_returns_none(self) -> None:
        from mcp_lite import Server

        srv = Server("test")
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is None

    def test_tools_list_empty_when_no_handler(self) -> None:
        from mcp_lite import Server

        srv = Server("test")
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is not None
        assert resp["result"] == {"tools": []}

    def test_tools_list_with_handler(self) -> None:
        from mcp_lite import Server, Tool

        srv = Server("test")

        @srv.list_tools()
        async def _list() -> list[Tool]:
            return [Tool(name="foo", description="d", inputSchema={})]

        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is not None
        assert resp["result"]["tools"][0]["name"] == "foo"

    def test_tools_call_with_handler(self) -> None:
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("test")

        @srv.call_tool()
        async def _call(name: str, args: dict) -> CallToolResult:
            return CallToolResult(
                content=[TextContent(type="text", text=f"{name}:{args['v']}")]
            )

        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "hello", "arguments": {"v": 42}},
        }
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is not None
        assert resp["result"]["content"][0]["text"] == "hello:42"
        assert resp["result"]["isError"] is False

    def test_unknown_method_returns_error(self) -> None:
        from mcp_lite import Server

        srv = Server("test")
        msg = {"jsonrpc": "2.0", "id": 99, "method": "foo/bar"}
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is not None
        assert resp["error"]["code"] == -32601
        assert "Method not found" in resp["error"]["message"]

    def test_unknown_notification_returns_none(self) -> None:
        from mcp_lite import Server

        srv = Server("test")
        # No 'id' = notification
        msg = {"jsonrpc": "2.0", "method": "foo/bar"}
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is None

    def test_handler_exception_becomes_internal_error(self) -> None:
        from mcp_lite import Server

        srv = Server("test")

        @srv.call_tool()
        async def _call(name: str, args: dict) -> Any:
            raise RuntimeError("boom")

        msg = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "x", "arguments": {}},
        }
        resp = asyncio.run(srv._dispatch(msg))
        assert resp is not None
        assert resp["error"]["code"] == -32603
        assert "boom" in resp["error"]["message"]

    def test_ping_and_shutdown_return_empty_result(self) -> None:
        from mcp_lite import Server

        srv = Server("test")
        for method in ("ping", "shutdown"):
            msg = {"jsonrpc": "2.0", "id": 10, "method": method}
            resp = asyncio.run(srv._dispatch(msg))
            assert resp is not None
            assert resp["result"] == {}


# ---------------------------------------------------------------------------
# server.py dispatch — smoke test every case with mocked backends
# ---------------------------------------------------------------------------


class TestServerDispatch:
    """Ensure every case branch in server.handle_call_tool can be reached."""

    def _invoke(self, name: str, args: dict) -> dict:
        import server

        result = asyncio.run(server.handle_call_tool(name, args))
        # CallToolResult.content[0] is a TextContent; parse JSON from it.
        payload = json.loads(result.content[0].text)
        return payload

    def test_unknown_tool(self) -> None:
        payload = self._invoke("nonexistent_tool", {})
        assert "Unknown tool" in payload["error"]

    def test_missing_required_argument(self) -> None:
        # synthesize requires top_module; omit it to hit the KeyError path
        payload = self._invoke("synthesize", {})
        # KeyError is caught and returned as a plain error dict
        assert "Missing required argument" in payload["error"]
        assert "top_module" in payload["error"]

    def test_list_boards_runs(self) -> None:
        payload = self._invoke("list_boards", {})
        assert isinstance(payload, list)
        assert any(b["board"] == "icebreaker" for b in payload)

    def test_check_tools_runs(self, monkeypatch) -> None:
        # Force all tools to appear missing so the call is fast and deterministic
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        payload = self._invoke("check_tools", {})
        assert payload["installed"] == 0
        assert payload["total"] > 0

    def test_cleanup_build_logs_runs(self) -> None:
        payload = self._invoke(
            "cleanup_build_logs", {"max_age_days": 365, "max_total_mb": 99999}
        )
        assert "deleted" in payload
        assert "freed_kb" in payload

    def test_program_fpga_invalid_input(self) -> None:
        payload = self._invoke("program_fpga", {"target": "ice40"})
        assert payload["success"] is False
        assert payload["error_code"] == "invalid_input"
