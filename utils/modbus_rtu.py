"""
MODBUS-RTU frame construction and parsing utilities.

Shared by the Zolix ZC300 and Yudian AI-828 drivers.
Provides CRC-16, read/write frame builders, and response parsers.

Supports function codes:
- 0x03 (Read Holding Registers)
- 0x04 (Read Input Registers)
- 0x06 (Write Single Register)
- 0x10 (Write Multiple Registers)
"""

from __future__ import annotations

import struct

# ---------------------------------------------------------------------------
# MODBUS CRC-16
# ---------------------------------------------------------------------------


def crc16(data: bytes) -> int:
    """Compute MODBUS CRC-16 (polynomial 0xA001, initial 0xFFFF).

    Returns
    -------
    int
        16-bit CRC value (0–65535).
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            lsb = crc & 1
            crc >>= 1
            if lsb:
                crc ^= 0xA001
    return crc


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def build_read_frame(slave_addr: int, function_code: int, register: int, count: int = 1) -> bytes:
    """Build a MODBUS RTU read frame.

    Parameters
    ----------
    slave_addr : int
        Slave address (1–247).
    function_code : int
        0x03 for holding registers, 0x04 for input registers.
    register : int
        **1-based** register number (e.g. 30050).  The PDU address
        (register - 1) is computed internally.
    count : int
        Number of consecutive registers to read (default 1).

    Returns
    -------
    bytes
        Complete MODBUS RTU frame (8 bytes for count=1).
    """
    pdu_addr = register - 1  # MODBUS PDU uses 0-based addressing
    pdu = struct.pack(">BBHH", slave_addr, function_code, pdu_addr, count)
    crc = crc16(pdu)
    return pdu + struct.pack("<H", crc)


def build_write_single_frame(slave_addr: int, register: int, value: int) -> bytes:
    """Build a MODBUS RTU Write Single Register (0x06) frame.

    Parameters
    ----------
    slave_addr : int
        Slave address.
    register : int
        **1-based** register number.
    value : int
        Unsigned 16-bit value to write.

    Returns
    -------
    bytes
        8-byte MODBUS RTU frame.
    """
    pdu_addr = register - 1
    pdu = struct.pack(">BBHH", slave_addr, 0x06, pdu_addr, value & 0xFFFF)
    crc = crc16(pdu)
    return pdu + struct.pack("<H", crc)


def build_write_multiple_frame(slave_addr: int, register: int, values: list[int]) -> bytes:
    """Build a MODBUS RTU Write Multiple Registers (0x10) frame.

    Parameters
    ----------
    slave_addr : int
        Slave address.
    register : int
        **1-based** starting register number.
    values : list of int
        List of 16-bit values to write.  Each value is a register.

    Returns
    -------
    bytes
        MODBUS RTU frame.
    """
    pdu_addr = register - 1
    count = len(values)
    byte_count = count * 2
    pdu = struct.pack(
        ">BBHHB",
        slave_addr, 0x10, pdu_addr, count, byte_count,
    )
    for v in values:
        pdu += struct.pack(">H", v & 0xFFFF)
    crc = crc16(pdu)
    return pdu + struct.pack("<H", crc)


def build_write_multiple_floats(slave_addr: int, register: int, floats: list[float]) -> bytes:
    """Build a Write Multiple Registers frame from float values.

    Each float is packed as 2 consecutive 16-bit registers (big-endian IEEE 754).
    The ZC300 uses this format for position, speed, and distance values.

    Parameters
    ----------
    slave_addr : int
        Slave address.
    register : int
        **1-based** starting register number.
    floats : list of float
        Float values to encode.  Each float consumes 2 registers.

    Returns
    -------
    bytes
        MODBUS RTU frame.
    """
    pdu_addr = register - 1
    count = len(floats) * 2
    byte_count = count * 2
    pdu = struct.pack(
        ">BBHHB",
        slave_addr, 0x10, pdu_addr, count, byte_count,
    )
    for f in floats:
        # Pack as big-endian 32-bit IEEE 754, then split into two 16-bit values
        raw = struct.pack(">f", f)
        hi, lo = struct.unpack(">HH", raw)
        pdu += struct.pack(">HH", hi, lo)
    crc = crc16(pdu)
    return pdu + struct.pack("<H", crc)


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------



def parse_multi_read_response(response: bytes, expected_function: int) -> list[int]:
    """Parse a multi-register MODBUS read response.

    Parameters
    ----------
    response : bytes
        Raw response from the device.
    expected_function : int
        Expected function code.

    Returns
    -------
    list of int
        List of signed 16-bit register values.

    Raises
    ------
    ValueError
        On malformed response or MODBUS exception.
    """
    if len(response) < 5:
        raise ValueError(f"Short response ({len(response)} bytes)")

    exc_code = expected_function | 0x80
    if response[1] == exc_code:
        code = response[2] if len(response) > 2 else 0
        raise ValueError(f"MODBUS exception {code}")

    if response[1] != expected_function:
        raise ValueError(
            f"Unexpected function code 0x{response[1]:02X}"
        )

    # Verify CRC
    data = response[:-2]
    received_crc = struct.unpack("<H", response[-2:])[0]
    if crc16(data) != received_crc:
        raise ValueError("CRC mismatch")

    byte_count = response[2]
    expected = 3 + byte_count + 2  # slave + fc + bc + data + crc
    if len(response) < expected:
        raise ValueError(f"Response too short: got {len(response)}, expected {expected}")

    results = []
    for i in range(3, 3 + byte_count, 2):
        raw = (response[i] << 8) | response[i + 1]
        if raw > 32767:
            raw -= 65536
        results.append(raw)
    return results


def parse_float_pair(reg_hi: int, reg_lo: int) -> float:
    """Convert two 16-bit MODBUS registers to a float.

    The ZC300 uses big-endian IEEE 754: register N=high word, register N+1=low word.

    Parameters
    ----------
    reg_hi : int
        High-order register value (unsigned 16-bit).
    reg_lo : int
        Low-order register value (unsigned 16-bit).

    Returns
    -------
    float
        Decoded float value.
    """
    raw = struct.pack(">HH", reg_hi & 0xFFFF, reg_lo & 0xFFFF)
    return struct.unpack(">f", raw)[0]


