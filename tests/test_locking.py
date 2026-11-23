import subprocess
import sys
import time

import pytest

from homevault.errors import RepositoryLockedError
from homevault.locking import REFRESH_EVERY, STALE_AFTER, Lock, read_locks


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


def test_check_held_detects_lock_removed_by_other_computer(repo):
    """另一台電腦把我們的 lock 當成殘留刪掉後，必須在下一個關鍵步驟前察覺。"""
    with repo.lock(exclusive=False) as lock:
        lock.check_held()
        repo.backend.delete(lock._name)
        with pytest.raises(RepositoryLockedError, match="lock 已失效"):
            lock.check_held()


def test_check_held_after_sleep_refreshes_and_detects_loss(repo, monkeypatch):
    """電腦睡眠超過更新間隔：先補一次更新；舊 lock 已被別人刪除就判定失效。"""
    with repo.lock(exclusive=False) as lock:
        lock.info.refreshed -= REFRESH_EVERY + 1
        old_name = lock._name
        lock.check_held()  # 舊 lock 還在：補更新即可
        assert lock._name != old_name
        assert lock.info.refreshed > time.time() - 5

        lock.info.refreshed -= REFRESH_EVERY + 1
        repo.backend.delete(lock._name)
        with pytest.raises(RepositoryLockedError):
            lock.check_held()


def test_backup_aborts_without_snapshot_when_lock_is_lost(repo, tmp_path):
    """情境：Mac 備份中途睡眠，Windows 的 prune 刪掉其 lock 與尚未被引用的 pack。"""
    from conftest import write_file

    from homevault.backup import BackupOptions, run_backup
    from homevault.locking import LOCK_DIR

    src = tmp_path / "src"
    for i in range(3):
        write_file(src / f"f{i}.bin", b"x" * 1000 + bytes([i]))

    def opener(path):
        for name in repo.backend.list(LOCK_DIR):  # 模擬另一台電腦移除了 lock
            repo.backend.delete(name)
        return open(path, "rb")

    with pytest.raises(RepositoryLockedError):
        run_backup(repo, [str(src)], BackupOptions(opener=opener, threads=1))
    assert repo.list_snapshot_ids() == []


def test_concurrent_removal_of_same_stale_lock_is_tolerated(repo, monkeypatch):
    other_host_lock(repo, exclusive=True, age=STALE_AFTER + 60)
    from homevault import locking

    real_read = locking.read_locks

    def read_then_vanish(backend, key):
        result = real_read(backend, key)
        for name, _ in result:  # 另一台電腦搶先刪掉了同一個殘留 lock
            backend.delete(name)
        return result

    monkeypatch.setattr(locking, "read_locks", read_then_vanish)
    lock = repo.lock(exclusive=True)
    lock._remove_stale_and_check_conflicts()


def test_restore_is_blocked_while_other_computer_prunes(repo, tmp_path):
    """還原時取得共享鎖：另一台電腦正在 prune 時拒絕，而不是還原到一半才失敗。"""
    from conftest import write_file

    from homevault.backup import run_backup
    from homevault.restore import run_restore

    src = tmp_path / "src"
    write_file(src / "a.txt", b"a")
    result = run_backup(repo, [str(src)])
    other_host_lock(repo, exclusive=True)
    with pytest.raises(RepositoryLockedError):
        run_restore(repo, result.snapshot, "", tmp_path / "out")
