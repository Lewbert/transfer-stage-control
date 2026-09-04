"""
Fake-serial regression tests for the Focus and Zolix drivers.

Runs without hardware: patches ``serial.Serial`` with an in-memory fake
whose replies are scripted.  Verifies the v0.4.1 fixes:

  Focus:  every command is ``\\n``-terminated (the v0.4.0 bug),
          handshake replies parsed, connect() fails loudly on a dead
          command channel, events filtered from replies.
  Zolix:  opcode rate limiting (no per-frame serial spam), bounded
          pre-stop poll, stop retry on lost replies (no silent success),
          stale-response discard, fail-backoff, generation-guarded
          post-stop verification (escalation vs. legitimate restart),
          decel stop_all per axis, single_step generation bump.

Run:  python tests/fake_serial_tests.py     (exit code 0 = pass)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

# Make the project root importable when run as a plain script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial

from utils.modbus_rtu import crc16

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Fake serial ports
# ---------------------------------------------------------------------------


class FakeSerial:
    """In-memory serial port.  ``_on_write`` schedules reply bytes as
    ``(due_monotonic, bytes)`` items; ``read``/``readline`` block until
    data is due or the port timeout elapses."""

    def __init__(self, port=None, baudrate=None, bytesize=None, parity=None,
                 stopbits=None, timeout=None, write_timeout=None, **kw):
        self._timeout = timeout if timeout is not None else 0.15
        self.is_open = True
        self.writes: list = []
        self._queue = []          # (due_monotonic, bytes)
        self._buf = bytearray()
        # Reentrant: write() holds the lock while _on_write → schedule()
        # needs it again.
        self._lock = threading.RLock()
        self.on_write_extra = None  # optional extra payload scheduling hook

    # -- plumbing --------------------------------------------------------
    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(bytes(data))
            self._on_write(bytes(data))
            if self.on_write_extra:
                self.on_write_extra(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    # -- scheduling ------------------------------------------------------
    def schedule(self, data: bytes, delay: float = 0.005) -> None:
        with self._lock:
            self._queue.append((time.monotonic() + delay, data))

    def _collect_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            due, rest = [], []
            for item in self._queue:
                (due.append(item[1]) if item[0] <= now else rest.append(item))
            self._queue = rest
            for chunk in due:
                self._buf += chunk

    # -- reading ---------------------------------------------------------
    @property
    def in_waiting(self) -> int:
        self._collect_due()
        with self._lock:
            return len(self._buf)

    def read(self, n: int = 1) -> bytes:
        # Poll in small increments until data is due or the port timeout
        # elapses — mirrors a real port where bytes arrive mid-wait.
        deadline = time.monotonic() + (self._timeout if self._timeout is not None else 0.15)
        while time.monotonic() < deadline:
            self._collect_due()
            with self._lock:
                if self._buf:
                    out = bytes(self._buf[:n])
                    del self._buf[:n]
                    return out
            time.sleep(0.002)
        return b""

    def readline(self) -> bytes:
        deadline = time.monotonic() + (self._timeout or 0.15)
        while time.monotonic() < deadline:
            self._collect_due()
            with self._lock:
                if b"\n" in self._buf:
                    idx = self._buf.index(b"\n") + 1
                    out = bytes(self._buf[:idx])
                    del self._buf[:idx]
                    return out
            time.sleep(0.002)
        return b""

    def _on_write(self, data: bytes) -> None:
        pass


class FocusFakeSerial(FakeSerial):
    """Focus firmware simulator — line-based, newline-strict.

    Every non-empty command must arrive newline-terminated (this is the
    v0.4.0 regression check); replies come from the *replies* table.
    Commands listed in *silent* get no reply.
    """

    def __init__(self, replies: dict, silent: set = (), **kw):
        super().__init__(**kw)
        self.replies = replies
        self.silent = set(silent)
        self.bad_terminated: list = []   # unterminated writes (bug evidence)

    def _on_write(self, data: bytes) -> None:
        for token in data.split(b"\n"):
            cmd = token.strip()
            if not cmd:
                continue
            if not data.endswith(b"\n"):
                self.bad_terminated.append(cmd.decode("ascii", "replace"))
                continue
            key = cmd.decode("ascii", "replace")
            if key in self.silent:
                continue
            reply = self.replies.get(key)
            if reply is not None:
                self.schedule(reply.encode("ascii") + b"\n")
            else:
                self.schedule(("OK:" + key).encode("ascii") + b"\n")


# MODBUS helpers -------------------------------------------------------------


def mb_read_reply(slave: int, values: list) -> bytes:
    """Build a 0x04 multi-register read reply (values = raw 16-bit words)."""
    n = len(values)
    body = bytes([slave, 0x04, n * 2]) + b"".join(v.to_bytes(2, "big") for v in values)
    return body + crc16(body).to_bytes(2, "little")


def mb_exception(slave: int, fn: int, exc: int) -> bytes:
    body = bytes([slave, fn | 0x80, exc])
    return body + crc16(body).to_bytes(2, "little")


def mb_echo(frame: bytes) -> bytes:
    return frame[:6] + crc16(frame[:6]).to_bytes(2, "little")


class ZolixFakeSerial(FakeSerial):
    """ZC300 simulator.  *rules*: list of ``(fn, register_or_None,
    responder)`` — first match wins; *responder* is a callable
    ``(frame) -> [(delay, bytes), ...] | None`` (None = silence).
    Default rules echo writes and answer reads with zeros."""

    def __init__(self, rules=(), latency: float = 0.005, **kw):
        super().__init__(**kw)
        self.rules = list(rules)
        self.latency = latency

    def _on_write(self, data: bytes) -> None:
        fn = data[1]
        register = ((data[2] << 8) | data[3]) + 1
        for rule_fn, rule_reg, responder in self.rules:
            if fn == rule_fn and (rule_reg is None or rule_reg == register):
                replies = responder(data)
                if replies:
                    for delay, payload in replies:
                        self.schedule(payload, delay)
                return
        # defaults
        if fn == 0x06:
            self.schedule(mb_echo(data), self.latency)
        elif fn == 0x10:
            self.schedule(mb_echo(data), self.latency)
        elif fn == 0x04:
            count = data[5]
            self.schedule(mb_read_reply(data[0], [0] * count), self.latency)


def frames_to(fake: FakeSerial, fn: int, register: int) -> list:
    return [
        w for w in fake.writes
        if len(w) > 3 and w[1] == fn and ((w[2] << 8) | w[3]) + 1 == register
    ]


def opcode_of(frame: bytes) -> int:
    """Opcode value of a 0x10 opcode-write frame (2 bytes, big-endian)."""
    return (frame[7] << 8) | frame[8]


def param_of(frame: bytes, index: int) -> int:
    """Parameter word *index* (0-based) of a 0x10 opcode-write frame."""
    return (frame[9 + 2 * index] << 8) | frame[10 + 2 * index]


def wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def instantiator(fake):
    """Return a serial.Serial replacement class that always yields *fake*.

    Overrides ``__init__`` too — returning an existing instance from
    ``__new__`` would otherwise re-initialize (and wipe) the fake.
    """
    class _Instantiating(type(fake)):
        def __new__(cls, *a, **k):
            return fake

        def __init__(self, *a, **k):
            pass

    return _Instantiating


# ---------------------------------------------------------------------------
# Focus scenarios
# ---------------------------------------------------------------------------

FOCUS_REPLIES = {
    "PING": "PONG",
    "STOP": "OK:STOP",
    "CFG:MAX:2000": "OK:CFG:MAX:2000",
    "STATUS?": "S:POS:0,MODE:IDLE,V:0,SPD:0,LIM:0,SLIM:0",
    "SPD:500": "OK:SPD:500",
    "SPD:0": "OK:SPD:0",
    "SPD:-500": "OK:SPD:-500",
}


def test_focus_newline_and_connect():
    from stage_control.hardware.focus import FocusDriver

    fake = FocusFakeSerial(FOCUS_REPLIES, timeout=0.5)
    fake.schedule(b"BOOT:FOCUSCTRL:1.0\n", delay=0.01)  # banner on reset
    serial.Serial = instantiator(fake)
    d = FocusDriver(port="FAKE", max_speed=2000)
    d.connect()
    assert d.is_connected, "focus connect failed"
    assert d._mode == "IDLE", f"mode cache not seeded: {d._mode}"
    assert not fake.bad_terminated, f"unterminated commands: {fake.bad_terminated}"
    assert fake.writes and all(w.endswith(b"\n") for w in fake.writes), \
        [w for w in fake.writes if not w.endswith(b"\n")]

    # continuous movement
    before = len(fake.writes)
    assert d.continuous_start("z", 1, 500) is True
    assert fake.writes[-1] == b"SPD:500\n", fake.writes[-1]
    assert d.continuous_start("z", 1, 500) is True          # rate-limited
    assert len(fake.writes) == before + 1, "rate limit failed"
    time.sleep(0.15)
    assert d.continuous_start("z", 1, 500) is True
    assert len(fake.writes) == before + 2, "re-send after interval failed"
    d.continuous_stop("z")
    assert fake.writes[-1] == b"SPD:0\n", fake.writes[-1]
    print("  focus newline + connect + rate limit ... OK")


def test_focus_event_filter():
    from stage_control.hardware.focus import FocusDriver

    fake = FocusFakeSerial(FOCUS_REPLIES, timeout=0.5)
    fake.schedule(b"BOOT:FOCUSCTRL:1.0\n", delay=0.01)
    fake.schedule(b"EV:LIM:+:123\n", delay=0.02)
    serial.Serial = instantiator(fake)
    d = FocusDriver(port="FAKE", max_speed=2000)
    d.connect()
    reply = d._send_command("PING")
    assert reply and "PONG" in reply, f"event filtered wrongly: {reply!r}"
    print("  focus event interleave filter ... OK")


def test_focus_connect_fails_loudly():
    from stage_control.hardware.focus import FocusDriver

    fake = FocusFakeSerial(FOCUS_REPLIES, silent={"STOP", "CFG:MAX:2000", "STATUS?"},
                           timeout=0.5)
    fake.schedule(b"BOOT:FOCUSCTRL:1.0\n", delay=0.01)
    serial.Serial = instantiator(fake)
    d = FocusDriver(port="FAKE", max_speed=2000)
    try:
        d.connect()
        raise AssertionError("connect() should have raised ConnectionError")
    except ConnectionError:
        pass
    assert not d.is_connected, "dead command channel must not report connected"
    print("  focus dead-channel connect failure ... OK")


# ---------------------------------------------------------------------------
# Zolix scenarios
# ---------------------------------------------------------------------------

REG_MOTION = 30012
REG_OPCODE = 30050
REG_SPEED_CONST_X = 30129


def _zolix_driver(fake, stop_mode="immediate"):
    from stage_control.hardware.zolix import ZolixDriver

    serial.Serial = instantiator(fake)
    return ZolixDriver(port="FAKE", timeout=0.15, stop_mode=stop_mode)


def test_zolix_rate_limit():
    fake = ZolixFakeSerial()
    d = _zolix_driver(fake)
    d.connect()
    n_start = len(fake.writes)

    for _ in range(20):
        assert d.continuous_start("x", 1, 1000) is True
    ops = frames_to(fake, 0x10, REG_OPCODE)
    speeds = frames_to(fake, 0x10, REG_SPEED_CONST_X)
    assert len(ops) == 1, f"OP_CONTINUOUS spam: {len(ops)} frames"
    assert len(speeds) == 1, f"speed write spam: {len(speeds)} frames"

    # no time window: same command later still does zero I/O
    time.sleep(0.5)
    before = len(fake.writes)
    assert d.continuous_start("x", 1, 1000) is True
    assert len(fake.writes) == before, "fast path did serial I/O"

    # direction change → stop + bounded poll + new opcode
    fake.rules.insert(0, (0x04, REG_MOTION, lambda f: [(0.01, mb_read_reply(1, [0]))]))
    assert d.continuous_start("x", -1, 1000) is True
    assert len(frames_to(fake, 0x10, REG_OPCODE)) == 2, "direction change did not re-fire"
    assert d._last_direction["x"] == 0x4E
    print("  zolix rate limiting ... OK")


def test_zolix_bounded_prestop_poll():
    fake = ZolixFakeSerial(
        rules=[(0x04, REG_MOTION, lambda f: [(0.06, mb_read_reply(1, [1]))])],
        latency=0.06,
    )
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True
    time.sleep(0.25)  # let the REEMIT_GATE pass so the direction change
    # actually reaches the pre-stop path
    reads_before = len(frames_to(fake, 0x04, REG_MOTION))
    t0 = time.monotonic()
    assert d.continuous_start("x", -1, 1000) is True
    elapsed = time.monotonic() - t0
    reads = len(frames_to(fake, 0x04, REG_MOTION)) - reads_before
    assert reads >= 2, f"pre-stop poll never read motion state: {reads}"
    assert elapsed < 0.9, f"pre-stop poll unbounded: {elapsed:.2f}s"
    print(f"  zolix bounded pre-stop poll ({elapsed:.2f}s, {reads} reads) ... OK")


def test_zolix_stop_retry_no_silent_success():
    calls = {"n": 0}

    def stop_responder(frame):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # first reply lost (the v0.4.0 silent-success bug)
        return [(0.01, mb_echo(frame))]

    fake = ZolixFakeSerial(rules=[(0x10, REG_OPCODE, stop_responder)])
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True
    calls["n"] = 0  # reset: start used one opcode frame
    d.continuous_stop("x")
    assert calls["n"] == 2, f"stop retry missing: {calls['n']} attempts"
    assert d._moving["x"] is False
    print("  zolix stop retry on lost reply ... OK")


def test_zolix_stale_discard():
    stale = mb_read_reply(1, [0, 0])  # the observed 01 04 02 00 00 shape

    def responder(frame):
        return [(0.005, stale), (0.02, mb_echo(frame))]

    fake = ZolixFakeSerial(rules=[(0x10, REG_OPCODE, responder)])
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True, "stale reply broke the start"
    print("  zolix stale response discard ... OK")


def test_zolix_restart_after_stop_no_backoff():
    """The 90°-change regression: a stop followed by an immediate restart
    of the same axis must NOT be dropped (FAIL_BACKOFF arms only on
    actual failures, not on successful attempts)."""
    fake = ZolixFakeSerial()
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True
    d.continuous_stop("x")
    ops_before = len(frames_to(fake, 0x10, REG_OPCODE))
    assert d.continuous_start("x", 1, 1000) is True, \
        "restart after successful stop was dropped"
    assert len(frames_to(fake, 0x10, REG_OPCODE)) == ops_before + 1, \
        "restart did not send a new OP_CONTINUOUS"
    # 90° variant: +x → stop → −x
    d.continuous_stop("x")
    ops_before = len(frames_to(fake, 0x10, REG_OPCODE))
    assert d.continuous_start("x", -1, 1000) is True, \
        "direction change after stop was dropped"
    assert len(frames_to(fake, 0x10, REG_OPCODE)) == ops_before + 1
    assert d._last_direction["x"] == 0x4E
    print("  zolix restart after stop (no backoff drop) ... OK")


def test_zolix_fail_backoff():
    fake = ZolixFakeSerial(rules=[(0x10, REG_OPCODE, lambda f: None)])
    d = _zolix_driver(fake)
    d.connect()
    t0 = time.monotonic()
    assert d.continuous_start("x", 1, 1000) is False
    first_elapsed = time.monotonic() - t0
    assert first_elapsed < 1.5, f"start retry too slow: {first_elapsed:.2f}s"
    before = len(fake.writes)
    assert d.continuous_start("x", 1, 1000) is False, "FAIL_BACKOFF not applied"
    assert len(fake.writes) == before, "backoff window still did serial I/O"
    print("  zolix fail backoff ... OK")


def test_zolix_verify_gen_guard():
    """Stop → legitimate restart before the verify fires → verify must NOT
    escalate (the axis is still moving, but the movement is the NEW move)."""
    start_count = {"n": 0}

    def motion_responder(frame):
        # Motion reads as "moving" only after the SECOND OP_CONTINUOUS
        # (the restart) — so the verify sees motion=1 with a bumped gen.
        value = 1 if start_count["n"] >= 2 else 0
        return [(0.01, mb_read_reply(1, [value, 0, 0]))]

    def opcode_responder(frame):
        if opcode_of(frame) == 0x0066:
            start_count["n"] += 1
        return [(0.01, mb_echo(frame))]

    fake = ZolixFakeSerial(rules=[
        (0x04, REG_MOTION, motion_responder),
        (0x10, REG_OPCODE, opcode_responder),
    ])
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True
    d.continuous_stop("x")
    # The restart succeeds naturally now — FAIL_BACKOFF is armed only by
    # actual failures (a successful start must not arm the window; this
    # was the v0.4.1 "axis ends stopped on 90° change" bug).
    assert d.continuous_start("x", 1, 1000) is True  # legit restart, gen++
    time.sleep(1.2)  # let the verify fire and finish
    stops = frames_to(fake, 0x10, REG_OPCODE)
    stop_frames = [f for f in stops if opcode_of(f) == 0x0068]
    # exactly one stop (the initial one); no escalation
    assert len(stop_frames) == 1, f"false escalation: {len(stop_frames)} stop frames"
    assert d._moving["x"] is True, "legit restart must not be killed"
    print("  zolix verify generation guard ... OK")


def test_zolix_verify_escalation():
    escalation_seen = {"v": False}

    def stop_responder(frame):
        if opcode_of(frame) == 0x0068:
            escalation_seen["v"] = True
        return [(0.01, mb_echo(frame))]

    def motion_responder(frame):
        value = 0 if escalation_seen["v"] else 1
        return [(0.01, mb_read_reply(1, [value, 0, 0]))]

    fake = ZolixFakeSerial(rules=[
        (0x10, REG_OPCODE, stop_responder),
        (0x04, REG_MOTION, motion_responder),
    ])
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True
    d.continuous_stop("x")
    assert wait_until(lambda: escalation_seen["v"], 3.0), "verify never escalated"
    assert wait_until(lambda: d._moving["x"] is False, 3.0)
    stops = [f for f in frames_to(fake, 0x10, REG_OPCODE) if opcode_of(f) == 0x0068]
    assert len(stops) == 2, f"expected stop + escalation, got {len(stops)}"
    print("  zolix verify escalation ... OK")


def test_zolix_verify_reconcile():
    """Stop completely lost (both attempts) but hardware stopped → verify
    reconciles _moving without escalating."""
    def stop_responder(frame):
        if opcode_of(frame) == 0x0068:
            return None
        return [(0.01, mb_echo(frame))]

    fake = ZolixFakeSerial(rules=[
        (0x10, REG_OPCODE, stop_responder),
        (0x04, REG_MOTION, lambda f: [(0.01, mb_read_reply(1, [0, 0, 0]))]),
    ])
    d = _zolix_driver(fake)
    d.connect()
    assert d.continuous_start("x", 1, 1000) is True
    d.continuous_stop("x")          # lost reply → _moving stays True
    assert d._moving["x"] is True
    assert wait_until(lambda: d._moving["x"] is False, 3.0), "verify did not reconcile"
    stops = [f for f in frames_to(fake, 0x10, REG_OPCODE) if opcode_of(f) == 0x0068]
    assert len(stops) == 2, f"unexpected escalation: {len(stops)} stop frames"
    print("  zolix verify software-state reconciliation ... OK")


def test_zolix_stop_all_modes():
    # decel → per-axis frames, no AXIS_ALL
    fake = ZolixFakeSerial()
    d = _zolix_driver(fake, stop_mode="decel")
    d.connect()
    d.stop_all()
    stops = [f for f in frames_to(fake, 0x10, REG_OPCODE) if opcode_of(f) == 0x0067]
    assert len(stops) == 3, f"decel stop_all: {len(stops)} frames"
    assert sorted(param_of(f, 0) for f in stops) == [0x31, 0x32, 0x33], \
        [hex(param_of(f, 0)) for f in stops]
    assert not any(param_of(f, 0) == 0x30 for f in stops), "AXIS_ALL used in decel mode"

    # immediate → single AXIS_ALL frame
    fake2 = ZolixFakeSerial()
    d2 = _zolix_driver(fake2, stop_mode="immediate")
    d2.connect()
    d2.stop_all()
    stops2 = [f for f in frames_to(fake2, 0x10, REG_OPCODE) if opcode_of(f) == 0x0068]
    assert len(stops2) == 1 and param_of(stops2[0], 0) == 0x30
    print("  zolix stop_all decel/immediate modes ... OK")


def test_zolix_single_step_gen():
    fake = ZolixFakeSerial()
    d = _zolix_driver(fake)
    d.connect()
    gen_before = d._motion_gen["x"]
    assert d.single_step("x", 1, 100) == 100
    assert d._motion_gen["x"] == gen_before + 1, "single_step did not bump gen"
    print("  zolix single_step generation bump ... OK")


def test_zolix_closed_port():
    fake = ZolixFakeSerial()
    d = _zolix_driver(fake)
    d.connect()
    fake.is_open = False
    try:
        d._send_frame(b"\x00\x04\x00\x00")
        raise AssertionError("expected ConnectionError on closed port")
    except ConnectionError:
        pass
    assert not d.is_connected
    print("  zolix closed-port guard ... OK")


# ---------------------------------------------------------------------------
# Resolver scenarios (pure logic — no serial)
# ---------------------------------------------------------------------------


def test_resolver_stick_8dir_reemission():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    g.right_x = 1.0  # E → zolix X+
    cmds = r.resolve({}, g, now=now)
    starts = [c for c in cmds if c.stage_id == "zolix" and c.mode == "continuous_start"]
    assert len(starts) == 1 and starts[0].axis == "x" and starts[0].direction == 1, cmds
    # same direction next frame → re-emitted (the v0.4.1 drop-recovery fix)
    cmds = r.resolve({}, g, now=now + 0.02)
    starts = [c for c in cmds if c.stage_id == "zolix" and c.mode == "continuous_start"]
    assert len(starts) == 1, "8-dir did not re-emit after commit"
    # center → exactly one stop, then nothing
    g.right_x = 0.0
    cmds = r.resolve({}, g, now=now + 0.04)
    stops = [c for c in cmds if c.stage_id == "zolix" and c.mode == "continuous_stop"]
    assert len(stops) == 1, cmds
    cmds = r.resolve({}, g, now=now + 0.06)
    stops = [c for c in cmds if c.stage_id == "zolix" and c.mode == "continuous_stop"]
    assert not stops
    print("  resolver 8-dir re-emission ... OK")


def test_resolver_face_buttons_suppressed_by_keyboard():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    g.button_y = True
    key_state = {"q": now - 0.5}  # Q held ≥ threshold → claims zolix:r
    cmds = r.resolve(key_state, g, now=now)
    kbd = [c for c in cmds if c.stage_id == "zolix" and c.axis == "r" and c.source == "keyboard"]
    assert kbd, "keyboard Q hold not emitted"
    btn = [c for c in cmds if c.stage_id == "zolix" and c.axis == "r" and c.source == "gamepad_button"]
    assert not btn, "face button not suppressed by keyboard claim"
    # release Y → no single-step either (press time was suppressed)
    g.button_y = False
    cmds = r.resolve(key_state, g, now=now + 0.02)
    steps = [c for c in cmds if c.stage_id == "zolix" and c.mode == "single_step"]
    assert not steps, "suppressed face button still fired a single-step"
    print("  resolver face-button keyboard suppression ... OK")


def test_resolver_focus_keys():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    cmds = r.resolve({"equal": now}, g, now=now)
    fc = [c for c in cmds if c.stage_id == "focus"]
    assert len(fc) == 1 and fc[0].mode == "continuous_start" \
        and fc[0].direction == 1 and fc[0].source == "keyboard", cmds
    # slow = max(10, 50, 2000//4) = 500 with default config
    assert fc[0].speed == 500, fc[0].speed
    # Shift held → fast = max speed
    cmds = r.resolve({"equal": now, "Shift_L": now}, g, now=now + 0.02)
    fc = [c for c in cmds if c.stage_id == "focus" and c.mode == "continuous_start"]
    assert fc and fc[0].speed == 2000, fc
    # release → exactly one stop; no focus single_step
    cmds = r.resolve({}, g, now=now + 0.04)
    stops = [c for c in cmds if c.stage_id == "focus" and c.mode == "continuous_stop"]
    assert len(stops) == 1, cmds
    sp = r.resolve_short_presses({}, {"equal": now}, now + 0.04)
    assert not [c for c in sp if c.stage_id == "focus"]
    print("  resolver focus keys ... OK")


def test_resolver_ui_branch_priority_and_shift():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    ui = {"zolix:x": (now - 0.5, 1)}
    cmds = r.resolve({}, g, now=now, ui_state=ui)
    ui_cmds = [c for c in cmds if c.stage_id == "zolix" and c.axis == "x" and c.source == "ui_button"]
    assert len(ui_cmds) == 1 and ui_cmds[0].mode == "continuous_start"
    assert ui_cmds[0].speed == r._slow_speed["zolix"]
    # keyboard takes priority over UI
    cmds = r.resolve({"d": now - 0.5}, g, now=now + 0.02, ui_state=ui)
    assert not [c for c in cmds if c.source == "ui_button"]
    assert [c for c in cmds if c.stage_id == "zolix" and c.axis == "x" and c.source == "keyboard"]
    # Shift mid-hold → fast speed on the UI emission
    cmds = r.resolve({"Shift_L": now}, g, now=now + 0.04, ui_state=ui)
    ui_cmds = [c for c in cmds if c.source == "ui_button"]
    assert ui_cmds and ui_cmds[0].speed == r._fast_speed["zolix"]
    # UI release → one stop
    cmds = r.resolve({}, g, now=now + 0.06, ui_state={"zolix:x": (0.0, 0)})
    stops = [c for c in cmds if c.stage_id == "zolix" and c.axis == "x" and c.mode == "continuous_stop"]
    assert len(stops) == 1 and stops[0].source == "ui_button"
    # UI release while keyboard holds the axis → NO stop
    r.resolve({"d": now - 0.5}, g, now=now + 0.08, ui_state=ui)
    cmds = r.resolve({"d": now - 0.5}, g, now=now + 0.10, ui_state={"zolix:x": (0.0, 0)})
    stops = [c for c in cmds if c.mode == "continuous_stop" and c.stage_id == "zolix" and c.axis == "x"]
    assert not stops, "UI release stopped an axis the keyboard still holds"
    print("  resolver UI branch priority + shift ... OK")


def test_resolver_short_press_guard():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    # axis claimed by a UI hold → keyboard tap must not single-step into it
    r.resolve({}, g, now=now, ui_state={"sigmakoki:y": (now - 0.5, 1)})
    sp = r.resolve_short_presses({}, {"Up": now - 0.02}, now + 0.03)
    assert not sp, "single-step fired into a claimed axis"
    # cooldown: two quick taps on an unclaimed axis → one single_step
    r2 = ActionResolver()
    now2 = time.perf_counter()
    r2.resolve({}, GamepadState(connected=True), now=now2)
    sp = r2.resolve_short_presses({}, {"Right": now2 - 0.02}, now2 + 0.01)
    assert len(sp) == 1, sp
    sp = r2.resolve_short_presses({}, {"Right": now2 - 0.01}, now2 + 0.02)
    assert not sp, "single-step cooldown missing"
    print("  resolver short-press guards ... OK")


def test_resolver_dpad_switch_stops():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    g.dpad_right = True
    r.resolve({}, g, now=now)
    r.resolve({}, g, now=now + 0.35)  # long-press threshold → continuous claim
    assert any(k.startswith("dpad:") for k in r._continuous_keys)
    stops = r.pop_dpad_stops()
    assert len(stops) == 1 and stops[0].stage_id == "sigmakoki" and stops[0].axis == "x", stops
    assert not any(k.startswith("dpad:") for k in r._continuous_keys)
    assert not any(k.startswith("dpad:") for k in r._gamepad_press_times)
    print("  resolver dpad switch stop emission ... OK")


def test_resolver_cancel_latch():
    from input_system.action_resolver import ActionResolver
    from input_system.gamepad_handler import GamepadState

    r = ActionResolver()
    g = GamepadState(connected=True)
    now = time.perf_counter()
    g.right_x = 1.0
    r.resolve({}, g, now=now)          # commit E (zolix x+)
    r.cancel_all_continuous()          # ESC latch
    cmds = r.resolve({}, g, now=now + 0.02)
    assert not [c for c in cmds if c.stage_id == "zolix" and c.mode == "continuous_start"], \
        "latched stick re-emitted after STOP ALL"
    g.right_x = 0.0                    # re-center → latch clears
    r.resolve({}, g, now=now + 0.04)
    g.right_x = 1.0                    # re-engage works again
    cmds = r.resolve({}, g, now=now + 0.06)
    assert [c for c in cmds if c.stage_id == "zolix" and c.mode == "continuous_start"]
    print("  resolver ESC latch ... OK")


# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_focus_newline_and_connect,
    test_focus_event_filter,
    test_focus_connect_fails_loudly,
    test_zolix_rate_limit,
    test_zolix_bounded_prestop_poll,
    test_zolix_stop_retry_no_silent_success,
    test_zolix_stale_discard,
    test_zolix_fail_backoff,
    test_zolix_restart_after_stop_no_backoff,
    test_zolix_verify_gen_guard,
    test_zolix_verify_escalation,
    test_zolix_verify_reconcile,
    test_zolix_stop_all_modes,
    test_zolix_single_step_gen,
    test_zolix_closed_port,
    test_resolver_stick_8dir_reemission,
    test_resolver_face_buttons_suppressed_by_keyboard,
    test_resolver_focus_keys,
    test_resolver_ui_branch_priority_and_shift,
    test_resolver_short_press_guard,
    test_resolver_dpad_switch_stops,
    test_resolver_cancel_latch,
]


def main() -> int:
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 — test harness
            failed += 1
            logging.exception("%s FAILED", test.__name__)
    if failed:
        print(f"\n{failed}/{len(ALL_TESTS)} tests FAILED")
        return 1
    print(f"\nAll {len(ALL_TESTS)} fake-serial tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
