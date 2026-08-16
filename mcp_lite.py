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
import contextvars
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# progressToken of the tools/call currently executing in this context.
# Set per-request in _handle_tools_call; propagates into asyncio.to_thread
# worker threads (contextvars are copied into the thread), so a tool running
# in a thread can report progress for the right request.
current_progress_token: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "current_progress_token", default=None
)

# Protocol revisions we implement, newest first. If the client requests one
# of these we echo it back; otherwise we answer with our latest (per spec).
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


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
    # Machine-readable result (2025-06-18 spec). Only sent to clients that
    # negotiated a protocol revision that defines it.
    structuredContent: dict | None = None  # noqa: N815

    def to_dict(self) -> dict:
        d: dict = {
            "content": [c.to_dict() for c in self.content],
            "isError": self.isError,
        }
        if self.structuredContent is not None:
            d["structuredContent"] = self.structuredContent
        return d


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
    _negotiated_version: str = field(default=LATEST_PROTOCOL_VERSION, init=False)
    _inflight: dict = field(default_factory=dict, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(
        default=None, init=False, repr=False
    )

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
        client_version = params.get("protocolVersion", "")
        if client_version in SUPPORTED_PROTOCOL_VERSIONS:
            self._negotiated_version = client_version
        else:
            # Unknown revision requested — answer with our latest; the client
            # disconnects if it can't work with it (per spec).
            self._negotiated_version = LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": self._negotiated_version,
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
        progress_token = (params.get("_meta") or {}).get("progressToken")
        ctx_token = current_progress_token.set(progress_token)
        try:
            result = await self._call_handler(name, args)
        finally:
            current_progress_token.reset(ctx_token)
        d = result.to_dict()
        # structuredContent was introduced in 2025-06-18; don't send it to
        # clients that negotiated an older revision.
        if self._negotiated_version < "2025-06-18":
            d.pop("structuredContent", None)
        return d

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

    def send_progress(
        self, progress: float, total: float | None = None, message: str = ""
    ) -> None:
        """Emit a notifications/progress for the current tools/call.

        No-op when the client didn't send a progressToken. Safe to call from
        tool code running in a worker thread (asyncio.to_thread): the write is
        marshalled onto the event loop thread so it cannot interleave with a
        response being sent concurrently.
        """
        token = current_progress_token.get()
        if token is None:
            return
        notif_params: dict = {"progressToken": token, "progress": progress}
        if total is not None:
            notif_params["total"] = total
        if message:
            notif_params["message"] = message
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": notif_params,
        }
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._send, notification)
        else:
            self._send(notification)

    async def _handle_message(self, message: dict) -> None:
        response = await self._dispatch(message)
        if response is not None:
            self._send(response)

    def _cancel_request(self, request_id: object) -> None:
        """Handle notifications/cancelled: abort the in-flight request task.

        Per spec no response is sent for a cancelled request (the task's
        CancelledError propagates and _handle_message never reaches _send).
        A subprocess already started by the tool keeps running to completion
        in its worker thread; only the response is abandoned.
        """
        task = self._inflight.get(request_id)
        if task is not None and not task.done():
            logger.info("Request %r cancelled by client", request_id)
            task.cancel()

    async def run(self) -> None:
        """Read JSON-RPC messages from stdin, dispatch, write responses to stdout.

        Each request is dispatched as its own task, so fast requests (ping,
        build_status, cancel_build) are answered while a slow tool call
        (synthesize, place_and_route) is still running.
        """
        loop = asyncio.get_running_loop()
        self._loop = loop

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

            if message.get("method") == "notifications/cancelled":
                self._cancel_request((message.get("params") or {}).get("requestId"))
                continue

            task = asyncio.create_task(self._handle_message(message))
            pending.add(task)
            task.add_done_callback(pending.discard)

            msg_id = message.get("id")
            if msg_id is not None:
                self._inflight[msg_id] = task

                def _untrack(_task: asyncio.Task, _id: object = msg_id) -> None:
                    self._inflight.pop(_id, None)

                task.add_done_callback(_untrack)

        # Let in-flight requests finish before shutting down.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# Tool, TextContent, CallToolResult are the only types server.py uses.
# Import them directly from this module instead of a `types` shim.
