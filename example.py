# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
example.py - Demonstrates fpgaZeroMCP capabilities directly.

Run:  python example.py
Requires: pip install -e .
Optional: iverilog, ghdl on PATH for lint/simulate examples.
"""

import json
from registry.resolver import CoreRegistry
from tools.lint import lint_hdl, lint_project
from tools.simulate import simulate

SEP = "-" * 60


def section(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")


def pp(data) -> None:
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# 1. List all cores
# ---------------------------------------------------------------------------
section("1. list_ip_cores()")
reg = CoreRegistry()
pp(reg.list_cores())


# ---------------------------------------------------------------------------
# 2. List cores filtered by category
# ---------------------------------------------------------------------------
section("2. list_ip_cores(category='memory')")
pp(reg.list_cores(category="memory"))


# ---------------------------------------------------------------------------
# 3. Get a core (manifest + HDL source)
# ---------------------------------------------------------------------------
section("3. get_ip_core('uart_tx')")
core = reg.get_core("uart_tx")
# Print manifest only — HDL source is long
pp(core["manifest"])
print(f"\n  [HDL files: {list(core['files'].keys())}]")


# ---------------------------------------------------------------------------
# 4. Generate a parameterized instance
#    200 MHz system clock, 115200 baud → CLKS_PER_BIT = 200_000_000 / 115200 ≈ 1736
# ---------------------------------------------------------------------------
section("4. generate_ip('uart_tx', CLKS_PER_BIT=1736, instance_name='u_uart')")
result = reg.generate_ip(
    name="uart_tx",
    parameters={"CLKS_PER_BIT": 1736},
    instance_name="u_uart",
)
print("Parameters used:", result["parameters_used"])
print("\nInstantiation snippet:")
print(result["instantiation"])


# ---------------------------------------------------------------------------
# 5. Generate a FIFO: 32-bit wide, 256 deep
# ---------------------------------------------------------------------------
section("5. generate_ip('fifo', DATA_WIDTH=32, DEPTH=256)")
result = reg.generate_ip(
    name="fifo",
    parameters={"DATA_WIDTH": 32, "DEPTH": 256},
    instance_name="u_fifo",
)
print("Parameters used:", result["parameters_used"])
print("\nInstantiation snippet:")
print(result["instantiation"])


# ---------------------------------------------------------------------------
# 6. Lint the FIFO HDL (requires iverilog)
# ---------------------------------------------------------------------------
section("6. lint_hdl(fifo.v)")
fifo_hdl = reg.get_core("fifo")["files"]["fifo.v"]
lint_result = lint_hdl(fifo_hdl, language="verilog")
if "error" in lint_result:
    print("  Skipped:", lint_result["error"])
else:
    status = "PASS" if lint_result["success"] else "FAIL"
    print(f"  [{status}] {lint_result.get('message', lint_result.get('stderr', ''))}")


# ---------------------------------------------------------------------------
# 7. Lint multiple files together (requires iverilog)
# ---------------------------------------------------------------------------
section("7. lint_project(uart_tx + fifo)")
uart_core = reg.get_core("uart_tx")
fifo_core = reg.get_core("fifo")
project_files = {}
project_files.update(uart_core["files"])
project_files.update(fifo_core["files"])
lint_proj = lint_project(project_files)
if "error" in lint_proj:
    print("  Skipped:", lint_proj["error"])
else:
    status = "PASS" if lint_proj["success"] else "FAIL"
    print(f"  [{status}] {lint_proj.get('message', lint_proj.get('stderr', ''))}")
    print(f"  Files: {lint_proj.get('files', [])}")


# ---------------------------------------------------------------------------
# 8. Simulate the UART TX (requires iverilog)
#    Simple testbench: send 0xAB and observe serial output
# ---------------------------------------------------------------------------
section("8. simulate(uart_tx + testbench, language='verilog')")

uart_hdl = reg.get_core("uart_tx")["files"]["uart_tx.v"]

testbench = """`timescale 1ns/1ps
module tb_uart_tx;
    // 10 MHz clock, 9600 baud -> CLKS_PER_BIT = 1042
    localparam CLK_PERIOD   = 100; // ns
    localparam CLKS_PER_BIT = 1042;
    localparam BIT_PERIOD   = CLK_PERIOD * CLKS_PER_BIT;
    localparam HALF_BIT     = BIT_PERIOD / 2;

    reg        clk = 0, rst_n = 0, i_tx_dv = 0;
    reg  [7:0] i_tx_byte = 0;
    wire       o_tx_serial, o_tx_active, o_tx_done;

    always #(CLK_PERIOD/2) clk = ~clk;

    uart_tx #(.DATA_WIDTH(8), .CLKS_PER_BIT(CLKS_PER_BIT)) dut (
        .clk(clk), .rst_n(rst_n),
        .i_tx_dv(i_tx_dv), .i_tx_byte(i_tx_byte),
        .o_tx_active(o_tx_active), .o_tx_serial(o_tx_serial),
        .o_tx_done(o_tx_done)
    );

    integer i;
    reg [7:0] rx_byte;

    initial begin
        // Reset
        rst_n = 0; #(CLK_PERIOD * 5);
        rst_n = 1; #(CLK_PERIOD * 2);

        // Send 0xAB
        i_tx_byte = 8'hAB;
        i_tx_dv   = 1;
        #(CLK_PERIOD);
        i_tx_dv   = 0;

        // Wait for start bit (line goes low)
        @(negedge o_tx_serial);
        // Sample at mid-bit: skip to middle of start bit, then capture 8 data bits
        #(HALF_BIT + BIT_PERIOD); // middle of bit 0 (LSB)
        for (i = 0; i < 8; i = i + 1) begin
            rx_byte[i] = o_tx_serial; // UART is LSB first
            #(BIT_PERIOD);
        end

        @(posedge o_tx_done);

        $display("TX done. Received: 0x%02X (expected 0xAB)", rx_byte);
        if (rx_byte == 8'hAB)
            $display("PASS");
        else
            $display("FAIL: byte mismatch");
        $finish;
    end

    // Timeout guard
    initial begin
        #(BIT_PERIOD * 15);
        $display("TIMEOUT");
        $finish;
    end
endmodule
"""

sim_result = simulate(uart_hdl, testbench, timeout=30)
if "error" in sim_result:
    print("  Skipped:", sim_result["error"])
elif not sim_result["success"]:
    print("  FAIL (stage:", sim_result.get("stage"), ")")
    print(sim_result.get("stderr", ""))
else:
    print(sim_result["stdout"].strip())


# ---------------------------------------------------------------------------
# 9. VHDL lint (requires ghdl)
# ---------------------------------------------------------------------------
section("9. lint_hdl(inverter.vhd, language='vhdl')")

vhdl_design = """library ieee;
use ieee.std_logic_1164.all;
entity inverter is
  port (a : in std_logic; y : out std_logic);
end entity;
architecture rtl of inverter is
begin
  y <= not a;
end architecture;
"""

vhdl_lint = lint_hdl(vhdl_design, language="vhdl")
if "error" in vhdl_lint:
    print("  Skipped:", vhdl_lint["error"])
else:
    status = "PASS" if vhdl_lint["success"] else "FAIL"
    print(f"  [{status}] {vhdl_lint.get('message', vhdl_lint.get('stderr', ''))}")


# ---------------------------------------------------------------------------
# 10. VHDL simulation (requires ghdl)
# ---------------------------------------------------------------------------
section("10. simulate(inverter + testbench, language='vhdl')")

vhdl_tb = """library ieee;
use ieee.std_logic_1164.all;
entity tb_inverter is
end entity;
architecture sim of tb_inverter is
  signal a, y : std_logic;
begin
  dut: entity work.inverter port map(a => a, y => y);
  process begin
    a <= '0'; wait for 1 ns;
    assert y = '1' report "FAIL: y should be 1" severity failure;
    a <= '1'; wait for 1 ns;
    assert y = '0' report "FAIL: y should be 0" severity failure;
    report "PASS";
    wait;
  end process;
end architecture;
"""

vhdl_sim = simulate(vhdl_design, vhdl_tb, language="vhdl", timeout=10)
if "error" in vhdl_sim:
    print("  Skipped:", vhdl_sim["error"])
elif not vhdl_sim["success"]:
    print("  FAIL (stage:", vhdl_sim.get("stage"), ")")
    print(vhdl_sim.get("stderr", ""))
else:
    output = vhdl_sim.get("stdout", "") + vhdl_sim.get("stderr", "")
    print(output.strip() if output.strip() else "  Simulation completed successfully")
