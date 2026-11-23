"""驗收 8：保留政策以固定時間序列做單元測試，含跨月、跨年、ISO 週 53、閏年。"""

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from homevault.errors import UsageError
from homevault.retention import KeepPolicy, apply_policy, bucket_key

TPE = timezone(timedelta(hours=8))


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TPE)


def kept(times: list[str], policy: KeepPolicy) -> list[str]:
    items = [(s, t(s)) for s in times]
    keep, _remove, _reasons = apply_policy(items, policy)
    return sorted(keep)


def test_daily_across_year_boundary_keeps_newest_per_day():
    times = [f"2024-12-{d:02d} 23:00" for d in range(25, 32)]
    times += [f"2025-01-{d:02d} 23:00" for d in range(1, 6)]
    times += ["2025-01-05 08:00", "2025-01-04 12:00"]  # 同一天的較舊快照
    assert kept(times, KeepPolicy(daily=7)) == [
        "2024-12-30 23:00",
        "2024-12-31 23:00",
        "2025-01-01 23:00",
        "2025-01-02 23:00",
        "2025-01-03 23:00",
        "2025-01-04 23:00",
        "2025-01-05 23:00",
    ]


def test_monthly_across_year_boundary():
    times = [
        "2024-11-10 23:00",
        "2024-11-20 23:00",
        "2024-12-05 23:00",
        "2024-12-31 23:59",
        "2025-01-01 00:01",
        "2025-01-15 23:00",
    ]
    assert kept(times, KeepPolicy(monthly=3)) == [
        "2024-11-20 23:00",
        "2024-12-31 23:59",
        "2025-01-15 23:00",
    ]


def test_weekly_uses_iso_weeks_including_week_53():
    # 2020-12-31（四）、2021-01-01（五）、2021-01-03（日）都屬於 ISO 2020-W53
    times = ["2020-12-27 23:00", "2020-12-31 23:00", "2021-01-01 23:00",
             "2021-01-03 23:00", "2021-01-04 23:00"]  # fmt: skip
    assert bucket_key("weekly", t("2021-01-01 23:00")) == (2020, 53)
    assert kept(times, KeepPolicy(weekly=3)) == [
        "2020-12-27 23:00",
        "2021-01-03 23:00",
        "2021-01-04 23:00",
    ]


def test_yearly():
    times = ["2023-06-01 10:00", "2023-12-31 23:00", "2024-01-01 01:00",
             "2024-12-31 22:00", "2025-02-01 23:00"]  # fmt: skip
    assert kept(times, KeepPolicy(yearly=2)) == ["2024-12-31 22:00", "2025-02-01 23:00"]


def test_leap_day_is_its_own_day():
    times = ["2024-02-28 23:00", "2024-02-29 23:00", "2024-03-01 23:00"]
    assert kept(times, KeepPolicy(daily=2)) == ["2024-02-29 23:00", "2024-03-01 23:00"]


def test_days_without_snapshots_do_not_count():
    """「最近 7 天」指有快照的 7 個日子：電腦關機兩週後，舊的每日快照不會全被刪光。"""
    times = [f"2024-03-{d:02d} 23:00" for d in range(1, 11)] + ["2024-03-25 23:00"]
    result = kept(times, KeepPolicy(daily=7))
    assert len(result) == 7
    assert result[0] == "2024-03-05 23:00"


def test_bucket_uses_snapshot_local_time():
    # 台北 2025-01-01 07:00 = UTC 2024-12-31 23:00，應歸在本地的 2025-01-01
    assert bucket_key("daily", t("2025-01-01 07:00")) == (2025, 1, 1)


def test_keep_last_and_combination_with_reasons():
    times = [f"2024-01-{d:02d} 23:00" for d in range(1, 32)]
    times += [f"2024-02-{d:02d} 23:00" for d in range(1, 30)]
    items = [(s, t(s)) for s in times]
    keep, remove, reasons = apply_policy(items, KeepPolicy(last=2, daily=3, monthly=2))
    assert sorted(keep) == [
        "2024-01-31 23:00",
        "2024-02-27 23:00",
        "2024-02-28 23:00",
        "2024-02-29 23:00",
    ]
    assert set(reasons["2024-02-29 23:00"]) == {"last", "daily", "monthly"}
    assert reasons["2024-01-31 23:00"] == ["monthly"]
    assert len(keep) + len(remove) == len(times)


def test_full_year_with_typical_family_policy():
    start = t("2024-01-01 23:00")
    times = [(start + timedelta(days=i)).strftime("%Y-%m-%d %H:%M") for i in range(400)]
    result = kept(times, KeepPolicy(daily=7, weekly=4, monthly=12))
    # 7 天 + 額外的週（扣掉與每日重疊者）+ 額外的月
    assert 7 + 3 + 11 - 2 <= len(result) <= 7 + 4 + 12
    assert result[-1] == times[-1]


def test_empty_policy_is_rejected():
    with pytest.raises(UsageError):
        apply_policy([("a", t("2024-01-01 00:00"))], KeepPolicy())


# ── 性質測試 ─────────────────────────────────────────────

RULES = ["last", "hourly", "daily", "weekly", "monthly", "yearly"]

times_strategy = st.lists(
    st.datetimes(min_value=datetime(2019, 1, 1), max_value=datetime(2026, 12, 31)),
    min_size=1,
    max_size=60,
    unique=True,
).map(lambda ds: [d.replace(tzinfo=TPE) for d in ds])

policy_strategy = st.builds(
    KeepPolicy,
    **{r: st.integers(0, 6) for r in RULES},
).filter(lambda p: not p.is_empty())


def oracle(items, policy: KeepPolicy) -> set:
    """另一種寫法的參考答案：先依時間桶分組，取最近 N 個桶中各自最新的快照。"""
    result = set()
    ordered = sorted(items, key=lambda it: it[1], reverse=True)
    for rule in RULES:
        n = getattr(policy, rule)
        buckets: dict = {}
        for i, (key, when) in enumerate(ordered):
            b = i if rule == "last" else bucket_key(rule, when)
            buckets.setdefault(b, key)  # 第一個出現的就是該桶最新的
        newest_buckets = sorted(
            buckets, key=lambda b: next(w for k, w in ordered if k == buckets[b]), reverse=True
        )[:n]
        result.update(buckets[b] for b in newest_buckets)
    return result


@settings(max_examples=300, deadline=None)
@given(times=times_strategy, policy=policy_strategy)
def test_policy_matches_oracle(times, policy):
    items = [(i, when) for i, when in enumerate(times)]
    keep, remove, _ = apply_policy(items, policy)
    assert set(keep) == oracle(items, policy)
    assert set(keep) | set(remove) == {i for i, _ in items}
    newest = max(items, key=lambda it: it[1])[0]
    assert newest in keep  # 最新的快照永遠保留


@settings(max_examples=300, deadline=None)
@given(times=times_strategy, policy=policy_strategy, data=st.data())
def test_policy_is_idempotent_after_partial_forget(times, policy, data):
    """中斷安全的基礎：forget 只刪了一部分就中斷，重跑結果完全相同。"""
    items = [(i, when) for i, when in enumerate(times)]
    keep, remove, _ = apply_policy(items, policy)
    deleted = data.draw(st.lists(st.sampled_from(remove), unique=True)) if remove else []
    already_deleted = set(deleted)
    remaining = [it for it in items if it[0] not in already_deleted]
    keep2, remove2, _ = apply_policy(remaining, policy)
    assert set(keep2) == set(keep)
    assert set(remove2) == set(remove) - already_deleted
