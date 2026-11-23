import os
import random
import sys
from pathlib import Path

import pytest
from conftest import PASSWORD, file_manifest, write_file

from homevault.backup import BackupOptions, ExcludeMatcher, path_components, run_backup
from homevault.errors import UsageError
from homevault.repository import Repository
from homevault.restore import run_restore

BASE_MTIME = 1_600_000_000_123_456_789

can_symlink = sys.platform != "win32"


def make_tree(root: Path) -> Path:
    write_file(root / "docs" / "報稅資料 2024.pdf", os.urandom(30000), BASE_MTIME)
    write_file(root / "docs" / "notes.txt", b"hello\n" * 500, BASE_MTIME + 1)
    write_file(root / "photos" / "家族旅行 🏖️" / "IMG_0001.jpg", os.urandom(50000), BASE_MTIME + 2)
    write_file(root / "photos" / "空檔案.txt", b"", BASE_MTIME + 3)
    write_file(root / "photos" / "dup.jpg", (root / "docs" / "報稅資料 2024.pdf").read_bytes())
    (root / "empty_dir").mkdir()
    if can_symlink:
        os.symlink("docs/notes.txt", root / "link-to-notes")
    return root


def backup_and_restore(repo: Repository, src: Path, dest: Path, **opts):
    result = run_backup(repo, [str(src)], BackupOptions(**opts))
    restored = run_restore(repo, result.snapshot, str(src), dest)
    return result, restored


def test_roundtrip_preserves_content_and_mtime(small_repo, tmp_path):
    src = make_tree(tmp_path / "src")
    result, restored = backup_and_restore(small_repo, src, tmp_path / "out")
    assert restored.errors == []
    assert file_manifest(tmp_path / "out" / "src") == file_manifest(src)
    assert result.files_new == 5
    restored_dir = tmp_path / "out" / "src" / "empty_dir"
    assert restored_dir.is_dir()
    assert restored_dir.stat().st_mtime_ns == (src / "empty_dir").stat().st_mtime_ns


def test_restore_whole_snapshot_keeps_full_path(small_repo, tmp_path):
    src = make_tree(tmp_path / "src")
    result = run_backup(small_repo, [str(src)])
    run_restore(small_repo, result.snapshot, "", tmp_path / "out")
    restored_root = tmp_path / "out" / Path(*path_components(str(src)))
    assert file_manifest(restored_root) == file_manifest(src)


def test_restore_single_file_and_single_folder(small_repo, tmp_path):
    src = make_tree(tmp_path / "src")
    result = run_backup(small_repo, [str(src)])
    snap_path = "/".join(path_components(str(src)))

    run_restore(small_repo, result.snapshot, f"{snap_path}/docs/notes.txt", tmp_path / "one")
    assert (tmp_path / "one" / "notes.txt").read_bytes() == (
        src / "docs" / "notes.txt"
    ).read_bytes()
    assert os.listdir(tmp_path / "one") == ["notes.txt"]

    run_restore(small_repo, result.snapshot, f"{snap_path}/photos", tmp_path / "folder")
    assert file_manifest(tmp_path / "folder" / "photos") == file_manifest(src / "photos")


def test_restore_does_not_overwrite_without_flag(small_repo, tmp_path):
    src = make_tree(tmp_path / "src")
    result = run_backup(small_repo, [str(src)])
    out = tmp_path / "out"
    write_file(out / "src" / "docs" / "notes.txt", b"local edit")
    restored = run_restore(small_repo, result.snapshot, str(src), out)
    assert restored.skipped == 1
    assert (out / "src" / "docs" / "notes.txt").read_bytes() == b"local edit"
    run_restore(small_repo, result.snapshot, str(src), out, overwrite=True)
    assert file_manifest(out / "src") == file_manifest(src)


class CountingOpener:
    def __init__(self):
        self.opened: list[str] = []

    def __call__(self, path):
        self.opened.append(os.path.basename(path))
        return open(path, "rb")


def test_incremental_backup_does_not_read_unchanged_files(small_repo, tmp_path):
    """驗收 7：大小與修改時間未變的檔案不會被重新讀取。"""
    src = make_tree(tmp_path / "src")
    run_backup(small_repo, [str(src)])

    write_file(src / "docs" / "notes.txt", b"changed content\n" * 500, BASE_MTIME + 99)
    opener = CountingOpener()
    result = run_backup(small_repo, [str(src)], BackupOptions(opener=opener))

    assert opener.opened == ["notes.txt"]
    assert result.files_changed == 1
    assert result.files_unmodified == 4
    assert result.progress.files_read == 1
    assert result.snapshot.parent is not None


def test_full_scan_rereads_every_file(small_repo, tmp_path):
    from homevault.pack import BlobType

    src = make_tree(tmp_path / "src")
    run_backup(small_repo, [str(src)])
    before = set(small_repo.index.blob_ids())
    opener = CountingOpener()
    run_backup(small_repo, [str(src)], BackupOptions(opener=opener, full_scan=True))
    assert len(opener.opened) == 5
    # 檔案內容全部去重；新增的只能是目錄中繼資料（上層目錄的 mtime 可能改變，
    # 例如 Windows 的暫存目錄很深且持續有其他程式寫入）
    new_blobs = set(small_repo.index.blob_ids()) - before
    assert {small_repo.index.get(b).type for b in new_blobs} <= {BlobType.TREE}


def test_incremental_snapshot_restores_latest_content(small_repo, tmp_path):
    src = make_tree(tmp_path / "src")
    run_backup(small_repo, [str(src)])
    write_file(src / "new file.txt", b"brand new", BASE_MTIME + 50)
    (src / "docs" / "notes.txt").unlink()
    result = run_backup(small_repo, [str(src)])
    run_restore(small_repo, result.snapshot, str(src), tmp_path / "out")
    assert file_manifest(tmp_path / "out" / "src") == file_manifest(src)


def test_exclude_patterns(small_repo, tmp_path):
    src = tmp_path / "src"
    write_file(src / "keep.txt", b"keep")
    write_file(src / "scratch.TMP", b"x")
    write_file(src / ".DS_Store", b"x")
    write_file(src / "app" / "node_modules" / "lib.js", b"x")
    write_file(src / "app" / "main.js", b"code")
    write_file(src / "build" / "out.bin", b"x")
    write_file(src / "app" / "build" / "keep.bin", b"kept: pattern is anchored")
    result = run_backup(
        small_repo,
        [str(src)],
        BackupOptions(excludes=["*.tmp", "node_modules", ".DS_Store", "/build"]),
    )
    run_restore(small_repo, result.snapshot, str(src), tmp_path / "out")
    assert sorted(file_manifest(tmp_path / "out" / "src")) == [
        "app/build/keep.bin",
        "app/main.js",
        "keep.txt",
    ]


def test_exclude_matcher_rules():
    m = ExcludeMatcher(["*.tmp", "cache/", "photos/thumbs", "# comment", ""])
    assert m.matches("a/b/x.tmp", is_dir=False)
    assert m.matches("deep/cache", is_dir=True)
    assert not m.matches("deep/cache", is_dir=False)
    assert m.matches("photos/thumbs", is_dir=True)
    assert not m.matches("other/photos/thumbs", is_dir=True)


def test_repository_inside_source_is_not_backed_up(tmp_path):
    src = tmp_path / "home"
    write_file(src / "file.txt", b"data")
    repo = Repository.init(src / "backup-repo", PASSWORD)
    result = run_backup(repo, [str(src)])
    run_restore(repo, result.snapshot, str(src), tmp_path / "out")
    assert sorted(file_manifest(tmp_path / "out" / "home")) == ["file.txt"]


def test_cross_host_backup_deduplicates(tmp_path):
    """驗收 4：同一份資料由兩台電腦各備份一次，第二次新增量 < 1%。"""
    repo = Repository.init(tmp_path / "repo", PASSWORD)
    src = tmp_path / "shared"
    rng = random.Random(4)
    for i in range(40):
        write_file(src / f"album{i % 4}" / f"photo{i}.jpg", rng.randbytes(rng.randint(1, 600_000)))
    total = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())

    first = run_backup(repo, [str(src)], BackupOptions(hostname="mac-mini"))
    second = run_backup(repo, [str(src)], BackupOptions(hostname="windows-pc"))
    assert first.snapshot.parent is None
    assert second.snapshot.parent is None  # 不同主機，沒有 parent，必須真的讀取內容
    assert second.progress.files_read == 40
    assert second.snapshot.summary["data_added"] < 0.01 * total


def test_multiple_sources_and_overlap_detection(small_repo, tmp_path):
    a = make_tree(tmp_path / "a")
    b = tmp_path / "b"
    write_file(b / "only.txt", b"b content")
    result = run_backup(small_repo, [str(a), str(b)])
    run_restore(small_repo, result.snapshot, str(b), tmp_path / "out")
    assert file_manifest(tmp_path / "out" / "b") == file_manifest(b)
    with pytest.raises(UsageError):
        run_backup(small_repo, [str(a), str(a / "docs")])


def test_unreadable_file_is_skipped_and_reported(small_repo, tmp_path):
    src = make_tree(tmp_path / "src")

    def flaky_opener(path):
        if path.endswith("notes.txt"):
            raise PermissionError("檔案被其他程式鎖定")
        return open(path, "rb")

    result = run_backup(small_repo, [str(src)], BackupOptions(opener=flaky_opener))
    assert len(result.errors) == 1
    assert "notes.txt" in result.errors[0]
    assert result.snapshot.summary["errors"] == 1


@pytest.mark.slow
def test_acceptance_1000_files_with_large_file_unicode_and_empty(tmp_path):
    """驗收 2：≥1,000 個檔案，含空檔、>100 MiB 檔案、中文與 emoji 檔名，還原後雜湊與 mtime 相同。"""
    repo = Repository.init(tmp_path / "repo", PASSWORD)
    src = tmp_path / "家庭資料"
    rng = random.Random(2)
    names = ["照片", "報稅", "影片", "🎉派對", "日記📔", "doc"]
    for i in range(1000):
        size = 0 if i % 50 == 0 else rng.randint(1, 30_000)
        folder = src / names[i % len(names)] / f"子資料夾{i % 7}"
        write_file(
            folder / f"{names[(i + 1) % len(names)]}_{i}.bin",
            rng.randbytes(size),
            BASE_MTIME + i * 1_000_003,
        )
    big = src / "影片" / "大型影片🎬.mp4"
    with open(big, "wb") as f:
        for _ in range(101):
            f.write(os.urandom(1024 * 1024))
    os.utime(big, ns=(BASE_MTIME, BASE_MTIME))

    result = run_backup(repo, [str(src)])
    assert result.files_new == 1001
    restored = run_restore(repo, result.snapshot, str(src), tmp_path / "out")
    assert restored.errors == []
    assert restored.files == 1001
    assert file_manifest(tmp_path / "out" / "家庭資料") == file_manifest(src)


def test_find_node_accepts_windows_drive_paths(small_repo):
    """快照內 C:\\Users\\Dad 存成 C/Users/Dad，還原時輸入 Windows 路徑也要找得到。"""
    from homevault.restore import find_node
    from homevault.snapshot import Snapshot
    from homevault.tree import DIR, FILE, Node

    small_repo.load_index()
    leaf = small_repo.save_tree([Node("photo.jpg", FILE, size=0)])
    users = small_repo.save_tree([Node("Dad", DIR, subtree=leaf)])
    drive = small_repo.save_tree([Node("Users", DIR, subtree=users)])
    root = small_repo.save_tree([Node("C", DIR, subtree=drive)])
    small_repo.flush()
    snap = Snapshot(time="2026-01-01T23:00:00+08:00", hostname="pc", paths=["C:\\"], tree=root)
    assert find_node(small_repo, snap, r"C:\Users\Dad\photo.jpg").name == "photo.jpg"
    assert find_node(small_repo, snap, "/C/Users/Dad").name == "Dad"


def test_path_components_for_posix_paths():
    assert path_components("/Users/mom/Pictures") == ["Users", "mom", "Pictures"]


def test_backup_falls_back_to_full_read_when_parent_tree_is_damaged(small_repo, tmp_path):
    """上一個快照的根 tree 損毀時，備份改為完整讀取，而不是每次都失敗。"""
    from homevault.pack import pack_name

    src = make_tree(tmp_path / "src")
    first = run_backup(small_repo, [str(src)])
    entry = small_repo.index.get(first.snapshot.tree)
    os.remove(small_repo.backend.root / pack_name(entry.pack_id))

    repo = Repository.open(small_repo.backend.root, PASSWORD)
    result = run_backup(repo, [str(src)])
    restored = run_restore(repo, result.snapshot, str(src), tmp_path / "out")
    assert restored.errors == []
    assert file_manifest(tmp_path / "out" / "src") == file_manifest(src)


@pytest.mark.skipif(not can_symlink, reason="Windows 建立符號連結需要權限")
def test_symlink_to_directory_records_directory_flag(small_repo, tmp_path):
    from homevault.restore import find_node

    src = make_tree(tmp_path / "src")
    os.symlink("docs", src / "docs-link")
    result = run_backup(small_repo, [str(src)])
    node = find_node(small_repo, result.snapshot, f"{src}/docs-link")
    assert node.target == "docs"
    assert node.target_is_dir
    assert not find_node(small_repo, result.snapshot, f"{src}/link-to-notes").target_is_dir
    run_restore(small_repo, result.snapshot, str(src), tmp_path / "out")
    assert os.readlink(tmp_path / "out" / "src" / "docs-link") == "docs"
