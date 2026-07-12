"""
Input Manager
==============

Orchestrates the input processing loop at ~60 Hz in a daemon thread.

Each tick:
1. Read current keyboard state (from shared dict)
2. Read current gamepad state (XInput)
3. Resolve actions → list of StageCommands
4. Dispatch commands to InstrumentManager
5. Every Nth tick: poll stage status → push to GUI queue

Also detects gamepad Back/Start special actions:
- Back button → toggle D-pad stage selector
- Start button → toggle enable for D-pad-selected stage
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Dict, Optional

from stage_control.stage_state import StageCommand, StageState
from stage_control.instruments import InstrumentManager
from input_system.keyboard_handler import KeyboardHandler
from input_system.gamepad_handler import GamepadHandler, GamepadState
from input_system.action_resolver import ActionResolver

logger = logging.getLogger("transfer_stage.input")


class InputManager:
    """60 Hz input processing loop.

    Parameters
    ----------
    instruments : InstrumentManager
        The instrument manager for command dispatch and status polling.
    keyboard : KeyboardHandler
        Keyboard state tracker (shared with GUI thread via bind_all).
    gamepad : GamepadHandler
        XInput gamepad handler.
    gui_queue : queue.Queue
        Queue for pushing StageState updates to the GUI drain loop.
    gamepad_status_queue : queue.Queue
        Queue for pushing gamepad connect/disconnect events to the GUI.
    loop_rate_hz : int
        Target loop frequency (default 60).
    status_poll_rate_hz : int
        How often to poll stage status (default 5, i.e. every 200ms).
    """

    def __init__(
        self,
        instruments: InstrumentManager,
        keyboard: KeyboardHandler,
        gamepad: GamepadHandler,
        gui_queue: queue.Queue,
        gamepad_status_queue: queue.Queue,
        loop_rate_hz: int = 60,
        status_poll_rate_hz: int = 5,
        trigger_threshold: float = 0.5,
    ) -> None:
        self._instruments = instruments
        self._keyboard = keyboard
        self._gamepad = gamepad
        self._gui_queue = gui_queue
        self._gamepad_status_queue = gamepad_status_queue

        # Resolver
        self._resolver = ActionResolver(
            long_press_threshold_s=0.300,
            trigger_threshold=trigger_threshold,
            sigmakoki_slow_speed=instruments.sigmakoki_slow_speed,
            sigmakoki_fast_speed=instruments.sigmakoki_fast_speed,
            zolix_slow_speed=instruments.zolix_slow_speed,
            zolix_fast_speed=instruments.zolix_fast_speed,
            sigmakoki_slow_z=instruments.sigmakoki_slow_z,
            sigmakoki_fast_z=instruments.sigmakoki_fast_z,
            zolix_slow_r=instruments.zolix_slow_r,
            zolix_fast_r=instruments.zolix_fast_r,
        )

        # Timing
        self._loop_interval_s = 1.0 / loop_rate_hz
        self._status_poll_interval_s = 1.0 / status_poll_rate_hz

        # State
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._status_thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()

        # Previous keyboard state for short-press detection
        self._prev_key_state: Dict[str, float] = {}

        # Gamepad connection tracking
        self._gamepad_was_connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the input loop and status poller in daemon threads."""
        if self._running:
            return
        self._running = True
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="input_loop",
        )
        self._thread.start()
        self._status_thread = threading.Thread(
            target=self._status_loop, daemon=True, name="status_poller",
        )
        self._status_thread.start()
        logger.info("Input loop started at ~%d Hz", int(1.0 / self._loop_interval_s))

    def stop(self) -> None:
        """Stop the input processing loop and status poller."""
        self._running = False
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._status_thread is not None:
            self._status_thread.join(timeout=2.0)
            self._status_thread = None
        logger.info("Input loop stopped")

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main input processing loop. Runs in a daemon thread."""
        last_status_poll = time.perf_counter()

        while not self._shutdown.is_set():
            frame_start = time.perf_counter()

            try:
                # 1. Read inputs
                key_state = self._keyboard.get_state()
                self._gamepad.update()
                gamepad = self._gamepad.state

                # 2. Resolve
                commands = self._resolver.resolve(key_state, gamepad, now=frame_start)

                # 3. Short-press detection (needs prev frame)
                short_commands = self._resolver.resolve_short_presses(
                    key_state, self._prev_key_state, frame_start,
                )
                commands.extend(short_commands)

                # 4. Handle gamepad special actions
                self._handle_gamepad_special(gamepad, commands)

                # 5. Dispatch commands
                for cmd in commands:
                    self._instruments.execute(cmd)

                # 6. Track gamepad connection changes
                self._check_gamepad_connection(gamepad)

                # 7. Save key state for next frame
                self._prev_key_state = dict(key_state)

            except (ConnectionError, OSError, ValueError) as exc:
                logger.warning("Recoverable error in input loop: %s", exc)
            except Exception:
                logger.exception("Unexpected error in input loop — emergency stop")
                try:
                    self._instruments.stop_all_stages()
                except Exception:
                    pass

            # Frame rate control
            elapsed = time.perf_counter() - frame_start
            sleep_time = self._loop_interval_s - elapsed
            if sleep_time > 0.001:
                self._shutdown.wait(sleep_time)

    # ------------------------------------------------------------------
    # Gamepad Special Actions
    # ------------------------------------------------------------------

    def _handle_gamepad_special(self, gamepad: GamepadState, commands: list) -> None:
        """Handle Back (toggle D-pad stage) and Start (toggle enable).

        These are handled EXCLUSIVELY here — the ActionResolver no longer
        processes special buttons to avoid double-consumption of edge
        detection state.
        """
        # Back → toggle D-pad stage
        if gamepad.just_pressed_back():
            new_stage = "zolix" if self._resolver.dpad_stage == "sigmakoki" else "sigmakoki"
            self._resolver.dpad_stage = new_stage  # uses setter — cleans up old state
            try:
                self._gamepad_status_queue.put_nowait({
                    "type": "dpad_stage", "stage": new_stage,
                })
            except queue.Full:
                pass
            logger.info("Gamepad Back: D-pad now controls %s", new_stage)

        # Start → toggle enable for D-pad-selected stage
        if gamepad.just_pressed_start():
            stage_id = self._resolver.dpad_stage
            new_enabled = self._instruments.toggle_enabled(stage_id)
            self._resolver.update_enabled({stage_id: new_enabled})
            logger.info("Gamepad Start: %s stage %s",
                         stage_id, "enabled" if new_enabled else "disabled")

    def _check_gamepad_connection(self, gamepad: GamepadState) -> None:
        """Detect and report gamepad connect/disconnect events."""
        if gamepad.connected != self._gamepad_was_connected:
            self._gamepad_was_connected = gamepad.connected
            try:
                self._gamepad_status_queue.put_nowait({
                    "type": "gamepad_connection",
                    "connected": gamepad.connected,
                })
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Status Polling
    # ------------------------------------------------------------------

    def _status_loop(self) -> None:
        """Dedicated status polling loop — runs in its own thread so slow
        hardware reads never block the 60 Hz input loop."""
        interval_s = self._status_poll_interval_s
        while not self._shutdown.is_set():
            try:
                self._poll_status()
            except Exception:
                logger.debug("Status poll error", exc_info=True)
            self._shutdown.wait(interval_s)

    def _poll_status(self) -> None:
        """Poll stage limits and positions, push to GUI queue."""
        try:
            states = self._instruments.poll_stage_status()
            for stage_id, state in states.items():
                try:
                    self._gui_queue.put_nowait(state)
                except queue.Full:
                    pass
        except Exception:
            logger.debug("Status poll error", exc_info=True)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def update_speeds(self, trigger_threshold: Optional[float] = None) -> None:
        """Refresh speed settings from InstrumentManager."""
        kwargs = dict(
            sigmakoki_slow=self._instruments.sigmakoki_slow_speed,
            sigmakoki_fast=self._instruments.sigmakoki_fast_speed,
            zolix_slow=self._instruments.zolix_slow_speed,
            zolix_fast=self._instruments.zolix_fast_speed,
            sigmakoki_slow_z=self._instruments.sigmakoki_slow_z,
            sigmakoki_fast_z=self._instruments.sigmakoki_fast_z,
            zolix_slow_r=self._instruments.zolix_slow_r,
            zolix_fast_r=self._instruments.zolix_fast_r,
        )
        if trigger_threshold is not None:
            kwargs["trigger_threshold"] = trigger_threshold
        self._resolver.update_speeds(**kwargs)

    def update_enabled(self, enabled: Dict[str, bool]) -> None:
        """Update per-stage enable state in the resolver."""
        self._resolver.update_enabled(enabled)
