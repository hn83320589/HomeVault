"""FastCDC 內容定義切塊（Content-Defined Chunking）。

切點規格（reference 與 fast 兩個實作都遵守同一份定義）：

* W(p) 是以位置 p 結尾、長度 64 bytes 的 gear hash：
  ``W(p) = Σ_{k=0..63} G[data[p-k]] << k  (mod 2^64)``。
  這等同於逐 byte 計算 ``h = (h << 1) + G[b]``，因為每次左移 1 bit，64 bytes 前的
  資料已經被移出 64-bit 暫存器。
* 從 chunk 起點 s 開始，剩餘 n bytes：
    - ``n <= min_size``：整段就是一個 chunk。
    - ``limit = min(n, max_size)``、``normal = min(avg_size, limit)``。
    - 候選長度 L 對應的判斷位置是 ``p = s + L - 1``（chunk 的最後一個 byte）。
    - 先在 ``L ∈ [min_size, normal)`` 找第一個 ``W(p) & mask_s == 0``；
      找不到再於 ``L ∈ [normal, limit)`` 找第一個 ``W(p) & mask_l == 0``；
      都找不到則 ``L = limit``。

mask 取 hash 的最高位元，因為 gear hash 的低位元只受最近幾個 byte 影響，
高位元才涵蓋完整的 64 bytes 視窗。mask_s 比平均值多 2 bits（較難切）、
mask_l 少 2 bits（較易切），也就是 FastCDC 的 normalized chunking level 2，
讓 chunk 大小集中在平均值附近。

因為 min_size >= 64，判斷位置的視窗必定完全落在當前 chunk 內，所以
「每個 chunk 重新計算 hash」與「整段資料一次算出所有 W(p)」結果完全一致。
fast 實作利用這點以 numpy 向量化計算。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO

import numpy as np

WINDOW = 64
_M64 = (1 << 64) - 1
_SCAN_BLOCK = 1 << 20  # 每次向量化計算 1 MiB，限制暫存陣列的記憶體用量


@dataclass(frozen=True)
class ChunkerParams:
    min_size: int
    avg_size: int
    max_size: int
    mask_s: int = field(init=False, repr=False)
    mask_l: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.min_size < WINDOW:
            raise ValueError(f"min_size 必須至少 {WINDOW} bytes")
        if self.avg_size & (self.avg_size - 1):
            raise ValueError("avg_size 必須是 2 的次方")
        if not self.min_size <= self.avg_size <= self.max_size:
            raise ValueError("必須滿足 min_size <= avg_size <= max_size")
        bits = self.avg_size.bit_length() - 1
        object.__setattr__(self, "mask_s", _top_bits_mask(bits + 2))
        object.__setattr__(self, "mask_l", _top_bits_mask(bits - 2))

    def to_dict(self) -> dict[str, int]:
        return {"min_size": self.min_size, "avg_size": self.avg_size, "max_size": self.max_size}


def _top_bits_mask(bits: int) -> int:
    return ((1 << bits) - 1) << (64 - bits)


DEFAULT_PARAMS = ChunkerParams(min_size=256 * 1024, avg_size=1024 * 1024, max_size=4 * 1024 * 1024)


def gear_table(seed: bytes) -> list[int]:
    """由備份庫的祕密種子推導 256 個 64-bit 隨機值。

    每個備份庫的切點都不同，攻擊者無法從切塊大小推測內容。
    """
    table = []
    for i in range(256):
        digest = hmac.digest(seed, b"homevault-gear" + i.to_bytes(2, "little"), hashlib.sha256)
        table.append(int.from_bytes(digest[:8], "little"))
    return table


def reference_chunk_lengths(data: bytes, gear: list[int], params: ChunkerParams) -> list[int]:
    """純 Python 參考實作：逐 byte 計算，直接對應模組說明中的規格。

    速度很慢（約 15 MiB/s），只用於測試，作為 fast 實作的正確性對照。
    """
    lengths = []
    start = 0
    while start < len(data):
        n = len(data) - start
        if n <= params.min_size:
            lengths.append(n)
            break
        limit = min(n, params.max_size)
        normal = min(params.avg_size, limit)
        length = limit
        h = 0
        for i in range(params.min_size - WINDOW, limit):
            h = ((h << 1) + gear[data[start + i]]) & _M64
            candidate = i + 1
            if candidate < params.min_size:
                continue
            mask = params.mask_s if candidate < normal else params.mask_l
            if candidate < limit and not h & mask:
                length = candidate
                break
        lengths.append(length)
        start += length
    return lengths


class Chunker:
    """以 numpy 向量化實作的串流切塊器。"""

    def __init__(
        self, seed: bytes, params: ChunkerParams = DEFAULT_PARAMS, read_size: int = 8 << 20
    ) -> None:
        if read_size < params.max_size:
            raise ValueError("read_size 不可小於 max_size")
        self.params = params
        self.read_size = read_size
        self._gear = np.array(gear_table(seed), dtype=np.uint64)
        self._mask_s = np.uint64(params.mask_s)
        self._mask_l = np.uint64(params.mask_l)

    def iter_chunks(self, stream: BinaryIO) -> Iterator[bytes]:
        p = self.params
        buf = bytearray()
        base = 0  # buf[0] 在整個串流中的絕對位置，永遠是當前 chunk 的起點
        empty = np.empty(0, dtype=np.int64)
        cand_s, cand_l = empty, empty  # 符合 mask 的絕對位置（已排序）
        eof = False
        while True:
            # 確保緩衝區至少有 max_size bytes，切點判斷才是最終結果
            while not eof and len(buf) < p.max_size:
                block = stream.read(self.read_size)
                if not block:
                    eof = True
                    break
                scanned = len(buf)
                buf += block
                new_s, new_l = self._scan(buf, scanned)
                cand_s = np.concatenate((cand_s, new_s + base))
                cand_l = np.concatenate((cand_l, new_l + base))
            if not buf:
                return
            length = self._find_cut(cand_s, cand_l, base, len(buf))
            yield bytes(buf[:length])
            del buf[:length]
            base += length
            cand_s = cand_s[np.searchsorted(cand_s, base) :]
            cand_l = cand_l[np.searchsorted(cand_l, base) :]

    def _find_cut(self, cand_s: np.ndarray, cand_l: np.ndarray, base: int, n: int) -> int:
        p = self.params
        if n <= p.min_size:
            return n
        limit = min(n, p.max_size)
        normal = min(p.avg_size, limit)
        lo, mid, hi = base + p.min_size - 1, base + normal - 1, base + limit - 1
        i = int(np.searchsorted(cand_s, lo))
        if i < len(cand_s) and cand_s[i] < mid:
            return int(cand_s[i]) - base + 1
        j = int(np.searchsorted(cand_l, mid))
        if j < len(cand_l) and cand_l[j] < hi:
            return int(cand_l[j]) - base + 1
        return limit

    def _scan(self, buf: bytearray, start: int) -> tuple[np.ndarray, np.ndarray]:
        """計算 buf[start:] 每個位置的 W(p)，回傳符合 mask_s 與 mask_l 的相對位置。

        位置 p 若不足 64 bytes 前文，算出的值不正確；但這些位置必定小於
        chunk 起點 + min_size - 1，永遠不會被 _find_cut 採用。
        """
        found_s, found_l = [], []
        for block_start in range(start, len(buf), _SCAN_BLOCK):
            block_end = min(block_start + _SCAN_BLOCK, len(buf))
            ctx = max(0, block_start - (WINDOW - 1))
            window = np.frombuffer(bytes(buf[ctx:block_end]), dtype=np.uint8)
            h = self._rolling_hash(window)[block_start - ctx :]
            found_s.append(np.flatnonzero((h & self._mask_s) == 0) + block_start)
            found_l.append(np.flatnonzero((h & self._mask_l) == 0) + block_start)
        if not found_s:
            empty = np.empty(0, dtype=np.int64)
            return empty, empty
        return np.concatenate(found_s), np.concatenate(found_l)

    def _rolling_hash(self, data: np.ndarray) -> np.ndarray:
        """以 6 次倍增計算 64 bytes 視窗的 gear hash：H_{2k}(i) = H_k(i) + H_k(i-k) << k。"""
        h = self._gear[data]
        shift = 1
        while shift < WINDOW:
            shifted = h.copy()
            shifted[shift:] += h[:-shift] << np.uint64(shift)
            h = shifted
            shift <<= 1
        return h
