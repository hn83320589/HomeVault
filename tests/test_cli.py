import os

import pytest
from conftest import PASSWORD, file_manifest, write_file

from homevault import __version__
from homevault.cli import fmt_bytes, main


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEVAULT_PASSWORD", PASSWORD)
    monkeypatch.delenv("HOMEVAULT_REPO", raising=False)
    return tmp_path


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_full_workflow_init_backup_snapshots_ls_restore(env, capsys):
    repo = str(env / "repo")
    src = env / "src"
    write_file(src / "照片" / "a.jpg", os.urandom(5000), 1_700_000_000_000_000_000)
    write_file(src / "readme.txt", b"hello", 1_700_000_000_000_000_000)

    assert run(capsys, "init", "--repo", repo)[0] == 0
    code, stdout, _ = run(capsys, "backup", str(src), "--repo", repo, "-q", "--no-low-priority")
    assert code == 0
    assert "新增 2" in stdout

    code, stdout, _ = run(capsys, "snapshots", "--repo", repo)
    assert code == 0
    assert "共 1 個快照" in stdout

    code, stdout, _ = run(capsys, "ls", "latest:" + str(src), "--repo", repo, "-R")
    assert code == 0
    assert "照片/a.jpg" in stdout
    assert "readme.txt" in stdout

    code, _, _ = run(
        capsys, "restore", "latest:" + str(src), "--repo", repo, "-t", str(env / "out")
    )
    assert code == 0
    assert file_manifest(env / "out" / "src") == file_manifest(src)


def test_password_file_option(env, capsys, monkeypatch):
    monkeypatch.delenv("HOMEVAULT_PASSWORD")
    pw_file = env / "pw.txt"
    pw_file.write_text(PASSWORD + "\n", encoding="utf-8")
    repo = str(env / "repo")
    assert run(capsys, "init", "--repo", repo, "--password-file", str(pw_file))[0] == 0
    code, _, _ = run(capsys, "snapshots", "--repo", repo, "--password-file", str(pw_file))
    assert code == 0


def test_missing_repo_argument_is_usage_error(env, capsys):
    code, _, err = run(capsys, "snapshots")
    assert code == 2
    assert "--repo" in err


def test_not_a_repository_message(env, capsys):
    code, _, err = run(capsys, "snapshots", "--repo", str(env))
    assert code == 1
    assert "不是 HomeVault 備份庫" in err


def test_unknown_snapshot_message(env, capsys):
    repo = str(env / "repo")
    run(capsys, "init", "--repo", repo)
    code, _, err = run(capsys, "restore", "deadbeef", "--repo", repo, "-t", str(env / "o"))
    assert code == 1
    assert "沒有任何快照" in err


def test_fmt_bytes():
    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(1536) == "1.50 KiB"
    assert fmt_bytes(5 * 1024**3) == "5.00 GiB"


def test_check_and_diff_commands(env, capsys):
    repo = str(env / "repo")
    src = env / "src"
    write_file(src / "a.txt", b"one")
    run(capsys, "init", "--repo", repo)
    run(capsys, "backup", str(src), "--repo", repo, "-q", "--no-low-priority")
    write_file(src / "b.txt", b"two")
    run(capsys, "backup", str(src), "--repo", repo, "-q", "--no-low-priority")

    code, stdout, _ = run(capsys, "check", "--read-data", "--repo", repo)
    assert code == 0
    assert "備份庫完好：2 個快照" in stdout

    _, listing, _ = run(capsys, "snapshots", "--repo", repo)
    ids = [line.split()[0] for line in listing.splitlines()[1:-1]]
    code, stdout, _ = run(capsys, "diff", ids[0], ids[1], "--repo", repo)
    assert code == 0
    assert "b.txt" in stdout
    assert "新增 1、刪除 0、修改 0" in stdout


def test_check_reports_corruption_with_exit_code(env, capsys):
    repo = env / "repo"
    src = env / "src"
    write_file(src / "precious.jpg", os.urandom(50000))
    run(capsys, "init", "--repo", str(repo))
    run(capsys, "backup", str(src), "--repo", str(repo), "-q", "--no-low-priority")
    pack = next(p for p in (repo / "data").rglob("*") if p.is_file())
    data = bytearray(pack.read_bytes())
    data[100] ^= 0xFF
    pack.write_bytes(bytes(data))

    code, stdout, _ = run(capsys, "check", "--read-data", "--repo", str(repo))
    assert code == 13
    assert "受影響的快照與檔案" in stdout
    assert "precious.jpg" in stdout
