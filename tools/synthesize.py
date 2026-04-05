# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from pathlib import Path

from tools.workspace import temporary_workspace

SYNTH_CMDS: dict[str, str] = {
    "generic": "synth",
    "ice40":   "synth_ice40",
    "ecp5":    "synth_ecp5",
    "nexus":   "synth_nexus",
    "gowin":   "synth_gowin",
    "xilinx":  "synth_xilinx",
    "intel":   "synth_intel",
}

_TOP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_LANG_EXT = {
    "verilog":       [".v"],
    "systemverilog": [".sv"],
    "vhdl":          [".vhd", ".vhdl"],
}


def validate_top_module(name: str) -> str | None:
    if not _TOP_RE.fullmatch(name):
        return (
            "Invalid top_module. Must be a Verilog identifier "
            "(letters/digits/_/$, not starting with a digit)."
        )
    return None


def _allowed_project_roots() -> list[Path]:
    """Return directories that project_dir is allowed to be under."""
    roots = [Path.cwd().resolve()]
    # FPGAZERO_ALLOWED_DIRS: os.pathsep-separated list of extra allowed roots
    env_dirs = os.environ.get("FPGAZERO_ALLOWED_DIRS", "").strip()
    if env_dirs:
        for d in env_dirs.split(os.pathsep):
            d = d.strip()
            if d:
                roots.append(Path(d).resolve())
    # User home directory is always allowed
    roots.append(Path.home().resolve())
    return roots


def _validate_project_dir(project_dir: str) -> str | None:
    """Return an error string if project_dir is outside all allowed roots."""
    try:
        resolved = Path(project_dir).resolve()
        for root in _allowed_project_roots():
            if resolved.is_relative_to(root):
                return None
        return (
            f"project_dir is outside allowed directories. "
            f"Got: {resolved}. Set FPGAZERO_ALLOWED_DIRS to add more roots."
        )
    except (OSError, ValueError) as exc:
        return f"Invalid project_dir: {exc}"


def _validate_filename(fname: str) -> str | None:
    """Return an error string if a filename in the files dict is unsafe."""
    p = Path(fname)
    if p.is_absolute():
        return f"Unsafe filename (absolute path): {fname}"
    if ".." in p.parts:
        return f"Unsafe filename (path traversal): {fname}"
    return None


def _resolve_sources(
    code: str = "",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    language: str = "verilog",
    tmpdir: str = "",
) -> tuple[list[str], str | None]:
    """Resolve HDL source files from one of three input modes.

    Returns (list_of_file_paths, error_string_or_None).
    Paths are absolute; for code/files modes they live inside tmpdir.
    """
    modes = sum([bool(code), bool(files), bool(project_dir)])
    if modes == 0:
        return [], "Provide one of: code, files, or project_dir."
    if modes > 1:
        return [], "Provide only one of: code, files, or project_dir."

    exts = _LANG_EXT.get(language, [".v"])

    if project_dir:
        path_err = _validate_project_dir(project_dir)
        if path_err:
            return [], path_err
        if not os.path.isdir(project_dir):
            return [], f"project_dir does not exist: '{project_dir}'"
        found: list[str] = []
        for ext in exts:
            found.extend(glob.glob(os.path.join(project_dir, "**", f"*{ext}"), recursive=True))
        if not found:
            return [], f"No {language} files found in '{project_dir}'"
        return sorted(set(found)), None

    if files:
        written: list[str] = []
        suffix = exts[0]
        for fname, src in files.items():
            fname_err = _validate_filename(fname)
            if fname_err:
                return [], fname_err
            if not any(fname.lower().endswith(e) for e in (".v", ".sv", ".vhd", ".vhdl")):
                fname = fname + suffix
            fpath = os.path.join(tmpdir, fname)
            # Verify resolved path is still inside tmpdir
            if not Path(fpath).resolve().is_relative_to(Path(tmpdir).resolve()):
                return [], f"Unsafe filename (escapes tmpdir): {fname}"
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(src)
            written.append(fpath)
        return written, None

    # Single code string
    suffix = exts[0]
    src_path = os.path.join(tmpdir, f"design{suffix}")
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(code)
    return [src_path], None


def _yosys_read_cmds(src_paths: list[str], language: str, top_module: str = "") -> str:
    """Generate Yosys read commands for the given language and source files.

    For VHDL, emits a single ghdl invocation that analyzes all files and
    elaborates the top module (requires ghdl-yosys-plugin, shipped in OSS CAD Suite).
    """
    paths = [p.replace("\\", "/") for p in src_paths]

    if language == "vhdl":
        # Single ghdl command: analyze all files + elaborate in one invocation
        all_files = " ".join(paths)
        if top_module:
            return f"ghdl --std=08 {all_files} -e {top_module}\n"
        return f"ghdl --std=08 {all_files}\n"

    lines: list[str] = []
    if language == "systemverilog":
        for p in paths:
            lines.append(f"read_verilog -sv {p}")
    else:
        for p in paths:
            lines.append(f"read_verilog {p}")

    return "\n".join(lines) + "\n"


def synthesize(
    code: str = "",
    top_module: str = "",
    target: str = "generic",
    language: str = "verilog",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    backend: str = "yosys",
    litex_board: str | None = None,
    litex_args: list[str] | None = None,
    timeout: int = 120,
) -> dict:
    """Synthesize HDL using Yosys or LiteX backend.

    Source input (provide exactly one):
      code:        single HDL source as a string
      files:       dict of filename → source code for multi-file designs
      project_dir: path to a directory containing HDL files on disk

    language: "verilog" (default), "systemverilog", or "vhdl".
    """
    if backend == "litex":
        if not litex_board:
            return {"success": False, "error": "litex_board is required for LiteX backend."}
        from tools.litex import litex_flow
        result = litex_flow(board=litex_board, args=litex_args or [], timeout=max(timeout, 120))
        result["backend"] = "litex"
        result["note"] = "LiteX backend ignores code/top_module and runs board target."
        return result

    synth_cmd = SYNTH_CMDS.get(target, "synth")
    if top_module:
        top_err = validate_top_module(top_module)
        if top_err:
            return {"success": False, "error": top_err}
    else:
        return {"success": False, "error": "top_module is required."}

    with temporary_workspace("synth_") as tmpdir:
        src_paths, err = _resolve_sources(code, files, project_dir, language, tmpdir)
        if err:
            return {"success": False, "error": err}

        out_json  = os.path.join(tmpdir, "synth.json")
        ys_script = os.path.join(tmpdir, "synth.ys")
        out_json_yosys = out_json.replace("\\", "/")

        # _yosys_read_cmds handles VHDL elaborate in a single ghdl invocation
        read_cmds = _yosys_read_cmds(src_paths, language, top_module)

        # ghdl-yosys-plugin lowercases VHDL entity names during import
        yosys_top = top_module.lower() if language == "vhdl" else top_module

        # Generic 'synth' doesn't support -json; use write_json separately
        if target == "generic":
            script = (
                f"{read_cmds}"
                f"{synth_cmd} -top {yosys_top}\n"
                f"write_json {out_json_yosys}\n"
                f"stat\n"
            )
        else:
            script = (
                f"{read_cmds}"
                f"{synth_cmd} -top {yosys_top} -json {out_json_yosys}\n"
                f"stat\n"
            )
        with open(ys_script, "w", encoding="utf-8") as f:
            f.write(script)

        try:
            result = subprocess.run(
                ["yosys", "-s", ys_script],
                capture_output=True, text=True, errors="replace", timeout=timeout,
            )

            modules: list[str] = []
            if os.path.exists(out_json):
                with open(out_json) as f:
                    netlist = json.load(f)
                modules = list(netlist.get("modules", {}).keys())

            output: dict = {
                "success": result.returncode == 0,
                "target": target,
                "language": language,
                "top_module": top_module,
                "modules": modules,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            if files:
                output["files"] = list(files.keys())
            if project_dir:
                resolved_dir = str(Path(project_dir).resolve())
                output["project_dir"] = project_dir
                output["source_files"] = [
                    os.path.relpath(p, resolved_dir) for p in src_paths
                ]
            return output

        except FileNotFoundError:
            return {"success": False, "error": "'yosys' not found. Install it and ensure it is on PATH."}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Synthesis timed out after {timeout} s."}
