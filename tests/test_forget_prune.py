import os

import pytest
from conftest import PASSWORD, file_manifest, write_file

from homevault.backup import BackupOptions, run_backup
from homevault.check import run_check
from homevault.errors import IntegrityError
from homevault.forget import forget_ids, run_forget
from homevault.pack import pack_name
from homevault.prune import run_prune
from homevault.repository import Repository
from homevault.restore import run_restore
from homevault.retention import KeepPolicy
from homevault.stats import run_stats


def fresh(repo):
    return Repository.open(repo.backend.root, PASSWORD)


def snapshot_series(repo, src, n, host="mac", tmp=None):
    """建立 n 個快照，每次都換掉一個大檔案，讓舊快照擁有獨有資料。"""
    write_file(src / "stable.bin", os.urandom(60_000))
    manifests = []
    for i in range(n):
        write_file(src / "changing.bin", os.urandom(80_000))
        write_file(src / f"note{i}.txt", f"note {i}".encode())
        result = run_backup(repo, [str(src)], BackupOptions(hostname=host))
        manifests.append((result.snapshot_id, file_manifest(src)))
    return manifests


def assert_restores_exactly(repo, snap_id, expected, dest):
    repo = fresh(repo)
    snap = repo.load_snapshot(snap_id)
    result = run_restore(repo, snap, snap.paths[0], dest)
    assert result.errors == []
    assert file_manifest(dest / os.path.basename(snap.paths[0])) == expected


def test_forget_groups_by_host(small_repo, tmp_path):
    snapshot_series(small_repo, tmp_path / "src", 4, host="mac")
    snapshot_series(small_repo, tmp_path / "src", 3, host="windows")
    plans = run_forget(small_repo, KeepPolicy(last=2))
    assert {(g.hostname, len(g.keep), len(g.remove)) for g in plans} == {
        ("mac", 2, 2),
        ("windows", 2, 1),
    }
    assert len(small_repo.list_snapshots()) == 4


def test_forget_dry_run_changes_nothing(small_repo, tmp_path):
    snapshot_series(small_repo, tmp_path / "src", 3)
    plans = run_forget(small_repo, KeepPolicy(last=1), dry_run=True)
    assert len(plans[0].remove) == 2
    assert len(small_repo.list_snapshots()) == 3


def test_forget_specific_snapshot(small_repo, tmp_path):
    series = snapshot_series(small_repo, tmp_path / "src", 2)
    forget_ids(small_repo, [series[0][0].hex()[:8]])
    assert [sid for sid, _ in small_repo.list_snapshots()] == [series[1][0]]


def test_prune_reclaims_space_and_keeps_snapshots_restorable(small_repo, tmp_path):
    """驗收 9：prune 後保留的快照仍可完整還原，備份庫確實變小。"""
    series = snapshot_series(small_repo, tmp_path / "src", 5)
    run_forget(small_repo, KeepPolicy(last=2))
    result = run_prune(fresh(small_repo), threshold=0.0)

    assert result.size_after < result.size_before
    assert result.plan.delete or result.plan.repack
    for snap_id, manifest in series[-2:]:
        assert_restores_exactly(small_repo, snap_id, manifest, tmp_path / snap_id.hex()[:8])
    assert run_check(fresh(small_repo), read_data=True).ok


def test_prune_repacks_partially_used_packs(tmp_path):
    repo = Repository.init(tmp_path / "repo", PASSWORD, pack_size=1024 * 1024)
    src = tmp_path / "src"
    write_file(src / "keep.bin", os.urandom(100_000))
    write_file(src / "drop.bin", os.urandom(100_000))
    run_backup(repo, [str(src)])  # 兩個檔案落在同一個 pack
    (src / "drop.bin").unlink()
    second = run_backup(repo, [str(src)])
    run_forget(repo, KeepPolicy(last=1))

    result = run_prune(fresh(repo), threshold=0.0)
    assert result.plan.repack, "部分使用的 pack 應被重新打包"
    assert result.size_before - result.size_after > 90_000
    assert_restores_exactly(repo, second.snapshot_id, file_manifest(src), tmp_path / "out")


def test_prune_threshold_keeps_mostly_used_packs(tmp_path):
    repo = Repository.init(tmp_path / "repo", PASSWORD, pack_size=1024 * 1024)
    src = tmp_path / "src"
    write_file(src / "big.bin", os.urandom(500_000))
    write_file(src / "small.bin", os.urandom(10_000))
    run_backup(repo, [str(src)])
    (src / "small.bin").unlink()
    run_backup(repo, [str(src)])
    run_forget(repo, KeepPolicy(last=1))
    result = run_prune(fresh(repo), threshold=0.2)
    assert result.plan.repack == []  # 只有約 2% 未使用，不值得重寫


def test_prune_twice_is_noop(small_repo, tmp_path):
    snapshot_series(small_repo, tmp_path / "src", 3)
    run_forget(small_repo, KeepPolicy(last=1))
    run_prune(fresh(small_repo), threshold=0.0)
    second = run_prune(fresh(small_repo), threshold=0.0)
    assert second.plan.delete == [] and second.plan.repack == []
    assert second.size_after == second.size_before


def test_prune_aborts_without_deleting_when_data_is_missing(small_repo, tmp_path):
    series = snapshot_series(small_repo, tmp_path / "src", 3)
    run_forget(small_repo, KeepPolicy(last=1))
    repo = fresh(small_repo)
    repo.load_index()
    snap = repo.load_snapshot(series[-1][0])
    some_blob = next(iter(repo.walk_blobs([snap]) - {snap.tree}))
    os.remove(repo.backend.root / pack_name(repo.index.get(some_blob).pack_id))
    before = sorted(p.name for p in (repo.backend.root / "data").rglob("*") if p.is_file())

    with pytest.raises(IntegrityError, match="prune 已中止"):
        run_prune(fresh(small_repo))
    after = sorted(p.name for p in (repo.backend.root / "data").rglob("*") if p.is_file())
    assert after == before


def test_prune_dry_run_changes_nothing(small_repo, tmp_path):
    snapshot_series(small_repo, tmp_path / "src", 3)
    run_forget(small_repo, KeepPolicy(last=1))
    root = small_repo.backend.root
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    result = run_prune(fresh(small_repo), dry_run=True)
    assert result.plan.delete or result.plan.repack
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_stats_reports_unique_data_per_snapshot(small_repo, tmp_path):
    snapshot_series(small_repo, tmp_path / "src", 3)
    stats = run_stats(fresh(small_repo))
    assert len(stats.snapshots) == 3
    assert stats.repo_size > stats.unique_stored > 0
    assert stats.total_logical > stats.unique_raw  # stable.bin 被三個快照共用
    for s in stats.snapshots:
        assert s.unique_stored >= 80_000  # 每個快照都有自己的 changing.bin
    assert 0 < stats.space_saving < 1
