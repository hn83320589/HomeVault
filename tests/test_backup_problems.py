"""備份時遇到被鎖住或正在變動的檔案。

家用電腦晚上 11 點常有程式開著：Outlook 資料檔、Excel、資料庫檔。
這些檔案每天都會變、每晚都被鎖住，不可因此從快照中消失。
"""

import io
import os

import pytest
from conftest import PASSWORD, write_file

from homevault.backup import BackupOptions, run_backup
from homevault.forget import run_forget
from homevault.prune import run_prune
from homevault.repository import Repository
from homevault.restore import find_node, run_restore
from homevault.retention import KeepPolicy

MTIME = 1_700_000_000_000_000_000
SECOND = 1_000_000_000  # mtime 以秒遞增：NTFS 精度為 100 奈秒，1 奈秒的差異會被截掉


def locked_opener(*locked_names):
    def opener(path):
        if os.path.basename(path) in locked_names:
            raise PermissionError(13, "檔案正被另一個程式使用", path)
        return open(path, "rb")

    return opener


def fresh(repo):
    r = Repository.open(repo.backend.root, PASSWORD)
    r.load_index(recover_orphans=False)
    return r


def test_file_locked_three_nights_keeps_last_readable_version(small_repo, tmp_path):
    """第 0 晚正常備份；之後內容每晚改變且每晚都被鎖住。快照中必須保留第 0 晚的版本。"""
    src = tmp_path / "src"
    write_file(src / "doc.txt", b"doc")
    write_file(src / "mail.pst", b"night 0 mailbox " * 1000, MTIME)
    night0 = run_backup(small_repo, [str(src)])

    for night in range(1, 4):
        content = f"night {night} mailbox ".encode() * 1000
        write_file(src / "mail.pst", content, MTIME + night * SECOND)
        result = run_backup(
            fresh(small_repo), [str(src)], BackupOptions(opener=locked_opener("mail.pst"))
        )
        node = find_node(fresh(small_repo), result.snapshot, f"{src}/mail.pst")
        assert node.stale_since == night0.snapshot.time  # 一直指向最後可讀的那一晚
        assert [p for p, _since in result.stale] == [str(src / "mail.pst")]
        assert result.errors == []
        assert result.snapshot.summary["files_stale"] == 1

    # 保留政策跑過之後，舊版本仍然存在於最新快照中，不會被 prune 清掉
    run_forget(fresh(small_repo), KeepPolicy(last=1))
    run_prune(fresh(small_repo), threshold=0.0)
    repo = fresh(small_repo)
    _sid, latest = repo.find_snapshot("latest")
    run_restore(repo, latest, str(src), tmp_path / "out")
    assert (tmp_path / "out" / "src" / "mail.pst").read_bytes() == b"night 0 mailbox " * 1000
    assert (tmp_path / "out" / "src" / "doc.txt").read_bytes() == b"doc"


def test_stale_marker_clears_once_file_is_readable_again(small_repo, tmp_path):
    src = tmp_path / "src"
    write_file(src / "mail.pst", b"v0", MTIME)
    run_backup(small_repo, [str(src)])
    write_file(src / "mail.pst", b"v1", MTIME + SECOND)
    run_backup(fresh(small_repo), [str(src)], BackupOptions(opener=locked_opener("mail.pst")))
    result = run_backup(fresh(small_repo), [str(src)])
    node = find_node(fresh(small_repo), result.snapshot, f"{src}/mail.pst")
    assert node.stale_since is None
    assert result.stale == []


def test_file_locked_since_first_night_is_reported_as_never_backed_up(small_repo, tmp_path):
    src = tmp_path / "src"
    write_file(src / "doc.txt", b"doc")
    write_file(src / "mail.pst", b"never readable")
    result = run_backup(small_repo, [str(src)], BackupOptions(opener=locked_opener("mail.pst")))
    assert len(result.errors) == 1
    assert "從未成功備份" in result.errors[0]
    assert "mail.pst" in result.errors[0]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="需要 POSIX 權限模型")
def test_unreadable_folder_keeps_previous_contents(small_repo, tmp_path):
    """資料夾讀不到時，不可讓整個資料夾在快照中「被刪除」。"""
    src = tmp_path / "src"
    write_file(src / "private" / "a.txt", b"secret a")
    write_file(src / "private" / "b.txt", b"secret b")
    first = run_backup(small_repo, [str(src)])
    os.chmod(src / "private", 0)
    try:
        result = run_backup(fresh(small_repo), [str(src)])
    finally:
        os.chmod(src / "private", 0o755)
    repo = fresh(small_repo)
    repo.load_index()
    node = find_node(repo, result.snapshot, f"{src}/private")
    assert node.stale_since == first.snapshot.time
    assert {n.name for n in repo.load_tree(node.subtree)} == {"a.txt", "b.txt"}
    assert [p for p, _ in result.stale] == [str(src / "private")]


class ChangingFile:
    """讀取時檔案被另一個程式改寫的測試替身。"""

    def __init__(self, path, rewrites: int):
        self.path = path
        self.remaining = rewrites
        self.opens = 0

    def __call__(self, path):
        if os.path.basename(path) != os.path.basename(self.path):
            return open(path, "rb")
        self.opens += 1
        data = self.path.read_bytes()
        if self.remaining > 0:
            self.remaining -= 1
            # 讀完之後檔案被改寫：大小與 mtime 都變了
            write_file(self.path, data + b" appended", MTIME + (10 + self.remaining) * SECOND)
        return io.BytesIO(data)


def test_file_modified_during_read_is_reread_once(small_repo, tmp_path):
    src = tmp_path / "src"
    target = write_file(src / "budget.xlsx", b"version A" * 500, MTIME)
    opener = ChangingFile(target, rewrites=1)
    result = run_backup(small_repo, [str(src)], BackupOptions(opener=opener))
    assert opener.opens == 2
    assert result.changing == []
    run_restore(small_repo, result.snapshot, str(src), tmp_path / "out")
    assert (tmp_path / "out" / "src" / "budget.xlsx").read_bytes() == target.read_bytes()


def test_file_that_keeps_changing_is_flagged_and_reread_next_time(small_repo, tmp_path):
    src = tmp_path / "src"
    target = write_file(src / "database.db", b"rows" * 500, MTIME)
    result = run_backup(small_repo, [str(src)], BackupOptions(opener=ChangingFile(target, 5)))
    assert result.changing == [str(target)]
    assert result.snapshot.summary["files_changing"] == 1
    node = find_node(fresh(small_repo), result.snapshot, f"{src}/database.db")
    assert node.changing

    # 下一次即使 size 與 mtime 都和快照記錄的相同，也要重新讀取，不可沿用可能不一致的內容
    write_file(target, b"r" * node.size, node.mtime_ns)
    opener = ChangingFile(target, 0)
    run_backup(fresh(small_repo), [str(src)], BackupOptions(opener=opener))
    assert opener.opens == 1


def test_cli_lists_stale_files_with_exit_code_3(small_repo, tmp_path, monkeypatch, capsys):
    from homevault import backup as backup_module
    from homevault.cli import main

    src = tmp_path / "src"
    write_file(src / "mail.pst", b"v0", MTIME)
    run_backup(small_repo, [str(src)])
    write_file(src / "mail.pst", b"v1", MTIME + SECOND)

    original = backup_module.BackupOptions
    monkeypatch.setattr(
        backup_module,
        "BackupOptions",
        lambda **kw: original(**{**kw, "opener": locked_opener("mail.pst")}),
    )
    monkeypatch.setenv("HOMEVAULT_PASSWORD", PASSWORD)
    repo = str(small_repo.backend.root)
    code = main(["backup", str(src), "--repo", repo, "-q", "--no-low-priority"])
    out = capsys.readouterr().out
    assert code == 3
    assert "沿用先前的版本" in out
    assert "mail.pst" in out

    main(["snapshots", "--repo", repo])
    assert "沿用舊版 1" in capsys.readouterr().out
    main(["ls", "latest:" + str(src), "--repo", repo])
    assert "沿用" in capsys.readouterr().out
