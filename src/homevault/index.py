"""Blob 索引：blob id → 所在的 pack 與位置。

記憶體中的 ``Index`` 為了支撐 200 萬個 blob，把位置資訊編碼成單一整數，
每筆約 150 bytes。同一個 blob 出現在多個 pack 是合法的（中斷後重寫、prune
的過渡狀態），查詢時任選一個即可。

Index 檔格式（壓縮後加密）::

    "HVIX" ∥ version u8 ∥ pack 數 u32 ∥ 每個 pack：pack_id 32B ∥ entries（同 pack header 格式）
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator
from dataclasses import dataclass

import zstandard

from homevault.crypto import MasterKey, seal, unseal
from homevault.errors import IntegrityError
from homevault.pack import BlobType, PackEntry, decode_entries, encode_entries

_MAGIC = b"HVIX\x01"
_AAD = b"homevault-index-v1"
_COUNT = struct.Struct("<I")
_MAX_INDEX_SIZE = 1 << 30

PackMap = dict[bytes, list[PackEntry]]


@dataclass(frozen=True, slots=True)
class IndexEntry:
    pack_id: bytes
    type: BlobType
    offset: int
    length: int
    raw_length: int


class Index:
    def __init__(self) -> None:
        self._packs: list[bytes] = []
        self._pack_no: dict[bytes, int] = {}
        self._blobs: dict[bytes, int] = {}

    def add_pack(self, pack_id: bytes, entries: list[PackEntry]) -> None:
        pack_no = self._pack_no.get(pack_id)
        if pack_no is None:
            pack_no = len(self._packs)
            self._packs.append(pack_id)
            self._pack_no[pack_id] = pack_no
        for e in entries:
            if e.id not in self._blobs:
                self._blobs[e.id] = (
                    pack_no << 97
                    | int(e.type) << 96
                    | e.offset << 64
                    | e.length << 32
                    | e.raw_length
                )

    def get(self, blob_id: bytes) -> IndexEntry | None:
        v = self._blobs.get(blob_id)
        if v is None:
            return None
        m32 = 0xFFFFFFFF
        return IndexEntry(
            pack_id=self._packs[v >> 97],
            type=BlobType((v >> 96) & 1),
            offset=(v >> 64) & m32,
            length=(v >> 32) & m32,
            raw_length=v & m32,
        )

    def __contains__(self, blob_id: bytes) -> bool:
        return blob_id in self._blobs

    def __len__(self) -> int:
        return len(self._blobs)

    def blob_ids(self) -> Iterator[bytes]:
        return iter(self._blobs)


def encode_index_file(key: MasterKey, packs: PackMap) -> bytes:
    parts = [_MAGIC, _COUNT.pack(len(packs))]
    for pack_id, entries in packs.items():
        parts.append(pack_id)
        parts.append(encode_entries(entries))
    compressed = zstandard.ZstdCompressor(level=3).compress(b"".join(parts))
    return seal(key.enc_key, compressed, _AAD)


def decode_index_file(key: MasterKey, raw: bytes) -> PackMap:
    try:
        plain = zstandard.ZstdDecompressor().decompress(
            unseal(key.enc_key, raw, _AAD), max_output_size=_MAX_INDEX_SIZE
        )
    except zstandard.ZstdError as exc:
        raise IntegrityError(f"index 檔解壓縮失敗：{exc}") from None
    if not plain.startswith(_MAGIC):
        raise IntegrityError("index 檔格式不正確")
    pos = len(_MAGIC)
    try:
        (count,) = _COUNT.unpack_from(plain, pos)
    except struct.error:
        raise IntegrityError("index 檔被截斷") from None
    pos += _COUNT.size
    packs: PackMap = {}
    for _ in range(count):
        pack_id = plain[pos : pos + 32]
        if len(pack_id) != 32:
            raise IntegrityError("index 檔被截斷")
        entries, pos = decode_entries(plain, pos + 32)
        packs[pack_id] = entries
    return packs


def index_file_id(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()
