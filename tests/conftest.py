import hashlib
import os
from pathlib import Path

import pytest

from homevault import crypto
from homevault.chunker import ChunkerParams
from homevault.repository import Repository

PASSWORD = "correct horse battery staple"
# 小型參數：讓測試用少量資料就能產生多個 chunk 與 pack
SMALL_CHUNKS = ChunkerParams(min_size=1024, avg_size=4096, max_size=16384)
SMALL_PACK = 64 * 1024


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    """測試中使用低成本的 scrypt 參數；正式參數另有測試涵蓋。"""
    monkeypatch.setattr(crypto, "DEFAULT_KDF_N", 2**10)


@pytest.fixture
def repo_path(tmp_path) -> Path:
    return tmp_path / "repo"


@pytest.fixture
def small_repo(repo_path) -> Repository:
    return Repository.init(repo_path, PASSWORD, chunker_params=SMALL_CHUNKS, pack_size=SMALL_PACK)


def file_manifest(root: Path) -> dict[str, tuple[str, int]]:
    """回傳 {相對路徑: (sha256, mtime_ns)}，用來比對還原結果。資料夾以 "<dir>" 表示。"""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink():
                result[p.relative_to(root).as_posix()] = ("link:" + os.readlink(p), 0)
        for f in filenames:
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                result[rel] = ("link:" + os.readlink(p), 0)
            else:
                st = p.stat()
                result[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns)
    return result


def write_file(path: Path, data: bytes, mtime_ns: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path
