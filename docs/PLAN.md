# Transfer Stage Control Application — Implementation Plan

## Context

Building a unified Windows desktop application to control a 2D material transfer system with 3 instruments: a **SigmaKoki XYZ stage** (Arduino-based, serial UART), a **Zolix XYR stage** (ZC300 controller, MODBUS-RTU over RS-485), and a **Yudian AI-828 temperature controller** (MODBUS-RTU over RS-485). Control via keyboard, Xbox-compatible gamepad, and on-screen buttons. Built to a single `.exe` via PyInstaller.

**Existing assets to reuse/adapt:**
- `temp_control_doc/yudian_ai828.py` — complete MODBUS-RTU driver (CRC-16, frame builder, register map verified on AI-828 V9.3). **Caveat:** auto-scan device discovery is unreliable — simplify to direct-connect only.
- `temp_control_doc/app.py` — proven tkinter patterns: settings persistence, background polling, queue-based UI updates, state machine. **Caveat:** roughly tested only, review carefully.
- `sigmakoki_XYZ_stage_doc/arduino_source_code.txt` — reference for pin definitions and protocol structure. Arduino firmware must be **rewritten** for non-blocking motion.
- `zolix_XYR_stage_doc/register-map.md` — **complete MODBUS register table** for the ZC300 controller (now available).

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| GUI | **tkinter** (ttk) | Stdlib, no extra DLLs, proven in existing app.py, easy PyInstaller |
| Gamepad | **XInput via ctypes** | Native Windows API, **zero dependencies**, Xbox 360/One/Series + compatibles, reliable |
| Serial | **pyserial** | Already used in existing code |
| MODBUS | Custom (based on yudian_ai828.py) | No extra dependency, CRC-16 already implemented |
| Packaging | **PyInstaller** | Single .exe, tkinter-aware |
| Python | **3.12** (conda env) | Modern, good PyInstaller support |

### Why XInput over pygame

XInput (`xinput1_4.dll`) is the native Windows API for Xbox controllers. Accessed via `ctypes` — zero pip dependencies, no SDL2 DLLs to bundle. Supports Xbox 360, Xbox One, Xbox Series, and most third-party "Xbox-compatible" controllers. Hot-plug detection via polling `XInputGetState` each tick — if the return code transitions between `ERROR_SUCCESS` and `ERROR_DEVICE_NOT_CONNECTED`, we detect connect/disconnect. Limited to 4 controllers (fine for this use case). The XInput state struct provides: left/right analog sticks (-32768 to 32767), left/right triggers (0-255), D-pad (bitmask), and all buttons (bitmask).

## Project Structure

```
transfer_stage_app/
├── main.py                                 # Entry point
├── app.py                                  # Orchestrator: wires everything, lifecycle
├── settings.json                           # Runtime settings & presets (auto-generated)
│
├── arduino_firmware/
│   └── transfer_stage_controller/
│       └── transfer_stage_controller.ino   # Redesigned non-blocking firmware
│
├── stage_control/
│   ├── __init__.py
│   ├── stage_state.py                      # StageState, StageCommand dataclasses
│   ├── instruments.py                      # InstrumentManager: owns all 3 drivers
│   └── hardware/
│       ├── __init__.py
│       ├── sigmakoki.py                    # UART text-protocol driver for Arduino
│       ├── zolix.py                        # ZC300 MODBUS driver (real register map)
│       └── yudian.py                       # Adapted from existing yudian_ai828.py
│
├── input_system/
│   ├── __init__.py
│   ├── input_mapping.py                    # Key/button → logical action config
│   ├── keyboard_handler.py                 # Key state via tkinter bind_all + shared dict
│   ├── gamepad_handler.py                  # XInput via ctypes + hot-plug detection
│   ├── action_resolver.py                  # Long-press, speed, disable → StageCommands
│   └── input_manager.py                    # ~60Hz loop: tick → resolve → dispatch
│
├── gui/
│   ├── __init__.py
│   ├── styles.py                           # Colors, fonts, ttk theme
│   ├── main_window.py                      # Top-level Tk, 3-column layout, key bindings
│   ├── status_panel.py                     # Device + gamepad connection indicators
│   ├── stage_panel.py                      # Position, speed, limits, buttons, enable toggle
│   ├── temperature_panel.py                # PV/SV/MV display, preset dropdown (from app.py)
│   ├── gamepad_indicator.py                # Gamepad connect/disconnect status
│   ├── axis_control_buttons.py             # On-screen directional button grid
│   ├── settings_dialog.py                  # Tabbed: COM/speed/invert for all devices
│   └── preset_dialog.py                    # Temperature preset editor (from app.py)
│
├── utils/
│   ├── __init__.py
│   ├── config.py                           # JSON settings load/save with defaults
│   ├── modbus_rtu.py                       # Shared CRC-16, MODBUS frame builder/parser
│   ├── serial_utils.py                     # Port enumeration helper
│   └── logging_config.py                   # File+console logging, exception hook
│
├── build_scripts/
│   ├── transfer_stage.spec                 # PyInstaller spec
│   └── build.bat                           # One-click build
│
├── environment.yml                         # conda env: python=3.12, pyserial, pyinstaller
└── README.md
```

### Physical Connections

| Device | Interface | COM Port | Notes |
|--------|-----------|----------|-------|
| SigmaKoki XYZ | Arduino USB (UART) | COM port (variable) | 9600 baud, 8N1, text protocol |
| Zolix XYR (ZC300) | USB cable (virtual COM) | COM port (variable) | MODBUS-RTU, **fixed 115200 baud** |
| Yudian AI-828 | USB-to-RS485 converter | COM port (variable) | MODBUS-RTU, typically 9600 baud |

All three devices appear as standard Windows COM ports via `pyserial`. No special USB drivers needed beyond the OS-provided CDC ACM / FTDI / CH340 drivers.

## Architecture

### Data Flow

```
┌─ MAIN THREAD ────┐    ┌─ INPUT LOOP THREAD (~60Hz) ──────────────────────┐
│ tkinter mainloop  │    │                                                  │
│                   │    │  KeyboardHandler ◄─ shared dict ◄─ bind_all()    │
│  bind_all(Key*) ──┼────┼─► (key states with durations)                    │
│                   │    │  GamepadHandler  ◄─ ctypes XInputGetState()      │
│  root.after() ◄───┼────┼─ queue.Queue (status updates)                    │
│  drain queues     │    │       │                                          │
│                   │    │       ▼                                          │
└───────────────────┘    │  ActionResolver.resolve()                        │
                          │       │ List[StageCommand]                      │
                          │       ▼                                         │
┌─ TEMP POLLER ─────┐    │  InstrumentManager.execute_batch()               │
│ 500ms interval     │    │  ├─ check enabled_map → drop if disabled        │
│ Yudian.read_all()  │    │  ├─ SigmaKokiDriver.continuous_start()          │
│ → queue → GUI      │    │  └─ ZolixDriver.continuous_start()              │
└────────────────────┘    │                                                  │
                          │  Every 200ms: poll LIMITS?/STATUS? → queue → GUI │
                          └──────────────────────────────────────────────────┘
```

### Data Types (`stage_control/stage_state.py`)

```python
@dataclass
class StageCommand:
    stage_id: str      # "sigmakoki" | "zolix"
    axis: str          # "x" | "y" | "z" | "r"
    mode: str          # "single_step" | "continuous_start" | "continuous_stop"
    direction: int     # +1 or -1
    speed: float       # steps/sec (computed by resolver)
    source: str        # "keyboard" | "gamepad_stick" | "gamepad_dpad" | "ui_button"

@dataclass
class StageState:
    stage_id: str
    enabled: bool
    position: dict     # {"x": int, "y": int, "z": int, "r": float}
    current_speed: dict
    limits: dict       # {"x+": bool, "x-": bool, ...}
    moving: dict
    connected: bool
```

### Hardware Drivers

#### SigmaKokiDriver (`stage_control/hardware/sigmakoki.py`)

Serial UART at 9600 baud, 8N1. Colon-delimited text commands to the Arduino. Thread-safe via `threading.Lock`.

**Protocol (new design for the rewritten Arduino firmware):**
```
MV:X:<dir>:<spd>   Continuous move, idempotent (sending again updates dir/speed without stopping)
STOP:X / STOP:ALL  Immediate stop
STEP:X:<dir>:<n>   Single-step N pulses (blocking on Arduino, brief)
SPD:X:<level>      Speed level 0-5 (50 Hz to 1 kHz)
LIMITS?            Returns L:X+:0,X-:1,... (0=normal, 1=triggered)
STATUS?            Returns S:X:1234,Y:-567,Z:42,XSPD:2,...
PING               Returns PONG
HOME               Reset position counters to 0
```
**Unsolicited events:** `EV:LIM:X+` (limit hit during move), `BOOT` (on startup). Driver reader thread filters events from command responses.

#### ZolixDriver (`stage_control/hardware/zolix.py`)

MODBUS-RTU over RS-485. **Fixed baud 115200, 8N1.** Uses `utils/modbus_rtu.py` for frame building.

**Register map (from `register-map.md`):**
- **Input registers (0x04):** 30012-30014 (motion state per axis: 0=stopped, 1=moving), 30015 (limit/home/alarm bitmask), 30016-30021 (float position per axis, 2 regs each)
- **Holding registers (0x03/0x06/0x10):** 30050 (opcode), 30051 (axis: 0x31=X, 0x32=Y, 0x33=Z), 30052 (direction: 0x50=P, 0x4E=N), 30053 (reserved)
- **Opcodes:** 0x0064=absolute move, 0x0065=fixed-length move (single step), 0x0066=continuous move, 0x0067=decel stop, 0x0068=immediate stop, 0x0069=home, 0x006D=save params
- **Speed registers:** 30123-30134 (initial speed, constant speed — float pairs, write via 0x10)
- **Enable registers:** 30066-30068 (per-axis: 0x01=enabled, 0x00=disabled)
- **ZC300 "Z" axis = R (rotation) axis in XYR stage logic**

**Key constraint:** CANNOT send a motion command to an axis that is already moving (returns MODBUS exception 0x06). This means: speed changes require stop → write speed → restart. The driver must track `moving` state per axis and handle this.

**Control flow for continuous move on Zolix:**
1. Write constant speed to 30129-30134 (float, 2 registers each) via 0x10
2. Write axis (0x31/0x32/0x33) to 30051 via 0x06
3. Write direction (0x50/0x4E) to 30052 via 0x06
4. Write opcode 0x0066 to 30050 via 0x06 → motion starts
5. To stop: write axis to 30051, write 0x0067 (decel) or 0x0068 (immediate) to 30050
6. Speed change: stop → write new speed → restart continuous (with debounce to avoid flicker)

**Control flow for single step on Zolix:**
1. Write distance to 30114-30119 (fixed-length distance, float) via 0x10
2. Write axis/direction/opcode 0x0065 to 30050-30052
3. Poll 30012-30014 until axis returns to stopped state

#### YudianDriver (`stage_control/hardware/yudian.py`)

Adapted from `temp_control_doc/yudian_ai828.py`. Key changes:
- **Remove `scan_devices()`** — full port/baudrate/slave scan is unreliable and slow. Replace with simple direct-connect using saved settings.
- **Keep the proven core:** CRC-16, frame builder, `connect()` handshake (read dPt, ensure Run mode), `read_pv/read_sv/read_mv/read_all/set_sv`, MODBUS exception handling.
- **Auto-connect flow:** Try saved port+slave+baud only. If that fails, user manually selects port in Settings dialog. No full scanning.

### Arduino Firmware Redesign

**Problem:** Existing firmware uses blocking `delay()` inside `moveAxis()` — only one axis moves at a time, serial is unresponsive during motion, no STOP possible mid-move.

**Solution:** Non-blocking `micros()`-based pulse generation for all 3 axes simultaneously.

```
Each axis tracked in RAM:
  struct { pins, moving(bool), direction(int8), speed_level(0-5),
           pulse_interval_us, last_toggle_us, pulse_state(bool),
           position(long), lim_pos_triggered, lim_neg_triggered, inverted }

loop():
  parseSerial()       // read bytes, build line, dispatch to handlers
  updateAllMotors()   // for each axis where moving==true:
                      //   if micros()-last_toggle >= pulse_interval/2:
                      //     toggle PUL pin, update position
                      //     at rising edge: check limits → stop+EV:LIM if triggered
```

**Key behaviors:**
- Limit hit during continuous move → stop that axis immediately, send `EV:LIM:X+` to PC
- `MV` command is idempotent — sending again just updates dir/speed without restarting
- Watchdog timer resets Arduino if loop hangs >2s
- `BOOT` message sent once on startup (PC uses this as "ready" signal)
- Pulse dead time: 10µs minimum between direction change and first pulse

### XInput Gamepad Handler (`input_system/gamepad_handler.py`)

```python
# XInput state struct (via ctypes)
class XINPUT_STATE(Structure):
    _fields_ = [
        ("dwPacketNumber", DWORD),
        ("Gamepad", XINPUT_GAMEPAD),  # wButtons, left/right trigger, thumb LX/LY/RX/RY
    ]

# Key XInput constants
XINPUT_GAMEPAD_DPAD_UP    = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN  = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT  = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008
XINPUT_GAMEPAD_START       = 0x0010
XINPUT_GAMEPAD_BACK        = 0x0020
XINPUT_GAMEPAD_A           = 0x1000
XINPUT_GAMEPAD_B           = 0x2000
XINPUT_GAMEPAD_X           = 0x4000
XINPUT_GAMEPAD_Y           = 0x8000
```

**Hot-plug detection:** Call `XInputGetState(0, ...)` each tick (60Hz). If return code transitions between `ERROR_SUCCESS` and `ERROR_DEVICE_NOT_CONNECTED`, update connection state and notify GUI via queue. First controller only (index 0).

**Analog stick mapping:** Values range -32768 to 32767. Apply deadzone (configurable, default ~10%). Map remaining range to speed linearly: `speed = magnitude * max_speed`. Left stick → SigmaKoki XY, Right stick → Zolix XY. Invert options per axis.

**Trigger mapping:** Left trigger → fast speed for SigmaKoki (left stick), Right trigger → fast speed for Zolix (right stick). Values 0-255, threshold for digital-style use.

**D-pad:** Single-step on short press, continuous slow on long press. Back button toggles which stage D-pad controls. Start button toggles software enable/disable for the D-pad-selected stage.

**Buttons:** X/Y → rotation (R-/R+), A/B → Z axis (Z+/Z-). Short press = single step, long press = continuous.

### Input System

- **KeyboardHandler**: `bind_all("<KeyPress>")` / `"<KeyRelease>"` on Main thread writes to `threading.Lock`-protected shared dict. Input loop reads each tick computed press durations.
  - WASD → SigmaKoki XY, Arrows → Zolix XY, Q/E → rotation, U/J → Z axis
  - Shift = fast speed modifier (keyboard only). Long press (>300ms) = continuous.
- **ActionResolver**: Pure function — raw input → `list[StageCommand]`. Handles: long-press detection, speed mode selection, software disable filtering, keyboard-over-gamepad priority, D-pad-over-stick priority.
- **InputManager**: ~60Hz daemon thread loop. Each tick: pump keyboard state, call XInput, resolve commands, dispatch to InstrumentManager, poll device status every Nth tick (200ms).

### Threading Model

| Thread | Frequency | Purpose |
|--------|-----------|---------|
| Main | Event-driven | tkinter mainloop, GUI rendering |
| Input Loop | ~60 Hz | Keyboard + XInput reading, resolve, dispatch, stage status polling |
| Temperature Poller | 2 Hz | MODBUS reads from Yudian |
| Connect Workers | One-shot | Non-blocking device connection (1 per device) |

All cross-thread communication via `queue.Queue` + `root.after()` drain loops. Per-driver `threading.Lock` on serial ports. Lock hierarchy: Keyboard → Gamepad → Command map → driver serial locks.

### GUI Layout

Three-column design (ttk style, Microsoft YaHei font):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ● SigmaKoki: CONNECTED  │ ● Zolix: CONNECTED  │ 🎮 Controller: OK   │
│ ● Yudian: 150.3°C       │                      │        [⚙ Settings] │
├──────────────────────────┬──────────────────────┬────────────────────┤
│  SIGMAKOKI XYZ  [✓] En.  │  ZOLIX XYR  [✓] En.  │  TEMPERATURE       │
│  Position: X:+1234       │  Position: X:+3210   │  ┌──────────┐      │
│            Y:-567        │           Y:-1098    │  │  150.3   │      │
│            Z:+42         │           R:45.0°    │  │    °C    │      │
│  Speed: 200 step/s       │  Speed: 200 step/s   │  └──────────┘      │
│  Limits: ○○○○○○ (6 LEDs) │  Limits: ○○○○○○     │  Target: [200.0]°C │
│                          │                      │  [Apply]           │
│  On-screen buttons:      │  On-screen buttons:  │  Preset: [▼]       │
│       [Y-]               │       [Y-]           │  Output: 45.0%     │
│  [X-] [X+] [Z+]         │  [X-] [X+] [R-]     │  [Edit Presets]    │
│       [Y+] [Z-]          │       [Y+] [R+]      │                    │
│  [HOME] [STOP ALL]       │  [HOME] [STOP ALL]   │                    │
└──────────────────────────┴──────────────────────┴────────────────────┘
```

- Limit LEDs: green circle = normal, red circle = triggered
- On-screen buttons: `ttk.Button` with `<ButtonPress-1>` / `<ButtonRelease-1>` for press/release + long-press via `after()` timers, slow speed only
- Escape key = STOP ALL globally
- Stage panels are identical components, instantiated twice with different config

### Settings (`settings.json`)

```json
{
  "_version": 1,
  "sigmakoki": {
    "port": "COM3", "baudrate": 9600, "timeout_s": 0.3,
    "slow_speed_hz": 200, "fast_speed_hz": 500,
    "invert_x": false, "invert_y": false, "invert_z": false,
    "single_step_amount": 10
  },
  "zolix": {
    "port": "COM4", "baudrate": 115200, "slave_address": 1,
    "timeout_s": 0.3,
    "slow_speed_pps": 1000, "fast_speed_pps": 5000,
    "invert_x": false, "invert_y": false, "invert_r": false,
    "single_step_amount": 100,
    "stop_mode": "decel"
  },
  "yudian": {
    "port": "COM5", "baudrate": 9600, "slave_address": 1,
    "timeout_s": 0.5, "poll_interval_ms": 500,
    "safety_temp_lo_c": -100.0, "safety_temp_hi_c": 400.0,
    "presets": [
      {"name": "Room Temp", "temp_c": 25.0},
      {"name": "Body Temp", "temp_c": 37.0},
      {"name": "Boiling", "temp_c": 100.0}
    ]
  },
  "gamepad": {
    "deadzone": 0.10,
    "invert_left_x": false, "invert_left_y": false,
    "invert_right_x": false, "invert_right_y": false,
    "trigger_threshold": 0.5
  },
  "input": {
    "long_press_threshold_ms": 300,
    "loop_rate_hz": 60,
    "status_poll_rate_hz": 5
  },
  "ui": {
    "window_width": 1060, "window_height": 680,
    "font_family": "Microsoft YaHei"
  }
}
```

### Build & Packaging

- **Conda env**: `python=3.12`, `pyserial`, `pyinstaller`. **No pygame** — XInput is via stdlib `ctypes`.
- **PyInstaller**: `--console=False`, `--one-file` (or `--onedir` for faster startup). Excludes unused libs (numpy, matplotlib, etc.). Hidden imports: `serial.tools.list_ports_windows`.
- **Estimated .exe**: ~15-20 MB (no SDL2 DLLs, much smaller than before).
- `build_scripts/build.bat`: `conda activate transfer_stage && pyinstaller --clean --noconfirm build_scripts\transfer_stage.spec`

## Implementation Order

### Phase 1: Foundation (utils, datatypes, conda env)
1. `environment.yml` — conda env with python=3.12, pyserial, pyinstaller
2. `utils/logging_config.py` — centralized logging, uncaught exception hook
3. `utils/config.py` — JSON settings load/save with defaults
4. `utils/serial_utils.py` — COM port enumeration helper
5. `utils/modbus_rtu.py` — CRC-16, MODBUS frame builder/parser (extracted from yudian_ai828.py)
6. `stage_control/stage_state.py` — StageState, StageCommand dataclasses
7. Adapt `yudian_ai828.py` → `stage_control/hardware/yudian.py` (remove scan_devices, keep core)

### Phase 2: Arduino Firmware
8. `arduino_firmware/transfer_stage_controller/transfer_stage_controller.ino` — non-blocking firmware, test via Arduino Serial Monitor

### Phase 3: Stage Drivers
9. `stage_control/hardware/sigmakoki.py` — UART driver, test against real Arduino
10. `stage_control/hardware/zolix.py` — MODBUS driver with real register map, test against ZC300 or MODBUS simulator
11. `stage_control/instruments.py` — InstrumentManager: connect/disconnect/execute/status poll
12. `stage_control/limit_monitor.py` — limit state tracking from polled data

### Phase 4: Input System
13. `input_system/input_mapping.py` — key/button → action config
14. `input_system/keyboard_handler.py` — key state tracking via bind_all
15. `input_system/gamepad_handler.py` — XInput via ctypes, hot-plug detection
16. `input_system/action_resolver.py` — input → StageCommands (pure logic, testable standalone)
17. `input_system/input_manager.py` — 60Hz loop orchestrator

### Phase 5: GUI
18. `gui/styles.py` — colors, fonts, theme constants
19. `gui/status_panel.py` — horizontal device+gamepad connection indicators
20. `gui/gamepad_indicator.py` — gamepad connect/disconnect + name
21. `gui/axis_control_buttons.py` — directional button grid with press/release/long-press
22. `gui/stage_panel.py` — per-stage composite: position, speed, limits, buttons, enable toggle, HOME, STOP ALL
23. `gui/temperature_panel.py` — adapted from app.py (PV, SV entry, Apply, preset dropdown, output %)
24. `gui/settings_dialog.py` — tabbed notebook (SigmaKoki / Zolix / Yudian / Gamepad / Input)
25. `gui/preset_dialog.py` — from app.py PresetDialog
26. `gui/main_window.py` — top-level composition, key bindings, queue drain loops
27. `app.py` — orchestrator: creates MainWindow, starts managers, handles startup auto-connect, shutdown
28. `main.py` — `if __name__ == "__main__": App().run()`

### Phase 6: Integration & Packaging
29. Integration test with all hardware connected
30. Edge cases: USB disconnect/reconnect, gamepad hot-plug, rapid input, limit hits, settings change while connected, close during movement
31. PyInstaller spec + `build.bat`
32. Test .exe on clean Windows machine

## Key Design Decisions & Tradeoffs

1. **Arduino can update speed mid-move, Zolix cannot.** The Arduino firmware supports `MV` idempotency (re-send to change dir/speed without stopping). The Zolix requires stop→write_speed→restart for speed changes. The `ActionResolver` handles this asymmetry: for Zolix, it debounces speed changes (500ms minimum between stop-restart cycles) to prevent flicker.

2. **Yudian auto-detect removed.** The existing `scan_devices()` scans all ports × baudrates × slaves, which is slow and flaky. Instead: on startup, try the saved device only. If that fails, show "Disconnected" and user manually selects port in Settings.

3. **XInput over pygame.** Zero dependencies, native Windows API, smaller .exe. Tradeoff: XInput only supports Xbox-compatible controllers (which is the requirement). Hot-plug detection is poll-based rather than event-based (60Hz polling is more than responsive enough).

4. **tkinter over PyQt.** Stdlib, proven in existing code, smaller .exe. Tradeoff: less modern look. Mitigated by ttk themed widgets and consistent color scheme.

## Verification

1. **Arduino firmware** — test all commands via Serial Monitor (`PING`, `MV:X:1:2`, `STOP:X`, `LIMITS?`, `STATUS?`, `STEP:X:1:100`). Trigger limits during movement.
2. **Stage drivers** — test from throwaway scripts against real hardware. Verify continuous start/stop, single step, limit detection, disconnect recovery.
3. **Input system** — console-print StageCommands. Test long-press, Shift modifier, gamepad mapping, software disable.
4. **GUI** — visual check of all panels. Button press/release. Settings save/restore across restarts.
5. **End-to-end** — all 3 devices, keyboard + gamepad simultaneously, temperature setting, status updates.
6. **.exe** — test on clean Windows machine without Python.
