"""備份庫統計：總大小、原始資料量、每個快照的獨有資料量。"""

from __future__ import annotations

from dataclasses import dataclass, field

from homevault.repository import Repository
from homevault.snapshot import Snapshot

_SHARED = -1


@dataclass
class SnapshotStats:
    snapshot_id: bytes
    snapshot: Snapshot
    logical_size: int
    unique_stored: int = 0  # 只被這個快照引用的資料，刪除它並 prune 後約可回收的空間


@dataclass
class RepoStats:
    repo_size: int = 0  # 備份庫所有檔案的實際大小
    pack_bytes: int = 0
    total_logical: int = 0  # 所有快照的原始資料量合計（未去重）
    unique_raw: int = 0  # 去重後的原始資料量
    unique_stored: int = 0  # 去重、壓縮、加密後的大小
    blob_count: int = 0
    snapshots: list[SnapshotStats] = field(default_factory=list)

    @property
    def space_saving(self) -> float:
        return 1 - self.unique_stored / self.total_logical if self.total_logical else 0.0


def _snapshot_blobs(repo: Repository, snap: Snapshot) -> set[bytes]:
    return repo.walk_blobs([snap])


def run_stats(repo: Repository) -> RepoStats:
    with repo.lock(exclusive=False):
        return _collect(repo)


def _collect(repo: Repository) -> RepoStats:
    repo.load_index(recover_orphans=False)
    stats = RepoStats()
    for directory in ("data", "index", "snapshots", "keys", "locks"):
        for name in repo.backend.list(directory):
            size = repo.backend.size(name)
            stats.repo_size += size
            if directory == "data":
                stats.pack_bytes += size
    stats.repo_size += repo.backend.size("config")

    # 每個 blob 的擁有者：唯一引用它的快照編號，或 _SHARED
    owner: dict[bytes, int] = {}
    snaps = repo.list_snapshots()
    for i, (sid, snap) in enumerate(snaps):
        for blob_id in _snapshot_blobs(repo, snap):
            owner[blob_id] = i if owner.get(blob_id, i) == i else _SHARED
        logical = int(snap.summary.get("total_bytes", 0))
        stats.snapshots.append(SnapshotStats(sid, snap, logical))
        stats.total_logical += logical

    for blob_id, who in owner.items():
        entry = repo.index.get(blob_id)
        if entry is None:
            continue
        stats.blob_count += 1
        stats.unique_raw += entry.raw_length
        stats.unique_stored += entry.length
        if who != _SHARED:
            stats.snapshots[who].unique_stored += entry.length
    return stats
