# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Close the remaining coverage gaps across modules.

Targets: mcp_lite.run() stdio loop, registry/resolver branches,
tools/build_manager monitor thread + cleanup, tools/synthesize filelist
and VHDL paths, tools/workspace fallback selection.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture
def scratch_dir(monkeypatch):
    """Yield a temp directory and clean up. Avoids pytest-qt `tmp_path` conflict.

    Also whitelists the scratch path in FPGAZERO_ALLOWED_DIRS so tests that
    pass it as `project_dir` aren't rejected by the security check on Linux
    (where /tmp is outside both cwd and $HOME).
    """
    path = tempfile.mkdtemp(prefix="pytest_final_")
    monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", path)
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# ---------------------------------------------------------------------------
# mcp_lite.run() — drive the stdio loop with in-memory pipes
# ---------------------------------------------------------------------------


class TestMcpLiteRunLoop:
    def test_run_loop_processes_request_and_exits_on_eof(self, monkeypatch) -> None:
        """Feed one initialize request, then EOF, and verify a well-formed response."""
        from mcp_lite import Server

        srv = Server("test-srv", version="0.0.1")

        # Build a fake stdin: one JSON line, then EOF
        request = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
            + "\n"
        )
        fake_stdin = io.StringIO(request)
        fake_stdout = io.StringIO()

        monkeypatch.setattr(sys, "stdin", fake_stdin)
        monkeypatch.setattr(sys, "stdout", fake_stdout)

        asyncio.run(srv.run())

        out = fake_stdout.getvalue().strip()
        assert out, "server should have written a response"
        resp = json.loads(out)
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "test-srv"

    def test_run_loop_ignores_blank_lines(self, monkeypatch) -> None:
        from mcp_lite import Server

        srv = Server("test")
        # Blank lines followed by a ping, then EOF
        fake_stdin = io.StringIO(
            '\n\n  \n{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n'
        )
        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        monkeypatch.setattr(sys, "stdout", fake_stdout)

        asyncio.run(srv.run())
        resp = json.loads(fake_stdout.getvalue().strip())
        assert resp["id"] == 7

    def test_run_loop_handles_malformed_json(self, monkeypatch) -> None:
        from mcp_lite import Server

        srv = Server("test")
        # Malformed line gets a JSON-RPC parse error; the valid ping still works.
        fake_stdin = io.StringIO(
            'not valid json\n{"jsonrpc": "2.0", "id": 8, "method": "ping"}\n'
        )
        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        monkeypatch.setattr(sys, "stdout", fake_stdout)

        asyncio.run(srv.run())
        lines = [line for line in fake_stdout.getvalue().splitlines() if line.strip()]
        assert len(lines) == 2
        parse_err = json.loads(lines[0])
        assert parse_err["error"]["code"] == -32700
        assert parse_err["id"] is None
        assert json.loads(lines[1])["id"] == 8

    def test_run_loop_does_not_write_for_notifications(self, monkeypatch) -> None:
        from mcp_lite import Server

        srv = Server("test")
        # Notification (no id) + notification, then EOF. Nothing should be written.
        fake_stdin = io.StringIO(
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        monkeypatch.setattr(sys, "stdout", fake_stdout)

        asyncio.run(srv.run())
        assert fake_stdout.getvalue() == ""


# ---------------------------------------------------------------------------
# registry/resolver
# ---------------------------------------------------------------------------


def _write_core(
    base: Path,
    name: str,
    *,
    category: str = "misc",
    files: list[str] | None = None,
    lang: str = "verilog",
    params: dict | None = None,
) -> Path:
    """Create a valid core directory for testing."""
    core_dir = base / name
    core_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": f"{name} core",
        "author": "test",
        "license": "MIT",
        "language": lang,
        "category": category,
        "files": files or [f"{name}.v"],
        "tags": [],
        "parameters": params or {},
        "ports": {},
    }
    (core_dir / "core.json").write_text(json.dumps(manifest), encoding="utf-8")
    for f in manifest["files"]:
        fp = core_dir / f
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f"// {f}\nmodule {name}; endmodule\n", encoding="utf-8")
    return core_dir


class TestRegistryResolver:
    def test_extra_core_paths_from_env(self, monkeypatch, scratch_dir) -> None:
        from registry.resolver import _extra_core_paths

        sep = os.pathsep
        monkeypatch.setenv("USERCORES_PATH", f"{scratch_dir}{sep}  {sep}/some/other")
        paths = _extra_core_paths()
        # Blank entries dropped; the sep with whitespace should resolve normally
        assert Path(scratch_dir) in paths

    def test_extra_core_paths_from_config(self, monkeypatch, scratch_dir) -> None:
        """Config file with core_paths list is honored."""
        from registry import resolver

        fake_config = scratch_dir / "config.json"
        fake_config.write_text(json.dumps({"core_paths": ["/foo", "/bar"]}))
        monkeypatch.setattr(resolver, "CONFIG_FILE", fake_config)
        monkeypatch.delenv("USERCORES_PATH", raising=False)
        paths = resolver._extra_core_paths()
        assert Path("/foo") in paths
        assert Path("/bar") in paths

    def test_extra_core_paths_malformed_config(
        self, monkeypatch, scratch_dir, caplog
    ) -> None:
        """Malformed config logs warning but doesn't crash."""
        from registry import resolver

        fake_config = scratch_dir / "config.json"
        fake_config.write_text("not json")
        monkeypatch.setattr(resolver, "CONFIG_FILE", fake_config)
        monkeypatch.delenv("USERCORES_PATH", raising=False)
        paths = resolver._extra_core_paths()
        assert paths == []  # no crashing

    def test_load_dir_skips_invalid_manifest(self, scratch_dir, caplog) -> None:
        from registry.resolver import CoreRegistry

        # Write an invalid core.json
        bad_core = scratch_dir / "bad_core"
        bad_core.mkdir()
        (bad_core / "core.json").write_text("not json", encoding="utf-8")

        reg = CoreRegistry(cores_dir=scratch_dir)
        assert "bad_core" not in reg._cache  # invalid manifest ignored

    def test_load_dir_skips_unsafe_file_paths(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        bad = scratch_dir / "unsafe_core"
        bad.mkdir()
        manifest = {
            "name": "unsafe_core",
            "version": "1.0",
            "description": "",
            "author": "t",
            "license": "MIT",
            "language": "verilog",
            "category": "misc",
            "files": ["../escape.v"],
        }
        (bad / "core.json").write_text(json.dumps(manifest), encoding="utf-8")

        reg = CoreRegistry(cores_dir=scratch_dir)
        # Unsafe path triggers ValueError inside _load_dir; core skipped
        assert "unsafe_core" not in reg._cache

    def test_list_cores_category_filter(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        _write_core(scratch_dir, "coreA", category="memory")
        _write_core(scratch_dir, "coreB", category="communication")
        reg = CoreRegistry(cores_dir=scratch_dir)

        all_cores = reg.list_cores()
        assert len(all_cores) == 2

        mem_only = reg.list_cores(category="memory")
        assert len(mem_only) == 1
        assert mem_only[0]["name"] == "coreA"

    def test_get_core_not_found(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.get_core("nonexistent")
        assert "error" in r

    def test_get_core_missing_file_reports_error_in_body(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        _write_core(scratch_dir, "c1", files=["c1.v"])
        # Delete the HDL file after manifest is written so manifest is valid
        # but the file is missing at read time
        (scratch_dir / "c1" / "c1.v").unlink()
        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.get_core("c1")
        assert "c1.v" in r["files"]
        assert "ERROR" in r["files"]["c1.v"]

    def test_generate_ip_with_parameters(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        _write_core(
            scratch_dir,
            "fifo",
            params={
                "WIDTH": {
                    "type": "integer",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 64,
                }
            },
        )
        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.generate_ip("fifo", parameters={"WIDTH": 32})
        assert "error" not in r
        assert r["parameters_used"]["WIDTH"] == 32
        assert ".WIDTH(32)" in r["instantiation"]

    def test_generate_ip_instance_name(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        _write_core(scratch_dir, "simple")
        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.generate_ip("simple", instance_name="u_simple")
        assert " u_simple " in r["instantiation"]

    def test_generate_ip_boolean_and_string_params(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        _write_core(
            scratch_dir,
            "mixed",
            params={
                "USE_CACHE": {"type": "boolean", "default": False},
                "MODE": {"type": "string", "default": "fast"},
            },
        )
        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.generate_ip("mixed", parameters={"USE_CACHE": True, "MODE": "slow"})
        assert r["parameters_used"]["USE_CACHE"] is True
        assert r["parameters_used"]["MODE"] == "slow"
        # Verilog snippet: booleans rendered as 1/0, strings quoted
        assert ".USE_CACHE(1)" in r["instantiation"]
        assert '.MODE("slow")' in r["instantiation"]

    def test_generate_ip_not_found(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.generate_ip("ghost")
        assert "error" in r

    def test_import_fusesoc_missing_file(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.import_fusesoc_core("/does/not/exist.core")
        assert "error" in r

    def test_import_fusesoc_parse_failure(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        bad = scratch_dir / "bogus.core"
        bad.write_text("{[not valid yaml")
        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.import_fusesoc_core(str(bad))
        assert "error" in r

    def test_import_fusesoc_happy_path(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        # Write a tiny CAPI2 core file alongside its referenced HDL
        core_file = scratch_dir / "my.core"
        core_file.write_text(
            "CAPI=2:\n"
            "name: ::my_core:1.0\n"
            "description: test\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - top.v\n"
            "    file_type: verilogSource\n"
            "targets:\n"
            "  default:\n"
            "    filesets: [rtl]\n"
        )
        (scratch_dir / "top.v").write_text("module my_core; endmodule\n")

        # Use a separate cores dir for the registry destination
        dest = scratch_dir / "cores"
        dest.mkdir()
        reg = CoreRegistry(cores_dir=dest)
        r = reg.import_fusesoc_core(str(core_file))
        assert r.get("imported") is True
        assert "my_core" in r["core_name"]
        # Cache reloaded: the core should now be listed
        assert r["core_name"] in reg._cache

    def test_reload_returns_count(self, scratch_dir) -> None:
        from registry.resolver import CoreRegistry

        _write_core(scratch_dir, "a")
        _write_core(scratch_dir, "b")
        reg = CoreRegistry(cores_dir=scratch_dir)
        r = reg.reload()
        assert r["success"] is True
        assert r["cores_loaded"] == 2


# ---------------------------------------------------------------------------
# tools/build_manager
# ---------------------------------------------------------------------------


class TestBuildManagerExtra:
    def test_monitor_thread_records_returncode(self, monkeypatch, scratch_dir) -> None:
        """Start a fake fast-exiting subprocess and verify the monitor thread records rc."""
        from tools import build_manager

        # Use real Popen wrapping Python itself with -c isn't allowed (allowlist).
        # Use iverilog since it's actually installed in this env.
        # If not installed, skip.
        import shutil as _shutil

        if not _shutil.which("iverilog"):
            pytest.skip("iverilog not available")

        mgr = build_manager.BuildManager()
        # iverilog -V exits quickly with rc 0
        r = mgr.start(cmd=["iverilog", "-V"])
        assert r["success"] is True
        bid = r["build_id"]
        # Wait for the monitor thread to complete
        for _ in range(40):
            s = mgr.status(bid, parse=False)
            if s["status"] != "running":
                break
            time.sleep(0.05)
        final = mgr.status(bid, parse=False)
        assert final["status"] in ("success", "failed")
        assert final["returncode"] is not None

    def test_build_record_tail_small_file(self, scratch_dir) -> None:
        from tools.build_manager import BuildRecord

        log = scratch_dir / "small.log"
        log.write_text("line1\nline2\nline3\n", encoding="utf-8")
        rec = BuildRecord(
            build_id="x",
            label="t",
            cmd=[],
            log_path=str(log),
            work_dir=".",
            start_time=0.0,
        )
        assert rec.tail(2) == "line2\nline3\n"
        assert rec.log_size_kb() >= 0

    def test_build_record_tail_large_file(self, scratch_dir) -> None:
        from tools.build_manager import BuildRecord

        log = scratch_dir / "big.log"
        # Write > 64KB to trigger the seek-from-end path
        with open(log, "w", encoding="utf-8") as f:
            for i in range(20000):
                f.write(f"line{i}\n")

        rec = BuildRecord(
            build_id="x",
            label="t",
            cmd=[],
            log_path=str(log),
            work_dir=".",
            start_time=0.0,
        )
        tail = rec.tail(5)
        # Tail should contain the last few lines
        assert "line19999" in tail

    def test_build_record_tail_missing_file(self) -> None:
        from tools.build_manager import BuildRecord

        rec = BuildRecord(
            build_id="x",
            label="t",
            cmd=[],
            log_path="/does/not/exist.log",
            work_dir=".",
            start_time=0.0,
        )
        assert rec.tail() == ""
        assert rec.log_size_kb() == 0.0
        assert rec.full_log() == ""

    def test_cleanup_logs_by_age(self, monkeypatch, scratch_dir) -> None:
        from tools.build_manager import BuildManager

        # Create fake build logs under the data root
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        logs_dir = scratch_dir / "builds"
        logs_dir.mkdir(parents=True)
        old = logs_dir / "old.log"
        new = logs_dir / "new.log"
        old.write_text("x" * 100)
        new.write_text("x" * 100)
        # Age the old log 30 days
        now = time.time()
        os.utime(old, (now - 30 * 86400, now - 30 * 86400))
        r = BuildManager.cleanup_logs(max_age_days=7, max_total_mb=999)
        assert r["deleted"] == 1
        assert not old.exists()
        assert new.exists()

    def test_cleanup_logs_by_size(self, monkeypatch, scratch_dir) -> None:
        from tools.build_manager import BuildManager

        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        logs_dir = scratch_dir / "builds"
        logs_dir.mkdir(parents=True)
        # Create 3 logs, each 1 KB, and trim to 2 KB total
        for i in range(3):
            (logs_dir / f"b{i}.log").write_bytes(b"x" * 1024)
            # Stagger mtime so we know which gets deleted first
            os.utime(
                logs_dir / f"b{i}.log", (time.time() - (10 - i), time.time() - (10 - i))
            )
        # max_total_mb=0 with 1KB min would force deletion; use a very small cap
        # cleanup uses bytes = max_total_mb * 1024 * 1024; 1 KB ≈ 0.001 MB; clamp to 0
        r = BuildManager.cleanup_logs(max_age_days=365, max_total_mb=0)
        # All three should have been deleted
        assert r["deleted"] == 3

    def test_cleanup_logs_no_directory(self, monkeypatch, scratch_dir) -> None:
        from tools.build_manager import BuildManager

        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir / "empty"))
        r = BuildManager.cleanup_logs()
        assert r == {"deleted": 0, "freed_kb": 0}


# ---------------------------------------------------------------------------
# tools/synthesize — VHDL path, filelist edges, include walk
# ---------------------------------------------------------------------------


class TestSynthesizeMoreCoverage:
    def test_vhdl_synth_script_generated(self, monkeypatch, scratch_dir) -> None:
        """VHDL source goes through ghdl-yosys-plugin with lowercased top."""
        from tools.synthesize import synthesize

        captured: list[list[str]] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            captured.append(cmd)
            # Read the yosys script to verify ghdl invocation structure
            idx = cmd.index("-s")
            script_path = cmd[idx + 1]
            with open(script_path, "r", encoding="utf-8") as f:
                script = f.read()
            # Stash script content for assertions
            captured.append(["SCRIPT", script])  # type: ignore[list-item]
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        synthesize(
            code="entity MyEntity is end; architecture a of MyEntity is begin end;",
            top_module="MyEntity",
            target="generic",
            language="vhdl",
        )
        # The script should call `ghdl --std=08 ... -e MyEntity` and then `synth -top myentity`
        scripts = [c[1] for c in captured if c and c[0] == "SCRIPT"]
        assert scripts, "no script captured"
        assert "ghdl --std=08" in scripts[0]
        assert "-e MyEntity" in scripts[0]
        # synth_cmd for generic is "synth", using lowercased top
        assert "synth -top myentity" in scripts[0]

    def test_synthesize_project_dir_uses_filelist(
        self, monkeypatch, scratch_dir
    ) -> None:
        """project_dir with files.f overrides globbing — respects compile order."""
        from tools.synthesize import synthesize

        (scratch_dir / "top.v").write_text("module top; endmodule\n")
        (scratch_dir / "sub.v").write_text("module sub; endmodule\n")
        (scratch_dir / "files.f").write_text(
            "# override order\nsub.v\ntop.v\n+define+FAST\n"
        )

        captured: list[str] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            idx = cmd.index("-s")
            script_path = cmd[idx + 1]
            captured.append(Path(script_path).read_text(encoding="utf-8"))
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        synthesize(project_dir=str(scratch_dir), top_module="top", target="generic")
        assert captured, "subprocess.run wasn't called"
        script = captured[0]
        # sub.v should appear before top.v in the read_verilog sequence
        sub_pos = script.find("sub.v")
        top_pos = script.find("top.v")
        assert sub_pos > 0 and top_pos > 0 and sub_pos < top_pos
        # +define+FAST should become -DFAST on read_verilog
        assert "-DFAST" in script

    def test_synthesize_include_dirs_autowalk(self, monkeypatch, scratch_dir) -> None:
        """Subdirectories with .vh/.svh headers are auto-added as -I paths."""
        from tools.synthesize import synthesize

        (scratch_dir / "top.v").write_text("module top; endmodule\n")
        inc = scratch_dir / "inc"
        inc.mkdir()
        (inc / "defs.vh").write_text("// header\n")

        captured: list[str] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            idx = cmd.index("-s")
            script_path = cmd[idx + 1]
            captured.append(Path(script_path).read_text(encoding="utf-8"))
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        synthesize(project_dir=str(scratch_dir), top_module="top", target="generic")
        script = captured[0]
        # both the project root and the inc/ subdir should be present as -I flags
        assert "-I" in script
        assert "inc" in script

    def test_synthesize_timeout(self, monkeypatch) -> None:
        from tools.synthesize import synthesize

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd="yosys", timeout=120)

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = synthesize(code="module t; endmodule", top_module="t", target="generic")
        assert r["success"] is False
        assert "timed out" in r["error"]

    def test_synthesize_yosys_missing(self, monkeypatch) -> None:
        from tools.synthesize import synthesize

        def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", fake_run)
        r = synthesize(code="module t; endmodule", top_module="t")
        assert "yosys" in r["error"]

    def test_parse_filelist_nested(self, scratch_dir) -> None:
        """Nested `-f other.f` directives are resolved recursively."""
        from tools.synthesize import _parse_filelist

        (scratch_dir / "a.v").write_text("module a; endmodule\n")
        (scratch_dir / "b.v").write_text("module b; endmodule\n")
        (scratch_dir / "nested.f").write_text("b.v\n+incdir+rtl\n")
        (scratch_dir / "files.f").write_text("a.v\n-f nested.f\n")
        sources, incdirs, defines = _parse_filelist(
            str(scratch_dir / "files.f"), str(scratch_dir)
        )
        # Both a.v and b.v from the nested filelist
        basenames = [os.path.basename(s) for s in sources]
        assert "a.v" in basenames
        assert "b.v" in basenames
        # incdir from nested file too
        assert any("rtl" in d for d in incdirs)


# ---------------------------------------------------------------------------
# tools/pnr — constraint auto-detection + VHDL + work_dir persistence
# ---------------------------------------------------------------------------


def _pnr_success_mock(captured: list[list[str]], netlist_contents: dict):
    """Return a subprocess.run replacement that simulates a successful synth+PnR."""

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        if cmd[0] == "yosys":
            # Look up the -json target path inside the script
            idx = cmd.index("-s")
            script = Path(cmd[idx + 1]).read_text(encoding="utf-8")
            for line in script.splitlines():
                if "-json " in line:
                    json_path = line.split("-json ", 1)[1].strip()
                    Path(json_path).write_text(json.dumps(netlist_contents))
            return _FakeProc(returncode=0, stdout="yosys ok")
        # nextpnr path — touch the output file
        for flag in ("--asc", "--textcfg", "--fasm", "--write"):
            if flag in cmd:
                Path(cmd[cmd.index(flag) + 1]).write_bytes(b"BITSTREAM")
                break
        return _FakeProc(returncode=0, stdout="nextpnr ok")

    return fake_run


class TestPnrMoreCoverage:
    def test_constraint_auto_detection(self, monkeypatch, scratch_dir) -> None:
        """PnR with project_dir + no constraint string should auto-pick the pcf."""
        from tools.pnr import place_and_route

        (scratch_dir / "top.v").write_text("module top; endmodule\n")
        (scratch_dir / "pins.pcf").write_text("set_io clk 35\n")

        captured: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            _pnr_success_mock(captured, {"modules": {"top": {}}}),
        )
        r = place_and_route(
            project_dir=str(scratch_dir),
            top_module="top",
            target="ice40",
            device="hx1k",
        )
        assert r["success"] is True
        assert "auto-detected" in r["constraints"]
        # Verify --pcf was passed to nextpnr
        nextpnr_cmd = [c for c in captured if c[0].startswith("nextpnr-")][0]
        assert "--pcf" in nextpnr_cmd

    def test_pnr_work_dir_persistence(self, monkeypatch, scratch_dir) -> None:
        """work_dir is reused, not recreated, and returned in the response."""
        from tools.pnr import place_and_route

        captured: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            _pnr_success_mock(captured, {"modules": {"top": {}}}),
        )
        wdir = scratch_dir / "workdir"
        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="ice40",
            device="hx1k",
            work_dir=str(wdir),
        )
        assert r["work_dir"] == str(wdir)
        # The directory should still exist (persistent)
        assert wdir.exists()

    def test_pnr_vhdl_lowercases_top(self, monkeypatch, scratch_dir) -> None:
        """VHDL synth command should use lowercased top_module."""
        from tools.pnr import place_and_route

        captured_scripts: list[str] = []

        def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] == "yosys":
                # Snapshot the script NOW — tmpdir is deleted after the call
                idx = cmd.index("-s")
                captured_scripts.append(Path(cmd[idx + 1]).read_text(encoding="utf-8"))
                # Write a fake netlist so the pipeline continues
                for line in captured_scripts[-1].splitlines():
                    if "-json " in line:
                        json_path = line.split("-json ", 1)[1].strip()
                        Path(json_path).write_text(
                            json.dumps({"modules": {"mydesign": {}}})
                        )
                return _FakeProc(returncode=0)
            # nextpnr — touch output file
            for flag in ("--asc", "--textcfg", "--fasm", "--write"):
                if flag in cmd:
                    Path(cmd[cmd.index(flag) + 1]).write_bytes(b"BITSTREAM")
                    break
            return _FakeProc(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        place_and_route(
            code="entity MyDesign is end; architecture a of MyDesign is begin end;",
            top_module="MyDesign",
            target="ice40",
            device="hx1k",
            language="vhdl",
        )
        assert captured_scripts, "yosys was never invoked"
        assert "-top mydesign" in captured_scripts[0]

    def test_pnr_parse_utilization_per_target(self) -> None:
        """Each target has its own utilization pattern."""
        from tools.pnr import _parse_utilization

        ecp5_out = "LUT4:            100/ 24288\nTRELLIS_IO:       5/  197\n"
        util = _parse_utilization(ecp5_out, "ecp5")
        assert util["luts_used"] == 100
        assert util["luts_total"] == 24288
        assert util["ios_used"] == 5

        nexus_out = "OXIDE_COMB:     200/10000\nOXIDE_FF:       150/10000\n"
        util = _parse_utilization(nexus_out, "nexus")
        assert util["luts_used"] == 200
        assert util["ffs_used"] == 150

        gowin_out = "LUT:            333/ 4608\nFF:             100/ 3456\n"
        util = _parse_utilization(gowin_out, "gowin")
        assert util["luts_used"] == 333

    def test_pnr_constraint_glob_prefers_root(self, monkeypatch, scratch_dir) -> None:
        from tools.pnr import _find_constraints

        (scratch_dir / "root.pcf").write_text("x\n")
        sub = scratch_dir / "sub"
        sub.mkdir()
        (sub / "nested.pcf").write_text("y\n")
        found = _find_constraints(str(scratch_dir), "ice40")
        assert found.endswith("root.pcf")

    def test_pnr_unknown_target(self) -> None:
        from tools.pnr import place_and_route

        r = place_and_route(
            code="module top; endmodule",
            top_module="top",
            target="mars",
            device="x",
        )
        assert r["success"] is False
        assert "Unsupported PnR target" in r["error"]


# ---------------------------------------------------------------------------
# tools/workspace — fallback selection when primary root fails
# ---------------------------------------------------------------------------


class TestWorkspaceFallback:
    def test_env_override_takes_priority(self, monkeypatch, scratch_dir) -> None:
        from tools.workspace import temporary_workspace

        monkeypatch.setenv("FPGAZERO_TMPDIR", str(scratch_dir))
        with temporary_workspace("test_") as tmp:
            # The resolved tmpdir should be under scratch_dir
            assert Path(tmp).resolve().is_relative_to(scratch_dir.resolve())

    def test_falls_back_when_primary_mkdir_fails(
        self, monkeypatch, scratch_dir
    ) -> None:
        """If the FPGAZERO_TMPDIR cannot be created, fall back to cwd/no_commit."""
        from tools.workspace import temporary_workspace

        # Point FPGAZERO_TMPDIR at an unwritable location by mocking mkdir
        bad = scratch_dir / "bad_root"
        monkeypatch.setenv("FPGAZERO_TMPDIR", str(bad))
        # Chdir to scratch_dir so the cwd/no_commit fallback is a real location
        monkeypatch.chdir(scratch_dir)

        original_mkdir = Path.mkdir
        call_count = {"n": 0}

        def failing_mkdir(self, *args, **kwargs):
            # Fail the first mkdir attempt (bad_root), succeed afterwards
            if str(self).endswith("bad_root") and call_count["n"] == 0:
                call_count["n"] += 1
                raise OSError("simulated failure")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)

        with temporary_workspace("fallback_") as tmp:
            # The fallback (cwd/no_commit/fpgazero_tmp) should be used
            assert "no_commit" in tmp or "fpgazero_tmp" in tmp

    def test_raises_if_all_roots_fail(self, monkeypatch) -> None:
        """If every candidate root fails, OSError is raised."""
        from tools.workspace import temporary_workspace

        def always_fail(*_args, **_kwargs):
            raise OSError("no writable dir")

        monkeypatch.setattr(Path, "mkdir", always_fail)
        with pytest.raises(OSError):
            with temporary_workspace("nope_") as _:
                pass


# ---------------------------------------------------------------------------
# server.py dispatch with real behavior assertions
# ---------------------------------------------------------------------------


class TestServerDispatchBehavior:
    """Go beyond smoke-testing — assert correct results from each dispatch branch."""

    def _call(self, name: str, args: dict) -> dict:
        import server

        result = asyncio.run(server.handle_call_tool(name, args))
        return json.loads(result.content[0].text)

    def test_lint_hdl_dispatch(self, monkeypatch) -> None:
        import tools.lint as lint_mod

        monkeypatch.setattr(
            lint_mod,
            "lint_hdl",
            lambda **_: {"success": True, "marker": "lint_hdl"},
        )
        r = self._call("lint_hdl", {"code": "module x; endmodule"})
        assert r["marker"] == "lint_hdl"

    def test_lint_project_dispatch(self, monkeypatch) -> None:
        import tools.lint as lint_mod

        monkeypatch.setattr(
            lint_mod,
            "lint_project",
            lambda **_: {"success": True, "marker": "lint_project"},
        )
        r = self._call("lint_project", {"files": {"a.v": "module a; endmodule"}})
        assert r["marker"] == "lint_project"

    def test_synthesize_dispatch(self, monkeypatch) -> None:
        import tools.synthesize as synth

        monkeypatch.setattr(
            synth,
            "synthesize",
            lambda **_: {"success": True, "marker": "synthesize"},
        )
        r = self._call(
            "synthesize",
            {"top_module": "top", "code": "module top; endmodule"},
        )
        assert r["marker"] == "synthesize"

    def test_place_and_route_dispatch(self, monkeypatch) -> None:
        import tools.pnr as pnr

        monkeypatch.setattr(
            pnr,
            "place_and_route",
            lambda **_: {"success": True, "marker": "pnr"},
        )
        r = self._call(
            "place_and_route",
            {
                "top_module": "top",
                "target": "ice40",
                "device": "hx1k",
                "code": "module top; endmodule",
            },
        )
        assert r["marker"] == "pnr"

    def test_simulate_dispatch(self, monkeypatch) -> None:
        import tools.simulate as sim

        monkeypatch.setattr(
            sim,
            "simulate",
            lambda **_: {"success": True, "marker": "sim"},
        )
        r = self._call(
            "simulate",
            {"code": "module t; endmodule", "testbench": "module tb; endmodule"},
        )
        assert r["marker"] == "sim"

    def test_get_diagnostics_dispatch(self, monkeypatch) -> None:
        import tools.lsp as lsp

        monkeypatch.setattr(lsp, "get_diagnostics", lambda **_: {"marker": "diag"})
        r = self._call("get_diagnostics", {"code": "module t; endmodule"})
        assert r["marker"] == "diag"

    def test_format_hdl_dispatch(self, monkeypatch) -> None:
        import tools.lsp as lsp

        monkeypatch.setattr(lsp, "format_hdl", lambda **_: {"marker": "fmt"})
        r = self._call("format_hdl", {"code": "module t;endmodule"})
        assert r["marker"] == "fmt"

    def test_litex_build_dispatch(self, monkeypatch) -> None:
        import tools.litex as litex

        monkeypatch.setattr(litex, "litex_build", lambda **_: {"marker": "litex_build"})
        r = self._call("litex_build", {"board": "arty"})
        assert r["marker"] == "litex_build"

    def test_litex_soc_dispatch(self, monkeypatch) -> None:
        import tools.litex as litex

        monkeypatch.setattr(litex, "litex_soc", lambda **_: {"marker": "litex_soc"})
        r = self._call("litex_soc", {"board": "arty"})
        assert r["marker"] == "litex_soc"

    def test_litex_flow_dispatch(self, monkeypatch) -> None:
        import tools.litex as litex

        monkeypatch.setattr(litex, "litex_flow", lambda **_: {"marker": "flow"})
        r = self._call("litex_flow", {"board": "arty"})
        assert r["marker"] == "flow"

    def test_search_github_cores_dispatch(self, monkeypatch) -> None:
        import registry.github as gh

        monkeypatch.setattr(gh, "search_repos", lambda **_: [{"marker": "search"}])
        r = self._call("search_github_cores", {"query": "uart"})
        assert r[0]["marker"] == "search"

    def test_start_build_dispatch(self, monkeypatch) -> None:
        import server

        monkeypatch.setattr(server._builds(), "start", lambda **_: {"marker": "start"})
        r = self._call("start_build", {"cmd": ["yosys", "-V"]})
        assert r["marker"] == "start"

    def test_build_status_dispatch(self, monkeypatch) -> None:
        import server

        monkeypatch.setattr(
            server._builds(),
            "status",
            lambda **_: {"marker": "status"},
        )
        r = self._call("build_status", {"build_id": "abc"})
        assert r["marker"] == "status"

    def test_list_builds_dispatch(self, monkeypatch) -> None:
        import server

        monkeypatch.setattr(
            server._builds(), "list_builds", lambda: [{"marker": "list"}]
        )
        r = self._call("list_builds", {})
        assert r[0]["marker"] == "list"

    def test_cancel_build_dispatch(self, monkeypatch) -> None:
        import server

        monkeypatch.setattr(
            server._builds(),
            "cancel",
            lambda **_: {"marker": "cancel"},
        )
        r = self._call("cancel_build", {"build_id": "abc"})
        assert r["marker"] == "cancel"

    def test_reload_registry_dispatch(self, monkeypatch) -> None:
        import server

        monkeypatch.setattr(server._registry(), "reload", lambda: {"marker": "reload"})
        r = self._call("reload_registry", {})
        assert r["marker"] == "reload"

    def test_unhandled_exception_logged_and_reported(self, monkeypatch) -> None:
        """Force an unexpected exception inside a handler and verify the error path."""
        import tools.lint as lint_mod

        def boom(**_):
            raise RuntimeError("boom")

        monkeypatch.setattr(lint_mod, "lint_hdl", boom)
        r = self._call("lint_hdl", {"code": "module t; endmodule"})
        assert "error" in r
        assert "boom" in r["error"]
