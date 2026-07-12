"""
SigmaKoki XYZ Stage Driver
===========================

Serial UART driver for the Arduino-based SigmaKoki XYZ stage.

Protocol: colon-delimited text commands over 115200 baud, 8N1.
The Arduino firmware runs a non-blocking motion engine — the PC can
send commands at any time, including STOP during continuous moves.

Thread-safe: all serial I/O protected by ``threading.Lock``.
Unsolicited events (EV:LIM, BOOT) are filtered from command responses
by the ``_read_response`` method.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import serial

logger = logging.getLogger("transfer_stage.sigmakoki")

# Speed level → approximate steps/sec (for display/reporting)
SPEED_LEVEL_TO_HZ = {0: 25, 1: 50, 2: 100, 3: 167, 4: 250, 5: 500}


def _hz_to_speed_level(hz: float) -> int:
    """Map a steps/sec value to the closest Arduino speed level (0–5)."""
    if hz <= 37:
        return 0
    elif hz <= 75:
        return 1
    elif hz <= 133:
        return 2
    elif hz <= 208:
        return 3
    elif hz <= 375:
        return 4
    else:
        return 5


class SigmaKokiDriver:
    """Serial driver for the Arduino-controlled SigmaKoki XYZ stage.

    Parameters
    ----------
    port : str
        COM port name, e.g. ``"COM3"``.
    baudrate : int
        Baud rate (default 115200).
    timeout : float
        Serial read timeout in seconds.
    """

    def __init__(
        self,
        port: str = "",
        baudrate: int = 115200,
        timeout: float = 0.3,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser: Optional[serial.Serial] = None
        self._connected = False
        self._lock = threading.Lock()
        self._event_listeners: list = []  # callbacks for unsolicited events

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and wait for the BOOT/READY message.

        Raises
        ------
        serial.SerialException
            If the port cannot be opened.
        ConnectionError
            If no BOOT message is received within the timeout.
        """
        logger.info("Opening %s at %d baud", self._port, self._baudrate)
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self._timeout,
        )
        # Flush any stale data
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

        # Wait for BOOT / READY message (Arduino may have just reset)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            line = self._read_line()
            if line is None:
                continue
            if "BOOT" in line or "READY" in line:
                logger.info("Arduino ready: %s", line.strip())
                self._connected = True
                return
        # If we didn't get BOOT, try a PING
        try:
            if self.ping():
                self._connected = True
                return
        except Exception:
            pass
        self._ser.close()
        self._ser = None
        raise ConnectionError(f"No response from Arduino on {self._port}")

    def disconnect(self) -> None:
        """Stop all axes and close the serial port."""
        logger.info("Disconnecting SigmaKoki")
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

    # ------------------------------------------------------------------
    # Motion Commands
    # ------------------------------------------------------------------

    def continuous_start(self, axis: str, direction: int, speed_hz: float) -> bool:
        """Start or update continuous movement on an axis.

        Idempotent — calling again updates direction and speed
        without stopping the axis.

        Parameters
        ----------
        axis : str
            ``"x"``, ``"y"``, or ``"z"``.
        direction : int
            +1 for positive, -1 for negative.
        speed_hz : float
            Desired speed in steps/sec (mapped to nearest speed level).

        Returns
        -------
        bool
            ``False`` if the axis is at a limit in the requested direction.
        """
        level = _hz_to_speed_level(speed_hz)
        cmd = f"MV:{axis.upper()}:{direction}:{level}\n"
        response = self._send_command(cmd)
        if response and "ERR" in response and "LIMIT" in response:
            logger.warning("SigmaKoki %s: limit prevents move dir=%d", axis, direction)
            return False
        return True

    def continuous_stop(self, axis: str) -> None:
        """Stop continuous movement on a single axis."""
        cmd = f"STOP:{axis.upper()}\n"
        self._send_command(cmd)

    def stop_all(self) -> None:
        """Emergency stop all axes immediately."""
        self._send_command("STOP:ALL\n")

    def single_step(self, axis: str, direction: int, steps: int) -> int:
        """Move an axis by a precise number of steps.

        This is briefly blocking on the Arduino side (usually <100ms).
        This method blocks until the Arduino responds.

        Returns
        -------
        int
            Actual steps moved (may be 0 if at limit).
        """
        cmd = f"STEP:{axis.upper()}:{direction}:{steps}\n"
        response = self._send_command(cmd, timeout=5.0)
        if response and "OK:STEP:" in response:
            # Parse: OK:STEP:X:<actual>
            try:
                parts = response.strip().split(":")
                return int(parts[3])
            except (IndexError, ValueError):
                pass
        return 0

    def zero(self) -> None:
        """Reset software position counters to zero (no physical movement)."""
        self._send_command("HOME\n")

    # ------------------------------------------------------------------
    # Speed Control
    # ------------------------------------------------------------------

    def set_speed_level(self, axis: str, level: int) -> None:
        """Set the speed level (0–5) for an axis."""
        level = max(0, min(5, level))
        self._send_command(f"SPD:{axis.upper()}:{level}\n")

    def set_speed_all(self, level: int) -> None:
        """Set the speed level for all axes."""
        level = max(0, min(5, level))
        self._send_command(f"SPD:ALL:{level}\n")

    # ------------------------------------------------------------------
    # Status Queries
    # ------------------------------------------------------------------

    def get_limits(self) -> Dict[str, int]:
        """Read all limit switch states.

        Returns
        -------
        dict
            Keys ``"x+"``, ``"x-"``, ``"y+"``, ``"y-"``, ``"z+"``, ``"z-"``.
            Value is 1 (triggered/at limit) or 0 (normal).
        """
        response = self._send_command("LIMITS?\n")
        return self._parse_limits(response)

    def get_status(self) -> Dict[str, Any]:
        """Read current positions and speed levels.

        Returns
        -------
        dict
            ``{"x": int, "y": int, "z": int, "xspd": int, "yspd": int, "zspd": int}``
        """
        response = self._send_command("STATUS?\n")
        return self._parse_status(response)

    def ping(self) -> bool:
        """Send PING, expect PONG.  Returns ``True`` if device is alive."""
        response = self._send_command("PING\n")
        return response is not None and "PONG" in response

    # ------------------------------------------------------------------
    # Internal: Serial I/O
    # ------------------------------------------------------------------

    def _send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        """Send a command and return the response line.

        Filters out unsolicited events (EV:LIM, BOOT) — they are
        dispatched to event listeners rather than returned as the
        command response.
        """
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                logger.warning("SigmaKoki: send on closed port")
                return None

            try:
                # Drain any pending unsolicited events before sending
                self._drain_events()
                self._ser.write(cmd.encode("ascii"))
                self._ser.flush()

                actual_timeout = timeout or self._timeout
                deadline = time.time() + actual_timeout

                while time.time() < deadline:
                    line = self._read_line()
                    if line is None:
                        continue
                    line = line.strip()
                    if not line:
                        continue

                    # Filter unsolicited events
                    if line.startswith("EV:") or line == "BOOT":
                        self._dispatch_event(line)
                        continue

                    # This is our command response
                    return line

                logger.debug("SigmaKoki: timeout waiting for response to %r", cmd.strip())
                return None

            except (serial.SerialException, OSError) as exc:
                logger.error("SigmaKoki: serial error: %s", exc)
                self._connected = False
                return None

    def _read_line(self) -> Optional[str]:
        """Read one line from the serial port (non-blocking, with timeout)."""
        try:
            if self._ser is None:
                return None
            line = self._ser.readline()
            if line:
                return line.decode("ascii", errors="replace")
            return None
        except (serial.SerialException, OSError):
            return None

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _drain_events(self) -> None:
        """Read and dispatch any pending unsolicited events from the serial buffer.

        Called before resetting the input buffer to avoid losing
        EV:LIM events that arrived between commands.
        """
        if self._ser is None:
            return
        try:
            while self._ser.in_waiting > 0:
                line = self._read_line()
                if line:
                    line = line.strip()
                    if line.startswith("EV:") or line == "BOOT":
                        self._dispatch_event(line)
        except (serial.SerialException, OSError):
            pass

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def add_event_listener(self, callback) -> None:
        """Register a callback for unsolicited events.

        Callback signature: ``callback(event_type: str, axis: str, direction: str)``
        e.g. ``callback("LIM", "X", "+")`` or ``callback("BOOT", "", "")``.
        """
        self._event_listeners.append(callback)

    def _dispatch_event(self, line: str) -> None:
        """Dispatch an unsolicited event to registered listeners."""
        if line.startswith("EV:LIM:"):
            # Format: EV:LIM:X+ or EV:LIM:X-
            try:
                axis = line[7]
                direction = line[8]
                logger.info("SigmaKoki event: LIM %s%s", axis, direction)
                for cb in self._event_listeners:
                    try:
                        cb("LIM", axis, direction)
                    except Exception:
                        pass
            except IndexError:
                pass
        elif line == "BOOT":
            logger.info("SigmaKoki event: BOOT (controller restarted)")
            for cb in self._event_listeners:
                try:
                    cb("BOOT", "", "")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Response Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_limits(response: Optional[str]) -> Dict[str, int]:
        """Parse LIMITS? response into a dict.

        Expected format: ``L:X+:0,X-:1,Y+:0,Y-:0,Z+:0,Z-:0``
        """
        limits: Dict[str, int] = {"x+": 0, "x-": 0, "y+": 0, "y-": 0, "z+": 0, "z-": 0}
        if not response or not response.startswith("L:"):
            return limits
        try:
            body = response[2:]  # strip "L:"
            for pair in body.split(","):
                key, val = pair.split(":")
                limits[key.lower()] = int(val)
        except (ValueError, IndexError):
            pass
        return limits

    @staticmethod
    def _parse_status(response: Optional[str]) -> Dict[str, Any]:
        """Parse STATUS? response.

        Expected format: ``S:X:1234,Y:-567,Z:42,XSPD:2,YSPD:3,ZSPD:2``
        """
        status: Dict[str, Any] = {"x": 0, "y": 0, "z": 0, "xspd": 2, "yspd": 2, "zspd": 2}
        if not response or not response.startswith("S:"):
            return status
        try:
            body = response[2:]
            for pair in body.split(","):
                key, val = pair.split(":")
                k = key.strip().lower()
                if k in ("x", "y", "z"):
                    status[k] = int(val)
                elif k in ("xspd", "yspd", "zspd"):
                    status[k] = int(val)
        except (ValueError, IndexError):
            pass
        return status

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return f"SigmaKokiDriver({self._port}, {state})"
