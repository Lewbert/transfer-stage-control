"""
Shared data types for the stage control system.

Defines StageCommand (what the input system produces) and
StageState (what the hardware drivers report).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Literal


StageId = Literal["sigmakoki", "zolix"]
AxisName = Literal["x", "y", "z", "r"]
CommandMode = Literal["single_step", "continuous_start", "continuous_stop"]
InputSource = Literal["keyboard", "gamepad_stick", "gamepad_dpad", "gamepad_button", "ui_button"]


@dataclass
class StageCommand:
    """A movement command produced by the input system, consumed by InstrumentManager.

    Attributes
    ----------
    stage_id : str
        ``"sigmakoki"`` or ``"zolix"``.
    axis : str
        ``"x"``, ``"y"``, ``"z"``, or ``"r"``.
    mode : str
        ``"single_step"``, ``"continuous_start"``, or ``"continuous_stop"``.
    direction : int
        +1 for positive, -1 for negative.
    speed : float
        Desired speed in steps per second (or Hz for Arduino speed levels).
        Ignored for ``continuous_stop`` commands.
    source : str
        Where the command originated (for logging / debugging).
    """

    stage_id: StageId
    axis: AxisName
    mode: CommandMode
    direction: int  # +1 or -1
    speed: float
    source: InputSource = "keyboard"

    def __repr__(self) -> str:
        return (
            f"StageCommand({self.stage_id}.{self.axis}, "
            f"{self.mode}, dir={self.direction:+d}, "
            f"speed={self.speed:.0f}, src={self.source})"
        )


@dataclass
class StageState:
    """Snapshot of a stage's current status, updated by hardware polling.

    All position values are in steps (or degrees for rotation).
    """

    stage_id: StageId
    connected: bool = False
    enabled: bool = True

    # Software position tracking (steps, cumulative)
    position: Dict[str, float] = field(default_factory=lambda: {"x": 0, "y": 0, "z": 0, "r": 0.0})

    # Current speed per axis (steps/sec or Hz)
    current_speed: Dict[str, float] = field(default_factory=lambda: {"x": 0, "y": 0, "z": 0, "r": 0.0})

    # Limit switch states — True = triggered (at limit)
    limits: Dict[str, bool] = field(default_factory=lambda: {
        "x+": False, "x-": False,
        "y+": False, "y-": False,
        "z+": False, "z-": False,
    })

    # Per-axis motion flags — True = axis is currently moving
    moving: Dict[str, bool] = field(default_factory=lambda: {"x": False, "y": False, "z": False, "r": False})

    # Zolix-specific: axis alarms and emergency stop
    emergency_stop: bool = False
    axis_alarms: Dict[str, bool] = field(default_factory=lambda: {"x": False, "y": False, "r": False})
    home_switch: Dict[str, bool] = field(default_factory=lambda: {"x": False, "y": False, "r": False})
