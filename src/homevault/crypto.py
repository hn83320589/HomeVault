"""金鑰推導、驗證加密（AES-256-GCM）與 blob 識別碼（HMAC-SHA256）。

金鑰結構：
    密碼 --scrypt--> KEK --AES-GCM 解開--> MasterKey(enc_key, mac_key, chunker_seed)

密碼與 master key 分離，日後改密碼只需重新加密 key file，不必重寫資料。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unicodedata
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from homevault.errors import IntegrityError, WrongPasswordError

KEY_SIZE = 32
SEED_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
OVERHEAD = NONCE_SIZE + TAG_SIZE

DEFAULT_KDF_N = 2**17  # 約 128 MiB 記憶體、0.3~0.5 秒
KDF_R = 8
KDF_P = 1
_KEY_FILE_AAD = b"homevault-key-v1"


@dataclass(frozen=True)
class MasterKey:
    enc_key: bytes
    mac_key: bytes
    chunker_seed: bytes

    @classmethod
    def generate(cls) -> MasterKey:
        return cls(os.urandom(KEY_SIZE), os.urandom(KEY_SIZE), os.urandom(SEED_SIZE))

    def to_bytes(self) -> bytes:
        return self.enc_key + self.mac_key + self.chunker_seed

    @classmethod
    def from_bytes(cls, raw: bytes) -> MasterKey:
        if len(raw) != 2 * KEY_SIZE + SEED_SIZE:
            raise IntegrityError("master key 長度不正確")
        return cls(raw[:KEY_SIZE], raw[KEY_SIZE : 2 * KEY_SIZE], raw[2 * KEY_SIZE :])

    def blob_id(self, data: bytes) -> bytes:
        """以 HMAC-SHA256 計算識別碼；沒有 mac_key 就無法由已知內容推算出 id。"""
        return hmac.digest(self.mac_key, data, hashlib.sha256)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """加密並驗證：回傳 nonce(12) ∥ ciphertext ∥ tag(16)。"""
    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, sealed: bytes, aad: bytes) -> bytes:
    if len(sealed) < OVERHEAD:
        raise IntegrityError("加密資料長度不足，可能已被截斷")
    try:
        return AESGCM(key).decrypt(sealed[:NONCE_SIZE], sealed[NONCE_SIZE:], aad)
    except InvalidTag:
        raise IntegrityError("資料驗證失敗：內容已損毀或遭竄改") from None


def _derive_kek(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    normalized = unicodedata.normalize("NFC", password).encode("utf-8")
    return Scrypt(salt=salt, length=KEY_SIZE, n=n, r=r, p=p).derive(normalized)


def create_key_file(password: str, master: MasterKey, kdf_n: int = DEFAULT_KDF_N) -> bytes:
    salt = os.urandom(32)
    kek = _derive_kek(password, salt, kdf_n, KDF_R, KDF_P)
    doc = {
        "version": 1,
        "kdf": "scrypt",
        "n": kdf_n,
        "r": KDF_R,
        "p": KDF_P,
        "salt": base64.b64encode(salt).decode(),
        "data": base64.b64encode(seal(kek, master.to_bytes(), _KEY_FILE_AAD)).decode(),
    }
    return json.dumps(doc, indent=2).encode()


def open_key_file(password: str, raw: bytes) -> MasterKey:
    try:
        doc = json.loads(raw)
        if doc.get("kdf") != "scrypt":
            raise IntegrityError(f"不支援的 KDF：{doc.get('kdf')}")
        salt = base64.b64decode(doc["salt"])
        sealed = base64.b64decode(doc["data"])
        n, r, p = int(doc["n"]), int(doc["r"]), int(doc["p"])
    except (ValueError, KeyError, TypeError) as exc:
        raise IntegrityError(f"key file 格式損毀：{exc}") from None
    kek = _derive_kek(password, salt, n, r, p)
    try:
        return MasterKey.from_bytes(unseal(kek, sealed, _KEY_FILE_AAD))
    except IntegrityError:
        raise WrongPasswordError("密碼錯誤，無法開啟備份庫（備份庫未做任何變更）") from None
