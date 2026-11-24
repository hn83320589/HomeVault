"""建立快照。

流程：取得共享鎖 → 載入 index（含中斷復原）→ 走訪來源、切塊存入 pack
→ 由下而上寫 tree → flush pack 與 index → 寫 snapshot（commit point）。

增量判斷：檔案的 type、size、mtime_ns 都與 parent 快照相同時，直接沿用
parent 的區塊清單，完全不開啟檔案。
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath
from typing import BinaryIO

from homevault.chunker import Chunker
from homevault.errors import IntegrityError, UsageError
from homevault.pack import BlobType
from homevault.repository import TMP_MAX_AGE, Repository
from homevault.snapshot import Snapshot
from homevault.tree import DIR, FILE, SYMLINK, Node

log = logging.getLogger("homevault")

Opener = Callable[[str], BinaryIO]


def _default_opener(path: str) -> BinaryIO:
    return open(path, "rb")


class BackupCancelledError(Exception):
    """備份被中斷，工作執行緒提早結束。"""


@dataclass
class BackupOptions:
    excludes: list[str] = field(default_factory=list)
    full_scan: bool = False
    hostname: str | None = None
    tags: list[str] = field(default_factory=list)
    threads: int = 4
    opener: Opener = _default_opener  # 測試替身可藉此計算實際開啟了哪些檔案


@dataclass
class Progress:
    """備份進度。由多個執行緒更新，由 CLI 的顯示執行緒讀取。"""

    files_done: int = 0
    bytes_done: int = 0
    bytes_read: int = 0
    files_read: int = 0
    stored_new: int = 0
    raw_new: int = 0
    started: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_file(self, size: int, *, was_read: bool = False) -> None:
        with self._lock:
            self.files_done += 1
            self.bytes_done += size
            if was_read:
                self.files_read += 1

    def add_read(self, raw: int, stored_new: int, new: bool) -> None:
        with self._lock:
            self.bytes_read += raw
            if new:
                self.raw_new += raw
                self.stored_new += stored_new

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def dedup_ratio(self) -> float:
        """已讀取的資料中，因為已存在而不必寫入的比例。"""
        return 1 - self.raw_new / self.bytes_read if self.bytes_read else 0.0


@dataclass
class BackupResult:
    snapshot_id: bytes
    snapshot: Snapshot
    progress: Progress
    files_new: int = 0
    files_changed: int = 0
    files_unmodified: int = 0
    dirs: int = 0
    errors: list[str] = field(default_factory=list)  # 無法備份、也沒有舊版本可沿用
    stale: list[tuple[str, str]] = field(default_factory=list)  # (路徑, 沿用的版本時間)
    changing: list[str] = field(default_factory=list)  # 備份時持續變動的檔案

    @property
    def has_problems(self) -> bool:
        return bool(self.errors or self.stale or self.changing)


class ExcludeMatcher:
    """排除規則（gitignore 風格的子集，不分大小寫）。

    * ``*.tmp``、``node_modules``：比對檔名（任何層級）
    * ``photos/cache``、``/build``：含 ``/`` 時比對相對於來源資料夾的路徑
    * 結尾加 ``/``（如 ``cache/``）：只比對資料夾
    """

    def __init__(self, patterns: list[str]) -> None:
        self._rules: list[tuple[str, bool, bool]] = []
        for raw in patterns:
            p = raw.strip().replace("\\", "/")
            if not p or p.startswith("#"):
                continue
            dir_only = p.endswith("/")
            p = p.rstrip("/")
            anchored = "/" in p
            self._rules.append((p.lstrip("/").lower(), anchored, dir_only))

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        rel = rel_path.lower()
        name = rel.rsplit("/", 1)[-1]
        for pattern, anchored, dir_only in self._rules:
            if dir_only and not is_dir:
                continue
            target = rel if anchored else name
            if fnmatch.fnmatchcase(target, pattern):
                return True
            if anchored and pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:]):
                return True
        return False


def path_components(path: str) -> list[str]:
    """把絕對路徑轉成快照內的路徑元件。C:\\Users\\Mom → ["C", "Users", "Mom"]。"""
    pure = PurePath(path)
    parts = list(pure.parts)
    anchor = parts.pop(0) if pure.anchor else ""
    head = anchor.replace("\\", "/").strip("/").replace(":", "")
    if head:
        parts = [p for p in head.split("/") if p] + parts
    return parts


class _Archiver:
    def __init__(
        self,
        repo: Repository,
        options: BackupOptions,
        progress: Progress,
        parent_time: str | None = None,
    ) -> None:
        self.repo = repo
        self.parent_time = parent_time
        self.opt = options
        self.progress = progress
        self.chunker = Chunker(repo.key.chunker_seed, repo.config.chunker)
        self.excludes = ExcludeMatcher(options.excludes)
        self.repo_root = os.path.normcase(os.path.abspath(repo.backend.root))
        self.result_errors: list[str] = []
        self.stale: list[tuple[str, str]] = []
        self.changing: list[str] = []
        self.counts = {"new": 0, "changed": 0, "unmodified": 0, "dirs": 0, "stale": 0}
        self._counts_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(1, options.threads))
        self._slots = threading.BoundedSemaphore(max(1, options.threads) * 2)

    def cancel(self) -> None:
        """中斷（例如 Ctrl+C）時讓執行中的工作在下一個區塊就停止，不必等整個檔案讀完。"""
        self._cancelled.set()

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _count(self, key: str) -> None:
        with self._counts_lock:
            self.counts[key] += 1

    def _error(self, message: str) -> None:
        log.warning(message)
        with self._counts_lock:
            self.result_errors.append(message)

    def _reuse_previous(
        self, path: str, name: str, parent: Node | None, reason: str
    ) -> Node | None:
        """讀取失敗時沿用上一個快照的版本，避免檔案或資料夾從快照中消失。

        例如每晚 11 點都開著的 Outlook 資料檔：若直接略過，它會從之後的每個快照
        消失，舊快照被 forget 之後就完全沒有備份。沿用舊版本讓最新快照持續引用它。
        """
        usable = (
            parent is not None
            and (parent.type != FILE or all(self.repo.has_blob(c) for c in parent.chunks))
            and (parent.type != DIR or (parent.subtree and self.repo.has_blob(parent.subtree)))
        )
        if not usable:
            self._error(f"無法讀取 {path}：{reason}（從未成功備份過，本次略過）")
            return None
        since = parent.stale_since or self.parent_time or "未知時間"
        log.warning("無法讀取 %s：%s（沿用 %s 的版本）", path, reason, since)
        with self._counts_lock:
            self.stale.append((path, since))
            self.counts["stale"] += 1
        return dataclasses.replace(parent, name=name, stale_since=since)

    # ── 走訪 ─────────────────────────────────────────────

    def backup_root(self, sources: list[str], parent_nodes: list[Node] | None) -> bytes:
        """把多個來源組合成一棵以完整路徑為結構的 tree，回傳根 tree id。"""
        layout: dict = {}
        for src in sources:
            cursor = layout
            comps = path_components(src)
            for comp in comps[:-1]:
                cursor = cursor.setdefault(comp, {})
            cursor[comps[-1]] = src
        return self._virtual_dir(layout, "", parent_nodes)

    def _virtual_dir(
        self, layout: dict, real_prefix: str, parent_nodes: list[Node] | None
    ) -> bytes:
        nodes = []
        parent_map = {n.name: n for n in parent_nodes or []}
        for name, value in layout.items():
            parent = parent_map.get(name)
            if isinstance(value, str):
                node = self._source_node(value, name, parent)
            else:
                real = self._prefix_path(real_prefix, name)
                try:
                    st = os.stat(real)
                    mode, mtime_ns = st.st_mode, st.st_mtime_ns
                except OSError:  # 例如 UNC 路徑的 server 層級無法 stat
                    mode, mtime_ns = stat.S_IFDIR | 0o755, 0
                sub_parent = self._subtree_nodes(parent)
                node = Node(name, DIR, mode=mode, mtime_ns=mtime_ns)
                node.subtree = self._virtual_dir(value, real, sub_parent)
            if node:
                nodes.append(node)
        return self.repo.save_tree(nodes)

    @staticmethod
    def _prefix_path(prefix: str, name: str) -> str:
        if not prefix:
            return f"{name}:\\" if os.name == "nt" else f"/{name}"
        return os.path.join(prefix, name)

    def _source_node(self, path: str, name: str, parent: Node | None) -> Node | None:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return self._symlink_node(path, name, st, parent)
        if stat.S_ISDIR(st.st_mode):
            return self._dir_node(path, name, st, "", parent)
        future = self._file_future(path, name, st, parent)
        return future.result() if future else None

    def _subtree_nodes(self, parent: Node | None) -> list[Node] | None:
        if parent is None or parent.type != DIR or parent.subtree is None:
            return None
        try:
            return self.repo.load_tree(parent.subtree)
        except Exception as exc:
            log.warning("無法讀取上一個快照的目錄資料，改為完整讀取：%s", exc)
            return None

    def _dir_node(
        self, path: str, name: str, st: os.stat_result, rel: str, parent: Node | None
    ) -> Node:
        self._count("dirs")
        parent_map = {n.name: n for n in self._subtree_nodes(parent) or []}
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            reused = self._reuse_previous(path, name, parent, str(exc))
            if reused:
                return reused
            entries = []

        pending: list[Future[Node | None]] = []
        nodes: list[Node] = []
        subdirs = []
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as exc:
                reused = self._reuse_previous(
                    entry.path, entry.name, parent_map.get(entry.name), str(exc)
                )
                if reused:
                    nodes.append(reused)
                continue
            is_link = stat.S_ISLNK(est.st_mode) or _is_junction(entry)
            is_dir = stat.S_ISDIR(est.st_mode) and not is_link
            if self.excludes.matches(child_rel, is_dir):
                continue
            if is_dir and os.path.normcase(os.path.abspath(entry.path)) == self.repo_root:
                continue  # 備份庫位於來源資料夾內時，不可把備份庫備份進自己
            child_parent = parent_map.get(entry.name)
            if is_link:
                node = self._symlink_node(entry.path, entry.name, est, child_parent)
                if node:
                    nodes.append(node)
            elif is_dir:
                subdirs.append((entry, est, child_rel, child_parent))
            elif stat.S_ISREG(est.st_mode):
                future = self._file_future(entry.path, entry.name, est, child_parent)
                if future:
                    pending.append(future)
            # 其他類型（socket、裝置檔）不備份

        # 先把本層檔案交給執行緒處理，再遞迴子資料夾，讓 I/O 與 CPU 重疊
        for entry, est, child_rel, child_parent in subdirs:
            nodes.append(self._dir_node(entry.path, entry.name, est, child_rel, child_parent))
        for future in pending:
            node = future.result()
            if node:
                nodes.append(node)
        tree_id = self.repo.save_tree(nodes)
        return Node(name, DIR, mode=st.st_mode, mtime_ns=st.st_mtime_ns, subtree=tree_id)

    def _symlink_node(
        self, path: str, name: str, st: os.stat_result, parent: Node | None = None
    ) -> Node | None:
        try:
            target = os.readlink(path)
        except OSError as exc:
            return self._reuse_previous(path, name, parent, str(exc))
        return Node(
            name,
            SYMLINK,
            mode=st.st_mode,
            mtime_ns=st.st_mtime_ns,
            target=target,
            target_is_dir=os.path.isdir(path),  # Windows 還原時需要區分目錄連結
        )

    # ── 檔案 ─────────────────────────────────────────────

    def _file_future(
        self, path: str, name: str, st: os.stat_result, parent: Node | None
    ) -> Future[Node | None] | None:
        if (
            not self.opt.full_scan
            and parent is not None
            and parent.type == FILE
            and parent.size == st.st_size
            and parent.mtime_ns == st.st_mtime_ns
            and not parent.changing  # 上次讀到的內容可能不一致，必須重讀
            and all(self.repo.has_blob(c) for c in parent.chunks)
        ):
            self._count("unmodified")
            self.progress.add_file(st.st_size)
            done: Future[Node | None] = Future()
            done.set_result(
                Node(
                    name,
                    FILE,
                    mode=st.st_mode,
                    mtime_ns=st.st_mtime_ns,
                    size=parent.size,
                    chunks=list(parent.chunks),
                )
            )
            return done
        self._slots.acquire()
        try:
            future = self._pool.submit(self._store_file, path, name, st, parent)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _f: self._slots.release())
        return future

    def _read_chunks(self, path: str) -> tuple[list[bytes], int]:
        chunks: list[bytes] = []
        size = 0
        with self.opt.opener(path) as f:
            for chunk in self.chunker.iter_chunks(f):
                if self._cancelled.is_set():
                    raise BackupCancelledError
                blob_id, stored = self.repo.save_blob(BlobType.DATA, chunk)
                chunks.append(blob_id)
                size += len(chunk)
                self.progress.add_read(len(chunk), stored, stored > 0)
        return chunks, size

    def _store_file(
        self, path: str, name: str, st: os.stat_result, parent: Node | None
    ) -> Node | None:
        """讀取檔案。讀取前後的 size 與 mtime 不同代表讀取期間被修改，重讀一次；
        仍然在變動就保存讀到的內容並標記 changing，下次備份一定重讀。"""
        changing = False
        for attempt in range(2):
            try:
                chunks, size = self._read_chunks(path)
            except OSError as exc:
                return self._reuse_previous(path, name, parent, str(exc))
            try:
                after = os.stat(path)
            except OSError:
                break  # 讀完後檔案被刪除：保存讀到的內容
            if after.st_mtime_ns == st.st_mtime_ns and after.st_size == size:
                break
            if attempt == 0:
                log.info("檔案在讀取期間被修改，重新讀取：%s", path)
                st = after
            else:
                changing = True
        if changing:
            log.warning("檔案在備份過程中持續被修改：%s（保存的內容可能不一致）", path)
            with self._counts_lock:
                self.changing.append(path)
        self._count("changed" if parent is not None else "new")
        self.progress.add_file(size, was_read=True)
        return Node(
            name,
            FILE,
            mode=st.st_mode,
            mtime_ns=st.st_mtime_ns,
            size=size,
            chunks=chunks,
            changing=changing,
        )


def _is_junction(entry: os.DirEntry[str]) -> bool:
    # Windows 的 junction（例如 AppData 裡的 "Application Data"）會指回上層，不可跟隨
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction and is_junction())


def find_parent(repo: Repository, hostname: str, paths: list[str]) -> tuple[bytes, Snapshot] | None:
    key = (hostname, tuple(sorted(paths)))
    candidates = [s for s in repo.list_snapshots() if s[1].group_key() == key]
    return candidates[-1] if candidates else None


def normalize_sources(sources: list[str]) -> list[str]:
    if not sources:
        raise UsageError("請指定至少一個要備份的資料夾")
    result = []
    for src in sources:
        path = os.path.abspath(src)
        if not os.path.lexists(path):
            raise UsageError(f"來源不存在：{src}")
        result.append(path)
    comps = [path_components(p) for p in result]
    for i, a in enumerate(comps):
        for j, b in enumerate(comps):
            if i != j and a == b[: len(a)]:
                raise UsageError(f"來源路徑重疊：{result[i]} 包含 {result[j]}")
    return result


def run_backup(
    repo: Repository,
    sources: list[str],
    options: BackupOptions | None = None,
    progress: Progress | None = None,
) -> BackupResult:
    options = options or BackupOptions()
    progress = progress or Progress()
    paths = normalize_sources(sources)
    hostname = options.hostname or socket.gethostname()
    with repo.lock(exclusive=False) as lock:
        repo.backend.clean_tmp(TMP_MAX_AGE)
        recovered = repo.load_index(recover_orphans=True)
        if recovered:
            log.info("沿用上次中斷時已寫入的 %d 個 pack", len(recovered))
        stats_before = dataclasses.replace(repo.stats)
        parent = find_parent(repo, hostname, paths)
        parent_root = None
        if parent:
            try:
                parent_root = repo.load_tree(parent[1].tree)
            except IntegrityError as exc:
                log.warning("上一個快照的目錄資料無法讀取，本次改為完整讀取：%s", exc)

        archiver = _Archiver(repo, options, progress, parent[1].time if parent else None)
        try:
            root_tree = archiver.backup_root(paths, parent_root)
        except BaseException:
            archiver.cancel()
            raise
        finally:
            archiver.close()
        lock.check_held()
        repo.flush()
        lock.check_held()  # 寫 pack 與 index 期間也可能失去 lock

        snapshot = Snapshot(
            time=datetime.now().astimezone().isoformat(),
            hostname=hostname,
            paths=paths,
            tree=root_tree,
            parent=parent[0] if parent else None,
            tags=list(options.tags),
            summary={
                "files_new": archiver.counts["new"],
                "files_changed": archiver.counts["changed"],
                "files_unmodified": archiver.counts["unmodified"],
                "dirs": archiver.counts["dirs"],
                "total_bytes": progress.bytes_done,
                "bytes_read": progress.bytes_read,
                "data_added": repo.stats.raw_bytes_added - stats_before.raw_bytes_added,
                "data_added_stored": (
                    repo.stats.stored_bytes_added - stats_before.stored_bytes_added
                ),
                "errors": len(archiver.result_errors),
                "files_stale": len(archiver.stale),
                "files_changing": len(archiver.changing),
                # 只保留前 100 筆，避免大量無法讀取的檔案讓快照檔過大
                "stale_files": [{"path": p, "since": t} for p, t in archiver.stale[:100]],
                "changing_files": archiver.changing[:100],
                "duration": round(progress.elapsed, 3),
            },
        )
        lock.check_held()
        snap_id = repo.save_snapshot(snapshot)  # ← commit point
    return BackupResult(
        snapshot_id=snap_id,
        snapshot=snapshot,
        progress=progress,
        files_new=archiver.counts["new"],
        files_changed=archiver.counts["changed"],
        files_unmodified=archiver.counts["unmodified"],
        dirs=archiver.counts["dirs"],
        errors=archiver.result_errors,
        stale=archiver.stale,
        changing=archiver.changing,
    )
