import os
import shutil
import time

import pytest

from homevault.errors import IntegrityError, RepositoryUnavailableError
from homevault.storage import LocalBackend, ObjectMissingError


@pytest.fixture
def backend(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return LocalBackend(root)


def test_write_and_read_back(backend):
    backend.write_atomic("snapshots/abc", b"payload")
    assert backend.read("snapshots/abc") == b"payload"
    assert backend.size("snapshots/abc") == 7
    assert backend.exists("snapshots/abc")


def test_read_range(backend):
    backend.write_atomic("data/ab/abcd", b"0123456789")
    assert backend.read_range("data/ab/abcd", 3, 4) == b"3456"


def test_write_atomic_leaves_no_temp_files(backend):
    backend.write_atomic("index/x1", b"a")
    assert os.listdir(backend.root / "tmp") == []


def test_list_returns_posix_names_recursively(backend):
    backend.write_atomic("data/ab/ab01", b"1")
    backend.write_atomic("data/cd/cd02", b"2")
    backend.write_atomic("index/i1", b"3")
    assert sorted(backend.list("data")) == ["data/ab/ab01", "data/cd/cd02"]
    assert backend.list("index") == ["index/i1"]
    assert backend.list("snapshots") == []


def test_delete(backend):
    backend.write_atomic("locks/l1", b"x")
    backend.delete("locks/l1")
    assert not backend.exists("locks/l1")


def test_missing_object_is_integrity_error(backend):
    with pytest.raises(ObjectMissingError):
        backend.read("data/00/0000")
    assert issubclass(ObjectMissingError, IntegrityError)


def test_unplugged_drive_is_reported_as_unavailable(backend):
    backend.write_atomic("data/ab/ab01", b"1")
    shutil.rmtree(backend.root)
    with pytest.raises(RepositoryUnavailableError):
        backend.read("data/ab/ab01")
    with pytest.raises(RepositoryUnavailableError):
        backend.write_atomic("data/ab/ab02", b"2")


def test_clean_tmp_removes_only_old_files(backend):
    (backend.root / "tmp").mkdir(exist_ok=True)
    old = backend.root / "tmp" / "old.tmp"
    new = backend.root / "tmp" / "new.tmp"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    past = time.time() - 2 * 86400
    os.utime(old, (past, past))
    assert backend.clean_tmp(max_age_seconds=86400) == 1
    assert not old.exists()
    assert new.exists()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="需要 POSIX 權限模型")
def test_list_raises_instead_of_returning_partial_results(backend):
    """目錄讀取錯誤不可被吞掉：不完整的快照清單會讓 prune 誤刪資料。"""
    backend.write_atomic("snapshots/aa", b"1")
    unreadable = backend.root / "snapshots"
    os.chmod(unreadable, 0)
    try:
        with pytest.raises(RepositoryUnavailableError):
            backend.list("snapshots")
    finally:
        os.chmod(unreadable, 0o755)
