"""
Gamepad Indicator
=================

Small panel showing gamepad connection status and D-pad stage selector.
Updated via the gamepad_status_queue from the InputManager.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui.styles import (
    COLOR_OK, COLOR_GRAY,
    FONT_STATUS, FONT_SMALL,
)


class GamepadIndicator(ttk.Frame):
    """Compact gamepad status display."""

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        self._status_label = ttk.Label(
            self, text="🎮 No Controller", font=FONT_SMALL, foreground=COLOR_GRAY,
        )
        self._status_label.pack(side=tk.LEFT, padx=4)

        self._dpad_label = ttk.Label(
            self, text="", font=FONT_STATUS, foreground=COLOR_GRAY,
        )
        self._dpad_label.pack(side=tk.LEFT, padx=4)

    def set_connected(self, connected: bool, name: str = "Xbox Controller") -> None:
        """Update connection state."""
        if connected:
            self._status_label.configure(
                text=f"🎮 {name}", foreground=COLOR_OK,
            )
        else:
            self._status_label.configure(
                text="🎮 No Controller", foreground=COLOR_GRAY,
            )

    def set_dpad_stage(self, stage_id: str) -> None:
        """Show which stage the D-pad controls."""
        name = "SigmaKoki XYZ" if stage_id == "sigmakoki" else "Zolix XYR"
        self._dpad_label.configure(
            text=f"D-pad → {name}", foreground=COLOR_OK,
        )
