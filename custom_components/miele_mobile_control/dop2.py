"""DOP2 binary leaf codec for the Miele app cloud tunnel.

Pure Python, no Home Assistant imports. Layouts follow the jplot/koti
miele-sdk (GlobalPsSelect 2/1577, GlobalPsContext 2/1574) and round-trip
byte for byte against captures of a WSI863 (Wi-Fi module EK057, fw 08.37).

A leaf is (header, fields):
  header: {"unit": int, "attr": int, "idx1": int, "idx2": int}
  fields: {field_id: [type_byte, value]} — a dict keeps wire order
          (Python dicts preserve insertion order).
Field values are ints, floats, bools, bytes, or (nested) field dicts/lists.
"""

from __future__ import annotations

import struct
from typing import Any

# Fixed-size scalar types: type byte -> (size, struct format)
FIXED: dict[int, tuple[int, str]] = {
    0x01: (1, ">?"),
    0x02: (1, ">B"),
    0x03: (1, ">b"),
    0x04: (1, ">B"),
    0x05: (2, ">H"),
    0x06: (2, ">h"),
    0x07: (2, ">H"),
    0x08: (4, ">I"),
    0x09: (4, ">i"),
    0x0A: (4, ">I"),
    0x0B: (8, ">Q"),
    0x0C: (8, ">q"),
    0x0D: (8, ">Q"),
    0x0E: (4, ">f"),
    0x0F: (8, ">d"),
}
# Array types 0x11..0x1F hold count + N elements of the base type 0x01..0x0F
ARRAY: dict[int, int] = {0x10 + k: k for k in range(1, 16)}
# Homogeneous struct array (0x21): count + N * (field-count + fields)
TYPE_STRUCT_ARRAY = 0x21


class Dop2DecodeError(ValueError):
    """A leaf does not decode."""


def _value(buf: bytes, off: int, t: int) -> tuple[Any, int]:
    if t in FIXED:
        size, fmt = FIXED[t]
        return struct.unpack_from(fmt, buf, off)[0], off + size
    if t == 0x10:  # struct: field count + fields
        (count,) = struct.unpack_from(">H", buf, off)
        return _fields(buf, off + 2, count)
    if t in ARRAY:
        (count,) = struct.unpack_from(">H", buf, off)
        off += 2
        out = []
        for _ in range(count):
            v, off = _value(buf, off, ARRAY[t])
            out.append(v)
        return out, off
    if t == 0x20:  # opaque bytes
        (n,) = struct.unpack_from(">H", buf, off)
        return bytes(buf[off + 2 : off + 2 + n]), off + 2 + n
    if t == TYPE_STRUCT_ARRAY:
        (count,) = struct.unpack_from(">H", buf, off)
        off += 2
        out = []
        for _ in range(count):
            (fc,) = struct.unpack_from(">H", buf, off)
            v, off = _fields(buf, off + 2, fc)
            out.append(v)
        return out, off
    raise Dop2DecodeError(f"unknown DOP2 type 0x{t:02x} at offset {off}")


def _fields(buf: bytes, off: int, count: int) -> tuple[dict[int, list[Any]], int]:
    out: dict[int, list[Any]] = {}
    for _ in range(count):
        idx, t = struct.unpack_from(">HB", buf, off)
        v, off = _value(buf, off + 3, t)
        out[idx] = [t, v]
    return out, off


def decode(buf: bytes) -> tuple[dict[str, int], dict[int, list[Any]]]:
    """Decode a leaf -> (header, fields)."""
    if len(buf) < 12:
        raise Dop2DecodeError(f"leaf too short: {len(buf)} bytes")
    _ln, unit, attr, i1, i2, cnt = struct.unpack_from(">HHHHHH", buf, 0)
    fields, off = _fields(buf, 12, cnt)
    if off != len(buf):
        raise Dop2DecodeError(f"trailing bytes: consumed {off} of {len(buf)}")
    return {"unit": unit, "attr": attr, "idx1": i1, "idx2": i2}, fields


def _enc_value(t: int, v: Any) -> bytes:
    if t in FIXED:
        return struct.pack(FIXED[t][1], v)
    if t == 0x10:
        return struct.pack(">H", len(v)) + _enc_fields(v)
    if t in ARRAY:
        return struct.pack(">H", len(v)) + b"".join(_enc_value(ARRAY[t], e) for e in v)
    if t == 0x20:
        return struct.pack(">H", len(v)) + v
    if t == TYPE_STRUCT_ARRAY:
        return struct.pack(">H", len(v)) + b"".join(
            struct.pack(">H", len(e)) + _enc_fields(e) for e in v
        )
    raise Dop2DecodeError(f"unknown DOP2 type 0x{t:02x}")


def _enc_fields(fields: dict[int, list[Any]]) -> bytes:
    return b"".join(
        struct.pack(">HB", idx, t) + _enc_value(t, v) for idx, (t, v) in fields.items()
    )


def encode(header: dict[str, int], fields: dict[int, list[Any]]) -> bytes:
    """Encode (header, fields) -> leaf bytes, unpadded."""
    body = struct.pack(
        ">HHHHH",
        header["unit"],
        header["attr"],
        header["idx1"],
        header["idx2"],
        len(fields),
    )
    body += _enc_fields(fields)
    return struct.pack(">H", len(body)) + body


def set_field(fields: dict[int, list[Any]], idx: int, value: Any) -> None:
    """Set field *idx* to *value*, keeping its wire type."""
    fields[idx][1] = value
