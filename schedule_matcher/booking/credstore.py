"""Encrypt credentials at rest with Windows DPAPI.

DPAPI ties the ciphertext to this Windows user account on this machine, so
the database can hold NTU passwords without storing them in plain text, and
copying the .db file to another machine yields nothing readable. On other
platforms the values fall back to plain bytes (the DB already lives outside
any synced folder).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys

_ENTROPY = b"schedule-matcher-v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    inp = _blob(data)
    entropy = _blob(_ENTROPY)
    out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def encrypt(text: str) -> bytes:
    data = text.encode("utf-8")
    if sys.platform != "win32":
        return data
    return _crypt(data, protect=True)


def decrypt(data: bytes | None) -> str | None:
    if data is None:
        return None
    if sys.platform != "win32":
        return bytes(data).decode("utf-8")
    return _crypt(bytes(data), protect=False).decode("utf-8")
