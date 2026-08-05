# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Structured error codes for MCP tool responses.

Every tool error dict should include an 'error_code' field so AI clients
can make retry/fallback decisions without parsing English messages.
"""

from __future__ import annotations

# Tool availability
TOOL_NOT_FOUND = "tool_not_found"

# Execution
TIMEOUT = "timeout"
INVALID_INPUT = "invalid_input"

# HDL-specific
SYNTAX_ERROR = "syntax_error"
ELABORATION_ERROR = "elaboration_error"

# Synthesis / PnR
SYNTHESIS_FAILED = "synthesis_failed"
PNR_FAILED = "pnr_failed"
TIMING_FAILURE = "timing_failure"
RESOURCE_OVERFLOW = "resource_overflow"

# Simulation
SIM_FAILED = "sim_failed"

# File / path
PATH_SECURITY = "path_security"
FILE_NOT_FOUND = "file_not_found"

# Build manager
NOT_ALLOWED = "not_allowed"
BUILD_NOT_FOUND = "build_not_found"

# Registry
CORE_NOT_FOUND = "core_not_found"

# Network / server
NETWORK_ERROR = "network_error"
INTERNAL_ERROR = "internal_error"


def err(code: str, message: str, **extra: object) -> dict:
    """Build a standard error dict with error_code."""
    return {"success": False, "error": message, "error_code": code, **extra}
