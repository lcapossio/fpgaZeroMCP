# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
example.py - Demonstrates fpgaZeroMCP capabilities directly.

Run:  python example.py
Requires: pip install -e .
Optional: iverilog on PATH for lint/simulate examples.
"""

import json
from registry.resolver import CoreRegistry
from tools.lint import lint_hdl
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
# 7. Simulate the UART TX (requires iverilog)
#    Simple testbench: send 0xAB and observe serial output
# ---------------------------------------------------------------------------
section("7. simulate(uart_tx + testbench)")

uart_hdl = reg.get_core("uart_tx")["files"]["uart_tx.v"]

testbench = """`timescale 1ns/1ps
module tb_uart_tx;
    // 10 MHz clock, 9600 baud -> CLKS_PER_BIT = 1042
    localparam CLK_PERIOD   = 100; // ns
    localparam CLKS_PER_BIT = 1042;
    localparam BIT_PERIOD   = CLK_PERIOD * CLKS_PER_BIT;

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
    reg [9:0] frame; // start + 8 data + stop

    initial begin
        // Reset
        rst_n = 0; #(CLK_PERIOD * 5);
        rst_n = 1; #(CLK_PERIOD * 2);

        // Send 0xAB
        i_tx_byte = 8'hAB;
        i_tx_dv   = 1;
        #(CLK_PERIOD);
        i_tx_dv   = 0;

        // Capture the frame (start + 8 bits + stop)
        @(negedge o_tx_serial); // wait for start bit
        frame[0] = o_tx_serial; // start bit (0)
        for (i = 1; i <= 8; i = i + 1) begin
            #(BIT_PERIOD);
            frame[i] = o_tx_serial;
        end
        #(BIT_PERIOD);
        frame[9] = o_tx_serial; // stop bit (1)

        @(posedge o_tx_done);

        $display("TX done. Frame[start=0, data=7:0, stop=1]:");
        $display("  start=%b  data=%b%b%b%b%b%b%b%b  stop=%b",
            frame[0],
            frame[1],frame[2],frame[3],frame[4],
            frame[5],frame[6],frame[7],frame[8],
            frame[9]);
        $display("  Received byte (LSB first): 0x%02X",
            {frame[8],frame[7],frame[6],frame[5],frame[4],frame[3],frame[2],frame[1]});

        if ({frame[8],frame[7],frame[6],frame[5],frame[4],frame[3],frame[2],frame[1]} == 8'hAB)
            $display("  PASS: received 0xAB correctly");
        else
            $display("  FAIL: byte mismatch");

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
