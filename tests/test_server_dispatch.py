# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
import asyncio
import json

import server


def test_handle_call_tool_uses_to_thread(monkeypatch) -> None:
    called = []

    async def fake_to_thread(func, *args, **kwargs):
        called.append(func.__name__)
        return func(*args, **kwargs)

    def fake_lint_hdl(**_kwargs):
        return {"ok": True}

    monkeypatch.setattr(server.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(server, "lint_hdl", fake_lint_hdl)

    result = asyncio.run(server.handle_call_tool("lint_hdl", {"code": "module m; endmodule"}))
    assert called == ["fake_lint_hdl"]

    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert result.isError is False
