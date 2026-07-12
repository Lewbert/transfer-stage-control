"""
Stage Panel
============

Composite panel for a single stage (SigmaKoki XYZ or Zolix XYR).
Shows position, speed, limit switch indicators, on-screen directional
buttons, software enable toggle, HOME, and STOP ALL.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from stage_control.stage_state import StageState

from gui.styles import (
    COLOR_BLUE, COLOR_GRAY, FONT_SMALL, FONT_MONO,
    create_limit_indicator, set_limit_color,
)
from gui.axis_control_buttons import AxisControlButtons


class StagePanel(ttk.LabelFrame):
    """Per-stage control and status panel.

    Parameters
    ----------
    parent : tk.Widget
    stage_id : str
        ``"sigmakoki"`` or ``"zolix"``.
    title : str
        Panel title (e.g. "SIGMAKOKI XYZ STAGE").
    axes : list of str
        Axes for this stage, e.g. ``["x", "y", "z"]``.
    on_enable_toggle : callable
        Called when the enable checkbox is toggled:
        ``on_enable_toggle(stage_id, enabled)``.
    on_button_press : callable
        ``on_button_press(stage_id, axis, direction, single_step)``
    on_button_release : callable
        ``on_button_release(stage_id, axis)``
    on_stop : callable
        ``on_stop(stage_id)``
    on_zero : callable
        ``on_zero(stage_id)``
    step_um : dict
        ``{axis: (factor, suffix)}`` — e.g. ``{"x": (2.0, "µm"), "y": (2.0, "µm"), "z": (0.5, "µm")}``.
    """

    def __init__(
        self,
        parent: tk.Widget,
        stage_id: str,
        title: str,
        axes: list = None,
        on_enable_toggle: Callable = None,
        on_button_press: Callable = None,
        on_button_release: Callable = None,
        on_stop: Callable = None,
        on_zero: Callable = None,
        step_um: dict = None,
    ) -> None:
        super().__init__(parent, text=title, padding=4)
        self._stage_id = stage_id
        self._axes = axes or ["x", "y", "z"]
        self._on_enable_toggle = on_enable_toggle
        self._on_stop = on_stop
        self._on_zero = on_zero
        self._on_button_press = on_button_press
        self._on_button_release = on_button_release

        self.configure(style="Panel.TLabelframe")

        self._limit_circles: dict = {}
        self._position_labels: dict = {}
        self._converted_labels: dict = {}
        self._speed_label = None
        self._enable_var = tk.BooleanVar(value=True)

        # Conversion: axis → (factor, suffix)
        if step_um is None:
            step_um = {}
        self._step_um: dict = {}
        for axis in self._axes:
            factor, suffix = step_um.get(axis, (1.0, ""))
            self._step_um[axis] = (factor, suffix)

        self._build()

    def _build(self) -> None:
        """Build the panel layout."""
        # Enable checkbox
        enable_frame = ttk.Frame(self, style="Panel.TFrame")
        enable_frame.pack(fill=tk.X, pady=(0, 4))
        cb = ttk.Checkbutton(
            enable_frame, text="Enabled", style="Panel.TCheckbutton",
            variable=self._enable_var,
            command=self._on_enable_changed,
        )
        cb.pack(side=tk.LEFT)

        # Status table — axis | Position (steps + converted) | limits
        tbl = ttk.Frame(self, style="Panel.TFrame")
        tbl.pack(fill=tk.X, pady=(0, 2))
        tbl.grid_columnconfigure(0, weight=0)  # axis label
        tbl.grid_columnconfigure(1, weight=1)  # steps
        tbl.grid_columnconfigure(2, weight=1)  # converted
        tbl.grid_columnconfigure(3, weight=0)  # limits

        # Header: "Position" spans steps+converted columns
        ttk.Label(tbl, text="", font=FONT_SMALL, style="Panel.TLabel").grid(row=0, column=0)
        ttk.Label(tbl, text="Position", font=FONT_SMALL, foreground=COLOR_GRAY,
                  style="Panel.TLabel", anchor="center").grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=1)
        ttk.Label(tbl, text="Limits", font=FONT_SMALL, foreground=COLOR_GRAY,
                  style="Panel.TLabel", anchor="c").grid(row=0, column=3)

        for i, axis in enumerate(self._axes):
            r = i + 1
            ttk.Label(tbl, text=f"{axis.upper()}", font=FONT_MONO,
                      style="Panel.TLabel", anchor="e").grid(
                row=r, column=0, sticky="e", padx=(0, 4))
            # Steps — integer, right-aligned, compact
            val_label = ttk.Label(tbl, text="0", font=FONT_MONO,
                                  style="Value.TLabel", anchor="e")
            val_label.grid(row=r, column=1, sticky="e", padx=(0, 2))
            self._position_labels[axis] = val_label
            # Converted — 2dp with unit, right-aligned
            conv_label = ttk.Label(tbl, text="", font=FONT_MONO,
                                   style="Value.TLabel", anchor="e")
            conv_label.grid(row=r, column=2, sticky="e", padx=(0, 2))
            self._converted_labels[axis] = conv_label
            # Limits
            lim_frame = ttk.Frame(tbl, style="Panel.TFrame")
            lim_frame.grid(row=r, column=3, padx=(2, 0))
            c1 = create_limit_indicator(lim_frame, size=8)
            c1.pack(side=tk.LEFT, padx=0)
            self._limit_circles[f"{axis}+"] = c1
            c2 = create_limit_indicator(lim_frame, size=8)
            c2.pack(side=tk.LEFT, padx=0)
            self._limit_circles[f"{axis}-"] = c2

        # Speed line
        spd_frame = ttk.Frame(self, style="Panel.TFrame")
        spd_frame.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(spd_frame, text="Speed:", font=FONT_SMALL, style="Panel.TLabel").pack(
            side=tk.LEFT, padx=(0, 8),
        )
        self._speed_label = ttk.Label(
            spd_frame, text="0 step/s", font=FONT_MONO, style="Value.TLabel",
        )
        self._speed_label.pack(side=tk.LEFT)

        # Directional buttons
        self._buttons = AxisControlButtons(
            self, axes=self._axes,
            on_press=self._on_btn_press,
            on_release=self._on_btn_release,
        )
        self._buttons.pack(fill=tk.X, pady=(6, 0))

        # Click handler for single-step on short click
        self._buttons.bind_click(self._on_btn_click)

    def update_step_um(self, step_um: dict) -> None:
        """Update conversion factors at runtime (called after settings change)."""
        for axis in self._axes:
            factor, suffix = step_um.get(axis, (1.0, ""))
            self._step_um[axis] = (factor, suffix)

    # ------------------------------------------------------------------

    def _on_enable_changed(self) -> None:
        if self._on_enable_toggle:
            self._on_enable_toggle(self._stage_id, self._enable_var.get())

    def _on_btn_press(self, axis: str, direction: int, single_step: bool) -> None:
        if axis == "zero":
            if self._on_zero:
                self._on_zero(self._stage_id)
        elif axis == "stop":
            if self._on_stop:
                self._on_stop(self._stage_id)
        elif self._on_button_press:
            self._on_button_press(self._stage_id, axis, direction, single_step)

    def _on_btn_release(self, axis: str) -> None:
        if self._on_button_release:
            self._on_button_release(self._stage_id, axis)

    def _on_btn_click(self, axis: str, direction: int) -> None:
        """Short click on a directional button → single step."""
        if self._on_button_press:
            self._on_button_press(self._stage_id, axis, direction, single_step=True)

    # ------------------------------------------------------------------
    # Update from StageState
    # ------------------------------------------------------------------

    def update_state(self, state: StageState) -> None:
        """Refresh all display elements from a StageState snapshot."""
        # Enable checkbox
        self._enable_var.set(state.enabled)

        # Position + converted
        for axis in self._axes:
            pos = state.position.get(axis, 0)
            lbl = self._position_labels.get(axis)
            if lbl:
                if isinstance(pos, (int, float)):
                    lbl.configure(text=f"{pos:.0f}")
                else:
                    lbl.configure(text=f"{pos}")

            # Converted value — 2 decimal places with unit
            conv_lbl = self._converted_labels.get(axis)
            if conv_lbl:
                factor, suffix = self._step_um.get(axis, (1.0, ""))
                converted = pos * factor
                if suffix:
                    conv_lbl.configure(text=f"{converted:.2f} {suffix}")
                else:
                    conv_lbl.configure(text="")

        # Speed (show max of active axes)
        speeds = [state.current_speed.get(a, 0) for a in self._axes]
        max_speed = max(speeds) if speeds else 0
        if self._speed_label:
            self._speed_label.configure(text=f"{max_speed:.0f} step/s")

        # Limit indicators
        for key, circle in self._limit_circles.items():
            triggered = state.limits.get(key, False)
            set_limit_color(circle, triggered)

        # Connected state — dim all data if disconnected
        fg = COLOR_BLUE if state.connected else COLOR_GRAY
        for lbl in self._position_labels.values():
            lbl.configure(foreground=fg)
        for lbl in self._converted_labels.values():
            lbl.configure(foreground=fg)
        if self._speed_label:
            self._speed_label.configure(foreground=fg)

