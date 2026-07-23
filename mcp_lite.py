# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Minimal MCP stdio server — stdlib only.

Implements the subset of the Model Context Protocol we need:
  - initialize / initialized handshake
  - tools/list
  - tools/call

Wire format: JSON-RPC 2.0 over newline-delimited stdio. One message per line.
Requests are dispatched concurrently: a slow tool call does not block ping,
tools/list, or further tool calls.

This replaces the official `mcp` SDK (which pulls pydantic, httpx, anyio,
pydantic_settings — tens of megabytes of transitive deps) with ~200 lines
of stdlib code. Tradeoff: we track the MCP spec manually; in exchange
we cut ~15-20 MB RSS per server session.

The public API mirrors what server.py used from `mcp.server.Server`:
  app = Server("name")
  @app.list_tools()
  async def handle_list_tools() -> list[Tool]: ...
  @app.call_tool()
  async def handle_call_tool(name: str, args: dict) -> CallToolResult: ...
  asyncio.run(app.run())
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# MCP protocol version we speak. Update when the spec evolves.
_PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Data types — mirror the mcp SDK's types.* namespace for API compatibility
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """Tool definition — equivalent to mcp.types.Tool."""

    name: str
    description: str
    inputSchema: dict  # noqa: N815 — MCP wire name is camelCase

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema,
        }


@dataclass
class TextContent:
    """Text content block — equivalent to mcp.types.TextContent."""

    type: str  # always "text"
    text: str

    def to_dict(self) -> dict:
        return {"type": self.type, "text": self.text}


@dataclass
class CallToolResult:
    """Tool invocation result — equivalent to mcp.types.CallToolResult."""

    content: list[TextContent]
    isError: bool = False  # noqa: N815 — MCP wire name is camelCase

    def to_dict(self) -> dict:
        return {
            "content": [c.to_dict() for c in self.content],
            "isError": self.isError,
        }


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


ListToolsHandler = Callable[[], Awaitable[list[Tool]]]
CallToolHandler = Callable[[str, dict], Awaitable[CallToolResult]]


@dataclass
class Server:
    """Minimal MCP server. Register handlers via decorators, then call run()."""

    name: str
    version: str = "0.0.0"
    _list_handler: ListToolsHandler | None = field(default=None, init=False)
    _call_handler: CallToolHandler | None = field(default=None, init=False)

    def list_tools(self) -> Callable[[ListToolsHandler], ListToolsHandler]:
        """Decorator: register the list_tools handler."""

        def _register(func: ListToolsHandler) -> ListToolsHandler:
            self._list_handler = func
            return func

        return _register

    def call_tool(self) -> Callable[[CallToolHandler], CallToolHandler]:
        """Decorator: register the call_tool handler."""

        def _register(func: CallToolHandler) -> CallToolHandler:
            self._call_handler = func
            return func

        return _register

    # ------------------------------------------------------------------
    # Protocol handlers
    # ------------------------------------------------------------------

    def _make_response(self, req_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _make_error(self, req_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    async def _handle_initialize(self, params: dict) -> dict:
        # Echo back the client's protocol version if we recognize it, otherwise
        # our own. Most clients accept either.
        client_version = params.get("protocolVersion", _PROTOCOL_VERSION)
        return {
            "protocolVersion": client_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    async def _handle_tools_list(self) -> dict:
        if self._list_handler is None:
            return {"tools": []}
        tools = await self._list_handler()
        return {"tools": [t.to_dict() for t in tools]}

    async def _handle_tools_call(self, params: dict) -> dict:
        if self._call_handler is None:
            raise RuntimeError("call_tool handler not registered")
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        result = await self._call_handler(name, args)
        return result.to_dict()

    async def _dispatch(self, message: dict) -> dict | None:
        """Route a single JSON-RPC message. Returns a response dict or None for notifications."""
        method = message.get("method", "")
        params = message.get("params", {}) or {}
        req_id = message.get("id")

        # Notifications (no id) get no response.
        is_notification = req_id is None

        try:
            if method == "initialize":
                result = await self._handle_initialize(params)
            elif method in ("initialized", "notifications/initialized"):
                return None  # notification, no response
            elif method == "tools/list":
                result = await self._handle_tools_list()
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            elif method == "ping":
                result = {}
            elif method == "shutdown":
                result = {}
            else:
                if is_notification:
                    return None
                return self._make_error(req_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            logger.exception("Error handling %s", method)
            if is_notification:
                return None
            return self._make_error(req_id, -32603, f"Internal error: {exc}")

        if is_notification:
            return None
        return self._make_response(req_id, result)

    # ------------------------------------------------------------------
    # Transport — newline-delimited JSON over stdio
    # ------------------------------------------------------------------

    def _send(self, response: dict) -> None:
        """Write one response line to stdout.

        Called only from coroutines on the event loop thread, with no await
        between write and flush, so concurrent requests cannot interleave
        their output.
        """
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    async def _handle_message(self, message: dict) -> None:
        response = await self._dispatch(message)
        if response is not None:
            self._send(response)

    async def run(self) -> None:
        """Read JSON-RPC messages from stdin, dispatch, write responses to stdout.

        Each request is dispatched as its own task, so fast requests (ping,
        build_status, cancel_build) are answered while a slow tool call
        (synthesize, place_and_route) is still running.
        """
        loop = asyncio.get_running_loop()

        # asyncio doesn't wrap stdin/stdout as streams on Windows cleanly.
        # Use run_in_executor with blocking readline — simpler and portable.
        def _read_line() -> str:
            return sys.stdin.readline()

        pending: set[asyncio.Task] = set()

        while True:
            line = await loop.run_in_executor(None, _read_line)
            if not line:
                # EOF — client disconnected
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Malformed JSON from client: %s", exc)
                self._send(self._make_error(None, -32700, f"Parse error: {exc}"))
                continue

            task = asyncio.create_task(self._handle_message(message))
            pending.add(task)
            task.add_done_callback(pending.discard)

        # Let in-flight requests finish before shutting down.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# Tool, TextContent, CallToolResult are the only types server.py uses.
# Import them directly from this module instead of a `types` shim.
