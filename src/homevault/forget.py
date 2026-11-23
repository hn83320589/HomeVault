"""依保留政策刪除舊快照。

forget 只刪除 snapshot 檔，每個刪除各自獨立；中斷時只會刪掉一部分，
而保留政策的冪等性保證重跑會得到相同結果。實際釋放空間由 prune 負責。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from homevault.errors import UsageError
from homevault.repository import Repository
from homevault.retention import KeepPolicy, apply_policy
from homevault.snapshot import Snapshot


@dataclass
class ForgetGroup:
    hostname: str
    paths: tuple[str, ...]
    keep: list[tuple[bytes, Snapshot, list[str]]] = field(default_factory=list)
    remove: list[tuple[bytes, Snapshot]] = field(default_factory=list)


def plan_forget(snapshots: list[tuple[bytes, Snapshot]], policy: KeepPolicy) -> list[ForgetGroup]:
    """依「主機 + 來源路徑」分組後套用保留政策，兩台電腦的快照各自保留。"""
    groups: dict[tuple[str, tuple[str, ...]], list[tuple[bytes, Snapshot]]] = defaultdict(list)
    for sid, snap in snapshots:
        groups[snap.group_key()].append((sid, snap))
    plans = []
    for (hostname, paths), members in sorted(groups.items()):
        by_id = dict(members)
        keep, remove, reasons = apply_policy([(sid, s.datetime) for sid, s in members], policy)
        plans.append(
            ForgetGroup(
                hostname=hostname,
                paths=paths,
                keep=[(sid, by_id[sid], reasons[sid]) for sid in keep],
                remove=[(sid, by_id[sid]) for sid in remove],
            )
        )
    return plans


def run_forget(repo: Repository, policy: KeepPolicy, *, dry_run: bool = False) -> list[ForgetGroup]:
    if policy.is_empty():
        raise UsageError("請至少指定一條保留規則（例如 --keep-daily 7），避免誤刪所有快照")
    with repo.lock(exclusive=True) as lock:
        plans = plan_forget(repo.list_snapshots(), policy)
        if not dry_run:
            lock.check_held()
            for group in plans:
                for sid, _snap in group.remove:
                    repo.delete_snapshot(sid)
    return plans


def forget_ids(repo: Repository, snapshot_refs: list[str]) -> list[bytes]:
    """刪除指定的快照。"""
    with repo.lock(exclusive=True):
        ids = [repo.find_snapshot(ref)[0] for ref in snapshot_refs]
        for sid in ids:
            repo.delete_snapshot(sid)
    return ids
