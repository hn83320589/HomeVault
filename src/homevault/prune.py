"""移除不再被任何快照引用的資料。

寫入順序（每一步中斷都安全，見 docs/design.md §5.4）::

    ① 取得獨佔鎖，列出現有的 index 檔（之後要刪除的就是這一批）
    ② 走訪所有快照 → 仍在使用的 blob；任何快照讀不到 → 中止，不刪任何東西
    ③ 依使用率把 pack 分成：保留 / 刪除 / 重新打包
    ④ 重新打包：讀出仍在用的 blob，驗證後寫入新 pack（tmp→fsync→rename）
    ⑤ 寫新 index：涵蓋所有保留的 pack
    ⑥ 刪除 ① 列出的舊 index
    ⑦ 刪除不再需要的 pack

⑥ 必須在 ⑦ 之前：若先刪 pack，中斷時舊 index 仍指向已刪除的 pack，
下次備份會以為 blob 已存在而跳過寫入，造成無聲的資料遺失。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homevault.errors import IntegrityError
from homevault.index import Index, PackMap
from homevault.pack import decode_blob, pack_name, read_pack_header
from homevault.repository import Repository

log = logging.getLogger("homevault")

DEFAULT_REPACK_THRESHOLD = 0.2
TMP_MAX_AGE_PRUNE = 3600
_INDEX_FILE_MAX_ENTRIES = 50_000


@dataclass
class PrunePlan:
    keep: list[bytes] = field(default_factory=list)
    delete: list[bytes] = field(default_factory=list)
    repack: list[bytes] = field(default_factory=list)
    used_blobs: int = 0
    unused_blobs: int = 0
    bytes_to_free: int = 0


@dataclass
class PruneResult:
    plan: PrunePlan
    size_before: int = 0
    size_after: int = 0
    packs_written: int = 0


def _repo_data_size(repo: Repository) -> int:
    total = 0
    for directory in ("data", "index"):
        for name in repo.backend.list(directory):
            total += repo.backend.size(name)
    return total


def _all_packs(repo: Repository) -> tuple[PackMap, list[str]]:
    """回傳所有 pack 的內容（含未被索引的 pack）與目前的 index 檔名清單。"""
    existing = repo.list_pack_ids()
    files, broken = repo.read_index_files()
    old_index_names = [*files, *broken]
    packs: PackMap = {}
    for content in files.values():
        for pack_id, entries in content.items():
            if pack_id in existing:
                packs.setdefault(pack_id, entries)
    for pack_id in sorted(existing - packs.keys()):
        try:
            packs[pack_id] = read_pack_header(repo.backend, pack_id, repo.key)
        except IntegrityError as exc:
            log.warning("pack %s 無法解讀，視為垃圾：%s", pack_id.hex()[:16], exc)
            packs[pack_id] = []
    return packs, old_index_names


def plan_prune(
    packs: PackMap, used: set[bytes], threshold: float = DEFAULT_REPACK_THRESHOLD
) -> PrunePlan:
    plan = PrunePlan()
    kept_blobs: set[bytes] = set()
    # 完全在使用中的 pack 優先保留，重複的 blob 才會落在其他 pack 而被回收
    order = sorted(packs, key=lambda p: (-sum(e.id in used for e in packs[p]), p))
    for pack_id in order:
        entries = packs[pack_id]
        needed = [e for e in entries if e.id in used and e.id not in kept_blobs]
        total = sum(e.length for e in entries) or 1
        unused_bytes = total - sum(e.length for e in needed)
        if not needed:
            plan.delete.append(pack_id)
            plan.bytes_to_free += unused_bytes
        elif len(needed) == len(entries) or unused_bytes / total <= threshold:
            plan.keep.append(pack_id)
        else:
            plan.repack.append(pack_id)
            plan.bytes_to_free += unused_bytes
        kept_blobs.update(e.id for e in needed)
        plan.unused_blobs += len(entries) - len(needed)
    plan.used_blobs = len(kept_blobs)
    return plan


def _write_index_files(repo: Repository, packs: PackMap) -> None:
    batch: PackMap = {}
    count = 0
    for pack_id, entries in packs.items():
        batch[pack_id] = entries
        count += len(entries)
        if count >= _INDEX_FILE_MAX_ENTRIES:
            repo.write_index(batch)
            batch, count = {}, 0
    if batch:
        repo.write_index(batch)


def run_prune(
    repo: Repository, *, threshold: float = DEFAULT_REPACK_THRESHOLD, dry_run: bool = False
) -> PruneResult:
    with repo.lock(exclusive=True) as lock:
        # ① 現有 index 與 pack
        packs, old_index_names = _all_packs(repo)
        index = Index()
        for pack_id, entries in packs.items():
            index.add_pack(pack_id, entries)
        repo.index = index
        repo.index_loaded = True

        # ② 仍在使用的 blob；任何一個讀不到就中止
        try:
            used = repo.walk_blobs(s for _sid, s in repo.list_snapshots())
        except IntegrityError as exc:
            raise IntegrityError(
                f"無法讀取快照的目錄資料（{exc}），為避免擴大損失，prune 已中止。"
                "請先執行 `homevault check --read-data`。"
            ) from None
        missing = [b for b in used if b not in index]
        if missing:
            raise IntegrityError(
                f"有 {len(missing)} 個仍被快照引用的區塊不在任何 pack 中，"
                "為避免擴大損失，prune 已中止。請先執行 `homevault check --read-data`。"
            )

        # ③ 分類
        plan = plan_prune(packs, used, threshold)
        result = PruneResult(plan=plan, size_before=_repo_data_size(repo))
        if dry_run or not (plan.delete or plan.repack):
            result.size_after = result.size_before - (plan.bytes_to_free if dry_run else 0)
            return result

        # ④ 重新打包
        kept_blobs = {e.id for p in plan.keep for e in packs[p] if e.id in used}
        stats_before = repo.stats.packs_written
        for pack_id in plan.repack:
            raw = repo.backend.read(pack_name(pack_id))
            for e in packs[pack_id]:
                if e.id not in used or e.id in kept_blobs:
                    continue
                sealed = raw[e.offset : e.offset + e.length]
                decode_blob(repo.key, e.type, e.id, sealed)  # 驗證；損毀時拋出例外並中止
                repo.copy_sealed_blob(e.type, e.id, sealed, e.raw_length)
                kept_blobs.add(e.id)
        repo.flush()  # 新 pack 與其 index
        result.packs_written = repo.stats.packs_written - stats_before

        # ⑤ 保留的舊 pack 寫入新的 index
        _write_index_files(repo, {p: packs[p] for p in plan.keep})

        # ⑥ 刪除舊 index → ⑦ 刪除不需要的 pack（之前確認 lock 仍然有效）
        lock.check_held()
        for name in old_index_names:
            repo.backend.delete(name)
        lock.check_held()
        for pack_id in [*plan.delete, *plan.repack]:
            repo.backend.delete(pack_name(pack_id))
        # 只清除舊的暫存檔：另一台電腦可能正在寫 lock 的暫存檔
        repo.backend.clean_tmp(TMP_MAX_AGE_PRUNE)
        result.size_after = _repo_data_size(repo)
    return result
