import hashlib
import json
import unicodedata

import pytest

from homevault.crypto import (
    MasterKey,
    create_key_file,
    open_key_file,
    seal,
    unseal,
)
from homevault.errors import IntegrityError, WrongPasswordError

FAST_KDF = 2**10  # 測試用的低成本 scrypt 參數


@pytest.fixture
def key() -> MasterKey:
    return MasterKey.generate()


def test_seal_roundtrip(key):
    sealed = seal(key.enc_key, b"hello", aad=b"ctx")
    assert unseal(key.enc_key, sealed, aad=b"ctx") == b"hello"


def test_seal_uses_random_nonce(key):
    assert seal(key.enc_key, b"same", b"") != seal(key.enc_key, b"same", b"")


def test_tampered_ciphertext_detected(key):
    sealed = bytearray(seal(key.enc_key, b"important data", b""))
    sealed[15] ^= 0x01
    with pytest.raises(IntegrityError):
        unseal(key.enc_key, bytes(sealed), b"")


def test_aad_mismatch_detected(key):
    sealed = seal(key.enc_key, b"data", aad=b"blob-a")
    with pytest.raises(IntegrityError):
        unseal(key.enc_key, sealed, aad=b"blob-b")


def test_truncated_ciphertext_detected(key):
    with pytest.raises(IntegrityError):
        unseal(key.enc_key, b"short", b"")


def test_master_key_serialization_roundtrip(key):
    assert MasterKey.from_bytes(key.to_bytes()) == key


def test_blob_id_is_keyed_hmac_not_plain_sha256(key):
    data = b"family photo bytes"
    assert key.blob_id(data) != hashlib.sha256(data).digest()
    assert key.blob_id(data) == key.blob_id(data)
    assert MasterKey.generate().blob_id(data) != key.blob_id(data)


def test_key_file_opens_with_correct_password(key):
    raw = create_key_file("correct horse", key, kdf_n=FAST_KDF)
    assert open_key_file("correct horse", raw) == key


def test_key_file_rejects_wrong_password(key):
    raw = create_key_file("correct horse", key, kdf_n=FAST_KDF)
    with pytest.raises(WrongPasswordError):
        open_key_file("wrong horse", raw)


def test_key_file_does_not_contain_master_key_in_plaintext(key):
    raw = create_key_file("pw", key, kdf_n=FAST_KDF)
    for secret in (key.enc_key, key.mac_key, key.chunker_seed):
        assert secret not in raw
        assert secret.hex().encode() not in raw
    assert json.loads(raw)["kdf"] == "scrypt"


def test_password_unicode_normalization_is_consistent(key):
    """Mac 輸入法可能產生 NFD，Windows 通常是 NFC，兩者應視為同一個密碼。"""
    password = "家庭相簿é"
    raw = create_key_file(unicodedata.normalize("NFD", password), key, kdf_n=FAST_KDF)
    assert open_key_file(unicodedata.normalize("NFC", password), raw) == key


def test_corrupted_key_file_reports_integrity_error(key):
    with pytest.raises(IntegrityError):
        open_key_file("pw", b"{not json")
