# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Formal property verification via SymbiYosys (sby).

sby was already on the build allowlist and in the healthcheck, but there was no
way to reach it except through raw `start_build`. This exposes it as a first-class
tool: it writes the .sby task file, runs it, and returns a structured verdict
(PASS / FAIL / UNKNOWN / ERROR) plus the failing assertion and counterexample
trace, so a client does not have to parse sby's console output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fpgazero_mcp.paths import data_root
from fpgazero_mcp.tools.errors import (
    err,
    FORMAL_FAILED,
    INTERNAL_ERROR,
    INVALID_INPUT,
    TIMEOUT,
    TOOL_NOT_FOUND,
)
from fpgazero_mcp.tools import proctree
from fpgazero_mcp.tools.progress import make_reporter
from fpgazero_mcp.tools.sources import (
    resolve_sources,
    validate_top_module,
)
from fpgazero_mcp.tools.textutil import truncate_log
from fpgazero_mcp.tools.workspace import temporary_workspace

# sby modes. `bmc` bounded-checks assertions, `prove` attempts unbounded proof by
# k-induction, `cover` looks for a trace reaching each cover() statement, and
# `live` checks liveness properties.
MODES = ("bmc", "prove", "cover", "live")

# Engine strings are passed through to the [engines] section verbatim, so they
# are restricted to a known-good set rather than accepting arbitrary text that
# lands in a generated file.
ENGINES = {
    "smtbmc": "smtbmc",
    "smtbmc-yices": "smtbmc yices",
    "smtbmc-z3": "smtbmc z3",
    "smtbmc-boolector": "smtbmc boolector",
    "abc-pdr": "abc pdr",
    "aiger-suprove": "aiger suprove",
}

# Modes that can only ever give a bounded answer: a passing bmc/cover run says
# "no counterexample within depth", not "the property holds". `live` is an
# unbounded liveness check and `prove` an unbounded safety proof.
_BOUNDED_MODES = ("bmc", "cover")

# sby's live mode is only implemented by the aiger suprove engine.
_LIVE_ENGINES = ("aiger-suprove",)

# "SBY 12:00:00 [design] DONE (PASS, rc=0)"
_DONE_RE = re.compile(r"DONE \((PASS|FAIL|UNKNOWN|ERROR)[,)]")
# "SBY 12:00:00 [design] engine_0: ##   Assert failed in top: top.sv:12.3-14.6 ($assert$..)"
_ASSERT_RE = re.compile(r"Assert failed in ([^:]+): (.+?)\s*$", re.MULTILINE)
# "SBY 12:00:00 [design] engine_0: ##   Writing trace to VCD file: engine_0/trace.vcd"
_TRACE_RE = re.compile(r"[Ww]riting trace to \w+ file: (\S+)")
# "SBY 12:00:00 [design] summary: engine_0 (smtbmc yices) returned FAIL"
_ENGINE_RE = re.compile(r"summary: engine_\d+ \(([^)]+)\) returned (\w+)")
# "SBY 12:00:00 [design] summary: counterexample trace: design/engine_0/trace.vcd"
_SUMMARY_TRACE_RE = re.compile(r"summary: (?:counterexample trace|trace): (\S+)")
# "ERROR: syntax error, unexpected ..." from the yosys frontend inside sby
_YOSYS_ERROR_RE = re.compile(r"^ERROR: (.+?)\s*$", re.MULTILINE)


def _sby_script(
    src_paths: list[str],
    language: str,
    top_module: str,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
) -> str:
    """Build the [script] section body.

    This is deliberately not `yosys_read_cmds`: formal verification needs
    `read -formal`, which keeps assert/assume/cover cells and $anyconst/$anyseq
    rather than discarding them the way the synthesis frontend does.
    """
    names = [os.path.basename(p) for p in src_paths]
    if language == "vhdl":
        # ghdl-yosys-plugin; entity names are lowercased on import.
        files = " ".join(names)
        return f"ghdl --std=08 {files} -e {top_module.lower()}\nprep -top {top_module.lower()}\n"

    flags = ["-formal"]
    if language == "systemverilog":
        flags.append("-sv")
    for d in include_dirs or []:
        flags.append(f"-I{d.replace(chr(92), '/')}")
    for d in defines or []:
        flags.append(f"-D{d}")
    flag_str = " ".join(flags)

    lines = [f"read {flag_str} {n}" for n in names]
    lines.append(f"prep -top {top_module}")
    return "\n".join(lines) + "\n"


def _sby_config(
    script: str,
    src_names: list[str],
    mode: str,
    depth: int,
    engine: str,
    multiclock: bool,
) -> str:
    """Assemble a complete .sby task file."""
    options = [f"mode {mode}"]
    # `depth` bounds bmc/cover and the base case of prove's induction. live
    # mode has no depth, and sby rejects options a task doesn't use.
    if mode != "live":
        options.append(f"depth {depth}")
    if multiclock:
        options.append("multiclock on")

    return (
        "[options]\n"
        + "\n".join(options)
        + "\n\n[engines]\n"
        + engine
        + "\n\n[script]\n"
        + script
        + "\n[files]\n"
        + "\n".join(src_names)
        + "\n"
    )


def _parse_sby_log(stdout: str, returncode: int) -> dict:
    """Extract a structured verdict from sby console output."""
    status_match = _DONE_RE.search(stdout)
    if status_match:
        status = status_match.group(1)
    elif returncode == 0:
        status = "PASS"
    else:
        # sby exits 1 on a task error before it ever prints a DONE line.
        status = "ERROR"

    engines = [
        {"engine": m.group(1), "result": m.group(2)}
        for m in _ENGINE_RE.finditer(stdout)
    ]

    assertions = [
        {"module": m.group(1), "location": m.group(2).strip()}
        for m in _ASSERT_RE.finditer(stdout)
    ]

    trace = None
    summary_trace = _SUMMARY_TRACE_RE.search(stdout)
    if summary_trace:
        trace = summary_trace.group(1)
    else:
        written = _TRACE_RE.search(stdout)
        if written:
            trace = written.group(1)

    result: dict = {
        "status": status,
        "engines": engines,
        "failed_assertions": assertions,
    }
    if trace:
        result["counterexample_trace"] = trace
    if status == "ERROR":
        errors = [m.group(1) for m in _YOSYS_ERROR_RE.finditer(stdout)]
        if errors:
            result["errors"] = errors[:10]
    return result


def _keep_traces(tmpdir: str, parsed: dict) -> None:
    """Copy sby's trace files out of the temporary workspace.

    The workspace is deleted when formal_verify returns, so a trace path
    pointing into it would be dangling by the time the caller reads it.
    Traces go under the data root; parsed["counterexample_trace"] is rewritten
    to the kept copy and parsed["traces"] lists all of them.
    """
    workdir = Path(tmpdir) / "design"  # sby names it after design.sby
    found = sorted(
        p for ext in ("*.vcd", "*.yw") for p in workdir.glob(f"engine_*/**/{ext}")
    )
    if not found:
        return
    keep = data_root() / "formal" / uuid4().hex[:12]
    kept: dict[str, str] = {}
    for src in found:
        rel = src.relative_to(workdir).as_posix()
        dst = keep / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError:
            continue
        kept[rel] = str(dst)
    if not kept:
        return
    parsed["traces"] = [v for k, v in kept.items() if k.endswith(".vcd")] or list(
        kept.values()
    )
    trace = parsed.get("counterexample_trace")
    if trace:
        # sby reports it relative to tmpdir ("design/engine_0/trace.vcd") or
        # to its workdir ("engine_0/trace.vcd").
        rel = trace.replace("\\", "/").removeprefix("design/")
        if rel in kept:
            parsed["counterexample_trace"] = kept[rel]


def formal_verify(
    code: str = "",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    top_module: str = "top",
    language: str = "verilog",
    mode: str = "bmc",
    depth: int = 20,
    engine: str = "smtbmc",
    multiclock: bool = False,
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
    timeout: int = 300,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Run SymbiYosys formal verification on a design containing assertions.

    Design source input (provide exactly one): code, files, or project_dir.

    mode:   "bmc" (bounded model check, default), "prove" (unbounded, k-induction),
            "cover" (find a trace reaching each cover statement), or "live".
    depth:  number of cycles to explore for bounded modes.
    engine: a key of ENGINES, e.g. "smtbmc-yices" or "abc-pdr".

    Returns a dict whose "status" is PASS, FAIL, UNKNOWN or ERROR, with the
    failing assertions and the counterexample trace path when status is FAIL.
    Note that a PASS in a bounded mode only means no counterexample exists
    within `depth` cycles; "proved" is reported only for mode="prove".
    """
    report = make_reporter(progress)

    if mode not in MODES:
        return err(
            INVALID_INPUT,
            f"Invalid mode '{mode}'. Must be one of: {', '.join(MODES)}.",
        )
    if engine not in ENGINES:
        return err(
            INVALID_INPUT,
            f"Invalid engine '{engine}'. Must be one of: {', '.join(sorted(ENGINES))}.",
        )
    if mode == "live" and engine not in _LIVE_ENGINES:
        return err(
            INVALID_INPUT,
            f"mode='live' requires engine {' or '.join(_LIVE_ENGINES)} "
            f"(got '{engine}').",
        )
    if not isinstance(depth, int) or depth < 1 or depth > 10000:
        return err(INVALID_INPUT, "depth must be an integer between 1 and 10000.")

    top_err = validate_top_module(top_module)
    if top_err:
        return err(INVALID_INPUT, top_err)

    report(0.05, "Resolving sources")

    with temporary_workspace("formal_") as tmpdir:
        src_paths, src_err = resolve_sources(code, files, project_dir, language, tmpdir)
        if src_err:
            return err(INVALID_INPUT, src_err)

        # sby copies [files] entries into its own workdir, so every source has to
        # be reachable by basename from the task file's directory. project_dir
        # sources live elsewhere, so stage a flat copy.
        staged: list[str] = []
        seen: set[str] = set()
        for path in src_paths:
            name = os.path.basename(path)
            if name in seen:
                return err(
                    INVALID_INPUT,
                    f"Duplicate source basename '{name}'. sby stages sources by "
                    "basename, so file names must be unique across the design.",
                )
            seen.add(name)
            dest = os.path.join(tmpdir, name)
            if os.path.abspath(dest) != os.path.abspath(path):
                try:
                    Path(dest).write_bytes(Path(path).read_bytes())
                except OSError as exc:
                    return err(INTERNAL_ERROR, f"Could not stage '{path}': {exc}")
            staged.append(name)

        # sby runs yosys on its own copies of the sources, in its workdir, so
        # `include lookups must use absolute directories: the ones given
        # (relative to project_dir), and for a project the root and each
        # source's directory, where its headers sit. The script line is
        # space-separated, so directories added automatically are skipped
        # when they contain whitespace rather than breaking the read.
        base = project_dir or os.getcwd()
        inc_dirs = [
            os.path.abspath(d if os.path.isabs(d) else os.path.join(base, d))
            for d in include_dirs or []
        ]
        if project_dir:
            auto = [project_dir, *(os.path.dirname(p) for p in src_paths)]
            inc_dirs += [
                a
                for a in (os.path.abspath(d) for d in auto)
                if not any(c.isspace() for c in a)
            ]
        inc_dirs = list(dict.fromkeys(inc_dirs))

        script = _sby_script(src_paths, language, top_module, inc_dirs, defines)
        config = _sby_config(script, staged, mode, depth, ENGINES[engine], multiclock)
        sby_file = os.path.join(tmpdir, "design.sby")
        Path(sby_file).write_text(config, encoding="utf-8")

        report(0.2, f"Running sby ({mode}, depth {depth})")

        try:
            # run_tree, not subprocess.run: sby starts solvers in their own
            # process groups, which a plain timeout would leave running.
            proc = proctree.run_tree(
                # -f overwrites the workdir from any previous run in this tmpdir.
                ["sby", "-f", "design.sby"],
                timeout=timeout,
                cwd=tmpdir,
            )
        except FileNotFoundError:
            return err(
                TOOL_NOT_FOUND,
                "'sby' (SymbiYosys) not found. Install OSS CAD Suite, which bundles it.",
            )
        except subprocess.TimeoutExpired:
            return err(
                TIMEOUT,
                f"Formal verification timed out after {timeout} s. "
                "Reduce depth, or try engine='abc-pdr' for mode='prove'.",
            )

        report(0.9, "Parsing results")

        combined = proc.stdout + proc.stderr
        log_text, log_trunc = truncate_log(combined)
        parsed = _parse_sby_log(combined, proc.returncode)
        status = parsed["status"]

        _keep_traces(tmpdir, parsed)

        result: dict = {
            "success": status == "PASS",
            "tool": "sby",
            "mode": mode,
            "depth": depth if mode != "live" else None,
            "engine": ENGINES[engine],
            "top_module": top_module,
            "language": language,
            "returncode": proc.returncode,
            "log": log_text,
            "log_truncated": log_trunc,
            **parsed,
        }

        if status == "PASS":
            if mode == "prove":
                result["proved"] = True
                result["summary"] = (
                    f"Proved: no reachable state violates the assertions in '{top_module}'."
                )
            elif mode == "cover":
                result["summary"] = (
                    f"All cover statements were reached within {depth} cycles."
                )
            elif mode == "live":
                result["proved"] = True
                result["summary"] = (
                    f"Proved: the liveness properties in '{top_module}' hold."
                )
            else:
                result["proved"] = False
                result["summary"] = (
                    f"No counterexample within {depth} cycles. This is a bounded "
                    "result — use mode='prove' for an unbounded proof."
                )
        elif status == "FAIL":
            detail = ""
            if parsed["failed_assertions"]:
                first = parsed["failed_assertions"][0]
                detail = f" First failure: {first['module']} at {first['location']}."
            result["error"] = f"Property violated in '{top_module}'.{detail}"
            result["error_code"] = FORMAL_FAILED
        elif status == "UNKNOWN":
            result["error"] = (
                "sby returned UNKNOWN — the engine could neither prove nor "
                "refute the properties. For mode='prove' this usually means "
                "induction failed; strengthen the invariant or try engine='abc-pdr'."
            )
            result["error_code"] = FORMAL_FAILED
        else:
            result["error"] = "sby failed to run the task — see log."
            result["error_code"] = FORMAL_FAILED

        if mode in _BOUNDED_MODES:
            result["bounded"] = True

        return result
