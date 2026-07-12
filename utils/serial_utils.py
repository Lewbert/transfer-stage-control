"""
Serial port enumeration utilities.

Provides cached COM port listing with descriptions, avoiding
repeated calls to serial.tools.list_ports (which can hang with
certain buggy COM port drivers).
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

import serial.tools.list_ports


# Cache: {port_name: description}
_port_cache: Dict[str, str] = {}
_cache_lock = threading.Lock()


def list_available_ports(refresh: bool = False) -> List[str]:
    """Return a list of available COM port names.

    Results are cached after the first call.  Pass ``refresh=True``
    to force a re-scan.

    Returns
    -------
    list of str
        Port names, e.g. ``["COM3", "COM4"]``.
    """
    global _port_cache
    with _cache_lock:
        if refresh or not _port_cache:
            _port_cache.clear()
            try:
                for port in serial.tools.list_ports.comports():
                    _port_cache[port.device] = port.description or port.device
            except Exception:
                pass
        return sorted(_port_cache.keys())


def get_port_description(port_name: str) -> str:
    """Return a human-readable description for *port_name*.

    If the port was not seen during enumeration, returns *port_name* itself.
    """
    with _cache_lock:
        if not _port_cache:
            list_available_ports()
        return _port_cache.get(port_name, port_name)


def find_port_by_description(hint: str) -> Optional[str]:
    """Find a COM port whose description contains *hint* (case-insensitive).

    Returns the first match, or ``None``.
    """
    with _cache_lock:
        if not _port_cache:
            list_available_ports()
        for port, desc in _port_cache.items():
            if hint.lower() in desc.lower():
                return port
    return None
