"""Passphrase encryption for settings bundles (ADR-0013): scrypt KDF
(stdlib) + AESGCM (`cryptography` — a vetted AEAD, deliberately no hand-rolled
construction). Encrypts the secret-bearing sections (B and C) of a bundle;
section A stays plaintext so the file remains diffable.

Wrong passphrase and tampered ciphertext are indistinguishable BY DESIGN —
one error, one message — so the envelope leaks nothing about which it was.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MIN_PASSPHRASE_LEN = 8

# scrypt cost: interactive-grade (~100 ms) — the envelope protects an
# operator-carried file, not a wire protocol; maxmem must cover 128*n*r bytes
_KDF = {"n": 16384, "r": 8, "p": 1}
_MAXMEM = 64 * 1024 * 1024


class DecryptError(ValueError):
    """Wrong passphrase OR corrupted/tampered envelope — deliberately one
    indistinguishable failure."""


def _derive(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt,
                          n=int(kdf["n"]), r=int(kdf["r"]), p=int(kdf["p"]),
                          maxmem=_MAXMEM, dklen=32)


def encrypt_blob(passphrase: str, plaintext: bytes) -> dict:
    """→ the bundle `protected` envelope. Fresh salt + nonce per call."""
    if len(passphrase) < MIN_PASSPHRASE_LEN:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE_LEN} "
                         "characters")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive(passphrase, salt, _KDF)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return {"v": 1, "kdf": "scrypt", **_KDF, "cipher": "aesgcm",
            "salt_b64": base64.b64encode(salt).decode(),
            "nonce_b64": base64.b64encode(nonce).decode(),
            "ct_b64": base64.b64encode(ct).decode()}


def decrypt_blob(passphrase: str, envelope: dict) -> bytes:
    try:
        if envelope.get("v") != 1 or envelope.get("cipher") != "aesgcm" \
                or envelope.get("kdf") != "scrypt":
            raise DecryptError("unsupported envelope")
        # Pin KDF cost to encrypt-time constants. Never trust envelope n/r/p
        # for scrypt work — a hostile p/n stalls the FastAPI event loop.
        for k, expected in _KDF.items():
            if k in envelope and envelope[k] != expected:
                raise DecryptError("unsupported envelope")
        salt = base64.b64decode(envelope["salt_b64"])
        nonce = base64.b64decode(envelope["nonce_b64"])
        ct = base64.b64decode(envelope["ct_b64"])
        key = _derive(passphrase, salt, _KDF)
        return AESGCM(key).decrypt(nonce, ct, None)
    except DecryptError:
        raise
    except (InvalidTag, KeyError, ValueError, TypeError):
        raise DecryptError("wrong passphrase or corrupted bundle")
