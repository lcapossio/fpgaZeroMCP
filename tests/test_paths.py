# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests that runtime state never lands inside the installed package.

The package installs into site-packages, which is the wrong place -- and often
not writable -- for build logs, bitstreams, scratch workspaces and cores
imported at runtime. These tests pin that down so a future __file__-relative
default cannot creep back in.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

import fpgazero_mcp
from fpgazero_mcp.paths import data_root, user_cores_dir
from fpgazero_mcp.registry.resolver import BUILTIN_CORES_DIR, CoreRegistry

PACKAGE_DIR = Path(fpgazero_mcp.__file__).resolve().parent


@pytest.fixture
def scratch_dir():
    """Temp directory fixture. pytest.ini disables `scratch_dir` (-p no:tmpdir)."""
    path = tempfile.mkdtemp(prefix="pytest_paths_")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestDataRoot:
    def test_default_is_outside_the_package(self, monkeypatch) -> None:
        monkeypatch.delenv("FPGAZERO_DATA_DIR", raising=False)
        assert not data_root().resolve().is_relative_to(PACKAGE_DIR)

    def test_default_is_under_home(self, monkeypatch) -> None:
        monkeypatch.delenv("FPGAZERO_DATA_DIR", raising=False)
        assert data_root().resolve().is_relative_to(Path.home().resolve())

    def test_env_override(self, monkeypatch, scratch_dir) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        assert data_root() == scratch_dir

    def test_blank_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", "   ")
        assert data_root().resolve().is_relative_to(Path.home().resolve())

    def test_user_cores_dir_follows_data_root(self, monkeypatch, scratch_dir) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        assert user_cores_dir() == scratch_dir / "cores"
        assert not user_cores_dir().is_relative_to(PACKAGE_DIR)


class TestWorkspaceRoots:
    def test_scratch_workspace_is_outside_the_package(
        self, monkeypatch, scratch_dir
    ) -> None:
        from fpgazero_mcp.tools.workspace import temporary_workspace

        monkeypatch.delenv("FPGAZERO_TMPDIR", raising=False)
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        with temporary_workspace("pathtest_") as tmp:
            assert not Path(tmp).resolve().is_relative_to(PACKAGE_DIR)


class TestRegistryDirs:
    def test_default_searches_builtin_and_user_dirs(
        self, monkeypatch, scratch_dir
    ) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        monkeypatch.delenv("USERCORES_PATH", raising=False)
        reg = CoreRegistry()
        assert reg._search_dirs == [BUILTIN_CORES_DIR, scratch_dir / "cores"]

    def test_default_import_destination_is_writable_not_the_package(
        self, monkeypatch, scratch_dir
    ) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        reg = CoreRegistry()
        assert reg._dir == scratch_dir / "cores"
        assert not reg._dir.is_relative_to(PACKAGE_DIR)

    def test_bundled_cores_are_still_discovered_by_default(
        self, monkeypatch, scratch_dir
    ) -> None:
        monkeypatch.setenv("FPGAZERO_DATA_DIR", str(scratch_dir))
        monkeypatch.delenv("USERCORES_PATH", raising=False)
        names = {c["name"] for c in CoreRegistry().list_cores()}
        # uart_tx and fifo ship inside the package.
        assert "uart_tx" in names, names

    def test_explicit_cores_dir_keeps_single_directory_semantics(
        self, monkeypatch, scratch_dir
    ) -> None:
        """An explicit cores_dir must not also pull in the bundled cores."""
        monkeypatch.delenv("USERCORES_PATH", raising=False)
        reg = CoreRegistry(cores_dir=scratch_dir)
        assert reg._search_dirs == [scratch_dir]
        assert reg._dir == scratch_dir
        assert reg.list_cores() == []
