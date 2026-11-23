"""保留政策（純函式，不碰檔案系統）。

每條規則有自己的「時間桶」：
    hourly = 年月日時、daily = 年月日、weekly = ISO 年週、monthly = 年月、yearly = 年

由新到舊掃描，遇到新的時間桶且該規則配額 > 0，就保留這個快照（它是該桶最新的一個）。
「最近 7 天」指「有快照的最近 7 個日子」，所以電腦關機一段時間後，舊快照不會一次被刪光。
時間桶以快照建立時的本地時間計算。

性質：若 K = policy(S)，則對任何 K ⊆ S' ⊆ S，policy(S') = K。
所以 forget 中途中斷再重跑，結果完全相同。
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime

from homevault.errors import UsageError


@dataclass(frozen=True)
class KeepPolicy:
    last: int = 0
    hourly: int = 0
    daily: int = 0
    weekly: int = 0
    monthly: int = 0
    yearly: int = 0

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) <= 0 for f in fields(self))

    def rules(self) -> list[tuple[str, int]]:
        return [(f.name, getattr(self, f.name)) for f in fields(self) if getattr(self, f.name) > 0]


def bucket_key(rule: str, when: datetime) -> tuple[int, ...]:
    if rule == "hourly":
        return (when.year, when.month, when.day, when.hour)
    if rule == "daily":
        return (when.year, when.month, when.day)
    if rule == "weekly":
        iso = when.isocalendar()
        return (iso.year, iso.week)
    if rule == "monthly":
        return (when.year, when.month)
    if rule == "yearly":
        return (when.year,)
    raise ValueError(f"未知的保留規則：{rule}")


def apply_policy[K: Hashable](
    items: Sequence[tuple[K, datetime]], policy: KeepPolicy
) -> tuple[list[K], list[K], dict[K, list[str]]]:
    """回傳 (保留, 刪除, 保留原因)。items 為 (識別碼, 建立時間)。"""
    if policy.is_empty():
        raise UsageError("請至少指定一條保留規則（例如 --keep-daily 7），避免誤刪所有快照")
    ordered = sorted(items, key=lambda it: it[1], reverse=True)
    reasons: dict[K, list[str]] = {}
    for rule, count in policy.rules():
        remaining = count
        last_bucket: object = None
        for position, (key, when) in enumerate(ordered):
            if remaining <= 0:
                break
            bucket = position if rule == "last" else bucket_key(rule, when)
            if bucket != last_bucket:
                reasons.setdefault(key, []).append(rule)
                remaining -= 1
                last_bucket = bucket
    keep = [key for key, _ in ordered if key in reasons]
    remove = [key for key, _ in ordered if key not in reasons]
    return keep, remove, reasons
