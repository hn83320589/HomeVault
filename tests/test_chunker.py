import io
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from homevault.chunker import (
    DEFAULT_PARAMS,
    Chunker,
    ChunkerParams,
    gear_table,
    reference_chunk_lengths,
)

SEED = b"\x01" * 32
# 縮小版參數：讓性質測試在幾 KB 的資料上就能產生許多切點
SMALL = ChunkerParams(min_size=64, avg_size=256, max_size=1024)


def chunk_lengths(chunker: Chunker, data: bytes) -> list[int]:
    return [len(c) for c in chunker.iter_chunks(io.BytesIO(data))]


def random_bytes(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


class TestParams:
    def test_default_params_match_requirements(self):
        assert DEFAULT_PARAMS.min_size == 256 * 1024
        assert DEFAULT_PARAMS.avg_size == 1024 * 1024
        assert DEFAULT_PARAMS.max_size == 4 * 1024 * 1024

    @pytest.mark.parametrize(
        ("mn", "avg", "mx"),
        [(32, 256, 1024), (64, 300, 1024), (512, 256, 1024), (64, 2048, 1024)],
    )
    def test_invalid_params_rejected(self, mn, avg, mx):
        with pytest.raises(ValueError):
            ChunkerParams(min_size=mn, avg_size=avg, max_size=mx)


def test_gear_table_is_deterministic_and_seed_dependent():
    t1, t2 = gear_table(SEED), gear_table(SEED)
    assert t1 == t2
    assert len(t1) == 256
    assert all(0 <= v < 2**64 for v in t1)
    assert gear_table(b"\x02" * 32) != t1


def test_empty_input_yields_no_chunks():
    assert chunk_lengths(Chunker(SEED, SMALL), b"") == []
    assert reference_chunk_lengths(b"", gear_table(SEED), SMALL) == []


def test_input_smaller_than_min_is_single_chunk():
    data = random_bytes(50, 1)
    assert chunk_lengths(Chunker(SEED, SMALL), data) == [50]


@settings(max_examples=200, deadline=None)
@given(data=st.binary(max_size=8000))
def test_reference_respects_size_bounds(data):
    lengths = reference_chunk_lengths(data, gear_table(SEED), SMALL)
    assert sum(lengths) == len(data)
    for n in lengths[:-1]:
        assert SMALL.min_size <= n <= SMALL.max_size
    if lengths:
        assert 0 < lengths[-1] <= SMALL.max_size


@settings(max_examples=200, deadline=None)
@given(
    data=st.one_of(
        st.binary(max_size=12000),
        st.integers(0, 2**32).map(lambda s: random_bytes(20000, s)),
    ),
    read_size=st.integers(SMALL.max_size, 5000),
)
def test_fast_chunker_equals_reference(data, read_size):
    """numpy 快速版本必須與純 Python 參考實作產生完全相同的切點。"""
    expected = reference_chunk_lengths(data, gear_table(SEED), SMALL)
    actual = chunk_lengths(Chunker(SEED, SMALL, read_size=read_size), data)
    assert actual == expected


def test_chunks_concatenate_to_original():
    data = random_bytes(30000, 7)
    chunks = list(Chunker(SEED, SMALL).iter_chunks(io.BytesIO(data)))
    assert b"".join(chunks) == data


def test_constant_data_cuts_at_max_size():
    data = b"\x00" * 5000
    lengths = chunk_lengths(Chunker(SEED, SMALL), data)
    assert lengths[:-1] == [SMALL.max_size] * (len(lengths) - 1)


def test_average_chunk_size_is_close_to_target():
    params = ChunkerParams(min_size=1024, avg_size=4096, max_size=16384)
    lengths = chunk_lengths(Chunker(SEED, params), random_bytes(8 * 1024 * 1024, 3))
    mean = sum(lengths) / len(lengths)
    assert 0.8 * params.avg_size <= mean <= 1.4 * params.avg_size


def test_different_seeds_give_different_boundaries():
    data = random_bytes(50000, 11)
    a = chunk_lengths(Chunker(b"\x01" * 32, SMALL), data)
    b = chunk_lengths(Chunker(b"\x02" * 32, SMALL), data)
    assert a != b


@pytest.mark.slow
def test_insert_10_bytes_into_50mib_file_changes_less_than_5mib():
    """驗收 3：在 50 MiB 檔案開頭插入 10 bytes，新產生的區塊合計 < 5 MiB。"""
    original = random_bytes(50 * 1024 * 1024, 42)
    modified = b"0123456789" + original
    chunker = Chunker(SEED, DEFAULT_PARAMS)
    before = set(chunker.iter_chunks(io.BytesIO(original)))
    after = list(chunker.iter_chunks(io.BytesIO(modified)))
    new_bytes = sum(len(c) for c in after if c not in before)
    assert new_bytes < 5 * 1024 * 1024
