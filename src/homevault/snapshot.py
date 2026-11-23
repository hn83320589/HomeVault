"""快照：某個時間點的備份紀錄，指向根目錄的 tree blob。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from homevault.errors import IntegrityError


@dataclass
class Snapshot:
    time: str  # ISO 8601，含建立時的本地時區 offset
    hostname: str
    paths: list[str]  # 使用者指定的原始來源路徑
    tree: bytes
    parent: bytes | None = None
    tags: list[str] = field(default_factory=list)
    summary: dict[str, object] = field(default_factory=dict)

    @property
    def datetime(self) -> datetime:
        return datetime.fromisoformat(self.time)

    def group_key(self) -> tuple[str, tuple[str, ...]]:
        """保留政策與 parent 選擇的分組依據：同一台電腦、同一組來源路徑。"""
        return self.hostname, tuple(sorted(self.paths))

    def encode(self) -> bytes:
        return json.dumps(
            {
                "version": 1,
                "time": self.time,
                "hostname": self.hostname,
                "paths": self.paths,
                "tree": self.tree.hex(),
                "parent": self.parent.hex() if self.parent else None,
                "tags": self.tags,
                "summary": self.summary,
            },
            ensure_ascii=True,
        ).encode("ascii")

    @classmethod
    def decode(cls, raw: bytes) -> Snapshot:
        try:
            d = json.loads(raw)
            return cls(
                time=d["time"],
                hostname=d["hostname"],
                paths=list(d["paths"]),
                tree=bytes.fromhex(d["tree"]),
                parent=bytes.fromhex(d["parent"]) if d.get("parent") else None,
                tags=list(d.get("tags", [])),
                summary=dict(d.get("summary", {})),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrityError(f"快照格式損毀：{exc}") from None
