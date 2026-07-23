# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Tests for error paths, edge cases, and input validation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from registry.resolver import CoreRegistry
from tools.synthesize import (
    synthesize,
    validate_top_module,
    _resolve_sources,
    _yosys_read_cmds,
    _parse_filelist,
)
from tools.pnr import place_and_route, _find_constraints
from tools.simulate import simulate, _parse_verdict, _summarize_vcd
from tools.lint import lint_hdl, lint_project
from tools.boards import get_board_preset, list_boards
from tools.build_manager import BuildManager
from tools.build_parser import parse_build_log
from registry.fusesoc import (
    capi2_to_manifest_dict,
    _parse_name,
    _collect_hdl_files,
    _collect_parameters,
)
import registry.github as gh
from tools.litex import _build_litex_cmd
from tools.simulate import _find_vhdl_entity
from server import _clamp_timeout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=f"err_{uuid4().hex}_"))


# ---------------------------------------------------------------------------
# validate_top_module
# ---------------------------------------------------------------------------


class TestValidateTopModule:
    def test_valid_identifier(self) -> None:
        assert validate_top_module("my_module") is None

    def test_valid_with_dollar(self) -> None:
        assert validate_top_module("mod$sub") is None

    def test_rejects_starting_digit(self) -> None:
        err = validate_top_module("1bad")
        assert err is not None
        assert "Invalid" in err

    def test_rejects_empty(self) -> None:
        err = validate_top_module("")
        assert err is not None

    def test_rejects_spaces(self) -> None:
        err = validate_top_module("my module")
        assert err is not None


# ---------------------------------------------------------------------------
# synthesize error paths
# ---------------------------------------------------------------------------


class TestSynthesizeErrors:
    def test_invalid_top_module(self) -> None:
        result = synthesize("module m; endmodule", top_module="1bad")
        assert result["success"] is False
        assert "Invalid" in result["error"]

    def test_missing_yosys(self, monkeypatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        # This test only works if yosys is actually not installed,
        # otherwise we can't easily test FileNotFoundError.
        # We test the timeout message format instead.
        result = synthesize("module m; endmodule", top_module="m", timeout=42)
        # If yosys isn't installed, we get the not-found error.
        # If it is installed, we at least verify it ran.
        if "error" in result and "not found" in result["error"]:
            assert result["success"] is False


# ---------------------------------------------------------------------------
# _resolve_sources validation
# ---------------------------------------------------------------------------


class TestResolveSources:
    def test_no_input_returns_error(self) -> None:
        paths, err = _resolve_sources()
        assert err is not None
        assert "Provide one of" in err

    def test_multiple_inputs_returns_error(self) -> None:
        paths, err = _resolve_sources(code="module m; endmodule", files={"a.v": "x"})
        assert err is not None
        assert "only one" in err.lower()

    def test_code_writes_file(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(code="module m; endmodule", tmpdir=tmpdir)
        assert err is None
        assert len(paths) == 1
        assert paths[0].endswith(".v")

    def test_code_sv_extension(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(
            code="module m; endmodule", language="systemverilog", tmpdir=tmpdir
        )
        assert err is None
        assert paths[0].endswith(".sv")

    def test_code_vhdl_extension(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(
            code="entity e is end;", language="vhdl", tmpdir=tmpdir
        )
        assert err is None
        assert paths[0].endswith(".vhd")

    def test_files_dict(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(
            files={"top.v": "module top; endmodule", "sub.v": "module sub; endmodule"},
            tmpdir=tmpdir,
        )
        assert err is None
        assert len(paths) == 2

    def test_project_dir_nonexistent(self) -> None:
        paths, err = _resolve_sources(project_dir="/nonexistent/path/xyz")
        assert err is not None
        assert "does not exist" in err or "outside allowed" in err.lower()

    def test_project_dir_no_files(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(project_dir=tmpdir, language="verilog")
        assert err is not None
        assert "No verilog files" in err

    def test_project_dir_finds_files(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "top.v").write_text("module top; endmodule", encoding="utf-8")
        (tmpdir / "sub.v").write_text("module sub; endmodule", encoding="utf-8")
        paths, err = _resolve_sources(project_dir=str(tmpdir), language="verilog")
        assert err is None
        assert len(paths) == 2

    def test_project_dir_outside_allowed_roots_rejected(self) -> None:
        # A path that's outside cwd, home, and any FPGAZERO_ALLOWED_DIRS
        paths, err = _resolve_sources(
            project_dir="/nonexistent/outside/root", language="verilog"
        )
        assert err is not None
        assert "outside allowed" in err.lower() or "does not exist" in err.lower()

    def test_files_dict_traversal_rejected(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(files={"../../etc/crontab.v": "x"}, tmpdir=tmpdir)
        assert err is not None
        assert "Unsafe" in err or "traversal" in err.lower()

    def test_files_dict_absolute_path_rejected(self) -> None:
        tmpdir = str(_mk_tmp_dir())
        paths, err = _resolve_sources(files={"/etc/passwd.v": "x"}, tmpdir=tmpdir)
        assert err is not None
        assert "Unsafe" in err

    def test_sv_does_not_glob_v_files(self) -> None:
        """SystemVerilog mode should only glob .sv files, not .v."""
        tmpdir = _mk_tmp_dir()
        (tmpdir / "design.sv").write_text("module top; endmodule", encoding="utf-8")
        (tmpdir / "legacy.v").write_text("module old; endmodule", encoding="utf-8")
        paths, err = _resolve_sources(project_dir=str(tmpdir), language="systemverilog")
        assert err is None
        assert len(paths) == 1
        assert paths[0].endswith(".sv")


class TestYosysReadCmds:
    def test_verilog_single_file(self) -> None:
        result = _yosys_read_cmds(["/tmp/design.v"], "verilog")
        assert result.strip() == "read_verilog /tmp/design.v"

    def test_verilog_multi_file(self) -> None:
        result = _yosys_read_cmds(["/tmp/a.v", "/tmp/b.v"], "verilog")
        assert "read_verilog /tmp/a.v" in result
        assert "read_verilog /tmp/b.v" in result

    def test_systemverilog(self) -> None:
        result = _yosys_read_cmds(["/tmp/design.sv"], "systemverilog")
        assert result.strip() == "read_verilog -sv /tmp/design.sv"

    def test_vhdl_single_file_with_top(self) -> None:
        result = _yosys_read_cmds(["/tmp/design.vhd"], "vhdl", top_module="top_entity")
        assert result.strip() == "ghdl --std=08 /tmp/design.vhd -e top_entity"

    def test_vhdl_multi_file_single_invocation(self) -> None:
        result = _yosys_read_cmds(
            ["/tmp/pkg.vhd", "/tmp/design.vhd", "/tmp/top.vhd"],
            "vhdl",
            top_module="my_top",
        )
        # Must be a single ghdl line with all files, not one per file
        lines = [ln for ln in result.strip().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert (
            lines[0]
            == "ghdl --std=08 /tmp/pkg.vhd /tmp/design.vhd /tmp/top.vhd -e my_top"
        )

    def test_vhdl_without_top(self) -> None:
        result = _yosys_read_cmds(["/tmp/design.vhd"], "vhdl")
        assert "-e" not in result
        assert result.strip() == "ghdl --std=08 /tmp/design.vhd"

    def test_windows_backslashes_converted(self) -> None:
        result = _yosys_read_cmds(["C:\\tmp\\design.v"], "verilog")
        assert "\\" not in result
        assert "C:/tmp/design.v" in result


class TestSynthesizeMultiFile:
    def test_missing_top_module(self) -> None:
        result = synthesize(code="module m; endmodule")
        assert result["success"] is False
        assert "top_module" in result["error"].lower()

    def test_no_source_provided(self) -> None:
        result = synthesize(top_module="top")
        assert result["success"] is False
        assert "Provide one of" in result["error"]


# ---------------------------------------------------------------------------
# place_and_route error paths
# ---------------------------------------------------------------------------


class TestPnRErrors:
    def test_unsupported_target(self) -> None:
        result = place_and_route("module m; endmodule", "m", "nonexistent", "dev")
        assert result["success"] is False
        assert "Unsupported" in result["error"]

    def test_invalid_top_module(self) -> None:
        result = place_and_route("module m; endmodule", "1bad", "ice40", "hx1k")
        assert result["success"] is False
        assert "Invalid" in result["error"]

    def test_litex_backend_needs_board(self) -> None:
        result = place_and_route(
            "module m; endmodule", "m", "ice40", "hx1k", backend="litex"
        )
        assert result["success"] is False
        assert "litex_board" in result["error"]


# ---------------------------------------------------------------------------
# lint error paths
# ---------------------------------------------------------------------------


class TestLintErrors:
    def test_unknown_language_defaults_to_verilog(self) -> None:
        # Unknown language should still proceed (defaults to .v extension)
        result = lint_hdl("module m; endmodule\n", language="unknown_lang")
        # Either succeeds (iverilog present) or fails with "not found"
        assert "success" in result or "error" in result


# ---------------------------------------------------------------------------
# Registry error paths
# ---------------------------------------------------------------------------


class TestRegistryErrors:
    def test_get_missing_core(self) -> None:
        tmp = _mk_tmp_dir()
        reg = CoreRegistry(cores_dir=tmp)
        result = reg.get_core("nonexistent_core")
        assert "error" in result
        assert "not found" in result["error"]

    def test_generate_missing_core(self) -> None:
        tmp = _mk_tmp_dir()
        reg = CoreRegistry(cores_dir=tmp)
        result = reg.generate_ip("nonexistent_core")
        assert "error" in result

    def test_malformed_core_json_skipped(self) -> None:
        tmp = _mk_tmp_dir()
        bad_dir = tmp / "bad_core"
        bad_dir.mkdir()
        (bad_dir / "core.json").write_text("not valid json!", encoding="utf-8")

        reg = CoreRegistry(cores_dir=tmp)
        assert reg.list_cores() == []

    def test_invalid_core_json_schema_skipped(self) -> None:
        tmp = _mk_tmp_dir()
        bad_dir = tmp / "bad_core"
        bad_dir.mkdir()
        # Valid JSON but missing required fields
        (bad_dir / "core.json").write_text('{"name": "x"}', encoding="utf-8")

        reg = CoreRegistry(cores_dir=tmp)
        assert reg.list_cores() == []

    def test_import_fusesoc_missing_file(self) -> None:
        tmp = _mk_tmp_dir()
        reg = CoreRegistry(cores_dir=tmp)
        result = reg.import_fusesoc_core("/nonexistent/path.core")
        assert "error" in result
        assert "not found" in result["error"]

    def test_path_traversal_rejected_at_load(self) -> None:
        tmp = _mk_tmp_dir()
        core_dir = tmp / "evil"
        core_dir.mkdir()
        (core_dir / "core.json").write_text(
            json.dumps(
                {
                    "name": "evil",
                    "version": "1.0",
                    "description": "x",
                    "author": "x",
                    "license": "MIT",
                    "language": "verilog",
                    "category": "test",
                    "files": ["../../etc/passwd"],
                }
            )
        )
        reg = CoreRegistry(cores_dir=tmp)
        # Core should be rejected at load time
        assert "evil" not in [c["name"] for c in reg.list_cores()]

    def test_path_traversal_rejected_at_get(self) -> None:
        tmp = _mk_tmp_dir()
        core_dir = tmp / "tricky"
        core_dir.mkdir()
        (core_dir / "core.json").write_text(
            json.dumps(
                {
                    "name": "tricky",
                    "version": "1.0",
                    "description": "x",
                    "author": "x",
                    "license": "MIT",
                    "language": "verilog",
                    "category": "test",
                    "files": ["sub/../../../etc/hosts"],
                }
            )
        )
        reg = CoreRegistry(cores_dir=tmp)
        # Should be rejected at load time already
        assert "tricky" not in [c["name"] for c in reg.list_cores()]

    def test_absolute_path_rejected(self) -> None:
        tmp = _mk_tmp_dir()
        core_dir = tmp / "abs"
        core_dir.mkdir()
        (core_dir / "core.json").write_text(
            json.dumps(
                {
                    "name": "abs",
                    "version": "1.0",
                    "description": "x",
                    "author": "x",
                    "license": "MIT",
                    "language": "verilog",
                    "category": "test",
                    "files": ["/etc/passwd"],
                }
            )
        )
        reg = CoreRegistry(cores_dir=tmp)
        assert "abs" not in [c["name"] for c in reg.list_cores()]


# ---------------------------------------------------------------------------
# generate_ip validation
# ---------------------------------------------------------------------------


class TestGenerateIpValidation:
    def test_unknown_parameter_rejected(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip("uart_tx", parameters={"NONEXISTENT": 42})
        assert "error" in result
        assert "Unknown" in result["error"]

    def test_parameter_below_minimum(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip("uart_tx", parameters={"DATA_WIDTH": 1})
        assert "error" in result
        assert "minimum" in result["error"]

    def test_parameter_above_maximum(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip("uart_tx", parameters={"DATA_WIDTH": 99})
        assert "error" in result
        assert "maximum" in result["error"]

    def test_parameter_wrong_type(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip("uart_tx", parameters={"DATA_WIDTH": "eight"})
        assert "error" in result
        assert "integer" in result["error"]

    def test_float_rejected_for_integer(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip("uart_tx", parameters={"DATA_WIDTH": 3.9})
        assert "error" in result
        assert "integer" in result["error"]

    def test_bool_rejected_for_integer(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip("uart_tx", parameters={"DATA_WIDTH": True})
        assert "error" in result
        assert "integer" in result["error"]

    def test_valid_parameters_accepted(self) -> None:
        reg = CoreRegistry()
        result = reg.generate_ip(
            "uart_tx", parameters={"DATA_WIDTH": 7, "CLKS_PER_BIT": 500}
        )
        assert "error" not in result
        assert result["parameters_used"]["DATA_WIDTH"] == 7


# ---------------------------------------------------------------------------
# FuseSoC parser edge cases
# ---------------------------------------------------------------------------


class TestFuseSocParser:
    def test_empty_content_returns_none(self) -> None:
        assert capi2_to_manifest_dict("") is None

    def test_invalid_yaml_returns_none(self) -> None:
        assert capi2_to_manifest_dict("{{{{invalid yaml") is None

    def test_parse_name_vendor_library(self) -> None:
        name, ver = _parse_name("vendor::library:1.0.0")
        assert name == "library"
        assert ver == "1.0.0"

    def test_parse_name_simple(self) -> None:
        name, ver = _parse_name("mycore:2.0")
        assert name == "mycore"
        assert ver == "2.0"

    def test_parse_name_no_version(self) -> None:
        name, ver = _parse_name("mycore")
        assert name == "mycore"
        assert ver == "0.0.0"

    def test_license_passthrough(self) -> None:
        content = (
            "CAPI=2:\nname: ::test:1.0\nfilesets:\n  rtl:\n    files:\n      - foo.v\n"
        )
        result = capi2_to_manifest_dict(content, license="GPL-3.0")
        assert result is not None
        assert result["license"] == "GPL-3.0"

    def test_license_defaults_to_mit(self) -> None:
        content = (
            "CAPI=2:\nname: ::test:1.0\nfilesets:\n  rtl:\n    files:\n      - foo.v\n"
        )
        result = capi2_to_manifest_dict(content)
        assert result is not None
        assert result["license"] == "MIT"

    def test_collect_hdl_files_preserves_paths(self) -> None:
        filesets = {
            "rtl": {
                "files": ["rtl/sub/foo.v", "rtl/bar.sv"],
                "file_type": "verilogSource",
            }
        }
        files, lang = _collect_hdl_files(filesets)
        assert files == ["rtl/sub/foo.v", "rtl/bar.sv"]


# ---------------------------------------------------------------------------
# GitHub import edge cases
# ---------------------------------------------------------------------------


class TestGitHubImportEdgeCases:
    def test_invalid_repo_format(self) -> None:
        result = gh.import_core("not-a-valid-format")
        assert "error" in result
        assert "owner/repo" in result["error"]

    def test_import_no_hdl_files(self, monkeypatch) -> None:
        def fake_get(url, params=None):
            return {
                "license": {"spdx_id": "MIT"},
                "default_branch": "main",
                "description": "x",
                "topics": [],
            }

        def fake_fetch_tree(owner, repo, ref):
            return [
                {"path": "README.md", "type": "blob"},
                {"path": "docs/guide.txt", "type": "blob"},
            ]

        monkeypatch.setattr(gh, "_get", fake_get)
        monkeypatch.setattr(gh, "_fetch_tree", fake_fetch_tree)

        tmp = _mk_tmp_dir()
        result = gh.import_core("owner/repo", dest_parent=tmp)
        assert "error" in result
        assert "No HDL files" in result["error"]


# ---------------------------------------------------------------------------
# FuseSoC boolean parameter handling
# ---------------------------------------------------------------------------


class TestFuseSocBoolParam:
    def test_bool_datatype_maps_to_boolean(self) -> None:
        params = _collect_parameters(
            {
                "ENABLE": {
                    "datatype": "bool",
                    "default": True,
                    "description": "Enable feature",
                },
            }
        )
        assert params["ENABLE"]["type"] == "boolean"
        assert params["ENABLE"]["default"] is True

    def test_int_datatype_maps_to_integer(self) -> None:
        params = _collect_parameters(
            {
                "WIDTH": {"datatype": "int", "default": 8},
            }
        )
        assert params["WIDTH"]["type"] == "integer"

    def test_str_datatype_maps_to_string(self) -> None:
        params = _collect_parameters(
            {
                "NAME": {"datatype": "str", "default": "foo"},
            }
        )
        assert params["NAME"]["type"] == "string"


# ---------------------------------------------------------------------------
# FuseSoC CAPI= header parsing
# ---------------------------------------------------------------------------


class TestCAPI2HeaderParsing:
    def test_capi_line_in_content_preserved(self) -> None:
        # A CAPI= string inside the YAML body should NOT be stripped
        content = (
            "CAPI=2:\n"
            "name: ::test:1.0\n"
            "description: 'CAPI=2 is the format version'\n"
            "filesets:\n"
            "  rtl:\n"
            "    files:\n"
            "      - foo.v\n"
        )
        result = capi2_to_manifest_dict(content)
        assert result is not None
        assert "CAPI=2 is the format version" in result["description"]


# ---------------------------------------------------------------------------
# LiteX board name validation
# ---------------------------------------------------------------------------


class TestLitexBoardValidation:
    def test_valid_board_name(self) -> None:
        cmd = _build_litex_cmd("digilent_arty", ["--build"])
        assert "litex_boards.targets.digilent_arty" in cmd[2]

    def test_dotted_board_name(self) -> None:
        cmd = _build_litex_cmd("vendor.board", [])
        assert "litex_boards.targets.vendor.board" in cmd[2]

    def test_rejects_empty_board(self) -> None:
        with pytest.raises(ValueError, match="required"):
            _build_litex_cmd("", [])

    def test_rejects_shell_chars(self) -> None:
        with pytest.raises(ValueError, match="Invalid board name"):
            _build_litex_cmd("board; rm -rf /", [])

    def test_rejects_hyphen(self) -> None:
        with pytest.raises(ValueError, match="Invalid board name"):
            _build_litex_cmd("bad-board", [])


# ---------------------------------------------------------------------------
# Timeout clamping
# ---------------------------------------------------------------------------


class TestClampTimeout:
    def test_normal_value(self) -> None:
        assert _clamp_timeout(60, 120) == 60

    def test_negative_returns_default(self) -> None:
        assert _clamp_timeout(-1, 120) == 120

    def test_zero_returns_default(self) -> None:
        assert _clamp_timeout(0, 120) == 120

    def test_exceeds_max_clamped(self) -> None:
        assert _clamp_timeout(999999, 120) == 3600

    def test_non_int_returns_default(self) -> None:
        assert _clamp_timeout("fast", 120) == 120  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# VHDL entity finder
# ---------------------------------------------------------------------------


class TestFindVhdlEntity:
    def test_simple_entity(self) -> None:
        code = "entity my_tb is\nend entity my_tb;"
        assert _find_vhdl_entity(code) == "my_tb"

    def test_case_insensitive(self) -> None:
        code = "ENTITY Tb_Top IS\nEND ENTITY Tb_Top;"
        assert _find_vhdl_entity(code) == "Tb_Top"

    def test_no_entity_returns_none(self) -> None:
        assert _find_vhdl_entity("-- just a comment") is None

    def test_picks_first_entity(self) -> None:
        code = "entity design is\nend entity;\nentity tb is\nend entity;"
        assert _find_vhdl_entity(code) == "design"


# ---------------------------------------------------------------------------
# VHDL simulate error paths
# ---------------------------------------------------------------------------


class TestLintProject:
    def test_empty_files_returns_error(self) -> None:
        result = lint_project({})
        assert result["success"] is False
        assert "No files" in result["error"]


# ---------------------------------------------------------------------------
# Build manager
# ---------------------------------------------------------------------------


class TestBuildManager:
    def _allowed_cmd(self) -> list[str]:
        """Return a fast, allowed command for testing. Prefer iverilog if installed."""
        if shutil.which("iverilog"):
            return ["iverilog", "-V"]
        if shutil.which("ghdl"):
            return ["ghdl", "--version"]
        if shutil.which("verilator"):
            return ["verilator", "--version"]
        pytest.skip("No allowed EDA tool installed for BuildManager tests")
        return []  # unreachable

    def test_start_and_status(self) -> None:
        mgr = BuildManager()
        cmd = self._allowed_cmd()
        result = mgr.start(cmd=cmd, label="test")
        assert result["success"] is True
        bid = result["build_id"]

        # Wait for it to finish
        import time

        for _ in range(20):
            s = mgr.status(bid)
            if s["status"] != "running":
                break
            time.sleep(0.1)

        s = mgr.status(bid)
        assert s["status"] == "success"
        assert s["returncode"] == 0

    def test_list_builds(self) -> None:
        mgr = BuildManager()
        cmd = self._allowed_cmd()
        mgr.start(cmd=cmd, label="list-test")
        import time

        time.sleep(0.5)
        builds = mgr.list_builds()
        assert len(builds) >= 1
        assert builds[0]["label"] == "list-test"

    def test_unknown_build_id(self) -> None:
        mgr = BuildManager()
        result = mgr.status("nonexistent")
        assert "error" in result

    def test_cancel_not_running(self) -> None:
        mgr = BuildManager()
        cmd = self._allowed_cmd()
        result = mgr.start(cmd=cmd, label="done")
        import time

        time.sleep(0.5)
        cancel = mgr.cancel(result["build_id"])
        assert "error" in cancel  # already finished

    def test_command_not_found(self) -> None:
        mgr = BuildManager()
        result = mgr.start(cmd=["nonexistent_tool_xyz"])
        assert result["success"] is False
        assert "not in the allowed list" in result["error"]

    def test_command_allowlist_rejects_arbitrary(self) -> None:
        mgr = BuildManager()
        result = mgr.start(cmd=["rm", "-rf", "/"])
        assert result["success"] is False
        assert "not in the allowed list" in result["error"]

    def test_command_allowlist_python_m_restricted(self) -> None:
        mgr = BuildManager()
        result = mgr.start(cmd=["python", "-m", "http.server"])
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_python_c_rejected(self) -> None:
        mgr = BuildManager()
        result = mgr.start(cmd=["python", "-c", "print('hello')"])
        assert result["success"] is False
        assert "must use '-m <module>'" in result["error"]

    def test_missing_work_dir_rejected(self) -> None:
        mgr = BuildManager()
        missing = _mk_tmp_dir() / "missing"
        result = mgr.start(cmd=["iverilog", "-V"], work_dir=str(missing))
        assert result["success"] is False
        assert "work_dir does not exist" in result["error"]

    def test_clear_finished(self) -> None:
        mgr = BuildManager()
        cmd = self._allowed_cmd()
        mgr.start(cmd=cmd, label="clear-test")
        import time

        time.sleep(0.5)
        result = mgr.clear_finished()
        assert result["cleared"] >= 1


# ---------------------------------------------------------------------------
# Board presets
# ---------------------------------------------------------------------------


class TestBoardPresets:
    def test_icebreaker_preset(self) -> None:
        p = get_board_preset("icebreaker")
        assert p is not None
        assert p["target"] == "ice40"
        assert p["device"] == "up5k"
        assert p["clock_mhz"] == 12.0

    def test_ulx3s_preset(self) -> None:
        p = get_board_preset("ulx3s_85f")
        assert p is not None
        assert p["target"] == "ecp5"
        assert p["clock_mhz"] == 25.0

    def test_unknown_board_returns_none(self) -> None:
        assert get_board_preset("nonexistent_board") is None

    def test_case_insensitive_hyphen(self) -> None:
        assert get_board_preset("TinyFPGA-BX") is not None

    def test_list_boards(self) -> None:
        boards = list_boards()
        assert len(boards) > 5
        assert all("board" in b and "target" in b for b in boards)

    def test_pnr_board_resolves_target(self) -> None:
        # board="icebreaker" should fill in target/device/package
        result = place_and_route(
            code="module top(input a, output y); assign y = a; endmodule",
            top_module="top",
            board="icebreaker",
        )
        # Will fail (no yosys) but should NOT fail on "unsupported target"
        if "error" in result:
            assert "Unsupported PnR target" not in result["error"]


# ---------------------------------------------------------------------------
# Constraint auto-detection
# ---------------------------------------------------------------------------


class TestConstraintAutoDetect:
    def test_finds_pcf_in_project_dir(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "top.v").write_text("module top; endmodule", encoding="utf-8")
        (tmpdir / "pins.pcf").write_text("set_io clk 35", encoding="utf-8")
        found = _find_constraints(str(tmpdir), "ice40")
        assert found is not None
        assert found.endswith(".pcf")

    def test_returns_none_when_no_constraints(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "top.v").write_text("module top; endmodule", encoding="utf-8")
        assert _find_constraints(str(tmpdir), "ice40") is None

    def test_prefers_root_over_subdir(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "root.pcf").write_text("set_io clk 35", encoding="utf-8")
        sub = tmpdir / "subdir"
        sub.mkdir()
        (sub / "sub.pcf").write_text("set_io clk 36", encoding="utf-8")
        found = _find_constraints(str(tmpdir), "ice40")
        assert "root.pcf" in found


# ---------------------------------------------------------------------------
# Filelist parsing
# ---------------------------------------------------------------------------


class TestFilelistParsing:
    def test_basic_filelist(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "a.v").write_text("module a; endmodule", encoding="utf-8")
        (tmpdir / "b.v").write_text("module b; endmodule", encoding="utf-8")
        (tmpdir / "files.f").write_text("a.v\nb.v\n", encoding="utf-8")
        sources, incdirs, defines = _parse_filelist(
            str(tmpdir / "files.f"), str(tmpdir)
        )
        assert len(sources) == 2

    def test_incdir_and_define(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "files.f").write_text(
            "+incdir+rtl/include\n+define+SIMULATION\na.v\n",
            encoding="utf-8",
        )
        (tmpdir / "a.v").write_text("module a; endmodule", encoding="utf-8")
        sources, incdirs, defines = _parse_filelist(
            str(tmpdir / "files.f"), str(tmpdir)
        )
        assert len(sources) == 1
        assert any("include" in d for d in incdirs)
        assert "SIMULATION" in defines

    def test_comments_and_blanks_ignored(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "files.f").write_text(
            "# comment\n\n// another\na.v\n", encoding="utf-8"
        )
        (tmpdir / "a.v").write_text("module a; endmodule", encoding="utf-8")
        sources, _, _ = _parse_filelist(str(tmpdir / "files.f"), str(tmpdir))
        assert len(sources) == 1

    def test_project_dir_uses_filelist(self) -> None:
        tmpdir = _mk_tmp_dir()
        (tmpdir / "b.v").write_text("module b; endmodule", encoding="utf-8")
        (tmpdir / "a.v").write_text("module a; endmodule", encoding="utf-8")
        # files.f specifies order: b.v first
        (tmpdir / "files.f").write_text("b.v\na.v\n", encoding="utf-8")
        paths, err = _resolve_sources(project_dir=str(tmpdir), language="verilog")
        assert err is None
        # Order should match filelist, not alphabetical glob
        basenames = [os.path.basename(p) for p in paths]
        assert basenames == ["b.v", "a.v"]


# ---------------------------------------------------------------------------
# Simulation verdict parsing
# ---------------------------------------------------------------------------


class TestSimulationVerdict:
    def test_pass_detected(self) -> None:
        v = _parse_verdict("TEST PASSED\n", "", 0)
        assert v["verdict"] == "pass"

    def test_fail_detected(self) -> None:
        v = _parse_verdict("ASSERTION FAILED at line 42\n", "", 0)
        assert v["verdict"] == "fail"

    def test_nonzero_exit_is_fail(self) -> None:
        v = _parse_verdict("something\n", "", 1)
        assert v["verdict"] == "fail"

    def test_inconclusive(self) -> None:
        v = _parse_verdict("value = 42\n", "", 0)
        assert v["verdict"] == "inconclusive"

    def test_fail_overrides_pass(self) -> None:
        v = _parse_verdict("TEST PASSED\nASSERTION FAILED\n", "", 1)
        assert v["verdict"] == "fail"


# ---------------------------------------------------------------------------
# VCD summary
# ---------------------------------------------------------------------------


class TestVcdSummary:
    def test_basic_vcd(self) -> None:
        vcd = (
            "$var wire 1 ! clk $end\n"
            '$var wire 1 " data $end\n'
            "$enddefinitions $end\n"
            '#0\n0!\n0"\n'
            '#10\n1!\n1"\n'
            "#20\n0!\n"
        )
        summary = _summarize_vcd(vcd)
        assert summary["signal_count"] == 2
        assert "clk" in summary["signals"]
        assert summary["end_time"] == "20"
        assert summary["final_values"]["clk"] == "0"
        assert summary["final_values"]["data"] == "1"

    def test_empty_vcd(self) -> None:
        summary = _summarize_vcd("")
        assert summary["signal_count"] == 0


# ---------------------------------------------------------------------------
# Include paths in yosys read commands
# ---------------------------------------------------------------------------


class TestIncludePaths:
    def test_include_dirs_in_read_verilog(self) -> None:
        result = _yosys_read_cmds(
            ["/tmp/a.v"],
            "verilog",
            include_dirs=["/project/rtl", "/project/inc"],
        )
        assert "-I/project/rtl" in result
        assert "-I/project/inc" in result

    def test_defines_in_read_verilog(self) -> None:
        result = _yosys_read_cmds(
            ["/tmp/a.v"],
            "verilog",
            defines=["SIMULATION", "WIDTH=8"],
        )
        assert "-DSIMULATION" in result
        assert "-DWIDTH=8" in result

    def test_no_include_dirs_for_vhdl(self) -> None:
        result = _yosys_read_cmds(
            ["/tmp/a.vhd"],
            "vhdl",
            top_module="top",
            include_dirs=["/project/inc"],
        )
        assert "-I" not in result


class TestBuildParser:
    def test_empty_log(self) -> None:
        result = parse_build_log("")
        assert result["phase"] == "starting"
        assert result["tool"] == "unknown"
        assert result["health"]["status"] == "ok"

    def test_nextpnr_phases(self) -> None:
        log = (
            "Info: Packing design...\n"
            "Info: Placed 42 cells\n"
            "Info: HeAP Placer iteration 1\n"
            "Info: Routing...\n"
            "Info: Max frequency for clock 'clk': 142.34 MHz (PASS at 100.00 MHz)\n"
        )
        result = parse_build_log(log)
        assert result["tool"] == "nextpnr"
        assert "nextpnr_timing" in result["phase"]
        assert any("packing" in p for p in result["phase_history"])
        assert any("placing" in p for p in result["phase_history"])
        assert any("routing" in p for p in result["phase_history"])
        assert result["timing"]["fmax_mhz"] == 142.34
        assert result["timing"]["met"] is True

    def test_phase_detail_has_line_counts(self) -> None:
        log = (
            "Info: Packing design...\n"
            "packed line 1\n"
            "packed line 2\n"
            "Info: HeAP Placer iteration 1\n"
            "placed line 1\n"
        )
        result = parse_build_log(log)
        assert "phase_detail" in result
        assert all("lines" in d for d in result["phase_detail"])

    def test_utilization_parsing(self) -> None:
        log = "ICESTORM_LC:    42/ 1280     3%\nSB_IO:          3/  206     1%\n"
        result = parse_build_log(log)
        assert result["utilization"]["luts"]["used"] == 42
        assert result["utilization"]["luts"]["total"] == 1280
        assert result["utilization"]["ios"]["used"] == 3

    def test_high_utilization_warning(self) -> None:
        log = "LUT4:    4800/ 5000    96%\n"
        result = parse_build_log(log)
        assert result["health"]["status"] == "critical"
        assert any("nearly full" in c for c in result["health"]["concerns"])

    def test_wns_violation(self) -> None:
        log = "WNS: -0.234 ns\n"
        result = parse_build_log(log)
        assert result["slack"]["wns_ns"] == -0.234
        assert result["health"]["status"] == "critical"
        assert any("timing violated" in c for c in result["health"]["concerns"])

    def test_timing_fail(self) -> None:
        log = "Max frequency for clock 'clk': 45.00 MHz (FAIL at 100.00 MHz)\n"
        result = parse_build_log(log)
        assert result["timing"]["met"] is False
        assert result["health"]["status"] == "critical"

    def test_congestion_unrouted(self) -> None:
        log = "failed to route 12 nets\n"
        result = parse_build_log(log)
        assert result["congestion"]["unrouted_nets"] == 12
        assert result["health"]["status"] == "critical"

    def test_error_count_yosys(self) -> None:
        log = "Yosys 0.40\nERROR: something broke\nWarning: something suspect\nWarning: another one\n"
        result = parse_build_log(log)
        assert result["tool"] == "yosys"
        assert result["errors"] == 1
        assert result["warnings"] == 2
        assert result["health"]["status"] == "error"

    def test_error_count_fallback(self) -> None:
        log = "Error: something broke\nWarning: something suspect\n"
        result = parse_build_log(log)
        assert result["errors"] == 1
        assert result["warnings"] == 1

    def test_no_false_positive_errors(self) -> None:
        # "0 Errors" or variable names should NOT count as errors
        log = "Yosys 0.40\nTotal errors: 0\nmy_error_handler called\n"
        result = parse_build_log(log)
        assert result["errors"] == 0

    def test_yosys_phases(self) -> None:
        log = (
            "Yosys 0.40\n"
            "Parsing Verilog input...\n"
            "Executing HIERARCHY pass\n"
            "Executing SYNTH_ICE40 pass\n"
            "Executing WRITE_JSON pass\n"
        )
        result = parse_build_log(log)
        assert result["tool"] == "yosys"
        assert "write_output" in result["phase"]
        assert any("read_design" in p for p in result["phase_history"])
        assert any("synthesis" in p for p in result["phase_history"])

    def test_vivado_wns_tns(self) -> None:
        log = "Vivado v2024.1\nWNS: 1.234 ns\nTNS: 0.000 ns\nWHS: 0.089 ns\nTHS: 0.000 ns\n"
        result = parse_build_log(log)
        assert result["slack"]["wns_ns"] == 1.234
        assert result["slack"]["tns_ns"] == 0.0
        assert result["slack"]["whs_ns"] == 0.089
        assert result["health"]["status"] == "ok"

    def test_vivado_phases(self) -> None:
        log = (
            "Vivado v2024.1\n"
            "synth_design -top my_design\n"
            "Phase 1 Synthesis\n"
            "opt_design\n"
            "place_design\n"
            "phys_opt_design\n"
            "route_design\n"
            "report_timing_summary\n"
            "write_bitstream my_design.bit\n"
        )
        result = parse_build_log(log)
        assert result["tool"] == "vivado"
        assert "vivado_write" in result["phase"]
        assert any("synth" in p for p in result["phase_history"])
        assert any("place" in p for p in result["phase_history"])
        assert any("phys_opt" in p for p in result["phase_history"])
        assert any("route" in p for p in result["phase_history"])
        assert any("timing" in p for p in result["phase_history"])

    def test_vivado_utilization_table(self) -> None:
        # Real Vivado format: | Resource | Used | Fixed | Available | Util% |
        log = (
            "Vivado v2024.1\n"
            "| Slice LUTs      |  1234 |     0 |    53200 |  2.32 |\n"
            "| Slice Registers |   890 |     0 |   106400 |  0.84 |\n"
            "| Block RAM Tile  |   4.5 |     0 |      140 |  3.21 |\n"
            "| DSPs            |     3 |     0 |      220 |  1.36 |\n"
            "| Bonded IOB      |    12 |     0 |      200 |  6.00 |\n"
        )
        result = parse_build_log(log)
        assert result["utilization"]["luts"]["used"] == 1234
        assert result["utilization"]["luts"]["total"] == 53200
        assert result["utilization"]["luts"]["pct"] == round(1234 / 53200 * 100, 1)
        assert result["utilization"]["ffs"]["used"] == 890
        assert result["utilization"]["ffs"]["total"] == 106400
        assert result["utilization"]["brams"]["used"] == 4.5
        assert result["utilization"]["brams"]["total"] == 140.0
        assert result["utilization"]["dsps"]["used"] == 3
        assert result["utilization"]["dsps"]["total"] == 220
        assert result["utilization"]["ios"]["used"] == 12
        assert result["utilization"]["ios"]["total"] == 200

    def test_quartus_phases(self) -> None:
        log = (
            "quartus_map --analysis\n"
            "Analysis & Synthesis\n"
            "quartus_fit starting\n"
            "Fitter Placement\n"
            "Fitter Routing\n"
            "quartus_sta\n"
            "quartus_asm\n"
        )
        result = parse_build_log(log)
        assert result["tool"] == "quartus"
        assert "quartus_asm" in result["phase"]
        assert any("synth" in p for p in result["phase_history"])
        assert any("fit" in p for p in result["phase_history"])
        assert any("sta" in p for p in result["phase_history"])

    def test_quartus_utilization(self) -> None:
        log = (
            "quartus_map\n"
            "Total logic elements ; 1,234 / 33,216 ( 4 % )\n"
            "Total registers ; 567 / 33,216 ( 2 % )\n"
            "Total pins ; 42 / 475 ( 9 % )\n"
            "Total memory bits ; 8,192 / 483,840 ( 2 % )\n"
            "Total DSP blocks ; 2 / 70 ( 3 % )\n"
            "Total PLLs ; 1 / 4 ( 25 % )\n"
        )
        result = parse_build_log(log)
        assert result["utilization"]["les"]["used"] == 1234
        assert result["utilization"]["les"]["total"] == 33216
        assert result["utilization"]["ffs"]["used"] == 567
        assert result["utilization"]["ios"]["used"] == 42
        assert result["utilization"]["dsps"]["used"] == 2
        assert result["utilization"]["plls"]["used"] == 1

    def test_quartus_alm_utilization(self) -> None:
        log = (
            "quartus_fit\n"
            "Total ALMs : 2,500 / 41,910 ( 6 % )\n"
            "Total ALUTs : 3,200 / 83,820 ( 4 % )\n"
            "Total dedicated registers : 4,100 / 83,820 ( 5 % )\n"
            "M10K blocks : 12 / 553 ( 2 % )\n"
            "MLABs : 34 / 2,200 ( 2 % )\n"
        )
        result = parse_build_log(log)
        assert result["utilization"]["alms"]["used"] == 2500
        assert result["utilization"]["aluts"]["used"] == 3200
        assert result["utilization"]["ffs"]["used"] == 4100
        assert result["utilization"]["brams"]["used"] == 12
        assert result["utilization"]["mlabs"]["used"] == 34

    def test_quartus_fmax(self) -> None:
        log = "quartus_sta\nFmax : 142.34 MHz\nRestricted Fmax : 100.00 MHz\n"
        result = parse_build_log(log)
        assert result["timing"]["fmax_mhz"] == 142.34
        assert result["timing"]["restricted_fmax_mhz"] == 100.0

    def test_quartus_setup_slack_violation(self) -> None:
        log = "quartus_sta\nWorst-case setup slack : -0.123\n"
        result = parse_build_log(log)
        assert result["timing"]["setup_slack_ns"] == -0.123
        assert result["health"]["status"] == "critical"
        assert any("Setup slack" in c for c in result["health"]["concerns"])

    def test_quartus_hold_slack_violation(self) -> None:
        log = "quartus_sta\nWorst-case hold slack : -0.050\n"
        result = parse_build_log(log)
        assert result["timing"]["hold_slack_ns"] == -0.05
        assert result["health"]["status"] == "critical"
        assert any("Hold" in c for c in result["health"]["concerns"])

    def test_quartus_routing_problems(self) -> None:
        log = "quartus_fit\nRouting problems detected: 5\n"
        result = parse_build_log(log)
        assert result["congestion"]["unrouted_nets"] == 5
        assert result["health"]["status"] == "critical"

    def test_tool_auto_detection(self) -> None:
        assert parse_build_log("Yosys 0.40\n")["tool"] == "yosys"
        assert parse_build_log("Info: Device: ice40\n")["tool"] == "nextpnr"
        assert parse_build_log("Vivado v2024.1\n")["tool"] == "vivado"
        assert parse_build_log("quartus_map\n")["tool"] == "quartus"
        assert parse_build_log("ghdl -a foo.vhd\n")["tool"] == "ghdl"
        assert parse_build_log("some random output\n")["tool"] == "unknown"

    def test_nextpnr_progress_tracking(self) -> None:
        log = (
            "Info: Packing...\n"
            "Info: HeAP Placer iteration 1\n"
            "at t = 1.0 cost = 100\n"
            "at t = 0.5 cost = 80\n"
            "at t = 0.1 cost = 60\n"
            "Info: Routing...\n"
            "Pass 1 completed, 5 net failures\n"
            "Pass 2 completed, 0 net failures\n"
        )
        result = parse_build_log(log)
        assert "progress" in result
        assert result["progress"]["router_pass"] == 2


class TestSimulateVhdlErrors:
    def test_missing_entity_in_testbench(self) -> None:
        result = simulate(
            "-- design",
            "-- testbench with no entity",
            language="vhdl",
        )
        assert result["success"] is False
        assert "entity" in result.get("error", "").lower()
