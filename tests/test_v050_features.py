# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for v0.5.0: protocol negotiation, structuredContent, cancellation,
error_code envelope, perf work (parallel check_tools, parse window, tarball)."""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Protocol version negotiation
# ---------------------------------------------------------------------------


class TestVersionNegotiation:
    def _init(self, requested: str) -> tuple[dict, object]:
        from mcp_lite import Server

        srv = Server("t")
        resp = asyncio.run(
            srv._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": requested},
                }
            )
        )
        assert resp is not None
        return resp["result"], srv

    def test_known_version_echoed(self) -> None:
        result, srv = self._init("2024-11-05")
        assert result["protocolVersion"] == "2024-11-05"
        assert srv._negotiated_version == "2024-11-05"

    def test_unknown_version_gets_latest(self) -> None:
        from mcp_lite import LATEST_PROTOCOL_VERSION

        result, _srv = self._init("1999-01-01")
        assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION

    def test_latest_version_echoed(self) -> None:
        from mcp_lite import LATEST_PROTOCOL_VERSION

        result, _srv = self._init(LATEST_PROTOCOL_VERSION)
        assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# structuredContent gating
# ---------------------------------------------------------------------------


class TestStructuredContent:
    def _server(self):
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("t")

        @srv.call_tool()
        async def _ct(name: str, args: dict) -> CallToolResult:
            return CallToolResult(
                content=[TextContent(type="text", text='{"x": 1}')],
                structuredContent={"x": 1},
            )

        return srv

    def _call(self, srv, version: str) -> dict:
        asyncio.run(
            srv._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": version},
                }
            )
        )
        resp = asyncio.run(
            srv._dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "x", "arguments": {}},
                }
            )
        )
        assert resp is not None
        return resp["result"]

    def test_sent_to_2025_06_18_clients(self) -> None:
        result = self._call(self._server(), "2025-06-18")
        assert result["structuredContent"] == {"x": 1}

    def test_stripped_for_old_clients(self) -> None:
        result = self._call(self._server(), "2024-11-05")
        assert "structuredContent" not in result

    def test_server_results_carry_structured_content(self, monkeypatch) -> None:
        import tools.lint as tools_lint
        import server

        monkeypatch.setattr(
            tools_lint, "lint_hdl", lambda **_kw: {"success": True, "tool": "iverilog"}
        )
        result = asyncio.run(server.handle_call_tool("lint_hdl", {"code": "x"}))
        assert result.structuredContent == {"success": True, "tool": "iverilog"}


# ---------------------------------------------------------------------------
# notifications/cancelled
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancelled_request_gets_no_response(self, monkeypatch) -> None:
        from mcp_lite import CallToolResult, Server, TextContent

        srv = Server("t")

        @srv.call_tool()
        async def _ct(name: str, args: dict) -> CallToolResult:
            await asyncio.sleep(5)
            return CallToolResult(content=[TextContent(type="text", text="late")])

        lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/call",
                    "params": {"name": "slow", "arguments": {}},
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 42, "reason": "user"},
                }
            ),
            json.dumps({"jsonrpc": "2.0", "id": 43, "method": "ping"}),
        ]
        import sys

        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdin", stdin)
        monkeypatch.setattr(sys, "stdout", stdout)

        start = time.monotonic()
        asyncio.run(srv.run())
        elapsed = time.monotonic() - start

        ids = [json.loads(line)["id"] for line in stdout.getvalue().splitlines()]
        assert 43 in ids  # ping answered
        assert 42 not in ids  # cancelled request produced no response
        assert elapsed < 4  # run() did not wait out the 5 s sleep


# ---------------------------------------------------------------------------
# error_code envelope
# ---------------------------------------------------------------------------


class TestErrorCodes:
    def test_all_used_codes_are_declared(self) -> None:
        """Every "error_code" literal in the source must exist in tools/errors.py."""
        import tools.errors as errors_mod

        declared = {
            v for k, v in vars(errors_mod).items() if k.isupper() and isinstance(v, str)
        }
        root = Path(__file__).parent.parent
        used: set[str] = set()
        for pattern in ("tools/*.py", "registry/*.py", "server.py", "mcp_lite.py"):
            for path in root.glob(pattern):
                used.update(
                    re.findall(
                        r'"error_code": "(\w+)"', path.read_text(encoding="utf-8")
                    )
                )
        assert used, "expected error_code literals in source"
        undeclared = used - declared
        assert not undeclared, f"codes missing from tools/errors.py: {undeclared}"

    def test_failed_synthesis_has_error_envelope(self, monkeypatch) -> None:
        import subprocess

        from tools.synthesize import synthesize

        class _Proc:
            returncode = 1
            stdout = "ERROR: syntax error"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
        r = synthesize("module m; endmodule", top_module="m")
        assert r["success"] is False
        assert r["error_code"] == "synthesis_failed"
        assert "error" in r

    def test_unknown_core_has_code(self) -> None:
        from registry.resolver import CoreRegistry

        r = CoreRegistry().get_core("definitely_not_a_core")
        assert r["error_code"] == "core_not_found"


# ---------------------------------------------------------------------------
# check_tools
# ---------------------------------------------------------------------------


class TestCheckTools:
    def test_covers_all_shelled_out_tools(self, monkeypatch) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _n: None)
        from tools.healthcheck import check_tools

        r = check_tools()
        names = {t["tool"] for t in r["tools"]}
        for required in (
            "nextpnr-nexus",
            "nextpnr-gowin",
            "sby",
            "iceprog",
            "openFPGALoader",
            "ecpprog",
            "litex",
        ):
            assert required in names
        assert r["total"] == len(r["tools"])


# ---------------------------------------------------------------------------
# build_status parse window
# ---------------------------------------------------------------------------


class TestParseWindow:
    def _record(self, tmp: Path, content: bytes):
        from tools.build_manager import BuildRecord

        log = tmp / "b.log"
        log.write_bytes(content)
        return BuildRecord(
            build_id="x",
            label="",
            cmd=[],
            log_path=str(log),
            work_dir=".",
            start_time=0.0,
        )

    def test_small_log_returned_whole(self) -> None:
        tmp = Path(tempfile_dir())
        rec = self._record(tmp, b"line1\nline2\n")
        assert rec.parse_window() == "line1\nline2\n"

    def test_large_log_returns_tail_window(self) -> None:
        tmp = Path(tempfile_dir())
        blob = b"x" * (3 * 1024 * 1024) + b"\nlast line\n"
        rec = self._record(tmp, blob)
        window = rec.parse_window()
        assert len(window) <= rec._PARSE_WINDOW_BYTES
        assert window.endswith("last line\n")


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="pytest_v050_")


# ---------------------------------------------------------------------------
# cleanup covers bitstreams
# ---------------------------------------------------------------------------


class TestCleanupBitstreams:
    def test_bitstreams_trimmed_by_age(self, monkeypatch) -> None:
        import os

        from tools.build_manager import BuildManager

        data_dir = Path(tempfile_dir())
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(data_dir))
        bs_dir = data_dir / "bitstreams"
        bs_dir.mkdir(parents=True)
        old = bs_dir / "old.asc"
        new = bs_dir / "new.asc"
        old.write_bytes(b"x" * 100)
        new.write_bytes(b"x" * 100)
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))

        r = BuildManager.cleanup_logs(max_age_days=7, max_total_mb=999)
        assert r["deleted"] == 1
        assert not old.exists()
        assert new.exists()


# ---------------------------------------------------------------------------
# GitHub tarball download
# ---------------------------------------------------------------------------


def _make_tarball(root: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"{root}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestTarballImport:
    def test_extracts_wanted_files(self, monkeypatch) -> None:
        from registry import github

        tarball = _make_tarball(
            "owner-repo-abc123",
            {"rtl/top.v": "module top; endmodule", "README.md": "# nope"},
        )
        monkeypatch.setattr(github, "_request_bytes", lambda url, **_kw: tarball)
        out = github._download_tarball_files("o", "r", "main", ["rtl/top.v"])
        assert out == {"rtl/top.v": "module top; endmodule"}

    def test_failure_returns_none_for_fallback(self, monkeypatch) -> None:
        from registry import github

        def boom(url, **_kw):
            raise OSError("network down")

        monkeypatch.setattr(github, "_request_bytes", boom)
        assert github._download_tarball_files("o", "r", "main", ["a.v"]) is None

    def test_size_cap_enforced(self, monkeypatch) -> None:
        from registry import github

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, _n):
                return b"x" * (1024 * 1024)  # endless megabytes

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
        with pytest.raises(ValueError, match="cap"):
            github._request_bytes("http://x", max_bytes=4 * 1024 * 1024)


# ---------------------------------------------------------------------------
# lsp WSL fallback
# ---------------------------------------------------------------------------


class TestLspWslFallback:
    def test_diagnostics_use_wsl_when_native_missing(self, monkeypatch) -> None:
        import shutil
        import subprocess

        import tools.lsp as lsp

        captured: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **_kw):
            captured.append(cmd)
            return _Proc()

        monkeypatch.setattr(shutil, "which", lambda _n: None)
        monkeypatch.setattr(lsp, "_wsl_has_verilator", lambda: True)
        monkeypatch.setattr(subprocess, "run", fake_run)
        r = lsp._verilator_diagnostics("C:\\tmp\\diag.v", "verilog")
        assert r["tool"] == "verilator"
        assert captured[0][:2] == ["wsl", "verilator"]
        assert "/mnt/c/tmp/diag.v" in captured[0]
