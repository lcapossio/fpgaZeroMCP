# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOG = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"


class HTTPStatusError(Exception):
    """Raised for non-retryable 4xx/5xx HTTP responses."""

    def __init__(self, code: int, body: str) -> None:
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code = code
        self.body = body


class TransportError(Exception):
    """Raised when a request fails at the transport layer (connection, DNS, timeout)."""


def _build_headers() -> dict[str, str]:
    hdrs = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "fpgaZeroMCP/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    return hdrs


_HEADERS = _build_headers()

# ---------------------------------------------------------------------------
# Allowed licenses
# Override by setting FPGAZERO_ALLOWED_LICENSES to a comma-separated list of
# SPDX identifiers, e.g.:  FPGAZERO_ALLOWED_LICENSES=MIT,Apache-2.0
# Default: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC,
#          GPL-2.0, GPL-3.0, LGPL-2.1, LGPL-3.0
# ---------------------------------------------------------------------------
_DEFAULT_LICENSES = {
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "ISC",
    "GPL-2.0",
    "GPL-3.0",
    "LGPL-2.1",
    "LGPL-3.0",
}


def _allowed_licenses() -> set[str]:
    raw = os.environ.get("FPGAZERO_ALLOWED_LICENSES", "")
    if raw.strip():
        return {s.strip() for s in raw.split(",") if s.strip()}
    return _DEFAULT_LICENSES


HDL_EXTS = {".v", ".sv", ".vhd", ".vhdl"}
CORE_EXTS = {".core"}

_LANGUAGE_MAP = {
    "verilog": "Verilog",
    "systemverilog": "SystemVerilog",
    "vhdl": "VHDL",
}

_CATEGORY_KEYWORDS = {
    "communication": [
        "uart",
        "spi",
        "i2c",
        "usb",
        "ethernet",
        "can",
        "serial",
        "rs232",
    ],
    "memory": ["fifo", "ram", "rom", "cache", "memory", "sdram", "ddr", "sram"],
    "dsp": ["fft", "dsp", "filter", "fir", "iir", "cordic", "decimat"],
    "cpu": ["cpu", "risc", "riscv", "mips", "processor", "core", "rv32"],
    "video": ["vga", "hdmi", "video", "display", "lcd", "dvi"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _request_text(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 15,
) -> str:
    """HTTP GET returning response body text. Retries transient errors with backoff.

    Raises HTTPStatusError for non-retryable 4xx/5xx, TransportError for
    connection-layer failures.
    """
    hdrs = headers or _HEADERS
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    last_transport_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # HTTPError is a subclass of URLError; status codes land here.
            if exc.code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
                delay = 2**attempt
                LOG.debug("GitHub %d, retrying in %ds", exc.code, delay)
                time.sleep(delay)
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise HTTPStatusError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            last_transport_err = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2**attempt)
                continue
            raise TransportError(str(exc)) from exc
        except TimeoutError as exc:
            last_transport_err = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2**attempt)
                continue
            raise TransportError(f"Request timed out: {exc}") from exc

    # Defensive — should not be reached
    raise TransportError(str(last_transport_err) if last_transport_err else "unknown")


def _get(url: str, params: dict | None = None) -> dict | list:
    return json.loads(_request_text(url, params=params))


def _get_dict(url: str, params: dict | None = None) -> dict:
    """Like _get but asserts the JSON response is a dict."""
    data = _get(url, params=params)
    if not isinstance(data, dict):
        raise ValueError(f"Expected dict from GitHub API, got {type(data).__name__}")
    return data


# Hard cap on tarball downloads — an FPGA IP repo should never approach this.
_MAX_TARBALL_BYTES = 100 * 1024 * 1024


def _request_bytes(
    url: str,
    *,
    headers: dict | None = None,
    timeout: int = 60,
    max_bytes: int = _MAX_TARBALL_BYTES,
) -> bytes:
    """HTTP GET returning raw bytes, aborting past max_bytes."""
    req = urllib.request.Request(url, headers=headers or _HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"Download exceeds {max_bytes // (1024 * 1024)} MB cap"
                )
            chunks.append(chunk)
        return b"".join(chunks)


def _download_tarball_files(
    owner: str, repo: str, ref: str, wanted: list[str]
) -> dict[str, str] | None:
    """Fetch the repo tarball once and extract the wanted paths.

    One HTTP request instead of one per file — faster, atomic, and far
    friendlier to API rate limits. Returns None on any failure so the
    caller can fall back to per-file raw downloads.
    """
    import io
    import tarfile

    url = f"{GITHUB_API}/repos/{owner}/{repo}/tarball/{ref}"
    try:
        data = _request_bytes(url)
        wanted_set = set(wanted)
        out: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                # Strip the tarball's "<owner>-<repo>-<sha>/" root component
                rel = member.name.split("/", 1)[1] if "/" in member.name else ""
                if rel in wanted_set:
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        out[rel] = fobj.read().decode("utf-8", errors="replace")
        return out
    except Exception as exc:
        LOG.info("Tarball download failed (%s); falling back to per-file", exc)
        return None


def _download_raw(owner: str, repo: str, path: str, ref: str) -> str:
    # raw.githubusercontent.com doesn't need auth headers for public repos.
    url = f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{path}"
    return _request_text(
        url,
        headers={"User-Agent": "fpgaZeroMCP/0.1"},
        timeout=30,
    )


def _fetch_tree(owner: str, repo: str, ref: str) -> list[dict]:
    data = _get_dict(
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
    dominant = max(counts, key=counts.__getitem__) if counts else ".v"
    return {
        ".v": "verilog",
        ".sv": "systemverilog",
        ".vhd": "vhdl",
        ".vhdl": "vhdl",
    }.get(dominant, "verilog")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_repos(
    query: str,
    language: str | None = None,
    max_results: int = 10,
) -> list[dict] | dict:
    """Search GitHub for open-source FPGA IP repositories (license checked at import)."""
    q = f"{query} topic:fpga"
    if language and language in _LANGUAGE_MAP:
        q += f" language:{_LANGUAGE_MAP[language]}"

    try:
        data = _get_dict(
            f"{GITHUB_API}/search/repositories",
            params={
                "q": q,
                "sort": "stars",
                "order": "desc",
                "per_page": min(max_results, 30),
            },
        )
    except HTTPStatusError as e:
        return {
            "error": f"GitHub API error {e.code}: {e.body}",
            "error_code": "network_error",
        }
    except TransportError as e:
        return {
            "error": f"GitHub API connection error: {e}",
            "error_code": "network_error",
        }

    results = []
    for item in data.get("items", [])[:max_results]:
        results.append(
            {
                "repo": item["full_name"],
                "description": item.get("description") or "",
                "stars": item["stargazers_count"],
                "license": (item.get("license") or {}).get("spdx_id", "unknown"),
                "topics": item.get("topics", []),
                "default_branch": item.get("default_branch", "main"),
                "url": item["html_url"],
            }
        )
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
        return {
            "error": "repo must be 'owner/repo', e.g. 'ultraembedded/core_uart'",
            "error_code": "invalid_input",
        }
    owner, repo = parts
    subdir_norm = subdir.strip("/")

    # Repo metadata
    try:
        meta = _get_dict(f"{GITHUB_API}/repos/{owner}/{repo}")
    except HTTPStatusError as e:
        return {
            "error": f"GitHub API {e.code}: {e.body}",
            "error_code": "network_error",
        }
    except Exception as e:
        return {"error": str(e), "error_code": "network_error"}

    license_id = (meta.get("license") or {}).get("spdx_id", "unknown")
    allowed = _allowed_licenses()
    if license_id not in allowed:
        return {
            "error": (
                f"Repository license '{license_id}' is not in the allowed list: "
                f"{', '.join(sorted(allowed))}. "
                "Set FPGAZERO_ALLOWED_LICENSES to override."
            ),
            "error_code": "not_allowed",
        }
    if not ref:
        ref = meta.get("default_branch", "main")

    # File tree
    try:
        tree = _fetch_tree(owner, repo, ref)
    except Exception as e:
        return {
            "error": f"Could not fetch file tree: {e}",
            "error_code": "network_error",
        }

    hdl_paths, core_paths = _filter_files(tree, subdir_norm)
    if not hdl_paths:
        scope = f"{owner_repo}/{subdir_norm}" if subdir_norm else owner_repo
        return {
            "error": f"No HDL files (.v/.sv/.vhd) found in {scope}",
            "error_code": "file_not_found",
        }

    # Try FuseSoC .core file for richer metadata
    fuse_manifest: dict | None = None
    if core_paths:
        try:
            raw = _download_raw(owner, repo, core_paths[0], ref)
            from registry.fusesoc import capi2_to_manifest_dict

            fuse_manifest = capi2_to_manifest_dict(
                raw,
                source=f"https://github.com/{owner_repo}",
                license=license_id,
            )
        except Exception:
            pass  # non-fatal

    # Download HDL files (preserve relative paths)
    core_name = repo.lower().replace("-", "_").replace(".", "_")
    dest_dir = dest_parent / core_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    failed: list[dict] = []
    tar_files = _download_tarball_files(owner, repo, ref, hdl_paths)
    for path in hdl_paths:
        rel_path = Path(path)
        if subdir_norm:
            rel_path = rel_path.relative_to(subdir_norm)
        try:
            if tar_files is not None:
                if path not in tar_files:
                    raise KeyError(f"'{path}' missing from tarball")
                content = tar_files[path]
            else:
                content = _download_raw(owner, repo, path, ref)
            target = (dest_dir / rel_path).resolve()
            # Defense in depth: the tree API controls rel_path; never let it
            # write outside the destination directory.
            if not target.is_relative_to(dest_dir.resolve()):
                raise ValueError(f"path escapes destination: {rel_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            downloaded.append(rel_path.as_posix())
        except Exception as e:
            failed.append({"file": rel_path.as_posix(), "error": str(e)})

    if failed and not downloaded:
        # Total failure — clean up and abort
        import shutil

        shutil.rmtree(dest_dir, ignore_errors=True)
        return {
            "error": f"All file downloads failed for {owner_repo}",
            "error_code": "network_error",
            "failed_files": failed,
        }

    if failed:
        # Partial failure -- don't register as a core (no core.json written)
        # Files that did download are kept for manual repair
        return {
            "imported": False,
            "partial": True,
            "core_name": core_name,
            "source": f"https://github.com/{owner_repo}",
            "ref": ref,
            "files_fetched": downloaded,
            "failed_files": failed,
            "error": (
                f"{len(failed)} of {len(hdl_paths)} file(s) failed to download. "
                f"Core not registered. Downloaded files kept in {dest_dir} for manual repair."
            ),
            "error_code": "network_error",
        }

    # Build manifest (FuseSoC wins if available)
    description = meta.get("description") or f"Imported from github.com/{owner_repo}"
    topics = meta.get("topics", [])

    manifest = fuse_manifest or {
        "name": core_name,
        "version": ref,
        "description": description,
        "author": owner,
        "license": license_id,
        "language": _dominant_language(hdl_paths),
        "category": _infer_category(" ".join(topics) + " " + repo + " " + description),
        "tags": topics[:8],
        "parameters": {},
        "ports": {},
        "files": downloaded,
    }

    # Always update files list to what was actually downloaded
    manifest["files"] = downloaded
    manifest["source"] = f"https://github.com/{owner_repo}"

    (dest_dir / "core.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    return {
        "imported": True,
        "core_name": core_name,
        "source": f"https://github.com/{owner_repo}",
        "ref": ref,
        "license": license_id,
        "files_fetched": downloaded,
        "fusesoc_used": fuse_manifest is not None,
        "manifest": manifest,
    }
