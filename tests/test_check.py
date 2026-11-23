import os

import pytest
from conftest import PASSWORD, write_file

from homevault.backup import run_backup
from homevault.check import run_check
from homevault.index import decode_index_file
from homevault.pack import BlobType, pack_name
from homevault.repository import Repository


@pytest.fixture
def two_snapshots(small_repo, tmp_path):
    src = tmp_path / "src"
    write_file(src / "photos" / "shared.jpg", os.urandom(40000))
    write_file(src / "docs" / "only_first.txt", os.urandom(30000))
    first = run_backup(small_repo, [str(src)])
    (src / "docs" / "only_first.txt").unlink()
    write_file(src / "docs" / "only_second.txt", os.urandom(30000))
    second = run_backup(small_repo, [str(src)])
    return small_repo, src, first, second


def fresh(repo: Repository) -> Repository:
    return Repository.open(repo.backend.root, PASSWORD)


def test_check_passes_on_healthy_repository(two_snapshots):
    repo = two_snapshots[0]
    result = run_check(fresh(repo), read_data=True)
    assert result.ok, result.errors
    assert result.blobs_verified > 0


def _pack_holding(repo: Repository, rel_path: str, snapshot):
    """找出某個檔案第一個區塊所在的 pack 與位置。"""
    from homevault.restore import find_node

    repo.load_index(recover_orphans=False)
    node = find_node(repo, snapshot, rel_path)
    return repo.index.get(node.chunks[0])


def _flip_byte(path, offset):
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0xFF]))


def test_single_corrupted_byte_is_detected_and_affected_files_listed(two_snapshots):
    """驗收 11：損壞 pack 中的一個 byte，check --read-data 偵測並列出受影響的快照與檔案。"""
    repo, src, first, second = two_snapshots
    target = f"{src}/docs/only_first.txt"
    entry = _pack_holding(fresh(repo), target, first.snapshot)
    _flip_byte(repo.backend.root / pack_name(entry.pack_id), entry.offset + entry.length // 2)

    structural = run_check(fresh(repo), read_data=False)
    assert structural.ok  # 結構檢查不讀內容，偵測不到 byte 層級的損毀

    result = run_check(fresh(repo), read_data=True)
    assert not result.ok
    affected_files = {p for paths in result.affected.values() for p in paths}
    assert any(p.endswith("docs/only_first.txt") for p in affected_files)
    assert first.snapshot_id in result.affected
    assert second.snapshot_id not in result.affected
    assert not any("shared.jpg" in p for p in affected_files)


def test_missing_pack_is_detected_by_structural_check(two_snapshots):
    repo, src, first, _second = two_snapshots
    entry = _pack_holding(fresh(repo), f"{src}/photos/shared.jpg", first.snapshot)
    os.remove(repo.backend.root / pack_name(entry.pack_id))
    result = run_check(fresh(repo), read_data=False)
    assert not result.ok
    assert any("不存在" in e for e in result.errors)
    files = {p for paths in result.affected.values() for p in paths}
    assert any(p.endswith("photos/shared.jpg") for p in files)
    assert len(result.affected) == 2  # 兩個快照都引用 shared.jpg


def test_corrupted_pack_header_is_reported_but_blobs_still_verified(two_snapshots):
    repo = two_snapshots[0]
    some_pack = repo.list_pack_ids().pop()
    path = repo.backend.root / pack_name(some_pack)
    _flip_byte(path, os.path.getsize(path) - 10)
    result = run_check(fresh(repo), read_data=True)
    assert not result.ok
    assert any("header" in e for e in result.errors)
    assert result.affected == {}  # 資料本身完好，所有快照仍可還原


def test_orphan_packs_and_temp_files_are_info_not_errors(two_snapshots):
    repo = two_snapshots[0]
    # 模擬中斷：多寫一個沒有 index 的 pack，以及一個殘留的暫存檔
    r = fresh(repo)
    r.load_index()
    r.save_blob(BlobType.DATA, os.urandom(100_000))  # 超過 pack 大小，會寫出 pack
    (repo.backend.root / "tmp" / "leftover.tmp").write_bytes(b"partial")
    result = run_check(fresh(repo), read_data=True)
    assert result.ok, result.errors
    assert any("未被索引" in i for i in result.infos)


def test_index_pointing_to_wrong_blob_is_detected(two_snapshots):
    """index 損毀但檔案仍可解密時，雙重驗證（AAD + HMAC）可以抓出錯誤。"""
    repo = two_snapshots[0]
    r = fresh(repo)
    index_name = r.backend.list("index")[0]
    packs = decode_index_file(r.key, r.backend.read(index_name))
    entries = next(e for e in packs.values() if len(e) >= 2)
    # 讓前兩個 blob 的 offset 互換
    a, b = entries[0], entries[1]
    entries[0] = type(a)(a.type, a.id, b.offset, b.length, a.raw_length)
    entries[1] = type(b)(b.type, b.id, a.offset, a.length, b.raw_length)
    os.remove(r.backend.root / index_name)
    r.write_index(packs)
    result = run_check(fresh(repo), read_data=True)
    assert not result.ok
