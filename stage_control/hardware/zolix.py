"""
Zolix XYR Stage Driver (ZC300 Controller)
==========================================

MODBUS-RTU driver for the Zolix ZC300 series motion controller,
configured for an XYR stage (X/Y translation + R rotation).

The ZC300 is a 3-axis controller.  Axis mapping:
    ZC300 "X" → logical "x" (translation)
    ZC300 "Y" → logical "y" (translation)
    ZC300 "Z" → logical "r" (rotation)  [third axis used for rotation]

Protocol: MODBUS-RTU over RS-485 (USB virtual COM port).
    Fixed baud: 115200, 8 data bits, no parity, 1 stop bit.

Key constraint: The ZC300 returns MODBUS exception 0x06 if a motion
command is sent while the target axis is already moving.  Speed
changes therefore require: stop → write speed → restart continuous.
The driver tracks per-axis ``moving`` state to manage this.

Register reference: ``zolix_XYR_stage_doc/register-map.md``
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import serial

from utils.modbus_rtu import (
    build_read_frame,
    build_write_multiple_floats,
    build_write_multiple_frame,
    build_write_single_frame,
    crc16,
    parse_multi_read_response,
    parse_float_pair,
)

logger = logging.getLogger("transfer_stage.zolix")

# ---------------------------------------------------------------------------
# ZC300 Register Map (1-based MODBUS register numbers)
# ---------------------------------------------------------------------------

# --- Input Registers (function code 0x04) ---
REG_MOTION_STATE  = 30012  # X/Y/Z motion state (3 regs: 0=stopped, 1=moving)
REG_STATUS        = 30015  # Limit/home/alarm/estop bitmask
REG_POS_X_HI      = 30016  # X position (float, 2 registers)
REG_POS_Y_HI      = 30018  # Y position (float, 2 registers)
REG_POS_Z_HI      = 30020  # Z position → R axis (float, 2 registers)

# --- Holding Registers (function codes 0x03/0x06/0x10) ---
REG_OPCODE        = 30050  # Operation code
REG_OP_AXIS       = 30051  # Axis selector (0x31=X, 0x32=Y, 0x33=Z)
REG_OP_DIR        = 30052  # Direction (0x50=P, 0x4E=N)
REG_OP_PARAM3     = 30053  # Reserved

REG_ENABLE_X      = 30066  # X enable (0x01=enabled, 0x00=disabled)
REG_ENABLE_Y      = 30067  # Y enable
REG_ENABLE_Z      = 30068  # Z/R enable

REG_SPEED_INIT_X  = 30123  # X initial speed (float, 2 regs)
REG_SPEED_INIT_Y  = 30125  # Y initial speed
REG_SPEED_INIT_Z  = 30127  # Z/R initial speed
REG_SPEED_CONST_X = 30129  # X constant speed (float, 2 regs)
REG_SPEED_CONST_Y = 30131  # Y constant speed
REG_SPEED_CONST_Z = 30133  # Z/R constant speed

REG_ACC_X         = 30135  # X acceleration (float, 2 regs)
REG_ACC_Y         = 30137  # Y acceleration
REG_ACC_Z         = 30139  # Z/R acceleration

REG_DIST_X        = 30114  # X fixed-length distance (float, 2 regs)
REG_DIST_Y        = 30116  # Y fixed-length distance
REG_DIST_Z        = 30118  # Z/R fixed-length distance

# --- Opcodes ---
OP_ABSOLUTE       = 0x0064  # Absolute move (to target position)
OP_FIXED_LENGTH   = 0x0065  # Fixed-length (single step) move
OP_CONTINUOUS     = 0x0066  # Continuous move
OP_DECEL_STOP     = 0x0067  # Decelerate to stop
OP_IMMEDIATE_STOP = 0x0068  # Immediate (emergency) stop
OP_HOME           = 0x0069  # Home / return to origin
OP_SAVE_PARAMS    = 0x006D  # Save parameters to non-volatile memory

# --- Axis Selectors ---
AXIS_X = 0x31
AXIS_Y = 0x32
AXIS_Z = 0x33  # Maps to logical "R" in our system
AXIS_ALL = 0x30

# --- Direction ---
DIR_POS = 0x50  # Positive / forward
DIR_NEG = 0x4E  # Negative / backward

# --- Status Bitmask (register 30015) ---
STATUS_X_POS_LIMIT  = 0   # Bit 0
STATUS_X_NEG_LIMIT  = 1   # Bit 1
STATUS_X_HOME       = 2   # Bit 2
STATUS_Y_POS_LIMIT  = 3   # Bit 3
STATUS_Y_NEG_LIMIT  = 4   # Bit 4
STATUS_Y_HOME       = 5   # Bit 5
STATUS_Z_POS_LIMIT  = 6   # Bit 6
STATUS_Z_NEG_LIMIT  = 7   # Bit 7
STATUS_Z_HOME       = 8   # Bit 8
STATUS_ESTOP        = 9   # Bit 9
STATUS_X_ALARM      = 10  # Bit 10
STATUS_Y_ALARM      = 11  # Bit 11
STATUS_Z_ALARM      = 12  # Bit 12

# --- MODBUS Exception Codes ---
EXC_ILLEGAL_FUNC    = 0x01
EXC_ILLEGAL_ADDR    = 0x02
EXC_ILLEGAL_DATA    = 0x03
EXC_CMD_ALARM       = 0x06  # Axis busy / motion command rejected
EXC_LIMIT           = 0x07  # Limit switch triggered
EXC_ESTOP           = 0x08  # Emergency stop active
EXC_NOT_ENABLED     = 0x09  # Axis not enabled
EXC_BAD_OPCODE      = 0x0A  # Invalid opcode

# --- Serial timing (lab-measured on the ZC300) ---
MIN_SERIAL_TIMEOUT_S    = 0.15   # reply latency measured at 57-73 ms
READ_DEADLINE_S         = 0.25   # per-transaction read deadline
OP_RETRY_DELAY_S        = 0.05   # sleep between opcode retries (under lock)
OP_STOP_RETRIES         = 1      # extra attempts for stops (2 total)
OP_START_RETRIES        = 1      # extra attempts for starts (2 total)
REEMIT_GATE_S           = 0.20   # min interval between real serial re-emissions per axis
FAIL_BACKOFF_S          = 0.30   # suppress re-attempts after a failed start
PRESTOP_POLL_BUDGET_S   = 0.30   # wall budget for the pre-stop motion poll
PRESTOP_READ_TIMEOUT_S  = 0.15   # read deadline used by the pre-stop poll
VERIFY_DELAY_S          = 0.30   # sleep before post-stop verification
VERIFY_RECHECK_DELAY_S  = 0.20   # sleep before escalation follow-up read


class ZolixNoResponse(ValueError):
    """Raised when no valid MODBUS reply arrives after all retries.

    Subclasses ``ValueError`` so all existing ``except ValueError``
    handlers treat it as a failed transaction.
    """

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _axis_to_zc300(axis: str) -> int:
    """Map logical axis name to ZC300 axis selector."""
    mapping = {"x": AXIS_X, "y": AXIS_Y, "r": AXIS_Z}
    return mapping.get(axis.lower(), AXIS_X)


def _axis_to_idx(axis: str) -> int:
    """Map logical axis name to 0-based index for register offset calcs."""
    mapping = {"x": 0, "y": 1, "r": 2}
    return mapping.get(axis.lower(), 0)


class ZolixDriver:
    """MODBUS-RTU driver for the Zolix ZC300 XYR stage controller.

    Parameters
    ----------
    port : str
        COM port name.
    slave_address : int
        MODBUS slave address (1–255, default 1).
    baudrate : int
        Fixed at 115200 for the ZC300.
    timeout : float
        Serial read timeout in seconds.
    stop_mode : str
        ``"decel"`` (0x0067) or ``"immediate"`` (0x0068).
    """

    def __init__(
        self,
        port: str = "",
        slave_address: int = 1,
        baudrate: int = 115200,
        timeout: float = 0.05,
        stop_mode: str = "immediate",
        verbose_logging: bool = False,
    ) -> None:
        self._port = port
        self._slave = slave_address
        self._baudrate = baudrate
        # The ZC300 replies in ~60 ms — a shorter read timeout caused
        # every transaction to time out and discard the late reply.
        self._timeout = max(float(timeout), MIN_SERIAL_TIMEOUT_S)
        self._stop_opcode = OP_DECEL_STOP if stop_mode == "decel" else OP_IMMEDIATE_STOP
        self._ser: Optional[serial.Serial] = None
        self._connected = False
        self._lock = threading.Lock()

        # Verbose transaction logging (console + debug.log) for stop-bug
        # diagnosis.  See docs/hardware/zolix/stop_bug_analysis.md.
        self._verbose = verbose_logging

        # Post-stop verification bookkeeping
        self._pending_stop_checks: set = set()

        # Track per-axis state to avoid sending motion commands to busy axes
        self._moving: Dict[str, bool] = {"x": False, "y": False, "r": False}

        # Cache — skip redundant register writes
        self._last_written_speed: Dict[str, float] = {"x": 0, "y": 0, "r": 0}
        self._last_written_distance: Dict[str, float] = {"x": 0, "y": 0, "r": 0}
        self._last_command_time = 0.0  # for idle detection in status polling
        self._zero_offset: Dict[str, float] = {"x": 0.0, "y": 0.0, "r": 0.0}
        self._last_direction: Dict[str, int] = {"x": 0, "y": 0, "r": 0}
        self._last_stop_time: Dict[str, float] = {"x": 0.0, "y": 0.0, "r": 0.0}

        # Opcode rate limiting (per axis, monotonic clock).
        # _last_serial_time  = updated on EVERY serial attempt (REEMIT_GATE)
        # _last_attempt_time = updated ONLY when a start FAILS (FAIL_BACKOFF) —
        #   a successful start must NOT arm the backoff window, or a fast
        #   stick sweep's stop→restart gets silently dropped (the v0.4.1
        #   "axis ends stopped on 90° change" bug).
        self._last_opcode: Dict[str, Optional[tuple]] = {"x": None, "y": None, "r": None}
        self._last_opcode_time: Dict[str, float] = {"x": 0.0, "y": 0.0, "r": 0.0}
        self._last_serial_time: Dict[str, float] = {"x": 0.0, "y": 0.0, "r": 0.0}
        self._last_attempt_time: Dict[str, float] = {"x": 0.0, "y": 0.0, "r": 0.0}

        # Motion generation counter — incremented on EVERY successful
        # motion command; used by the post-stop verify to distinguish
        # legitimate restarts from failed stops.
        self._motion_gen: Dict[str, int] = {"x": 0, "y": 0, "r": 0}

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open serial port and verify MODBUS communication.

        Raises
        ------
        serial.SerialException
            If the port cannot be opened.
        ConnectionError
            If no MODBUS response is received.
        """
        logger.info("Opening %s at %d baud (slave=%d)", self._port, self._baudrate, self._slave)
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self._timeout,
        )
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

        # Verify communication — read motion state register
        try:
            self._read_input_register(REG_MOTION_STATE)
        except Exception as exc:
            self._ser.close()
            self._ser = None
            raise ConnectionError(f"No MODBUS response from ZC300 on {self._port}: {exc}")

        # Enable all three axes
        try:
            for reg in (REG_ENABLE_X, REG_ENABLE_Y, REG_ENABLE_Z):
                self._write_single_locked(reg, 0x01)
            logger.info("Zolix: all axes enabled")
        except Exception as exc:
            logger.warning("Zolix: axis enable failed (may already be enabled): %s", exc)

        # Set max acceleration once — never change during operation
        try:
            max_acc = 10000000.0  # 10M pulses/s² — near-instant ramp-up
            for acc_reg in (REG_ACC_X, REG_ACC_Y, REG_ACC_Z):
                self._send_frame(
                    build_write_multiple_floats(self._slave, acc_reg, [max_acc]),
                    expected_fn=0x10,
                )
            logger.info("Zolix: max acceleration set (10M)")
        except Exception as exc:
            logger.warning("Zolix: acc set failed: %s", exc)

        self._connected = True
        logger.info("Connected to Zolix ZC300 on %s", self._port)

    def disconnect(self) -> None:
        """Stop all axes and close the serial port."""
        logger.info("Disconnecting Zolix ZC300")
        try:
            self.stop_all()
        except Exception:
            pass
        with self._lock:
            self._connected = False
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ser is not None and self._ser.is_open

    @property
    def last_command_time(self) -> float:
        """Timestamp of the last sent command (for idle detection)."""
        return self._last_command_time

    # ------------------------------------------------------------------
    # Motion Commands
    # ------------------------------------------------------------------

    def continuous_start(self, axis: str, direction: int, speed_pps: float) -> bool:
        """Start continuous movement.

        The resolver re-emits this every frame while an input is held;
        the driver rate-limits real serial transactions (the ZC300 needs
        ~60 ms each) and only pre-stops on direction changes / recent
        stops.  A bounded motion poll waits for the pre-stop to take
        effect before re-starting.
        """
        if speed_pps <= 0:
            return False

        ax = axis.lower()
        zc300_axis = _axis_to_zc300(ax)
        direction_code = DIR_POS if direction > 0 else DIR_NEG
        key = (direction_code, int(round(speed_pps)))

        # Fast path: same opcode already active — zero serial I/O.
        # No time window: there is nothing new to send.
        if self._moving.get(ax) and self._last_opcode.get(ax) == key:
            return True

        now_m = time.monotonic()
        since_serial = now_m - self._last_serial_time.get(ax, 0.0)
        since_fail = now_m - self._last_attempt_time.get(ax, 0.0)
        if self._moving.get(ax):
            # A different opcode is wanted but we just did serial — the
            # resolver re-emits every frame, so skip this one.
            if since_serial < REEMIT_GATE_S:
                return True
        elif since_fail < FAIL_BACKOFF_S:
            # A recent attempt FAILED — back off instead of re-flooding.
            return False

        prev_dir = self._last_direction.get(ax, 0)
        recent_stop = time.time() - self._last_stop_time.get(ax, 0) < 0.1
        need_prestop = False
        prestop_gen = self._motion_gen.get(ax, 0)
        with self._lock:
            self._last_serial_time[ax] = time.monotonic()
            if direction_code != prev_dir or recent_stop:
                # Pre-stop only if the hardware says the axis is moving
                # (bounded single read — unknown → proceed, the start
                # attempt reveals the truth).  No post-stop verify here —
                # a verify could fire between this stop and the new move
                # and escalate against a move the user just commanded;
                # if the start ultimately fails we schedule one below.
                hw_moving = False
                try:
                    hw_moving = (
                        self._read_motion_value_locked(ax, read_timeout=PRESTOP_READ_TIMEOUT_S) == 1
                    )
                except (ValueError, ConnectionError):
                    pass
                if hw_moving:
                    self._stop_axis_locked(ax, zc300_axis, verify=False)
                    need_prestop = True

        if need_prestop:
            self._wait_motion_stopped(ax)

        with self._lock:
            # Write speed (skip if unchanged)
            last_spd = self._last_written_speed.get(ax, -1)
            if abs(speed_pps - last_spd) > 0.5:
                self._write_speed_locked(ax, speed_pps)
                self._last_written_speed[ax] = speed_pps

            # Fire continuous move
            try:
                self._write_opcode_block(
                    OP_CONTINUOUS, zc300_axis, direction_code, retries=OP_START_RETRIES,
                )
            except ValueError as exc:
                time.sleep(0.10)  # let the controller finish the pre-stop
                try:
                    self._write_opcode_block(
                        OP_CONTINUOUS, zc300_axis, direction_code, retries=0,
                    )
                except ValueError as exc2:
                    if "exception 7" in str(exc2):
                        # At limit — the axis is definitely not moving
                        self._moving[ax] = False
                    logger.warning("Zolix %s: continuous start failed after retry: %s",
                                   ax, exc2)
                    self._last_attempt_time[ax] = time.monotonic()  # arm FAIL_BACKOFF
                    if need_prestop:
                        # The pre-stop may have failed (axis still moving
                        # the old way) — verify/escalate now that no new
                        # move was commanded.
                        self._schedule_post_stop_check(ax, prestop_gen)
                    return False

            self._moving[ax] = True
            self._last_direction[ax] = direction_code
            self._last_opcode[ax] = key
            self._last_opcode_time[ax] = time.monotonic()
            # NOTE: _last_attempt_time deliberately NOT updated here —
            # success must not arm FAIL_BACKOFF.
            self._motion_gen[ax] += 1
            return True

    def _wait_motion_stopped(self, ax: str) -> None:
        """Wait (bounded) for an axis's motion state to report stopped.

        Worst case ≈ PRESTOP_POLL_BUDGET_S; the caller proceeds after the
        budget regardless — a busy rejection on the following start is
        absorbed by continuous_start's delayed retry.
        """
        deadline = time.monotonic() + PRESTOP_POLL_BUDGET_S
        while time.monotonic() < deadline:
            time.sleep(0.02)
            with self._lock:
                try:
                    if self._read_motion_value_locked(ax, read_timeout=PRESTOP_READ_TIMEOUT_S) == 0:
                        return
                except (ValueError, ConnectionError):
                    return
        logger.debug("Zolix %s: pre-stop poll budget elapsed; proceeding "
                     "(0x06 retry covers it)", ax)

    def continuous_stop(self, axis: str) -> None:
        """Stop continuous movement on a single axis."""
        ax = axis.lower()
        zc300_axis = _axis_to_zc300(ax)
        with self._lock:
            self._stop_axis_locked(ax, zc300_axis)

    def stop_all(self) -> None:
        """Stop all three axes immediately (or decel, per config).

        Decel mode uses per-axis stops (0x31/0x32/0x33) — the
        0x0067 + AXIS_ALL combination is undocumented in the manual and
        untested on the lab unit.  Failed stops leave _moving set; the
        post-stop verification escalates with an immediate stop.
        """
        now = time.time()
        gens = dict(self._motion_gen)
        with self._lock:
            if self._stop_opcode == OP_DECEL_STOP:
                for a in ("x", "y", "r"):
                    zc300_axis = _axis_to_zc300(a)
                    self._vlog("Zolix STOP ALL (decel) cmd: opcode=0x%04X, axis=0x%02X",
                               OP_DECEL_STOP, zc300_axis)
                    try:
                        self._write_opcode_block(OP_DECEL_STOP, zc300_axis,
                                                 retries=OP_STOP_RETRIES)
                        self._moving[a] = False
                        self._last_stop_time[a] = now
                        self._vlog("Zolix stop sent OK: axis=%s", a)
                    except (ValueError, ConnectionError) as exc:
                        logger.error("Zolix %s: stop_all (decel) failed: %s", a, exc)
                        # leave _moving True — verification will escalate
            else:
                self._vlog("Zolix STOP ALL cmd: opcode=0x%04X (IMMEDIATE), "
                           "axis_selector=0x%02X", OP_IMMEDIATE_STOP, AXIS_ALL)
                try:
                    self._write_opcode_block(OP_IMMEDIATE_STOP, AXIS_ALL,
                                             retries=OP_STOP_RETRIES)
                    self._vlog("Zolix stop_all sent OK")
                    for a in ("x", "y", "r"):
                        self._moving[a] = False
                        self._last_stop_time[a] = now
                except (ValueError, ConnectionError) as exc:
                    logger.error("Zolix: stop_all command failed: %s", exc)
            # Verification against hardware motion state (escalates on failure)
            self._schedule_post_stop_check_all(gens)

    # ------------------------------------------------------------------
    # Post-stop verification (escalates with an immediate stop on failure)
    # ------------------------------------------------------------------

    def _schedule_post_stop_check(self, axis: str, gen: int) -> None:
        """Schedule one motion-state check after a stop.  *gen* is the
        motion-generation counter captured before the stop — a newer
        generation at check time means the movement is a legitimate
        restart, not a failed stop."""
        if axis in self._pending_stop_checks:
            return
        self._pending_stop_checks.add(axis)
        threading.Thread(
            target=self._post_stop_check, args=(axis, gen),
            daemon=True, name=f"zolix_stop_check_{axis}",
        ).start()

    def _schedule_post_stop_check_all(self, gens: Dict[str, int]) -> None:
        """Schedule one motion-state check for all axes (per-axis gen guard)."""
        if "all" in self._pending_stop_checks:
            return
        self._pending_stop_checks.add("all")
        threading.Thread(
            target=self._post_stop_check_all, args=(gens,),
            daemon=True, name="zolix_stop_check_all",
        ).start()

    def _post_stop_check(self, axis: str, gen: int) -> None:
        try:
            time.sleep(VERIFY_DELAY_S)  # outside the lock — lets decel finish
            with self._lock:
                if not self.is_connected:
                    return
                states = self._read_motion_states_locked()
                moving = states.get(axis, False)
                gen_now = self._motion_gen.get(axis, 0)
                if gen_now != gen:
                    logger.debug(
                        "Zolix POST-STOP VERIFY: axis %s movement is a "
                        "legitimate restart (gen %d→%d)", axis, gen, gen_now,
                    )
                    return
                if not moving:
                    if self._moving.get(axis, False):
                        # The stop's reply was lost but it took effect —
                        # reconcile so the rate-limit fast path heals.
                        self._moving[axis] = False
                        logger.info(
                            "Zolix POST-STOP VERIFY: axis %s stopped; "
                            "reconciled software state", axis,
                        )
                    else:
                        self._vlog("Zolix POST-STOP VERIFY: axis %s stopped (motion_state=0)", axis)
                    return
                logger.warning(
                    "Zolix POST-STOP VERIFY: axis %s still moving "
                    "(motion_state=1) — issuing immediate stop", axis,
                )
                try:
                    self._write_opcode_block(
                        OP_IMMEDIATE_STOP, _axis_to_zc300(axis), retries=OP_STOP_RETRIES,
                    )
                    self._moving[axis] = False
                    self._last_stop_time[axis] = time.time()
                except (ValueError, ConnectionError) as exc:
                    logger.error(
                        "Zolix POST-STOP VERIFY: escalation stop for axis %s "
                        "failed: %s", axis, exc,
                    )
                    return
            time.sleep(VERIFY_RECHECK_DELAY_S)
            with self._lock:
                if not self.is_connected:
                    return
                states = self._read_motion_states_locked()
                if states.get(axis, False):
                    logger.error(
                        "Zolix POST-STOP VERIFY: axis %s STILL moving after "
                        "escalation stop", axis,
                    )
                else:
                    self._vlog("Zolix POST-STOP VERIFY: axis %s stopped after escalation", axis)
        except Exception as exc:
            logger.debug("Zolix post-stop check error: %s", exc)
        finally:
            self._pending_stop_checks.discard(axis)

    def _post_stop_check_all(self, gens: Dict[str, int]) -> None:
        try:
            time.sleep(VERIFY_DELAY_S)  # outside the lock
            with self._lock:
                if not self.is_connected:
                    return
                states = self._read_motion_states_locked()
                escalated = []
                for axis in ("x", "y", "r"):
                    if not states.get(axis, False):
                        if self._moving.get(axis, False):
                            self._moving[axis] = False
                            logger.info(
                                "Zolix POST-STOP VERIFY (all): axis %s stopped; "
                                "reconciled software state", axis,
                            )
                        continue
                    if self._motion_gen.get(axis, 0) != gens.get(axis, 0):
                        logger.debug(
                            "Zolix POST-STOP VERIFY (all): axis %s movement is a "
                            "legitimate restart", axis,
                        )
                        continue
                    logger.warning(
                        "Zolix POST-STOP VERIFY (all): axis %s still moving — "
                        "issuing immediate stop", axis,
                    )
                    try:
                        self._write_opcode_block(
                            OP_IMMEDIATE_STOP, _axis_to_zc300(axis), retries=OP_STOP_RETRIES,
                        )
                        self._moving[axis] = False
                        self._last_stop_time[axis] = time.time()
                        escalated.append(axis)
                    except (ValueError, ConnectionError) as exc:
                        logger.error(
                            "Zolix POST-STOP VERIFY (all): escalation stop for "
                            "axis %s failed: %s", axis, exc,
                        )
            if escalated:
                time.sleep(VERIFY_RECHECK_DELAY_S)
                with self._lock:
                    if not self.is_connected:
                        return
                    states = self._read_motion_states_locked()
                    still = [a for a in escalated if states.get(a, False)]
                    if still:
                        logger.error(
                            "Zolix POST-STOP VERIFY (all): axes STILL moving "
                            "after escalation: %s", still,
                        )
                    else:
                        self._vlog("Zolix POST-STOP VERIFY (all): axes stopped after escalation")
            else:
                self._vlog("Zolix POST-STOP VERIFY (all): all axes stopped")
        except Exception as exc:
            logger.debug("Zolix post-stop check error: %s", exc)
        finally:
            self._pending_stop_checks.discard("all")

    def single_step(self, axis: str, direction: int, steps: int) -> int:
        """Execute a fixed-length (single step) move.

        This method blocks until the axis stops moving.

        Parameters
        ----------
        axis : str
            ``"x"``, ``"y"``, or ``"r"``.
        direction : int
            +1 or -1.
        steps : int
            Number of pulses to move.

        Returns
        -------
        int
            Steps requested (0 if at limit or error).
        """
        ax = axis.lower()
        zc300_axis = _axis_to_zc300(ax)
        direction_code = DIR_POS if direction > 0 else DIR_NEG

        with self._lock:
            # Write fixed speed for single-step — independent of last stick speed
            step_speed = 1000.0  # reasonable default for single-step jogging
            self._write_speed_locked(ax, step_speed)
            self._last_written_speed[ax] = step_speed

            # Write distance only if changed
            last_dist = self._last_written_distance.get(ax, -1)
            if abs(steps - last_dist) > 0.5:
                dist_reg = REG_DIST_X + (_axis_to_idx(ax) * 2)
                self._send_frame(
                    build_write_multiple_floats(self._slave, dist_reg, [float(steps)]),
                    expected_fn=0x10,
                )
                self._last_written_distance[ax] = steps

            # Fire fixed-length move — MUST use 0x10 per ZC300 spec
            try:
                self._write_opcode_block(
                    OP_FIXED_LENGTH, zc300_axis, direction_code, retries=OP_START_RETRIES,
                )
            except ValueError as exc:
                logger.warning("Zolix single_step %s failed: %s", ax, exc)
                return 0

            self._moving[ax] = True
            # A single-step is a motion command — bump the generation so a
            # pending post-stop verify never escalates this legit move.
            self._motion_gen[ax] += 1
            self._last_serial_time[ax] = time.monotonic()  # real serial, not a failure

        # Fire-and-forget: don't block the input loop polling for completion.
        # The status poller updates motion state for the GUI asynchronously.
        return steps

    def zero_all(self) -> None:
        """Reset position counters to zero at current location (no physical movement).

        Stores the current raw position as an offset so the GUI displays
        positions relative to this zero point.
        """
        with self._lock:
            # Read current raw positions
            data = self._read_input_registers_locked(REG_MOTION_STATE, 10)
            pos_x = parse_float_pair(data[4], data[5]) if len(data) >= 6 else 0.0
            pos_y = parse_float_pair(data[6], data[7]) if len(data) >= 8 else 0.0
            pos_r = parse_float_pair(data[8], data[9]) if len(data) >= 10 else 0.0
            self._zero_offset["x"] = pos_x
            self._zero_offset["y"] = pos_y
            self._zero_offset["r"] = pos_r
            for a in ("x", "y", "r"):
                self._moving[a] = False

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    def set_enabled(self, axis: str, enabled: bool) -> None:
        """Enable or disable an axis (software level, register 30066-30068)."""
        reg = REG_ENABLE_X + _axis_to_idx(axis.lower())
        value = 0x01 if enabled else 0x00
        with self._lock:
            self._write_single_locked(reg, value)

    # ------------------------------------------------------------------
    # Status Queries
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Read the full status bitmap and positions in ONE multi-read call.

        Reads 10 contiguous input registers (30012–30021):
          30012-30014 = motion states (3)
          30015       = status/limit bitmask (1)
          30016-30021 = positions (6, float pairs)

        Returns
        -------
        dict
            Positions, limits, home switches, alarms, estop state.
        """
        with self._lock:
            # Single multi-read: 10 registers from REG_MOTION_STATE (30012)
            data = self._read_input_registers_locked(REG_MOTION_STATE, 10)

            motion = {
                "x": data[0] == 1 if len(data) > 0 else False,
                "y": data[1] == 1 if len(data) > 1 else False,
                "r": data[2] == 1 if len(data) > 2 else False,
            }
            status_raw = data[3] if len(data) > 3 else 0
            pos_x = parse_float_pair(data[4], data[5]) if len(data) >= 6 else 0.0
            pos_y = parse_float_pair(data[6], data[7]) if len(data) >= 8 else 0.0
            pos_r = parse_float_pair(data[8], data[9]) if len(data) >= 10 else 0.0

        # Decode status bitmap
        def bit(n):
            return bool(status_raw & (1 << n))

        limits = {
            "x+": bit(STATUS_X_POS_LIMIT), "x-": bit(STATUS_X_NEG_LIMIT),
            "y+": bit(STATUS_Y_POS_LIMIT), "y-": bit(STATUS_Y_NEG_LIMIT),
            "r+": bit(STATUS_Z_POS_LIMIT), "r-": bit(STATUS_Z_NEG_LIMIT),
        }
        home = {
            "x": bit(STATUS_X_HOME), "y": bit(STATUS_Y_HOME), "r": bit(STATUS_Z_HOME),
        }
        alarms = {
            "x": bit(STATUS_X_ALARM), "y": bit(STATUS_Y_ALARM), "r": bit(STATUS_Z_ALARM),
        }

        return {
            "position": {
                "x": pos_x - self._zero_offset["x"],
                "y": pos_y - self._zero_offset["y"],
                "r": pos_r - self._zero_offset["r"],
            },
            "moving": motion,
            "limits": limits,
            "home_switch": home,
            "axis_alarms": alarms,
            "emergency_stop": bit(STATUS_ESTOP),
        }

    def get_limits(self) -> Dict[str, int]:
        """Read limit switch states only.

        Returns
        -------
        dict
            Keys ``"x+"``, ``"x-"``, ``"y+"``, ``"y-"``, ``"r+"``, ``"r-"``.
            Value is 1 (triggered) or 0 (normal).
        """
        status = self.get_status()
        limits = status["limits"]
        return {k: 1 if v else 0 for k, v in limits.items()}

    # ------------------------------------------------------------------
    # Internal: Locked MODBUS I/O
    # ------------------------------------------------------------------

    def set_verbose(self, enabled: bool) -> None:
        """Toggle verbose transaction logging (console + debug.log)."""
        self._verbose = enabled

    def _vlog(self, msg: str, *args) -> None:
        """INFO-level log when verbose is enabled, DEBUG otherwise."""
        if self._verbose:
            logger.info(msg, *args)
        else:
            logger.debug(msg, *args)

    def _log_frame(self, tag: str, frame: bytes, dur_ms: Optional[float] = None) -> None:
        """Log a serial frame — full hex dump when verbose is on, otherwise
        a compact non-hex line (keeps lab evidence in debug.log without
        per-frame hex formatting on the input thread)."""
        if self._verbose:
            if dur_ms is not None:
                self._vlog("Zolix %s frame (%d bytes, %.1f ms): %s",
                           tag, len(frame), dur_ms, frame.hex(" "))
            else:
                self._vlog("Zolix %s frame (%d bytes): %s", tag, len(frame), frame.hex(" "))
            return
        fn = frame[1] if len(frame) > 1 else -1
        if dur_ms is not None:
            logger.debug("Zolix %s frame (%d bytes, fn 0x%02X, %.1f ms)",
                         tag, len(frame), fn, dur_ms)
        else:
            logger.debug("Zolix %s frame (%d bytes, fn 0x%02X)", tag, len(frame), fn)

    @staticmethod
    def _extract_frame(
        buf: bytearray, slave: int, expected_fn: Optional[int],
    ) -> Optional[tuple]:
        """Extract one complete, CRC-valid MODBUS frame from *buf*.

        Returns ``(frame_bytes, is_stale)`` for the first complete frame
        from *slave*, or ``None`` if nothing complete is available yet.
        ``is_stale`` is True when the frame's function byte doesn't match
        *expected_fn* (a reply to an earlier command).
        """
        for i in range(len(buf)):
            if buf[i] != slave or len(buf) - i < 5:
                continue
            fn = buf[i + 1]
            if fn & 0x80:            # exception reply: addr+fn+code+crc
                frame_len = 5
            elif fn in (0x06, 0x10):  # write echo: addr+fn+reg(2)+val(2)+crc
                frame_len = 8
            elif fn in (0x03, 0x04):  # read reply: addr+fn+count+data+crc
                frame_len = 3 + buf[i + 2] + 2
            else:
                continue             # unknown fn — skip this offset
            if len(buf) - i < frame_len:
                return None          # incomplete frame yet
            frame = bytes(buf[i:i + frame_len])
            if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                continue             # bad CRC — noise; keep scanning
            if expected_fn is not None and fn not in (expected_fn, expected_fn | 0x80):
                return (frame, True)  # complete but stale
            return (frame, False)
        return None

    def _send_frame(
        self, frame: bytes, expected_fn: Optional[int] = None,
        read_timeout: Optional[float] = None,
    ) -> bytes:
        """Send a MODBUS frame and wait for a valid reply.

        Reads byte-by-byte until *read_timeout* (default
        ``READ_DEADLINE_S``) elapses, assembling complete CRC-valid
        frames.  Complete replies to earlier commands (wrong function
        byte vs *expected_fn*) are discarded and the wait continues —
        a late reply is never attributed to the wrong transaction.
        Returns ``b""`` when nothing valid arrives.
        """
        if self._ser is None or not self._ser.is_open:
            raise ConnectionError("serial port not open")
        deadline = time.monotonic() + (read_timeout if read_timeout is not None else READ_DEADLINE_S)
        try:
            self._last_command_time = time.time()
            # Log-only drain of pending bytes (replaces blind
            # reset_input_buffer, which used to discard late replies)
            pending = b""
            while self._ser.in_waiting > 0:
                pending += self._ser.read(self._ser.in_waiting)
            if pending:
                logger.debug("Zolix drained %d stale bytes before TX: %s",
                             len(pending), pending.hex(" "))

            t0 = time.perf_counter()
            self._ser.write(frame)
            self._ser.flush()
            self._log_frame("TX", frame)

            buf = bytearray()
            stale_discarded = 0
            while time.monotonic() < deadline:
                chunk = self._ser.read(1)  # paced by the clamped serial timeout
                if not chunk:
                    continue
                buf += chunk
                found = self._extract_frame(buf, self._slave, expected_fn)
                if found is None:
                    continue             # incomplete yet
                frame_bytes, is_stale = found
                del buf[:len(frame_bytes)]
                if is_stale:
                    if stale_discarded < 3:
                        stale_discarded += 1
                        logger.warning(
                            "Zolix stale response discarded while waiting for "
                            "fn 0x%02X: %s", expected_fn or 0, frame_bytes.hex(" "),
                        )
                    continue             # keep waiting until deadline
                self._log_frame("RX", frame_bytes,
                                dur_ms=(time.perf_counter() - t0) * 1000.0)
                return frame_bytes

            self._vlog("Zolix RX: no valid response to fn 0x%02X frame (%d bytes)",
                       frame[1] if len(frame) > 1 else -1, len(frame))
            return b""
        except (serial.SerialException, OSError) as exc:
            self._connected = False
            raise ConnectionError(f"Serial error: {exc}") from exc

    def _write_opcode_block(
        self, opcode: int, *params: int, retries: int = OP_STOP_RETRIES,
    ) -> None:
        """Write an opcode command using function 0x10 (required by ZC300).

        Only writes the registers actually used by this opcode — the ZC300
        rejects frames with wrong register count (exception 0x03).
        Retries on lost replies (no valid response) and busy rejections
        (exception 0x06); never silently succeeds without a reply.

        Caller must hold ``_lock``.
        """
        values = [opcode] + list(params)
        frame = build_write_multiple_frame(
            self._slave, REG_OPCODE, values,
        )
        attempts = 1 + max(0, retries)
        for attempt in range(attempts):
            self._vlog("Zolix opcode: 0x%04X params=%s (attempt %d/%d)",
                       opcode, list(params), attempt + 1, attempts)
            response = self._send_frame(frame, expected_fn=0x10)
            if not response:
                # No valid reply — never treat as success (v0.4.0's
                # silent-false-success bug).  Retry, then fail loudly.
                if attempt < attempts - 1:
                    logger.warning(
                        "Zolix opcode 0x%04X: no valid response (attempt %d/%d) — retrying",
                        opcode, attempt + 1, attempts,
                    )
                    time.sleep(OP_RETRY_DELAY_S)
                    continue
                raise ZolixNoResponse(
                    f"Zolix opcode 0x{opcode:04X}: no valid response after "
                    f"{attempts} attempt(s)"
                )
            if response[1] == (0x10 | 0x80):
                exc = response[2] if len(response) > 2 else 0
                if exc == EXC_CMD_ALARM:
                    self._vlog(
                        "Zolix EXC 0x%02X on opcode 0x%04X — axis busy / "
                        "new-motion-forbidden", exc, opcode,
                    )
                logger.warning("Zolix opcode 0x%04X rejected: exception 0x%02X "
                               "(response: %s)", opcode, exc, response.hex(" "))
                if exc == EXC_CMD_ALARM and attempt < attempts - 1:
                    # Busy — retry after a short delay
                    time.sleep(OP_RETRY_DELAY_S)
                    continue
                raise ValueError(f"MODBUS exception {exc}")
            return  # clean 0x10 echo

    def _write_single_locked(self, register: int, value: int) -> None:
        """Write a single holding register (caller must hold _lock)."""
        frame = build_write_single_frame(self._slave, register, value)
        response = self._send_frame(frame, expected_fn=0x06)
        if not response:
            raise ZolixNoResponse(
                f"Zolix write-single register {register}: no valid response"
            )

    def _read_input_register_locked(
        self, register: int, read_timeout: Optional[float] = None,
    ) -> int:
        """Read a single input register, return signed 16-bit (caller must hold _lock)."""
        frame = build_read_frame(self._slave, 0x04, register, count=1)
        response = self._send_frame(frame, expected_fn=0x04, read_timeout=read_timeout)
        raw = (response[3] << 8) | response[4]
        if raw > 32767:
            raw -= 65536
        return raw

    def _read_input_registers_locked(
        self, start_register: int, count: int, read_timeout: Optional[float] = None,
    ) -> list[int]:
        """Read multiple input registers (caller must hold _lock)."""
        frame = build_read_frame(self._slave, 0x04, start_register, count=count)
        response = self._send_frame(frame, expected_fn=0x04, read_timeout=read_timeout)
        return parse_multi_read_response(response, 0x04)

    def _read_motion_value_locked(
        self, axis: str, read_timeout: Optional[float] = None,
    ) -> int:
        """Read one axis's motion-state register (0=stopped, 1=moving)."""
        reg = REG_MOTION_STATE + _axis_to_idx(axis)
        return self._read_input_register_locked(reg, read_timeout=read_timeout)

    def _read_input_register(self, register: int) -> int:
        """Read a single input register (acquires lock)."""
        with self._lock:
            return self._read_input_register_locked(register)

    def _read_motion_states_locked(self) -> Dict[str, bool]:
        """Read motion states (caller must hold _lock)."""
        data = self._read_input_registers_locked(REG_MOTION_STATE, 3)
        return {
            "x": data[0] == 1 if len(data) > 0 else False,
            "y": data[1] == 1 if len(data) > 1 else False,
            "r": data[2] == 1 if len(data) > 2 else False,
        }

    def _write_speed_locked(self, axis: str, speed_pps: float) -> None:
        """Write constant speed for an axis (caller must hold _lock).

        Acceleration is set once on connect to max value and never changed.
        """
        idx = _axis_to_idx(axis)
        const_reg = REG_SPEED_CONST_X + (idx * 2)

        if abs(speed_pps - self._last_written_speed.get(axis, -1)) > 0.5:
            response = self._send_frame(
                build_write_multiple_floats(self._slave, const_reg, [speed_pps]),
                expected_fn=0x10,
            )
            if not response:
                raise ZolixNoResponse(
                    f"Zolix speed write for {axis}: no valid response"
                )
            self._last_written_speed[axis] = speed_pps

    def _stop_axis_locked(self, axis: str, zc300_axis: int, verify: bool = True) -> None:
        """Stop a single axis.  State is only updated on success — a failed
        stop must not clear _moving, or the resolver will think the axis
        stopped when it didn't.  The post-stop verification escalates
        with an immediate stop when the hardware keeps moving.
        *verify* is False for pre-stops inside ``continuous_start`` (the
        follow-up start is verified by its own retry logic instead)."""
        gen = self._motion_gen.get(axis, 0)
        stop_name = "DECEL" if self._stop_opcode == OP_DECEL_STOP else "IMMEDIATE"
        self._vlog("Zolix STOP cmd: opcode=0x%04X (%s), axis=0x%02X",
                   self._stop_opcode, stop_name, zc300_axis)
        try:
            self._write_opcode_block(self._stop_opcode, zc300_axis,
                                     retries=OP_STOP_RETRIES)
        except (ValueError, ConnectionError) as exc:
            logger.error("Zolix %s: stop command failed after retries: %s", axis, exc)
            # leave _moving True — verification will escalate
            if verify:
                self._schedule_post_stop_check(axis, gen)
            return
        self._moving[axis] = False
        self._last_stop_time[axis] = time.time()
        self._vlog("Zolix stop sent OK: axis=%s", axis)
        if verify:
            self._schedule_post_stop_check(axis, gen)

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def save_parameters(self) -> bool:
        """Save current parameters to non-volatile memory.

        Per the ZC300 manual, all axes must be stationary for this to succeed.

        Returns
        -------
        bool
            ``True`` if saved successfully.
        """
        with self._lock:
            # Check all axes are stopped
            motion = self._read_motion_states_locked()
            if any(motion.values()):
                logger.warning("Cannot save params: axes are moving")
                return False
            try:
                self._write_opcode_block(OP_SAVE_PARAMS)
                return True
            except ValueError as exc:
                logger.warning("Save params failed: %s", exc)
                return False

    @property
    def cached_speed(self) -> Dict[str, float]:
        """Return a copy of the last written per-axis speed (steps/sec)."""
        return dict(self._last_written_speed)

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return f"ZolixDriver({self._port}, slave={self._slave}, {state})"
