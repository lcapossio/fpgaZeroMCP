// fifo.v - Synchronous FIFO
// fpgaZeroMCP community - MIT License
//
// Parameters:
//   DATA_WIDTH - Width of each word in bits (default 8)
//   DEPTH      - Number of entries; must be a power of 2 (default 16)
//
// Simultaneous read and write in the same cycle is supported.
// Writes are ignored when full; reads are ignored when empty.

module fifo #(
    parameter DATA_WIDTH = 8,
    parameter DEPTH      = 16
)(
    input  wire                   clk,
    input  wire                   rst_n,
    input  wire                   wr_en,
    input  wire [DATA_WIDTH-1:0]  wr_data,
    input  wire                   rd_en,
    output reg  [DATA_WIDTH-1:0]  rd_data,
    output wire                   full,
    output wire                   empty,
    output wire [$clog2(DEPTH):0] count
);

    localparam ADDR_W = $clog2(DEPTH);

    reg [DATA_WIDTH-1:0] mem [0:DEPTH-1];
    reg [ADDR_W-1:0]     wr_ptr;
    reg [ADDR_W-1:0]     rd_ptr;
    reg [ADDR_W:0]       r_count;

    wire do_write = wr_en && !full;
    wire do_read  = rd_en && !empty;

    assign full  = (r_count == DEPTH);
    assign empty = (r_count == 0);
    assign count = r_count;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr  <= {ADDR_W{1'b0}};
            rd_ptr  <= {ADDR_W{1'b0}};
            r_count <= {(ADDR_W+1){1'b0}};
            rd_data <= {DATA_WIDTH{1'b0}};
        end else begin
            if (do_write) begin
                mem[wr_ptr] <= wr_data;
                wr_ptr      <= wr_ptr + 1'b1;
            end

            if (do_read) begin
                rd_data <= mem[rd_ptr];
                rd_ptr  <= rd_ptr + 1'b1;
            end

            case ({do_write, do_read})
                2'b10:   r_count <= r_count + 1'b1;
                2'b01:   r_count <= r_count - 1'b1;
                default: r_count <= r_count;
            endcase
        end
    end

endmodule
