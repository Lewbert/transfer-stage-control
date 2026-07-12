"""
Temperature Panel
==================

Display-only widget — creates and arranges labels, entries, buttons.
All polling, data flow, and display-update logic lives in MainWindow,
matching the proven architecture of the original ``app.py``.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict

from gui.styles import (
    COLOR_OK, COLOR_ERROR, COLOR_BLUE, COLOR_GRAY,
    FONT_LARGE, FONT_MEDIUM, FONT_SMALL, FONT_STATUS,
)

FONT_PV = FONT_LARGE


class TemperaturePanel(ttk.LabelFrame):
    """Display-only temperature panel.  MainWindow drives all updates."""

    def __init__(
        self,
        parent: tk.Widget,
        settings: Dict[str, Any],
        on_apply: Callable[[float], None],
        on_presets_changed: Callable[[list], None],
    ) -> None:
        super().__init__(parent, text="TEMPERATURE", padding=4, style="Temp.TLabelframe")
        self._settings = settings
        self._on_apply_cb = on_apply
        self._on_presets_changed = on_presets_changed

        self._sv_safety_checked = False
        self._safety_lo = settings.get("safety_temp_lo_c", -100.0)
        self._safety_hi = settings.get("safety_temp_hi_c", 400.0)

        self._build()

    # ==================================================================
    # Layout
    # ==================================================================

    def _build(self) -> None:
        # PV
        pv_frame = ttk.Frame(self)
        pv_frame.pack(fill=tk.X, pady=(4, 6))
        self._pv_label = ttk.Label(pv_frame, text="--.- °C", font=FONT_PV, foreground=COLOR_GRAY)
        self._pv_label.pack()

        # Target
        sv_frame = ttk.Frame(self)
        sv_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(sv_frame, text="Target", font=FONT_SMALL, foreground=COLOR_GRAY).pack(anchor="w")
        entry_row = ttk.Frame(sv_frame)
        entry_row.pack(fill=tk.X, pady=(3, 4))
        self._sv_var = tk.StringVar(value="--.-")
        self._sv_entry = ttk.Entry(
            entry_row, textvariable=self._sv_var, font=FONT_MEDIUM,
            width=7, justify="center", state="disabled",
        )
        self._sv_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._sv_entry.bind("<Return>", lambda e: self._apply())
        ttk.Label(entry_row, text="°C", font=FONT_MEDIUM).pack(side=tk.LEFT, padx=(6, 0))
        self._apply_btn = ttk.Button(
            sv_frame, text="Apply", command=self._apply, state="disabled",
        )
        self._apply_btn.pack(fill=tk.X)
        self._sv_error_label = ttk.Label(
            sv_frame, text="", font=FONT_STATUS, foreground=COLOR_ERROR,
        )
        self._sv_error_label.pack(anchor="w", pady=(2, 0))

        # Presets
        preset_frame = ttk.Frame(self)
        preset_frame.pack(fill=tk.X, pady=(0, 4))
        self._preset_var = tk.StringVar()
        self._preset_combo = ttk.Combobox(
            preset_frame, textvariable=self._preset_var, state="readonly", width=22,
        )
        self._preset_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        ttk.Button(preset_frame, text="⚙", width=3, command=self._on_edit_presets,
                   ).pack(side=tk.LEFT, padx=(4, 0))
        self._refresh_preset_list()

        # Output
        mv_frame = ttk.Frame(self)
        mv_frame.pack(fill=tk.X, pady=(0, 6))
        self._mv_label = ttk.Label(mv_frame, text="Output: --.- %", font=FONT_SMALL, foreground=COLOR_GRAY)
        self._mv_label.pack(anchor="w")

        # Status
        self._status_label = ttk.Label(
            self, text="● Disconnected", font=FONT_STATUS, foreground=COLOR_ERROR,
        )
        self._status_label.pack(fill=tk.X)

    # ==================================================================
    # Public setters — called by MainWindow's polling logic
    # ==================================================================

    def set_pv(self, value: float, color: str = COLOR_BLUE) -> None:
        self._pv_label.configure(text=f"{value:.1f} °C", foreground=color)

    def set_mv(self, value: float) -> None:
        self._mv_label.configure(text=f"Output: {value:.1f} %", foreground=COLOR_BLUE)

    def set_sv(self, value: float) -> None:
        try:
            current = float(self._sv_var.get())
        except ValueError:
            current = None
        if current != value:
            self._sv_entry.configure(state="normal")
            self._sv_entry.delete(0, "end")
            self._sv_entry.insert(0, f"{value:.1f}")

    def set_connected(self) -> None:
        self._sv_entry.configure(state="normal")
        self._apply_btn.configure(state="normal")
        self._status_label.configure(text="● Connected", foreground=COLOR_OK)

    def set_disconnected(self) -> None:
        self._sv_entry.configure(state="disabled")
        self._apply_btn.configure(state="disabled")
        self._pv_label.configure(text="--.- °C", foreground=COLOR_GRAY)
        self._mv_label.configure(text="Output: --.- %", foreground=COLOR_GRAY)
        self._status_label.configure(text="● Disconnected", foreground=COLOR_ERROR)
        self._sv_safety_checked = False  # re-sync SV on reconnect

    def update_safety_limits(self, lo: float, hi: float) -> None:
        self._safety_lo = lo
        self._safety_hi = hi

    def set_error(self, msg: str) -> None:
        self._status_label.configure(text=f"● Error: {msg}", foreground=COLOR_ERROR)

    def show_sv_error(self, msg: str, color: str = COLOR_ERROR) -> None:
        self._sv_error_label.configure(text=msg, foreground=color)

    def clear_sv_error(self) -> None:
        self._sv_error_label.configure(text="")

    def flash_apply_ok(self) -> None:
        self._apply_btn.configure(text="✓ Applied")
        self.winfo_toplevel().after(1200, lambda: self._apply_btn.configure(text="Apply"))

    def get_safety_limits(self) -> tuple:
        return self._safety_lo, self._safety_hi

    @property
    def sv_safety_checked(self) -> bool:
        return self._sv_safety_checked

    @sv_safety_checked.setter
    def sv_safety_checked(self, v: bool) -> None:
        self._sv_safety_checked = v

    # ==================================================================
    # Presets (self-contained — no hardware dependency)
    # ==================================================================

    def _refresh_preset_list(self) -> None:
        presets = self._settings.get("presets", [])
        active = [p for p in presets if p.get("name", "").strip()]
        self._preset_combo["values"] = ["  -- Custom --"] + [
            f"{p['name']}  —  {p['temp_c']:.1f} °C" for p in active
        ]
        self._preset_combo.current(0)

    def _on_preset_selected(self, event=None) -> None:
        idx = self._preset_combo.current()
        if idx <= 0:
            return
        # Use same filtered list as _refresh_preset_list to keep index in sync
        active = [p for p in self._settings.get("presets", []) if p.get("name", "").strip()]
        pi = idx - 1
        if 0 <= pi < len(active):
            t = active[pi]["temp_c"]
            self._sv_entry.configure(state="normal")
            self._sv_entry.delete(0, "end")
            self._sv_entry.insert(0, f"{t:.1f}")
            self._apply()

    def _on_edit_presets(self) -> None:
        from gui.preset_dialog import PresetDialog
        dlg = PresetDialog(
            self, self._settings.get("presets", []),
            temp_lo=self._safety_lo, temp_hi=self._safety_hi,
        )
        self.wait_window(dlg._win)
        if dlg.result is not None:
            self._settings["presets"] = dlg.result
            self._refresh_preset_list()
            if self._on_presets_changed:
                self._on_presets_changed(self._settings["presets"])

    def _apply(self) -> None:
        raw = self._sv_var.get().strip()
        try:
            value = float(raw)
        except ValueError:
            self._sv_error_label.configure(text=f"Invalid: {raw!r}")
            return
        lo, hi = self._safety_lo, self._safety_hi
        if value < lo:
            self._sv_error_label.configure(text=f"Below safety min ({lo}°C)")
            return
        if value > hi:
            self._sv_error_label.configure(text=f"Above safety max ({hi}°C)")
            return
        self._apply_btn.configure(state="disabled")
        self._sv_entry.configure(state="disabled")
        if self._on_apply_cb:
            self._on_apply_cb(value)

    def reenable_apply(self) -> None:
        self._apply_btn.configure(state="normal")
        self._sv_entry.configure(state="normal")
