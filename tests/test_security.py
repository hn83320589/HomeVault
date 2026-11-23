"""驗收 5（密碼錯誤）與驗收 6（備份庫不洩漏檔名與內容雜湊）。"""

import hashlib
import io
import os
import unicodedata
from pathlib import Path

import pytest
from conftest import PASSWORD, write_file

from homevault.backup import run_backup
from homevault.chunker import Chunker
from homevault.cli import main
from homevault.repository import Repository


def repo_fingerprint(root: Path) -> dict[str, str]:
    """備份庫所有檔案的名稱與內容雜湊。"""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def all_repo_bytes(root: Path) -> bytes:
    """備份庫內所有檔案的內容，以及所有檔名與目錄名稱。"""
    parts = []
    for p in sorted(root.rglob("*")):
        parts.append(p.relative_to(root).as_posix().encode())
        if p.is_file():
            parts.append(p.read_bytes())
    return b"\x00".join(parts)


SECRET_NAMES = [
    "TaxReturn2024_secret.pdf",
    "家族旅行照片_墾丁.jpg",
    "🎂生日派對.mov",
    "Café_résumé.docx",
]


@pytest.fixture
def populated_repo(tmp_path):
    src = tmp_path / "SecretFolderName"
    for i, name in enumerate(SECRET_NAMES):
        content = (f"confidential content {name} " * 3000).encode() + os.urandom(20000 * i)
        write_file(src / "私人資料夾" / name, content)
    repo = Repository.init(tmp_path / "repo", PASSWORD)
    run_backup(repo, [str(src)])
    return repo, src, tmp_path / "repo"


def _encodings(text: str) -> list[bytes]:
    variants = {unicodedata.normalize(form, text) for form in ("NFC", "NFD")}
    out = []
    for v in variants:
        out += [v.encode("utf-8"), v.encode("utf-16-le"), v.encode("utf-16-be")]
    return out


def test_repository_does_not_leak_file_names(populated_repo):
    _repo, src, repo_root = populated_repo
    blob = all_repo_bytes(repo_root)
    names = [*SECRET_NAMES, "私人資料夾", "SecretFolderName", src.parent.name]
    for name in names:
        for encoded in _encodings(name):
            assert encoded not in blob, f"備份庫中出現原始名稱 {name!r}"


def test_repository_does_not_leak_content_hashes(populated_repo):
    repo, src, repo_root = populated_repo
    blob = all_repo_bytes(repo_root)
    blob_lower = blob.lower()
    chunker = Chunker(repo.key.chunker_seed, repo.config.chunker)
    for path in (src / "私人資料夾").iterdir():
        data = path.read_bytes()
        pieces = [data, *chunker.iter_chunks(io.BytesIO(data))]
        for piece in pieces:
            for algo in (hashlib.sha256, hashlib.sha1, hashlib.md5):
                digest = algo(piece).digest()
                assert digest not in blob
                assert digest.hex().encode() not in blob_lower


def test_repository_does_not_contain_plaintext_content(populated_repo):
    _repo, _src, repo_root = populated_repo
    assert b"confidential content" not in all_repo_bytes(repo_root)


@pytest.mark.parametrize(
    "command",
    [
        ["snapshots"],
        ["backup", "{src}"],
        ["ls", "latest"],
        ["restore", "latest", "-t", "{out}"],
        ["unlock", "--all"],
        ["check", "--read-data"],
        ["diff", "latest", "latest"],
        ["forget", "--keep-last", "1", "--prune"],
        ["prune"],
        ["stats"],
    ],
)
def test_wrong_password_reads_nothing_and_changes_nothing(
    populated_repo, command, monkeypatch, capsys, tmp_path
):
    """驗收 5：密碼錯誤時清楚提示、不輸出任何資料、備份庫完全不變。"""
    _repo, src, repo_root = populated_repo
    before = repo_fingerprint(repo_root)
    monkeypatch.setenv("HOMEVAULT_PASSWORD", "not the password")
    out_dir = tmp_path / "restore-out"
    argv = [a.format(src=src, out=out_dir) for a in command] + ["--repo", str(repo_root), "-q"]

    code = main(argv)
    captured = capsys.readouterr()

    assert code == 10
    assert "密碼錯誤" in captured.err
    assert "備份庫未做任何變更" in captured.err
    assert captured.out == ""
    assert repo_fingerprint(repo_root) == before
    assert not out_dir.exists() or not any(out_dir.iterdir())
