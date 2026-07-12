"""
Preset Dialog — Editable Table
================================

Modal dialog for managing temperature presets using a directly-editable
``ttk.Treeview`` table.  Only presets with non-empty names appear in
the main UI dropdown.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

logger = logging.getLogger("transfer_stage.preset_dialog")


class PresetDialog:
    """Modal table editor for temperature presets."""

    def __init__(self, parent: tk.Widget, presets: List[Dict[str, Any]],
                 temp_lo: float = -200.0, temp_hi: float = 1300.0) -> None:
        self._presets = [dict(p) for p in presets]
        self._result: Optional[List[Dict[str, Any]]] = None
        self._edit_entry: Optional[ttk.Entry] = None
        self._edit_row: Optional[str] = None
        self._edit_col: Optional[int] = None
        self._temp_lo = temp_lo
        self._temp_hi = temp_hi

        self._win = tk.Toplevel(parent)
        self._win.title("Edit Presets")
        self._win.resizable(False, False)
        self._win.transient(parent)
        self._win.grab_set()

        f = ttk.Frame(self._win, padding=8)
        f.pack(fill="both", expand=True)

        # Treeview — name 200px (2/3), temperature 100px (1/3)
        cols = ("name", "temp")
        self._tree = ttk.Treeview(
            f, columns=cols, show="headings", height=8, selectmode="browse",
        )
        self._tree.heading("name", text="Preset Name")
        self._tree.heading("temp", text="Temperature")
        self._tree.column("name", width=200, minwidth=120)
        self._tree.column("temp", width=100, minwidth=60, anchor="center")
        self._tree.pack(fill="both", expand=True, pady=(0, 6))

        # Equal-width buttons with even spacing
        bf = ttk.Frame(f)
        bf.pack(fill="x")
        bf.grid_columnconfigure(0, weight=1, uniform="btn")
        bf.grid_columnconfigure(1, weight=1, uniform="btn")
        bf.grid_columnconfigure(2, weight=1, uniform="btn")
        bf.grid_columnconfigure(3, weight=1, uniform="btn")
        ttk.Button(bf, text="Add Row", command=self._add_row).grid(row=0, column=0, padx=2, sticky="ew")
        ttk.Button(bf, text="Delete", command=self._delete_selected).grid(row=0, column=1, padx=2, sticky="ew")
        ttk.Button(bf, text="OK", command=self._on_ok).grid(row=0, column=2, padx=2, sticky="ew")
        ttk.Button(bf, text="Cancel", command=self._win.destroy).grid(row=0, column=3, padx=2, sticky="ew")

        # Bindings
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Delete>", lambda e: self._delete_selected())
        self._win.bind("<Escape>", lambda e: self._win.destroy())

        self._populate()
        self._center(parent)

    # ==================================================================
    # Table population
    # ==================================================================

    def _populate(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for p in self._presets:
            self._tree.insert("", "end", values=(p["name"], f"{p['temp_c']:.1f} °C"))

    def _sync_from_tree(self) -> None:
        self._cancel_edit()
        self._presets.clear()
        for item in self._tree.get_children():
            name, temp_str = self._tree.item(item, "values")
            name = name.strip()
            temp_str = temp_str.replace("°C", "").strip()
            try:
                temp = round(float(temp_str), 1)
            except (ValueError, TypeError):
                if temp_str:
                    logger.warning("Preset '%s' has invalid temp: %r, defaulting to 25.0", name, temp_str)
                temp = 25.0
            self._presets.append({"name": name, "temp_c": temp})

    # ==================================================================
    # Inline editing
    # ==================================================================

    def _on_double_click(self, event: tk.Event) -> None:
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col_str = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if not item or not col_str:
            return
        col_idx = 0 if col_str == "#1" else 1
        self._start_edit(item, col_idx)

    def _start_edit(self, item: str, col_idx: int) -> None:
        self._cancel_edit()
        try:
            bbox = self._tree.bbox(item, f"#{col_idx + 1}")
            if not bbox:
                return
            x, y, w, h = bbox
        except (ValueError, TypeError):
            return

        current = self._tree.item(item, "values")[col_idx]
        if col_idx == 1:
            current = current.replace("°C", "").strip()

        entry_w = 24 if col_idx == 0 else 10
        self._edit_entry = ttk.Entry(self._tree, width=entry_w, font=("Microsoft YaHei", 10))
        self._edit_entry.insert(0, current)
        self._edit_entry.select_range(0, "end")
        self._edit_entry.place(x=x, y=y, width=w, height=h)
        self._edit_entry.focus_set()
        self._edit_entry.bind("<Return>", lambda e: self._commit_edit())
        self._edit_entry.bind("<Escape>", lambda e: self._cancel_edit())
        self._edit_entry.bind("<FocusOut>", lambda e: self._commit_edit())
        self._edit_row = item
        self._edit_col = col_idx

    def _commit_edit(self) -> None:
        if self._edit_entry is None or self._edit_row is None:
            return
        new_val = self._edit_entry.get().strip()
        values = list(self._tree.item(self._edit_row, "values"))
        if self._edit_col == 1 and new_val:
            try:
                v = float(new_val)
                if v < self._temp_lo or v > self._temp_hi:
                    messagebox.showwarning(
                        "Invalid Temperature",
                        f"Value must be between {self._temp_lo:.0f} °C and {self._temp_hi:.0f} °C.",
                        parent=self._win,
                    )
                    self._edit_entry.focus_set()
                    return
                new_val = f"{v:.1f} °C"
            except ValueError:
                pass
        values[self._edit_col] = new_val
        self._tree.item(self._edit_row, values=values)
        self._cancel_edit()

    def _cancel_edit(self) -> None:
        if self._edit_entry is not None:
            self._edit_entry.destroy()
        self._edit_entry = None
        self._edit_row = None
        self._edit_col = None

    # ==================================================================
    # Row operations
    # ==================================================================

    def _add_row(self) -> None:
        self._cancel_edit()
        item = self._tree.insert("", "end", values=("New Preset", "25.0 °C"))
        self._tree.selection_set(item)
        self._tree.focus(item)
        self._tree.see(item)
        self._start_edit(item, 0)

    def _delete_selected(self) -> None:
        self._cancel_edit()
        sel = self._tree.selection()
        if sel:
            self._tree.delete(sel[0])

    # ==================================================================
    # Result
    # ==================================================================

    def _on_ok(self) -> None:
        self._cancel_edit()
        self._sync_from_tree()
        self._result = self._presets
        self._win.destroy()

    def _center(self, parent: tk.Widget) -> None:
        self._win.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        ww, wh = self._win.winfo_width(), self._win.winfo_height()
        self._win.geometry(f"+{px + (pw - ww) // 2}+{py + (ph - wh) // 2}")

    @property
    def result(self) -> Optional[List[Dict[str, Any]]]:
        return self._result
