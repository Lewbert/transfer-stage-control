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
    # SigmaKoki XYZ — keyboard WASD + gamepad left stick
    "sk_x_pos": LogicalAction("sigmakoki", "x", +1, "SK X+"),
    "sk_x_neg": LogicalAction("sigmakoki", "x", -1, "SK X-"),
    "sk_y_pos": LogicalAction("sigmakoki", "y", +1, "SK Y+"),
    "sk_y_neg": LogicalAction("sigmakoki", "y", -1, "SK Y-"),
    "sk_z_pos": LogicalAction("sigmakoki", "z", +1, "SK Z+"),
    "sk_z_neg": LogicalAction("sigmakoki", "z", -1, "SK Z-"),

    # Zolix XYR — keyboard arrows + gamepad right stick
    "zx_x_pos": LogicalAction("zolix", "x", +1, "ZX X+"),
    "zx_x_neg": LogicalAction("zolix", "x", -1, "ZX X-"),
    "zx_y_pos": LogicalAction("zolix", "y", +1, "ZX Y+"),
    "zx_y_neg": LogicalAction("zolix", "y", -1, "ZX Y-"),
    "zx_r_pos": LogicalAction("zolix", "r", +1, "ZX R+"),
    "zx_r_neg": LogicalAction("zolix", "r", -1, "ZX R-"),
}

# ===================================================================
# Keyboard Mapping
# ===================================================================

# Map tkinter keysym → action id
# Note: WASD = SigmaKoki, Arrows = Zolix
KEYBOARD_MAP: Dict[str, str] = {
    # SigmaKoki stage (WASD)
    "w":       "sk_y_pos",    # W → move Y positive
    "W":       "sk_y_pos",
    "s":       "sk_y_neg",    # S → move Y negative
    "S":       "sk_y_neg",
    "a":       "sk_x_neg",    # A → move X negative
    "A":       "sk_x_neg",
    "d":       "sk_x_pos",    # D → move X positive
    "D":       "sk_x_pos",

    # Zolix stage (arrow keys)
    "Up":      "zx_y_pos",    # Up → move Y positive
    "Down":    "zx_y_neg",    # Down → move Y negative
    "Left":    "zx_x_neg",    # Left → move X negative
    "Right":   "zx_x_pos",    # Right → move X positive

    # Rotation (Q/E) — Zolix R axis
    "q":       "zx_r_neg",    # Q → rotate negative
    "Q":       "zx_r_neg",
    "e":       "zx_r_pos",    # E → rotate positive
    "E":       "zx_r_pos",

    # Z axis (U/J) — SigmaKoki Z axis
    "u":       "sk_z_pos",    # U → Z up
    "U":       "sk_z_pos",
    "j":       "sk_z_neg",    # J → Z down
    "J":       "sk_z_neg",
}

# Keys that act as speed modifiers
SPEED_MODIFIER_KEYS = {"Shift_L", "Shift_R"}

