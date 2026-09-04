"""
Action Resolver
================

Pure-logic module that converts raw input state (keyboard + gamepad)
into a list of ``StageCommand`` objects for dispatch to the hardware.

Has no hardware or I/O dependencies — fully testable standalone.

Handles:
- Short press → single step (on release, if held < long_press_threshold)
- Long press → continuous start (on threshold reached)
- Release after continuous → continuous stop
- Speed modifier (Shift / shoulder buttons LB+RB)
- LT/RT triggers → focus motor continuous movement (gamma/deadzone mapping)
- Software disable filtering
- Keyboard priority over gamepad for same axis
"""

from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional, Set

from stage_control.stage_state import StageCommand

logger = logging.getLogger("transfer_stage.action_resolver")

from input_system.input_mapping import (
    ACTIONS,
    KEYBOARD_MAP,
    SPEED_MODIFIER_KEYS,
)
from input_system.gamepad_handler import GamepadState

# Focus firmware floor (MIN_SPEED_SPS) — no setter on the firmware side,
# enforced PC-side by the trigger mapping.
FOCUS_FIRMWARE_MIN_SPS = 10


def focus_trigger_to_speed(
    lt: float, rt: float, *,
    min_speed: float, max_speed: float,
    gamma: float = 2.2, deadzone: float = 0.05, invert: bool = False,
) -> int:
    """Map analog triggers to a signed speed in steps/s (0 = stop).

    net = RT − LT (both pressed cancels); ``invert`` negates net.
    |net| <= deadzone → 0.  ``x`` is the deadzone-rescaled magnitude
    0..1; speed rises continuously from ``min_speed`` at the deadzone
    edge to ``max_speed`` at full deflection, shaped by ``gamma``.

    Returns a signed int: +speed for RT net, −speed for LT net.
    """
    net = rt - lt
    if invert:
        net = -net
    mag = abs(net)
    if mag <= deadzone:
        return 0
    x = (mag - deadzone) / (1.0 - deadzone)
    lo = max(FOCUS_FIRMWARE_MIN_SPS, float(min_speed))
    hi = max(lo, float(max_speed))
    speed = lo + (hi - lo) * (x ** float(gamma))
    speed = min(speed, hi)  # guard rounding at x == 1
    return int(round(speed)) if net > 0 else -int(round(speed))


class ActionResolver:
    """Resolves raw input state into stage motion commands.

    Parameters
    ----------
    long_press_threshold_s : float
        Hold duration (seconds) before a press transitions from
        single-step to continuous motion. Default 0.300 (300ms).
    sigmakoki_slow_speed : float
        Slow speed in steps/sec for SigmaKoki.
    sigmakoki_fast_speed : float
        Fast speed in steps/sec for SigmaKoki.
    zolix_slow_speed : float
        Slow speed in steps/sec for Zolix.
    zolix_fast_speed : float
        Fast speed in steps/sec for Zolix.
    trigger_threshold : float
        Gamepad trigger value above which fast speed is engaged (0.0–1.0).
    """

    # 8-direction → (axis, direction) tuples for Zolix right stick
    _DIR_MAP = {
        0: (("x", 1),),           1: (("x", 1), ("y", 1)),
        2: (("y", 1),),           3: (("x", -1), ("y", 1)),
        4: (("x", -1),),          5: (("x", -1), ("y", -1)),
        6: (("y", -1),),          7: (("x", 1), ("y", -1)),
    }

    def __init__(
        self,
        long_press_threshold_s: float = 0.300,
        sigmakoki_slow_speed: float = 200,
        sigmakoki_fast_speed: float = 500,
        zolix_slow_speed: float = 1000,
        zolix_fast_speed: float = 5000,
        sigmakoki_slow_z: float = 200,
        sigmakoki_fast_z: float = 500,
        zolix_slow_r: float = 500,
        zolix_fast_r: float = 2000,
        trigger_threshold: float = 0.5,
        focus_min_speed: float = 50,
        focus_max_speed: float = 2000,
        focus_gamma: float = 2.2,
        focus_deadzone: float = 0.05,
        focus_invert: bool = False,
    ) -> None:
        self._long_press_s = long_press_threshold_s
        # Vestigial since LB/RB replaced the triggers as the fast-mode
        # modifier — kept for signature compatibility (see update_speeds).
        self._trigger_threshold = trigger_threshold

        # Focus trigger mapping (LT/RT → continuous movement)
        self._focus_min = focus_min_speed
        self._focus_max = focus_max_speed
        self._focus_gamma = focus_gamma
        self._focus_deadzone = focus_deadzone
        self._focus_invert = focus_invert
        self._focus_active = False  # trigger currently commanding movement
        self._focus_paused = False  # ESC latch — clears on trigger release

        # ESC latch for re-emitting stick sources (8-dir) — a paused
        # stick id stays suppressed until the stick returns to center.
        self._stick_pause: Set[str] = set()

        # Speed tables
        self._slow_speed = {"sigmakoki": sigmakoki_slow_speed, "zolix": zolix_slow_speed}
        self._fast_speed = {"sigmakoki": sigmakoki_fast_speed, "zolix": zolix_fast_speed}
        # Per-axis speeds for Z and R (configurable, separate from XY)
        self._slow_r = {"zolix": zolix_slow_r}
        self._fast_r = {"zolix": zolix_fast_r}
        self._slow_z = {"sigmakoki": sigmakoki_slow_z}
        self._fast_z = {"sigmakoki": sigmakoki_fast_z}

        # Per-stage enabled state (updated externally by InputManager)
        self._enabled: Dict[str, bool] = {"sigmakoki": True, "zolix": True, "focus": True}

        # Track which keyboard keys are in continuous mode
        # key = "stage_id:axis", value = keysym
        self._continuous_keys: Dict[str, str] = {}

        # Track which axes are in continuous mode via analog stick
        self._continuous_stick: Dict[str, bool] = {}
        # Track last emitted continuous speed to skip redundant re-emissions
        self._continuous_speed: Dict[str, float] = {}

        # Track D-pad stage selector (Back button toggles — set by InputManager)
        self._dpad_stage: str = "sigmakoki"

        # Track gamepad button press times for short/long-press detection
        # key = "dpad:stage:axis:dir" or "btn:stage:axis:dir" → press timestamp
        self._gamepad_press_times: Dict[str, float] = {}

        # 8-direction stick tracking: stick_id → direction (-1=centered)
        self._last_stick_dir: Dict[str, int] = {}
        self._last_stick_fast: Dict[str, bool] = {}
        self._stick_dir_counter: Dict[str, int] = {}  # hysteresis counter

        # Single-step cooldown per (stage_id, axis) — prevents cross-axis blocking
        self._last_single_step_time: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def resolve(
        self,
        key_state: Dict[str, float],
        gamepad: GamepadState,
        now: Optional[float] = None,
        ui_state: Optional[Dict[str, tuple]] = None,
    ) -> List[StageCommand]:
        """Resolve one frame of input state into a list of stage commands.

        *ui_state* maps ``"stage:axis"`` → ``(press_time, direction)`` for
        on-screen button holds (written by the GUI thread, read here).
        """
        if now is None:
            now = time.perf_counter()
        if ui_state is None:
            ui_state = {}

        commands: List[StageCommand] = []

        # Track which axes are claimed by keyboard (for gamepad suppression)
        kb_claimed: Set[str] = set()  # "stage_id:axis"
        ui_claimed: Set[str] = set()  # "stage_id:axis" held by UI buttons

        # ---- Keyboard ----
        shift_held = self._is_shift_held(key_state)
        for keysym, press_time in key_state.items():
            if press_time <= 0:
                continue
            action_id = KEYBOARD_MAP.get(keysym)
            if action_id is None:
                continue
            action = ACTIONS.get(action_id)
            if action is None:
                continue
            if not self._enabled.get(action.stage_id, True):
                continue

            duration = now - press_time
            claim_key = f"{action.stage_id}:{action.axis}"

            if action.stage_id == "focus":
                # Focus has no single_step — start continuous IMMEDIATELY
                # on press so short taps nudge; Shift = fast.
                speed = self._focus_key_speed(fast=shift_held)
                commands.append(StageCommand(
                    stage_id=action.stage_id,
                    axis=action.axis,
                    mode="continuous_start",
                    direction=action.direction,
                    speed=speed,
                    source="keyboard",
                ))
                if claim_key not in self._continuous_keys:
                    self._continuous_keys[claim_key] = keysym
                    self._continuous_speed[claim_key] = speed
                elif speed != self._continuous_speed.get(claim_key, 0):
                    self._continuous_speed[claim_key] = speed
                kb_claimed.add(claim_key)
                continue

            if duration >= self._long_press_s:
                speed = self._get_speed(action.stage_id, action.axis, fast=shift_held)
                if claim_key not in self._continuous_keys:
                    commands.append(StageCommand(
                        stage_id=action.stage_id,
                        axis=action.axis,
                        mode="continuous_start",
                        direction=action.direction,
                        speed=speed,
                        source="keyboard",
                    ))
                    self._continuous_keys[claim_key] = keysym
                    self._continuous_speed[claim_key] = speed
                elif speed != self._continuous_speed.get(claim_key, 0):
                    # Shift toggled mid-hold — re-emit with new speed
                    commands.append(StageCommand(
                        stage_id=action.stage_id,
                        axis=action.axis,
                        mode="continuous_start",
                        direction=action.direction,
                        speed=speed,
                        source="keyboard",
                    ))
                    self._continuous_speed[claim_key] = speed
                kb_claimed.add(claim_key)

        # ---- On-screen UI buttons (long-press holds) ----
        for claim_key, entry in ui_state.items():
            press_time, direction = entry
            if press_time <= 0 or direction == 0:
                continue
            stage_id, axis = claim_key.split(":", 1)
            if not self._enabled.get(stage_id, True):
                continue
            if claim_key in kb_claimed:
                continue  # keyboard has priority over UI
            ui_key = f"ui:{claim_key}"
            speed = self._get_speed(stage_id, axis, fast=shift_held)
            commands.append(StageCommand(
                stage_id=stage_id, axis=axis,
                mode="continuous_start", direction=direction,
                speed=speed, source="ui_button",
            ))
            self._continuous_keys[ui_key] = str(direction)
            self._continuous_speed[ui_key] = speed
            ui_claimed.add(claim_key)

        # Combined claim set — suppresses all gamepad sources
        claimed = kb_claimed | ui_claimed

        # ---- Gamepad: analog sticks ----
        if gamepad.connected:
            self._handle_sticks(gamepad, claimed, commands)
            self._handle_dpad(gamepad, now, claimed, commands)
            self._handle_face_buttons(gamepad, now, claimed, commands)

        # ---- Gamepad: focus triggers (always run — on disconnect the
        # state is zeroed, so this emits the stop for a jog in progress)
        self._handle_triggers(gamepad, claimed, commands)

        # ---- Stop continuous axes that are no longer commanded ----
        self._handle_stops(key_state, gamepad, now, commands,
                           ui_state=ui_state, claimed=claimed)

        return commands

    # ------------------------------------------------------------------
    # Keyboard short-press detection (called after resolve, needs prev state)
    # ------------------------------------------------------------------

    def resolve_short_presses(
        self,
        key_state: Dict[str, float],
        prev_key_state: Dict[str, float],
        now: float,
    ) -> List[StageCommand]:
        """Detect short-press releases → single step commands.

        Call this each frame AFTER ``resolve()``, passing current AND
        previous key states.  Iterates over *prev* state to find keys
        that were pressed last frame but are not pressed now.
        """
        commands = []
        for keysym, prev_press_time in prev_key_state.items():
            if prev_press_time <= 0:
                continue
            # Check if this key is currently released
            if key_state.get(keysym, 0) > 0:
                continue

            action_id = KEYBOARD_MAP.get(keysym)
            if action_id is None:
                continue
            action = ACTIONS.get(action_id)
            if action is None:
                continue
            if not self._enabled.get(action.stage_id, True):
                continue

            claim_key = f"{action.stage_id}:{action.axis}"

            # Focus has no single_step — handled as continuous elsewhere
            if action.stage_id == "focus":
                continue

            # Only emit single-step if released before long-press threshold
            duration = now - prev_press_time
            if duration < self._long_press_s:
                # Don't single-step into an axis that is in continuous
                # motion, and apply the 0.2 s cooldown (same as dpad).
                if self._axis_claimed(action.stage_id, action.axis):
                    continue
                if now - self._last_single_step_time.get(claim_key, 0) <= 0.2:
                    continue
                self._last_single_step_time[claim_key] = now
                commands.append(StageCommand(
                    stage_id=action.stage_id,
                    axis=action.axis,
                    mode="single_step",
                    direction=action.direction,
                    speed=self._get_speed(action.stage_id, action.axis, fast=False),
                    source="keyboard",
                ))

        return commands

    # ------------------------------------------------------------------
    # Keyboard helpers
    # ------------------------------------------------------------------

    def _is_shift_held(self, key_state: Dict[str, float]) -> bool:
        for k in SPEED_MODIFIER_KEYS:
            if key_state.get(k, 0) > 0:
                return True
        return False

    def _focus_key_speed(self, fast: bool) -> float:
        """Keyboard focus speed: fast = focus max_speed;
        slow = max(firmware floor, min_speed, max_speed // 4).
        Defaults (min=50, max=2000) → slow 500, fast 2000 steps/s."""
        if fast:
            return float(self._focus_max)
        return float(max(FOCUS_FIRMWARE_MIN_SPS, self._focus_min, self._focus_max // 4))

    def _axis_claimed(self, stage_id: str, axis: str) -> bool:
        """True if any source is currently driving the axis continuously."""
        for key in self._continuous_keys:
            if key == f"{stage_id}:{axis}" or key.endswith(f":{stage_id}:{axis}"):
                return True
        return f"{stage_id}:{axis}" in self._continuous_stick

    # ------------------------------------------------------------------
    # Gamepad: Analog Sticks
    # ------------------------------------------------------------------

    def _handle_sticks(
        self,
        gamepad: GamepadState,
        claimed: Set[str],
        commands: List[StageCommand],
    ) -> None:
        """Map analog sticks — per-axis analog for Arduino, 8-dir for Zolix."""
        # Left stick → SigmaKoki (analog per-axis — Arduino handles mid-motion changes)
        # Stick axes use raw gamepad values (no negation).
        # X: left_x [-1.0 right, +1.0 left]; Y: left_y [-1.0 up, +1.0 down].
        # The Arduino firmware inverts Y+Z direction pins (axes[i].inverted for i>=1),
        # which is the sole source of hardware direction correction.
        # Use Settings → invert_x / invert_y / invert_z for per-axis reversal.
        if self._enabled.get("sigmakoki", True):
            fast = gamepad.button_left_shoulder
            max_spd = self._fast_speed["sigmakoki"] if fast else self._slow_speed["sigmakoki"]
            self._stick_analog(gamepad.left_x, "sigmakoki", "x", max_spd, claimed, commands)
            self._stick_analog(gamepad.left_y, "sigmakoki", "y", max_spd, claimed, commands)

        # Right stick → Zolix (8-direction)
        if self._enabled.get("zolix", True):
            fast = gamepad.button_right_shoulder
            self._stick_8dir(
                gamepad.right_x, gamepad.right_y, "zolix", "right",
                fast, claimed, commands,
            )

    def _stick_analog(
        self, value: float, stage_id: str, axis: str, max_speed: float,
        claimed: Set[str], commands: List[StageCommand],
    ) -> None:
        """Per-axis analog stick — continuous speed, small 10% dead zone."""
        ck = f"{stage_id}:{axis}"
        if ck in claimed:
            return
        if abs(value) < 0.10:
            if ck in self._continuous_stick:
                commands.append(StageCommand(
                    stage_id=stage_id, axis=axis,
                    mode="continuous_stop", direction=0, speed=0,
                    source="gamepad_stick",
                ))
                del self._continuous_stick[ck]
            return
        direction = 1 if value > 0 else -1
        speed = abs(value) * max_speed
        commands.append(StageCommand(
            stage_id=stage_id, axis=axis,
            mode="continuous_start", direction=direction,
            speed=speed, source="gamepad_stick",
        ))
        self._continuous_stick[ck] = True

    def _handle_triggers(
        self,
        gamepad: GamepadState,
        claimed: Set[str],
        commands: List[StageCommand],
    ) -> None:
        """Map LT/RT triggers to focus-motor continuous movement.

        Re-emits ``continuous_start`` every frame while the triggers
        command a nonzero speed (the driver rate-limits serial sends)
        and emits exactly one ``continuous_stop`` on release.
        """
        if not self._enabled.get("focus", True):
            return
        if self._focus_paused:
            # ESC latch — wait for the triggers to release before
            # allowing re-engagement.
            if focus_trigger_to_speed(
                gamepad.left_trigger, gamepad.right_trigger,
                min_speed=self._focus_min, max_speed=self._focus_max,
                gamma=self._focus_gamma, deadzone=self._focus_deadzone,
                invert=self._focus_invert,
            ) == 0:
                self._focus_paused = False
            return
        if "focus:z" in claimed:
            # Keyboard focus keys own the axis — emit nothing here and
            # don't touch _focus_active (keyboard stop branch handles it).
            return
        speed = focus_trigger_to_speed(
            gamepad.left_trigger, gamepad.right_trigger,
            min_speed=self._focus_min, max_speed=self._focus_max,
            gamma=self._focus_gamma, deadzone=self._focus_deadzone,
            invert=self._focus_invert,
        )
        if speed == 0:
            if self._focus_active:
                commands.append(StageCommand(
                    stage_id="focus", axis="z",
                    mode="continuous_stop", direction=0, speed=0,
                    source="gamepad_trigger",
                ))
                self._focus_active = False
            return
        commands.append(StageCommand(
            stage_id="focus", axis="z",
            mode="continuous_start", direction=1 if speed > 0 else -1,
            speed=float(abs(speed)), source="gamepad_trigger",
        ))
        self._focus_active = True

    def _stick_8dir(
        self, x: float, y: float,
        stage_id: str, stick_id: str,
        fast: bool,
        claimed: Set[str],
        commands: List[StageCommand],
    ) -> None:
        """8-direction stick: 50% dead zone, wide cardinals (60°), narrow diagonals (30°)."""
        _DIR_MAP = ActionResolver._DIR_MAP
        magnitude = math.sqrt(x * x + y * y)
        speed = self._fast_speed[stage_id] if fast else self._slow_speed[stage_id]

        # 50% dead zone — stop only the axes that were actually moving
        if magnitude < 0.50:
            self._stick_pause.discard(stick_id)  # ESC latch clears on re-center
            prev = self._last_stick_dir.pop(stick_id, -1)
            self._stick_dir_counter.pop(stick_id, None)  # reset hysteresis
            if prev >= 0:
                prev_axes = _DIR_MAP.get(prev, ())
                for ax, _dr in prev_axes:
                    ck = f"{stage_id}:{ax}"
                    commands.append(StageCommand(
                        stage_id=stage_id, axis=ax,
                        mode="continuous_stop", direction=0, speed=0,
                        source="gamepad_stick",
                    ))
                    self._continuous_stick.pop(ck, None)
                self._last_stick_fast.pop(stick_id, None)
            return

        if stick_id in self._stick_pause:
            return  # ESC latch — stick must re-center before re-engaging

        # Wide cardinals: abs(x) > 2*abs(y) → X only, abs(y) > 2*abs(x) → Y only
        # tan(63°) ≈ 2.0 — cardinals are ~63° wide, diagonals ~27°
        ax, ay = abs(x), abs(y)
        if ax > 2.0 * ay:
            direction = 0 if x > 0 else 4       # E or W
        elif ay > 2.0 * ax:
            direction = 2 if y > 0 else 6       # N or S
        elif x > 0 and y > 0:
            direction = 1                        # NE
        elif x < 0 and y > 0:
            direction = 3                        # NW
        elif x < 0 and y < 0:
            direction = 5                        # SW
        else:
            direction = 7                        # SE

        prev_dir = self._last_stick_dir.get(stick_id, -1)
        prev_fast = self._last_stick_fast.get(stick_id, False)

        # Direction stable — re-emit the committed axes every frame.
        # The driver's fast path makes this zero serial I/O when nothing
        # changed; if a real failure dropped the commit, the next frame
        # retries and recovers after FAIL_BACKOFF.  Do NOT touch the
        # hysteresis counter here (it only counts ticks toward a NEW
        # direction — resetting it would re-enable the oscillation
        # lock-out below).
        if prev_dir == direction and prev_fast == fast:
            for ax, dr in _DIR_MAP[direction]:
                ck = f"{stage_id}:{ax}"
                if ck in claimed:
                    continue
                commands.append(StageCommand(
                    stage_id=stage_id, axis=ax,
                    mode="continuous_start", direction=dr,
                    speed=speed, source="gamepad_stick",
                ))
                self._continuous_stick[ck] = True
            return

        # Hysteresis: require 2 consecutive ticks in a NEW direction before committing.
        # The counter is only incremented on direction change, never reset on match —
        # this prevents counter oscillation (1→0→1→0) from locking the axis in place
        # when stick noise alternates between two directions near a sector boundary.
        cnt = self._stick_dir_counter.get(stick_id, 0) + 1
        self._stick_dir_counter[stick_id] = cnt
        # Fast path: just exited dead zone (prev_dir == -1) — commit in 1 tick.
        # Direction changes between cardinals still require 2 ticks for debounce.
        threshold = 1 if prev_dir < 0 else 2
        if cnt < threshold:
            return  # not confirmed yet

        self._stick_dir_counter[stick_id] = 0

        # Stop previous movement — only axes that were actually active
        if prev_dir >= 0:
            prev_axes = _DIR_MAP.get(prev_dir, ())
            for ax, _dr in prev_axes:
                ck = f"{stage_id}:{ax}"
                commands.append(StageCommand(
                    stage_id=stage_id, axis=ax,
                    mode="continuous_stop", direction=0, speed=0,
                    source="gamepad_stick",
                ))
                self._continuous_stick.pop(ck, None)

        self._last_stick_dir[stick_id] = direction
        self._last_stick_fast[stick_id] = fast

        for ax, dr in _DIR_MAP[direction]:
            ck = f"{stage_id}:{ax}"
            if ck in claimed:
                continue
            commands.append(StageCommand(
                stage_id=stage_id, axis=ax,
                mode="continuous_start", direction=dr,
                speed=speed,
                source="gamepad_stick",
            ))
            self._continuous_stick[ck] = True

    # ------------------------------------------------------------------
    # Gamepad: D-Pad (with short/long-press detection)
    # ------------------------------------------------------------------

    def _handle_dpad(
        self,
        gamepad: GamepadState,
        now: float,
        claimed: Set[str],
        commands: List[StageCommand],
    ) -> None:
        """Map D-pad to XY movement with short/long-press detection.

        Speed: slow by default, fast when the stage's shoulder button
        is held (LB for SigmaKoki, RB for Zolix).
        """
        stage_id = self._dpad_stage
        if not self._enabled.get(stage_id, True):
            return

        # Shoulder-button fast mode per stage
        fast = (gamepad.button_left_shoulder if stage_id == "sigmakoki"
                else gamepad.button_right_shoulder)
        dpad_speed = self._fast_speed[stage_id] if fast else self._slow_speed[stage_id]

        dpad_dirs = [
            (gamepad.dpad_up, "y", +1),
            (gamepad.dpad_down, "y", -1),
            (gamepad.dpad_left, "x", -1),
            (gamepad.dpad_right, "x", +1),
        ]

        for pressed, axis, direction in dpad_dirs:
            claim_key = f"{stage_id}:{axis}"
            press_key = f"dpad:{claim_key}:{direction}"

            if claim_key in claimed:
                # Higher-priority source owns this axis — suppress entirely;
                # drop any press time so a release can't fire a single-step.
                self._gamepad_press_times.pop(press_key, None)
                continue

            if pressed:
                # Track press time if this is a new press
                if press_key not in self._gamepad_press_times:
                    self._gamepad_press_times[press_key] = now

                duration = now - self._gamepad_press_times[press_key]
                if duration >= self._long_press_s:
                    key = f"dpad:{claim_key}"
                    if key not in self._continuous_keys:
                        commands.append(StageCommand(
                            stage_id=stage_id,
                            axis=axis,
                            mode="continuous_start",
                            direction=direction,
                            speed=dpad_speed,
                            source="gamepad_dpad",
                        ))
                        self._continuous_keys[key] = str(direction)
                        self._continuous_speed[key] = dpad_speed
                    elif dpad_speed != self._continuous_speed.get(key, 0):
                        # Speed changed (trigger toggled) — re-emit
                        commands.append(StageCommand(
                            stage_id=stage_id,
                            axis=axis,
                            mode="continuous_start",
                            direction=direction,
                            speed=dpad_speed,
                            source="gamepad_dpad",
                        ))
                        self._continuous_speed[key] = dpad_speed
            else:
                # Not pressed — will be handled by _handle_stops
                pass

    # ------------------------------------------------------------------
    # Gamepad: Face Buttons (X/Y/A/B) with short/long-press detection
    # ------------------------------------------------------------------

    def _handle_face_buttons(
        self,
        gamepad: GamepadState,
        now: float,
        claimed: Set[str],
        commands: List[StageCommand],
    ) -> None:
        """Map X/Y/A/B with short/long-press + shoulder-button fast mode."""
        # X/Y → rotation (Zolix R) — RB for fast
        if self._enabled.get("zolix", True):
            fast = gamepad.button_right_shoulder
            speed = self._fast_r.get("zolix", 2000) if fast else self._slow_r.get("zolix", 500)
            for pressed, direction, btn_name in [
                (gamepad.button_x, -1, "X"),
                (gamepad.button_y, +1, "Y"),
            ]:
                self._handle_single_gamepad_button(
                    pressed, "zolix", "r", direction, btn_name, now, claimed, commands, speed,
                )

        # A/B → Z axis (SigmaKoki) — LB for fast
        if self._enabled.get("sigmakoki", True):
            fast = gamepad.button_left_shoulder
            speed = self._fast_z.get("sigmakoki", 500) if fast else self._slow_z.get("sigmakoki", 200)
            # One-time diagnostic: log all configured speeds
            if not getattr(self, '_speed_diag_done', False):
                self._speed_diag_done = True
                logger.info("ActionResolver speeds: XY slow=%s fast=%s | Z slow=%s fast=%s | R slow=%s fast=%s",
                            self._slow_speed, self._fast_speed,
                            self._slow_z, self._fast_z,
                            self._slow_r, self._fast_r)
            for pressed, direction, btn_name in [
                (gamepad.button_a, +1, "A"),
                (gamepad.button_b, -1, "B"),
            ]:
                self._handle_single_gamepad_button(
                    pressed, "sigmakoki", "z", direction, btn_name, now, claimed, commands, speed,
                )

    def _handle_single_gamepad_button(
        self,
        pressed: bool,
        stage_id: str,
        axis: str,
        direction: int,
        btn_name: str,
        now: float,
        claimed: Set[str],
        commands: List[StageCommand],
        speed: float = 200,
    ) -> None:
        """Handle short/long-press for a single gamepad button."""
        press_key = f"btn:{stage_id}:{axis}:{direction}"
        claim_key = f"btn:{stage_id}:{axis}"

        if f"{stage_id}:{axis}" in claimed:
            # Higher-priority source (keyboard/UI) owns this axis — suppress
            # entirely; drop any press time so the release cannot fire a
            # single-step either.
            self._gamepad_press_times.pop(press_key, None)
            return

        if pressed:
            if press_key not in self._gamepad_press_times:
                self._gamepad_press_times[press_key] = now

            duration = now - self._gamepad_press_times[press_key]
            if duration >= self._long_press_s:
                if claim_key not in self._continuous_keys:
                    commands.append(StageCommand(
                        stage_id=stage_id,
                        axis=axis,
                        mode="continuous_start",
                        direction=direction,
                        speed=speed,
                        source="gamepad_button",
                    ))
                    self._continuous_keys[claim_key] = btn_name
                    self._continuous_speed[claim_key] = speed
                elif speed != self._continuous_speed.get(claim_key, 0):
                    # Speed changed (trigger toggled) — re-emit
                    commands.append(StageCommand(
                        stage_id=stage_id,
                        axis=axis,
                        mode="continuous_start",
                        direction=direction,
                        speed=speed,
                        source="gamepad_button",
                    ))
                    self._continuous_speed[claim_key] = speed

    # ------------------------------------------------------------------
    # Stop detection
    # ------------------------------------------------------------------

    def _handle_stops(
        self,
        key_state: Dict[str, float],
        gamepad: GamepadState,
        now: float,
        commands: List[StageCommand],
        ui_state: Optional[Dict[str, tuple]] = None,
        claimed: Optional[Set[str]] = None,
    ) -> None:
        """Emit continuous_stop for axes that are no longer commanded,
        and single_step for gamepad buttons released before threshold."""
        if ui_state is None:
            ui_state = {}
        if claimed is None:
            claimed = set()

        # ---- Keyboard stops ----
        for claim_key, keysym in list(self._continuous_keys.items()):
            if claim_key.startswith(("dpad:", "btn:", "ui:")):
                continue
            if keysym in key_state and key_state[keysym] > 0:
                continue
            # Key released → stop (unless a UI button took over the axis)
            stage_id, axis = claim_key.split(":")
            if ui_state.get(claim_key, (0.0, 0))[0] > 0:
                del self._continuous_keys[claim_key]
                self._continuous_speed.pop(claim_key, None)
                continue  # UI still held — its branch re-emits this frame
            commands.append(StageCommand(
                stage_id=stage_id, axis=axis,
                mode="continuous_stop", direction=0, speed=0,
                source="keyboard",
            ))
            del self._continuous_keys[claim_key]
            self._continuous_speed.pop(claim_key, None)

        # ---- UI-button stops ----
        for claim_key in list(self._continuous_keys):
            if not claim_key.startswith("ui:"):
                continue
            parts = claim_key.split(":")
            stage_id, axis = parts[1], parts[2]
            plain = f"{stage_id}:{axis}"
            if ui_state.get(plain, (0.0, 0))[0] > 0:
                continue  # still held
            if plain not in claimed:
                # Released and no higher-priority source took over
                commands.append(StageCommand(
                    stage_id=stage_id, axis=axis,
                    mode="continuous_stop", direction=0, speed=0,
                    source="ui_button",
                ))
            del self._continuous_keys[claim_key]
            self._continuous_speed.pop(claim_key, None)

        # ---- D-pad stops (with single-step on short press) ----
        for press_key, press_time in list(self._gamepad_press_times.items()):
            if not press_key.startswith("dpad:"):
                continue
            # press_key format: "dpad:stage:axis:direction"
            parts = press_key.split(":")
            stage_id, axis, dir_str = parts[1], parts[2], parts[3]
            direction = int(dir_str)
            claim_key = f"dpad:{stage_id}:{axis}"
            claimed_axis = f"{stage_id}:{axis}" in claimed

            # Check if still pressed
            still_pressed = self._is_dpad_direction_pressed(gamepad, axis, direction)
            if still_pressed:
                continue

            # Released — was it short or long?
            held_duration = now - press_time
            if held_duration < self._long_press_s:
                # Short press → single step (200ms cooldown)
                if (self._enabled.get(stage_id, True) and not claimed_axis
                        and now - self._last_single_step_time.get(f"{stage_id}:{axis}", 0) > 0.2):
                    self._last_single_step_time[f"{stage_id}:{axis}"] = now
                    commands.append(StageCommand(
                        stage_id=stage_id, axis=axis,
                        mode="single_step",
                        direction=direction,
                        speed=self._get_speed(stage_id, axis, fast=False),
                        source="gamepad_dpad",
                    ))
            elif claim_key in self._continuous_keys:
                # Long press release → stop (unless a keyboard/UI source
                # took the axis over mid-hold)
                if not claimed_axis:
                    commands.append(StageCommand(
                        stage_id=stage_id, axis=axis,
                        mode="continuous_stop", direction=0, speed=0,
                        source="gamepad_dpad",
                    ))
                del self._continuous_keys[claim_key]
                self._continuous_speed.pop(claim_key, None)

            del self._gamepad_press_times[press_key]

        # ---- Face button stops (with single-step on short press) ----
        for press_key, press_time in list(self._gamepad_press_times.items()):
            if not press_key.startswith("btn:"):
                continue
            parts = press_key.split(":")
            stage_id, axis, dir_str = parts[1], parts[2], parts[3]
            direction = int(dir_str)
            claim_key = f"btn:{stage_id}:{axis}"
            claimed_axis = f"{stage_id}:{axis}" in claimed

            # Check if any button for this axis is still pressed
            still_pressed = self._is_face_button_pressed(gamepad, stage_id, axis, direction)
            if still_pressed:
                continue

            held_duration = now - press_time
            if held_duration < self._long_press_s:
                # Short press → single step (200ms cooldown)
                if (self._enabled.get(stage_id, True) and not claimed_axis
                        and now - self._last_single_step_time.get(f"{stage_id}:{axis}", 0) > 0.2):
                    self._last_single_step_time[f"{stage_id}:{axis}"] = now
                    commands.append(StageCommand(
                        stage_id=stage_id, axis=axis,
                        mode="single_step",
                        direction=direction,
                        speed=self._get_speed(stage_id, axis, fast=False),
                        source="gamepad_button",
                    ))
            elif claim_key in self._continuous_keys:
                if not claimed_axis:
                    commands.append(StageCommand(
                        stage_id=stage_id, axis=axis,
                        mode="continuous_stop", direction=0, speed=0,
                        source="gamepad_button",
                    ))
                del self._continuous_keys[claim_key]
                self._continuous_speed.pop(claim_key, None)

            del self._gamepad_press_times[press_key]

            # Check if another direction button for this axis is still held
            self._restore_other_direction(press_key, stage_id, axis, gamepad, now, claimed, commands)

        # ---- Stick stops are handled inline in _stick_analog / _stick_8dir ----

    # ------------------------------------------------------------------
    # Gamepad state queries for stop detection
    # ------------------------------------------------------------------

    def _is_dpad_direction_pressed(self, gamepad: GamepadState, axis: str, direction: int) -> bool:
        if axis == "x" and direction == +1:
            return gamepad.dpad_right
        if axis == "x" and direction == -1:
            return gamepad.dpad_left
        if axis == "y" and direction == +1:
            return gamepad.dpad_up
        if axis == "y" and direction == -1:
            return gamepad.dpad_down
        return False

    def _is_face_button_pressed(self, gamepad: GamepadState, stage_id: str, axis: str, direction: int) -> bool:
        if stage_id == "zolix" and axis == "r":
            if direction == -1:
                return gamepad.button_x
            if direction == +1:
                return gamepad.button_y
        if stage_id == "sigmakoki" and axis == "z":
            if direction == +1:
                return gamepad.button_a
            if direction == -1:
                return gamepad.button_b
        return False

    def _restore_other_direction(
        self, released_press_key: str, stage_id: str, axis: str,
        gamepad: GamepadState, now: float, claimed: Set[str],
        commands: List[StageCommand],
    ) -> None:
        """If another button for the same axis is still held, re-emit its command."""
        if f"{stage_id}:{axis}" in claimed:
            return  # a keyboard/UI source owns the axis now
        for press_key, press_time in self._gamepad_press_times.items():
            if press_key == released_press_key:
                continue
            if not press_key.startswith("btn:"):
                continue
            parts = press_key.split(":")
            if parts[1] != stage_id or parts[2] != axis:
                continue
            other_dir = int(parts[3])
            # This other button is still held — check if it was already continuous
            duration = now - press_time
            claim_key = f"btn:{stage_id}:{axis}"
            if duration >= self._long_press_s:
                if claim_key not in self._continuous_keys:
                    # Determine fast from the correct shoulder button
                    if stage_id == "sigmakoki":
                        fast = gamepad.button_left_shoulder
                    else:
                        fast = gamepad.button_right_shoulder
                    commands.append(StageCommand(
                        stage_id=stage_id, axis=axis,
                        mode="continuous_start",
                        direction=other_dir,
                        speed=self._get_speed(stage_id, axis, fast),
                        source="gamepad_button",
                    ))
                    self._continuous_keys[claim_key] = parts[3]
                    self._continuous_speed[claim_key] = self._get_speed(stage_id, axis, fast)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_speed(self, stage_id: str, axis: str, fast: bool) -> float:
        if axis == "r":
            return self._fast_r.get(stage_id, 2000) if fast else self._slow_r.get(stage_id, 500)
        if axis == "z":
            return self._fast_z.get(stage_id, 500) if fast else self._slow_z.get(stage_id, 200)
        return self._fast_speed[stage_id] if fast else self._slow_speed[stage_id]

    def update_enabled(self, enabled: Dict[str, bool]) -> None:
        """Update the per-stage enabled state."""
        self._enabled.update(enabled)

    def update_focus_config(
        self,
        min_speed: float,
        max_speed: float,
        gamma: float,
        deadzone: float,
        invert: bool,
    ) -> None:
        """Update the focus trigger-mapping config at runtime."""
        self._focus_min = min_speed
        self._focus_max = max_speed
        self._focus_gamma = gamma
        self._focus_deadzone = deadzone
        self._focus_invert = invert

    def update_speeds(
        self,
        sigmakoki_slow: float,
        sigmakoki_fast: float,
        zolix_slow: float,
        zolix_fast: float,
        sigmakoki_slow_z: float = 200,
        sigmakoki_fast_z: float = 500,
        zolix_slow_r: float = 500,
        zolix_fast_r: float = 2000,
        trigger_threshold: Optional[float] = None,
    ) -> None:
        """Update speed config at runtime."""
        self._slow_speed["sigmakoki"] = sigmakoki_slow
        self._fast_speed["sigmakoki"] = sigmakoki_fast
        self._slow_speed["zolix"] = zolix_slow
        self._fast_speed["zolix"] = zolix_fast
        self._slow_z["sigmakoki"] = sigmakoki_slow_z
        self._fast_z["sigmakoki"] = sigmakoki_fast_z
        self._slow_r["zolix"] = zolix_slow_r
        self._fast_r["zolix"] = zolix_fast_r
        if trigger_threshold is not None:
            self._trigger_threshold = trigger_threshold

    @property
    def dpad_stage(self) -> str:
        """Return which stage the D-pad currently controls."""
        return self._dpad_stage

    @dpad_stage.setter
    def dpad_stage(self, value: str) -> None:
        """Set which stage the D-pad controls.  Cleanup is the caller's job
        — InputManager calls ``pop_dpad_stops()`` BEFORE switching so the
        old stage's axes actually stop."""
        self._dpad_stage = value

    def pop_dpad_stops(self) -> List[StageCommand]:
        """Return continuous_stop commands for active dpad claims and clear
        all dpad tracking (claims, press times, speed cache)."""
        stops: List[StageCommand] = []
        for key in list(self._continuous_keys):
            if not key.startswith("dpad:"):
                continue
            _, stage_id, axis = key.split(":")
            stops.append(StageCommand(
                stage_id=stage_id, axis=axis,
                mode="continuous_stop", direction=0, speed=0,
                source="gamepad_dpad",
            ))
            del self._continuous_keys[key]
        for tracking in (self._gamepad_press_times, self._continuous_speed):
            for key in list(tracking):
                if key.startswith("dpad:"):
                    del tracking[key]
        return stops

    def cancel_all_continuous(self) -> None:
        """Escape / STOP ALL: drop every continuous claim and latch the
        re-emitting sources (8-dir sticks, focus triggers) until their
        input returns to neutral, so a still-held input cannot instantly
        restart an axis after an emergency stop."""
        self._continuous_keys.clear()
        self._continuous_speed.clear()
        self._continuous_stick.clear()
        self._gamepad_press_times.clear()
        self._stick_pause.update(self._last_stick_dir.keys())
        self._last_stick_dir.clear()
        self._last_stick_fast.clear()
        self._stick_dir_counter.clear()
        self._focus_paused = True
        self._focus_active = False

    @property
    def continuous_keys(self) -> Dict[str, str]:
        """Return copy of active continuous key map (for debugging)."""
        return dict(self._continuous_keys)
