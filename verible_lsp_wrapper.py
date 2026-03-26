#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""
Standalone wrapper exposing fpgaZeroMCP's Verible LSP tools.
Provides a stdio-based interface compatible with Claude Code MCP.
"""
import asyncio
import json
import sys
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from tools.lsp import format_hdl, get_diagnostics


async def handle_stdin():
    """Read JSON-RPC requests from stdin and process them."""
    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            request = json.loads(line.strip())

            # Simple JSON-RPC dispatcher
            method = request.get("method", "")
            params = request.get("params", {})
            request_id = request.get("id")

            if method == "get_diagnostics":
                result = get_diagnostics(
                    code=params.get("code", ""),
                    language=params.get("language", "verilog")
                )
            elif method == "format_hdl":
                result = format_hdl(
                    code=params.get("code", ""),
                    language=params.get("language", "verilog")
                )
            else:
                result = {"error": f"Unknown method: {method}"}

            # Send response
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            }
            print(json.dumps(response))
            sys.stdout.flush()

        except json.JSONDecodeError:
            continue
        except Exception as e:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -1, "message": str(e)}
            }
            print(json.dumps(error_response))
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(handle_stdin())
