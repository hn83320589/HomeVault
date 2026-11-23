"""以真實的子行程中斷備份：強制終止（等同斷電、拔硬碟）與 Ctrl+C。"""

import os
import signal
import subprocess
import sys
import time

import pytest
from conftest import PASSWORD, file_manifest, write_file

from homevault.backup import run_backup
from homevault.check import run_check
from homevault.repository import Repository
from homevault.restore import run_restore


def _setup(tmp_path):
    repo_path = tmp_path / "repo"
    repo = Repository.init(repo_path, PASSWORD, pack_size=1024 * 1024, kdf_n=2**10)
    small = tmp_path / "small"
    write_file(small / "existing.txt", b"existing snapshot data")
    existing = run_backup(repo, [str(small)])
    src = tmp_path / "big"
    for i in range(80):
        write_file(src / f"file{i:03d}.bin", os.urandom(1536 * 1024))
    return repo_path, existing, small, src


def _start_backup(repo_path, src):
    env = {**os.environ, "HOMEVAULT_PASSWORD": PASSWORD}
    cmd = [sys.executable, "-m", "homevault", "backup", str(src), "--repo", str(repo_path),
           "-q", "--no-low-priority", "--threads", "1"]  # fmt: skip
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def _wait_for_packs(repo_path, proc, count, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        packs = sum(1 for p in (repo_path / "data").rglob("*") if p.is_file())
        if packs >= count:
            return
        if proc.poll() is not None:
            pytest.fail(f"備份在中斷前就結束了：{proc.communicate()}")
        time.sleep(0.005)
    proc.kill()
    pytest.fail("等待 pack 寫入逾時")


def _verify_and_resume(repo_path, existing, small, src, tmp_path):
    assert run_check(Repository.open(repo_path, PASSWORD), read_data=True).ok
    repo = Repository.open(repo_path, PASSWORD)
    run_restore(repo, existing.snapshot, str(small), tmp_path / "old")
    assert file_manifest(tmp_path / "old" / "small") == file_manifest(small)

    total = sum(f.stat().st_size for f in src.iterdir())
    resumed = run_backup(Repository.open(repo_path, PASSWORD), [str(src)])
    assert resumed.snapshot.summary["data_added"] < total * 0.9  # 沿用中斷前寫入的資料
    run_restore(Repository.open(repo_path, PASSWORD), resumed.snapshot, str(src), tmp_path / "new")
    assert file_manifest(tmp_path / "new" / "big") == file_manifest(src)


@pytest.mark.slow
def test_killed_backup_process(tmp_path):
    repo_path, existing, small, src = _setup(tmp_path)
    proc = _start_backup(repo_path, src)
    _wait_for_packs(repo_path, proc, 20)
    proc.kill()  # SIGKILL / TerminateProcess：沒有任何清理的機會
    proc.wait()
    _verify_and_resume(repo_path, existing, small, src, tmp_path)


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="Windows 無法對子行程送出 SIGINT")
def test_ctrl_c_during_backup(tmp_path):
    repo_path, existing, small, src = _setup(tmp_path)
    proc = _start_backup(repo_path, src)
    _wait_for_packs(repo_path, proc, 20)
    proc.send_signal(signal.SIGINT)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert "已中斷" in err.decode()
    assert list((repo_path / "locks").iterdir()) == []  # lock 已釋放
    assert list((repo_path / "tmp").iterdir()) == []  # 暫存檔已清除
    _verify_and_resume(repo_path, existing, small, src, tmp_path)
