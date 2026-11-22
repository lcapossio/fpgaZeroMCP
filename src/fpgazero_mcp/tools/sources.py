# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Shared HDL source resolution, path validation and Yosys read-command emission.

Every backend (yosys synthesis, nextpnr place-and-route, Vivado, simulation)
takes the same three input modes -- an inline `code` string, a `files` dict, or
an on-disk `project_dir` -- and must apply the same path-safety rules and the
same language handling. That logic lives here so the backends share one
implementation instead of importing private helpers out of each other.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

_TOP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

LANG_EXT = {
    "verilog": [".v"],
    "systemverilog": [".sv"],
    "vhdl": [".vhd", ".vhdl"],
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
    # So is the server's own data root: tools create work dirs there (e.g.
    # close_timing's sweep dir) and pass them on, and FPGAZERO_DATA_DIR may
    # point outside cwd and home.
    from fpgazero_mcp.paths import data_root

    roots.append(data_root().resolve())
    return roots


def validate_path_in_roots(path: str, label: str) -> str | None:
    """Return an error string if path is outside all allowed roots."""
    try:
        resolved = Path(path).resolve()
        for root in _allowed_project_roots():
            if resolved.is_relative_to(root):
                return None
        return (
            f"{label} is outside allowed directories. "
            f"Got: {resolved}. Set FPGAZERO_ALLOWED_DIRS to add more roots."
        )
    except (OSError, ValueError) as exc:
        return f"Invalid {label}: {exc}"


def validate_project_dir(project_dir: str) -> str | None:
    """Return an error string if project_dir is outside all allowed roots."""
    return validate_path_in_roots(project_dir, "project_dir")


# Characters that separate commands or trigger substitution in the scripts
# these names are written into: Yosys splits commands on ';' and newlines
# (and runs '!cmd' as a shell command), Tcl substitutes $ and [ ] and quotes
# with { } and ". No HDL source needs them in its file name.
_FILENAME_SCRIPT_CHARS = frozenset(';{}[]$"`')


def validate_filename(fname: str) -> str | None:
    """Return an error string if a filename in the files dict is unsafe."""
    if any(ord(c) < 0x20 or c == "\x7f" for c in fname):
        return f"Unsafe filename (control character): {fname!r}"
    bad = sorted(set(fname) & _FILENAME_SCRIPT_CHARS)
    if bad:
        return (
            f"Unsafe filename (contains {' '.join(bad)}, which would be "
            f"interpreted in the generated tool script): {fname}"
        )
    p = Path(fname)
    if p.is_absolute():
        return f"Unsafe filename (absolute path): {fname}"
    if ".." in p.parts:
        return f"Unsafe filename (path traversal): {fname}"
    return None


def parse_filelist(
    filelist_path: str, base_dir: str
) -> tuple[list[str], list[str], list[str]]:
    """Parse a files.f / filelist (one path per line, +incdir+, +define+).

    Returns (source_files, include_dirs, defines).
    Paths are resolved relative to base_dir.
    """
    sources: list[str] = []
    incdirs: list[str] = []
    defines: list[str] = []

    with open(filelist_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if line.startswith("+incdir+"):
                inc = line[len("+incdir+") :]
                incdirs.append(str(Path(base_dir, inc).resolve()))
            elif line.startswith("+define+"):
                defines.append(line[len("+define+") :])
            elif line.startswith("-f "):
                # Nested filelist — resolve recursively
                nested = str(Path(base_dir, line[3:].strip()).resolve())
                if os.path.isfile(nested):
                    nested_srcs, nested_incs, nested_defs = parse_filelist(
                        nested, os.path.dirname(nested)
                    )
                    sources.extend(nested_srcs)
                    incdirs.extend(nested_incs)
                    defines.extend(nested_defs)
            else:
                sources.append(str(Path(base_dir, line).resolve()))

    return sources, incdirs, defines


def resolve_sources(
    code: str = "",
    files: dict[str, str] | None = None,
    project_dir: str | None = None,
    language: str = "verilog",
    tmpdir: str = "",
) -> tuple[list[str], str | None]:
    """Resolve HDL source files from one of three input modes.

    Returns (list_of_file_paths, error_string_or_None).
    Paths are absolute; for code/files modes they live inside tmpdir.

    In project_dir mode, if a files.f filelist exists in the project root,
    it is used instead of globbing. This supports compile order, +incdir+,
    and +define+ directives.
    """
    modes = sum([bool(code), bool(files), bool(project_dir)])
    if modes == 0:
        return [], "Provide one of: code, files, or project_dir."
    if modes > 1:
        return [], "Provide only one of: code, files, or project_dir."

    exts = LANG_EXT.get(language, [".v"])

    if project_dir:
        path_err = validate_project_dir(project_dir)
        if path_err:
            return [], path_err
        if not os.path.isdir(project_dir):
            return [], f"project_dir does not exist: '{project_dir}'"
        # Check for filelist first (files.f, sources.f)
        for filelist_name in ("files.f", "sources.f"):
            filelist_path = os.path.join(project_dir, filelist_name)
            if os.path.isfile(filelist_path):
                sources, _incdirs, _defines = parse_filelist(
                    filelist_path,
                    project_dir,
                )
                if sources:
                    return sources, None
        # Fallback: glob for HDL files
        found: list[str] = []
        for ext in exts:
            found.extend(
                glob.glob(os.path.join(project_dir, "**", f"*{ext}"), recursive=True)
            )
        if not found:
            return [], f"No {language} files found in '{project_dir}'"
        return sorted(set(found)), None

    if files:
        written: list[str] = []
        suffix = exts[0]
        for fname, src in files.items():
            fname_err = validate_filename(fname)
            if fname_err:
                return [], fname_err
            if not any(
                fname.lower().endswith(e) for e in (".v", ".sv", ".vhd", ".vhdl")
            ):
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


def yosys_read_cmds(
    src_paths: list[str],
    language: str,
    top_module: str = "",
    include_dirs: list[str] | None = None,
    defines: list[str] | None = None,
) -> str:
    """Generate Yosys read commands for the given language and source files.

    For VHDL, emits a single ghdl invocation that analyzes all files and
    elaborates the top module (requires ghdl-yosys-plugin, shipped in OSS CAD Suite).
    include_dirs: directories to add as -I flags for Verilog/SV `include resolution.
    defines: preprocessor defines to add as -D flags for Verilog/SV.
    """
    paths = [p.replace("\\", "/") for p in src_paths]
    extra_flags = ""
    if language != "vhdl":
        parts: list[str] = []
        if include_dirs:
            parts.extend(f"-I{d.replace(chr(92), '/')}" for d in include_dirs)
        if defines:
            parts.extend(f"-D{d}" for d in defines)
        if parts:
            extra_flags = " ".join(parts) + " "

    if language == "vhdl":
        # Single ghdl command: analyze all files + elaborate in one invocation
        all_files = " ".join(paths)
        if top_module:
            return f"ghdl --std=08 {all_files} -e {top_module}\n"
        return f"ghdl --std=08 {all_files}\n"

    lines: list[str] = []
    if language == "systemverilog":
        for p in paths:
            lines.append(f"read_verilog -sv {extra_flags}{p}")
    else:
        for p in paths:
            lines.append(f"read_verilog {extra_flags}{p}")

    return "\n".join(lines) + "\n"
