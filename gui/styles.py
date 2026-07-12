"""
GUI style constants — colors, fonts, and ttk theme configuration.

Based on the existing app.py color scheme with extensions
for stage control panels, limit indicators, and gamepad status.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


# ===================================================================
# Color Palette
# ===================================================================

COLOR_OK       = "#2e7d32"   # Green — connected, normal
COLOR_ERROR    = "#c62828"   # Red — disconnected, triggered, alarm
COLOR_WARN     = "#ef6c00"   # Orange — scanning, connecting, warning
COLOR_BLUE     = "#1565c0"   # Blue — data values
COLOR_GRAY     = "#757575"   # Gray — inactive, disabled, unknown
COLOR_BG       = "#fafafa"   # Light gray — window background
COLOR_BG_PANEL = "#ffffff"   # White — panel backgrounds
COLOR_DARK     = "#212121"   # Near black — text
COLOR_LIMIT_OK = "#4caf50"   # Green circle — limit normal
COLOR_LIMIT_HIT = "#f44336"  # Red circle — limit triggered
COLOR_STOP_BTN = "#d32f2f"   # Dark red — STOP ALL button
COLOR_HOME_BTN = "#ff9800"   # Orange — HOME button

# ===================================================================
# Fonts
# ===================================================================

FONT_FAMILY = "Microsoft YaHei"

def _font(size: int, bold: bool = False) -> tuple:
    return (FONT_FAMILY, size, "bold" if bold else "normal")

FONT_LARGE     = _font(40, bold=True)
FONT_MEDIUM    = _font(14)
FONT_SMALL     = _font(10)
FONT_STATUS    = _font(9)
FONT_MONO      = ("Consolas", 11)

# ===================================================================
# ttk Style Setup
# ===================================================================

def setup_styles() -> None:
    """Configure ttk styles for the application."""
    style = ttk.Style()

    # Use a modern theme if available
    available = style.theme_names()
    for preferred in ("vista", "winnative", "clam", "alt", "default"):
        if preferred in available:
            style.theme_use(preferred)
            break

    # General (grey background: window, temp panel, notebook)
    style.configure("TFrame", background=COLOR_BG)
    style.configure("TLabelframe", background=COLOR_BG)
    style.configure("TLabelframe.Label", font=FONT_SMALL, foreground=COLOR_DARK, background=COLOR_BG)

    # Panel frames (white background: stage panels)
    style.configure("Panel.TFrame", background=COLOR_BG_PANEL)
    style.configure("Panel.TLabelframe", background=COLOR_BG_PANEL, borderwidth=1, relief="solid")
    style.configure("Panel.TLabelframe.Label", font=FONT_MEDIUM, foreground=COLOR_DARK, background=COLOR_BG_PANEL)

    # Temp panel (grey background, same title font as stage panels)
    style.configure("Temp.TLabelframe", background=COLOR_BG, borderwidth=1, relief="solid")
    style.configure("Temp.TLabelframe.Label", font=FONT_MEDIUM, foreground=COLOR_DARK, background=COLOR_BG)

    # Buttons
    style.configure("TButton", font=FONT_SMALL, padding=(6, 3))

    # Labels
    style.configure("TLabel", background=COLOR_BG, font=FONT_SMALL, foreground=COLOR_DARK)
    style.configure("Panel.TLabel", background=COLOR_BG_PANEL, font=FONT_SMALL, foreground=COLOR_DARK)
    style.configure("Value.TLabel", background=COLOR_BG_PANEL, font=FONT_MONO, foreground=COLOR_BLUE)

    # Entry
    style.configure("TEntry", font=FONT_MEDIUM)

    # Separator
    style.configure("TSeparator", background="#e0e0e0")

    # Notebook (tabbed dialog) — grey background
    style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
    style.configure("TNotebook.Tab", font=FONT_SMALL, padding=(10, 4), background=COLOR_BG)

    # Checkbutton — grey bg by default
    style.configure("TCheckbutton", background=COLOR_BG, font=FONT_SMALL)
    # Checkbutton on white panels
    style.configure("Panel.TCheckbutton", background=COLOR_BG_PANEL, font=FONT_SMALL)

    # Combobox
    style.configure("TCombobox", font=FONT_SMALL)


# ===================================================================
# Helper: create a colored circle indicator (Canvas)
# ===================================================================

def create_limit_indicator(parent: tk.Widget, size: int = 12) -> tk.Canvas:
    """Create a small circle canvas for limit switch status.

    Returns
    -------
    tk.Canvas
        A small canvas with a circle.  Use ``itemconfigure`` on the
        circle (tag ``"circle"``) to change fill color.
    """
    canvas = tk.Canvas(
        parent, width=size + 4, height=size + 4,
        highlightthickness=0, bg=COLOR_BG_PANEL,
    )
    canvas.create_oval(
        2, 2, size + 2, size + 2,
        fill=COLOR_LIMIT_OK, outline=COLOR_GRAY,
        tags="circle", width=1,
    )
    return canvas


def set_limit_color(canvas: tk.Canvas, triggered: bool) -> None:
    """Set limit indicator color: green=normal, red=triggered."""
    color = COLOR_LIMIT_HIT if triggered else COLOR_LIMIT_OK
    canvas.itemconfigure("circle", fill=color)


# ===================================================================
# Helper: connection status dot
# ===================================================================

def create_status_dot(parent: tk.Widget, size: int = 8) -> tk.Canvas:
    """Create a small dot for connection status."""
    canvas = tk.Canvas(
        parent, width=size + 4, height=size + 4,
        highlightthickness=0, bg=COLOR_BG,
    )
    canvas.create_oval(
        2, 2, size + 2, size + 2,
        fill=COLOR_ERROR, outline="",
        tags="dot",
    )
    return canvas


def set_status_dot(canvas: tk.Canvas, state: str) -> None:
    """Set connection dot color.

    Parameters
    ----------
    state : str
        ``"connected"`` → green, ``"connecting"`` → orange,
        ``"disconnected"`` → red.
    """
    color_map = {
        "connected": COLOR_OK,
        "connecting": COLOR_WARN,
        "disconnected": COLOR_ERROR,
    }
    color = color_map.get(state, COLOR_GRAY)
    canvas.itemconfigure("dot", fill=color)
