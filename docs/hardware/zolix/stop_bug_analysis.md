# Zolix XYR Stage — Intermittent Stop Failure: Candidate-Cause Analysis

**Status:** diagnosis **confirmed** from the v0.4.0 lab log (2026-09-03,
`debug.log` captured with verbose logging enabled); fixes applied in **v0.4.1**
(see "Applied fixes" below).

**Symptom:** the Zolix XYR stage occasionally fails to stop after a
`continuous_stop` / `stop_all` is issued (stick/d-pad/face-button release,
Escape key, STOP ALL button, window close). The stage keeps moving while the
GUI shows it as stopped. Related symptom: **direction changes sometimes do
not take effect**, and the stage responds much more slowly than v0.3.0.

**How evidence is captured (v0.4.0+):**

1. Settings → Zolix XYR → enable **"Verbose logging (console + debug.log)"**.
2. Reproduce the stop failure repeatedly on the lab PC.
3. Copy `%APPDATA%\TransferStageControl\debug.log` **before restarting the
   app** — the log rotates (keeps the previous 2 sessions).

---

## Confirmed root causes (from the v0.4.0 lab log)

1. **Reply latency ≈ 57–73 ms vs ~50 ms read timeout.** Every MODBUS
   transaction takes the ZC300 ~60 ms. With the configured `read(256)` timeout
   at/under that, most reads returned EMPTY and the next transaction's
   `reset_input_buffer()` **discarded the late reply**. Log: 80
   `EMPTY/SHORT response — treated as SUCCESS (suspect)` lines (44× on 0x0066
   continuous opcode, 35× on 0x0068 stop opcode, 1× on 0x0065). Stops and
   moves "succeeded" without the controller ever acknowledging them.
2. **Stale-response attribution.** 8 `response validation FAIL` lines — a
   stale function-0x04 read reply (e.g. `01 04 02 00 00 b9 30`) was consumed
   as the reply to a 0x10 opcode write, because nothing validated which
   frame the response belonged to.
3. **Per-frame re-emission → serial backlog.** The 60 Hz input loop re-emits
   `continuous_start` every frame while an input is held; the driver
   performed full serial transactions every frame (no rate limit). With
   ~60 ms per transaction the controller fell behind, replies arrived late,
   and stops/restarts were lost in the noise. This is also the mechanism
   behind the direction-change failures (a start fired after a stop was
   rejected/lost).
4. **Pre-stop poll stall (latency regression vs v0.3.0).** The working tree
   pre-stopped on direction change *or* any stop <100 ms ago and polled the
   (correct) motion-state register 15 × (20 ms + ~60 ms) ≈ **1.2 s per frame
   on the input thread** whenever the axis was genuinely still moving.
   (v0.3.0 read an out-of-range register that returned 0 immediately and
   broke the loop — accidentally fast, wrongly implemented.)
5. **Verification gaps in v0.4.0.** The post-stop verify was read-only
   (never escalated), and produced false "still moving" WARNINGs when the
   still-held stick legitimately restarted the axis between the stop and the
   verify. Per-frame hex logging to the always-DEBUG file handler added
   input-thread I/O.

Notably the log contains **zero EXC 0x06 lines and zero "stop command
failed" lines** — stops failed via lost/unattributed replies and silent
false-successes, not via explicit rejections.

---

## Candidate causes (original v0.4.0 analysis — for reference)

### 1. Silent false-success on empty/short MODBUS responses

`_write_opcode_block` (zolix.py) checks for a MODBUS exception only when
`response and len(response) >= 3`. If the controller is busy and never
replies (read timeout), or only 1–2 bytes arrive, the stop is treated as
**success** and `_stop_axis_locked` / `stop_all` clear the software
`_moving` flags.

**Evidence in log:** lines like

```
Zolix opcode 0x0068: EMPTY/SHORT response (0 bytes) — treated as SUCCESS (suspect)
```

immediately before the observed failed-stop symptom, plus
`Zolix RX: EMPTY response (0 bytes) to …`.

### 2. No stop retry anywhere; upper layers swallow exceptions

- `_stop_axis_locked` / `stop_all` send the stop **once**; on exception they
  log and return — nothing re-sends.
- `InstrumentManager.execute()` catches all exceptions per command and drops
  the rest; the resolver emits `continuous_stop` exactly once per release
  (its `_continuous_keys`/`_continuous_stick` state is popped), so there is
  no second chance.
- `stop_all_stages`, `set_enabled`, `disconnect_all` all wrap driver calls
  in `try/except: pass`.

**Evidence in log:** `Zolix … stop command failed` WARNINGs (with response
hex) followed by `POST-STOP VERIFY … still moving` — i.e. the driver knew the
stop failed but nothing retried it.

### 3. Decel-stop + AXIS_ALL combination (config-dependent)

`stop_all` sends `(self._stop_opcode, AXIS_ALL)` where `AXIS_ALL = 0x30`.
The manual documents `0x30` as a valid axis selector in the general opcode
table, but the **0x0067 + 0x30 combination is untested on the lab unit** —
v0.4.1 avoids it by issuing per-axis decel stops (0x31/0x32/0x33) and keeps
the all-axes form for immediate stop (0x0068 + 0x30) only.

### 4. Stale responses masquerading as the current command's reply

`_send_frame` calls `reset_input_buffer()` before every frame, discarding
any pending bytes. If the controller is still processing the previous
command, its (possibly stale) response is read as the new command's reply —
or the real reply is flushed away. The old code never validated that a
response matched the just-sent frame.

**Evidence in log:** `Zolix response validation FAIL: expected fn 0x10 …`
lines (the new check compares slave address + function byte against the
sent frame).

### 5. No post-stop hardware verification (no watchdog)

After a stop, the Zolix hardware motion registers (30012–30014) were not
read for ≥1 s (`poll_stage_status` idle-gate uses `last_command_time`, which
the stop frame itself bumps), and even when a later poll showed the axis
still moving, nothing re-issued a stop.

**Evidence in log:** the new `POST-STOP VERIFY` lines — a
`… still moving (motion_state=1)` WARNING after an "OK" stop is the direct
detector for this failure mode.

### 6. Manual ambiguity: the stop itself may be rejected with 0x06

`register-map.md`: "0x06：指令报警（若当前轴正在运动，禁止发送新的运动指令）"
— *if the axis is moving, sending new motion commands is forbidden* — with
no documented exemption for stop commands. A stop sent while the controller
is busy processing another command (or while the axis is mid-move) may
itself get exception 0x06, and the old code's stop path had no busy
handling (busy handling existed only in `continuous_start`).

**Evidence in log:** `Zolix EXC 0x06 on opcode 0x0067/0x0068 — axis busy /
new-motion-forbidden` lines on stop opcodes.

---

## Existing mitigations already in the working tree (context, not fixes)

- `_stop_axis_locked` only clears `_moving` on success — a failed stop
  leaves the software state saying "moving".
- `continuous_start` pre-stops on direction reversal / recent stop and polls
  motion state up to ~300 ms before restarting (the only place the driver
  waits for a stop to take effect).
- `_last_stop_time` re-stop guard for diagonal→cardinal transitions.

## Non-goals (of the original v0.4.0 instrumentation release)

- No fix, no retry logic, no behavior change in v0.4.0.
- The v0.4.0 post-stop verification threads were **read-only** (function 0x04
  reads of registers 30012–30014); they could never move the stage.

## Applied fixes (v0.4.1)

- **Framed reads with stale discard** — `_send_frame` reads byte-by-byte until
  a per-transaction deadline (0.25 s), assembles CRC-valid frames, discards
  complete replies to earlier commands (wrong function byte) and keeps
  waiting; blind `reset_input_buffer()` replaced by a log-only drain.
- **No silent success** — an opcode write with no valid reply now raises
  `ZolixNoResponse` after retries (50 ms apart); 0x06 busy replies are
  retried, other exception codes raise immediately.
- **Opcode rate limiting** — `continuous_start` returns instantly (zero
  serial I/O) when the same direction+speed opcode is already active, gates
  real re-emissions at 0.20 s, and backs off 0.30 s after failed starts.
- **Bounded pre-stop poll** — single motion read to decide whether a pre-stop
  is needed; bounded ~0.30 s wall-clock wait (was up to ~1.2 s per frame).
- **Verified stops with escalation** — after every stop, a delayed check
  reads the motion registers: legitimate restarts (motion-generation counter
  changed) are ignored, lost-but-effective stops reconcile the software
  state, and genuinely still-moving axes get an immediate stop (0x0068) plus
  a follow-up recheck. The counter is also bumped by `single_step`.
- **Decel stop_all per axis** — 0x0067 with 0x31/0x32/0x33 (the all-axes
  combination is untested on the lab unit); immediate mode keeps 0x0068+0x30.
- **Serial timeout floor** — driver clamps the read timeout to ≥0.15 s
  (default config now 0.2 s) so replies can never outrun the read window.
- **Logging** — full hex dumps only when verbose logging is on; compact
  non-hex DEBUG lines otherwise keep lab evidence without input-thread cost.
