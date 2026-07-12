"""
Settings Dialog
================

Tabbed modal dialog for configuring all device connections,
speed settings, axis inversion, gamepad configuration, and
input parameters.

Reads from and writes to the application settings dict via
the config module.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from utils.serial_utils import list_available_ports

from gui.styles import FONT_SMALL


BAUDRATES = ["4800", "9600", "14400", "19200", "38400", "57600", "115200", "230400", "250000", "500000"]
PARITIES = ["N", "E", "O"]


class SettingsDialog:
    """Tabbed settings dialog for all application configuration."""

    def __init__(self, parent: tk.Widget, settings: Dict[str, Any]) -> None:
        self._settings = settings
        self._result: Optional[Dict[str, Any]] = None
        self._ports = list_available_ports(refresh=True)
        self._vars: Dict[str, Any] = {}

        self._win = tk.Toplevel(parent)
        self._win.title("Settings")
        self._win.resizable(False, False)
        self._win.transient(parent)
        self._win.grab_set()

        notebook = ttk.Notebook(self._win, padding=8)
        notebook.pack(fill="both", expand=True)

        # Tabs
        self._build_device_tab(notebook, "SigmaKoki XYZ", "sigmakoki")
        self._build_device_tab(notebook, "Zolix XYR", "zolix", has_slave=True)
        self._build_yudian_tab(notebook)
        self._build_gamepad_tab(notebook)
        self._build_input_tab(notebook)

        # Buttons
        btn_frame = ttk.Frame(self._win, padding=(8, 4))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Cancel", command=self._win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="OK", command=self._on_ok).pack(side="right")

        self._win.bind("<Return>", lambda e: self._on_ok())
        self._win.bind("<Escape>", lambda e: self._win.destroy())
        self._center(parent)

    def _center(self, parent: tk.Widget) -> None:
        self._win.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        ww, wh = self._win.winfo_width(), self._win.winfo_height()
        self._win.geometry(f"+{px + (pw - ww) // 2}+{py + (ph - wh) // 2}")

    # ------------------------------------------------------------------
    # Tab Builders
    # ------------------------------------------------------------------

    def _build_device_tab(
        self, notebook: ttk.Notebook, title: str, key: str, has_slave: bool = False,
    ) -> None:
        """Build a tab for SigmaKoki or Zolix stage settings."""
        cfg = self._settings.get(key, {})
        f = ttk.Frame(notebook, padding=12)
        notebook.add(f, text=title)
        f.grid_columnconfigure(0, weight=0)
        f.grid_columnconfigure(1, weight=1)

        r = 0
        prefix = key

        # COM Port
        ttk.Label(f, text="COM Port:").grid(row=r, column=0, sticky="w", pady=2)
        port_var = tk.StringVar(value=cfg.get("port", ""))
        avail = list(self._ports)
        if cfg.get("port") and cfg["port"] not in avail:
            avail.insert(0, cfg["port"])
        ttk.Combobox(f, textvariable=port_var, values=avail, width=10).grid(
            row=r, column=1, sticky="ew", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_port"] = port_var
        r += 1

        # Baudrate
        ttk.Label(f, text="Baudrate:").grid(row=r, column=0, sticky="w", pady=2)
        baud_var = tk.StringVar(value=str(cfg.get("baudrate", 115200)))
        ttk.Combobox(f, textvariable=baud_var, values=BAUDRATES, width=12).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_baudrate"] = baud_var
        r += 1

        # Slave address (Zolix and Yudian)
        if has_slave:
            ttk.Label(f, text="Slave Address:").grid(row=r, column=0, sticky="w", pady=2)
            slave_var = tk.IntVar(value=cfg.get("slave_address", 1))
            ttk.Spinbox(f, from_=1, to=255, textvariable=slave_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_slave"] = slave_var
            r += 1

        # Timeout
        ttk.Label(f, text="Timeout (s):").grid(row=r, column=0, sticky="w", pady=2)
        timeout_var = tk.DoubleVar(value=cfg.get("timeout_s", 0.05))
        ttk.Spinbox(f, from_=0.1, to=5.0, increment=0.1, textvariable=timeout_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_timeout"] = timeout_var
        r += 1

        # T separator
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4),
        )
        r += 1

        # ── Speed ──────────────────────────────────────────────
        ttk.Label(f, text="Speed", font=FONT_SMALL).grid(
            row=r, column=0, columnspan=2, sticky="w",
        )
        r += 1

        label_unit = "steps/s" if key == "zolix" else "Hz"
        ttk.Label(f, text=f"XY Slow ({label_unit}):").grid(row=r, column=0, sticky="w", pady=2)
        slow_var = tk.IntVar(value=cfg.get("slow_speed_hz", cfg.get("slow_speed_pps", 200)))
        ttk.Spinbox(f, from_=1, to=100000, textvariable=slow_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_slow"] = slow_var
        r += 1

        ttk.Label(f, text=f"XY Fast ({label_unit}):").grid(row=r, column=0, sticky="w", pady=2)
        fast_var = tk.IntVar(value=cfg.get("fast_speed_hz", cfg.get("fast_speed_pps", 500)))
        ttk.Spinbox(f, from_=1, to=100000, textvariable=fast_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_fast"] = fast_var
        r += 1

        if key == "sigmakoki":
            ttk.Label(f, text=f"Z Slow ({label_unit}):").grid(row=r, column=0, sticky="w", pady=2)
            zs_var = tk.IntVar(value=cfg.get("slow_speed_z", 200))
            ttk.Spinbox(f, from_=1, to=100000, textvariable=zs_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_slow_z"] = zs_var
            r += 1
            ttk.Label(f, text=f"Z Fast ({label_unit}):").grid(row=r, column=0, sticky="w", pady=2)
            zf_var = tk.IntVar(value=cfg.get("fast_speed_z", 500))
            ttk.Spinbox(f, from_=1, to=100000, textvariable=zf_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_fast_z"] = zf_var
            r += 1
        elif key == "zolix":
            ttk.Label(f, text=f"R Slow ({label_unit}):").grid(row=r, column=0, sticky="w", pady=2)
            rs_var = tk.IntVar(value=cfg.get("slow_speed_r", 500))
            ttk.Spinbox(f, from_=1, to=100000, textvariable=rs_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_slow_r"] = rs_var
            r += 1
            ttk.Label(f, text=f"R Fast ({label_unit}):").grid(row=r, column=0, sticky="w", pady=2)
            rf_var = tk.IntVar(value=cfg.get("fast_speed_r", 2000))
            ttk.Spinbox(f, from_=1, to=100000, textvariable=rf_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_fast_r"] = rf_var
            r += 1

        # ── Step ───────────────────────────────────────────────
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4),
        )
        r += 1
        ttk.Label(f, text="Step", font=FONT_SMALL).grid(
            row=r, column=0, columnspan=2, sticky="w",
        )
        r += 1

        ttk.Label(f, text="XY Single Step (steps):").grid(row=r, column=0, sticky="w", pady=2)
        step_var = tk.IntVar(value=cfg.get("single_step_amount", 10))
        ttk.Spinbox(f, from_=1, to=10000, textvariable=step_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_step"] = step_var
        r += 1

        if key == "sigmakoki":
            ttk.Label(f, text="Z Single Step (steps):").grid(row=r, column=0, sticky="w", pady=2)
            zstep_var = tk.IntVar(value=cfg.get("single_step_z", cfg.get("single_step_amount", 10)))
            ttk.Spinbox(f, from_=1, to=10000, textvariable=zstep_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_step_z"] = zstep_var
            r += 1
        elif key == "zolix":
            ttk.Label(f, text="R Single Step (steps):").grid(row=r, column=0, sticky="w", pady=2)
            rstep_var = tk.IntVar(value=cfg.get("single_step_r", cfg.get("single_step_amount", 100)))
            ttk.Spinbox(f, from_=1, to=10000, textvariable=rstep_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_step_r"] = rstep_var
            r += 1

        # ── Display ────────────────────────────────────────────
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4),
        )
        r += 1
        ttk.Label(f, text="Display", font=FONT_SMALL).grid(
            row=r, column=0, columnspan=2, sticky="w",
        )
        r += 1

        ttk.Label(f, text="XY µm/step:").grid(row=r, column=0, sticky="w", pady=2)
        xyum_var = tk.DoubleVar(value=cfg.get("um_per_step_xy", 1.0))
        ttk.Spinbox(f, from_=0.001, to=1000.0, increment=0.1, textvariable=xyum_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars[f"{prefix}_um_xy"] = xyum_var
        r += 1

        if key == "sigmakoki":
            ttk.Label(f, text="Z µm/step:").grid(row=r, column=0, sticky="w", pady=2)
            zum_var = tk.DoubleVar(value=cfg.get("um_per_step_z", 1.0))
            ttk.Spinbox(f, from_=0.001, to=1000.0, increment=0.1, textvariable=zum_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_um_z"] = zum_var
            r += 1
        elif key == "zolix":
            ttk.Label(f, text="R °/step:").grid(row=r, column=0, sticky="w", pady=2)
            rum_var = tk.DoubleVar(value=cfg.get("um_per_step_r", 1.0))
            ttk.Spinbox(f, from_=0.001, to=1000.0, increment=0.1, textvariable=rum_var, width=8).grid(
                row=r, column=1, sticky="w", pady=2, padx=(8, 0),
            )
            self._vars[f"{prefix}_um_r"] = rum_var
            r += 1

        # ── Axis ───────────────────────────────────────────────
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4),
        )
        r += 1
        ttk.Label(f, text="Invert Axes", font=FONT_SMALL).grid(
            row=r, column=0, columnspan=2, sticky="w",
        )
        r += 1

        axes = ["x", "y", "z"] if key == "sigmakoki" else ["x", "y", "r"]
        for axis in axes:
            inv_var = tk.BooleanVar(value=cfg.get(f"invert_{axis}", False))
            ttk.Checkbutton(f, text=f"Invert {axis.upper()} axis", variable=inv_var).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=1,
            )
            self._vars[f"{prefix}_invert_{axis}"] = inv_var
            r += 1

        # Flip XY
        flip_var = tk.BooleanVar(value=cfg.get("flip_xy", False))
        ttk.Checkbutton(f, text="Flip X/Y axes", variable=flip_var).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(4, 1),
        )
        self._vars[f"{prefix}_flip_xy"] = flip_var
        r += 1

    def _build_yudian_tab(self, notebook: ttk.Notebook) -> None:
        """Build the Yudian temperature controller settings tab."""
        cfg = self._settings.get("yudian", {})
        f = ttk.Frame(notebook, padding=12)
        notebook.add(f, text="Temperature")
        f.grid_columnconfigure(0, weight=0)
        f.grid_columnconfigure(1, weight=1)

        r = 0
        # COM Port
        ttk.Label(f, text="COM Port:").grid(row=r, column=0, sticky="w", pady=2)
        port_var = tk.StringVar(value=cfg.get("port", ""))
        avail = list(self._ports)
        if cfg.get("port") and cfg["port"] not in avail:
            avail.insert(0, cfg["port"])
        ttk.Combobox(f, textvariable=port_var, values=avail, width=10).grid(
            row=r, column=1, sticky="ew", pady=2, padx=(8, 0),
        )
        self._vars["yudian_port"] = port_var
        r += 1

        # Baudrate
        ttk.Label(f, text="Baudrate:").grid(row=r, column=0, sticky="w", pady=2)
        baud_var = tk.StringVar(value=str(cfg.get("baudrate", 9600)))
        ttk.Combobox(f, textvariable=baud_var, values=BAUDRATES, width=12).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["yudian_baudrate"] = baud_var
        r += 1

        # Slave address
        ttk.Label(f, text="Slave Address:").grid(row=r, column=0, sticky="w", pady=2)
        slave_var = tk.IntVar(value=cfg.get("slave_address", 1))
        ttk.Spinbox(f, from_=1, to=247, textvariable=slave_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["yudian_slave"] = slave_var
        r += 1

        # Timeout
        ttk.Label(f, text="Timeout (s):").grid(row=r, column=0, sticky="w", pady=2)
        timeout_var = tk.DoubleVar(value=cfg.get("timeout_s", 0.5))
        ttk.Spinbox(f, from_=0.1, to=5.0, increment=0.1, textvariable=timeout_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["yudian_timeout"] = timeout_var
        r += 1

        # Safety limits
        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4),
        )
        r += 1
        ttk.Label(f, text="Safety Limits", font=FONT_SMALL).grid(
            row=r, column=0, columnspan=2, sticky="w",
        )
        r += 1

        ttk.Label(f, text="Min (°C):").grid(row=r, column=0, sticky="w", pady=2)
        slo_var = tk.DoubleVar(value=cfg.get("safety_temp_lo_c", -100))
        ttk.Spinbox(f, from_=-200, to=1300, textvariable=slo_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["yudian_safety_lo"] = slo_var
        r += 1

        ttk.Label(f, text="Max (°C):").grid(row=r, column=0, sticky="w", pady=2)
        shi_var = tk.DoubleVar(value=cfg.get("safety_temp_hi_c", 400))
        ttk.Spinbox(f, from_=-200, to=1300, textvariable=shi_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["yudian_safety_hi"] = shi_var
        r += 1

    def _build_gamepad_tab(self, notebook: ttk.Notebook) -> None:
        """Build the gamepad configuration tab."""
        cfg = self._settings.get("gamepad", {})
        f = ttk.Frame(notebook, padding=12)
        notebook.add(f, text="Gamepad")
        f.grid_columnconfigure(0, weight=0)
        f.grid_columnconfigure(1, weight=1)

        r = 0
        ttk.Label(f, text="Trigger Threshold:").grid(row=r, column=0, sticky="w", pady=2)
        tt_var = tk.DoubleVar(value=cfg.get("trigger_threshold", 0.5))
        ttk.Spinbox(f, from_=0.0, to=1.0, increment=0.05, textvariable=tt_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["gamepad_trigger"] = tt_var
        r += 1

        ttk.Separator(f, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4),
        )
        r += 1
        ttk.Label(f, text="Invert Analog Sticks", font=FONT_SMALL).grid(
            row=r, column=0, columnspan=2, sticky="w",
        )
        r += 1

        for key, label in [
            ("left_x", "Invert Left Stick X"),
            ("left_y", "Invert Left Stick Y"),
            ("right_x", "Invert Right Stick X"),
            ("right_y", "Invert Right Stick Y"),
        ]:
            inv_var = tk.BooleanVar(value=cfg.get(f"invert_{key}", False))
            ttk.Checkbutton(f, text=label, variable=inv_var).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=1,
            )
            self._vars[f"gamepad_invert_{key}"] = inv_var
            r += 1

    def _build_input_tab(self, notebook: ttk.Notebook) -> None:
        """Build the input system configuration tab."""
        cfg = self._settings.get("input", {})
        f = ttk.Frame(notebook, padding=12)
        notebook.add(f, text="Input")
        f.grid_columnconfigure(0, weight=0)
        f.grid_columnconfigure(1, weight=1)

        r = 0
        ttk.Label(f, text="Long Press Threshold (ms):").grid(row=r, column=0, sticky="w", pady=2)
        lp_var = tk.IntVar(value=cfg.get("long_press_threshold_ms", 300))
        ttk.Spinbox(f, from_=100, to=1000, increment=50, textvariable=lp_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["input_long_press"] = lp_var
        r += 1

        ttk.Label(f, text="Loop Rate (Hz):").grid(row=r, column=0, sticky="w", pady=2)
        lr_var = tk.IntVar(value=cfg.get("loop_rate_hz", 60))
        ttk.Spinbox(f, from_=20, to=120, increment=10, textvariable=lr_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["input_loop_rate"] = lr_var
        r += 1

        ttk.Label(f, text="Status Poll Rate (Hz):").grid(row=r, column=0, sticky="w", pady=2)
        sp_var = tk.IntVar(value=cfg.get("status_poll_rate_hz", 10))
        ttk.Spinbox(f, from_=1, to=20, increment=1, textvariable=sp_var, width=8).grid(
            row=r, column=1, sticky="w", pady=2, padx=(8, 0),
        )
        self._vars["input_status_poll"] = sp_var
        r += 1

    # ------------------------------------------------------------------
    # OK handler
    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        """Validate and build result dict."""
        result = {}
        errors = []

        try:
            # SigmaKoki
            result["sigmakoki"] = {
                "port": self._vars["sigmakoki_port"].get().strip(),
                "baudrate": int(self._vars["sigmakoki_baudrate"].get()),
                "timeout_s": float(self._vars["sigmakoki_timeout"].get()),
                "slow_speed_hz": int(self._vars["sigmakoki_slow"].get()),
                "fast_speed_hz": int(self._vars["sigmakoki_fast"].get()),
                "single_step_amount": int(self._vars["sigmakoki_step"].get()),
                "single_step_z": int(self._vars["sigmakoki_step_z"].get()),
                "invert_x": self._vars["sigmakoki_invert_x"].get(),
                "invert_y": self._vars["sigmakoki_invert_y"].get(),
                "invert_z": self._vars["sigmakoki_invert_z"].get(),
                "flip_xy": self._vars["sigmakoki_flip_xy"].get(),
                "slow_speed_z": int(self._vars["sigmakoki_slow_z"].get()),
                "fast_speed_z": int(self._vars["sigmakoki_fast_z"].get()),
                "um_per_step_xy": float(self._vars["sigmakoki_um_xy"].get()),
                "um_per_step_z": float(self._vars["sigmakoki_um_z"].get()),
            }

            # Zolix
            result["zolix"] = {
                "port": self._vars["zolix_port"].get().strip(),
                "baudrate": int(self._vars["zolix_baudrate"].get()),
                "slave_address": int(self._vars["zolix_slave"].get()),
                "timeout_s": float(self._vars["zolix_timeout"].get()),
                "slow_speed_pps": int(self._vars["zolix_slow"].get()),
                "fast_speed_pps": int(self._vars["zolix_fast"].get()),
                "single_step_amount": int(self._vars["zolix_step"].get()),
                "single_step_r": int(self._vars["zolix_step_r"].get()),
                "invert_x": self._vars["zolix_invert_x"].get(),
                "invert_y": self._vars["zolix_invert_y"].get(),
                "invert_r": self._vars["zolix_invert_r"].get(),
                "flip_xy": self._vars["zolix_flip_xy"].get(),
                "slow_speed_r": int(self._vars["zolix_slow_r"].get()),
                "fast_speed_r": int(self._vars["zolix_fast_r"].get()),
                "um_per_step_xy": float(self._vars["zolix_um_xy"].get()),
                "um_per_step_r": float(self._vars["zolix_um_r"].get()),
                "stop_mode": self._settings.get("zolix", {}).get("stop_mode", "immediate"),
            }

            # Yudian
            slo = self._vars["yudian_safety_lo"].get()
            shi = self._vars["yudian_safety_hi"].get()
            if slo >= shi:
                errors.append("Min safety temp must be < Max safety temp.")
            result["yudian"] = {
                "port": self._vars["yudian_port"].get().strip(),
                "baudrate": int(self._vars["yudian_baudrate"].get()),
                "slave_address": int(self._vars["yudian_slave"].get()),
                "timeout_s": float(self._vars["yudian_timeout"].get()),
                "safety_temp_lo_c": float(slo),
                "safety_temp_hi_c": float(shi),
                "poll_interval_ms": self._settings.get("yudian", {}).get("poll_interval_ms", 500),
                "presets": self._settings.get("yudian", {}).get("presets", []),
            }

            # Gamepad
            result["gamepad"] = {
                "trigger_threshold": float(self._vars["gamepad_trigger"].get()),
                "invert_left_x": self._vars["gamepad_invert_left_x"].get(),
                "invert_left_y": self._vars["gamepad_invert_left_y"].get(),
                "invert_right_x": self._vars["gamepad_invert_right_x"].get(),
                "invert_right_y": self._vars["gamepad_invert_right_y"].get(),
            }

            # Input
            result["input"] = {
                "long_press_threshold_ms": int(self._vars["input_long_press"].get()),
                "loop_rate_hz": int(self._vars["input_loop_rate"].get()),
                "status_poll_rate_hz": int(self._vars["input_status_poll"].get()),
            }

        except (ValueError, tk.TclError) as exc:
            errors.append(str(exc))

        if errors:
            messagebox.showerror("Invalid Input", "\n".join(errors), parent=self._win)
            return

        self._result = result
        self._win.destroy()

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self._result
