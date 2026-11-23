import os

from conftest import write_file

from homevault.backup import run_backup
from homevault.browse import diff_snapshots


def test_diff_reports_added_removed_and_modified(small_repo, tmp_path):
    src = tmp_path / "src"
    write_file(src / "keep.txt", b"same", 1_000_000_000_000_000_000)
    write_file(src / "edit.txt", b"before", 1_000_000_000_000_000_000)
    write_file(src / "gone.txt", b"bye", 1_000_000_000_000_000_000)
    write_file(src / "olddir" / "a.txt", b"a")
    first = run_backup(small_repo, [str(src)])

    write_file(src / "edit.txt", b"after!", 1_000_000_000_500_000_000)
    os.remove(src / "gone.txt")
    write_file(src / "new" / "deep" / "n.txt", b"new")
    (src / "olddir" / "a.txt").unlink()
    (src / "olddir").rmdir()
    second = run_backup(small_repo, [str(src)])

    entries = diff_snapshots(small_repo, first.snapshot, second.snapshot)
    by_kind = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e.path.rsplit("/src/", 1)[-1])
    assert sorted(by_kind["+"]) == ["new", "new/deep", "new/deep/n.txt"]
    assert sorted(by_kind["-"]) == ["gone.txt", "olddir", "olddir/a.txt"]
    assert by_kind["M"] == ["edit.txt"]


def test_diff_of_identical_snapshots_is_empty(small_repo, tmp_path):
    src = tmp_path / "src"
    write_file(src / "a.txt", b"a")
    a = run_backup(small_repo, [str(src)])
    b = run_backup(small_repo, [str(src)])
    assert diff_snapshots(small_repo, a.snapshot, b.snapshot) == []
