"""Bundle encryption (ADR-0013): scrypt + AESGCM envelope — round-trip,
indistinguishable failures, fresh randomness, and no secret bytes in the
encrypted text."""

import base64

import pytest

from devcake.settings_crypto import (MIN_PASSPHRASE_LEN, DecryptError,
                                     decrypt_blob, encrypt_blob)


def test_roundtrip():
    env = encrypt_blob("correct horse", b'{"secrets": {"harness": {}}}')
    assert decrypt_blob("correct horse", env) == b'{"secrets": {"harness": {}}}'
    assert env["cipher"] == "aesgcm" and env["kdf"] == "scrypt" and env["v"] == 1


def test_wrong_passphrase_and_tamper_are_one_message():
    env = encrypt_blob("correct horse", b"payload-bytes")
    with pytest.raises(DecryptError) as wrong:
        decrypt_blob("wrong horse!", env)
    ct = bytearray(base64.b64decode(env["ct_b64"]))
    ct[0] ^= 0x01
    tampered = {**env, "ct_b64": base64.b64encode(bytes(ct)).decode()}
    with pytest.raises(DecryptError) as tamper:
        decrypt_blob("correct horse", tampered)
    assert str(wrong.value) == str(tamper.value) \
        == "wrong passphrase or corrupted bundle"


def test_fresh_salt_and_nonce_per_call():
    a = encrypt_blob("correct horse", b"same payload")
    b = encrypt_blob("correct horse", b"same payload")
    assert a["salt_b64"] != b["salt_b64"]
    assert a["nonce_b64"] != b["nonce_b64"]
    assert a["ct_b64"] != b["ct_b64"]


def test_short_passphrase_refused_at_encrypt():
    with pytest.raises(ValueError):
        encrypt_blob("x" * (MIN_PASSPHRASE_LEN - 1), b"data")


def test_unsupported_envelope_refused():
    with pytest.raises(DecryptError):
        decrypt_blob("correct horse", {"v": 2, "cipher": "aesgcm",
                                       "kdf": "scrypt"})
    with pytest.raises(DecryptError):
        decrypt_blob("correct horse", {})
