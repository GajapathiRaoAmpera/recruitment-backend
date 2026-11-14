"""Staff password hashing with the standard library's scrypt (no extra dependency)."""

import base64
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1  # ~16 MB, ~50 ms per hash: OWASP's scrypt minimum
MIN_LENGTH = 10


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, N, R, P)
    b64 = base64.b64encode
    return f"scrypt${N}${R}${P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    """False for a wrong password, a missing hash or a malformed one (never raises)."""
    if not stored:
        _scrypt(password, b"0" * 16, N, R, P)  # same cost as a real check: no timing hint
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False
