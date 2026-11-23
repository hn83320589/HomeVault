"""比較兩個快照的差異。

兩棵 tree 同步走訪，子樹 id 相同時代表內容完全一樣，直接跳過（Merkle 結構的好處）。
"""

from __future__ import annotations

from dataclasses import dataclass

from homevault.repository import Repository
from homevault.snapshot import Snapshot
from homevault.tree import DIR, Node

ADDED = "+"
REMOVED = "-"
MODIFIED = "M"


@dataclass(frozen=True)
class DiffEntry:
    kind: str
    path: str
    is_dir: bool = False


def _node_changed(a: Node, b: Node) -> bool:
    return (a.size, a.chunks, a.target, a.mtime_ns, a.mode) != (
        b.size,
        b.chunks,
        b.target,
        b.mtime_ns,
        b.mode,
    )


class _Differ:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.entries: list[DiffEntry] = []

    def _all(self, kind: str, node: Node, path: str) -> None:
        self.entries.append(DiffEntry(kind, path, node.type == DIR))
        if node.type == DIR and node.subtree:
            for child in self.repo.load_tree(node.subtree):
                self._all(kind, child, f"{path}/{child.name}")

    def trees(self, a_id: bytes | None, b_id: bytes | None, prefix: str) -> None:
        if a_id == b_id:
            return
        a = {n.name: n for n in self.repo.load_tree(a_id)} if a_id else {}
        b = {n.name: n for n in self.repo.load_tree(b_id)} if b_id else {}
        for name in sorted(a.keys() | b.keys()):
            path = f"{prefix}/{name}"
            na, nb = a.get(name), b.get(name)
            if na is None:
                self._all(ADDED, nb, path)
            elif nb is None:
                self._all(REMOVED, na, path)
            elif na.type != nb.type:
                self._all(REMOVED, na, path)
                self._all(ADDED, nb, path)
            elif na.type == DIR:
                self.trees(na.subtree, nb.subtree, path)
            elif _node_changed(na, nb):
                self.entries.append(DiffEntry(MODIFIED, path))


def diff_snapshots(repo: Repository, a: Snapshot, b: Snapshot) -> list[DiffEntry]:
    if not repo.index_loaded:
        repo.load_index(recover_orphans=False)
    differ = _Differ(repo)
    differ.trees(a.tree, b.tree, "")
    return differ.entries
