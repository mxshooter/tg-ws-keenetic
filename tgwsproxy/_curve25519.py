"""Pure-Python X25519 (RFC 7748) for boxes without ``cryptography``.

The forged Fake-TLS ServerHello carries a "server key share" that Telegram
clients skip entirely (all authentication is the ee-secret HMAC). It still
has to be a genuine Curve25519 point: random bytes are not, and DPI can use
that to tell a masquerading proxy from a real TLS server.

``cryptography`` is used when importable; this module is the portable
fallback (a 255-bit Montgomery ladder, ~1 ms on a desktop CPU).
"""
from __future__ import annotations

P = 2 ** 255 - 19
_A24 = 121665
_BASE_POINT = (9).to_bytes(32, "little")
_U_MASK = (1 << 255) - 1


def _clamp(scalar: bytes) -> int:
    """Apply the RFC 7748 clamping to a 32-byte little-endian scalar."""
    key = bytearray(scalar)
    key[0] &= 248
    key[31] &= 127
    key[31] |= 64
    return int.from_bytes(key, "little")


def x25519(scalar: bytes, point: bytes) -> bytes:
    """Return the X25519 shared value of ``scalar`` and the u-coordinate."""
    k = _clamp(scalar)
    x1 = int.from_bytes(point, "little") & _U_MASK
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for bit in range(254, -1, -1):
        kt = (k >> bit) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt

        a = (x2 + z2) % P
        aa = a * a % P
        b = (x2 - z2) % P
        bb = b * b % P
        e = (aa - bb) % P
        c = (x3 + z3) % P
        d = (x3 - z3) % P
        da = d * a % P
        cb = c * b % P
        x3 = (da + cb) % P
        x3 = x3 * x3 % P
        z3 = (da - cb) % P
        z3 = z3 * z3 % P * x1 % P
        x2 = aa * bb % P
        z2 = e * ((aa + _A24 * e) % P) % P

    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


def x25519_public_key(private_key: bytes) -> bytes:
    """Derive the public key (X25519(private, 9)) for a private scalar."""
    return x25519(private_key, _BASE_POINT)


def is_valid_point(point: bytes) -> bool:
    """True when ``point`` is a u-coordinate of a Curve25519 point.

    Used by the tests: a point is valid when u^3 + 486662u^2 + u is a
    quadratic residue modulo 2^255-19 (or u == 0).
    """
    if len(point) != 32:
        return False
    u = int.from_bytes(point, "little") & _U_MASK
    if u == 0:
        return True
    v = (u * u * u + 486662 * u * u + u) % P
    return pow(v, (P - 1) // 2, P) == 1
