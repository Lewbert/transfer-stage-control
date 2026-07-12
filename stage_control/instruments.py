"""
InstrumentManager
=================

Owns all three hardware drivers (SigmaKoki, Zolix, Yudian) and
coordinates their lifecycle: connection, command dispatch, and
status polling.

Command dispatch respects per-stage software enable/disable.
Status polling runs in the input loop thread and pushes
``StageState`` updates to a queue consumed by the GUI.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

from stage_control.stage_state import StageCommand, StageState
from stage_control.hardware.sigmakoki import SigmaKokiDriver, SPEED_LEVEL_TO_HZ
from stage_control.hardware.zolix import ZolixDriver
from stage_control.hardware.yudian import (
    YudianController,
    YudianCommunicationError,
    YudianConnectionError,
)

logger = logging.getLogger("transfer_stage.instruments")


class InstrumentManager:
    """Central manager for all three instruments.

    Parameters
    ----------
    sigmakoki_config : dict
        Config dict from settings.json ``["sigmakoki"]``.
    zolix_config : dict
        Config dict from settings.json ``["zolix"]``.
    yudian_config : dict
        Config dict from settings.json ``["yudian"]``.
    """

    def __init__(
        self,
        sigmakoki_config: Dict[str, Any],
        zolix_config: Dict[str, Any],
        yudian_config: Dict[str, Any],
    ) -> None:
        # Drivers
        self.sigmakoki = SigmaKokiDriver(
            port=sigmakoki_config.get("port", ""),
            baudrate=sigmakoki_config.get("baudrate", 115200),
            timeout=sigmakoki_config.get("timeout_s", 0.3),
        )
        self.zolix = ZolixDriver(
            port=zolix_config.get("port", ""),
            slave_address=zolix_config.get("slave_address", 1),
            baudrate=zolix_config.get("baudrate", 115200),
            timeout=zolix_config.get("timeout_s", 0.05),
            stop_mode=zolix_config.get("stop_mode", "immediate"),
        )
        self.yudian = YudianController(
            port=yudian_config.get("port", ""),
            slave_address=yudian_config.get("slave_address", 1),
            baudrate=yudian_config.get("baudrate", 9600),
            timeout=yudian_config.get("timeout_s", 0.5),
        )

        # Config for speed reference
        self._sigmakoki_slow = sigmakoki_config.get("slow_speed_hz", 200)
        self._sigmakoki_fast = sigmakoki_config.get("fast_speed_hz", 500)
        self._sigmakoki_invert = {
            "x": sigmakoki_config.get("invert_x", False),
            "y": sigmakoki_config.get("invert_y", False),
            "z": sigmakoki_config.get("invert_z", False),
        }
        self._sigmakoki_flip_xy = sigmakoki_config.get("flip_xy", False)
        self._sigmakoki_step = sigmakoki_config.get("single_step_amount", 10)
        self._sigmakoki_step_z = sigmakoki_config.get("single_step_z",
            sigmakoki_config.get("single_step_amount", 10))
        self._sigmakoki_slow_z = sigmakoki_config.get("slow_speed_z", 200)
        self._sigmakoki_fast_z = sigmakoki_config.get("fast_speed_z", 500)

        self._zolix_slow = zolix_config.get("slow_speed_pps", 1000)
        self._zolix_fast = zolix_config.get("fast_speed_pps", 5000)
        self._zolix_slow_r = zolix_config.get("slow_speed_r", 500)
        self._zolix_fast_r = zolix_config.get("fast_speed_r", 2000)
        self._zolix_invert = {
            "x": zolix_config.get("invert_x", False),
            "y": zolix_config.get("invert_y", False),
            "r": zolix_config.get("invert_r", False),
        }
        self._zolix_flip_xy = zolix_config.get("flip_xy", False)
        self._zolix_step = zolix_config.get("single_step_amount", 100)
        self._zolix_step_r = zolix_config.get("single_step_r",
            zolix_config.get("single_step_amount", 100))

        # Software enable/disable per stage
        self._enabled: Dict[str, bool] = {"sigmakoki": True, "zolix": True}

        # Per-axis "currently in continuous move" tracking
        # (for auto-stop when key is released)
        self._continuous_active: Dict[str, Dict[str, bool]] = {
            "sigmakoki": {"x": False, "y": False, "z": False},
            "zolix": {"x": False, "y": False, "r": False},
        }

        # Connection state tracking
        self._connecting = threading.Event()
        self._zolix_last_state: Optional[StageState] = None  # cache for idle-poll skip

    # ------------------------------------------------------------------
    # Connection Lifecycle
    # ------------------------------------------------------------------

    def connect_all(
        self,
        status_queue: queue.Queue,
        connect_sigmakoki: bool = True,
        connect_zolix: bool = True,
        connect_yudian: bool = True,
    ) -> None:
        """Connect to all configured devices in background threads.

        Each successful/failed connection posts a status dict to *status_queue*:
        ``{"device": "sigmakoki"|"zolix"|"yudian", "connected": bool, "error": str|None}``
        """
        if self._connecting.is_set():
            return
        self._connecting.set()

        threads_started = 0

        def _connect_one(device_name: str, driver, connect_fn):
            nonlocal threads_started
            try:
                if driver._port:
                    connect_fn()
                    status_queue.put({"device": device_name, "connected": True, "error": None})
                    logger.info("%s connected", device_name)
                else:
                    status_queue.put({"device": device_name, "connected": False, "error": "No port configured"})
            except Exception as exc:
                status_queue.put({"device": device_name, "connected": False, "error": str(exc)})
                logger.warning("%s connection failed: %s", device_name, exc)

        threads = []
        if connect_sigmakoki:
            threads_started += 1
            t = threading.Thread(
                target=_connect_one,
                args=("sigmakoki", self.sigmakoki, self.sigmakoki.connect),
                daemon=True,
                name="connect_sigmakoki",
            )
            threads.append(t)
            t.start()

        if connect_zolix:
            threads_started += 1
            t = threading.Thread(
                target=_connect_one,
                args=("zolix", self.zolix, self.zolix.connect),
                daemon=True,
                name="connect_zolix",
            )
            threads.append(t)
            t.start()

        if connect_yudian:
            threads_started += 1
            t = threading.Thread(
                target=_connect_one,
                args=("yudian", self.yudian, self.yudian.connect),
                daemon=True,
                name="connect_yudian",
            )
            threads.append(t)
            t.start()

        if threads_started == 0:
            self._connecting.clear()

        # Background thread to clear the flag when all connect threads finish
        def _clear_when_done():
            for t in threads:
                t.join(timeout=15.0)
            self._connecting.clear()

        threading.Thread(target=_clear_when_done, daemon=True, name="connect_cleanup").start()

    def disconnect_all(self) -> None:
        """Disconnect all devices, stopping all motion first."""
        for driver in [self.sigmakoki, self.zolix]:
            try:
                driver.stop_all()
            except Exception:
                pass
        for driver in [self.sigmakoki, self.zolix, self.yudian]:
            try:
                driver.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Command Dispatch
    # ------------------------------------------------------------------

    def execute(self, command: StageCommand) -> bool:
        """Dispatch a single StageCommand to the appropriate driver.

        Returns
        -------
        bool
            ``True`` if the command was dispatched, ``False`` if it was
            dropped (stage disabled, not connected, at limit, etc.).
        """
        stage_id = command.stage_id
        axis = command.axis

        # Software enable check
        if not self._enabled.get(stage_id, True):
            return False

        # Get the right driver
        driver = self._get_driver(stage_id)
        if driver is None or not driver.is_connected:
            return False

        # Apply flip XY if configured (swap X ↔ Y axis before inversion)
        flip_xy = self._sigmakoki_flip_xy if stage_id == "sigmakoki" else self._zolix_flip_xy
        if flip_xy and axis in ("x", "y"):
            axis = "y" if axis == "x" else "x"

        # Apply inversion if configured
        direction = command.direction
        invert_map = self._sigmakoki_invert if stage_id == "sigmakoki" else self._zolix_invert
        if invert_map.get(axis, False):
            direction = -direction

        # Dispatch by mode
        if command.mode == "continuous_start":
            speed = command.speed
            success = driver.continuous_start(axis, direction, speed)
            if success:
                self._continuous_active[stage_id][axis] = True
            else:
                logger.debug("%s %s: continuous_start rejected", stage_id, axis)
            return success

        elif command.mode == "continuous_stop":
            # Always dispatch stop — tracking state may be stale
            driver.continuous_stop(axis)
            self._continuous_active[stage_id][axis] = False
            return True

        elif command.mode == "single_step":
            if stage_id == "sigmakoki":
                step_amount = self._sigmakoki_step_z if axis == "z" else self._sigmakoki_step
            else:
                step_amount = self._zolix_step_r if axis == "r" else self._zolix_step
            result = driver.single_step(axis, direction, step_amount)
            return result != 0

        return False

    def stop_all_stages(self) -> None:
        """Emergency stop ALL axes on both stages."""
        for driver in [self.sigmakoki, self.zolix]:
            try:
                driver.stop_all()
            except Exception:
                pass
        for stage in ("sigmakoki", "zolix"):
            for axis in self._continuous_active[stage]:
                self._continuous_active[stage][axis] = False

    # ------------------------------------------------------------------
    # Software Enable / Disable
    # ------------------------------------------------------------------

    def set_enabled(self, stage_id: str, enabled: bool) -> None:
        """Enable or disable a stage in software.

        When disabled, all commands for that stage are silently dropped.
        Any active continuous moves are stopped.
        """
        self._enabled[stage_id] = enabled
        if not enabled:
            driver = self._get_driver(stage_id)
            if driver:
                try:
                    driver.stop_all()
                except Exception:
                    pass
            for axis in self._continuous_active[stage_id]:
                self._continuous_active[stage_id][axis] = False

    def is_enabled(self, stage_id: str) -> bool:
        return self._enabled.get(stage_id, True)

    def toggle_enabled(self, stage_id: str) -> bool:
        """Toggle enable state. Returns the new state."""
        new_state = not self._enabled.get(stage_id, True)
        self.set_enabled(stage_id, new_state)
        return new_state

    # ------------------------------------------------------------------
    # Status Polling
    # ------------------------------------------------------------------

    def poll_stage_status(self) -> Dict[str, StageState]:
        """Poll both stages for current status.

        Called from the input loop at ~5 Hz.

        Returns
        -------
        dict
            ``{"sigmakoki": StageState, "zolix": StageState}``
        """
        results = {}
        for stage_id, driver in [("sigmakoki", self.sigmakoki), ("zolix", self.zolix)]:
            state = StageState(stage_id=stage_id)
            state.enabled = self._enabled.get(stage_id, True)
            state.connected = driver.is_connected

            if driver.is_connected:
                try:
                    if stage_id == "sigmakoki":
                        self._poll_sigmakoki(driver, state)
                    else:
                        # Only poll Zolix when idle for 2+ seconds
                        if time.time() - driver.last_command_time >= 2.0:
                            self._poll_zolix(driver, state)
                            # Cache last known good state
                            self._zolix_last_state = state
                        elif self._zolix_last_state is not None:
                            # Reuse last known state — no zeros
                            cached = self._zolix_last_state
                            state.position = dict(cached.position)
                            state.limits = dict(cached.limits)
                            state.moving["x"] = driver._moving.get("x", False)
                            state.moving["y"] = driver._moving.get("y", False)
                            state.moving["r"] = driver._moving.get("r", False)
                            state.current_speed["x"] = driver.cached_speed.get("x", 0)
                            state.current_speed["y"] = driver.cached_speed.get("y", 0)
                            state.current_speed["r"] = driver.cached_speed.get("r", 0)
                            state.connected = True
                            state.enabled = self._enabled.get(stage_id, True)
                except Exception as exc:
                    logger.debug("Status poll failed for %s: %s", stage_id, exc)

            results[stage_id] = state
        return results

    def poll_temperature(self) -> Optional[Dict[str, float]]:
        """Poll the Yudian temperature controller.

        Called at ~2 Hz from the temperature polling thread.

        Returns
        -------
        dict or None
            ``{"pv": float, "sv": float, "mv": float}`` or ``None`` on error.
        """
        if not self.yudian.is_connected:
            return None
        try:
            return self.yudian.read_all()
        except (YudianCommunicationError, YudianConnectionError) as exc:
            logger.debug("Temperature poll failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_driver(self, stage_id: str):
        if stage_id == "sigmakoki":
            return self.sigmakoki
        elif stage_id == "zolix":
            return self.zolix
        return None

    def _poll_sigmakoki(self, driver: SigmaKokiDriver, state: StageState) -> None:
        """Fill StageState from SigmaKoki driver."""
        try:
            status = driver.get_status()
            limits = driver.get_limits()

            state.position["x"] = status.get("x", 0)
            state.position["y"] = status.get("y", 0)
            state.position["z"] = status.get("z", 0)

            # Map speed level → Hz
            xspd = status.get("xspd", 2)
            yspd = status.get("yspd", 2)
            zspd = status.get("zspd", 2)
            state.current_speed["x"] = SPEED_LEVEL_TO_HZ.get(xspd, 100)
            state.current_speed["y"] = SPEED_LEVEL_TO_HZ.get(yspd, 100)
            state.current_speed["z"] = SPEED_LEVEL_TO_HZ.get(zspd, 100)

            state.limits["x+"] = limits.get("x+", 0) == 1
            state.limits["x-"] = limits.get("x-", 0) == 1
            state.limits["y+"] = limits.get("y+", 0) == 1
            state.limits["y-"] = limits.get("y-", 0) == 1
            state.limits["z+"] = limits.get("z+", 0) == 1
            state.limits["z-"] = limits.get("z-", 0) == 1

            # Continuous active flags
            state.moving["x"] = self._continuous_active["sigmakoki"]["x"]
            state.moving["y"] = self._continuous_active["sigmakoki"]["y"]
            state.moving["z"] = self._continuous_active["sigmakoki"]["z"]
        except Exception as exc:
            logger.debug("SigmaKoki poll error: %s", exc)

    def _poll_zolix(self, driver: ZolixDriver, state: StageState) -> None:
        """Fill StageState from Zolix driver."""
        try:
            status = driver.get_status()

            state.position["x"] = status["position"].get("x", 0)
            state.position["y"] = status["position"].get("y", 0)
            state.position["r"] = status["position"].get("r", 0)

            cached = driver.cached_speed
            state.current_speed["x"] = cached.get("x", 0)
            state.current_speed["y"] = cached.get("y", 0)
            state.current_speed["r"] = cached.get("r", 0)

            limits = status["limits"]
            state.limits["x+"] = limits.get("x+", False)
            state.limits["x-"] = limits.get("x-", False)
            state.limits["y+"] = limits.get("y+", False)
            state.limits["y-"] = limits.get("y-", False)
            # Zolix R axis: map ZC300 Z-axis limits to StageState limit keys
            # StagePanel for Zolix uses "r+" and "r-" keys
            state.limits["r+"] = limits.get("r+", False)
            state.limits["r-"] = limits.get("r-", False)
            state.limits["z+"] = limits.get("x+", False)   # not used by Zolix UI
            state.limits["z-"] = limits.get("x-", False)   # but keep for completeness

            state.moving["x"] = status["moving"].get("x", False)
            state.moving["y"] = status["moving"].get("y", False)
            state.moving["r"] = status["moving"].get("r", False)

            state.home_switch = status.get("home_switch", {})
            state.axis_alarms = status.get("axis_alarms", {})
            state.emergency_stop = status.get("emergency_stop", False)
        except Exception as exc:
            logger.debug("Zolix poll error: %s", exc)

    def update_configs(
        self,
        sigmakoki_config: Dict[str, Any],
        zolix_config: Dict[str, Any],
    ) -> None:
        """Update cached config values (called after settings change)."""
        self._sigmakoki_slow = sigmakoki_config.get("slow_speed_hz", 200)
        self._sigmakoki_fast = sigmakoki_config.get("fast_speed_hz", 500)
        self._sigmakoki_invert = {
            "x": sigmakoki_config.get("invert_x", False),
            "y": sigmakoki_config.get("invert_y", False),
            "z": sigmakoki_config.get("invert_z", False),
        }
        self._sigmakoki_flip_xy = sigmakoki_config.get("flip_xy", False)
        self._sigmakoki_step = sigmakoki_config.get("single_step_amount", 10)
        self._sigmakoki_step_z = sigmakoki_config.get("single_step_z",
            sigmakoki_config.get("single_step_amount", 10))
        self._sigmakoki_slow_z = sigmakoki_config.get("slow_speed_z", 200)
        self._sigmakoki_fast_z = sigmakoki_config.get("fast_speed_z", 500)

        self._zolix_slow = zolix_config.get("slow_speed_pps", 1000)
        self._zolix_fast = zolix_config.get("fast_speed_pps", 5000)
        self._zolix_slow_r = zolix_config.get("slow_speed_r", 500)
        self._zolix_fast_r = zolix_config.get("fast_speed_r", 2000)
        self._zolix_invert = {
            "x": zolix_config.get("invert_x", False),
            "y": zolix_config.get("invert_y", False),
            "r": zolix_config.get("invert_r", False),
        }
        self._zolix_flip_xy = zolix_config.get("flip_xy", False)
        self._zolix_step = zolix_config.get("single_step_amount", 100)
        self._zolix_step_r = zolix_config.get("single_step_r",
            zolix_config.get("single_step_amount", 100))

    @property
    def sigmakoki_slow_speed(self) -> float:
        return self._sigmakoki_slow

    @property
    def sigmakoki_fast_speed(self) -> float:
        return self._sigmakoki_fast

    @property
    def zolix_slow_speed(self) -> float:
        return self._zolix_slow

    @property
    def zolix_fast_speed(self) -> float:
        return self._zolix_fast

    @property
    def sigmakoki_slow_z(self) -> float:
        return self._sigmakoki_slow_z

    @property
    def sigmakoki_fast_z(self) -> float:
        return self._sigmakoki_fast_z

    @property
    def zolix_slow_r(self) -> float:
        return self._zolix_slow_r

    @property
    def zolix_fast_r(self) -> float:
        return self._zolix_fast_r

