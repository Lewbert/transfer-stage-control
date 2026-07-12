"""
Yudian AI-828 Temperature Controller — GUI Application
======================================================

Built with tkinter/ttk.  MODBUS-RTU over RS485 (AFC=0).

Usage:  python app.py
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

import serial.tools.list_ports

from yudian_ai828 import (
    YudianCommunicationError,
    YudianConnectionError,
    YudianController,
)

# ---------------------------------------------------------------------------
# Paths & logging
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_APP_DIR, "settings.json")
LOG_FILE = os.path.join(_APP_DIR, "debug.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8", mode="w")],
)
_log = logging.getLogger("app")
_log.info("App starting")

def _log_uncaught(exc_type, exc_value, exc_tb):
    _log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)
sys.excepthook = _log_uncaught

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "Yudian AI-828 Controller"
POLL_INTERVAL_MS = 500       # Poll the device twice per second
QUEUE_CHECK_MS = 100
QUEUE_MAXSIZE = 5
SCAN_TIMEOUT = 0.10

def _font(size, bold=False):
    return ("Microsoft YaHei", size, "bold" if bold else "normal")

FONT_LARGE  = _font(40, bold=True)
FONT_MEDIUM = _font(14)
FONT_SMALL  = _font(10)
FONT_STATUS = _font(9)

COLOR_OK    = "#2e7d32"
COLOR_ERROR = "#c62828"
COLOR_WARN  = "#ef6c00"
COLOR_BLUE  = "#1565c0"
COLOR_GRAY  = "#757575"
COLOR_BG    = "#fafafa"

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    "port": "", "slave": 1, "baudrate": 9600,
    "parity": "N", "stopbits": 1, "timeout": 0.5,
    "safety_lo": -100.0, "safety_hi": 400.0,
    "last_port": "", "last_slave": 1, "last_baud": 9600,
    "presets": [
        {"name": "Room Temp", "temp": 25.0},
        {"name": "Body Temp", "temp": 37.0},
        {"name": "Boiling",   "temp": 100.0},
    ],
}

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            s = json.load(open(SETTINGS_FILE, "r", encoding="utf-8"))
            return {**DEFAULT_SETTINGS, **s}
    except Exception:
        pass
    return dict(DEFAULT_SETTINGS)

def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    json.dump(settings, open(SETTINGS_FILE, "w", encoding="utf-8"), indent=2)

# ---------------------------------------------------------------------------
# Settings Dialog
# ---------------------------------------------------------------------------

class SettingsDialog:
    BAUDRATES = ["4800","9600","14400","19200","38400","57600","115200"]
    PARITIES  = ["N","E","O"]

    def __init__(self, parent, settings):
        self._result = None
        self._settings = dict(settings)
        self._win = tk.Toplevel(parent)
        self._win.title("Connection Settings")
        self._win.resizable(False, False)
        self._win.transient(parent)
        self._win.grab_set()
        f = ttk.Frame(self._win, padding=12); f.pack(fill="both",expand=True)
        r=0

        ttk.Label(f,text="COM Port:").grid(row=r,column=0,sticky="w",pady=1)
        avail=[p.device for p in serial.tools.list_ports.comports()]
        if self._settings["port"] and self._settings["port"] not in avail:
            avail.insert(0,self._settings["port"])
        self._port_var=tk.StringVar(value=self._settings["port"] or (avail[0] if avail else ""))
        ttk.Combobox(f,textvariable=self._port_var,values=avail,width=22).grid(row=r,column=1,sticky="ew",pady=1,padx=(8,0)); r+=1

        self._baud_var=tk.StringVar(value=str(self._settings["baudrate"]))
        self._slave_var=tk.IntVar(value=self._settings["slave"])
        self._parity_var=tk.StringVar(value=self._settings["parity"])
        self._stopbits_var=tk.IntVar(value=self._settings["stopbits"])
        self._timeout_var=tk.DoubleVar(value=self._settings["timeout"])

        for lbl,w in [("Baud Rate:",ttk.Combobox(f,textvariable=self._baud_var,values=self.BAUDRATES,width=12)),
                       ("Slave Addr:",ttk.Spinbox(f,from_=1,to=247,textvariable=self._slave_var,width=6)),
                       ("Parity:",ttk.Combobox(f,textvariable=self._parity_var,values=self.PARITIES,width=6)),
                       ("Stop Bits:",ttk.Spinbox(f,from_=1,to=2,textvariable=self._stopbits_var,width=4)),
                       ("Timeout (s):",ttk.Spinbox(f,from_=0.1,to=5.0,increment=0.1,textvariable=self._timeout_var,width=6))]:
            ttk.Label(f,text=lbl).grid(row=r,column=0,sticky="w",pady=1)
            w.grid(row=r,column=1,sticky="w",pady=1,padx=(8,0)); r+=1

        ttk.Separator(f,orient="horizontal").grid(row=r,column=0,columnspan=2,sticky="ew",pady=(8,2)); r+=1
        ttk.Label(f,text="Safety Limits:",font=_font(10,bold=True)).grid(row=r,column=0,columnspan=2,sticky="w"); r+=1
        self._slo_var=tk.DoubleVar(value=self._settings.get("safety_lo",-100))
        self._shi_var=tk.DoubleVar(value=self._settings.get("safety_hi",400))
        ttk.Label(f,text="Min (°C):").grid(row=r,column=0,sticky="w",pady=1)
        ttk.Spinbox(f,from_=-200,to=1300,increment=10,textvariable=self._slo_var,width=8).grid(row=r,column=1,sticky="w",pady=1,padx=(8,0)); r+=1
        ttk.Label(f,text="Max (°C):").grid(row=r,column=0,sticky="w",pady=1)
        ttk.Spinbox(f,from_=-200,to=1300,increment=10,textvariable=self._shi_var,width=8).grid(row=r,column=1,sticky="w",pady=1,padx=(8,0)); r+=1

        bf=ttk.Frame(f); bf.grid(row=r,column=0,columnspan=2,pady=(12,0),sticky="e")
        ttk.Button(bf,text="Cancel",command=self._win.destroy).pack(side="right",padx=(6,0))
        ttk.Button(bf,text="OK",command=self._on_ok).pack(side="right")
        self._win.bind("<Return>",lambda _:self._on_ok())
        self._win.bind("<Escape>",lambda _:self._win.destroy())
        self._center(parent)

    def _center(self,parent):
        self._win.update_idletasks()
        pw,ph=parent.winfo_width(),parent.winfo_height()
        px,py=parent.winfo_x(),parent.winfo_y()
        ww,wh=self._win.winfo_width(),self._win.winfo_height()
        self._win.geometry(f"+{px+(pw-ww)//2}+{py+(ph-wh)//2}")

    def _on_ok(self):
        try:
            slo=float(self._slo_var.get()); shi=float(self._shi_var.get())
            if slo>=shi: messagebox.showerror("Invalid","Min safety must be < Max.",parent=self._win); return
            self._result={"port":self._port_var.get().strip(),"slave":int(self._slave_var.get()),
                          "baudrate":int(self._baud_var.get()),"parity":self._parity_var.get().strip().upper(),
                          "stopbits":int(self._stopbits_var.get()),"timeout":float(self._timeout_var.get()),
                          "safety_lo":slo,"safety_hi":shi}
        except (ValueError,tk.TclError) as exc:
            messagebox.showerror("Invalid Input",str(exc),parent=self._win); return
        self._win.destroy()

    @property
    def result(self):
        return self._result

# ---------------------------------------------------------------------------
# Preset Config Dialog
# ---------------------------------------------------------------------------

class PresetDialog:
    def __init__(self, parent, presets):
        self._presets = [dict(p) for p in presets]
        self._result = None
        self._win = tk.Toplevel(parent)
        self._win.title("Configure Presets")
        self._win.resizable(False,False)
        self._win.transient(parent)
        self._win.grab_set()
        f=ttk.Frame(self._win,padding=10); f.pack(fill="both",expand=True)

        lf=ttk.Frame(f); lf.pack(fill="both",expand=True,pady=(0,6))
        self._lb=tk.Listbox(lf,width=36,height=8,font=FONT_SMALL)
        sb=ttk.Scrollbar(lf,orient="vertical",command=self._lb.yview)
        self._lb.configure(yscrollcommand=sb.set)
        self._lb.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        self._refresh()

        ef=ttk.Frame(f); ef.pack(fill="x",pady=(0,6))
        ttk.Label(ef,text="Name:").grid(row=0,column=0,sticky="w")
        self._name_var=tk.StringVar()
        ttk.Entry(ef,textvariable=self._name_var,width=14).grid(row=0,column=1,padx=4)
        ttk.Label(ef,text="Temp (°C):").grid(row=0,column=2,sticky="w",padx=(8,0))
        self._temp_var=tk.StringVar(value="25.0")
        ttk.Entry(ef,textvariable=self._temp_var,width=7).grid(row=0,column=3,padx=4)

        bf=ttk.Frame(f); bf.pack(fill="x")
        ttk.Button(bf,text="Add/Update",command=self._on_add).pack(side="left",padx=(0,6))
        ttk.Button(bf,text="Delete",command=self._on_del).pack(side="left")
        ttk.Button(bf,text="OK",command=self._on_ok).pack(side="right",padx=(6,0))
        ttk.Button(bf,text="Cancel",command=self._win.destroy).pack(side="right")
        self._win.bind("<Escape>",lambda _:self._win.destroy())
        self._center(parent)

    def _center(self,parent):
        self._win.update_idletasks()
        pw,ph=parent.winfo_width(),parent.winfo_height()
        px,py=parent.winfo_x(),parent.winfo_y()
        ww,wh=self._win.winfo_width(),self._win.winfo_height()
        self._win.geometry(f"+{px+(pw-ww)//2}+{py+(ph-wh)//2}")

    def _refresh(self):
        self._lb.delete(0,"end")
        for p in self._presets: self._lb.insert("end",f"{p['name']:14s}  {p['temp']:.1f} °C")

    def _on_add(self):
        name=self._name_var.get().strip()
        try: temp=round(float(self._temp_var.get()),1)
        except ValueError: messagebox.showerror("Invalid","Temperature must be a number.",parent=self._win); return
        if not name: messagebox.showerror("Invalid","Name cannot be empty.",parent=self._win); return
        for p in self._presets:
            if p["name"]==name: p["temp"]=temp; self._refresh(); return
        self._presets.append({"name":name,"temp":temp}); self._refresh()

    def _on_del(self):
        sel=self._lb.curselection()
        if sel: del self._presets[sel[0]]; self._refresh()

    def _on_ok(self): self._result=self._presets; self._win.destroy()

    @property
    def result(self): return self._result

# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class TempControllerApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.geometry("560x420")
        self.root.configure(bg=COLOR_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._settings = load_settings()
        self._controller: Optional[YudianController] = None
        self._devices: List[Dict[str,Any]] = []
        self._polling = False
        self._poll_thread = None
        self._stop_event = threading.Event()
        self._data_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

        self._build_ui()
        self.root.after(300, self._auto_detect)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # -- Status ----------------------------------------------------
        sf = ttk.LabelFrame(main, text="Status", padding=4)
        sf.pack(fill="x", pady=(0, 4))
        self._status_label = ttk.Label(sf, text="● Disconnected", font=FONT_STATUS, foreground=COLOR_ERROR)
        self._status_label.pack(side="left")
        self._update_label = ttk.Label(sf, text="Last update: --", font=FONT_STATUS, foreground=COLOR_GRAY)
        self._update_label.pack(side="right")

        # -- Temperature ------------------------------------------------
        tf = ttk.LabelFrame(main, text="Temperature", padding=6)
        tf.pack(fill="x", pady=(0, 4))
        self._pv_label = ttk.Label(tf, text="--.- °C", font=FONT_LARGE, foreground=COLOR_GRAY)
        self._pv_label.pack()

        # Target row
        tr = ttk.Frame(tf)
        tr.pack(fill="x", pady=(6, 2))
        ttk.Label(tr, text="Target:", font=FONT_MEDIUM).pack(side="left", padx=(0, 6))
        self._sv_entry = ttk.Entry(tr, font=FONT_MEDIUM, width=8, justify="center")
        self._sv_entry.pack(side="left")
        self._sv_entry.insert(0, "--.-")
        self._sv_entry.configure(state="disabled")
        self._sv_entry.bind("<Return>", lambda _e: self._on_apply())
        ttk.Label(tr, text="°C", font=FONT_MEDIUM).pack(side="left", padx=(4, 10))
        self._apply_btn = ttk.Button(tr, text="Apply", command=self._on_apply, state="disabled")
        self._apply_btn.pack(side="left")

        # Preset row
        pr = ttk.Frame(tf)
        pr.pack(fill="x", pady=(2, 0))
        ttk.Label(pr, text="Preset:", font=FONT_SMALL).pack(side="left", padx=(0, 6))
        self._preset_var = tk.StringVar()
        self._preset_combo = ttk.Combobox(pr, textvariable=self._preset_var, state="readonly", width=28)
        self._preset_combo.pack(side="left", fill="x", expand=True)
        self._preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        ttk.Button(pr, text="⚙", width=3, command=self._on_configure_presets).pack(side="left", padx=(4, 0))
        self._refresh_preset_list()

        # Error label
        self._sv_error_label = ttk.Label(tf, text="", font=FONT_STATUS, foreground=COLOR_ERROR)
        self._sv_error_label.pack()

        # -- Power Output -----------------------------------------------
        pf = ttk.LabelFrame(main, text="Power Output (%)", padding=4)
        pf.pack(fill="x", pady=(0, 4))
        self._mv_label = ttk.Label(pf, text="--.- %", font=FONT_MEDIUM, foreground=COLOR_GRAY)
        self._mv_label.pack()

        # -- Device selector --------------------------------------------
        df = ttk.Frame(main)
        df.pack(fill="x", pady=(0, 4))
        ttk.Label(df, text="Device:", font=FONT_SMALL).pack(side="left", padx=(0, 6))
        self._dev_var = tk.StringVar()
        self._dev_combo = ttk.Combobox(df, textvariable=self._dev_var, state="readonly", width=32)
        self._dev_combo.pack(side="left", fill="x", expand=True)
        self._scan_btn = ttk.Button(df, text="↻ Refresh", command=self._on_refresh)
        self._scan_btn.pack(side="left", padx=(6, 0))

        # -- Action buttons --------------------------------------------
        bf = ttk.Frame(main)
        bf.pack(fill="x", pady=(2, 0))
        self._connect_btn = ttk.Button(bf, text="Connect", command=self._on_connect)
        self._connect_btn.pack(side="left", padx=(0, 6))
        self._disconnect_btn = ttk.Button(bf, text="Disconnect", command=self._on_disconnect, state="disabled")
        self._disconnect_btn.pack(side="left", padx=(0, 6))
        self._settings_btn = ttk.Button(bf, text="⚙ Settings", command=self._on_settings)
        self._settings_btn.pack(side="left")

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _refresh_preset_list(self):
        presets = self._settings.get("presets", [])
        self._preset_combo["values"] = ["  -- Custom --"] + [
            f"{p['name']} ({p['temp']:.1f} °C)" for p in presets
        ]
        self._preset_combo.current(0)

    def _on_preset_selected(self, _event=None):
        idx = self._preset_combo.current()
        if idx <= 0:  # "Custom" or nothing selected
            return
        presets = self._settings.get("presets", [])
        pi = idx - 1  # offset by the "Custom" entry
        if 0 <= pi < len(presets):
            t = presets[pi]["temp"]
            self._sv_entry.configure(state="normal")
            self._sv_entry.delete(0, "end")
            self._sv_entry.insert(0, f"{t:.1f}")
            if self._controller and self._controller.is_connected:
                self._on_apply()

    def _on_configure_presets(self):
        dlg = PresetDialog(self.root, self._settings.get("presets", []))
        self.root.wait_window(dlg._win)
        if dlg.result is not None:
            self._settings["presets"] = dlg.result
            save_settings(self._settings)
            self._refresh_preset_list()

    # ------------------------------------------------------------------
    # Auto-detect
    # ------------------------------------------------------------------

    def _auto_detect(self):
        """Try saved device first, then full scan."""
        last_port = self._settings.get("last_port", "")
        if last_port:
            self._set_status(f"● Trying {last_port}…", COLOR_WARN)
            self._set_ui_state("scanning")
            _log.info("Quick-connect: trying %s", last_port)
            threading.Thread(
                target=self._try_saved_device,
                args=(last_port, int(self._settings.get("last_slave", 1)),
                      int(self._settings.get("last_baud", 9600))),
                daemon=True, name="quick_connect",
            ).start()
        else:
            self._start_full_scan()

    def _try_saved_device(self, port, slave, baud):
        try:
            devs = YudianController.scan_devices(
                ports=[port], slaves=[slave], baudrates=[baud],
                timeout=SCAN_TIMEOUT,
            )
            if devs:
                _log.info("Saved device found at %s", port)
                self._devices = devs
                self.root.after(0, self._on_scan_done)
                return
        except Exception:
            _log.debug("Quick probe failed, falling back to full scan")
        _log.info("Saved device not found, starting full scan")
        self.root.after(0, self._start_full_scan)

    def _start_full_scan(self):
        self._set_status("● Scanning all ports…", COLOR_WARN)
        self._set_ui_state("scanning")
        def _scan():
            try:
                _log.info("Full scan starting")
                self._devices = YudianController.scan_devices(
                    ports=None, slaves=None, baudrates=None,
                    timeout=SCAN_TIMEOUT,
                )
                _log.info("Full scan done: %d device(s)", len(self._devices))
            except Exception as exc:
                _log.error("Scan crashed: %s\n%s", exc, traceback.format_exc())
                self._devices = []
                self.root.after(0, lambda: self._on_scan_error(str(exc)))
                return
            self.root.after(0, self._on_scan_done)
        threading.Thread(target=_scan, daemon=True, name="scanner").start()

    def _on_scan_done(self):
        self._populate_device_list()
        n = len(self._devices)
        if n == 1:
            self._set_status("● Device found — connecting…", COLOR_WARN)
            self._do_connect(self._devices[0])
        elif n >= 2:
            self._set_status(f"● {n} devices found — select and click Connect", COLOR_WARN)
            self._set_ui_state("disconnected")
        else:
            self._set_status("● No device detected — check connections", COLOR_ERROR)
            self._set_ui_state("disconnected")

    def _on_scan_error(self, msg):
        self._set_status(f"● Scan error: {msg}", COLOR_ERROR)
        self._set_ui_state("disconnected")

    def _populate_device_list(self):
        port_descs = {}
        try:
            for p in serial.tools.list_ports.comports():
                port_descs[p.device] = p.description or p.device
        except Exception:
            pass
        options = []
        for d in self._devices:
            desc = port_descs.get(d["port"], d["port"])
            options.append(f"{desc} ({d['port']})  [slave={d['slave']}]")
        self._dev_combo["values"] = options
        if options:
            self._dev_combo.current(0)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _on_connect(self):
        idx = self._dev_combo.current()
        if 0 <= idx < len(self._devices):
            self._do_connect(self._devices[idx])
        else:
            self._do_connect({
                "port": self._settings["port"],
                "slave": self._settings["slave"],
                "baudrate": self._settings["baudrate"],
            })

    def _do_connect(self, device_info):
        self._set_status(f"● Connecting to {device_info['port']}…", COLOR_WARN)
        self._set_ui_state("connecting")
        def _connect():
            try:
                ctrl = YudianController(
                    port=device_info["port"],
                    slave_address=int(device_info["slave"]),
                    baudrate=int(device_info.get("baudrate", self._settings["baudrate"])),
                    parity=self._settings.get("parity", "N"),
                    stopbits=int(self._settings.get("stopbits", 1)),
                    timeout=float(self._settings.get("timeout", 0.5)),
                )
                ctrl.connect()
            except YudianConnectionError as exc:
                self.root.after(0, lambda: self._on_connect_error(str(exc)))
                return
            except Exception as exc:
                self.root.after(0, lambda: self._on_connect_error(f"Unexpected error: {exc}"))
                return
            self._controller = ctrl
            self._settings["port"] = device_info["port"]
            self._settings["slave"] = int(device_info["slave"])
            self._settings["baudrate"] = int(device_info.get("baudrate", self._settings["baudrate"]))
            self._settings["last_port"] = device_info["port"]
            self._settings["last_slave"] = int(device_info["slave"])
            self._settings["last_baud"] = int(device_info.get("baudrate", 9600))
            save_settings(self._settings)
            self.root.after(0, self._on_connect_ok)
        threading.Thread(target=_connect, daemon=True, name="connector").start()

    def _on_connect_ok(self):
        _log.info("Connected successfully to %s", self._settings.get("port"))
        self._set_status("● Connected", COLOR_OK)
        self._set_ui_state("connected")
        self._sv_error_label.configure(text="")
        self._sv_safety_checked = False
        self._start_polling()

    def _on_connect_error(self, msg):
        _log.error("Connection failed: %s", msg)
        self._set_status(f"● Connection failed: {msg}", COLOR_ERROR)
        self._set_ui_state("disconnected")
        messagebox.showerror("Connection Error", msg, parent=self.root)

    def _on_disconnect(self):
        self._stop_polling()
        if self._controller:
            try: self._controller.disconnect()
            except Exception: pass
            self._controller = None
        self._pv_label.configure(text="--.- °C", foreground=COLOR_GRAY)
        self._mv_label.configure(text="--.- %", foreground=COLOR_GRAY)
        self._set_sv_display("--.-")
        self._update_label.configure(text="Last update: --")
        self._set_status("● Disconnected", COLOR_ERROR)
        self._set_ui_state("disconnected")

    def _set_sv_display(self, text):
        self._sv_entry.configure(state="normal")
        self._sv_entry.delete(0, "end")
        self._sv_entry.insert(0, text)
        self._sv_entry.configure(state="disabled")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _start_polling(self):
        if self._polling: return
        self._polling = True
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="poller")
        self._poll_thread.start()
        self.root.after(QUEUE_CHECK_MS, self._drain_queue)

    def _stop_polling(self):
        self._polling = False
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

    def _poll_loop(self):
        while not self._stop_event.is_set():
            if self._controller is None or not self._controller.is_connected:
                break
            try:
                data = self._controller.read_all()
                data["_ts"] = datetime.now().strftime("%H:%M:%S")
                try: self._data_queue.put_nowait(data)
                except queue.Full:
                    try: self._data_queue.get_nowait(); self._data_queue.put_nowait(data)
                    except (queue.Empty, queue.Full): pass
            except YudianCommunicationError as exc:
                self._data_queue.put({"error": str(exc)})
            except YudianConnectionError as exc:
                self._data_queue.put({"disconnected": str(exc)})
            self._stop_event.wait(POLL_INTERVAL_MS / 1000.0)

    def _drain_queue(self):
        latest = None
        while True:
            try: latest = self._data_queue.get_nowait()
            except queue.Empty: break
        if latest is not None:
            if "error" in latest:
                self._set_status(f"● Comm error: {latest['error']}", COLOR_ERROR)
            elif "disconnected" in latest:
                self._set_status(f"● Disconnected: {latest['disconnected']}", COLOR_ERROR)
            else:
                self._update_display(latest)
                self._set_status("● Connected", COLOR_OK)
        if self._polling:
            self.root.after(QUEUE_CHECK_MS, self._drain_queue)

    def _update_display(self, data):
        pv, mv, sv, ts = data.get("pv"), data.get("mv"), data.get("sv"), data.get("_ts", "--")
        if pv is not None:
            self._pv_label.configure(text=f"{pv:.1f} °C", foreground=COLOR_BLUE)
        if mv is not None:
            self._mv_label.configure(text=f"{mv:.1f} %", foreground=COLOR_BLUE)
        if sv is not None:
            if not getattr(self, "_sv_safety_checked", True):
                self._sv_safety_checked = True
                lo = float(self._settings.get("safety_lo", -100))
                hi = float(self._settings.get("safety_hi", 400))
                if sv < lo or sv > hi:
                    orig = sv; sv = max(lo, min(hi, sv))
                    self._sv_error_label.configure(
                        text=f"⚠ SV {orig:.1f} °C clamped to {sv:.0f} °C", foreground=COLOR_WARN)
                    threading.Thread(
                        target=lambda: self._controller.set_sv(sv),
                        daemon=True, name="safety_clamp",
                    ).start()
            # Clear preset selection if SV no longer matches it
            pi = self._preset_combo.current() - 1
            if pi >= 0:
                presets = self._settings.get("presets", [])
                if pi < len(presets) and presets[pi]["temp"] != sv:
                    self._preset_combo.current(0)

            # Always update SV display
            try:
                current = self._sv_entry.get().strip()
                try: current_val = float(current)
                except ValueError: current_val = None
                if current_val != sv:
                    self._sv_entry.configure(state="normal")
                    self._sv_entry.delete(0, "end")
                    self._sv_entry.insert(0, f"{sv:.1f}")
            except Exception:
                self._sv_entry.configure(state="normal")
                self._sv_entry.delete(0, "end")
                self._sv_entry.insert(0, f"{sv:.1f}")
        self._update_label.configure(text=f"Last update: {ts}")

    # ------------------------------------------------------------------
    # Setpoint
    # ------------------------------------------------------------------

    def _on_apply(self):
        if self._controller is None or not self._controller.is_connected:
            return
        raw = self._sv_entry.get().strip()
        try: value = float(raw)
        except ValueError: self._sv_error_label.configure(text=f"Invalid: {raw!r}"); return
        lo = float(self._settings.get("safety_lo", -100))
        hi = float(self._settings.get("safety_hi", 400))
        if value < lo: self._sv_error_label.configure(text=f"Below safety min ({lo:.0f} °C)"); return
        if value > hi: self._sv_error_label.configure(text=f"Above safety max ({hi:.0f} °C)"); return

        def _write():
            try:
                self._controller.set_sv(value)
                self.root.after(0, lambda: [
                    self._sv_error_label.configure(text=""),
                    self._apply_btn.configure(text="✓ Applied"),
                    self.root.after(1200, lambda: self._apply_btn.configure(text="Apply")),
                    self._preset_combo.current(0),  # clear to "Custom"
                ])
            except ValueError as exc:
                self.root.after(0, lambda: self._sv_error_label.configure(text=str(exc)))
            except YudianCommunicationError as exc:
                self.root.after(0, lambda: [
                    self._sv_error_label.configure(text=f"Write failed: {exc}"),
                    self._set_status(f"● Comm error: {exc}", COLOR_ERROR),
                ])
            except YudianConnectionError:
                self.root.after(0, lambda: self._on_disconnect())
        threading.Thread(target=_write, daemon=True, name="sv_write").start()

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def _set_status(self, text, color):
        self._status_label.configure(text=text, foreground=color)

    def _set_ui_state(self, state):
        """Enable/disable controls based on connection state."""
        if state == "scanning":
            self._connect_btn.configure(state="disabled")
            self._disconnect_btn.configure(state="disabled")
            self._apply_btn.configure(state="disabled")
            self._scan_btn.configure(state="disabled")
            self._settings_btn.configure(state="disabled")
            self._sv_entry.configure(state="disabled")
            self._dev_combo.configure(state="disabled")
        elif state == "connecting":
            self._connect_btn.configure(state="disabled")
            self._disconnect_btn.configure(state="disabled")
            self._apply_btn.configure(state="disabled")
            self._scan_btn.configure(state="disabled")
            self._settings_btn.configure(state="disabled")
            self._sv_entry.configure(state="disabled")
            self._dev_combo.configure(state="disabled")
        elif state == "connected":
            self._connect_btn.configure(state="disabled")
            self._disconnect_btn.configure(state="normal")
            self._apply_btn.configure(state="normal")
            self._scan_btn.configure(state="disabled")
            self._settings_btn.configure(state="disabled")
            self._sv_entry.configure(state="normal")
            self._dev_combo.configure(state="disabled")
        elif state == "disconnected":
            self._connect_btn.configure(state="normal")
            self._disconnect_btn.configure(state="disabled")
            self._apply_btn.configure(state="disabled")
            self._scan_btn.configure(state="normal")
            self._settings_btn.configure(state="normal")
            self._set_sv_display("--.-")
            self._dev_combo.configure(state="readonly")

    # ------------------------------------------------------------------
    # Settings / Refresh / Close
    # ------------------------------------------------------------------

    def _on_settings(self):
        _log.info("Opening settings dialog")
        try:
            dlg = SettingsDialog(self.root, self._settings)
            self.root.wait_window(dlg._win)
            if dlg.result is not None:
                self._settings.update(dlg.result)
                save_settings(self._settings)
                _log.info("Settings saved")
        except Exception:
            _log.error("Settings dialog crashed:\n%s", traceback.format_exc())
            messagebox.showerror("Error", "Failed to open settings. See debug.log.")

    def _on_refresh(self):
        self._devices.clear()
        self._dev_combo["values"] = []
        self._dev_var.set("")
        self._auto_detect()

    def _on_close(self):
        self._stop_polling()
        if self._controller:
            try: self._controller.disconnect()
            except Exception: pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    TempControllerApp().run()

if __name__ == "__main__":
    main()
