"""
Application settings persistence.

Loads and saves configuration from a JSON file alongside the application.
Provides sensible defaults for all sections (SigmaKoki, Zolix, Yudian,
gamepad, input, UI).
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Application base directory
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SETTINGS_FILE = os.path.join(_APP_DIR, "settings.json")

# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    "_version": 1,
    "sigmakoki": {
        "port": "",
        "baudrate": 115200,
        "timeout_s": 0.3,
        "slow_speed_hz": 200,
        "fast_speed_hz": 500,
        "slow_speed_z": 200,
        "fast_speed_z": 500,
        "invert_x": False,
        "invert_y": False,
        "invert_z": False,
        "flip_xy": False,
        "single_step_amount": 10,
        "single_step_z": 10,
        "um_per_step_xy": 1.0,
        "um_per_step_z": 1.0,
    },
    "zolix": {
        "port": "",
        "baudrate": 115200,
        "slave_address": 1,
        "timeout_s": 0.05,
        "slow_speed_pps": 1000,
        "fast_speed_pps": 5000,
        "slow_speed_r": 500,
        "fast_speed_r": 2000,
        "invert_x": False,
        "invert_y": False,
        "invert_r": False,
        "flip_xy": False,
        "single_step_amount": 100,
        "single_step_r": 100,
        "um_per_step_xy": 1.0,
        "um_per_step_r": 1.0,
        "stop_mode": "immediate",  # "decel" or "immediate"
        "ring_speeds": [500, 2500, 5000],  # slow, medium, fast (steps/sec) for stick control
    },
    "yudian": {
        "port": "",
        "baudrate": 9600,
        "slave_address": 1,
        "timeout_s": 0.5,
        "poll_interval_ms": 500,
        "safety_temp_lo_c": -100.0,
        "safety_temp_hi_c": 400.0,
        "presets": [
            {"name": "Room Temp", "temp_c": 25.0},
            {"name": "Body Temp", "temp_c": 37.0},
            {"name": "Boiling", "temp_c": 100.0},
        ],
    },
    "gamepad": {
        "deadzone": 0.20,
        "invert_left_x": False,
        "invert_left_y": False,
        "invert_right_x": False,
        "invert_right_y": False,
        "trigger_threshold": 0.5,
    },
    "input": {
        "long_press_threshold_ms": 300,
        "loop_rate_hz": 60,
        "status_poll_rate_hz": 10,
    },
    "ui": {
        "window_width": 720,
        "window_height": 420,
        "font_family": "Microsoft YaHei",
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*.  *base* is mutated in place."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_settings() -> Dict[str, Any]:
    """Load settings from disk, filling in defaults for any missing keys.

    Returns
    -------
    dict
        The merged settings dictionary.  Never ``None``; the default
        settings are returned if the file is missing or corrupt.
    """
    defaults = deepcopy(DEFAULT_SETTINGS)
    if not os.path.exists(SETTINGS_FILE):
        return defaults

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
    except (json.JSONDecodeError, OSError):
        disk = {}

    return _deep_merge(defaults, disk)


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist *settings* to disk as JSON.

    The file is written atomically (write to temp, then rename) to
    prevent corruption on crash during write.
    """
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        # Fallback: write directly (less safe but better than nothing)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, ensure_ascii=False)
        try:
            os.remove(tmp)
        except OSError:
            pass
