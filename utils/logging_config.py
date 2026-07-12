"""
Centralized logging configuration for the Transfer Stage Control application.

Configures both file (debug.log) and console output with consistent formatting.
Also installs a global uncaught-exception hook for crash reporting.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime

# ---------------------------------------------------------------------------
# Application base directory (handles frozen PyInstaller builds)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_FILE = os.path.join(_APP_DIR, "debug.log")

# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------

_log_initialized = False


def setup_logging(*, level: int = logging.DEBUG) -> logging.Logger:
    """Configure root logger with file and console handlers.

    Call once at application startup.  Subsequent calls are no-ops.

    Parameters
    ----------
    level : int
        Log level for the file handler (default DEBUG).  Console stays at INFO.

    Returns
    -------
    logging.Logger
        The root logger, ready for use.
    """
    global _log_initialized
    if _log_initialized:
        return logging.getLogger("transfer_stage")

    root = logging.getLogger("transfer_stage")
    root.setLevel(logging.DEBUG)

    # File handler — DEBUG, UTF-8, overwritten each session
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="w")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Console handler — INFO only (keep terminal clean)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "[%(levelname)-7s] %(name)s: %(message)s",
    ))
    root.addHandler(ch)

    # Suppress noisy third-party loggers
    logging.getLogger("serial").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    # Global exception hook — log uncaught exceptions before they crash
    def _log_uncaught(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root.critical(
            "Uncaught exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_uncaught

    _log_initialized = True
    root.info("Logging initialized — log file: %s", LOG_FILE)
    root.info("App directory: %s", _APP_DIR)

    return root


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the 'transfer_stage' namespace."""
    return logging.getLogger(f"transfer_stage.{name}")
