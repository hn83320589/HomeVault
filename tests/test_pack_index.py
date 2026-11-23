import os

import pytest

from homevault.crypto import MasterKey
from homevault.errors import IntegrityError
from homevault.index import Index, decode_index_file, encode_index_file
from homevault.pack import (
    BlobType,
    PackBuilder,
    PackEntry,
    decode_blob,
    encode_blob,
    pack_name,
    read_pack_header,
)
from homevault.storage import LocalBackend


@pytest.fixture
def key():
    return MasterKey.generate()


@pytest.fixture
def backend(tmp_path):
    return LocalBackend(tmp_path)


@pytest.mark.parametrize("plaintext", [b"a" * 10000, os.urandom(10000), b""])
def test_blob_roundtrip(key, plaintext):
    blob_id = key.blob_id(plaintext)
    sealed = encode_blob(key, BlobType.DATA, blob_id, plaintext)
    assert decode_blob(key, BlobType.DATA, blob_id, sealed) == plaintext


def test_compressible_blob_is_compressed(key):
    plaintext = b"tax report 2024 " * 1000
    sealed = encode_blob(key, BlobType.DATA, key.blob_id(plaintext), plaintext)
    assert len(sealed) < len(plaintext) // 4


def test_blob_with_wrong_type_or_id_is_rejected(key):
    plaintext = b"content"
    blob_id = key.blob_id(plaintext)
    sealed = encode_blob(key, BlobType.DATA, blob_id, plaintext)
    with pytest.raises(IntegrityError):
        decode_blob(key, BlobType.TREE, blob_id, sealed)
    with pytest.raises(IntegrityError):
        decode_blob(key, BlobType.DATA, key.blob_id(b"other"), sealed)


def _build_pack(key, blobs):
    builder = PackBuilder(key)
    for data in blobs:
        blob_id = key.blob_id(data)
        sealed = encode_blob(key, BlobType.DATA, blob_id, data)
        builder.add(BlobType.DATA, blob_id, sealed, len(data))
    return builder.finish()


def test_pack_header_roundtrip(key, backend):
    blobs = [os.urandom(1000), b"x" * 5000, b"hello"]
    pack_id, raw, entries = _build_pack(key, blobs)
    backend.write_atomic(pack_name(pack_id), raw)
    assert read_pack_header(backend, pack_id, key) == entries
    for entry, data in zip(entries, blobs, strict=True):
        sealed = backend.read_range(pack_name(pack_id), entry.offset, entry.length)
        assert decode_blob(key, entry.type, entry.id, sealed) == data
        assert entry.raw_length == len(data)


def test_pack_name_is_hash_of_content_and_header_hides_ids(key):
    blobs = [b"secret family photo"]
    pack_id, raw, entries = _build_pack(key, blobs)
    import hashlib

    assert pack_id == hashlib.sha256(raw).digest()
    assert pack_name(pack_id) == f"data/{pack_id.hex()[:2]}/{pack_id.hex()}"
    assert entries[0].id not in raw


def test_truncated_pack_header_detected(key, backend):
    pack_id, raw, _ = _build_pack(key, [b"abc"])
    backend.write_atomic(pack_name(pack_id), raw[:-10])
    with pytest.raises(IntegrityError):
        read_pack_header(backend, pack_id, key)


def _entry(n, pack_offset=0):
    return PackEntry(BlobType.DATA, bytes([n]) * 32, pack_offset, 100, 200)


def test_index_lookup():
    index = Index()
    pack_a = b"\xaa" * 32
    index.add_pack(pack_a, [_entry(1, 0), _entry(2, 100)])
    found = index.get(bytes([2]) * 32)
    assert found.pack_id == pack_a
    assert (found.offset, found.length, found.raw_length) == (100, 100, 200)
    assert found.type is BlobType.DATA
    assert bytes([1]) * 32 in index
    assert bytes([9]) * 32 not in index
    assert len(index) == 2


def test_index_tolerates_duplicate_blobs_across_packs():
    index = Index()
    index.add_pack(b"\xaa" * 32, [_entry(1)])
    index.add_pack(b"\xbb" * 32, [_entry(1)])
    assert len(index) == 1
    assert index.get(bytes([1]) * 32).pack_id in {b"\xaa" * 32, b"\xbb" * 32}


def test_index_file_roundtrip(key):
    packs = {b"\xaa" * 32: [_entry(1), _entry(2, 100)], b"\xbb" * 32: [_entry(3)]}
    raw = encode_index_file(key, packs)
    assert decode_index_file(key, raw) == packs
    assert bytes([1]) * 32 not in raw


def test_tampered_index_file_detected(key):
    raw = bytearray(encode_index_file(key, {b"\xaa" * 32: [_entry(1)]}))
    raw[20] ^= 0xFF
    with pytest.raises(IntegrityError):
        decode_index_file(key, bytes(raw))
