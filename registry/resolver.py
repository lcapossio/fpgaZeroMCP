# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from registry.manifest import CoreManifest

CORES_DIR   = Path(__file__).parent.parent / "cores"
CONFIG_FILE = Path.home() / ".fpgazero_mcp" / "config.json"
LOG = logging.getLogger(__name__)


def _extra_core_paths() -> list[Path]:
    """Collect extra core search paths from env var and config file.

    USERCORES_PATH  — os.pathsep-separated list of directories
    ~/.fpgazero_mcp/config.json — {"core_paths": ["/path/a", "/path/b"]}
    """
    paths: list[Path] = []

    # Environment variable (highest priority)
    env = os.environ.get("USERCORES_PATH", "")
    for p in env.split(os.pathsep):
        p = p.strip()
        if p:
            paths.append(Path(p))

    # Config file
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for p in cfg.get("core_paths", []):
                paths.append(Path(p))
        except Exception as exc:
            LOG.warning("Could not read %s: %s", CONFIG_FILE, exc)

    return paths


class CoreRegistry:
    def __init__(self, cores_dir: Path = CORES_DIR) -> None:
        self._dir   = cores_dir          # built-in / import destination
        self._cache: dict[str, tuple[CoreManifest, Path]] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_dir(self, directory: Path) -> None:
        """Scan one directory for core.json manifests and add them to cache."""
        if not directory.exists():
            return
        for core_dir in sorted(directory.iterdir()):
            manifest_path = core_dir / "core.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = CoreManifest.model_validate(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
                # Later paths override earlier ones for the same name
                self._cache[manifest.name] = (manifest, core_dir)
            except Exception as exc:
                LOG.warning("Could not load %s: %s", manifest_path, exc)

    def _load_all(self) -> None:
        self._cache.clear()
        # Built-in cores first, then extra paths (extra paths win on name clash)
        self._load_dir(self._dir)
        for extra in _extra_core_paths():
            self._load_dir(extra)

    def _manifest_dict(self, manifest: CoreManifest) -> dict:
        return manifest.model_dump()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_cores(self, category: str | None = None) -> list[dict]:
        """Return a summary list of all available IP cores, optionally filtered by category."""
        results = []
        for name, (manifest, core_dir) in self._cache.items():
            if category and manifest.category != category:
                continue
            results.append({
                "name":       manifest.name,
                "version":    manifest.version,
                "category":   manifest.category,
                "description":manifest.description,
                "tags":       manifest.tags,
                "language":   manifest.language,
                "parameters": list(manifest.parameters.keys()),
                "path":       str(core_dir),
            })
        return results

    def get_core(self, name: str) -> dict:
        """Return the full manifest and HDL source for a named core."""
        if name not in self._cache:
            return {"error": f"Core '{name}' not found. Use list_ip_cores to see available cores."}

        manifest, core_dir = self._cache[name]
        files: dict[str, str] = {}
        for filename in manifest.files:
            path = core_dir / filename
            if path.exists():
                files[filename] = path.read_text(encoding="utf-8")
            else:
                files[filename] = f"// ERROR: file '{filename}' not found in registry"

        return {
            "manifest": self._manifest_dict(manifest),
            "files":    files,
        }

    def import_github_core(
        self,
        owner_repo: str,
        subdir: str = "",
        ref: str = "",
    ) -> dict:
        """Download a GitHub repo and register it as a local core."""
        from registry.github import import_core
        result = import_core(
            owner_repo=owner_repo,
            subdir=subdir,
            ref=ref,
            dest_parent=self._dir,
        )
        if result.get("imported"):
            self._load_all()
        return result

    def import_fusesoc_core(self, path: str) -> dict:
        """Import a local CAPI2 .core file. HDL files must be in the same directory."""
        from registry.fusesoc import load_core_file

        src = Path(path)
        if not src.exists():
            return {"error": f"File not found: {path}"}

        manifest_dict = load_core_file(src)
        if not manifest_dict:
            return {"error": f"Could not parse CAPI2 .core file: {path}"}

        core_name = manifest_dict["name"]
        dest_dir  = self._dir / core_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        for fname in manifest_dict.get("files", []):
            rel = Path(fname)
            if rel.is_absolute() or rel.drive or ".." in rel.parts:
                return {"error": f"Unsafe file path in core file: {fname}"}
            src_hdl = (src.parent / rel).resolve()
            if not src_hdl.is_relative_to(src.parent.resolve()):
                return {"error": f"Unsafe source path in core file: {fname}"}
            if src_hdl.exists():
                dest_file = (dest_dir / rel).resolve()
                if not dest_file.is_relative_to(dest_dir.resolve()):
                    return {"error": f"Unsafe destination path in core file: {fname}"}
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_text(
                    src_hdl.read_text(encoding="utf-8"), encoding="utf-8"
                )
                copied.append(rel.as_posix())

        manifest_dict["files"] = copied or manifest_dict.get("files", [])
        (dest_dir / "core.json").write_text(
            json.dumps(manifest_dict, indent=2), encoding="utf-8"
        )

        self._load_all()
        return {
            "imported":    True,
            "core_name":   core_name,
            "files_copied": copied,
            "manifest":    manifest_dict,
        }

    def generate_ip(
        self,
        name: str,
        parameters: dict | None = None,
        instance_name: str | None = None,
    ) -> dict:
        """Return core HDL files plus a Verilog instantiation snippet with the given parameters."""
        core = self.get_core(name)
        if "error" in core:
            return core

        manifest_data = core["manifest"]
        params = {**{k: v["default"] for k, v in manifest_data["parameters"].items()}, **(parameters or {})}
        inst   = instance_name or f"{name}_inst"

        param_lines = [f"        .{k}({v})" for k, v in params.items()]
        param_block = ""
        if param_lines:
            param_block = " #(\n" + ",\n".join(param_lines) + "\n    )"

        port_lines = [f"        .{port}({port})" for port in manifest_data["ports"]]
        port_block = "\n" + ",\n".join(port_lines) + "\n    "

        snippet = f"    {name}{param_block} {inst} ({port_block});\n"

        return {
            "manifest":        manifest_data,
            "parameters_used": params,
            "instantiation":   snippet,
            "files":           core["files"],
        }
