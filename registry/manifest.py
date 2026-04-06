# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel


class ParameterSpec(BaseModel):
    type: Literal["integer", "string", "boolean"]
    default: Any
    description: str = ""
    minimum: int | None = None
    maximum: int | None = None


class PortSpec(BaseModel):
    direction: Literal["input", "output", "inout"]
    width: int | str  # int or parameter expression e.g. "DATA_WIDTH"
    description: str = ""


class CoreManifest(BaseModel):
    name: str
    version: str
    description: str
    author: str
    license: str
    language: Literal["verilog", "systemverilog", "vhdl"]
    category: str
    tags: list[str] = []
    parameters: dict[str, ParameterSpec] = {}
    ports: dict[str, PortSpec] = {}
    files: list[str]
