"""
Main Window
============

Top-level application window composing the status bar, stage panels,
temperature panel, and gamepad indicator.  Sets up keyboard bindings,
queue drain loops, and the application lifecycle (connect → poll → run).
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import ttk

from utils.config import load_settings, save_settings
from stage_control.instruments import InstrumentManager
from stage_control.hardware.yudian import (
    YudianCommunicationError,
    YudianConnectionError,
)
from stage_control.stage_state import StageCommand, StageState
from input_system.keyboard_handler import KeyboardHandler
from input_system.gamepad_handler import GamepadHandler
from input_system.input_manager import InputManager

from gui.styles import setup_styles, COLOR_BG, COLOR_BLUE, COLOR_WARN
from gui.status_panel import StatusPanel
from gui.stage_panel import StagePanel
from gui.temperature_panel import TemperaturePanel
from gui.gamepad_indicator import GamepadIndicator
from gui.settings_dialog import SettingsDialog

logger = logging.getLogger("transfer_stage.gui")


class MainWindow:
    """Top-level application window."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Transfer Stage Control System")
        self.root.configure(bg=COLOR_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.minsize(720, 420)

        # Window icon
        try:
            import os as _os, sys as _sys
            if getattr(_sys, 'frozen', False):
                _base = _sys._MEIPASS
            else:
                _base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            _icon = _os.path.join(_base, 'icon', 'icon.png')
            if _os.path.exists(_icon):
                _img = tk.PhotoImage(file=_icon)
                self.root.iconphoto(True, _img)
        except Exception:
            pass

        # Styles
        setup_styles()

        # Settings
        self._settings = load_settings()

        # Queues for cross-thread communication
        self._gui_queue = queue.Queue(maxsize=20)
        self._gamepad_status_queue = queue.Queue(maxsize=10)
        self._connect_status_queue = queue.Queue(maxsize=10)

        # Temp polling state
        self._temp_polling = False
        self._temp_stop_event = threading.Event()
        self._temp_latest = None  # written by poll thread, read by main thread
        self._temp_lock = threading.Lock()  # guards _temp_latest swap
        self._temp_poll_gen = 0   # generation counter — prevents duplicate display loops

        # Instrument manager
        self._instruments = InstrumentManager(
            sigmakoki_config=self._settings.get("sigmakoki", {}),
            zolix_config=self._settings.get("zolix", {}),
            yudian_config=self._settings.get("yudian", {}),
        )

        # Build UI
        self._build_ui()

        # Keyboard handler
        self._keyboard = KeyboardHandler(self.root)
        self._keyboard.bind_global("Escape", self._on_escape)

        # Gamepad handler
        gamepad_cfg = self._settings.get("gamepad", {})
        self._gamepad = GamepadHandler(
            deadzone=gamepad_cfg.get("deadzone", 0.10),
            invert={
                "left_x": gamepad_cfg.get("invert_left_x", False),
                "left_y": gamepad_cfg.get("invert_left_y", False),
                "right_x": gamepad_cfg.get("invert_right_x", False),
                "right_y": gamepad_cfg.get("invert_right_y", False),
            },
        )

        # Input manager
        input_cfg = self._settings.get("input", {})
        gamepad_cfg = self._settings.get("gamepad", {})
        self._input_manager = InputManager(
            instruments=self._instruments,
            keyboard=self._keyboard,
            gamepad=self._gamepad,
            gui_queue=self._gui_queue,
            gamepad_status_queue=self._gamepad_status_queue,
            loop_rate_hz=input_cfg.get("loop_rate_hz", 60),
            status_poll_rate_hz=input_cfg.get("status_poll_rate_hz", 10),
            trigger_threshold=gamepad_cfg.get("trigger_threshold", 0.5),
        )

        # Start input loop
        self._input_manager.start()

        # Start GUI drain loops
        self.root.after(30, self._drain_gui_queue)
        self.root.after(100, self._drain_gamepad_queue)
        self.root.after(200, self._drain_connect_queue)

        # Auto-connect
        self.root.after(500, self._auto_connect)

        # Window geometry
        ui_cfg = self._settings.get("ui", {})
        w = ui_cfg.get("window_width", 720)
        h = ui_cfg.get("window_height", 420)
        self.root.geometry(f"{w}x{h}")

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Build the three-column layout."""
        # Status bar
        self._status_panel = StatusPanel(self.root, on_settings=self._on_settings)
        self._status_panel.pack(fill=tk.X, padx=4, pady=(4, 0))

        # Gamepad indicator
        self._gamepad_indicator = GamepadIndicator(self.root)
        self._gamepad_indicator.pack(fill=tk.X, padx=8, pady=(0, 2))

        # Main content area — three equal-width columns via grid
        content = ttk.Frame(self.root)
        content.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        content.grid_columnconfigure(0, weight=1, uniform="col")
        content.grid_columnconfigure(1, weight=1, uniform="col")
        content.grid_columnconfigure(2, weight=1, uniform="col")
        content.grid_rowconfigure(0, weight=1)

        # Conversion factors from settings
        sk_cfg = self._settings.get("sigmakoki", {})
        sk_um_xy = sk_cfg.get("um_per_step_xy", 1.0)
        sk_um_z = sk_cfg.get("um_per_step_z", 1.0)
        zx_cfg = self._settings.get("zolix", {})
        zx_um_xy = zx_cfg.get("um_per_step_xy", 1.0)
        zx_um_r = zx_cfg.get("um_per_step_r", 1.0)

        # Left: SigmaKoki XYZ
        self._sk_panel = StagePanel(
            content,
            stage_id="sigmakoki",
            title="XYZ STAGE",
            axes=["x", "y", "z"],
            on_enable_toggle=self._on_enable_toggle,
            on_button_press=self._on_ui_button_press,
            on_button_release=self._on_ui_button_release,
            on_stop=lambda sid: self._instruments.stop_all_stages(),
            on_zero=lambda sid: self._send_zero(sid),
            step_um={"x": (sk_um_xy, "µm"), "y": (sk_um_xy, "µm"), "z": (sk_um_z, "µm")},
        )
        self._sk_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 2))

        # Center: Zolix XYR
        self._zx_panel = StagePanel(
            content,
            stage_id="zolix",
            title="XYR STAGE",
            axes=["x", "y", "r"],
            on_enable_toggle=self._on_enable_toggle,
            on_button_press=self._on_ui_button_press,
            on_button_release=self._on_ui_button_release,
            on_stop=lambda sid: self._instruments.stop_all_stages(),
            on_zero=lambda sid: self._send_zero(sid),
            step_um={"x": (zx_um_xy, "µm"), "y": (zx_um_xy, "µm"), "r": (zx_um_r, "°")},
        )
        self._zx_panel.grid(row=0, column=1, sticky="nsew", padx=2)

        # Right: Temperature
        self._temp_panel = TemperaturePanel(
            content,
            settings=self._settings.get("yudian", {}),
            on_apply=self._on_temp_apply,
            on_presets_changed=self._on_presets_changed,
        )
        self._temp_panel.grid(row=0, column=2, sticky="nsew", padx=(2, 0))

    # ------------------------------------------------------------------
    # Queue Drain Loops
    # ------------------------------------------------------------------

    def _drain_gui_queue(self) -> None:
        """Drain stage state updates into the UI panels."""
        while True:
            try:
                item = self._gui_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, StageState):
                panel = self._sk_panel if item.stage_id == "sigmakoki" else self._zx_panel
                panel.update_state(item)
        self.root.after(30, self._drain_gui_queue)

    def _drain_gamepad_queue(self) -> None:
        """Drain gamepad status events."""
        while True:
            try:
                msg = self._gamepad_status_queue.get_nowait()
            except queue.Empty:
                break
            if msg.get("type") == "gamepad_connection":
                connected = msg.get("connected", False)
                self._status_panel.set_gamepad_status(connected)
                self._gamepad_indicator.set_connected(connected)
            elif msg.get("type") == "dpad_stage":
                self._gamepad_indicator.set_dpad_stage(msg.get("stage", "sigmakoki"))
        self.root.after(200, self._drain_gamepad_queue)

    def _drain_connect_queue(self) -> None:
        """Drain device connection status updates."""
        while True:
            try:
                msg = self._connect_status_queue.get_nowait()
            except queue.Empty:
                break
            dev = msg.get("device", "")
            connected = msg.get("connected", False)

            if dev == "sigmakoki":
                self._status_panel.set_device_status("sigmakoki", connected)
            elif dev == "zolix":
                self._status_panel.set_device_status("zolix", connected)
            elif dev == "yudian":
                self._status_panel.set_device_status("yudian", connected)
                if connected:
                    self._temp_panel.set_connected()
                    self._start_temp_polling()
                else:
                    self._temp_panel.set_disconnected()
                    self._stop_temp_polling()
        self.root.after(200, self._drain_connect_queue)

    # ------------------------------------------------------------------
    # Temperature polling — on MainWindow, matching original app.py exactly
    # ------------------------------------------------------------------

    def _start_temp_polling(self) -> None:
        if self._temp_polling:
            return
        self._temp_polling = True
        self._temp_stop_event.clear()
        self._temp_poll_gen += 1  # bump generation so any stale loop exits
        # Start poll thread — it only writes to a plain attribute, NO tkinter calls
        threading.Thread(
            target=self._temp_poll_loop, daemon=True, name="temp_poller",
        ).start()
        # Start display checker on MAIN thread — polls the attribute
        self.root.after(100, self._temp_check_display, self._temp_poll_gen)

    def _stop_temp_polling(self) -> None:
        self._temp_polling = False
        self._temp_stop_event.set()

    def _temp_poll_loop(self) -> None:
        driver = self._instruments.yudian
        stop = self._temp_stop_event

        while not stop.is_set():
            # Read poll interval each iteration so Settings changes take effect
            interval = self._settings.get("yudian", {}).get("poll_interval_ms", 500) / 1000.0

            if driver is None or not driver.is_connected:
                with self._temp_lock:
                    self._temp_latest = {"disconnected": True}
                break
            try:
                data = driver.read_all()
                with self._temp_lock:
                    self._temp_latest = data
            except YudianCommunicationError as exc:
                with self._temp_lock:
                    self._temp_latest = {"error": str(exc)}
            except YudianConnectionError:
                with self._temp_lock:
                    self._temp_latest = {"disconnected": True}
                break
            except Exception as exc:
                logger.debug("Temp poll error: %s", exc)
                with self._temp_lock:
                    self._temp_latest = {"error": str(exc)}
            stop.wait(interval)

    def _temp_check_display(self, generation: int = 0) -> None:
        """Called on MAIN thread every 100ms — polls _temp_latest attribute."""
        # Exit immediately if this loop's generation is stale
        if generation != self._temp_poll_gen:
            return
        with self._temp_lock:
            data = self._temp_latest
            self._temp_latest = None
        if data is not None:
            if "disconnected" in data:
                self._temp_panel.set_disconnected()
                self._temp_polling = False
            elif "error" in data:
                self._temp_panel.set_error(data["error"])
            else:
                self._temp_update_display(data)
        if self._temp_polling:
            self.root.after(100, self._temp_check_display, generation)

    def _temp_update_display(self, data: dict) -> None:
        pv, mv, sv = data.get("pv"), data.get("mv"), data.get("sv")

        # Heat-status color for PV display
        if pv is not None and mv is not None and sv is not None:
            delta = pv - sv
            if mv > 80:
                pv_color = "#e53935"   # red — full power
            elif mv > 40:
                pv_color = "#fb8c00"   # orange — moderate heating
            elif mv > 10:
                pv_color = "#fdd835"   # amber — gentle heating
            elif abs(delta) < 0.5:
                pv_color = "#43a047"   # green — stable at target
            elif abs(delta) < 2.0:
                pv_color = "#66bb6a"   # light green — near target
            elif delta > 0:
                pv_color = "#1e88e5"   # blue — cooling
            else:
                pv_color = COLOR_BLUE  # default blue
        else:
            pv_color = COLOR_BLUE

        if pv is not None:
            self._temp_panel.set_pv(pv, pv_color)
        if mv is not None:
            self._temp_panel.set_mv(mv)
        if sv is not None:
            if not self._temp_panel.sv_safety_checked:
                self._temp_panel.sv_safety_checked = True
                # Only sync SV from device ONCE on startup
                lo, hi = self._temp_panel.get_safety_limits()
                if sv < lo or sv > hi:
                    clamped = max(lo, min(hi, sv))
                    logger.warning("SV %.1f clamped to %.1f", sv, clamped)
                    self._temp_panel.show_sv_error(
                        f"⚠ SV {sv:.1f}°C clamped to {clamped:.0f}°C", COLOR_WARN)
                    threading.Thread(
                        target=lambda: self._instruments.yudian.set_sv(clamped),
                        daemon=True,
                    ).start()
                    sv = clamped
                self._temp_panel.set_sv(sv)

    def _on_temp_apply(self, value: float) -> None:
        def _write():
            try:
                if self._instruments.yudian is None:
                    self.root.after(0, lambda: [
                        self._temp_panel.show_sv_error("Temperature controller not configured"),
                        self._temp_panel.reenable_apply(),
                    ])
                    return
                self._instruments.yudian.set_sv(value)
                self.root.after(0, lambda: [
                    self._temp_panel.clear_sv_error(),
                    self._temp_panel.flash_apply_ok(),
                    self._temp_panel.reenable_apply(),
                ])
            except (YudianCommunicationError, YudianConnectionError) as exc:
                self.root.after(0, lambda: [
                    self._temp_panel.show_sv_error(str(exc)),
                    self._temp_panel.reenable_apply(),
                ])
            except ValueError as exc:
                self.root.after(0, lambda: [
                    self._temp_panel.show_sv_error(str(exc)),
                    self._temp_panel.reenable_apply(),
                ])
        threading.Thread(target=_write, daemon=True, name="sv_write").start()

    # ------------------------------------------------------------------
    # Auto-connect
    # ------------------------------------------------------------------

    def _auto_connect(self) -> None:
        """Attempt to connect to all configured devices on startup."""
        self._instruments.connect_all(
            status_queue=self._connect_status_queue,
            connect_sigmakoki=bool(self._settings.get("sigmakoki", {}).get("port")),
            connect_zolix=bool(self._settings.get("zolix", {}).get("port")),
            connect_yudian=bool(self._settings.get("yudian", {}).get("port")),
        )

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    def _on_enable_toggle(self, stage_id: str, enabled: bool) -> None:
        """Software enable/disable toggle."""
        self._instruments.set_enabled(stage_id, enabled)
        self._input_manager.update_enabled({
            "sigmakoki": self._instruments.is_enabled("sigmakoki"),
            "zolix": self._instruments.is_enabled("zolix"),
        })

    def _on_ui_button_press(self, stage_id: str, axis: str, direction: int, single_step: bool) -> None:
        """On-screen button press — uses per-axis speed."""
        instruments = self._instruments
        if stage_id == "sigmakoki":
            if axis == "z":
                speed = instruments.sigmakoki_slow_z
            else:
                speed = instruments.sigmakoki_slow_speed
        else:  # zolix
            if axis == "r":
                speed = instruments.zolix_slow_r
            else:
                speed = instruments.zolix_slow_speed
        cmd = StageCommand(
            stage_id=stage_id,
            axis=axis,
            mode="single_step" if single_step else "continuous_start",
            direction=direction,
            speed=speed,
            source="ui_button",
        )
        self._instruments.execute(cmd)

    def _on_ui_button_release(self, stage_id: str, axis: str) -> None:
        """On-screen button release — stop continuous."""
        cmd = StageCommand(
            stage_id=stage_id,
            axis=axis,
            mode="continuous_stop",
            direction=0,
            speed=0,
            source="ui_button",
        )
        self._instruments.execute(cmd)

    def _on_escape(self) -> None:
        """Escape key — emergency stop all."""
        self._instruments.stop_all_stages()
        logger.info("ESCAPE: STOP ALL triggered")

    def _send_zero(self, stage_id: str) -> None:
        """Reset position counters to zero (no physical movement)."""
        driver = self._instruments.sigmakoki if stage_id == "sigmakoki" else self._instruments.zolix
        try:
            if stage_id == "sigmakoki":
                driver.zero()
            else:
                driver.zero_all()
        except Exception as exc:
            logger.warning("Zero failed for %s: %s", stage_id, exc)

    def _on_settings(self) -> None:
        """Open the settings dialog."""
        dlg = SettingsDialog(self.root, self._settings)
        self.root.wait_window(dlg._win)
        if dlg.result is not None:
            # Check if any port changed BEFORE merging (for reconnect decision)
            port_changed = False
            for section in ("sigmakoki", "zolix", "yudian"):
                if section in dlg.result and "port" in dlg.result[section]:
                    old_port = self._settings.get(section, {}).get("port", "")
                    new_port = dlg.result[section]["port"]
                    if old_port != new_port:
                        port_changed = True
                        break

            # Merge result into settings
            for section, values in dlg.result.items():
                if section in self._settings:
                    self._settings[section].update(values)
                else:
                    self._settings[section] = values
            save_settings(self._settings)

            # Update InstrumentManager configs
            self._instruments.update_configs(
                sigmakoki_config=self._settings.get("sigmakoki", {}),
                zolix_config=self._settings.get("zolix", {}),
            )

            # Update temperature safety limits
            yudian_cfg = self._settings.get("yudian", {})
            self._temp_panel.update_safety_limits(
                yudian_cfg.get("safety_temp_lo_c", -100.0),
                yudian_cfg.get("safety_temp_hi_c", 400.0),
            )
            self._temp_panel.sv_safety_checked = False  # re-validate SV against new limits

            # Update gamepad config
            gamepad_cfg = self._settings.get("gamepad", {})
            self._gamepad.update_config(
                deadzone=gamepad_cfg.get("deadzone", 0.10),
                invert={
                    "left_x": gamepad_cfg.get("invert_left_x", False),
                    "left_y": gamepad_cfg.get("invert_left_y", False),
                    "right_x": gamepad_cfg.get("invert_right_x", False),
                    "right_y": gamepad_cfg.get("invert_right_y", False),
                },
            )

            # Update input config
            self._input_manager.update_speeds(
                trigger_threshold=gamepad_cfg.get("trigger_threshold", 0.5),
            )

            # Refresh conversion factors
            sk_cfg = self._settings.get("sigmakoki", {})
            self._sk_panel.update_step_um({
                "x": (sk_cfg.get("um_per_step_xy", 1.0), "µm"),
                "y": (sk_cfg.get("um_per_step_xy", 1.0), "µm"),
                "z": (sk_cfg.get("um_per_step_z", 1.0), "µm"),
            })
            zx_cfg = self._settings.get("zolix", {})
            self._zx_panel.update_step_um({
                "x": (zx_cfg.get("um_per_step_xy", 1.0), "µm"),
                "y": (zx_cfg.get("um_per_step_xy", 1.0), "µm"),
                "r": (zx_cfg.get("um_per_step_r", 1.0), "°"),
            })

            if port_changed:
                self._auto_connect()

    def _on_presets_changed(self, presets: list) -> None:
        """Preset list was edited — save to settings."""
        self._settings["yudian"]["presets"] = presets
        save_settings(self._settings)

    def _on_close(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down…")
        self._input_manager.stop()
        self._stop_temp_polling()
        self._instruments.disconnect_all()
        self._keyboard.unbind()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the tkinter main loop."""
        self.root.mainloop()
