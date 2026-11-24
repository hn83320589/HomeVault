"""目錄節點（tree blob）的資料模型與序列化。

Tree blob 是一個目錄底下依名稱排序的節點清單。序列化結果是確定性的，
所以內容相同的目錄一定得到相同的 blob id，未變動的目錄自動去重（Merkle 結構）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from homevault.errors import IntegrityError

FILE = "file"
DIR = "dir"
SYMLINK = "symlink"
_TYPES = {FILE, DIR, SYMLINK}


@dataclass(slots=True)
class Node:
    name: str
    type: str
    mode: int = 0
    mtime_ns: int = 0
    size: int = 0
    chunks: list[bytes] = field(default_factory=list)
    subtree: bytes | None = None
    target: str | None = None
    target_is_dir: bool = False
    # 讀取失敗而沿用舊版本時，記錄該版本是在哪個快照時間讀到的
    stale_since: str | None = None
    # 備份時檔案持續被修改，保存的內容可能不一致
    changing: bool = False

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "name": self.name,
            "type": self.type,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
        }
        if self.type == FILE:
            d["size"] = self.size
            d["chunks"] = [c.hex() for c in self.chunks]
        elif self.type == DIR:
            d["subtree"] = self.subtree.hex() if self.subtree else None
        elif self.type == SYMLINK:
            d["target"] = self.target
            if self.target_is_dir:  # 只在為 True 時寫入，既有 tree 的內容與 id 不變
                d["dir"] = True
        if self.stale_since:
            d["stale_since"] = self.stale_since
        if self.changing:
            d["changing"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Node:
        node_type = d["type"]
        if node_type not in _TYPES:
            raise IntegrityError(f"未知的節點類型：{node_type}")
        name = d["name"]
        if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name:
            # 防止被竄改或損毀的 tree 讓還原寫到目標資料夾之外
            raise IntegrityError(f"不合法的檔名：{name!r}")
        subtree = d.get("subtree")
        return cls(
            name=name,
            type=node_type,
            mode=int(d.get("mode", 0)),
            mtime_ns=int(d.get("mtime_ns", 0)),
            size=int(d.get("size", 0)),
            chunks=[bytes.fromhex(c) for c in d.get("chunks", [])],
            subtree=bytes.fromhex(subtree) if subtree else None,
            target=d.get("target"),
            target_is_dir=bool(d.get("dir", False)),
            stale_since=d.get("stale_since"),
            changing=bool(d.get("changing", False)),
        )


def encode_tree(nodes: list[Node]) -> bytes:
    ordered = sorted(nodes, key=lambda n: n.name)
    # ensure_ascii=True：POSIX 上無法以 UTF-8 解碼的檔名會以 surrogate 表示，
    # 用跳脫序列才能安全序列化並原樣還原
    return json.dumps(
        {"nodes": [n.to_dict() for n in ordered]}, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def decode_tree(raw: bytes) -> list[Node]:
    try:
        doc = json.loads(raw)
        return [Node.from_dict(d) for d in doc["nodes"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise IntegrityError(f"tree 格式損毀：{exc}") from None
