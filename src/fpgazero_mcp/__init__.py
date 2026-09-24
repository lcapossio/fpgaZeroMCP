# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""fpgaZeroMCP - an MCP server exposing a complete FPGA toolchain."""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("fpgazero-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.8.0"

__all__ = ["__version__"]
