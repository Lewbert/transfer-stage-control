"""
Transfer Stage Control System
==============================

Entry point for the Transfer Stage Control application.

Usage::

    python main.py

Build to .exe::

    pyinstaller --clean --noconfirm build_scripts\\transfer_stage.spec
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # Required for PyInstaller multiprocessing support on Windows
    multiprocessing.freeze_support()

    from app import App

    app = App()
    app.run()
