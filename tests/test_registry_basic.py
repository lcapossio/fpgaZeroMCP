# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from registry.resolver import CoreRegistry


def _write_core(tmp_path: Path) -> None:
    core_dir = tmp_path / "uart_core"
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "uart.v").write_text("// uart\n", encoding="utf-8")
    manifest = {
        "name": "uart_core",
        "version": "1.0.0",
        "description": "UART test core",
        "author": "test",
        "license": "MIT",
        "language": "verilog",
        "category": "communication",
        "tags": ["uart"],
        "parameters": {
            "CLKS_PER_BIT": {
                "type": "integer",
                "default": 16,
                "description": "clock divisor",
            }
        },
        "ports": {
            "clk": {"direction": "input", "width": 1, "description": ""},
            "rx": {"direction": "input", "width": 1, "description": ""},
            "tx": {"direction": "output", "width": 1, "description": ""},
        },
        "files": ["uart.v"],
    }
    (core_dir / "core.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _mk_tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=f"reg_{uuid4().hex}_"))


def test_registry_list_get_generate() -> None:
    tmp_path = _mk_tmp_dir()
    _write_core(tmp_path)
    reg = CoreRegistry(cores_dir=tmp_path)

    listed = reg.list_cores()
    assert len(listed) == 1
    assert listed[0]["name"] == "uart_core"

    core = reg.get_core("uart_core")
    assert "manifest" in core
    assert "files" in core
    assert "uart.v" in core["files"]

    gen = reg.generate_ip(
        "uart_core", parameters={"CLKS_PER_BIT": 32}, instance_name="u0"
    )
    assert "instantiation" in gen
    assert "CLKS_PER_BIT" in gen["instantiation"]
    assert "u0" in gen["instantiation"]
