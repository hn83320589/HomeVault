"""從快照還原檔案。

* 每個區塊都經過 AES-GCM 驗證與 HMAC 比對。
* 先寫成 ``<名稱>.hv-partial`` 再改名，中斷時不會留下看似完整的半成品。
* 所有檔案寫完後，以後序（子目錄先於父目錄）設定目錄的 mtime 與權限。
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from homevault.errors import HomeVaultError, NotFoundError, RepositoryUnavailableError
from homevault.pack import BlobType
from homevault.platform_utils import IS_WINDOWS
from homevault.repository import Repository
from homevault.snapshot import Snapshot
from homevault.tree import DIR, FILE, SYMLINK, Node

log = logging.getLogger("homevault")

PARTIAL_SUFFIX = ".hv-partial"


@dataclass
class RestoreResult:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def split_snapshot_ref(ref: str) -> tuple[str, str]:
    """'abcd1234:/Users/mom/Pictures' → ('abcd1234', '/Users/mom/Pictures')。"""
    snap, _, path = ref.partition(":")
    return snap, path


def find_node(repo: Repository, snapshot: Snapshot, path: str) -> Node:
    """找出快照內某個路徑對應的節點；空字串或 "/" 代表根目錄。"""
    node = Node("", DIR, subtree=snapshot.tree)
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if parts and len(parts[0]) == 2 and parts[0][1] == ":":
        parts[0] = parts[0][0]  # Windows 磁碟代號：快照內 C:\Users 存成 C/Users
    for part in parts:
        if node.type != DIR or node.subtree is None:
            raise NotFoundError(f"快照中找不到路徑：{path}")
        children = {n.name: n for n in repo.load_tree(node.subtree)}
        if part not in children:
            raise NotFoundError(f"快照中找不到路徑：{path}")
        node = children[part]
    return node


class _Restorer:
    def __init__(self, repo: Repository, overwrite: bool) -> None:
        self.repo = repo
        self.overwrite = overwrite
        self.result = RestoreResult()
        self._dirs_to_finalize: list[tuple[Path, Node]] = []

    def error(self, message: str) -> None:
        log.warning(message)
        self.result.errors.append(message)

    def restore_children(self, tree_id: bytes, dest: Path) -> None:
        for child in self.repo.load_tree(tree_id):
            self.restore_node(child, dest)

    def restore_node(self, node: Node, dest_dir: Path) -> None:
        path = dest_dir / node.name
        try:
            if node.type == DIR:
                path.mkdir(parents=True, exist_ok=True)
                self.result.dirs += 1
                if node.subtree:
                    self.restore_children(node.subtree, path)
                self._dirs_to_finalize.append((path, node))
            elif node.type == FILE:
                self._restore_file(node, path)
            elif node.type == SYMLINK:
                self._restore_symlink(node, path)
        except RepositoryUnavailableError:
            raise  # 備份庫無法存取時中止，不要把每個檔案都記為錯誤
        except HomeVaultError as exc:
            self.error(f"還原 {path} 失敗：{exc}")
        except OSError as exc:
            self.error(f"還原 {path} 失敗：{exc}")

    def _restore_file(self, node: Node, path: Path) -> None:
        if os.path.lexists(path) and not self.overwrite:
            self.result.skipped += 1
            self.error(f"{path} 已存在，略過（使用 --overwrite 覆寫）")
            return
        partial = path.with_name(path.name + PARTIAL_SUFFIX)
        try:
            with open(partial, "wb") as f:
                for chunk_id in node.chunks:
                    f.write(self.repo.load_blob(BlobType.DATA, chunk_id))
            if os.path.lexists(path):
                _make_writable(path)
            os.replace(partial, path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        self.result.files += 1
        self.result.bytes += node.size
        try:
            os.utime(path, ns=(node.mtime_ns, node.mtime_ns))
            _apply_mode(path, node.mode)
        except OSError as exc:
            self.error(f"{path} 已還原，但無法設定修改時間或權限：{exc}")

    def _restore_symlink(self, node: Node, path: Path) -> None:
        if os.path.lexists(path):
            if not self.overwrite:
                self.result.skipped += 1
                self.error(f"{path} 已存在，略過（使用 --overwrite 覆寫）")
                return
            path.unlink()
        os.symlink(node.target or "", path, target_is_directory=node.target_is_dir)
        if os.utime in os.supports_follow_symlinks:
            os.utime(path, ns=(node.mtime_ns, node.mtime_ns), follow_symlinks=False)
        self.result.symlinks += 1

    def finalize_dirs(self) -> None:
        # 清單是後序：子目錄一定排在父目錄之前，先設定子目錄才不會改動父目錄的 mtime
        for path, node in self._dirs_to_finalize:
            try:
                os.utime(path, ns=(node.mtime_ns, node.mtime_ns))
                _apply_mode(path, node.mode)
            except OSError as exc:
                self.error(f"無法設定 {path} 的時間或權限：{exc}")


def _apply_mode(path: Path, mode: int) -> None:
    if not mode:
        return
    if IS_WINDOWS:
        # Windows 只支援唯讀屬性
        os.chmod(path, stat.S_IREAD if not mode & stat.S_IWUSR else stat.S_IREAD | stat.S_IWRITE)
    else:
        os.chmod(path, stat.S_IMODE(mode))


def _make_writable(path: Path) -> None:
    # 覆寫唯讀檔案前先解除唯讀；失敗時交由後續的 os.replace 回報錯誤
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def run_restore(
    repo: Repository,
    snapshot: Snapshot,
    subpath: str,
    target: str | Path,
    *,
    overwrite: bool = False,
) -> RestoreResult:
    """還原快照（或其中的資料夾、單一檔案）到 target。

    * 未指定 subpath：還原整個快照，保留完整路徑結構。
    * 指定 subpath：把該資料夾或檔案本身放到 target 底下。
    """
    # 共享鎖：避免另一台電腦同時 prune，還原到一半才發現資料被移走
    with repo.lock(exclusive=False):
        repo.load_index(recover_orphans=False)
        node = find_node(repo, snapshot, subpath)
        target_path = Path(target)
        target_path.mkdir(parents=True, exist_ok=True)
        restorer = _Restorer(repo, overwrite)
        if node.name == "":
            restorer.restore_children(node.subtree or b"", target_path)
        else:
            restorer.restore_node(node, target_path)
        restorer.finalize_dirs()
    return restorer.result
