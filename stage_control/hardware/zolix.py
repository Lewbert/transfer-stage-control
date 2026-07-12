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
    ) -> None:
        self._port = port
        self._slave = slave_address
        self._baudrate = baudrate
        self._timeout = timeout
        self._stop_opcode = OP_DECEL_STOP if stop_mode == "decel" else OP_IMMEDIATE_STOP
        self._ser: Optional[serial.Serial] = None
        self._connected = False
        self._lock = threading.Lock()

        # Track per-axis state to avoid sending motion commands to busy axes
        self._moving: Dict[str, bool] = {"x": False, "y": False, "r": False}

        # Cache — skip redundant register writes
        self._last_written_speed: Dict[str, float] = {"x": 0, "y": 0, "r": 0}
        self._last_written_distance: Dict[str, float] = {"x": 0, "y": 0, "r": 0}
        self._last_command_time = 0.0  # for idle detection in status polling
        self._zero_offset: Dict[str, float] = {"x": 0.0, "y": 0.0, "r": 0.0}
        self._last_direction: Dict[str, int] = {"x": 0, "y": 0, "r": 0}

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
        """Start continuous movement.  Pre-stops the axis only on direction reversal;
        same-direction re-emissions (e.g. D-pad per-frame refresh) are handled
        as speed-update only, avoiding unnecessary stop→start cycles."""
        if speed_pps <= 0:
            return False

        ax = axis.lower()
        zc300_axis = _axis_to_zc300(ax)
        direction_code = DIR_POS if direction > 0 else DIR_NEG

        # Only pre-stop if changing direction (not on same-direction re-emission)
        prev_dir = self._last_direction.get(ax, 0)
        need_poll = False
        with self._lock:
            if self._moving.get(ax) and direction_code != prev_dir:
                self._stop_axis_locked(ax, zc300_axis)
                need_poll = True

        # Poll outside lock so other threads can use serial during wait
        if need_poll:
            for _ in range(15):  # up to ~300 ms
                time.sleep(0.02)
                with self._lock:
                    try:
                        reg = REG_MOTION_STATE + zc300_axis
                        val = self._read_input_register_locked(reg)
                        if val == 0:
                            break
                    except Exception:
                        break

        with self._lock:
            # Write speed (skip if unchanged)
            last_spd = self._last_written_speed.get(ax, -1)
            if abs(speed_pps - last_spd) > 0.5:
                self._write_speed_locked(ax, speed_pps)
                self._last_written_speed[ax] = speed_pps

            # Fire continuous move
            try:
                self._write_opcode_block(OP_CONTINUOUS, zc300_axis, direction_code)
                self._moving[ax] = True
                self._last_direction[ax] = direction_code
                return True
            except ValueError as exc:
                if "exception 6" in str(exc) or "exception 7" in str(exc):
                    logger.warning("Zolix %s: command rejected (exc: %s)", ax, exc)
                    return False
                raise

    def continuous_stop(self, axis: str) -> None:
        """Stop continuous movement on a single axis."""
        ax = axis.lower()
        zc300_axis = _axis_to_zc300(ax)
        with self._lock:
            self._stop_axis_locked(ax, zc300_axis)

    def stop_all(self) -> None:
        """Stop all three axes immediately (or decel, per config)."""
        with self._lock:
            self._write_opcode_block(self._stop_opcode, AXIS_ALL)
            for a in ("x", "y", "r"):
                self._moving[a] = False

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
                )
                self._last_written_distance[ax] = steps

            # Fire fixed-length move — MUST use 0x10 per ZC300 spec
            try:
                self._write_opcode_block(OP_FIXED_LENGTH, zc300_axis, direction_code)
            except ValueError as exc:
                logger.warning("Zolix single_step %s failed: %s", ax, exc)
                return 0

            self._moving[ax] = True

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

    def _send_frame(self, frame: bytes) -> bytes:
        """Send a MODBUS frame and wait for the response."""
        try:
            self._last_command_time = time.time()
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            self._ser.write(frame)
            self._ser.flush()
            time.sleep(0.002)
            return self._ser.read(256)
        except (serial.SerialException, OSError) as exc:
            self._connected = False
            raise ConnectionError(f"Serial error: {exc}") from exc

    def _write_opcode_block(self, opcode: int, *params: int) -> None:
        """Write an opcode command using function 0x10 (required by ZC300).

        Only writes the registers actually used by this opcode — the ZC300
        rejects frames with wrong register count (exception 0x03).

        Caller must hold ``_lock``.
        """
        values = [opcode] + list(params)
        frame = build_write_multiple_frame(
            self._slave, REG_OPCODE, values,
        )
        logger.debug("Zolix opcode: 0x%04X params=%s", opcode, list(params))
        response = self._send_frame(frame)
        if response and len(response) >= 3 and response[1] == (0x10 | 0x80):
            exc = response[2] if len(response) > 2 else 0
            logger.warning("Zolix opcode 0x%04X rejected: exception 0x%02X", opcode, exc)
            raise ValueError(f"MODBUS exception {exc}")

    def _write_single_locked(self, register: int, value: int) -> None:
        """Write a single holding register (caller must hold _lock)."""
        frame = build_write_single_frame(self._slave, register, value)
        self._send_frame(frame)

    def _read_input_register_locked(self, register: int) -> int:
        """Read a single input register, return signed 16-bit (caller must hold _lock)."""
        frame = build_read_frame(self._slave, 0x04, register, count=1)
        response = self._send_frame(frame)  # waits for response
        raw = (response[3] << 8) | response[4]
        if raw > 32767:
            raw -= 65536
        return raw

    def _read_input_registers_locked(self, start_register: int, count: int) -> list[int]:
        """Read multiple input registers (caller must hold _lock)."""
        frame = build_read_frame(self._slave, 0x04, start_register, count=count)
        response = self._send_frame(frame)  # waits for response
        return parse_multi_read_response(response, 0x04)

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
            self._send_frame(
                build_write_multiple_floats(self._slave, const_reg, [speed_pps]),
            )
            self._last_written_speed[axis] = speed_pps

    def _stop_axis_locked(self, axis: str, zc300_axis: int) -> None:
        """Stop a single axis.  Response wait in _send_frame naturally
        spaces commands — the next command only fires after ZC300 echoes."""
        self._write_opcode_block(self._stop_opcode, zc300_axis)
        self._moving[axis] = False

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
