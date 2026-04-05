# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Flash a bitstream to an FPGA board using iceprog or openFPGALoader."""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile

from tools.errors import err, TOOL_NOT_FOUND, TIMEOUT, INVALID_INPUT

# Default programmer per target
_DEFAULT_PROGRAMMER = {
    "ice40": "iceprog",
    "ecp5":  "openFPGALoader",
    "nexus": "openFPGALoader",
    "gowin": "openFPGALoader",
}


def program_fpga(
    target: str,
    bitstream_b64: str = "",
    bitstream_path: str = "",
    programmer: str = "",
    board: str = "",
    timeout: int = 60,
) -> dict:
    """Flash a bitstream to an FPGA.

    Provide either bitstream_b64 (base64-encoded) or bitstream_path (file on disk).
    programmer: "iceprog", "openFPGALoader", or auto-detect from target.
    board: openFPGALoader board flag (e.g. "ulx3s", "tangnano9k") — optional.
    """
    if not bitstream_b64 and not bitstream_path:
        return err(INVALID_INPUT, "Provide either bitstream_b64 or bitstream_path.")
    if bitstream_b64 and bitstream_path:
        return err(INVALID_INPUT, "Provide only one of bitstream_b64 or bitstream_path.")

    prog = programmer or _DEFAULT_PROGRAMMER.get(target, "openFPGALoader")

    # Write base64 to a temp file if needed
    tmp_file = None
    try:
        if bitstream_b64:
            try:
                data = base64.b64decode(bitstream_b64)
            except Exception:
                return err(INVALID_INPUT, "Invalid base64 in bitstream_b64.")
            tmp_file = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
            tmp_file.write(data)
            tmp_file.close()
            file_path = tmp_file.name
        else:
            if not os.path.exists(bitstream_path):
                return err(INVALID_INPUT, f"Bitstream file not found: {bitstream_path}")
            file_path = bitstream_path

        cmd = _build_program_cmd(prog, file_path, board)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
            )
        except FileNotFoundError:
            return err(TOOL_NOT_FOUND, f"'{prog}' not found. Install OSS CAD Suite.")
        except subprocess.TimeoutExpired:
            return err(TIMEOUT, f"Programming timed out after {timeout} s.")

        return {
            "success": result.returncode == 0,
            "programmer": prog,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    finally:
        if tmp_file and os.path.exists(tmp_file.name):
            os.unlink(tmp_file.name)


def _build_program_cmd(programmer: str, file_path: str, board: str) -> list[str]:
    if programmer == "iceprog":
        return ["iceprog", file_path]

    # openFPGALoader
    cmd = ["openFPGALoader"]
    if board:
        cmd += ["--board", board]
    cmd.append(file_path)
    return cmd
