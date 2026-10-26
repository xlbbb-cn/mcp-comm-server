"""Serial helper functions: parameter parsing, payload encoding, formatting."""

from __future__ import annotations

import base64
import binascii
import math
import re
from typing import Any, Literal

import serial

DataFormat = Literal["str", "text", "ascii", "hex", "base64"]

PARITY_MAP: dict[str, str] = {
    "N": serial.PARITY_NONE,
    "NONE": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "EVEN": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "ODD": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "MARK": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
    "SPACE": serial.PARITY_SPACE,
}

STOPBITS_MAP: dict[float, float] = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}

BYTESIZE_MAP: dict[int, int] = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}

PARITY_NAMES: dict[str, str] = {
    serial.PARITY_NONE: "N",
    serial.PARITY_EVEN: "E",
    serial.PARITY_ODD: "O",
    serial.PARITY_MARK: "M",
    serial.PARITY_SPACE: "S",
}


class PayloadError(ValueError):
    """Raised when a payload cannot be encoded or decoded with the given format."""


def parse_parity(value: str | None) -> str:
    """Map a parity name ('N', 'E', 'O', 'M', 'S' or long names) to a pyserial constant."""
    if value is None:
        return serial.PARITY_NONE
    if value in PARITY_MAP.values():
        return value
    return PARITY_MAP.get(str(value).strip().upper(), serial.PARITY_NONE)


def parse_stopbits(value: float | str | None) -> float:
    """Map 1 / 1.5 / 2 to a pyserial stop-bit constant."""
    if value is None:
        return serial.STOPBITS_ONE
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return serial.STOPBITS_ONE
    return STOPBITS_MAP.get(as_float, serial.STOPBITS_ONE)


def parse_bytesize(value: int | str | None) -> int:
    """Map 5 / 6 / 7 / 8 to a pyserial data-bit constant."""
    if value is None:
        return serial.EIGHTBITS
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return serial.EIGHTBITS
    return BYTESIZE_MAP.get(as_int, serial.EIGHTBITS)


def parity_name(value: str | None) -> str:
    """Human readable single letter name for a pyserial parity constant."""
    if value is None:
        return "N"
    return PARITY_NAMES.get(value, str(value))


_SIMPLE_ESCAPES = {
    "r": "\r",
    "n": "\n",
    "t": "\t",
    "0": "\0",
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
}

_HEX_SEPARATOR_RE = re.compile(r"[\s,:\-_]+")


def decode_escapes(text: str) -> str:
    r"""Turn literal escape sequences (\r, \n, \t, \xNN, \0) into real characters.

    A two-digit ``\xNN`` escape is decoded as the latin-1 character with that
    code point (``"\\xFF"`` -> ``"\xff"``).  Use :func:`encode_escaped_text`
    when the result is sent to a serial device: it preserves the exact byte
    value instead of re-encoding the character with the payload encoding.
    Malformed escapes such as ``"\x4"`` or ``"\xZZ"`` are kept verbatim.
    """
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch != "\\" or i + 1 >= length:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
            continue
        if nxt in {"x", "X"}:
            hex_part = text[i + 2 : i + 4]
            if len(hex_part) == 2:
                try:
                    out.append(chr(int(hex_part, 16)))
                except ValueError:
                    pass
                else:
                    i += 4
                    continue
            # Incomplete or malformed escape: keep the backslash so the
            # caller can see exactly what was supplied.
            out.append("\\")
            i += 1
            continue
        # Unknown escape: keep the escaped character, drop the backslash
        # (historical behaviour, e.g. "\q" -> "q").
        out.append(nxt)
        i += 2
    return "".join(out)


def encode_escaped_text(text: str, encoding: str = "utf-8") -> bytes:
    r"""Encode text with escape expansion, preserving ``\xNN`` as raw bytes.

    ``encode_escapes`` + ``str.encode`` is lossy for non-ASCII escapes:
    ``"\\xFF".encode("utf-8")`` would emit ``b"\xc3\xbf"`` instead of
    ``b"\xff"``.  Serial payloads are byte oriented, so escapes are applied at
    the byte level here.
    """
    out = bytearray()
    plain: list[str] = []

    def flush_plain() -> None:
        if plain:
            out.extend("".join(plain).encode(encoding))
            plain.clear()

    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch != "\\" or i + 1 >= length:
            plain.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _SIMPLE_ESCAPES:
            flush_plain()
            out.extend(_SIMPLE_ESCAPES[nxt].encode(encoding))
            i += 2
            continue
        if nxt in {"x", "X"}:
            hex_part = text[i + 2 : i + 4]
            if len(hex_part) == 2:
                try:
                    value = int(hex_part, 16)
                except ValueError:
                    pass
                else:
                    flush_plain()
                    out.append(value)
                    i += 4
                    continue
            plain.append("\\")
            i += 1
            continue
        plain.append(nxt)
        i += 2
    flush_plain()
    return bytes(out)


def encode_payload(data: str | bytes, data_format: DataFormat = "text", encoding: str = "utf-8") -> bytes:
    """Encode tool input into raw bytes.

    data_format:
      - str / text: plain string, escape sequences such as ``\\r\\n`` are expanded
      - ascii: plain ASCII string, escape sequences are expanded
      - hex: hexadecimal string, separators (space, comma, ``-``, ``:``, ``_``)
        and a ``0x`` prefix per token are allowed
      - base64: base64 encoded string (whitespace ignored, padding required)
    """
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)

    fmt = (data_format or "text").lower()
    if fmt in {"str", "text"}:
        return encode_escaped_text(data, encoding)
    if fmt == "ascii":
        try:
            return encode_escaped_text(data, "ascii")
        except UnicodeEncodeError as exc:
            raise PayloadError(f"payload is not ASCII: {exc}") from exc
    if fmt == "hex":
        if not data.strip():
            return b""
        # Strip a 0x prefix only at the start of each token.  A global
        # str.replace("0x", "") silently corrupts values such as "A0x1".
        parts: list[str] = []
        for token in _HEX_SEPARATOR_RE.split(data.strip()):
            if not token:
                continue
            if token[:2].lower() == "0x":
                token = token[2:]
            if token:
                parts.append(token)
        cleaned = "".join(parts)
        if not cleaned:
            raise PayloadError("hex payload contains no hex digits")
        try:
            return bytes.fromhex(cleaned)
        except ValueError as exc:
            raise PayloadError(f"invalid hex payload: {exc}") from exc
    if fmt == "base64":
        cleaned_b64 = "".join(data.split())
        if not cleaned_b64:
            return b""
        try:
            # validate=True rejects non-alphabet characters instead of
            # silently discarding them and returning b"".
            return base64.b64decode(cleaned_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PayloadError(f"invalid base64 payload: {exc}") from exc
    raise PayloadError(
        f"unsupported data_format: {data_format!r} (use str, text, ascii, hex or base64)"
    )


def decode_text(raw: bytes, encoding: str = "utf-8") -> str:
    """Decode bytes to text, never raising: undecodable bytes become U+FFFD."""
    if not raw:
        return ""
    try:
        return raw.decode(encoding)
    except LookupError:
        # Unknown encoding name: raw.decode(encoding, errors="replace") would
        # raise the same LookupError, so fall back to a known encoding.
        return raw.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return raw.decode(encoding, errors="replace")


def hex_bytes(raw: bytes) -> str:
    """Return lower-case hex without separators."""
    return raw.hex() if raw else ""


def decode_payload_preview(raw: bytes, encoding: str = "utf-8") -> str:
    """Decode bytes for display, keeping control characters visible (``\\r\\n``)."""
    if not raw:
        return ""
    text = decode_text(raw, encoding)
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def hex_dump(raw: bytes) -> str:
    """Return space separated upper-case hex, e.g. ``41 54 0D 0A``."""
    return " ".join(f"{byte:02X}" for byte in raw)


def make_json_safe(value: Any) -> Any:
    """Best effort conversion for values that JSON encoders cannot handle."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        # NaN/Infinity are not valid JSON numbers.
        return str(value)
    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]
    return value


def format_port_info(port: Any) -> dict[str, Any]:
    """Convert a pyserial ListPortInfo entry into a JSON friendly dict."""
    return {
        "device": getattr(port, "device", None),
        "name": getattr(port, "name", None),
        "description": getattr(port, "description", None),
        "hwid": getattr(port, "hwid", None),
        "vid": getattr(port, "vid", None),
        "pid": getattr(port, "pid", None),
        "serial_number": getattr(port, "serial_number", None),
        "manufacturer": getattr(port, "manufacturer", None),
        "product": getattr(port, "product", None),
        "interface": getattr(port, "interface", None),
        "location": getattr(port, "location", None),
    }
