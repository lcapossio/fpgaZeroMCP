# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
FuseSoC CAPI2 (.core file) parser and converter.

Converts a CAPI2 manifest into our core.json format.
Reference: https://fusesoc.readthedocs.io/en/stable/user/capi2.html
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]

HDL_EXTS = {".v": "verilog", ".sv": "systemverilog", ".vhd": "vhdl", ".vhdl": "vhdl"}


def _parse_capi2(content: str) -> dict:
    """Strip the CAPI=2: header line and parse the remaining YAML."""
    lines = content.splitlines()
    if lines and lines[0].startswith("CAPI="):
        lines = lines[1:]
    return yaml.safe_load("\n".join(lines)) or {}


def _parse_name(raw: str) -> tuple[str, str]:
    """
    Parse a FuseSoC core name like  'vendor::library:version'
    or '::name:version' or 'name:version'.
    Returns (core_name, version).
    """
    # Strip leading/trailing colons, split on one or more colons
    parts = [p for p in re.split(r":+", raw.strip(":")) if p]
    if len(parts) >= 2:
        name, version = parts[-2], parts[-1]
    elif len(parts) == 1:
        name, version = parts[0], "0.0.0"
    else:
        name, version = "unknown", "0.0.0"
    return name.lower().replace("-", "_"), version


def _collect_hdl_files(filesets: dict) -> tuple[list[str], str]:
    """Return (filenames, dominant_language) from a CAPI2 filesets dict."""
    files: list[str] = []
    lang_votes: dict[str, int] = {}

    for fs in filesets.values():
        file_type = fs.get("file_type", "")
        for entry in fs.get("files", []):
            fname = entry if isinstance(entry, str) else next(iter(entry))
            ext = Path(fname).suffix.lower()
            if ext in HDL_EXTS:
                files.append(fname)
                lang = HDL_EXTS[ext]
                lang_votes[lang] = lang_votes.get(lang, 0) + 1

        # file_type can also hint the language even for non-standard extensions
        if "SystemVerilog" in file_type:
            lang_votes["systemverilog"] = lang_votes.get("systemverilog", 0) + 1
        elif "Verilog" in file_type:
            lang_votes["verilog"] = lang_votes.get("verilog", 0) + 1
        elif "VHDL" in file_type:
            lang_votes["vhdl"] = lang_votes.get("vhdl", 0) + 1

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)

    language = max(lang_votes, key=lang_votes.__getitem__) if lang_votes else "verilog"
    return unique, language


def _collect_parameters(raw: dict | None) -> dict:
    """Convert CAPI2 parameters section to our ParameterSpec dicts."""
    if not raw:
        return {}
    result = {}
    for pname, pdata in raw.items():
        dtype = pdata.get("datatype", "int")
        default = pdata.get("default", 0)
        if dtype in ("int", "intval"):
            ptype = "integer"
        elif dtype in ("bool",):
            ptype = "boolean"
        else:
            ptype = "string"
        result[pname] = {
            "type": ptype,
            "default": default,
            "description": pdata.get("description", ""),
        }
    return result


def capi2_to_manifest_dict(
    content: str,
    source: str = "",
    license: str = "",
) -> dict | None:
    """
    Parse a CAPI2 .core file and return a core.json-compatible dict.
    Returns None if parsing fails.
    """
    try:
        data = _parse_capi2(content)
    except yaml.YAMLError:
        return None

    if not data:
        return None

    name, version = _parse_name(data.get("name", "unknown"))
    description = data.get("description", "")
    filesets = data.get("filesets", {})
    hdl_files, language = _collect_hdl_files(filesets)
    parameters = _collect_parameters(data.get("parameters"))
    tags = list((data.get("targets") or {}).keys())[:6]

    return {
        "name": name,
        "version": version,
        "description": description,
        "author": "",
        "license": license or "MIT",
        "language": language,
        "category": "uncategorized",
        "tags": tags,
        "parameters": parameters,
        "ports": {},
        "files": hdl_files,
        "source": source,
    }


def load_core_file(path: Path) -> dict | None:
    """Load a local CAPI2 .core file and return a manifest dict."""
    try:
        return capi2_to_manifest_dict(
            path.read_text(encoding="utf-8"),
            source=str(path),
        )
    except Exception:
        return None
