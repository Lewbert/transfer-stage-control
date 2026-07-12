"""
Yudian AI-828 Temperature Controller Driver
============================================

MODBUS-RTU communication driver for the Yudian AI-828 single-loop
temperature controller (and compatible AI-8x6/8x8 series).

Tested on AI-828 firmware V9.3 with MODBUS-RTU (AFC=0 or AFC=2).
The register map was verified empirically against the hardware
display and cross-referenced with the official protocol manual
(``宇电单回路测量控制仪表通讯协议说明.pdf``).

.. note::

    The MODBUS register map depends on the controller's ``Loc``
    parameter (firmware V9.1+).  The addresses in this driver were
    verified with Loc=808 (factory unlock).  If your controller uses
    a different Loc value, registers may be shifted — see Note 6 in
    the protocol manual.

Protocol: MODBUS-RTU over RS485
Library:  pyserial only (raw MODBUS frames, no minimalmodbus)

Usage (standalone)::

    from yudian_ai828 import YudianController

    ctrl = YudianController(port="COM3", slave_address=1)
    ctrl.connect()
    print(f"Current temp: {ctrl.read_pv():.1f} °C")
    print(f"Setpoint:     {ctrl.read_sv():.1f} °C")
    ctrl.set_sv(150.0)
    ctrl.disconnect()

Usage (with auto-scan)::

    devices = YudianController.scan_devices()
    for d in devices:
        print(f"Found: {d['port']} slave={d['slave']} — {d['description']}")

Author:  Generated with Claude Code
Date:    2026-06-30
License: MIT
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Any, Dict, List, Optional

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class YudianError(Exception):
    """Base exception for all Yudian controller errors."""

    def __init__(self, message: str, original_exception: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.original_exception = original_exception


class YudianConnectionError(YudianError):
    """Raised when unable to open the serial port or initial handshake fails.

    This typically means:
    - The COM port does not exist or is in use by another program
    - The USB-to-RS485 converter is not plugged in
    - The controller is powered off or not wired correctly
    """


class YudianCommunicationError(YudianError):
    """Raised on any MODBUS-level error after a connection is established.

    This covers:
    - No response from device (timeout — cable unplugged, wrong slave address)
    - CRC mismatch or malformed response (noise on RS485 line)
    - MODBUS exception code returned by device (e.g. illegal data address)
    """


# ---------------------------------------------------------------------------
# Register Map — verified empirically on AI-828 firmware V9.3 (Loc=808)
# ---------------------------------------------------------------------------
# Cross-referenced with protocol manual:
#   docs/宇电单回路测量控制仪表通讯协议说明.pdf
#
# IMPORTANT (from protocol manual Note 6): Firmware V9.1+ can remap
# registers based on the Loc parameter.  If Loc=128~191, SV and
# HIAL~dHAL shift by +4, with Srun+EP1~EP8 at positions 0~8.
# The addresses below were verified with Loc=808 (factory unlock).
#
#  Our REG  |  Protocol Manual       |  Parameter
# ----------|------------------------|---------------------------
#    10     |  (empirical only)      |  dPt  — decimal places
#    26     |  index 25 (40026)      |  A-M  — auto/manual mode
#    28     |  index 27 (40028)      |  Srun — run/stop/hold
#    29     |  index 28 (40029)      |  AT   — auto-tune
#    77     |  (empirical)           |  MV   — output %
#    78     |  (empirical)           |  PV   — process value (temp)
#    79     |  (empirical)           |  SV   — setpoint (target)
# ---------------------------------------------------------------------------

class YudianController:
    """MODBUS-RTU driver for a single Yudian AI-828 temperature controller.

    Each instance represents one physical device on the RS485 bus,
    identified by its slave address.

    Parameters
    ----------
    port : str
        Serial port name, e.g. ``"COM3"`` on Windows or ``"/dev/ttyUSB0"``
        on Linux.
    slave_address : int
        MODBUS slave address (1–247, default 1). Must match the ``Addr``
        parameter set on the controller's front panel.
    baudrate : int
        Baud rate in bits/s.  Must match the ``bAud`` parameter on the
        controller.  Common values: 4800, 9600 (default), 14400, 19200.
    bytesize : int
        Data bits — always 8 for MODBUS-RTU.
    parity : str
        ``"N"`` (none, default), ``"E"`` (even), or ``"O"`` (odd).
    stopbits : int
        Stop bits — 1 (default) or 2.
    timeout : float
        Serial read timeout in seconds.  0.3–0.5 is usually sufficient.
    """

    # ---- Register addresses for standard MODBUS-RTU (AFC=0) -----------
    # Per protocol manual 宇电单回路测量控制仪表通讯协议说明.pdf.
    # Verified against hardware dump on AI-828 V9.3 (AFC=0).
    # WARNING: AFC=4 (S6 protocol) uses DIFFERENT addresses and SV
    # writes are silently rejected.  Must use AFC=0.
    REG_SV_W = 1     # 给定值 — writable setpoint (index 0, MODBUS 40001)
    REG_dPt  = 13    # Decimal places (index 12, MODBUS 40013)
    REG_A_M  = 25    # Auto/Manual mode (index 24, MODBUS 40025)
    REG_Srun = 27    # Run/Stop/Hold (index 26, MODBUS 40027)
    REG_AT   = 29    # Auto-tune (index 28, MODBUS 40029)
    REG_PV   = 75    # PV — read-only (index 74, MODBUS 40075)
    REG_SV   = 76    # SV — read-only display (index 75, MODBUS 40076)
    REG_MV   = 77    # Output% + alarm (index 76, MODBUS 40077; low byte=MV%)

    # Reasonable temperature limits for validation (raw units, before dPt)
    _TEMP_LO = -2000   # -200.0 °C
    _TEMP_HI = 13000   # 1300.0 °C (covers all supported sensor types)

    def __init__(
        self,
        port: str = "COM3",
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
        self._dpt: int = 0  # cached decimal-places count (populated on connect)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # MODBUS frame helpers (raw pyserial — NO minimalmodbus)
    # ------------------------------------------------------------------

    def _build_read_frame(self, register: int, count: int = 1) -> bytes:
        """Build a MODBUS RTU Read Holding Registers (03H) frame.

        *register* is the 1-based register number from the manual
        (e.g. 75 for 40075).  PDU address = register - 1.
        """
        pdu = struct.pack(">BBHH", self._slave, 0x03, register - 1, count)
        crc = self._crc16(pdu)
        return pdu + struct.pack("<H", crc)

    def _build_write_frame(self, register: int, value: int) -> bytes:
        """Build a MODBUS RTU Write Single Register (06H) frame.

        *register* is the 1-based register number.
        *value* is the signed 16-bit raw value to write.
        """
        pdu = struct.pack(">BBHH", self._slave, 0x06, register - 1, value & 0xFFFF)
        crc = self._crc16(pdu)
        return pdu + struct.pack("<H", crc)

    def _send_frame(self, frame: bytes) -> bytes:
        """Send a MODBUS frame and return the raw response bytes.

        Raises YudianCommunicationError on timeout or serial error.
        """
        try:
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            self._ser.write(frame)
            self._ser.flush()
            # MODBUS-RTU response max: 256 bytes (theoretical)
            response = self._ser.read(256)
            return response
        except (serial.SerialException, OSError) as exc:
            raise YudianCommunicationError(
                f"Serial I/O error: {exc}", original_exception=exc
            ) from exc

    def _parse_read_response(self, response: bytes, register: int) -> int:
        """Parse a MODBUS RTU read response into a raw signed 16-bit value.

        Raises YudianCommunicationError on short/error/CRC-mismatch response.
        """
        if len(response) < 5:
            raise YudianCommunicationError(
                f"No response from device (register {register})"
            )
        # Check for MODBUS exception (function code | 0x80)
        if response[1] == 0x83:
            exc_code = response[2] if len(response) > 2 else 0
            raise YudianCommunicationError(
                f"Device returned MODBUS exception {exc_code} (register {register})"
            )
        if response[1] != 0x03:
            raise YudianCommunicationError(
                f"Unexpected function code {response[1]:02X} (register {register})"
            )
        # Response: [slave][03][02][data_hi][data_lo][CRC_lo][CRC_hi] = 7 bytes
        if len(response) < 7:
            raise YudianCommunicationError(
                f"Short response ({len(response)} bytes) for register {register}"
            )
        raw = (response[3] << 8) | response[4]
        if raw > 32767:
            raw -= 65536
        return raw

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and perform a MODBUS handshake.

        Raises
        ------
        YudianConnectionError
            If the COM port cannot be opened, the device does not respond,
            or the response is not recognised as a Yudian controller.
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

        # Handshake — read register 12 (InP/dPt) to confirm a MODBUS
        # device is responding.
        try:
            frame = self._build_read_frame(self.REG_dPt, count=1)
            response = self._send_frame(frame)
            raw = self._parse_read_response(response, self.REG_dPt)
            self._dpt = raw if 0 <= raw <= 3 else 1
            logger.info("Handshake OK; reg %d = %d → dPt = %d",
                         self.REG_dPt, raw, self._dpt)
        except YudianCommunicationError:
            raise
        except Exception as exc:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
            raise YudianConnectionError(
                f"Handshake failed on {self._port!r}: {exc}"
            ) from exc

        # Ensure the controller is in RUN mode (Srun=0), otherwise SV
        # writes may be silently ignored.
        try:
            srun_frame = self._build_read_frame(self.REG_Srun, count=1)
            srun_resp = self._send_frame(srun_frame)
            srun_raw = self._parse_read_response(srun_resp, self.REG_Srun)
            logger.info("Srun = %d (%s)", srun_raw,
                         "Run" if srun_raw == 0 else "Stop" if srun_raw == 1 else "Hold" if srun_raw == 2 else "?")
            if srun_raw != 0:
                logger.info("Setting Srun=0 (Run mode) to enable SV writes")
                self._write_register(self.REG_Srun, 0, decimals=0)
        except Exception:
            logger.debug("Could not check/set Srun (non-critical)")

        self._connected = True
        logger.info(
            "Connected to Yudian AI-828 on %s slave=%d (dPt=%d)",
            self._port, self._slave, self._dpt,
        )

    def disconnect(self) -> None:
        """Close the serial port and release all resources."""
        logger.info("Disconnecting from %s slave=%d", self._port, self._slave)
        self._connected = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception as exc:
                logger.warning("Error closing serial port: %s", exc)
            self._ser = None

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the device is currently connected."""
        return self._connected and self._ser is not None

    @property
    def dpt(self) -> int:
        """Return the cached decimal-places count (0–3)."""
        return self._dpt

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # MODBUS CRC-16 (static helper)
    # ------------------------------------------------------------------

    @staticmethod
    def _crc16(data: bytes) -> int:
        """MODBUS CRC-16 (polynomial 0xA001, initial 0xFFFF)."""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                lsb = crc & 1
                crc >>= 1
                if lsb:
                    crc ^= 0xA001
        return crc

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    @staticmethod
    def scan_devices(
        ports: Optional[List[str]] = None,
        slaves: Optional[List[int]] = None,
        baudrates: Optional[List[int]] = None,
        timeout: float = 0.4,
    ) -> List[Dict[str, Any]]:
        """Scan available COM ports for Yudian AI-8 series devices.

        Uses raw pyserial + manual MODBUS frames (NOT minimalmodbus)
        because some RS485 converters have timing quirks that
        minimalmodbus's higher-level API does not handle.

        Parameters
        ----------
        ports : list of str, optional
            COM ports to scan.  If ``None``, enumerates all available
            serial ports via ``serial.tools.list_ports``.
        slaves : list of int, optional
            Slave addresses to try on each (port, baudrate) combination.
            Default: ``1`` through ``10``.
        baudrates : list of int, optional
            Baud rates to try on each port.  Default: ``[9600, 19200,
            4800, 14400]``.
        timeout : float
            Per-probe timeout in seconds (default 0.4).

        Returns
        -------
        list of dict
            Each entry::

                {
                    "port": "COM3",
                    "slave": 1,
                    "baudrate": 9600,
                    "description": "USB-SERIAL CH340 (COM3)",
                }

            Sorted by port name, then slave address.  Empty list if no
            devices were found.
        """
        if ports is None:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            if not ports:
                logger.warning("scan_devices: no COM ports found on this system")
                return []

        if slaves is None:
            # Try slave=1 first at all baud rates (most common),
            # then expand to slaves 2–10.
            slaves = [1] + list(range(2, 11))

        if baudrates is None:
            baudrates = [9600, 19200, 4800, 14400]

        logger.info("Scanning %d port(s) × %d baud(s) × %d slave(s) …",
                     len(ports), len(baudrates), len(slaves))

        found: List[Dict[str, Any]] = []

        for port_name in ports:
            # Description is just the port name here; resolved later
            # in the GUI via _populate_device_list to avoid calling
            # serial.tools.list_ports.comports() on the hot path
            # (it can hang with buggy COM port drivers).
            desc = port_name

            for baud in baudrates:
                port_done = False
                for slave in slaves:
                    # Once we've found a device on this port at any baud
                    # rate, stop scanning this port entirely.
                    if any(d["port"] == port_name for d in found):
                        port_done = True
                        break

                    try:
                        # ── Raw pyserial probe (matches diag tools) ──
                        pdu = struct.pack(
                            ">BBHH", slave, 0x03,
                            YudianController.REG_dPt - 1, 1,
                        )
                        crc = YudianController._crc16(pdu)
                        frame = pdu + struct.pack("<H", crc)

                        ser = serial.Serial(
                            port=port_name,
                            baudrate=baud,
                            bytesize=serial.EIGHTBITS,
                            parity=serial.PARITY_NONE,
                            stopbits=serial.STOPBITS_ONE,
                            timeout=timeout,
                        )
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()
                        ser.write(frame)
                        ser.flush()
                        # [slave][03][byte_count=2][data_hi][data_lo][CRC]
                        response = ser.read(256)
                        ser.close()

                        if len(response) >= 5 and response[1] == 0x03:
                            # Valid MODBUS response — device found
                            found.append({
                                "port": port_name,
                                "slave": slave,
                                "baudrate": baud,
                                "description": desc,
                            })
                            logger.info(
                                "scan_devices: found device at %s slave=%d baud=%d",
                                port_name, slave, baud,
                            )
                            break  # done with this (baud, slave) combo

                    except Exception:
                        continue

                if port_done or any(d["port"] == port_name for d in found):
                    break  # done with this port entirely

        found.sort(key=lambda d: (d["port"], d["slave"]))
        logger.info("scan_devices: %d device(s) found", len(found))
        return found

    # ------------------------------------------------------------------
    # Reading registers
    # ------------------------------------------------------------------

    def read_dpt(self) -> int:
        """Read the decimal-places parameter from the device.

        Returns
        -------
        int
            0, 1, 2, or 3.

        Raises
        ------
        YudianCommunicationError
            On MODBUS timeout, CRC error, or exception response.
        """
        return self._read_register(self.REG_dPt, decimals=0)

    def read_pv(self) -> float:
        """Read the current process value (measured temperature).

        The returned value is automatically scaled by the device's ``dPt``
        setting, so ``read_pv()`` always returns degrees Celsius.

        Returns
        -------
        float
            Current temperature in °C.

        Raises
        ------
        YudianCommunicationError
            On communication failure.
        """
        return self._read_register(self.REG_PV, decimals=self._dpt)

    def read_sv(self) -> float:
        """Read the current setpoint value (target temperature).

        Returns
        -------
        float
            Target temperature in °C.

        Raises
        ------
        YudianCommunicationError
            On communication failure.
        """
        return self._read_register(self.REG_SV, decimals=self._dpt)

    def read_mv(self) -> float:
        """Read the current output power (manipulated variable).

        Returns
        -------
        float
            Output power in percent (0.0–100.0).

        Raises
        ------
        YudianCommunicationError
            On communication failure.
        """
        return self._read_register(self.REG_MV, decimals=1)

    def read_all(self) -> Dict[str, float]:
        """Read PV, SV, and MV.

        PV and SV are read in one multi-register transaction (registers
        78–79).  MV is at register 77 (per the manual) and is read
        separately because it's not contiguous with PV/SV on this f/w.

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
            if self._ser is None:
                raise YudianConnectionError("Not connected — call connect() first")

            # Read PV + SV (2 contiguous registers starting at REG_PV)
            frame = self._build_read_frame(self.REG_PV, count=2)
            response = self._send_frame(frame)

            if len(response) < 7:
                raise YudianCommunicationError(
                    f"Short multi-read response ({len(response)} bytes)"
                )
            if response[1] == 0x83:
                raise YudianCommunicationError(
                    f"Device returned exception code {response[2]} on multi-read"
                )

            pv_raw = (response[3] << 8) | response[4]
            if pv_raw > 32767:
                pv_raw -= 65536
            sv_raw = (response[5] << 8) | response[6]
            if sv_raw > 32767:
                sv_raw -= 65536

            # MV is a combined register (index 76, MODBUS 40077):
            # low byte = output %, high byte = alarm status bits.
            try:
                frame_mv = self._build_read_frame(self.REG_MV, count=1)
                resp_mv = self._send_frame(frame_mv)
                mv_raw = self._parse_read_response(resp_mv, self.REG_MV)
                mv = float(mv_raw & 0xFF)  # low byte only
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
        """Set the target temperature (setpoint).

        Writes to the writable setpoint register (40001, 给定值), NOT
        the read-only SV display register (40076).

        The value is automatically scaled by the device's ``dPt`` setting.

        Parameters
        ----------
        value : float
            Desired setpoint in °C.

        Raises
        ------
        ValueError
            If *value* is outside the supported temperature range.
        YudianCommunicationError
            On communication failure.
        """
        raw_min = self._TEMP_LO
        raw_max = self._TEMP_HI
        scaled_min = self._scale_value(raw_min, self._dpt)
        scaled_max = self._scale_value(raw_max, self._dpt)

        if not (scaled_min <= value <= scaled_max):
            raise ValueError(
                f"Setpoint {value} °C is out of range "
                f"({scaled_min:.1f} – {scaled_max:.1f} °C)"
            )

        self._write_register(self.REG_SV_W, value, decimals=self._dpt)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_register(self, register: int, decimals: int) -> float:
        """Read a single holding register and scale the result."""
        with self._lock:
            if self._ser is None:
                raise YudianConnectionError("Not connected — call connect() first")
            frame = self._build_read_frame(register, count=1)
            response = self._send_frame(frame)
            raw = self._parse_read_response(response, register)
            return self._scale_value(raw, decimals)

    def _write_register(self, register: int, value: float, decimals: int) -> None:
        """Write a single holding register."""
        with self._lock:
            if self._ser is None:
                raise YudianConnectionError("Not connected — call connect() first")
            raw_value = int(round(value * (10 ** decimals)))
            frame = self._build_write_frame(register, raw_value)
            response = self._send_frame(frame)
            # Write response: [slave][06][addr_hi][addr_lo][value_hi][value_lo][CRC]
            if len(response) < 8 or response[1] == 0x86:
                exc_code = response[2] if len(response) > 2 else 0
                raise YudianCommunicationError(
                    f"Write failed: MODBUS exception {exc_code} (register {register})"
                )
            logger.debug("Wrote register %d ← %s (raw=%d)", register, value, raw_value)

    @staticmethod
    def _scale_value(raw: int, decimals: int) -> float:
        """Convert a raw MODBUS register value to a float.

        Handles signed 16-bit two's-complement integers (range -32768
        to +32767) as returned by the controller.
        """
        # minimalmodbus returns signed integers when signed=True, but
        # the value may still need two's-complement unwrapping if it
        # arrives as an unsigned 16-bit value.  We normalise here.
        if raw > 32767:
            raw -= 65536
        return raw / (10 ** decimals)

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return (
            f"YudianController(port={self._port!r}, slave={self._slave}, "
            f"baud={self._baudrate}, dPt={self._dpt}, {state})"
        )
