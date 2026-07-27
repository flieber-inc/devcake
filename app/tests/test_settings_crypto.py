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


def test_hostile_scrypt_params_rejected_fast():
    """Untrusted n/r/p must not drive scrypt cost (event-loop DoS)."""
    import time
    from devcake.settings_crypto import _KDF

    env = encrypt_blob("correct horse", b"payload-bytes")
    hostile = {**env, "p": 1024}
    t0 = time.perf_counter()
    with pytest.raises(DecryptError) as exc:
        decrypt_blob("correct horse", hostile)
    elapsed = time.perf_counter() - t0
    assert str(exc.value) == "unsupported envelope"
    assert elapsed < 0.2, f"hostile KDF took {elapsed:.3f}s — params not pinned"

    for key, bad in (("n", 2**20), ("r", 64), ("p", 64)):
        with pytest.raises(DecryptError) as e:
            decrypt_blob("correct horse", {**env, key: bad})
        assert str(e.value) == "unsupported envelope"

    # matching params (the ones encrypt wrote) still decrypt
    assert decrypt_blob("correct horse", {**env, **_KDF}) == b"payload-bytes"
