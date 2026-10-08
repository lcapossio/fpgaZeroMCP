# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import glob as _glob
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fpgazero_mcp.tools.boards import get_board_preset
from fpgazero_mcp.tools.progress import make_reporter
from fpgazero_mcp.tools.sources import (
    resolve_sources,
    validate_path_in_roots,
    validate_top_module,
    yosys_read_cmds,
)
from fpgazero_mcp.tools.synthesize import SYNTH_CMDS
from fpgazero_mcp.tools.textutil import truncate_log
from fpgazero_mcp.paths import data_root
from fpgazero_mcp.tools.workspace import temporary_workspace

# nextpnr binary per target
NEXTPNR_BIN = {
    "ice40": "nextpnr-ice40",
    "ecp5": "nextpnr-ecp5",
    "nexus": "nextpnr-nexus",
    "gowin": "nextpnr-gowin",
}

# Constraints file extension per target
CONSTRAINTS_EXT = {
    "ice40": ".pcf",
    "ecp5": ".lpf",
    "nexus": ".pdc",
    "gowin": ".cst",
}

# nextpnr output file extension per target
OUTPUT_EXT = {
    "ice40": ".asc",
    "ecp5": ".config",
    "nexus": ".fasm",
    "gowin": "_pnr.json",
}


def _find_constraints(project_dir: str, target: str) -> str | None:
    """Auto-detect a constraint file in project_dir for the given target.

    Returns the file path if found, else None.
    """
    ext = CONSTRAINTS_EXT.get(target)
    if not ext:
        return None
    matches = _glob.glob(os.path.join(project_dir, f"**/*{ext}"), recursive=True)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer files in root over subdirectories, then shortest path
        matches.sort(key=lambda p: (p.count(os.sep), len(p)))
        return matches[0]
    return None


def _inputs_digest(script: str, src_paths: list[str]) -> str:
    """Fingerprint the synthesis inputs: the yosys script plus every source.

    Used to decide whether a netlist in a persistent work_dir still matches the
    design. Content-based rather than mtime-based on purpose: resolve_sources
    rewrites code/files sources into the work_dir on every run, so their mtimes
    are always newer than the netlist and an mtime check would never reuse.
    """
    h = hashlib.sha256()
    h.update(script.encode("utf-8"))
    for path in sorted(src_paths):
        h.update(os.path.basename(path).encode("utf-8"))
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            # Unreadable source: make the digest unmatchable so we re-synthesize
            # rather than reuse a netlist we cannot vouch for.
            h.update(b"<unreadable>")
    return h.hexdigest()


def place_and_route(
    code: str = "",
    top_module: str = "",
    target: str = "",
    device: str = "",
    package: str = "",
    constraints: str = "",
    language: str = "verilog",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    board: str | None = None,
    nextpnr_args: list[str] | None = None,
    work_dir: str | None = None,
    timeout: int = 300,
    backend: str = "yosys",
    seed: int | None = None,
    target_mhz: float | None = None,
    allow_timing_fail: bool = False,
    reuse_netlist: bool = False,
    synth_directive: str = "default",
    place_directive: str = "default",
    route_directive: str = "default",
    phys_opt: bool = False,
    litex_board: str | None = None,
    litex_args: list[str] | None = None,
    return_bitstream_b64: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Synthesize HDL with Yosys then place-and-route with nextpnr.

    Source input (provide exactly one):
      code:        single HDL source as a string
      files:       dict of filename → source code for multi-file designs
      project_dir: path to a directory containing HDL files on disk

    language: "verilog" (default), "systemverilog", or "vhdl".
    board: optional board preset (e.g. "icebreaker") — sets target/device/package/clock.
    nextpnr_args: extra arguments passed to nextpnr (e.g. ["--seed", "42"]).
    seed: nextpnr placer seed. Different seeds give different timing results
          for the same netlist; sweeping them is what `close_timing` does.
    target_mhz: clock target to judge timing against. Defaults to the board
                preset's clock when a board is given.
    allow_timing_fail: pass --timing-allow-fail to nextpnr, so a design that
                routes but misses timing still returns a bitstream and an
                achieved fmax instead of failing the run. Off by default so
                a plain call still treats missed timing as a failure.
    reuse_netlist: skip synthesis and reuse the netlist.json already present in
                work_dir. Requires work_dir. Lets a seed sweep synthesize once
                and re-run place-and-route only, since the netlist does not
                depend on the placer seed.
    work_dir: path to a persistent working directory. If provided, files and
              synthesis artifacts are kept across runs for incremental workflows.
              The path is returned in the response for reuse.

    constraints: optional PCF/LPF/PDC/CST text. If omitted and project_dir is
                 used, auto-detects constraint files from the project directory.
    return_bitstream_b64: also include the bitstream as base64 in the response.
                 By default only bitstream_path (a file on disk, ready for
                 program_fpga) is returned to keep the response small.
    progress: optional callback (fraction 0..1, message) invoked at phase
              boundaries; exceptions from it are swallowed.
    """
    report = make_reporter(progress)
    if backend == "litex":
        if not litex_board:
            return {
                "success": False,
                "error": "litex_board is required for LiteX backend.",
                "error_code": "invalid_input",
            }
        from fpgazero_mcp.tools.litex import litex_build

        result = litex_build(
            board=litex_board,
            args=litex_args or [],
            timeout=max(timeout, 300),
            progress=progress,
        )
        result["backend"] = "litex"
        result["note"] = (
            "LiteX backend ignores code/top_module/target/device and runs board build."
        )
        return result

    # Apply board preset (explicit params override preset values)
    preset = get_board_preset(board) if board else None
    if preset:
        target = target or preset["target"]
        device = device or preset["device"]
        package = package or preset.get("package", "")

    # Xilinx has no nextpnr backend — route to the Vivado batch flow.
    if backend == "vivado" or target == "xilinx":
        from fpgazero_mcp.tools.vivado import vivado_place_and_route

        result = vivado_place_and_route(
            code=code,
            top_module=top_module,
            part=device,
            constraints=constraints,
            language=language,
            files=files,
            project_dir=project_dir,
            work_dir=work_dir,
            timeout=timeout,
            return_bitstream_b64=return_bitstream_b64,
            synth_directive=synth_directive,
            place_directive=place_directive,
            route_directive=route_directive,
            phys_opt=phys_opt,
            progress=progress,
        )
        if board:
            result["board"] = board
        if target_mhz and "timing" in result:
            result["timing"]["target_mhz"] = target_mhz
            fmax = result["timing"].get("max_freq_mhz")
            if fmax is not None:
                result["timing"]["meets_target"] = fmax >= target_mhz
        elif preset and preset.get("clock_mhz") and "timing" in result:
            target_mhz = preset["clock_mhz"]
            result["timing"]["target_mhz"] = target_mhz
            fmax = result["timing"].get("max_freq_mhz")
            if fmax is not None:
                result["timing"]["meets_target"] = fmax >= target_mhz
        return result

    if not target or target not in NEXTPNR_BIN:
        supported = list(NEXTPNR_BIN.keys()) + ["xilinx (Vivado backend)"]
        return {
            "success": False,
            "error": f"Unsupported PnR target '{target}'. Supported: {supported}",
            "error_code": "invalid_input",
        }

    synth_cmd = SYNTH_CMDS.get(target)
    if not synth_cmd:
        return {
            "success": False,
            "error": f"No Yosys synth command for target '{target}'",
            "error_code": "invalid_input",
        }
    if not top_module:
        return {
            "success": False,
            "error": "top_module is required.",
            "error_code": "invalid_input",
        }
    top_err = validate_top_module(top_module)
    if top_err:
        return {"success": False, "error": top_err, "error_code": "invalid_input"}

    # Determine whether to use a persistent work_dir or a temp workspace
    if backend == "yosys" and target != "xilinx":
        if (
            synth_directive != "default"
            or place_directive != "default"
            or route_directive != "default"
            or phys_opt
        ):
            return {
                "success": False,
                "error": (
                    "synth_directive/place_directive/route_directive/phys_opt are "
                    "Vivado-only. For nextpnr targets use `seed` instead."
                ),
                "error_code": "invalid_input",
            }

    if reuse_netlist and not work_dir:
        return {
            "success": False,
            "error": "reuse_netlist requires work_dir (the netlist is read from it).",
            "error_code": "invalid_input",
        }

    use_persistent = bool(work_dir)
    persistent_dir: str = ""
    if use_persistent:
        assert work_dir is not None
        path_err = validate_path_in_roots(work_dir, "work_dir")
        if path_err:
            return {"success": False, "error": path_err, "error_code": "path_security"}
        os.makedirs(work_dir, exist_ok=True)
        persistent_dir = work_dir

    def _run_in(tmpdir: str) -> dict:
        src_paths, err = resolve_sources(code, files, project_dir, language, tmpdir)
        if err:
            return {"success": False, "error": err, "error_code": "invalid_input"}
        report(0.05, "sources resolved")

        netlist_json = os.path.join(tmpdir, "netlist.json")
        ys_script = os.path.join(tmpdir, "synth.ys")
        out_file = os.path.join(tmpdir, f"out{OUTPUT_EXT[target]}")
        cst_file = os.path.join(tmpdir, f"constraints{CONSTRAINTS_EXT[target]}")

        # Auto-detect include directories and defines from project_dir
        include_dirs: list[str] = []
        defines: list[str] = []
        if project_dir:
            from fpgazero_mcp.tools.sources import parse_filelist

            resolved_proj = str(Path(project_dir).resolve())
            include_dirs.append(resolved_proj)
            # Check filelist for +incdir+ and +define+ directives
            for filelist_name in ("files.f", "sources.f"):
                filelist_path = os.path.join(project_dir, filelist_name)
                if os.path.isfile(filelist_path):
                    _, fl_incdirs, fl_defines = parse_filelist(
                        filelist_path, project_dir
                    )
                    include_dirs.extend(fl_incdirs)
                    defines.extend(fl_defines)
                    break
            # Also add subdirectories with header files
            for root, _dirs, fnames in os.walk(resolved_proj):
                if any(f.endswith((".vh", ".svh")) for f in fnames):
                    if root not in include_dirs:
                        include_dirs.append(root)

        # yosys_read_cmds handles VHDL elaborate in a single ghdl invocation
        read_cmds = yosys_read_cmds(
            src_paths, language, top_module, include_dirs, defines
        )
        netlist_yosys = netlist_json.replace("\\", "/")

        # ghdl-yosys-plugin lowercases VHDL entity names during import
        yosys_top = top_module.lower() if language == "vhdl" else top_module

        script = f"{read_cmds}{synth_cmd} -top {yosys_top} -json {netlist_yosys}\n"
        with open(ys_script, "w", encoding="utf-8") as f:
            f.write(script)

        # ------------------------------------------------------------------
        # Stage 1: Synthesis
        # ------------------------------------------------------------------
        import time as _time

        deadline = _time.monotonic() + timeout
        synth_stdout = ""
        synth_stderr = ""
        netlist_reused = False

        stamp_path = netlist_json + ".inputs"
        digest = _inputs_digest(script, src_paths)
        reuse_skipped_reason = ""

        if reuse_netlist:
            if not os.path.exists(netlist_json):
                reuse_skipped_reason = "no netlist.json in work_dir yet"
            else:
                try:
                    with open(stamp_path, "r", encoding="utf-8") as f:
                        recorded = f.read().strip()
                except OSError:
                    recorded = ""
                if not recorded:
                    reuse_skipped_reason = (
                        "netlist has no recorded input fingerprint; it was not "
                        "produced by this tool"
                    )
                elif recorded != digest:
                    reuse_skipped_reason = "sources or synthesis options changed"

        if reuse_netlist and not reuse_skipped_reason:
            # Seed sweeps re-run place-and-route against an unchanged netlist;
            # re-synthesizing it every attempt costs time and yields nothing.
            netlist_reused = True
            report(0.1, "reusing netlist from work_dir, skipping synthesis")
        else:
            synth_timeout = min(max(timeout // 3, 30), timeout)
            report(0.1, "running yosys synthesis")
            try:
                synth = subprocess.run(
                    ["yosys", "-s", ys_script],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=synth_timeout,
                )
            except FileNotFoundError:
                return {
                    "success": False,
                    "error": "'yosys' not found. Install OSS CAD Suite.",
                    "error_code": "tool_not_found",
                }
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "error": f"Synthesis timed out after {synth_timeout} s.",
                    "error_code": "timeout",
                }
            synth_stdout = synth.stdout
            synth_stderr = synth.stderr

            if synth.returncode != 0:
                return {
                    "success": False,
                    "stage": "synthesis",
                    "error": "Synthesis failed — see stdout/stderr.",
                    "error_code": "synthesis_failed",
                    "stdout": synth.stdout,
                    "stderr": synth.stderr,
                }

            if not os.path.exists(netlist_json):
                return {
                    "success": False,
                    "stage": "synthesis",
                    "error": "Yosys did not produce a netlist JSON.",
                    "error_code": "synthesis_failed",
                    "stdout": synth.stdout,
                    "stderr": synth.stderr,
                }

            # Record what this netlist was built from, so a later
            # reuse_netlist run can tell whether it is still valid.
            if use_persistent:
                try:
                    with open(stamp_path, "w", encoding="utf-8") as f:
                        f.write(digest)
                except OSError:
                    pass

        report(0.5, "synthesis complete, starting place and route")

        # Resolve constraints: explicit string > auto-detect from project_dir
        effective_cst: str | None = None
        cst_source = "none"
        if constraints:
            with open(cst_file, "w", encoding="utf-8") as f:
                f.write(constraints)
            effective_cst = cst_file
            cst_source = "provided"
        elif project_dir:
            auto_cst = _find_constraints(project_dir, target)
            if auto_cst:
                effective_cst = auto_cst
                cst_source = f"auto-detected: {os.path.basename(auto_cst)}"

        # ------------------------------------------------------------------
        # Stage 2: Place and route
        # ------------------------------------------------------------------
        cmd = _build_cmd(
            NEXTPNR_BIN[target],
            target,
            device,
            package,
            netlist_json,
            out_file,
            effective_cst,
        )
        extra_args = list(nextpnr_args or [])
        if seed is not None and "--seed" not in extra_args:
            extra_args += ["--seed", str(seed)]
        if allow_timing_fail and "--timing-allow-fail" not in extra_args:
            # Without this nextpnr treats a missed clock constraint as a hard
            # error, which would make the attempts in a sweep incomparable:
            # a near miss would return no fmax at all.
            extra_args.append("--timing-allow-fail")
        if extra_args:
            cmd.extend(extra_args)

        pnr_timeout = int(deadline - _time.monotonic())
        if pnr_timeout < 10:
            return {
                "success": False,
                "stage": "place_and_route",
                "error": (
                    f"Synthesis used most of the {timeout} s budget; "
                    f"only {max(pnr_timeout, 0)} s remain for PnR. "
                    "Increase timeout or simplify the design."
                ),
                "error_code": "timeout",
                "synth_log": synth.stdout,
            }
        try:
            pnr = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=pnr_timeout,
            )
        except FileNotFoundError:
            return {
                "success": False,
                "error": f"'{NEXTPNR_BIN[target]}' not found. Install OSS CAD Suite.",
                "error_code": "tool_not_found",
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Place and route timed out after {pnr_timeout} s.",
                "error_code": "timeout",
            }

        report(0.9, "place and route finished, collecting results")
        combined_output = pnr.stdout + pnr.stderr
        pnr_ok = pnr.returncode == 0

        # Keep full logs on failure; truncate on success so routine runs
        # don't flood the AI client's context (timing/utilization are
        # returned as structured fields anyway).
        synth_log, synth_trunc = (
            truncate_log(synth_stdout) if pnr_ok else (synth_stdout, False)
        )
        pnr_stdout, pnr_trunc = (
            truncate_log(pnr.stdout) if pnr_ok else (pnr.stdout, False)
        )

        result: dict = {
            "success": pnr_ok,
            "stage": "place_and_route",
            "target": target,
            "device": device,
            "package": package,
            "language": language,
            "top_module": top_module,
            "constraints": cst_source,
            "timing": _parse_timing(combined_output),
            "utilization": _parse_utilization(combined_output, target),
            "synth_log": synth_log,
            "pnr_stdout": pnr_stdout,
            "pnr_stderr": pnr.stderr,
        }
        if netlist_reused:
            result["netlist_reused"] = True
        elif reuse_netlist and reuse_skipped_reason:
            # Say so rather than silently re-synthesizing: a caller that asked
            # for reuse is reasoning about how long the run should take.
            result["netlist_reused"] = False
            result["netlist_reuse_skipped"] = reuse_skipped_reason
        if synth_stderr and not pnr_ok:
            result["synth_stderr"] = synth_stderr
        if seed is not None:
            result["seed"] = seed
        if synth_trunc or pnr_trunc:
            result["logs_truncated"] = True
        if not pnr_ok:
            result["error"] = "Place and route failed — see pnr_stdout/pnr_stderr."
            result["error_code"] = "pnr_failed"

        # Evaluate timing against the explicit target, else the board clock.
        effective_target = target_mhz
        if effective_target is None and preset and preset.get("clock_mhz"):
            effective_target = preset["clock_mhz"]
        if effective_target:
            fmax = result["timing"].get("max_freq_mhz")
            result["timing"]["target_mhz"] = effective_target
            if fmax is not None:
                result["timing"]["meets_target"] = fmax >= effective_target

        # "routed" is deliberately distinct from "success": with
        # allow_timing_fail a design can route and produce a bitstream while
        # still missing its clock target, and a sweep needs to tell those apart.
        result["routed"] = pnr_ok and os.path.exists(out_file)

        if board:
            result["board"] = board
        if use_persistent:
            result["work_dir"] = tmpdir
        if nextpnr_args:
            result["nextpnr_args"] = nextpnr_args

        # Persist the bitstream and return its path (feeds straight into
        # program_fpga's bitstream_path). Base64 only on request — it is
        # large and rarely useful inline.
        if pnr_ok and os.path.exists(out_file):
            if use_persistent:
                bitstream_path = out_file
            else:
                bs_dir = data_root() / "bitstreams"
                bs_dir.mkdir(parents=True, exist_ok=True)
                bs_name = f"{top_module}_{target}_{uuid4().hex[:8]}{OUTPUT_EXT[target]}"
                bitstream_path = str(bs_dir / bs_name)
                shutil.copyfile(out_file, bitstream_path)
            result["bitstream_path"] = bitstream_path
            result["bitstream_ext"] = OUTPUT_EXT[target]
            if return_bitstream_b64:
                with open(out_file, "rb") as bf:
                    result["bitstream_b64"] = base64.b64encode(bf.read()).decode(
                        "ascii"
                    )

        report(1.0, "place and route complete")
        return result

    if use_persistent:
        return _run_in(persistent_dir)
    else:
        with temporary_workspace("pnr_") as tmpdir:
            return _run_in(tmpdir)


def _build_cmd(
    binary: str,
    target: str,
    device: str,
    package: str,
    netlist: str,
    out_file: str,
    constraints: str | None,
) -> list[str]:
    cmd = [binary]

    if target == "ice40":
        cmd += [f"--{device}"]
        if package:
            cmd += ["--package", package]
        cmd += ["--json", netlist, "--asc", out_file]
        if constraints:
            cmd += ["--pcf", constraints]

    elif target == "ecp5":
        cmd += [f"--{device}"]
        if package:
            cmd += ["--package", package]
        cmd += ["--json", netlist, "--textcfg", out_file]
        if constraints:
            cmd += ["--lpf", constraints]

    elif target == "nexus":
        cmd += ["--device", device, "--json", netlist, "--fasm", out_file]
        if constraints:
            cmd += ["--pdc", constraints]

    elif target == "gowin":
        cmd += ["--device", device, "--json", netlist, "--write", out_file]
        if constraints:
            cmd += ["--cst", constraints]

    return cmd


def _parse_timing(output: str) -> dict:
    timing: dict = {}

    # "Max frequency for clock 'clk': 142.34 MHz (PASS at 12.00 MHz)"
    m = re.search(r"Max frequency for clock[^:]*:\s*([\d.]+)\s*MHz", output)
    if m:
        timing["max_freq_mhz"] = float(m.group(1))

    # "Critical path: 7.03 ns"
    m = re.search(r"[Cc]ritical path[^:]*:\s*([\d.]+)\s*ns", output)
    if m:
        timing["critical_path_ns"] = float(m.group(1))

    return timing


def _parse_utilization(output: str, target: str) -> dict:
    util: dict = {}

    patterns = {
        "ice40": [
            (r"ICESTORM_LC[:\s]+([\d]+)/\s*([\d]+)", "luts"),
            (r"SB_IO[:\s]+([\d]+)/\s*([\d]+)", "ios"),
            (r"SB_RAM40_4K[:\s]+([\d]+)/\s*([\d]+)", "brams"),
        ],
        "ecp5": [
            (r"LUT4[:\s]+([\d]+)/\s*([\d]+)", "luts"),
            (r"TRELLIS_IO[:\s]+([\d]+)/\s*([\d]+)", "ios"),
            (r"TRELLIS_RAMW[:\s]+([\d]+)/\s*([\d]+)", "brams"),
        ],
        "nexus": [
            (r"OXIDE_COMB[:\s]+([\d]+)/\s*([\d]+)", "luts"),
            (r"OXIDE_FF[:\s]+([\d]+)/\s*([\d]+)", "ffs"),
        ],
        "gowin": [
            (r"LUT[:\s]+([\d]+)/\s*([\d]+)", "luts"),
            (r"FF[:\s]+([\d]+)/\s*([\d]+)", "ffs"),
        ],
    }

    for pattern, label in patterns.get(target, []):
        m = re.search(pattern, output)
        if m:
            util[f"{label}_used"] = int(m.group(1))
            util[f"{label}_total"] = int(m.group(2))

    return util
