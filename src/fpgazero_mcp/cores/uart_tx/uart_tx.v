// uart_tx.v - Simple UART Transmitter
// fpgaZeroMCP community - MIT License
//
// Parameters:
//   DATA_WIDTH   - Data bits per frame (default 8)
//   CLKS_PER_BIT - System clock cycles per UART bit
//                  e.g. 100 MHz / 115200 baud = 868
//
// Usage:
//   Assert i_tx_dv for one clock cycle with i_tx_byte set.
//   o_tx_active goes high during transmission.
//   o_tx_done pulses for one cycle when complete.

module uart_tx #(
    parameter DATA_WIDTH   = 8,
    parameter CLKS_PER_BIT = 868
)(
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire                  i_tx_dv,
    input  wire [DATA_WIDTH-1:0] i_tx_byte,
    output reg                   o_tx_active,
    output reg                   o_tx_serial,
    output reg                   o_tx_done
);

    localparam [2:0]
        IDLE  = 3'd0,
        START = 3'd1,
        DATA  = 3'd2,
        STOP  = 3'd3;

    localparam CTR_W = $clog2(CLKS_PER_BIT);
    localparam BIT_W = $clog2(DATA_WIDTH);

    reg [2:0]          r_state;
    reg [CTR_W-1:0]    r_clk_count;
    reg [BIT_W-1:0]    r_bit_index;
    reg [DATA_WIDTH-1:0] r_tx_data;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            r_state     <= IDLE;
            o_tx_serial <= 1'b1;
            o_tx_done   <= 1'b0;
            o_tx_active <= 1'b0;
            r_clk_count <= {CTR_W{1'b0}};
            r_bit_index <= {BIT_W{1'b0}};
            r_tx_data   <= {DATA_WIDTH{1'b0}};
        end else begin
            o_tx_done <= 1'b0;

            case (r_state)
                IDLE: begin
                    o_tx_serial <= 1'b1;
                    o_tx_active <= 1'b0;
                    r_clk_count <= {CTR_W{1'b0}};
                    r_bit_index <= {BIT_W{1'b0}};
                    if (i_tx_dv) begin
                        o_tx_active <= 1'b1;
                        r_tx_data   <= i_tx_byte;
                        r_state     <= START;
                    end
                end

                START: begin
                    o_tx_serial <= 1'b0;
                    if (r_clk_count < CLKS_PER_BIT - 1) begin
                        r_clk_count <= r_clk_count + 1'b1;
                    end else begin
                        r_clk_count <= {CTR_W{1'b0}};
                        r_state     <= DATA;
                    end
                end

                DATA: begin
                    o_tx_serial <= r_tx_data[r_bit_index];
                    if (r_clk_count < CLKS_PER_BIT - 1) begin
                        r_clk_count <= r_clk_count + 1'b1;
                    end else begin
                        r_clk_count <= {CTR_W{1'b0}};
                        if (r_bit_index < DATA_WIDTH - 1) begin
                            r_bit_index <= r_bit_index + 1'b1;
                        end else begin
                            r_bit_index <= {BIT_W{1'b0}};
                            r_state     <= STOP;
                        end
                    end
                end

                STOP: begin
                    o_tx_serial <= 1'b1;
                    if (r_clk_count < CLKS_PER_BIT - 1) begin
                        r_clk_count <= r_clk_count + 1'b1;
                    end else begin
                        r_clk_count <= {CTR_W{1'b0}};
                        o_tx_done   <= 1'b1;
                        o_tx_active <= 1'b0;
                        r_state     <= IDLE;
                    end
                end

                default: r_state <= IDLE;
            endcase
        end
    end

endmodule
