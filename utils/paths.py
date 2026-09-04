"""
Application data directory helpers.

Determines where settings and logs are stored:
- Frozen (.exe): ``%APPDATA%\\TransferStageControl\\``
- Dev mode: project root
"""

from __future__ import annotations

import os
import sys


def get_app_dir() -> str:
    """Return the application data directory, creating it if needed.

    In frozen mode (PyInstaller .exe), always uses ``%APPDATA%``.
    In dev mode, returns the project root.
    """
    if getattr(sys, "frozen", False):
        path = os.path.join(os.environ["APPDATA"], "TransferStageControl")
    else:
        path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(path, exist_ok=True)
    return path


SETTINGS_FILE = os.path.join(get_app_dir(), "settings.json")
LOG_FILE = os.path.join(get_app_dir(), "debug.log")
