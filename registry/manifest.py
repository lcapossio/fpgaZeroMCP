# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Manifest types for IP cores.

Hand-rolled dataclasses with manual validation instead of pydantic, to avoid
pulling in pydantic just for this one purpose. When the mcp SDK is replaced,
pydantic will no longer be a transitive dependency at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


_VALID_PARAM_TYPES = ("integer", "string", "boolean")
_VALID_PORT_DIRECTIONS = ("input", "output", "inout")
_VALID_LANGUAGES = ("verilog", "systemverilog", "vhdl")


class ValidationError(ValueError):
    """Raised when a manifest dict does not conform to the schema."""


@dataclass
class ParameterSpec:
    type: str  # "integer" | "string" | "boolean"
    default: Any
    description: str = ""
    minimum: int | None = None
    maximum: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> ParameterSpec:
        ptype = data.get("type")
        if ptype not in _VALID_PARAM_TYPES:
            raise ValidationError(
                f"ParameterSpec.type must be one of {_VALID_PARAM_TYPES}, got {ptype!r}"
            )
        return cls(
            type=ptype,
            default=data.get("default"),
            description=data.get("description", "") or "",
            minimum=data.get("minimum"),
            maximum=data.get("maximum"),
        )


@dataclass
class PortSpec:
    direction: str  # "input" | "output" | "inout"
    width: int | str  # int, or a parameter expression like "DATA_WIDTH"
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> PortSpec:
        direction = data.get("direction")
        if direction not in _VALID_PORT_DIRECTIONS:
            raise ValidationError(
                f"PortSpec.direction must be one of {_VALID_PORT_DIRECTIONS}, "
                f"got {direction!r}"
            )
        width = data.get("width")
        if not isinstance(width, (int, str)):
            raise ValidationError(
                f"PortSpec.width must be int or str, got {type(width).__name__}"
            )
        return cls(
            direction=direction,
            width=width,
            description=data.get("description", "") or "",
        )


@dataclass
class CoreManifest:
    name: str
    version: str
    description: str
    author: str
    license: str
    language: str  # "verilog" | "systemverilog" | "vhdl"
    category: str
    files: list[str]
    tags: list[str] = field(default_factory=list)
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    ports: dict[str, PortSpec] = field(default_factory=dict)

    @classmethod
    def model_validate(cls, data: dict) -> CoreManifest:
        """Validate and construct a CoreManifest from a dict (pydantic-compatible name)."""
        if not isinstance(data, dict):
            raise ValidationError(f"Expected dict, got {type(data).__name__}")

        required = (
            "name",
            "version",
            "description",
            "author",
            "license",
            "language",
            "category",
            "files",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise ValidationError(f"Missing required fields: {missing}")

        lang = data["language"]
        if lang not in _VALID_LANGUAGES:
            raise ValidationError(
                f"language must be one of {_VALID_LANGUAGES}, got {lang!r}"
            )

        files = data["files"]
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ValidationError("files must be a list[str]")

        for str_field in (
            "name",
            "version",
            "description",
            "author",
            "license",
            "category",
        ):
            if not isinstance(data[str_field], str):
                raise ValidationError(f"{str_field} must be a string")

        tags = data.get("tags", []) or []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValidationError("tags must be a list[str]")

        params_raw = data.get("parameters", {}) or {}
        if not isinstance(params_raw, dict):
            raise ValidationError("parameters must be a dict")
        parameters = {k: ParameterSpec.from_dict(v) for k, v in params_raw.items()}

        ports_raw = data.get("ports", {}) or {}
        if not isinstance(ports_raw, dict):
            raise ValidationError("ports must be a dict")
        ports = {k: PortSpec.from_dict(v) for k, v in ports_raw.items()}

        return cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            license=data["license"],
            language=lang,
            category=data["category"],
            files=list(files),
            tags=list(tags),
            parameters=parameters,
            ports=ports,
        )

    def model_dump(self) -> dict:
        """Serialize to a dict (pydantic-compatible name)."""
        return asdict(self)
