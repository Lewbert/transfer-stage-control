"""
Status Panel
=============

Horizontal bar at the top of the main window showing:
- Connection status dots + labels for all 3 devices
- Gamepad connection indicator
- Settings button
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gui.styles import (
    COLOR_OK, COLOR_ERROR, COLOR_GRAY,
    FONT_STATUS,
    create_status_dot, set_status_dot,
)


class StatusPanel(ttk.Frame):
    """Top-of-window device connection status bar."""

    def __init__(
        self,
        parent: tk.Widget,
        on_settings: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self._on_settings = on_settings
        self._dots: dict = {}
        self._labels: dict = {}
        self._build()

    def _build(self) -> None:
        """Build the status bar layout."""
        # Device status indicators — left side
        devices_frame = ttk.Frame(self)
        devices_frame.pack(side=tk.LEFT, fill=tk.X, padx=4, pady=2)

        for i, (dev_id, dev_name) in enumerate([
            ("sigmakoki", "XYZ"),
            ("zolix", "XYR"),
            ("yudian", "Temp"),
        ]):
            dot = create_status_dot(devices_frame)
            dot.pack(side=tk.LEFT, padx=(6 if i > 0 else 0, 1))

            label = ttk.Label(
                devices_frame,
                text=dev_name,
                font=FONT_STATUS,
                foreground=COLOR_GRAY,
            )
            label.pack(side=tk.LEFT, padx=(0, 6))

            self._dots[dev_id] = dot
            self._labels[dev_id] = label

        # Separator
        ttk.Separator(devices_frame, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=1,
        )

        # Gamepad indicator
        self._gamepad_dot = create_status_dot(devices_frame)
        self._gamepad_dot.pack(side=tk.LEFT, padx=(0, 1))

        self._gamepad_label = ttk.Label(
            devices_frame,
            text="Gamepad",
            font=FONT_STATUS,
            foreground=COLOR_GRAY,
        )
        self._gamepad_label.pack(side=tk.LEFT, padx=(0, 4))

        # Settings button — right side
        ttk.Button(
            self, text="⚙ Settings", command=self._on_settings,
        ).pack(side=tk.RIGHT, padx=8, pady=2)

    # ------------------------------------------------------------------
    # Update methods
    # ------------------------------------------------------------------

    def set_device_status(self, device_id: str, connected: bool) -> None:
        """Update a device's connection indicator.

        Parameters
        ----------
        device_id : str
            ``"sigmakoki"``, ``"zolix"``, or ``"yudian"``.
        connected : bool
        """
        dot = self._dots.get(device_id)
        label = self._labels.get(device_id)
        if dot is None or label is None:
            return

        state = "connected" if connected else "disconnected"
        set_status_dot(dot, state)

        name_map = {"sigmakoki": "XYZ", "zolix": "XYR", "yudian": "Temp"}
        name = name_map.get(device_id, device_id)

        label.configure(
            text=name,
            foreground=COLOR_OK if connected else COLOR_ERROR,
        )

    def set_gamepad_status(self, connected: bool) -> None:
        """Update the gamepad indicator."""
        state = "connected" if connected else "disconnected"
        set_status_dot(self._gamepad_dot, state)
        display = "Gamepad"
        self._gamepad_label.configure(
            text=display,
            foreground=COLOR_OK if connected else COLOR_GRAY,
        )
