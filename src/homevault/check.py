"""備份庫完整性檢查。

* 結構檢查（預設）：index 可解密、index 指到的 pack 存在且 header 一致、
  所有快照的 tree 可讀、引用的 blob 都在 index 中。
* ``read_data``：讀取每個 pack，驗證 SHA-256 等於檔名，並逐一解密每個 blob、比對 HMAC。

發現損毀時，回報引用到損毀 blob 的「快照 → 檔案路徑」。
中斷造成的殘留（未被索引的 pack、tmp 暫存檔）只列為資訊，不算錯誤。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from homevault.errors import IntegrityError
from homevault.index import Index
from homevault.pack import PackEntry, decode_blob, pack_name, read_pack_header
from homevault.repository import Repository
from homevault.snapshot import Snapshot
from homevault.storage import TMP_DIR

log = logging.getLogger("homevault")


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    damaged_blobs: set[bytes] = field(default_factory=set)
    # 快照 id → 受影響的路徑
    affected: dict[bytes, list[str]] = field(default_factory=dict)
    snapshots: dict[bytes, Snapshot] = field(default_factory=dict)
    blobs_verified: int = 0
    packs_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


class _Checker:
    def __init__(self, repo: Repository, progress: Callable[[str], None] | None) -> None:
        self.repo = repo
        self.r = CheckResult()
        self.progress = progress or (lambda _msg: None)
        self.pack_entries: dict[bytes, list[PackEntry]] = {}

    def error(self, message: str) -> None:
        log.debug(message)
        self.r.errors.append(message)

    # ── 結構 ─────────────────────────────────────────────

    def check_index_and_packs(self) -> None:
        self.progress("檢查 index 與 pack")
        existing = self.repo.list_pack_ids()
        files, broken = self.repo.read_index_files()
        for name in broken:
            self.error(f"index 檔 {name} 無法解密或格式損毀")
        index = Index()
        for name, packs in files.items():
            for pack_id, entries in packs.items():
                if pack_id not in existing:
                    self.error(f"index {name[-12:]} 引用的 pack {pack_id.hex()[:16]} 不存在")
                    self.r.damaged_blobs.update(e.id for e in entries)
                    continue
                index.add_pack(pack_id, entries)
                self.pack_entries.setdefault(pack_id, entries)
        # 同一個 blob 若在其他完好的 pack 中還有一份，就不算損毀
        self.r.damaged_blobs = {b for b in self.r.damaged_blobs if b not in index}
        self.repo.index = index
        self.repo.index_loaded = True

        for pack_id, entries in self.pack_entries.items():
            try:
                header = read_pack_header(self.repo.backend, pack_id, self.repo.key)
            except IntegrityError as exc:
                self.error(f"pack {pack_id.hex()[:16]} 的 header 損毀：{exc}")
                continue
            if sorted(header, key=lambda e: e.offset) != sorted(entries, key=lambda e: e.offset):
                self.error(f"pack {pack_id.hex()[:16]} 的 header 與 index 記錄不一致")
        orphans = existing - self.pack_entries.keys()
        if orphans:
            self.r.infos.append(
                f"有 {len(orphans)} 個未被索引的 pack（通常是中斷的備份留下的），"
                "下次備份會沿用，或以 prune 清除"
            )
        tmp_files = self.repo.backend.list(TMP_DIR)
        if tmp_files:
            self.r.infos.append(f"tmp/ 中有 {len(tmp_files)} 個中斷殘留的暫存檔")

    def check_snapshots(self) -> None:
        self.progress("檢查快照與目錄結構")
        checked_trees: set[bytes] = set()
        for snap_id in self.repo.list_snapshot_ids():
            try:
                snap = self.repo.load_snapshot(snap_id)
            except IntegrityError as exc:
                self.error(f"快照 {snap_id.hex()[:8]} 無法讀取：{exc}")
                continue
            self.r.snapshots[snap_id] = snap
            stack = [snap.tree]
            while stack:
                tree_id = stack.pop()
                if tree_id in checked_trees:
                    continue
                checked_trees.add(tree_id)
                if tree_id not in self.repo.index:
                    self._missing(tree_id, "目錄資料（tree）")
                    continue
                try:
                    nodes = self.repo.load_tree(tree_id)
                except IntegrityError as exc:
                    self.error(f"tree {tree_id.hex()[:16]} 無法讀取：{exc}")
                    self.r.damaged_blobs.add(tree_id)
                    continue
                for node in nodes:
                    if node.subtree:
                        stack.append(node.subtree)
                    for chunk in node.chunks:
                        if chunk not in self.repo.index:
                            self._missing(chunk, "資料區塊")

    def _missing(self, blob_id: bytes, what: str) -> None:
        if blob_id not in self.r.damaged_blobs:
            self.error(f"{what} {blob_id.hex()[:16]} 不在任何 pack 中")
        self.r.damaged_blobs.add(blob_id)

    # ── 讀取資料 ─────────────────────────────────────────

    def read_data(self) -> None:
        total = len(self.pack_entries)
        for n, (pack_id, entries) in enumerate(sorted(self.pack_entries.items()), 1):
            self.progress(f"讀取並驗證 pack {n}/{total}")
            try:
                raw = self.repo.backend.read(pack_name(pack_id))
            except IntegrityError as exc:
                self.error(f"pack {pack_id.hex()[:16]} 無法讀取：{exc}")
                self.r.damaged_blobs.update(e.id for e in entries)
                continue
            self.r.packs_checked += 1
            if hashlib.sha256(raw).digest() != pack_id:
                self.error(f"pack {pack_id.hex()[:16]} 的內容與檔名雜湊不符（檔案已損毀）")
            for e in entries:
                sealed = raw[e.offset : e.offset + e.length]
                try:
                    decode_blob(self.repo.key, e.type, e.id, sealed)
                    self.r.blobs_verified += 1
                except IntegrityError as exc:
                    self.error(f"pack {pack_id.hex()[:16]} 中的 blob {e.id.hex()[:16]} 損毀：{exc}")
                    self.r.damaged_blobs.add(e.id)

    # ── 受影響範圍 ───────────────────────────────────────

    def map_affected(self) -> None:
        damaged = self.r.damaged_blobs
        if not damaged:
            return
        memo: dict[bytes, list[str]] = {}

        def damaged_paths(tree_id: bytes) -> list[str]:
            """回傳 tree 底下受影響的相對路徑；以 tree id 快取，共用子樹只計算一次。"""
            if tree_id in memo:
                return memo[tree_id]
            memo[tree_id] = []  # 防止損毀資料造成循環
            if tree_id in damaged:
                result = ["（此目錄的內容無法讀取）"]
            else:
                result = []
                try:
                    nodes = self.repo.load_tree(tree_id)
                except IntegrityError:
                    nodes = []
                    result.append("（此目錄的內容無法讀取）")
                for node in nodes:
                    if any(c in damaged for c in node.chunks):
                        result.append(node.name)
                    if node.subtree:
                        result.extend(f"{node.name}/{p}" for p in damaged_paths(node.subtree))
            memo[tree_id] = result
            return result

        for snap_id, snap in self.r.snapshots.items():
            paths = damaged_paths(snap.tree)
            if paths:
                self.r.affected[snap_id] = ["/" + p for p in paths]


def run_check(
    repo: Repository, *, read_data: bool = False, progress: Callable[[str], None] | None = None
) -> CheckResult:
    """檢查備份庫。會取得共享鎖，避免與 prune 同時執行而看到過渡狀態。"""
    with repo.lock(exclusive=False):
        checker = _Checker(repo, progress)
        checker.check_index_and_packs()
        checker.check_snapshots()
        if read_data:
            checker.read_data()
        checker.map_affected()
    return checker.r
