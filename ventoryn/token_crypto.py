"""Authenticated encryption for OAuth / API tokens held by Axionory.

Standard library only — there is no AES in the stdlib, so this is a
stream cipher built from HMAC-SHA256 (keystream = HMAC(key, nonce ||
counter)) with encrypt-then-MAC. It gives confidentiality and
tamper-evidence for tokens at rest; it is not a general-purpose
crypto library and must not be used for anything else.

Key: the ``VENTORYN_TOKEN_KEY`` environment variable. It may be a
base64 or hex string, or any passphrase — it is run through SHA-256
so any form yields a 32-byte key. If the variable is unset the
module raises at use time (fail closed: a token is never stored
unencrypted because the key is missing). A test key may be passed
explicitly.

Stored format (all base64): ``v1.<nonce>.<ciphertext>.<mac>``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac as _hmac
import os
import secrets

ENV_VAR = "VENTORYN_TOKEN_KEY"
_PREFIX = "v1"


def _derive_key(raw: str | bytes | None) -> bytes:
    if raw is None:
        raw = os.environ.get(ENV_VAR, "")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise RuntimeError(
                f"{ENV_VAR} is not set — tokens cannot be stored "
                "(refusing to store a token unencrypted)")
        # base64 (padded or not) or hex first; anything else is a
        # passphrase and is hashed as-is.
        try:
            padded = raw + "=" * (-len(raw) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(decoded) >= 16:
                return hashlib.sha256(decoded).digest()
        except (binascii.Error, ValueError):
            pass
        try:
            decoded = bytes.fromhex(raw)
            if len(decoded) >= 16:
                return hashlib.sha256(decoded).digest()
        except ValueError:
            pass
        return hashlib.sha256(raw.encode("utf-8")).digest()
    return hashlib.sha256(bytes(raw)).digest()


def _xor_stream(key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = _hmac.new(
            key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out))


def encrypt_token(plaintext: str, key: str | bytes | None = None) -> str:
    """Encrypt a token. Returns ``v1.<nonce>.<ciphertext>.<mac>`` (base64
    parts). The stored value never equals the plaintext and never
    contains it."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty token")
    k = _derive_key(key)
    nonce = secrets.token_bytes(16)
    body = _xor_stream(k, nonce, plaintext.encode("utf-8"))
    mac = _hmac.new(k, nonce + body, hashlib.sha256).digest()
    enc = lambda b: base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")
    return f"{_PREFIX}.{enc(nonce)}.{enc(body)}.{enc(mac)}"


def decrypt_token(stored: str, key: str | bytes | None = None) -> str:
    """Decrypt a value from :func:`encrypt_token`. Raises ValueError
    on a bad format or a failed MAC (tampered or wrong key)."""
    if not isinstance(stored, str) or not stored.startswith(_PREFIX + "."):
        raise ValueError("not an encrypted token value")
    parts = stored.split(".")
    if len(parts) != 4:
        raise ValueError("not an encrypted token value")
    dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    try:
        nonce, body, mac = dec(parts[1]), dec(parts[2]), dec(parts[3])
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not an encrypted token value") from exc
    k = _derive_key(key)
    expected = _hmac.new(k, nonce + body, hashlib.sha256).digest()
    if not _hmac.compare_digest(expected, mac):
        raise ValueError("token failed integrity check (tampered or wrong key)")
    return _xor_stream(k, nonce, body).decode("utf-8")


def is_encrypted(value: str | None) -> bool:
    """True when a stored value looks like an encrypted token."""
    return (
        isinstance(value, str)
        and value.startswith(_PREFIX + ".")
        and len(value.split(".")) == 4
    )
