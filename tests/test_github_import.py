# SPDX-FileCopyrightText: 2025 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
import json
import shutil
from pathlib import Path
from uuid import uuid4

import registry.github as gh


def _mk_tmp_dir() -> Path:
    base = Path("no_commit") / "pytest_tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"gh_{uuid4().hex}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    return d


def test_import_core_rejects_non_mit(monkeypatch) -> None:
    def fake_get(url: str, params: dict | None = None):
        assert url.endswith("/repos/owner/repo")
        return {
            "license": {"spdx_id": "Apache-2.0"},
            "default_branch": "main",
            "description": "x",
            "topics": [],
        }

    monkeypatch.setattr(gh, "_get", fake_get)

    tmp_path = _mk_tmp_dir()
    result = gh.import_core("owner/repo", dest_parent=tmp_path)
    assert "error" in result
    assert "not MIT" in result["error"]


def test_import_core_preserves_paths(monkeypatch) -> None:
    def fake_get(url: str, params: dict | None = None):
        assert url.endswith("/repos/owner/repo")
        return {
            "license": {"spdx_id": "MIT"},
            "default_branch": "main",
            "description": "x",
            "topics": ["uart"],
        }

    def fake_fetch_tree(owner: str, repo: str, ref: str):
        return [
            {"path": "rtl/uart_rx.v", "type": "blob"},
            {"path": "rtl/sub/uart_tx.v", "type": "blob"},
            {"path": "docs/readme.md", "type": "blob"},
        ]

    def fake_download(owner: str, repo: str, path: str, ref: str):
        return f"// {path}\n"

    monkeypatch.setattr(gh, "_get", fake_get)
    monkeypatch.setattr(gh, "_fetch_tree", fake_fetch_tree)
    monkeypatch.setattr(gh, "_download_raw", fake_download)

    tmp_path = _mk_tmp_dir()
    result = gh.import_core("owner/repo", subdir="rtl", dest_parent=tmp_path)
    assert result.get("imported") is True
    files = result["files_fetched"]
    assert files == ["uart_rx.v", "sub/uart_tx.v"]

    core_dir = tmp_path / "repo"
    assert (core_dir / "uart_rx.v").exists()
    assert (core_dir / "sub" / "uart_tx.v").exists()

    manifest_path = core_dir / "core.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["files"] == ["uart_rx.v", "sub/uart_tx.v"]
