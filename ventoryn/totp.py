"""TOTP two-factor authentication — standard library only.

RFC 6238 TOTP (SHA-1, 30-second steps, 6 digits) plus one-time
backup codes. This is the ONE TOTP implementation used by both the
admin login and the customer sign-in — do not build a second one.

Secrets are base32 (RFC 4648, no padding) so they can be typed by
hand or scanned from the QR code the setup page draws. Verification
accepts the current step and one step either side (clock skew), and
nothing else.

Backup codes: 8 codes of 8 digits each, shown once at setup and
stored only as salted SHA-256 hashes. A code works once and then
fails — the caller must mark it used on success.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

STEP_SECONDS = 30
DIGITS = 6
#: Accept the current step plus one step of clock skew either side.
WINDOW_STEPS = 1
BACKUP_CODE_COUNT = 8


def new_secret() -> str:
    """A fresh random TOTP secret (base32, no padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key_bytes(secret: str) -> bytes:
    padded = secret.strip().upper() + "=" * (-len(secret.strip()) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, when: float | None = None) -> str:
    """The 6-digit code for `secret` at time `when` (default: now)."""
    if when is None:
        when = time.time()
    counter = int(when // STEP_SECONDS)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(_key_bytes(secret), msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (
        (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF)
        % (10 ** DIGITS)
    )
    return f"{value:0{DIGITS}d}"


def verify_code(secret: str, code: str, when: float | None = None) -> bool:
    """True when `code` is valid for `secret` now (±1 step). The code
    must be exactly 6 digits; anything else fails."""
    if not isinstance(code, str) or len(code) != DIGITS or not code.isdigit():
        return False
    if when is None:
        when = time.time()
    for delta in range(-WINDOW_STEPS, WINDOW_STEPS + 1):
        if hmac.compare_digest(
            code, code_at(secret, when + delta * STEP_SECONDS)
        ):
            return True
    return False


def provisioning_uri(secret: str, account: str, issuer: str = "Axionory") -> str:
    """otpauth:// URI for the authenticator app (label + issuer)."""
    label = f"{issuer}:{account}".replace(" ", "%20")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def new_backup_codes() -> list[str]:
    """8 fresh backup codes (8 digits each, grouped for readability)."""
    codes = set()
    while len(codes) < BACKUP_CODE_COUNT:
        codes.add(f"{secrets.randbelow(100_000_000):08d}")
    return sorted(codes)


def hash_backup_code(code: str, salt: str) -> str:
    """Salted hash of one backup code — this is what gets stored.
    The code itself is never stored."""
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def verify_backup_code(code: str, salt: str, stored_hashes: list[str]) -> str | None:
    """Return the stored hash matching `code`, or None. The caller
    must delete that hash (mark the code used) on success."""
    want = hash_backup_code(code, salt)
    for stored in stored_hashes:
        if hmac.compare_digest(want, stored):
            return stored
    return None
