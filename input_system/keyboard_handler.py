"""
Keyboard Handler
=================

Tracks keyboard key press/release state via tkinter ``bind_all()``
events.  All state is stored in a ``threading.Lock``-protected dict
shared with the Input Loop thread.

Key events are captured by the Main (GUI) thread; the Input Loop
reads the shared dict each tick to compute press durations.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Set

import tkinter as tk

from input_system.input_mapping import KEYBOARD_MAP, SPEED_MODIFIER_KEYS


class KeyboardHandler:
    """Tracks keyboard state for the input system.

    Uses ``bind_all`` on a tkinter root widget to capture key events.
    The state dict ``{keysym: press_time_or_0}`` is protected by a
    lock for thread-safe access from the Input Loop thread.
    """

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._lock = threading.Lock()

        # Shared state: keysym → timestamp of press (or 0 if not pressed)
        self._key_state: Dict[str, float] = {}

        # Set of tracked keys (only those we care about)
        self._tracked_keys: Set[str] = set(KEYBOARD_MAP.keys()) | SPEED_MODIFIER_KEYS

        # Callbacks for global actions (Escape = STOP ALL)
        self._global_callbacks: Dict[str, Callable] = {}
        self._global_held: Set[str] = set()   # global keys currently held
        self._focus_out_gen = 0               # generation for deferred FocusOut check

        self._bind()

    @staticmethod
    def _normalize_keysym(keysym: str) -> str:
        """Single-letter keysyms arrive as 'w' normally but as 'W' once
        Shift is held (auto-repeat/release) — normalize so press and
        release update the same state entry (fixes stuck keys and broken
        short-press detection)."""
        if len(keysym) == 1 and keysym.isalpha():
            return keysym.lower()
        return keysym

    # ------------------------------------------------------------------
    # Binding
    # ------------------------------------------------------------------

    def _bind(self) -> None:
        """Register key event handlers on the root window."""
        self._root.bind_all("<KeyPress>", self._on_key_press)
        self._root.bind_all("<KeyRelease>", self._on_key_release)
        # Also track focus-out to clear stuck keys
        self._root.bind_all("<FocusOut>", self._on_focus_out)

    def unbind(self) -> None:
        """Remove key event handlers."""
        self._root.unbind_all("<KeyPress>")
        self._root.unbind_all("<KeyRelease>")
        self._root.unbind_all("<FocusOut>")

    # ------------------------------------------------------------------
    # Event handlers (run on Main thread)
    # ------------------------------------------------------------------

    def _on_key_press(self, event: tk.Event) -> None:
        """Record a key press."""
        keysym = self._normalize_keysym(event.keysym)
        # Global callbacks FIRST — Escape is not in _tracked_keys, and the
        # old code returned before this lookup so Escape never fired.
        cb = self._global_callbacks.get(keysym)
        if cb is not None:
            with self._lock:
                was_held = keysym in self._global_held
                self._global_held.add(keysym)
            if not was_held:  # fire once per press, not on auto-repeat
                cb()
        if keysym not in self._tracked_keys:
            return
        with self._lock:
            if self._key_state.get(keysym, 0) == 0:
                self._key_state[keysym] = time.perf_counter()

    def _on_key_release(self, event: tk.Event) -> None:
        """Record a key release."""
        keysym = self._normalize_keysym(event.keysym)
        if keysym in self._global_callbacks:
            with self._lock:
                self._global_held.discard(keysym)
        if keysym not in self._tracked_keys:
            return
        with self._lock:
            self._key_state[keysym] = 0

    def _on_focus_out(self, event: tk.Event) -> None:
        """Deferred focus check: only clear keys when the WHOLE window
        lost focus (not when focus moved between widgets, e.g. clicking
        an on-screen button while holding a movement key)."""
        self._focus_out_gen += 1
        self._root.after(50, lambda g=self._focus_out_gen: self._focus_out_recheck(g))

    def _focus_out_recheck(self, gen: int) -> None:
        if gen != self._focus_out_gen:
            return
        try:
            if self._root.focus_get() is not None:
                return  # focus moved to a child widget — keys stay held
        except Exception:
            pass
        with self._lock:  # whole window lost focus — clear everything
            for key in self._tracked_keys:
                self._key_state[key] = 0
            self._global_held.clear()

    # ------------------------------------------------------------------
    # Global action callbacks
    # ------------------------------------------------------------------

    def bind_global(self, keysym: str, callback: Callable) -> None:
        """Register a callback for a global key (e.g. Escape → STOP ALL)."""
        self._global_callbacks[keysym] = callback

    # ------------------------------------------------------------------
    # State reader (called from Input Loop thread)
    # ------------------------------------------------------------------

    def get_state(self) -> Dict[str, float]:
        """Return a snapshot of current key states.

        Returns
        -------
        dict
            ``{keysym: press_timestamp}`` — 0 means not pressed.
            The timestamp is ``time.perf_counter()`` from when the
            key was first pressed.
        """
        with self._lock:
            return dict(self._key_state)

