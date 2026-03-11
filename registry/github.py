# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "fpgaZeroMCP/0.1",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ---------------------------------------------------------------------------
# Allowed licenses
# Override by setting FPGAZERO_ALLOWED_LICENSES to a comma-separated list of
# SPDX identifiers, e.g.:  FPGAZERO_ALLOWED_LICENSES=MIT,Apache-2.0
# Default: MIT, GPL-2.0, GPL-3.0, LGPL-2.1, LGPL-3.0
# ---------------------------------------------------------------------------
_DEFAULT_LICENSES = {"MIT", "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0"}

def _allowed_licenses() -> set[str]:
    raw = os.environ.get("FPGAZERO_ALLOWED_LICENSES", "")
    if raw.strip():
        return {s.strip() for s in raw.split(",") if s.strip()}
    return _DEFAULT_LICENSES

HDL_EXTS  = {".v", ".sv", ".vhd", ".vhdl"}
CORE_EXTS = {".core"}

_LANGUAGE_MAP = {
    "verilog":       "Verilog",
    "systemverilog": "SystemVerilog",
    "vhdl":          "VHDL",
}

_CATEGORY_KEYWORDS = {
    "communication": ["uart", "spi", "i2c", "usb", "ethernet", "can", "serial", "rs232"],
    "memory":        ["fifo", "ram", "rom", "cache", "memory", "sdram", "ddr", "sram"],
    "dsp":           ["fft", "dsp", "filter", "fir", "iir", "cordic", "decimat"],
    "cpu":           ["cpu", "risc", "riscv", "mips", "processor", "core", "rv32"],
    "video":         ["vga", "hdmi", "video", "display", "lcd", "dvi"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> dict | list:
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, headers=_HEADERS, params=params)
        resp.raise_for_status()
        return resp.json()


def _download_raw(owner: str, repo: str, path: str, ref: str) -> str:
    url = f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers={"User-Agent": "fpgaZeroMCP/0.1"})
        resp.raise_for_status()
        return resp.text


def _fetch_tree(owner: str, repo: str, ref: str) -> list[dict]:
    data = _get(
        f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}",
        params={"recursive": "1"},
    )
    return data.get("tree", [])


def _filter_files(tree: list[dict], subdir: str = "") -> tuple[list[str], list[str]]:
    """Return (hdl_paths, core_paths) from a tree, scoped to subdir if given."""
    hdl, cores = [], []
    prefix = subdir.strip("/") + "/" if subdir else ""
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item["path"]
        if prefix and not path.startswith(prefix):
            continue
        ext = Path(path).suffix.lower()
        if ext in HDL_EXTS:
            hdl.append(path)
        elif ext in CORE_EXTS:
            cores.append(path)
    return hdl, cores


def _infer_category(text: str) -> str:
    text = text.lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return "uncategorized"


def _dominant_language(hdl_paths: list[str]) -> str:
    counts: dict[str, int] = {}
    for p in hdl_paths:
        ext = Path(p).suffix.lower()
        counts[ext] = counts.get(ext, 0) + 1
    dominant = max(counts, key=counts.get) if counts else ".v"
    return {".v": "verilog", ".sv": "systemverilog",
            ".vhd": "vhdl", ".vhdl": "vhdl"}.get(dominant, "verilog")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_repos(
    query: str,
    language: str | None = None,
    max_results: int = 10,
) -> list[dict]:
    """Search GitHub for open-source FPGA IP repositories (license checked at import)."""
    q = f"{query} topic:fpga"
    if language and language in _LANGUAGE_MAP:
        q += f" language:{_LANGUAGE_MAP[language]}"

    try:
        data = _get(f"{GITHUB_API}/search/repositories", params={
            "q": q, "sort": "stars", "order": "desc",
            "per_page": min(max_results, 30),
        })
    except httpx.HTTPStatusError as e:
        return [{"error": f"GitHub API error {e.response.status_code}: {e.response.text}"}]

    results = []
    for item in data.get("items", [])[:max_results]:
        results.append({
            "repo":           item["full_name"],
            "description":    item.get("description") or "",
            "stars":          item["stargazers_count"],
            "license":        (item.get("license") or {}).get("spdx_id", "unknown"),
            "topics":         item.get("topics", []),
            "default_branch": item.get("default_branch", "main"),
            "url":            item["html_url"],
        })
    return results


def import_core(
    owner_repo: str,
    subdir: str = "",
    ref: str = "",
    dest_parent: Path = Path(__file__).parent.parent / "cores",
) -> dict:
    """Download a GitHub repo and write it into dest_parent/<core_name>/.

    Automatically uses FuseSoC CAPI2 metadata if a .core file is found.
    Returns the manifest dict on success or an error dict.
    """
    parts = owner_repo.strip("/").split("/")
    if len(parts) != 2:
        return {"error": "repo must be 'owner/repo', e.g. 'ultraembedded/core_uart'"}
    owner, repo = parts
    subdir_norm = subdir.strip("/")

    # Repo metadata
    try:
        meta = _get(f"{GITHUB_API}/repos/{owner}/{repo}")
    except httpx.HTTPStatusError as e:
        return {"error": f"GitHub API {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}

    license_id = (meta.get("license") or {}).get("spdx_id", "unknown")
    allowed = _allowed_licenses()
    if license_id not in allowed:
        return {
            "error": (
                f"Repository license '{license_id}' is not in the allowed list: "
                f"{', '.join(sorted(allowed))}. "
                "Set FPGAZERO_ALLOWED_LICENSES to override."
            )
        }
    if not ref:
        ref = meta.get("default_branch", "main")

    # File tree
    try:
        tree = _fetch_tree(owner, repo, ref)
    except Exception as e:
        return {"error": f"Could not fetch file tree: {e}"}

    hdl_paths, core_paths = _filter_files(tree, subdir_norm)
    if not hdl_paths:
        scope = f"{owner_repo}/{subdir_norm}" if subdir_norm else owner_repo
        return {"error": f"No HDL files (.v/.sv/.vhd) found in {scope}"}

    # Try FuseSoC .core file for richer metadata
    fuse_manifest: dict | None = None
    if core_paths:
        try:
            raw = _download_raw(owner, repo, core_paths[0], ref)
            from registry.fusesoc import capi2_to_manifest_dict
            fuse_manifest = capi2_to_manifest_dict(raw, source=f"https://github.com/{owner_repo}")
        except Exception:
            pass  # non-fatal

    # Download HDL files (preserve relative paths)
    core_name = repo.lower().replace("-", "_").replace(".", "_")
    dest_dir  = dest_parent / core_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    for path in hdl_paths:
        try:
            content = _download_raw(owner, repo, path, ref)
            rel_path = Path(path)
            if subdir_norm:
                rel_path = rel_path.relative_to(subdir_norm)
            target = dest_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            downloaded.append(rel_path.as_posix())
        except Exception as e:
            # Write a stub so the manifest isn't broken
            rel_path = Path(path)
            if subdir_norm:
                rel_path = rel_path.relative_to(subdir_norm)
            target = dest_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"// Download failed: {e}\n", encoding="utf-8")
            downloaded.append(rel_path.as_posix())

    # Build manifest (FuseSoC wins if available)
    description = meta.get("description") or f"Imported from github.com/{owner_repo}"
    topics      = meta.get("topics", [])

    manifest = fuse_manifest or {
        "name":        core_name,
        "version":     ref,
        "description": description,
        "author":      owner,
        "license":     license_id,
        "language":    _dominant_language(hdl_paths),
        "category":    _infer_category(" ".join(topics) + " " + repo + " " + description),
        "tags":        topics[:8],
        "parameters":  {},
        "ports":       {},
        "files":       downloaded,
    }

    # Always update files list to what was actually downloaded
    manifest["files"]  = downloaded
    manifest["source"] = f"https://github.com/{owner_repo}"

    (dest_dir / "core.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "imported":      True,
        "core_name":     core_name,
        "source":        f"https://github.com/{owner_repo}",
        "ref":           ref,
        "license":       license_id,
        "files_fetched": downloaded,
        "fusesoc_used":  fuse_manifest is not None,
        "manifest":      manifest,
    }
