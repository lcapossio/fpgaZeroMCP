# SPDX-FileCopyrightText: 2026 Leonardo Capossio (bard0) <hello@bard0.com>
# SPDX-License-Identifier: MIT
"""Board presets for common FPGA development boards.

Each preset maps a board name to its target, device, package, and
default clock frequency, so users can say board="icebreaker" instead
of manually specifying target/device/package.
"""
from __future__ import annotations


BOARD_PRESETS: dict[str, dict] = {
    # ----- iCE40 boards -----
    "icebreaker": {
        "target": "ice40",
        "device": "up5k",
        "package": "sg48",
        "clock_mhz": 12.0,
    },
    "icestick": {
        "target": "ice40",
        "device": "hx1k",
        "package": "tq144",
        "clock_mhz": 12.0,
    },
    "icesugar": {
        "target": "ice40",
        "device": "up5k",
        "package": "sg48",
        "clock_mhz": 12.0,
    },
    "tinyfpga_bx": {
        "target": "ice40",
        "device": "lp8k",
        "package": "cm81",
        "clock_mhz": 16.0,
    },
    # ----- ECP5 boards -----
    "ulx3s_25f": {
        "target": "ecp5",
        "device": "25k",
        "package": "CABGA381",
        "clock_mhz": 25.0,
    },
    "ulx3s_45f": {
        "target": "ecp5",
        "device": "45k",
        "package": "CABGA381",
        "clock_mhz": 25.0,
    },
    "ulx3s_85f": {
        "target": "ecp5",
        "device": "85k",
        "package": "CABGA381",
        "clock_mhz": 25.0,
    },
    "orangecrab_r02": {
        "target": "ecp5",
        "device": "25k",
        "package": "CSFBGA285",
        "clock_mhz": 48.0,
    },
    "colorlight_i5": {
        "target": "ecp5",
        "device": "25k",
        "package": "CABGA256",
        "clock_mhz": 25.0,
    },
    # ----- Gowin boards -----
    "tangnano_9k": {
        "target": "gowin",
        "device": "GW1NR-LV9QN88PC6/I5",
        "package": "",
        "clock_mhz": 27.0,
    },
    "tangnano_20k": {
        "target": "gowin",
        "device": "GW2A-LV18PG256C8/I7",
        "package": "",
        "clock_mhz": 27.0,
    },
}


def get_board_preset(board: str) -> dict | None:
    """Return the preset dict for a board name, or None if unknown."""
    return BOARD_PRESETS.get(board.lower().replace("-", "_"))


def list_boards() -> list[dict]:
    """Return all known board presets."""
    return [
        {"board": name, **preset}
        for name, preset in sorted(BOARD_PRESETS.items())
    ]
