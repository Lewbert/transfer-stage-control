"""
XInput Gamepad Handler
=======================

Native Windows XInput gamepad support via ctypes.
Zero extra dependencies — calls ``xinput1_4.dll`` directly.

Supports Xbox 360, Xbox One, Xbox Series controllers, and most
third-party "Xbox-compatible" gamepads.

Hot-plug detection via periodic polling of ``XInputGetState``.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_short,
    c_ubyte,
    c_ulong,
    c_ushort,
    windll,
)
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("transfer_stage.gamepad")

# ---------------------------------------------------------------------------
# Win32 Constants
# ---------------------------------------------------------------------------

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

# XInput button bitmask
XINPUT_BUTTON_DPAD_UP    = 0x0001
XINPUT_BUTTON_DPAD_DOWN  = 0x0002
XINPUT_BUTTON_DPAD_LEFT  = 0x0004
XINPUT_BUTTON_DPAD_RIGHT = 0x0008
XINPUT_BUTTON_START       = 0x0010
XINPUT_BUTTON_BACK        = 0x0020
XINPUT_BUTTON_LEFT_THUMB  = 0x0040
XINPUT_BUTTON_RIGHT_THUMB = 0x0080
XINPUT_BUTTON_LEFT_SHOULDER  = 0x0100
XINPUT_BUTTON_RIGHT_SHOULDER = 0x0200
XINPUT_BUTTON_GUIDE       = 0x0400
XINPUT_BUTTON_A           = 0x1000
XINPUT_BUTTON_B           = 0x2000
XINPUT_BUTTON_X           = 0x4000
XINPUT_BUTTON_Y           = 0x8000

# ---------------------------------------------------------------------------
# XInput structs
# ---------------------------------------------------------------------------

class XINPUT_GAMEPAD(Structure):
    """XInput gamepad state (12 bytes)."""
    _fields_ = [
        ("wButtons",       c_ushort),
        ("bLeftTrigger",   c_ubyte),
        ("bRightTrigger",  c_ubyte),
        ("sThumbLX",       c_short),
        ("sThumbLY",       c_short),
        ("sThumbRX",       c_short),
        ("sThumbRY",       c_short),
    ]


class XINPUT_STATE(Structure):
    """XInput state for a single controller (16 bytes)."""
    _fields_ = [
        ("dwPacketNumber", c_ulong),
        ("Gamepad",        XINPUT_GAMEPAD),
    ]


# ---------------------------------------------------------------------------
# Processed gamepad state
# ---------------------------------------------------------------------------

@dataclass
class GamepadState:
    """Normalized gamepad state for consumption by ActionResolver."""

    connected: bool = False

    # Analog sticks — normalized to [-1.0, +1.0]
    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0

    # Triggers — normalized to [0.0, 1.0]
    left_trigger: float = 0.0
    right_trigger: float = 0.0

    # D-pad (mutually exclusive directions)
    dpad_up: bool = False
    dpad_down: bool = False
    dpad_left: bool = False
    dpad_right: bool = False

    # Face buttons (momentary, True if held)
    button_a: bool = False
    button_b: bool = False
    button_x: bool = False
    button_y: bool = False

    # Special buttons
    button_start: bool = False
    button_back: bool = False
    button_left_shoulder: bool = False
    button_right_shoulder: bool = False
    button_left_thumb: bool = False
    button_right_thumb: bool = False

    # Button press tracking (for edge detection)
    # These store previous frame's button state for just_pressed detection
    _prev_buttons: int = 0
    _prev_start: bool = False
    _prev_back: bool = False

    def just_pressed_start(self) -> bool:
        result = self.button_start and not self._prev_start
        self._prev_start = self.button_start
        return result

    def just_pressed_back(self) -> bool:
        result = self.button_back and not self._prev_back
        self._prev_back = self.button_back
        return result

    def raw_buttons(self) -> int:
        return (
            (XINPUT_BUTTON_DPAD_UP    if self.dpad_up else 0) |
            (XINPUT_BUTTON_DPAD_DOWN  if self.dpad_down else 0) |
            (XINPUT_BUTTON_DPAD_LEFT  if self.dpad_left else 0) |
            (XINPUT_BUTTON_DPAD_RIGHT if self.dpad_right else 0) |
            (XINPUT_BUTTON_START      if self.button_start else 0) |
            (XINPUT_BUTTON_BACK       if self.button_back else 0) |
            (XINPUT_BUTTON_A          if self.button_a else 0) |
            (XINPUT_BUTTON_B          if self.button_b else 0) |
            (XINPUT_BUTTON_X          if self.button_x else 0) |
            (XINPUT_BUTTON_Y          if self.button_y else 0) |
            (XINPUT_BUTTON_LEFT_SHOULDER  if self.button_left_shoulder else 0) |
            (XINPUT_BUTTON_RIGHT_SHOULDER if self.button_right_shoulder else 0) |
            (XINPUT_BUTTON_LEFT_THUMB  if self.button_left_thumb else 0) |
            (XINPUT_BUTTON_RIGHT_THUMB if self.button_right_thumb else 0)
        )


# ---------------------------------------------------------------------------
# GamepadHandler
# ---------------------------------------------------------------------------

class GamepadHandler:
    """Reads Xbox-compatible gamepad state via XInput.

    Parameters
    ----------
    deadzone : float
        Minimum stick deflection before registering input (0.0–1.0).
        Default 0.10 = 10%.
    invert : dict
        Per-axis invert flags: ``{"left_x": bool, "left_y": bool,
        "right_x": bool, "right_y": bool}``.

    Usage (from Input Loop thread)::

        handler = GamepadHandler()
        handler.update()          # call each tick (~60 Hz)
        state = handler.state     # read normalized GamepadState
    """

    # Max analog stick value (XInput range: -32768 to +32767)
    STICK_MAX = 32767.0

    # Re-scan interval for hot-plug detection
    RESCAN_INTERVAL_S = 2.0

    def __init__(
        self,
        deadzone: float = 0.10,
        invert: Optional[Dict[str, bool]] = None,
    ) -> None:
        self._deadzone = deadzone
        self._invert = invert or {
            "left_x": False, "left_y": False,
            "right_x": False, "right_y": False,
        }

        # Public state — updated each tick
        self.state = GamepadState()

        # Internal
        self._xinput = None
        self._XInputGetState = None
        self._controller_index = 0  # First controller only
        self._last_rescan = 0.0
        self._lock = threading.Lock()

        # Load XInput DLL
        self._load_xinput()

    # ------------------------------------------------------------------
    # DLL Loading
    # ------------------------------------------------------------------

    def _load_xinput(self) -> bool:
        """Try to load xinput1_4.dll (Win8+) or fall back to xinput1_3.dll."""
        dll_names = ["xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"]
        for name in dll_names:
            try:
                self._xinput = windll.LoadLibrary(name)
                self._XInputGetState = self._xinput.XInputGetState
                self._XInputGetState.argtypes = [c_ulong, POINTER(XINPUT_STATE)]
                self._XInputGetState.restype = c_ulong
                logger.info("Loaded %s for XInput", name)
                return True
            except OSError:
                continue
        logger.warning("Could not load any XInput DLL")
        return False

    # ------------------------------------------------------------------
    # Update — call each tick
    # ------------------------------------------------------------------

    def update(self) -> None:
        """Read current gamepad state. Call at ~60Hz from Input Loop thread.

        Automatically detects hot-plug (connect/disconnect) by
        checking the return code of XInputGetState.
        """
        if self._XInputGetState is None:
            self.state.connected = False
            return

        with self._lock:
            now = time.perf_counter()

            # Periodic re-scan for hot-plug
            if now - self._last_rescan >= self.RESCAN_INTERVAL_S:
                self._last_rescan = now

            xstate = XINPUT_STATE()
            result = self._XInputGetState(self._controller_index, byref(xstate))

            if result == ERROR_SUCCESS:
                was_connected = self.state.connected
                if not was_connected:
                    logger.info("Gamepad connected (index %d)", self._controller_index)
                self.state.connected = True

                # Save previous button state for edge detection
                self.state._prev_buttons = self.state.raw_buttons()

                self._parse_gamepad(xstate.Gamepad)
            else:
                if self.state.connected:
                    logger.info("Gamepad disconnected (%s)", "not found" if result == ERROR_DEVICE_NOT_CONNECTED else f"error {result}")
                self.state.connected = False
                self._zero_state()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_gamepad(self, gamepad: XINPUT_GAMEPAD) -> None:
        """Parse raw XInput gamepad state into normalized GamepadState."""
        s = self.state

        # Analog sticks — normalize to [-1.0, +1.0]
        s.left_x = self._normalize_stick(gamepad.sThumbLX, self._invert["left_x"])
        s.left_y = self._normalize_stick(gamepad.sThumbLY, self._invert["left_y"])
        s.right_x = self._normalize_stick(gamepad.sThumbRX, self._invert["right_x"])
        s.right_y = self._normalize_stick(gamepad.sThumbRY, self._invert["right_y"])

        # Triggers — normalize to [0.0, 1.0]
        s.left_trigger = gamepad.bLeftTrigger / 255.0
        s.right_trigger = gamepad.bRightTrigger / 255.0

        # D-pad
        buttons = gamepad.wButtons
        s.dpad_up    = bool(buttons & XINPUT_BUTTON_DPAD_UP)
        s.dpad_down  = bool(buttons & XINPUT_BUTTON_DPAD_DOWN)
        s.dpad_left  = bool(buttons & XINPUT_BUTTON_DPAD_LEFT)
        s.dpad_right = bool(buttons & XINPUT_BUTTON_DPAD_RIGHT)

        # Face buttons
        s.button_a = bool(buttons & XINPUT_BUTTON_A)
        s.button_b = bool(buttons & XINPUT_BUTTON_B)
        s.button_x = bool(buttons & XINPUT_BUTTON_X)
        s.button_y = bool(buttons & XINPUT_BUTTON_Y)

        # Special buttons
        s.button_start = bool(buttons & XINPUT_BUTTON_START)
        s.button_back  = bool(buttons & XINPUT_BUTTON_BACK)
        s.button_left_shoulder  = bool(buttons & XINPUT_BUTTON_LEFT_SHOULDER)
        s.button_right_shoulder = bool(buttons & XINPUT_BUTTON_RIGHT_SHOULDER)
        s.button_left_thumb  = bool(buttons & XINPUT_BUTTON_LEFT_THUMB)
        s.button_right_thumb = bool(buttons & XINPUT_BUTTON_RIGHT_THUMB)

    def _zero_state(self) -> None:
        """Reset all state fields to zero."""
        s = self.state
        s.left_x = s.left_y = s.right_x = s.right_y = 0.0
        s.left_trigger = s.right_trigger = 0.0
        s.dpad_up = s.dpad_down = s.dpad_left = s.dpad_right = False
        s.button_a = s.button_b = s.button_x = s.button_y = False
        s.button_start = s.button_back = False
        s.button_left_shoulder = s.button_right_shoulder = False
        s.button_left_thumb = s.button_right_thumb = False

    def _normalize_stick(self, raw: int, invert: bool) -> float:
        """Convert raw stick value (-32768..32767) to normalized [-1, +1] with deadzone."""
        value = raw / self.STICK_MAX
        # Clamp
        if value > 1.0:
            value = 1.0
        elif value < -1.0:
            value = -1.0

        # Deadzone
        if abs(value) < self._deadzone:
            return 0.0

        # Rescale so that deadzone→1.0 maps to 0.0→1.0
        sign = 1.0 if value > 0 else -1.0
        value = sign * (abs(value) - self._deadzone) / (1.0 - self._deadzone)

        if invert:
            value = -value

        return value

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def update_config(self, deadzone: float, invert: Dict[str, bool]) -> None:
        """Update runtime config without recreating the handler."""
        self._deadzone = deadzone
        self._invert = invert
