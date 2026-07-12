"""
Yudian AI-828 Temperature Controller Driver
============================================

MODBUS-RTU communication driver for the Yudian AI-828 single-loop
temperature controller (and compatible AI-8x6/8x8 series).

Tested on AI-828 firmware V9.3 with MODBUS-RTU (AFC=0 or AFC=2).

Protocol: MODBUS-RTU over RS-485 (USB-to-RS485 converter)
Library:  pyserial + shared modbus_rtu utilities

Adapted from the standalone ``yudian_ai828.py`` reference driver.
Key changes from reference:
- Removed ``scan_devices()`` — unreliable full-port scan.
  Auto-connect tries saved device only; falls back to manual selection.
- MODBUS frame construction delegates to ``utils.modbus_rtu``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import serial

from utils.modbus_rtu import (
    build_read_frame,
    build_write_single_frame,
)

logger = logging.getLogger("transfer_stage.yudian")


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


class YudianError(Exception):
    """Base exception for all Yudian controller errors."""

    def __init__(self, message: str, original_exception: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.original_exception = original_exception


class YudianConnectionError(YudianError):
    """Raised when unable to open serial port or handshake fails."""


class YudianCommunicationError(YudianError):
    """Raised on MODBUS-level errors (timeout, CRC, exception response)."""


# ---------------------------------------------------------------------------
# Register Map — verified empirically on AI-828 firmware V9.3 (Loc=808)
# ---------------------------------------------------------------------------


class YudianController:
    """MODBUS-RTU driver for a single Yudian AI-828 temperature controller.

    Each instance represents one physical device on the RS-485 bus,
    identified by its slave address.
    """

    # Register addresses — 1-based MODBUS register numbers
    REG_SV_W = 1      # Writable setpoint (40001)
    REG_dPt = 13      # Decimal places (40013)
    REG_A_M = 25      # Auto/Manual mode (40025)
    REG_Srun = 27     # Run/Stop/Hold (40027)
    REG_AT = 29       # Auto-Tune (40029)
    REG_PV = 75       # Process Value — read-only (40075)
    REG_SV = 76       # Setpoint display — read-only (40076)
    REG_MV = 77       # Output% + alarm (40077, low byte = MV%)

    # Temperature range for validation (raw units, before dPt scaling)
    _TEMP_LO = -2000   # -200.0 °C
    _TEMP_HI = 13000   # 1300.0 °C

    def __init__(
        self,
        port: str = "",
        slave_address: int = 1,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 0.5,
    ) -> None:
        self._port = port
        self._slave = slave_address
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._timeout = timeout

        self._ser: Optional[serial.Serial] = None
        self._connected: bool = False
        self._dpt: int = 1  # cached decimal-places count (populated on connect)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and perform a MODBUS handshake.

        Handshake: read dPt (register 13) to confirm device is responding,
        then ensure Srun=0 (Run mode) so SV writes are accepted.

        Raises
        ------
        YudianConnectionError
            If the COM port cannot be opened or handshake fails.
        """
        logger.info(
            "Connecting to %s slave=%d baud=%d …",
            self._port, self._slave, self._baudrate,
        )

        # Open serial port
        try:
            self._ser = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=self._bytesize,
                parity=self._parity,
                stopbits=self._stopbits,
                timeout=self._timeout,
            )
        except (serial.SerialException, OSError) as exc:
            raise YudianConnectionError(
                f"Cannot open serial port {self._port!r}: {exc}",
                original_exception=exc,
            ) from exc

        # Handshake — read dPt
        try:
            raw = self._read_raw_register(self.REG_dPt)
            self._dpt = raw if 0 <= raw <= 3 else 1
            logger.info("Handshake OK: dPt=%d", self._dpt)
        except YudianCommunicationError:
            self._close_port()
            raise
        except Exception as exc:
            self._close_port()
            raise YudianConnectionError(
                f"Handshake failed on {self._port!r}: {exc}"
            ) from exc

        # Ensure Run mode (Srun=0) so SV writes work
        try:
            srun_raw = self._read_raw_register(self.REG_Srun)
            logger.info("Srun=%d (%s)", srun_raw,
                         "Run" if srun_raw == 0 else "Stop" if srun_raw == 1 else "Hold")
            if srun_raw != 0:
                logger.info("Setting Srun=0 (Run mode)")
                self._write_raw_register(self.REG_Srun, 0)
        except Exception:
            logger.debug("Could not check/set Srun (non-critical)", exc_info=True)

        self._connected = True
        logger.info("Connected to Yudian AI-828 on %s", self._port)

    def disconnect(self) -> None:
        """Close the serial port and release resources."""
        logger.info("Disconnecting from %s", self._port)
        with self._lock:
            self._connected = False
            self._close_port()

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the device is currently connected."""
        return self._connected and self._ser is not None and self._ser.is_open

    @property
    def dpt(self) -> int:
        """Return the cached decimal-places count (0–3)."""
        return self._dpt

    # ------------------------------------------------------------------
    # Reading registers
    # ------------------------------------------------------------------

    def read_pv(self) -> float:
        """Read the current process value (measured temperature) in °C."""
        return self._read_register(self.REG_PV, decimals=self._dpt)

    def read_sv(self) -> float:
        """Read the current setpoint (target temperature) in °C."""
        return self._read_register(self.REG_SV, decimals=self._dpt)

    def read_mv(self) -> float:
        """Read the current output power in percent (0.0–100.0)."""
        raw = self._read_raw_register(self.REG_MV)
        return float(raw & 0xFF)  # low byte only

    def read_all(self) -> Dict[str, float]:
        """Read PV, SV, and MV in one logical operation.

        Returns
        -------
        dict
            ``{"pv": float, "sv": float, "mv": float}``

        Raises
        ------
        YudianCommunicationError
            On communication failure.
        """
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise YudianConnectionError("Not connected")

            # Read PV + SV in one multi-register transaction (2 registers from REG_PV)
            frame = build_read_frame(self._slave, 0x03, self.REG_PV, count=2)
            response = self._send_frame(frame)

            if len(response) < 9:
                raise YudianCommunicationError(f"Short multi-read ({len(response)} bytes)")
            if response[1] == 0x83:
                raise YudianCommunicationError(f"Exception code {response[2]}")

            pv_raw = self._to_signed((response[3] << 8) | response[4])
            sv_raw = self._to_signed((response[5] << 8) | response[6])

            # Diagnostic: log raw values on first successful read
            if not getattr(self, '_read_all_diag_done', False):
                self._read_all_diag_done = True
                logger.info("Yudian read_all raw: PV=%d SV=%d (dPt=%d) → PV=%.1f SV=%.1f °C",
                            pv_raw, sv_raw, self._dpt,
                            self._scale_value(pv_raw, self._dpt),
                            self._scale_value(sv_raw, self._dpt))

            # Read MV separately (not contiguous with PV/SV on V9.3)
            # Inline — cannot call _read_raw_register() which re-acquires _lock!
            try:
                frame_mv = build_read_frame(self._slave, 0x03, self.REG_MV, count=1)
                resp_mv = self._send_frame(frame_mv)
                mv_raw = self._parse_single_response(resp_mv, self.REG_MV)
                mv = float(mv_raw & 0xFF)
            except YudianCommunicationError:
                mv = 0.0

            return {
                "pv": self._scale_value(pv_raw, self._dpt),
                "sv": self._scale_value(sv_raw, self._dpt),
                "mv": mv,
            }

    # ------------------------------------------------------------------
    # Writing registers
    # ------------------------------------------------------------------

    def set_sv(self, value: float) -> None:
        """Set the target temperature (setpoint) in °C.

        Writes to register 40001 (REG_SV_W), NOT the read-only 40076.

        Raises
        ------
        ValueError
            If *value* is outside the supported temperature range.
        YudianCommunicationError
            On communication failure.
        """
        scaled_min = self._scale_value(self._TEMP_LO, self._dpt)
        scaled_max = self._scale_value(self._TEMP_HI, self._dpt)
        if not (scaled_min <= value <= scaled_max):
            raise ValueError(
                f"Setpoint {value}°C out of range ({scaled_min:.1f}–{scaled_max:.1f}°C)"
            )
        self._write_register(self.REG_SV_W, value, decimals=self._dpt)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _close_port(self) -> None:
        """Safely close the serial port."""
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _send_frame(self, frame: bytes) -> bytes:
        """Send a MODBUS frame and return the raw response."""
        if self._ser is None:
            raise YudianConnectionError("Not connected")
        try:
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            self._ser.write(frame)
            self._ser.flush()
            return self._ser.read(256)
        except (serial.SerialException, OSError) as exc:
            raise YudianCommunicationError(f"Serial I/O error: {exc}") from exc

    def _read_raw_register(self, register: int) -> int:
        """Read a single holding register, return raw signed 16-bit value."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise YudianConnectionError("Not connected")
            frame = build_read_frame(self._slave, 0x03, register, count=1)
            response = self._send_frame(frame)
            return self._parse_single_response(response, register)

    def _read_register(self, register: int, decimals: int) -> float:
        """Read a single register and scale by decimal places."""
        raw = self._read_raw_register(register)
        return self._scale_value(raw, decimals)

    def _write_raw_register(self, register: int, value: int) -> None:
        """Write a raw 16-bit value to a single holding register."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise YudianConnectionError("Not connected")
            frame = build_write_single_frame(self._slave, register, value)
            response = self._send_frame(frame)
            if len(response) < 8 or response[1] == 0x86:
                code = response[2] if len(response) > 2 else 0
                raise YudianCommunicationError(f"Write failed: exception {code}")

    def _write_register(self, register: int, value: float, decimals: int) -> None:
        """Write a scaled float value to a single holding register."""
        raw = int(round(value * (10 ** decimals)))
        self._write_raw_register(register, raw)

    def _parse_single_response(self, response: bytes, register: int) -> int:
        """Parse a single-register read response into a signed 16-bit int."""
        if len(response) < 5:
            raise YudianCommunicationError(f"No response (register {register})")
        if response[1] == 0x83:
            code = response[2] if len(response) > 2 else 0
            raise YudianCommunicationError(f"Exception {code} (register {register})")
        if response[1] != 0x03:
            raise YudianCommunicationError(
                f"Unexpected function code 0x{response[1]:02X}"
            )
        if len(response) < 7:
            raise YudianCommunicationError(f"Short response ({len(response)} bytes)")
        raw = (response[3] << 8) | response[4]
        return self._to_signed(raw)

    @staticmethod
    def _to_signed(raw: int) -> int:
        """Convert unsigned 16-bit to signed (two's complement)."""
        if raw > 32767:
            raw -= 65536
        return raw

    @staticmethod
    def _scale_value(raw: int, decimals: int) -> float:
        """Convert a raw register value to a float using the decimal-places count."""
        return raw / (10 ** decimals)

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return (
            f"YudianController(port={self._port!r}, slave={self._slave}, "
            f"baud={self._baudrate}, dPt={self._dpt}, {state})"
        )
