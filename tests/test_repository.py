import os

import pytest
from conftest import PASSWORD, SMALL_CHUNKS, SMALL_PACK

from homevault.errors import (
    IntegrityError,
    NotFoundError,
    RepositoryNotFoundError,
    UsageError,
    WrongPasswordError,
)
from homevault.pack import BlobType, pack_name
from homevault.repository import Repository
from homevault.snapshot import Snapshot


def reopen(path) -> Repository:
    repo = Repository.open(path, PASSWORD)
    repo.load_index()
    return repo


def test_init_creates_layout(small_repo, repo_path):
    for d in ["data/00", "data/ff", "index", "snapshots", "keys", "locks", "tmp"]:
        assert (repo_path / d).is_dir()
    assert (repo_path / "config").is_file()
    assert len(os.listdir(repo_path / "keys")) == 1


def test_init_refuses_non_empty_directory(tmp_path):
    (tmp_path / "existing.txt").write_text("x")
    with pytest.raises(UsageError):
        Repository.init(tmp_path, PASSWORD)


def test_open_non_repository_fails_clearly(tmp_path):
    with pytest.raises(RepositoryNotFoundError):
        Repository.open(tmp_path, PASSWORD)


def test_open_with_wrong_password(small_repo, repo_path):
    with pytest.raises(WrongPasswordError):
        Repository.open(repo_path, "wrong")


def test_save_and_load_blob_with_dedup(small_repo):
    small_repo.load_index()
    blob_id, stored = small_repo.save_blob(BlobType.DATA, b"hello world")
    assert stored > 0
    again_id, again_stored = small_repo.save_blob(BlobType.DATA, b"hello world")
    assert (again_id, again_stored) == (blob_id, 0)
    small_repo.flush()
    assert small_repo.load_blob(BlobType.DATA, blob_id) == b"hello world"


def test_blobs_survive_reopen(small_repo, repo_path):
    small_repo.load_index()
    ids = [small_repo.save_blob(BlobType.DATA, os.urandom(5000))[0] for _ in range(30)]
    small_repo.flush()
    repo = reopen(repo_path)
    assert all(repo.has_blob(i) for i in ids)
    assert small_repo.stats.packs_written >= 2  # 150 KB 資料、64 KB pack


def test_packs_written_before_crash_are_recovered(small_repo, repo_path):
    """模擬中斷：pack 已寫入但 index 還沒寫，重新開啟後應能沿用。"""
    small_repo.load_index()
    ids = [small_repo.save_blob(BlobType.DATA, os.urandom(5000))[0] for _ in range(30)]
    written = small_repo.stats.packs_written
    assert written >= 2
    assert os.listdir(repo_path / "index") == []  # 尚未 flush，沒有 index

    repo = Repository.open(repo_path, PASSWORD)
    recovered = repo.load_index(recover_orphans=True)
    assert len(recovered) == written
    in_packs = [i for i in ids if repo.has_blob(i)]
    assert len(in_packs) >= len(ids) - 15  # 最後一個未封檔的 pack 內容會遺失
    repo.flush()  # 復原的項目會被寫入 index
    assert len(os.listdir(repo_path / "index")) == 1


def test_index_entries_for_missing_packs_are_ignored(small_repo, repo_path):
    small_repo.load_index()
    blob_id, _ = small_repo.save_blob(BlobType.DATA, b"will be lost")
    small_repo.flush()
    entry = small_repo.index.get(blob_id)
    os.remove(repo_path / pack_name(entry.pack_id))
    repo = reopen(repo_path)
    assert not repo.has_blob(blob_id)


def test_load_missing_blob_raises_integrity_error(small_repo):
    small_repo.load_index()
    with pytest.raises(IntegrityError):
        small_repo.load_blob(BlobType.DATA, b"\x00" * 32)


def _snapshot(repo, time):
    tree = repo.save_tree([])
    return Snapshot(time=time, hostname="host", paths=["/data"], tree=tree)


def test_snapshot_listing_and_lookup(small_repo):
    small_repo.load_index()
    old = small_repo.save_snapshot(_snapshot(small_repo, "2024-01-01T10:00:00+08:00"))
    new = small_repo.save_snapshot(_snapshot(small_repo, "2024-02-01T10:00:00+08:00"))
    small_repo.flush()
    assert [sid for sid, _ in small_repo.list_snapshots()] == [old, new]
    assert small_repo.find_snapshot("latest")[0] == new
    assert small_repo.find_snapshot(old.hex()[:8])[0] == old
    with pytest.raises(NotFoundError):
        small_repo.find_snapshot("ffffffffffff")


def test_config_records_chunker_and_pack_params(small_repo, repo_path):
    repo = Repository.open(repo_path, PASSWORD)
    assert repo.config.chunker == SMALL_CHUNKS
    assert repo.config.pack_size == SMALL_PACK
