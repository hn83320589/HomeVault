import subprocess
import sys
import time

import pytest

from homevault.errors import RepositoryLockedError
from homevault.locking import STALE_AFTER, Lock, read_locks


@pytest.fixture
def repo(small_repo):
    return small_repo


def other_host_lock(repo, *, exclusive, age=0.0):
    lock = Lock(repo.backend, repo.key, exclusive=exclusive, hostname="other-computer")
    lock.info.refreshed -= age
    lock.info.pid = 999_999
    lock._name = lock._write()  # 直接寫入，模擬另一台電腦持有的 lock
    return lock


def test_shared_locks_can_coexist(repo):
    with repo.lock(exclusive=False), repo.lock(exclusive=False):
        assert len(read_locks(repo.backend, repo.key)) == 2
    assert read_locks(repo.backend, repo.key) == []


def test_exclusive_lock_conflicts_with_active_lock_on_other_computer(repo):
    other_host_lock(repo, exclusive=False)
    with pytest.raises(RepositoryLockedError) as exc:
        repo.lock(exclusive=True).acquire()
    assert "other-computer" in str(exc.value)
    assert "homevault unlock" in str(exc.value)
    assert len(read_locks(repo.backend, repo.key)) == 1  # 自己的 lock 已清除


def test_backup_blocked_by_exclusive_lock(repo):
    other_host_lock(repo, exclusive=True)
    with pytest.raises(RepositoryLockedError):
        repo.lock(exclusive=False).acquire()


def test_stale_lock_from_other_computer_is_removed(repo):
    other_host_lock(repo, exclusive=True, age=STALE_AFTER + 60)
    with repo.lock(exclusive=True):
        infos = [info for _, info in read_locks(repo.backend, repo.key)]
        assert [i.hostname for i in infos] != ["other-computer"]
        assert len(infos) == 1


def test_lock_of_dead_process_on_same_host_is_removed(repo):
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(proc.stdout)
    lock = Lock(repo.backend, repo.key, exclusive=True)
    lock.info.pid = dead_pid
    lock._name = lock._write()
    with repo.lock(exclusive=True):
        assert len(read_locks(repo.backend, repo.key)) == 1


def test_lock_left_by_crashed_operation_in_same_process_is_stale(repo):
    crashed = repo.lock(exclusive=True)
    crashed.acquire()
    from homevault import locking

    locking._active_sessions.discard(crashed.info.session)  # 模擬持有者已消失
    crashed._stop.set()
    with repo.lock(exclusive=True):
        pass


def test_lock_refresh_updates_timestamp(repo, monkeypatch):
    from homevault import locking

    monkeypatch.setattr(locking, "REFRESH_EVERY", 0.05)
    with repo.lock(exclusive=False) as lock:
        first = lock.info.refreshed
        time.sleep(0.3)
        infos = [i for _, i in read_locks(repo.backend, repo.key)]
        assert len(infos) == 1
        assert infos[0].refreshed > first
