"""
Transfer Stage Control Application — Orchestrator
===================================================

Wires together logging, settings, hardware drivers, input system,
and GUI.  This is the main application class instantiated by ``main.py``.
"""

from __future__ import annotations

import sys

from utils.logging_config import setup_logging, get_logger
from gui.main_window import MainWindow


class App:
    """Application entry point.

    Sets up logging, creates the main window, and starts the event loop.
    """

    def __init__(self) -> None:
        self._log = setup_logging()
        self._log.info("=" * 60)
        self._log.info("Transfer Stage Control System — starting")
        self._log.info("Python %s", sys.version)
        self._log.info("=" * 60)

    def run(self) -> None:
        """Create the main window and start the tkinter event loop."""
        try:
            window = MainWindow()
            window.run()
        except Exception as exc:
            self._log.exception("Fatal error during startup: %s", exc)
            raise
        finally:
            self._log.info("Application shutdown complete")
