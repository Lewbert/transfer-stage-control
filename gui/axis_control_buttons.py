"""
Axis Control Buttons
=====================

On-screen directional button grid for stage control.
Supports press-and-hold (continuous) and click (single step) behavior
via tkinter ``<ButtonPress-1>`` / ``<ButtonRelease-1>`` events.

Slow speed only — no fast mode from UI buttons.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Optional

from gui.styles import (
    COLOR_STOP_BTN, COLOR_HOME_BTN, FONT_SMALL,
)


class AxisControlButtons(ttk.Frame):
    """Directional button pad for a single stage — 4×2 table.

    Layout::

         X+    X-
         Y+    Y-
         Z+    Z-
         STOP  HOME

    For Zolix XYR, Z+/Z- are replaced by R+/R-.

    Parameters
    ----------
    parent : tk.Widget
        Parent widget.
    axes : list of str
        Axes to create buttons for, e.g. ``["x", "y", "z"]`` or
        ``["x", "y", "r"]``.
    on_press : callable
        Called when a button is pressed: ``on_press(axis, direction)``.
    on_release : callable
        Called when a button is released: ``on_release(axis)``.
    long_press_ms : int
        Duration after which a press becomes continuous (default 300).
    single_step_on_click : bool
        If True, a quick click (< long_press_ms) triggers a single step.
        The ``on_press`` callback handles single-step via the
        ``single_step`` flag.
    """

    def __init__(
        self,
        parent: tk.Widget,
        axes: list = None,
        on_press: Optional[Callable] = None,
        on_release: Optional[Callable] = None,
        long_press_ms: int = 300,
        single_step_on_click: bool = True,
    ) -> None:
        super().__init__(parent)
        self._axes = axes or ["x", "y", "z"]
        self._on_press_cb = on_press
        self._on_release_cb = on_release
        self._long_press_ms = long_press_ms
        self._single_step_on_click = single_step_on_click

        # Track press state per button
        self._press_timers: Dict[str, str] = {}  # timer ID per axis
        self._is_continuous: Dict[str, bool] = {}  # axis → True if continuous started
        self._press_direction: Dict[str, int] = {}  # axis → direction at press time

        self._build()

    def _build(self) -> None:
        """Build the 4×2 button table."""
        has_z = "z" in self._axes
        has_r = "r" in self._axes
        zr_axis = "z" if has_z else "r"
        zr_pos = "Z+" if has_z else "R+"
        zr_neg = "Z-" if has_z else "R-"

        self.grid_columnconfigure(0, weight=1, uniform="btn")
        self.grid_columnconfigure(1, weight=1, uniform="btn")

        # Row 0: X+ | X-
        self._make_btn("x", +1, "X+", row=0, col=0)
        self._make_btn("x", -1, "X-", row=0, col=1)

        # Row 1: Y+ | Y-
        self._make_btn("y", +1, "Y+", row=1, col=0)
        self._make_btn("y", -1, "Y-", row=1, col=1)

        # Row 2: Z+/R+ | Z-/R-
        self._make_btn(zr_axis, +1, zr_pos, row=2, col=0)
        self._make_btn(zr_axis, -1, zr_neg, row=2, col=1)

        # Row 3: STOP | HOME
        btn_stop = tk.Button(
            self, text="STOP", width=8,
            bg=COLOR_STOP_BTN, fg="white",
            font=FONT_SMALL, relief=tk.RAISED, bd=2,
        )
        btn_stop.grid(row=3, column=0, padx=2, pady=(4, 1), sticky="ew")
        btn_stop.bind("<ButtonPress-1>", lambda e: self._on_stop_press())

        btn_zero = tk.Button(
            self, text="ZERO", width=8,
            bg=COLOR_HOME_BTN, fg="white",
            font=FONT_SMALL, relief=tk.RAISED, bd=2,
        )
        btn_zero.grid(row=3, column=1, padx=2, pady=(4, 1), sticky="ew")
        btn_zero.bind("<ButtonPress-1>", lambda e: self._on_zero_press())

    def _make_btn(self, axis: str, direction: int, text: str, row: int, col: int) -> ttk.Button:
        """Create a directional button at the given grid position."""
        btn = ttk.Button(self, text=text, width=8)
        btn.grid(row=row, column=col, padx=2, pady=1, sticky="ew")
        btn.bind("<ButtonPress-1>", lambda e, a=axis, d=direction: self._on_btn_press(a, d))
        btn.bind("<ButtonRelease-1>", lambda e, a=axis: self._on_btn_release(a))
        return btn

    # ------------------------------------------------------------------
    # Button event handlers
    # ------------------------------------------------------------------

    def _on_btn_press(self, axis: str, direction: int) -> None:
        """Handle a direction button press."""
        self._press_direction[axis] = direction
        self._is_continuous[axis] = False

        # Schedule long-press timer
        timer_id = self.winfo_toplevel().after(
            self._long_press_ms,
            lambda a=axis, d=direction: self._on_long_press(a, d),
        )
        self._press_timers[axis] = timer_id

    def _on_btn_release(self, axis: str) -> None:
        """Handle a direction button release."""
        # Cancel long-press timer
        timer_id = self._press_timers.pop(axis, None)
        if timer_id:
            self.winfo_toplevel().after_cancel(timer_id)

        was_continuous = self._is_continuous.pop(axis, False)
        direction = self._press_direction.pop(axis, 0)

        if was_continuous:
            # Stop continuous movement
            if self._on_release_cb:
                self._on_release_cb(axis)
        elif self._single_step_on_click and direction != 0:
            # Short click → single step
            cb = getattr(self, "_click_callback", None)
            if cb:
                cb(axis, direction)

    def _on_long_press(self, axis: str, direction: int) -> None:
        """Called when a button is held past the threshold."""
        self._is_continuous[axis] = True
        if self._on_press_cb:
            self._on_press_cb(axis, direction, single_step=False)

    def _on_stop_press(self) -> None:
        """STOP button pressed — stop all movement on this stage."""
        if self._on_press_cb:
            self._on_press_cb("stop", 0, single_step=False)

    def _on_zero_press(self) -> None:
        """ZERO button pressed — reset position counter."""
        if self._on_press_cb:
            self._on_press_cb("zero", 0, single_step=False)

    # ------------------------------------------------------------------
    # Click-based single step (separate from press/release above)
    # ------------------------------------------------------------------

    def bind_click(self, callback: Callable[[str, int], None]) -> None:
        """Register a callback for single-step clicks.

        ``callback(axis, direction)`` is called on short press+release.
        """
        self._click_callback = callback
