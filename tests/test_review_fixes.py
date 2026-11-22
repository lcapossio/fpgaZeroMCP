"""Regressions for defects found in the pre-merge review of the v0.8 branch.

- Vivado results lacked "routed", so close_timing ranked every Vivado
  attempt as unrouted and reported the sweep as failed.
- close_timing returned the bitstream path of the best attempt, but every
  attempt writes the same file in the shared work_dir, so a later, worse
  attempt had overwritten it.
- The netlist-reuse fingerprint ignored `include headers.
- Source file names reached the generated Vivado Tcl inside {...} without
  escaping; a '}' in a name ended the quote and the rest ran as Tcl.
- Vivado's inline WNS fallback used [\\d.]+ (crashes on "WNS=6.361.") and
  took the first, pre-route estimate.
- JSON-RPC batch members could not be cancelled; a non-scalar request id
  crashed the read loop.
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
from pathlib import Path

import pytest


@pytest.fixture
def scratch_dir():
    """pytest.ini disables tmp_path (-p no:tmpdir)."""
    path = tempfile.mkdtemp(prefix="pytest_review_")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _completed(cmd, stdout="", returncode=0):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")


ROUTED_LOG = """\
****** Vivado v2025.2 (64-bit)
Command: synth_design -top top -part xc7a35tcpg236-1
synth_design completed successfully
Command: route_design
INFO: [Route 35-57] Estimated Timing Summary | WNS=1.250  | TNS=0.000  |
route_design completed successfully
"""


def _fake_vivado(monkeypatch, stdout: str, returncode: int = 0, write_bit=True):
    import fpgazero_mcp.tools.vivado as vv

    monkeypatch.setattr(vv.shutil, "which", lambda _cmd: "vivado")
    scripts: list[str] = []

    def fake_run(cmd, **kwargs):
        tcl_file = cmd[-1]
        scripts.append(Path(tcl_file).read_text(encoding="utf-8"))
        if write_bit:
            with open(os.path.join(os.path.dirname(tcl_file), "out.bit"), "wb") as f:
                f.write(b"bit")
        return _completed(cmd, stdout=stdout, returncode=returncode)

    monkeypatch.setattr(vv.proctree, "run_tree", fake_run)
    return scripts


# --- Vivado "routed" --------------------------------------------------------


class TestVivadoRouted:
    def _run(self):
        from fpgazero_mcp.tools.vivado import vivado_place_and_route

        return vivado_place_and_route(
            code="module top; endmodule", top_module="top", part="xc7a35tcpg236-1"
        )

    def test_success_is_routed(self, monkeypatch, scratch_dir) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        _fake_vivado(monkeypatch, ROUTED_LOG)
        r = self._run()
        assert r["success"] is True and r["routed"] is True

    def test_bitstream_failure_after_routing_is_still_routed(self, monkeypatch) -> None:
        log = ROUTED_LOG + "ERROR: [DRC NSTD-1] Unspecified I/O Standard\n"
        _fake_vivado(monkeypatch, log, returncode=1, write_bit=False)
        r = self._run()
        assert r["success"] is False and r["routed"] is True

    def test_failure_before_routing_is_not_routed(self, monkeypatch) -> None:
        _fake_vivado(monkeypatch, "ERROR: [Synth 8-439] module not found\n", 1, False)
        r = self._run()
        assert r["success"] is False and r["routed"] is False

    def test_close_timing_vivado_sweep_succeeds(self, monkeypatch, scratch_dir) -> None:
        """End to end through the real Vivado wrapper, not a canned result."""
        from fpgazero_mcp.tools.timing import close_timing

        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        _fake_vivado(monkeypatch, ROUTED_LOG)
        out = close_timing(
            code="module top; endmodule",
            top_module="top",
            target="xilinx",
            device="xc7a35tcpg236-1",
            backend="vivado",
            max_attempts=2,
        )
        assert out["success"] is True, out.get("error")
        assert [a["routed"] for a in out["attempts"]] == [True, True]
        assert os.path.exists(out["bitstream_path"])


# --- close_timing keeps the winner's bitstream -------------------------------


def test_close_timing_returns_the_best_attempts_bitstream(
    monkeypatch, scratch_dir
) -> None:
    from fpgazero_mcp.tools.timing import close_timing

    fmax_by_attempt = [50.0, 60.0, 40.0]  # best is attempt 2, not the last
    calls: list[int] = []

    def fake_pnr(**kwargs):
        calls.append(1)
        n = len(calls)
        # Like the real backend: every attempt writes the same file.
        out_file = Path(kwargs["work_dir"]) / "out.bin"
        out_file.write_text(f"attempt{n}", encoding="utf-8")
        return {
            "success": True,
            "routed": True,
            "timing": {"max_freq_mhz": fmax_by_attempt[n - 1]},
            "utilization": {},
            "bitstream_path": str(out_file),
            "bitstream_ext": ".bin",
        }

    monkeypatch.setattr("fpgazero_mcp.tools.pnr.place_and_route", fake_pnr)
    monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", str(scratch_dir))
    out = close_timing(
        code="module top; endmodule",
        top_module="top",
        target="ice40",
        device="up5k",
        package="sg48",
        seeds=[1, 2, 3],
        work_dir=str(scratch_dir),
    )
    assert out["best_attempt"] == 2
    assert Path(out["bitstream_path"]).read_text(encoding="utf-8") == "attempt2"


# --- netlist fingerprint covers headers --------------------------------------


def test_reuse_refuses_stale_netlist_after_header_edit(
    monkeypatch, scratch_dir
) -> None:
    from fpgazero_mcp.tools import pnr as pnr_mod

    proj = scratch_dir / "proj"
    work = scratch_dir / "work"
    proj.mkdir()
    (proj / "top.v").write_text(
        '`include "defs.vh"\nmodule top(input clk); endmodule\n', encoding="utf-8"
    )
    (proj / "defs.vh").write_text("`define WIDTH 8\n", encoding="utf-8")

    yosys_runs: list[int] = []

    class _P:
        returncode = 0
        stdout = "Info: Max frequency for clock 'clk': 100.00 MHz (PASS at 12.00 MHz)\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        if cmd[0] == "yosys":
            yosys_runs.append(1)
            (work / "netlist.json").write_text("{}", encoding="utf-8")
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", str(scratch_dir))

    def run(**kw):
        return pnr_mod.place_and_route(
            project_dir=str(proj),
            top_module="top",
            target="ice40",
            device="up5k",
            package="sg48",
            work_dir=str(work),
            **kw,
        )

    run()
    assert run(reuse_netlist=True)["netlist_reused"] is True  # unchanged: reuse
    (proj / "defs.vh").write_text("`define WIDTH 16\n", encoding="utf-8")
    r = run(reuse_netlist=True)
    assert r["netlist_reused"] is False
    assert len(yosys_runs) == 2


# --- Tcl quoting -------------------------------------------------------------


class TestTclQuoting:
    def test_plain_path_keeps_braces(self) -> None:
        from fpgazero_mcp.tools.vivado import _tcl_path

        assert _tcl_path("C:\\work dir\\a.v") == "{C:/work dir/a.v}"

    def test_brace_in_path_is_escaped_not_brace_quoted(self) -> None:
        from fpgazero_mcp.tools.vivado import _tcl_path

        q = _tcl_path("/w/a.v}; set injected 1; #.v")
        assert q == "/w/a.v\\}\\;\\ set\\ injected\\ 1\\;\\ #.v"

    def test_control_characters_refused(self) -> None:
        from fpgazero_mcp.tools.vivado import _tcl_path

        with pytest.raises(ValueError):
            _tcl_path("/w/a\n.v")

    def test_dollar_in_top_module_is_quoted(self) -> None:
        from fpgazero_mcp.tools.vivado import _generate_tcl

        tcl = _generate_tcl(["/w/a.v"], "verilog", "a$b", "xc7a35t", None, "/w/o.bit")
        assert "synth_design -top {a$b} -part xc7a35t" in tcl

    @pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not on PATH")
    @pytest.mark.parametrize(
        "path",
        [
            "/w/a.v}; set injected 1; #.v",
            "/w/a.v} [set injected 1] {",
            "/w/$injected[set injected 1].v",
            "/w/odd {name} with spaces.v",
            "/w/back\\slash.v",
        ],
    )
    def test_tcl_reads_path_literally(self, path: str) -> None:
        """Run the generated line through a real Tcl interpreter."""
        from fpgazero_mcp.tools.vivado import _tcl_path

        script = (
            "set injected 0\n"
            "proc read_verilog {args} { puts [list ARGS {*}$args] }\n"
            f"read_verilog {_tcl_path(path)}\n"
            'puts "INJECTED=$injected"\n'
        )
        r = subprocess.run(
            ["tclsh"], input=script, capture_output=True, text=True, timeout=10
        )
        assert "INJECTED=0" in r.stdout, r.stdout + r.stderr
        expected = path.replace("\\", "/")
        args_line = next(x for x in r.stdout.splitlines() if x.startswith("ARGS"))
        parsed = subprocess.run(
            ["tclsh"],
            input=f"puts [lindex {{{args_line}}} 1]\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        # One argument, exactly the path.
        assert args_line.count("ARGS") == 1
        assert parsed.stdout.rstrip("\n") == expected


@pytest.mark.parametrize(
    "name", ["a;b.v", "a}.v", "a{.v", "a[b].v", "a$b.v", 'a".v', "a`.v", "a\nb.v"]
)
def test_validate_filename_rejects_script_metacharacters(name: str) -> None:
    from fpgazero_mcp.tools.sources import validate_filename

    assert validate_filename(name) is not None


@pytest.mark.parametrize("name", ["top.v", "rtl/core.sv", "my file.v", "a-b_c.v"])
def test_validate_filename_accepts_ordinary_names(name: str) -> None:
    from fpgazero_mcp.tools.sources import validate_filename

    assert validate_filename(name) is None


# --- Vivado inline WNS fallback ----------------------------------------------


def test_inline_wns_fallback_takes_last_value_and_ignores_trailing_period() -> None:
    from fpgazero_mcp.tools.vivado import _parse_timing

    log = (
        "INFO: [Place 30-746] Post Placement Timing Summary WNS=6.361. For the\n"
        "INFO: [Route 35-57] Estimated Timing Summary | WNS=6.667  | TNS=0.000  |\n"
    )
    t = _parse_timing(log, 10.0)
    assert t["wns_ns"] == 6.667
    assert t["tns_ns"] == 0.0
    assert t["max_freq_mhz"] == pytest.approx(1000 / (10 - 6.667), abs=0.01)


def test_inline_wns_fallback_survives_trailing_period_only() -> None:
    from fpgazero_mcp.tools.vivado import _parse_timing

    assert _parse_timing("Timing Summary WNS=6.361. done\n", None)["wns_ns"] == 6.361


# --- mcp_lite -----------------------------------------------------------------


def test_batch_member_can_be_cancelled(monkeypatch) -> None:
    from fpgazero_mcp.mcp_lite import CallToolResult, Server, TextContent

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    srv = Server("t")

    async def slow_call(name, args):
        await asyncio.sleep(30)
        return CallToolResult(content=[TextContent(text="late")])

    srv._call_handler = slow_call

    async def scenario() -> None:
        batch = [
            {"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {}},
            {"jsonrpc": "2.0", "id": 21, "method": "ping"},
        ]
        task = asyncio.create_task(srv._handle_batch(batch))
        for _ in range(100):
            await asyncio.sleep(0)
            if 20 in srv._inflight:
                break
        assert 20 in srv._inflight
        srv._cancel_request(20)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    # The cancelled member gets no entry; its sibling still answers.
    assert replies == [[{"jsonrpc": "2.0", "id": 21, "result": {}}]]
    assert srv._inflight == {}


def test_non_scalar_request_id_does_not_kill_read_loop(monkeypatch) -> None:
    from fpgazero_mcp.mcp_lite import Server

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": [1], "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    asyncio.run(Server("t").run())
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert {"jsonrpc": "2.0", "id": 2, "result": {}} in replies
    assert {"jsonrpc": "2.0", "id": [1], "result": {}} in replies


# --- process-tree termination (real processes, no mocks) ---------------------

# The child starts a grandchild that inherits its stdout (holding the pipe
# open, like vivado.exe under vivado.bat or a solver under sby) and writes a
# marker file if it lives long enough.
_SPAWNER = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
time.sleep(60)
"""
_GRANDCHILD = """
import sys, time
{prelude}
time.sleep(3)
open(sys.argv[1], "w").write("survived")
"""


def _spawn_cmd(marker: Path, prelude: str = "") -> list[str]:
    return [
        sys.executable,
        "-c",
        _SPAWNER,
        _GRANDCHILD.format(prelude=prelude),
        str(marker),
    ]


def test_run_tree_timeout_kills_grandchildren_and_returns_promptly(
    scratch_dir,
) -> None:
    import time

    from fpgazero_mcp.tools.proctree import run_tree

    marker = scratch_dir / "marker"
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_tree(_spawn_cmd(marker), timeout=1)
    # subprocess.run would block here until the grandchild released the pipe.
    assert time.monotonic() - t0 < 3
    time.sleep(3.5)
    assert not marker.exists(), "grandchild outlived the timeout"


def test_run_tree_returns_output() -> None:
    from fpgazero_mcp.tools.proctree import run_tree

    r = run_tree([sys.executable, "-c", "print('hi')"], timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "hi"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_terminate_tree_kills_descendant_that_ignores_sigterm(scratch_dir) -> None:
    import time

    from fpgazero_mcp.tools.proctree import group_popen_kwargs, terminate_tree

    marker = scratch_dir / "marker"
    prelude = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)"
    proc = subprocess.Popen(
        _spawn_cmd(marker, prelude),
        stdout=subprocess.DEVNULL,
        **group_popen_kwargs(),
    )
    time.sleep(0.5)  # let the grandchild start and install its handler
    terminate_tree(proc)  # the leader dies on SIGTERM; the grandchild doesn't
    time.sleep(3.5)
    assert not marker.exists(), "SIGTERM-ignoring descendant survived cancel"


# --- formal_verify --------------------------------------------------------------

_SBY_FAIL = """\
SBY 13:37:02 [design] engine_0: ##   Assert failed in top: design.v:3.3-3.20
SBY 13:37:02 [design] engine_0: ##   Writing trace to VCD file: engine_0/trace.vcd
SBY 13:37:02 [design] summary: engine_0 (smtbmc) returned FAIL
SBY 13:37:02 [design] summary: counterexample trace: design/engine_0/trace.vcd
SBY 13:37:02 [design] DONE (FAIL, rc=2)
"""
_SBY_PASS = "SBY 13:37:02 [design] DONE (PASS, rc=0)\n"
_FORMAL_DESIGN = "module top(input clk); endmodule\n"


def _fake_sby(monkeypatch, stdout: str, captured: dict, write_trace=False):
    from fpgazero_mcp.tools import proctree

    def fake_run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        captured["sby"] = (cwd / "design.sby").read_text(encoding="utf-8")
        if write_trace:
            trace = cwd / "design" / "engine_0" / "trace.vcd"
            trace.parent.mkdir(parents=True)
            trace.write_text("$timescale 1ns $end\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(proctree, "run_tree", fake_run)


class TestFormalFixes:
    def test_live_mode_omits_depth_and_is_unbounded(self, monkeypatch) -> None:
        from fpgazero_mcp.tools.formal import formal_verify

        cap: dict = {}
        _fake_sby(monkeypatch, _SBY_PASS, cap)
        r = formal_verify(code=_FORMAL_DESIGN, mode="live", engine="aiger-suprove")
        assert "mode live" in cap["sby"]
        assert "depth" not in cap["sby"]
        assert "bounded" not in r and r["proved"] is True

    def test_live_mode_rejects_engines_that_cannot_do_it(self) -> None:
        from fpgazero_mcp.tools.formal import formal_verify

        r = formal_verify(code=_FORMAL_DESIGN, mode="live", engine="smtbmc")
        assert r["error_code"] == "invalid_input"

    def test_counterexample_trace_survives_workspace_cleanup(
        self, monkeypatch, scratch_dir
    ) -> None:
        from fpgazero_mcp.tools.formal import formal_verify

        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        _fake_sby(monkeypatch, _SBY_FAIL, {}, write_trace=True)
        r = formal_verify(code=_FORMAL_DESIGN, mode="bmc")
        assert r["status"] == "FAIL"
        trace = Path(r["counterexample_trace"])
        assert trace.is_absolute() and trace.is_file()
        assert trace.is_relative_to(scratch_dir)
        assert r["traces"] == [str(trace)]

    def test_project_headers_reachable_from_sby_workdir(
        self, monkeypatch, scratch_dir
    ) -> None:
        from fpgazero_mcp.tools.formal import formal_verify

        proj = scratch_dir / "proj"
        (proj / "inc").mkdir(parents=True)
        (proj / "top.v").write_text(
            '`include "defs.vh"\nmodule top(input clk); endmodule\n',
            encoding="utf-8",
        )
        (proj / "defs.vh").write_text("`define W 8\n", encoding="utf-8")
        cap: dict = {}
        _fake_sby(monkeypatch, _SBY_PASS, cap)
        formal_verify(project_dir=str(proj), include_dirs=["inc"])
        read_line = next(x for x in cap["sby"].splitlines() if x.startswith("read "))
        flags = [t[2:] for t in read_line.split() if t.startswith("-I")]
        assert Path(flags[0]) == (proj / "inc").resolve()  # relative to project
        assert Path(proj).resolve() in {Path(f) for f in flags}  # header's dir
        assert all(Path(f).is_absolute() for f in flags)


# --- build_status catch-up and oversized lines --------------------------------


def _build_record(scratch_dir: Path):
    from fpgazero_mcp.tools.build_manager import BuildRecord

    log = scratch_dir / "b.log"
    log.write_bytes(b"")
    rec = BuildRecord(
        build_id="x", label="", cmd=[], log_path=str(log), work_dir=".", start_time=0.0
    )
    return rec, log


def test_catchup_just_over_limit_does_not_reparse_head(
    monkeypatch, scratch_dir
) -> None:
    from fpgazero_mcp.tools.build_manager import BuildRecord

    # Scaled down: 1000-byte limit, 100-byte head.
    monkeypatch.setattr(BuildRecord, "_PARSE_MAX_CATCHUP_BYTES", 1000)
    monkeypatch.setattr(BuildRecord, "_PARSE_HEAD_BYTES", 100)
    rec, log = _build_record(scratch_dir)
    # The head (first 100 bytes) holds a short line and then a warning. The
    # log is 5 bytes over the limit, so size - limit lands inside the short
    # line, before the already-parsed warning.
    text = "a" * 9 + "\n" + "WARNING: [Synth 8-1] once\n"
    while len(text) < 1005 - 50:
        text += "x" * 49 + "\n"
    text += "z" * (1005 - len(text) - 1) + "\n"
    log.write_bytes(text.encode())
    assert log.stat().st_size == 1005
    info = rec.build_info()
    assert info["warnings"] == 1
    assert info.get("log_bytes_skipped", 0) >= 0


def test_catchup_resume_on_a_line_start_keeps_that_line(
    monkeypatch, scratch_dir
) -> None:
    from fpgazero_mcp.tools.build_manager import BuildRecord

    monkeypatch.setattr(BuildRecord, "_PARSE_MAX_CATCHUP_BYTES", 500)
    monkeypatch.setattr(BuildRecord, "_PARSE_HEAD_BYTES", 50)
    rec, log = _build_record(scratch_dir)
    tail = "WARNING: [Synth 8-2] at the resume point\n"
    body = "y" * 99 + "\n"
    # Make size - limit land exactly on the first byte of `tail` + filler.
    tail_block = tail + body * ((500 - len(tail)) // 100)
    pad = 500 - len(tail_block)
    tail_block += "z" * (pad - 1) + "\n" if pad > 0 else ""
    log.write_bytes((body * 20 + tail_block).encode())
    assert log.stat().st_size - 500 == 20 * 100  # resume = start of `tail`
    assert rec.build_info()["warnings"] == 1


def test_oversized_line_remainder_is_not_parsed_as_new_lines(
    monkeypatch, scratch_dir
) -> None:
    from fpgazero_mcp.tools.build_manager import BuildRecord

    monkeypatch.setattr(BuildRecord, "_PARSE_CHUNK_BYTES", 64)
    rec, log = _build_record(scratch_dir)
    # A 200-byte line whose later part would look like an error line if it
    # were parsed on its own after a chunk boundary.
    long_line = "a" * 64 + "ERROR: not really" + "b" * 119 + "\n"
    log.write_bytes((long_line + "WARNING: [X 1-1] real\n").encode())
    info = rec.build_info()
    assert info["errors"] == 0
    assert info["warnings"] == 1


# --- close_timing: closure needs a routed attempt; budget; data dir ---------


def test_unrouted_attempt_that_logged_a_passing_fmax_does_not_close(
    monkeypatch,
) -> None:
    from fpgazero_mcp.tools.timing import close_timing

    results = [
        {"success": False, "routed": False, "timing": {"max_freq_mhz": 90.0}},
        {"success": True, "routed": True, "timing": {"max_freq_mhz": 60.0}},
    ]
    calls: list[int] = []

    def fake_pnr(**kwargs):
        calls.append(1)
        r = dict(results[len(calls) - 1])
        r["timing"] = {**r["timing"], "meets_target": True}
        return r

    monkeypatch.setattr("fpgazero_mcp.tools.pnr.place_and_route", fake_pnr)
    out = close_timing(
        code="module top; endmodule",
        top_module="top",
        target="ice40",
        device="up5k",
        target_mhz=50.0,
        seeds=[1, 2],
    )
    assert len(calls) == 2, "the unrouted attempt must not stop the sweep"
    assert out["closed"] is True and out["best_attempt"] == 2


def test_budget_is_checked_before_building_a_huge_plan() -> None:
    import time

    from fpgazero_mcp.tools.timing import close_timing

    t0 = time.monotonic()
    out = close_timing(
        code="module top; endmodule",
        top_module="top",
        target="ice40",
        device="up5k",
        max_attempts=100_000_000,
        timeout=300,
    )
    assert out["error_code"] == "invalid_input"
    assert time.monotonic() - t0 < 1


def test_data_root_is_an_allowed_root(monkeypatch, scratch_dir) -> None:
    """close_timing passes its own sweep dir (under the data root) as work_dir;
    that must pass validation even when the data root is outside cwd/home."""
    from fpgazero_mcp.tools.sources import validate_path_in_roots

    data = scratch_dir / "data"
    monkeypatch.setenv("FPGAZERO_DATA_DIR", str(data))
    monkeypatch.setenv("FPGAZERO_ALLOWED_DIRS", "")
    cwd = scratch_dir / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(os.devnull)))
    assert validate_path_in_roots(str(data / "timing" / "abc"), "work_dir") is None
    assert validate_path_in_roots(str(scratch_dir / "other"), "work_dir")


# --- mcp_lite: malformed members don't sink a batch or the loop -------------


def test_malformed_cancel_in_batch_keeps_sibling_replies(monkeypatch) -> None:
    from fpgazero_mcp.mcp_lite import Server

    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": [1]},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": [1]},
        },
        {},
    ]
    lines = [
        json.dumps(batch),
        json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": 5}
        ),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    asyncio.run(Server("t").run())
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    batch_reply = next(r for r in replies if isinstance(r, list))
    assert {"jsonrpc": "2.0", "id": 1, "result": {}} in batch_reply
    # {} has no method: an invalid request, answered rather than ignored.
    assert any(r.get("error", {}).get("code") == -32600 for r in batch_reply)
    assert {"jsonrpc": "2.0", "id": 2, "result": {}} in replies
