"""
Motorized Microscope Z-Focus Driver
===================================

Serial driver for the Arduino-based microscope Z-focus module
(firmware project: ``D:\\Projects\\motorized_focus``).

Protocol: ASCII line commands over 115200 baud, 8N1, ``\\n`` terminated,
no checksum.  One reply line per command.  Unsolicited events
(``EV:STOP``, ``EV:LIM``, ``EV:TMO``, ``EV:WDT``, ``EV:DONE``, ``BOOT:``)
may arrive between replies and are filtered by prefix — a reader must
never assume the next line answers the last command.

Simplified integration: continuous movement only (``SPD:<n>`` with
signed speed in steps/s, ``SPD:0`` = ramp stop, ``STOP`` = immediate
abort).  The analog-trigger mapping (gamma/deadzone/min/max/invert) is
computed in ``input_system.action_resolver``; this driver only applies
speed commands with client-side rate limiting so the 60 Hz input loop
does not spam the serial line.

Thread-safe: all serial I/O protected by ``threading.Lock``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import serial

logger = logging.getLogger("transfer_stage.focus")

# Firmware constants (compile-time in the firmware; mirrored client-side)
MIN_SPEED_SPS = 10        # firmware clamps |SPD| up to this value
HARD_MAX_SPS = 5000       # CFG:MAX clamp upper bound
DEFAULT_ACCEL_SPS2 = 20000  # firmware default; not touched by this driver

# Client-side SPD send rate limiting (mirrors the reference PC client)
RATE_MIN_CHANGE_FRAC = 0.05  # resend if |Δspeed| >= 5% of max speed
RATE_MIN_INTERVAL_S = 0.10   # ...or at least 100 ms elapsed

# Connection handshake (Arduino auto-resets on port open, ~2 s boot delay)
HANDSHAKE_TIMEOUT_S = 5.0
PING_INTERVAL_S = 0.5


def _clamp_max_speed(max_speed: float) -> int:
    """Clamp a max-speed value to the firmware CFG:MAX range [10, 5000]."""
    return int(max(MIN_SPEED_SPS, min(HARD_MAX_SPS, max_speed)))


class FocusDriver:
    """Serial driver for the motorized microscope Z-focus module.

    Parameters
    ----------
    port : str
        COM port name, e.g. ``"COM5"``.
    baudrate : int
        Baud rate (firmware-fixed at 115200).
    timeout : float
        Serial read timeout in seconds.
    max_speed : float
        Upper speed limit in steps/s, applied to the firmware via
        ``CFG:MAX`` on connect (clamped to [10, 5000]).
    """

    def __init__(
        self,
        port: str = "",
        baudrate: int = 115200,
        timeout: float = 0.5,
        max_speed: float = 2000,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._max_speed = _clamp_max_speed(max_speed)
        self._ser: Optional[serial.Serial] = None
        self._connected = False
        self._lock = threading.Lock()
        self._last_command_time = 0.0
        # Continuous-move bookkeeping
        self._active = False                # trigger currently commanding movement
        self._last_sent_speed: Optional[int] = None
        self._last_send_t = 0.0
        self._mode = "IDLE"                 # cached from STATUS? (for TRAP preempt)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and handshake with the firmware.

        Opening the port resets the Arduino (~2 s bootloader delay), so
        ``PING`` is retried until ``PONG``/``READY``/``BOOT:`` arrives
        (or the handshake timeout elapses).  On success, a safety
        ``STOP`` is sent, then ``CFG:MAX`` is applied, and one
        ``STATUS?`` seeds the mode cache.

        Raises
        ------
        serial.SerialException
            If the port cannot be opened.
        ConnectionError
            If no firmware response arrives within the handshake timeout,
            or if the post-handshake configuration burst (STOP/CFG:MAX)
            gets no replies.
        """
        logger.info("Opening focus port %s at %d baud", self._port, self._baudrate)
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

        deadline = time.time() + HANDSHAKE_TIMEOUT_S
        saw_boot = False
        while time.time() < deadline:
            line = self._read_line()
            if line:
                line = line.strip()
                if line.startswith("EV:") or line.startswith("BOOT:"):
                    logger.debug("Focus handshake event: %s", line)
                    if line.startswith("BOOT:"):
                        saw_boot = True
                if "PONG" in line or "READY" in line or "BOOT:" in line:
                    logger.info("Focus firmware ready%s: %s",
                                " (boot banner)" if saw_boot else "", line)
                    self._connected = True
                    break
            if time.time() >= deadline:
                break
            if not self._connected:
                # Arduino may still be in the bootloader — retry PING
                try:
                    self._ser.write(b"PING\n")
                    self._ser.flush()
                except (serial.SerialException, OSError):
                    break
                time.sleep(PING_INTERVAL_S)

        if not self._connected:
            self._ser.close()
            self._ser = None
            raise ConnectionError(f"No response from focus firmware on {self._port}")

        # Reconnect safety + configuration.  A dead command channel must
        # fail the connection (not report a green dot), so STOP/CFG:MAX
        # without replies raise; STATUS? is advisory only (the 2 Hz poll
        # reseeds the mode cache).
        try:
            if self._send_command("STOP") is None:
                raise ConnectionError("no reply to STOP")
            applied = self._send_command(f"CFG:MAX:{self._max_speed}")
            if applied is None:
                raise ConnectionError("no reply to CFG:MAX")
            if f"OK:CFG:MAX:{self._max_speed}" not in applied:
                logger.warning("Focus CFG:MAX mismatch: expected %d, got %r",
                               self._max_speed, applied)
            status_reply = self._send_command("STATUS?")
            if status_reply is None:
                logger.warning("Focus: STATUS? unanswered; mode cache unseeded")
            else:
                self._mode = self._parse_status(status_reply).get("mode", "IDLE")
        except (ConnectionError, serial.SerialException, OSError) as exc:
            logger.error("Focus: handshake config failed: %s", exc)
            self._connected = False
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
            raise

    def disconnect(self) -> None:
        """Ramp-stop, abort, and close the serial port."""
        logger.info("Disconnecting focus")
        for cmd in ("SPD:0", "STOP"):
            try:
                self._send_command(cmd)
            except Exception:
                pass
        with self._lock:
            self._connected = False
            self._active = False
            self._last_sent_speed = None
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

    def continuous_start(self, axis: str, direction: int, speed: float) -> bool:
        """Start or update continuous movement (signed speed, steps/s).

        ``axis`` is accepted for interface compatibility and ignored
        (single-axis device).  The resolver re-emits this every frame
        while the trigger is held; actual serial sends are rate-limited
        here to avoid saturating the firmware.

        Returns
        -------
        bool
            ``False`` if the firmware rejected the command (``ERR:BUSY``
            or ``ERR:LIMIT``).
        """
        mag = int(round(max(0.0, min(float(speed), self._max_speed))))
        signed = mag if direction > 0 else -mag

        if signed == 0:
            self._send_command("SPD:0")
            self._active = False
            self._last_sent_speed = None
            return True

        now = time.monotonic()
        if (
            self._active
            and self._last_sent_speed is not None
            and abs(signed - self._last_sent_speed) < RATE_MIN_CHANGE_FRAC * self._max_speed
            and (now - self._last_send_t) < RATE_MIN_INTERVAL_S
        ):
            return True  # within rate-limit window — keep current speed

        with self._lock:
            if self._mode == "TRAP":
                # Preempt a point-to-point move before issuing SPD
                self._send_command_locked("STOP")
                self._mode = "IDLE"
            response = self._send_command_locked(f"SPD:{signed}")
            if response and ("ERR:BUSY" in response or "ERR:LIMIT" in response):
                logger.warning("Focus SPD:%d rejected: %s", signed, response.strip())
                return False
            self._last_sent_speed = signed
            self._last_send_t = now
            self._active = True
            return True

    def continuous_stop(self, axis: str) -> None:
        """Ramp-stop the axis (``SPD:0``) — release semantics.

        Sends exactly one ``SPD:0``; the rate-limit state is reset so a
        subsequent start always sends a fresh ``SPD:``.
        """
        self._send_command("SPD:0")
        with self._lock:
            self._active = False
            self._last_sent_speed = None
            self._last_send_t = 0.0

    def stop_all(self) -> None:
        """Immediate abort — used on emergency paths only (Escape, close)."""
        self._send_command("STOP")
        with self._lock:
            self._active = False
            self._last_sent_speed = None
            self._last_send_t = 0.0

    def single_step(self, axis: str, direction: int, steps: int) -> int:
        """Not supported on the focus module (continuous movement only)."""
        logger.debug("Focus: single_step not supported (axis=%s)", axis)
        return 0

    # ------------------------------------------------------------------
    # Status Queries
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Query firmware status (``STATUS?``).

        Returns
        -------
        dict
            ``{"position": int, "mode": str, "velocity": int,
              "target_speed": int, "limit": int, "soft_limit": int}``
            with defaults for missing/parseable keys.
        """
        response = self._send_command("STATUS?")
        status = self._parse_status(response)
        self._mode = status.get("mode", "IDLE")
        return status

    def get_limits(self) -> Dict[str, int]:
        """Derive limit flags from the STATUS ``LIM`` field (display-only).

        Motion gating stays firmware-side; these values are never used
        to block commands.
        """
        status = self.get_status()
        lim = status.get("limit", 0)
        return {"z+": 1 if lim > 0 else 0, "z-": 1 if lim < 0 else 0}

    def ping(self) -> bool:
        """Send PING, expect PONG.  Returns ``True`` if device is alive."""
        response = self._send_command("PING")
        return response is not None and "PONG" in response

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_max_speed(self, max_speed: float) -> None:
        """Apply a new max speed via ``CFG:MAX`` (firmware clamps and
        EEPROM-persists the value)."""
        clamped = _clamp_max_speed(max_speed)
        response = self._send_command(f"CFG:MAX:{clamped}")
        if response and f"OK:CFG:MAX:{clamped}" in response:
            self._max_speed = clamped
            logger.info("Focus max speed set to %d", clamped)
        else:
            logger.warning("Focus CFG:MAX:%d unexpected reply: %r", clamped, response)

    # ------------------------------------------------------------------
    # Internal: Serial I/O
    # ------------------------------------------------------------------

    def _send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        """Send a command and return the reply line (unsolicited events
        filtered out)."""
        with self._lock:
            return self._send_command_locked(cmd, timeout)

    def _send_command_locked(
        self, cmd: str, timeout: Optional[float] = None
    ) -> Optional[str]:
        """Caller must hold ``self._lock``."""
        if self._ser is None or not self._ser.is_open:
            logger.debug("Focus: send %r on closed port", cmd.strip())
            return None

        # Firmware is line-based: commands are only processed once a
        # newline arrives (``\r`` is ignored).  Normalize here so every
        # call site is safe.
        if not cmd.endswith("\n"):
            cmd += "\n"

        try:
            self._last_command_time = time.time()
            self._drain_events_locked()
            logger.debug("Focus TX: %r", cmd.strip())
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

                # Unsolicited events may interleave with replies —
                # never assume the next line answers the last command.
                if line.startswith("EV:") or line.startswith("BOOT:"):
                    logger.debug("Focus event: %s", line)
                    self._handle_event_locked(line)
                    continue

                logger.debug("Focus RX: %r", line)
                return line

            logger.debug("Focus: timeout waiting for response to %r", cmd.strip())
            return None

        except (serial.SerialException, OSError) as exc:
            logger.error("Focus: serial error: %s", exc)
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

    def _drain_events_locked(self) -> None:
        """Read and log any pending unsolicited events from the serial buffer."""
        if self._ser is None:
            return
        try:
            while self._ser.in_waiting > 0:
                line = self._read_line()
                if line:
                    stripped = line.strip()
                    if stripped.startswith("EV:") or stripped.startswith("BOOT:"):
                        logger.debug("Focus event: %s", stripped)
                        self._handle_event_locked(stripped)
        except (serial.SerialException, OSError):
            pass

    def _handle_event_locked(self, line: str) -> None:
        """Track firmware state changes from unsolicited events."""
        if line.startswith("EV:STOP:"):
            self._active = False
        elif line.startswith("EV:TMO:"):
            # Firmware auto-stopped on serial inactivity
            self._active = False
            logger.warning("Focus firmware TMO auto-stop: %s", line)

    # ------------------------------------------------------------------
    # Response Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_status(response: Optional[str]) -> Dict[str, Any]:
        """Parse a ``STATUS?`` reply.

        Expected format:
        ``S:POS:<p>,MODE:<m>,V:<v>,SPD:<t>,LIM:<b>,SLIM:<o>``
        """
        status: Dict[str, Any] = {
            "position": 0, "mode": "IDLE", "velocity": 0,
            "target_speed": 0, "limit": 0, "soft_limit": 0,
        }
        if not response or not response.startswith("S:"):
            return status
        try:
            body = response[2:]
            for pair in body.split(","):
                key, val = pair.split(":")
                key = key.strip().lower()
                if key == "pos":
                    status["position"] = int(val)
                elif key == "mode":
                    status["mode"] = val.strip()
                elif key == "v":
                    status["velocity"] = int(val)
                elif key == "spd":
                    status["target_speed"] = int(val)
                elif key == "lim":
                    status["limit"] = int(val)
                elif key == "slim":
                    status["soft_limit"] = int(val)
        except (ValueError, IndexError):
            pass
        return status

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @property
    def last_command_time(self) -> float:
        return self._last_command_time

    def __repr__(self) -> str:
        state = "connected" if self._connected else "disconnected"
        return f"FocusDriver({self._port}, {state})"
