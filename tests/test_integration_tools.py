# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from tools.lint import lint_hdl, lint_project
from tools.lsp import get_diagnostics, format_hdl
from tools.simulate import simulate
from tools.synthesize import synthesize
from tools.pnr import place_and_route


def _mk_tmp_dir() -> Path:
    base = Path("no_commit") / "pytest_tmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"int_{uuid4().hex}"
    if d.exists():
        for child in d.rglob("*"):
            if child.is_file():
                child.unlink()
        for child in sorted(d.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
    d.mkdir()
    return d


def _have(*bins: str) -> bool:
    return all(shutil.which(b) for b in bins)


VERILOG_OK = "module top(input wire a, output wire y); assign y = a; endmodule\n"


@pytest.mark.integration
def test_lint_verilog_iverilog() -> None:
    if not _have("iverilog"):
        pytest.skip("iverilog not installed")
    result = lint_hdl(VERILOG_OK, language="verilog")
    assert result.get("success") is True


@pytest.mark.integration
def test_lint_project_cross_module() -> None:
    if not _have("iverilog"):
        pytest.skip("iverilog not installed")
    sub_mod = "module sub(input wire a, output wire y); assign y = ~a; endmodule\n"
    top_mod = (
        "module top(input wire a, output wire y);\n"
        "  sub u0(.a(a), .y(y));\n"
        "endmodule\n"
    )
    result = lint_project({"sub.v": sub_mod, "top.v": top_mod}, top_module="top")
    assert result.get("success") is True
    assert len(result.get("files", [])) == 2


@pytest.mark.integration
def test_lint_project_missing_module() -> None:
    if not _have("iverilog"):
        pytest.skip("iverilog not installed")
    # top references sub which is not provided — should fail
    top_mod = (
        "module top(input wire a, output wire y);\n"
        "  sub u0(.a(a), .y(y));\n"
        "endmodule\n"
    )
    result = lint_project({"top.v": top_mod}, top_module="top")
    assert result.get("success") is False


@pytest.mark.integration
def test_diagnostics_verilator_or_verible() -> None:
    if not (_have("verilator") or _have("verible-verilog-lint")):
        pytest.skip("verilator/verible not installed")
    result = get_diagnostics("module top; wire x = 1'b1; endmodule\n", language="verilog")
    assert "diagnostics" in result


@pytest.mark.integration
def test_format_verible() -> None:
    if not _have("verible-verilog-format"):
        pytest.skip("verible-verilog-format not installed")
    messy = "module top ( input wire a , output wire y ); assign y=a; endmodule\n"
    result = format_hdl(messy, language="verilog")
    assert result.get("success") is True
    assert "formatted" in result


@pytest.mark.integration
def test_simulate_iverilog_vvp() -> None:
    if not _have("iverilog", "vvp"):
        pytest.skip("iverilog/vvp not installed")
    design = "module top(input wire a, output wire y); assign y = a; endmodule\n"
    tb = (
        "module tb;\n"
        "  reg a; wire y;\n"
        "  top dut(.a(a), .y(y));\n"
        "  initial begin a=0; #1; $display(\"y=%0d\", y); a=1; #1; $display(\"y=%0d\", y); $finish; end\n"
        "endmodule\n"
    )
    result = simulate(design, tb, timeout=10)
    assert result.get("success") is True
    assert "y=0" in result.get("stdout", "")


@pytest.mark.integration
def test_synthesize_yosys() -> None:
    if not _have("yosys"):
        pytest.skip("yosys not installed")
    result = synthesize(VERILOG_OK, top_module="top", target="generic")
    assert result.get("success") is True
    assert "top" in result.get("modules", [])


@pytest.mark.integration
def test_place_and_route_ice40() -> None:
    if not _have("yosys", "nextpnr-ice40"):
        pytest.skip("yosys/nextpnr-ice40 not installed")
    result = place_and_route(
        VERILOG_OK,
        top_module="top",
        target="ice40",
        device="hx1k",
        package="tq144",
        constraints="set_io a 1\nset_io y 2\n",
        timeout=30,
    )
    assert result.get("stage") == "place_and_route"


@pytest.mark.integration
def test_lint_vhdl_ghdl() -> None:
    if not _have("ghdl"):
        pytest.skip("ghdl not installed")
    vhdl = (
        "library ieee;\n"
        "use ieee.std_logic_1164.all;\n"
        "entity inverter is\n"
        "  port (a : in std_logic; y : out std_logic);\n"
        "end entity;\n"
        "architecture rtl of inverter is\n"
        "begin\n"
        "  y <= not a;\n"
        "end architecture;\n"
    )
    result = lint_hdl(vhdl, language="vhdl")
    assert result.get("success") is True
    assert result.get("tool") == "ghdl"


@pytest.mark.integration
def test_simulate_vhdl_ghdl() -> None:
    if not _have("ghdl"):
        pytest.skip("ghdl not installed")
    design = (
        "library ieee;\n"
        "use ieee.std_logic_1164.all;\n"
        "entity inverter is\n"
        "  port (a : in std_logic; y : out std_logic);\n"
        "end entity;\n"
        "architecture rtl of inverter is\n"
        "begin\n"
        "  y <= not a;\n"
        "end architecture;\n"
    )
    tb = (
        "library ieee;\n"
        "use ieee.std_logic_1164.all;\n"
        "entity tb_inverter is\n"
        "end entity;\n"
        "architecture sim of tb_inverter is\n"
        "  signal a, y : std_logic;\n"
        "begin\n"
        "  dut: entity work.inverter port map(a => a, y => y);\n"
        "  process begin\n"
        "    a <= '0'; wait for 1 ns;\n"
        "    assert y = '1' report \"FAIL: y should be 1\" severity failure;\n"
        "    a <= '1'; wait for 1 ns;\n"
        "    assert y = '0' report \"FAIL: y should be 0\" severity failure;\n"
        "    report \"PASS\";\n"
        "    wait;\n"
        "  end process;\n"
        "end architecture;\n"
    )
    result = simulate(design, tb, language="vhdl", timeout=10)
    assert result.get("success") is True
    assert result.get("tool") == "ghdl"
    assert "PASS" in result.get("stdout", "") + result.get("stderr", "")
