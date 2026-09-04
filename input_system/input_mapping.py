"""
Input Mapping Configuration
============================

Defines the mapping from physical keys/buttons to logical actions.
These are the default bindings; most can be overridden via settings.json.

All key names use tkinter ``keysym`` conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# ===================================================================
# Logical action identifiers
# ===================================================================


@dataclass(frozen=True)
class LogicalAction:
    """A logical input action independent of physical input device."""

    stage_id: str      # "sigmakoki" or "zolix"
    axis: str          # "x", "y", "z", "r"
    direction: int     # +1 or -1
    label: str         # Human-readable short label


# Define all possible logical actions
ACTIONS = {
    # SigmaKoki XYZ — keyboard arrows (XY) / R+F (Z) + gamepad left stick
    "sk_x_pos": LogicalAction("sigmakoki", "x", +1, "SK X+"),
    "sk_x_neg": LogicalAction("sigmakoki", "x", -1, "SK X-"),
    "sk_y_pos": LogicalAction("sigmakoki", "y", +1, "SK Y+"),
    "sk_y_neg": LogicalAction("sigmakoki", "y", -1, "SK Y-"),
    "sk_z_pos": LogicalAction("sigmakoki", "z", +1, "SK Z+"),
    "sk_z_neg": LogicalAction("sigmakoki", "z", -1, "SK Z-"),

    # Zolix XYR — keyboard WASD (XY) + Q/E (R) + gamepad right stick
    "zx_x_pos": LogicalAction("zolix", "x", +1, "ZX X+"),
    "zx_x_neg": LogicalAction("zolix", "x", -1, "ZX X-"),
    "zx_y_pos": LogicalAction("zolix", "y", +1, "ZX Y+"),
    "zx_y_neg": LogicalAction("zolix", "y", -1, "ZX Y-"),
    "zx_r_pos": LogicalAction("zolix", "r", +1, "ZX R+"),
    "zx_r_neg": LogicalAction("zolix", "r", -1, "ZX R-"),

    # Focus Z — keyboard +/− keys
    "fc_z_pos": LogicalAction("focus", "z", +1, "Focus Z+"),
    "fc_z_neg": LogicalAction("focus", "z", -1, "Focus Z-"),
}

# ===================================================================
# Keyboard Mapping
# ===================================================================

# Map tkinter keysym → action id
# Note: WASD = Zolix X/Y, Arrows = SigmaKoki X/Y, Q/E = Zolix R,
#       R/F = SigmaKoki Z, +/- = Focus Z (Shift = fast).
# Both letter cases are listed; the handler also folds case at runtime.
KEYBOARD_MAP: Dict[str, str] = {
    # Zolix XYR stage (WASD)
    "w":       "zx_y_pos",    # W → Zolix Y positive
    "W":       "zx_y_pos",
    "s":       "zx_y_neg",    # S → Zolix Y negative
    "S":       "zx_y_neg",
    "a":       "zx_x_neg",    # A → Zolix X negative
    "A":       "zx_x_neg",
    "d":       "zx_x_pos",    # D → Zolix X positive
    "D":       "zx_x_pos",

    # Zolix R rotation (Q/E)
    "q":       "zx_r_neg",    # Q → rotate negative
    "Q":       "zx_r_neg",
    "e":       "zx_r_pos",    # E → rotate positive
    "E":       "zx_r_pos",

    # SigmaKoki Z axis (R/F)
    "r":       "sk_z_pos",    # R → SK Z up
    "R":       "sk_z_pos",
    "f":       "sk_z_neg",    # F → SK Z down
    "F":       "sk_z_neg",

    # SigmaKoki XY (arrow keys)
    "Up":      "sk_y_pos",    # Up → SK Y positive
    "Down":    "sk_y_neg",    # Down → SK Y negative
    "Left":    "sk_x_neg",    # Left → SK X negative
    "Right":   "sk_x_pos",    # Right → SK X positive

    # Focus Z (+/− keys).  On a US layout "+" is Shift+"=", so "equal"
    # is the unshifted bind and Shift naturally acts as the fast modifier.
    "equal":       "fc_z_pos",
    "plus":        "fc_z_pos",
    "KP_Add":      "fc_z_pos",
    "minus":       "fc_z_neg",
    "underscore":  "fc_z_neg",
    "KP_Subtract": "fc_z_neg",
}

# Keys that act as speed modifiers
SPEED_MODIFIER_KEYS = {"Shift_L", "Shift_R"}

