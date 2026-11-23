"""Blob 編碼與 pack 檔格式。

Blob 處理順序：明文 → zstd 壓縮（沒變小就存原文，以 1 byte flag 標示）→ AES-GCM 加密，
AAD 綁定 ``type ∥ blob_id``，避免 blob 被調換或 index 指錯位置時讀出錯誤內容。

Pack 檔格式（header 放在檔尾，可邊寫邊串流）::

    [加密 blob 1][加密 blob 2]...[加密 header][header 長度 u32 little-endian]

header 解密後 = 筆數 u32 + 每筆 ``type u8 ∥ id 32B ∥ offset u32 ∥ length u32 ∥ raw_length u32``
Pack 的檔名是整個檔案內容的 SHA-256，所以檔名本身就是校驗碼。
"""

from __future__ import annotations

import hashlib
import struct
import threading
from dataclasses import dataclass
from enum import IntEnum

import zstandard

from homevault.crypto import MasterKey, seal, unseal
from homevault.errors import IntegrityError
from homevault.storage import LocalBackend

_HEADER_AAD = b"homevault-pack-header-v1"
_ENTRY = struct.Struct("<B32sIII")
_COUNT = struct.Struct("<I")
_FLAG_RAW = 0
_FLAG_ZSTD = 1
ZSTD_LEVEL = 3

_local = threading.local()


class BlobType(IntEnum):
    DATA = 0
    TREE = 1


@dataclass(frozen=True, slots=True)
class PackEntry:
    type: BlobType
    id: bytes
    offset: int
    length: int
    raw_length: int


def pack_name(pack_id: bytes) -> str:
    h = pack_id.hex()
    return f"data/{h[:2]}/{h}"


def _blob_aad(blob_type: BlobType, blob_id: bytes) -> bytes:
    return bytes([blob_type]) + blob_id


def _compressor() -> zstandard.ZstdCompressor:
    # zstandard 的 compressor 物件不是執行緒安全的，每個執行緒各用一個
    if not hasattr(_local, "cctx"):
        _local.cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        _local.dctx = zstandard.ZstdDecompressor()
    return _local.cctx


def _decompressor() -> zstandard.ZstdDecompressor:
    _compressor()
    return _local.dctx


def encode_blob(key: MasterKey, blob_type: BlobType, blob_id: bytes, plaintext: bytes) -> bytes:
    compressed = _compressor().compress(plaintext)
    if len(compressed) < len(plaintext):
        payload = bytes([_FLAG_ZSTD]) + compressed
    else:
        payload = bytes([_FLAG_RAW]) + plaintext
    return seal(key.enc_key, payload, _blob_aad(blob_type, blob_id))


def decode_blob(key: MasterKey, blob_type: BlobType, blob_id: bytes, sealed: bytes) -> bytes:
    """解密並驗證 blob；除了 AES-GCM 的驗證之外，還會重算 HMAC 與 blob id 比對。"""
    payload = unseal(key.enc_key, sealed, _blob_aad(blob_type, blob_id))
    if not payload:
        raise IntegrityError("blob 內容為空")
    flag, body = payload[0], payload[1:]
    if flag == _FLAG_ZSTD:
        try:
            plaintext = _decompressor().decompress(body)
        except zstandard.ZstdError as exc:
            raise IntegrityError(f"blob 解壓縮失敗：{exc}") from None
    elif flag == _FLAG_RAW:
        plaintext = body
    else:
        raise IntegrityError(f"未知的壓縮標記：{flag}")
    if key.blob_id(plaintext) != blob_id:
        raise IntegrityError(f"blob {blob_id.hex()[:16]} 內容與識別碼不符")
    return plaintext


class PackBuilder:
    """在記憶體中累積 blob，滿了之後由 finish() 產生完整的 pack 檔內容。"""

    def __init__(self, key: MasterKey) -> None:
        self._key = key
        self._parts: list[bytes] = []
        self.size = 0
        self.entries: list[PackEntry] = []

    def add(self, blob_type: BlobType, blob_id: bytes, sealed: bytes, raw_length: int) -> None:
        self.entries.append(PackEntry(blob_type, blob_id, self.size, len(sealed), raw_length))
        self._parts.append(sealed)
        self.size += len(sealed)

    def __len__(self) -> int:
        return len(self.entries)

    def finish(self) -> tuple[bytes, bytes, list[PackEntry]]:
        """回傳 (pack_id, pack 檔內容, entries)。"""
        header = encode_entries(self.entries)
        sealed_header = seal(self._key.enc_key, header, _HEADER_AAD)
        raw = b"".join([*self._parts, sealed_header, _COUNT.pack(len(sealed_header))])
        return hashlib.sha256(raw).digest(), raw, list(self.entries)


def encode_entries(entries: list[PackEntry]) -> bytes:
    out = [_COUNT.pack(len(entries))]
    out.extend(_ENTRY.pack(e.type, e.id, e.offset, e.length, e.raw_length) for e in entries)
    return b"".join(out)


def decode_entries(raw: bytes, pos: int = 0) -> tuple[list[PackEntry], int]:
    try:
        (count,) = _COUNT.unpack_from(raw, pos)
        pos += _COUNT.size
        entries = []
        for _ in range(count):
            t, blob_id, offset, length, raw_length = _ENTRY.unpack_from(raw, pos)
            entries.append(PackEntry(BlobType(t), blob_id, offset, length, raw_length))
            pos += _ENTRY.size
    except (struct.error, ValueError) as exc:
        raise IntegrityError(f"項目清單格式損毀：{exc}") from None
    return entries, pos


def read_pack_header(backend: LocalBackend, pack_id: bytes, key: MasterKey) -> list[PackEntry]:
    """只讀取 pack 檔尾的 header；用於中斷復原與一致性檢查。"""
    name = pack_name(pack_id)
    size = backend.size(name)
    if size < _COUNT.size:
        raise IntegrityError(f"pack {pack_id.hex()} 太短")
    (header_len,) = _COUNT.unpack(backend.read_range(name, size - _COUNT.size, _COUNT.size))
    data_end = size - _COUNT.size - header_len
    if data_end < 0:
        raise IntegrityError(f"pack {pack_id.hex()} header 長度不合理，檔案可能被截斷")
    sealed = backend.read_range(name, data_end, header_len)
    entries, _ = decode_entries(unseal(key.enc_key, sealed, _HEADER_AAD))
    for e in entries:
        if e.offset + e.length > data_end:
            raise IntegrityError(f"pack {pack_id.hex()} header 指向檔案範圍之外")
    return entries
