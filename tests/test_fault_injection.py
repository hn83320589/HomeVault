"""驗收 10：以故障注入測試中斷安全。

對 backup、forget、prune 三個流程，**窮舉每一個會改變狀態的操作**
（write、fsync、rename、delete、mkdir）作為中斷點。每次中斷後驗證：

1. ``check --read-data`` 通過；
2. 中斷前已存在的快照都能完整還原（內容雜湊與 mtime 完全相同）；
3. 重新執行原本的指令可以成功完成，之後 ``check --read-data`` 仍然通過。

為了讓操作序列可重現，先建立一份範本（備份庫 + 來源資料夾），每個案例都從
範本複製出完全相同的狀態（相同金鑰、相同資料），因此第 k 個操作永遠是同一個動作。
"""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import PASSWORD, SMALL_CHUNKS, SMALL_PACK, file_manifest, write_file
from faults import FaultInjectingBackend, SimulatedCrash, op_kinds, op_signature

from homevault.backup import run_backup
from homevault.check import run_check
from homevault.forget import run_forget
from homevault.prune import run_prune
from homevault.repository import Repository
from homevault.restore import run_restore
from homevault.retention import KeepPolicy

BASE_MTIME = 1_650_000_000_000_000_000


@dataclass
class Template:
    store: Path  # 範本的保存位置
    live: Path  # 每個案例實際操作的位置（固定路徑，增量備份才找得到 parent）
    manifests: dict[bytes, dict] = field(default_factory=dict)
    times: dict[bytes, object] = field(default_factory=dict)

    @property
    def repo(self) -> Path:
        return self.live / "repo"

    @property
    def src(self) -> Path:
        return self.live / "src"

    def reset(self) -> None:
        if self.live.exists():
            shutil.rmtree(self.live)
        shutil.copytree(self.store, self.live, symlinks=True)


def _mutate(src: Path, round_no: int) -> None:
    rng = random.Random(round_no)
    for i in range(3):
        name = f"round{round_no}_{i}.bin"
        write_file(src / f"dir{i % 2}" / name, rng.randbytes(rng.randint(5_000, 40_000)),
                   BASE_MTIME + round_no * 1000 + i)  # fmt: skip
    files = sorted(p for p in src.rglob("*.bin") if p.is_file())
    victim = files[rng.randrange(len(files))]
    write_file(victim, rng.randbytes(25_000), BASE_MTIME + round_no * 1000 + 99)
    if round_no >= 2:
        files[0].unlink()


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Template:
    base = tmp_path_factory.mktemp("fault")
    t = Template(store=base / "template", live=base / "live")
    repo = Repository.init(
        t.repo, PASSWORD, chunker_params=SMALL_CHUNKS, pack_size=SMALL_PACK, kdf_n=2**10
    )
    rng = random.Random(0)
    for i in range(10):
        write_file(t.src / f"dir{i % 3}" / f"base{i}.bin", rng.randbytes(rng.randint(0, 30_000)),
                   BASE_MTIME + i)  # fmt: skip
    write_file(t.src / "空檔案.txt", b"", BASE_MTIME)
    write_file(t.src / "相簿 📷" / "照片.jpg", rng.randbytes(20_000), BASE_MTIME)
    for round_no in range(4):  # 4 個既有快照
        if round_no:
            _mutate(t.src, round_no)
        result = run_backup(repo, [str(t.src)])
        t.manifests[result.snapshot_id] = file_manifest(t.src)
        t.times[result.snapshot_id] = result.snapshot.datetime
    shutil.copytree(t.live, t.store, symlinks=True)
    return t


# ── 三個情境 ─────────────────────────────────────────────


class Scenario:
    name: str

    def prepare(self, t: Template) -> None:
        """執行被測指令之前的準備（不注入故障）。"""

    def run(self, repo: Repository, t: Template) -> None:
        raise NotImplementedError

    def verify_after_rerun(self, t: Template, repo: Repository) -> None:
        """重跑成功後的額外檢查。"""


class BackupScenario(Scenario):
    name = "backup"

    def prepare(self, t):
        _mutate(t.src, 10)
        rng = random.Random(10)
        for i in range(6):  # 多寫幾個 pack，讓中斷點分布在 pack 寫入之間
            write_file(t.src / "new" / f"video{i}.bin", rng.randbytes(40_000), BASE_MTIME + i)

    def run(self, repo, t):
        run_backup(repo, [str(t.src)])

    def verify_after_rerun(self, t, repo):
        sid, snap = repo.find_snapshot("latest")
        assert sid not in t.manifests
        out = t.live / "verify-new"
        run_restore(repo, snap, str(t.src), out)
        assert file_manifest(out / "src") == file_manifest(t.src)


class CheckpointBackupScenario(BackupScenario):
    """每 2 個 pack 就寫一次 index checkpoint，涵蓋備份途中寫 index 的中斷點。"""

    name = "backup-checkpoint"

    def run(self, repo, t):
        from homevault import repository

        original = repository.CHECKPOINT_EVERY_PACKS
        repository.CHECKPOINT_EVERY_PACKS = 2
        try:
            super().run(repo, t)
        finally:
            repository.CHECKPOINT_EVERY_PACKS = original


class ForgetScenario(Scenario):
    name = "forget"

    def run(self, repo, t):
        run_forget(repo, KeepPolicy(last=2))

    def verify_after_rerun(self, t, repo):
        newest_two = sorted(t.manifests, key=lambda s: t.times[s])[-2:]
        assert sorted(sid for sid, _ in repo.list_snapshots()) == sorted(newest_two)


class PruneScenario(Scenario):
    name = "prune"

    def prepare(self, t):
        run_forget(Repository.open(t.repo, PASSWORD), KeepPolicy(last=2))

    def run(self, repo, t):
        run_prune(repo, threshold=0.0)

    def verify_after_rerun(self, t, repo):
        size = sum(f.stat().st_size for f in (t.repo / "data").rglob("*") if f.is_file())
        template_size = sum(
            f.stat().st_size for f in (t.store / "repo" / "data").rglob("*") if f.is_file()
        )
        assert size < template_size


SCENARIOS = {
    s.name: s
    for s in (BackupScenario(), CheckpointBackupScenario(), ForgetScenario(), PruneScenario())
}
_op_counts: dict[str, tuple[int, set[str]]] = {}
_op_signatures: dict[str, list[tuple[str, str]]] = {}


def count_ops(t: Template, scenario: Scenario) -> tuple[int, set[str]]:
    if scenario.name not in _op_counts:
        t.reset()
        scenario.prepare(t)
        backend = FaultInjectingBackend(t.repo)
        scenario.run(Repository.open(t.repo, PASSWORD, backend=backend), t)
        _op_counts[scenario.name] = (backend.ops, op_kinds(backend.log))
        _op_signatures[scenario.name] = op_signature(backend.log)
    return _op_counts[scenario.name]


def sequence_mismatch(
    counted: list[tuple[str, str]],
    actual: list[tuple[str, str]],
    *,
    crashed: bool,
    crash_at: int,
) -> str | None:
    """比對「計數時」與「這次中斷時」的操作序列。一致時回傳 None，否則回傳診斷訊息。

    窮舉中斷點的前提是同一個情境每次執行的操作序列完全相同；若不同，第 k 個中斷點
    可能落在不同的動作上，或根本到達不了，測試結果就失去意義。
    """
    problems = []
    for i, (want, got) in enumerate(zip(counted, actual, strict=False), 1):
        if want != got:
            problems.append(f"第 {i} 個操作不一致：計數時為 {want}，這次為 {got}")
            break
    if not crashed:
        problems.append(
            f"這次執行沒有到達第 {crash_at} 個操作就結束了"
            f"（計數時共 {len(counted)} 個操作，這次只有 {len(actual)} 個）"
        )
    if not problems:
        return None
    return (
        "操作序列不確定，這個中斷點的測試結果不可信："
        + "；".join(problems)
        + f"\n這次最後 8 個操作：{actual[-8:]}"
    )


def crash_and_verify(t: Template, scenario: Scenario, crash_at: int, power_loss: bool) -> None:
    count_ops(t, scenario)  # 確保已取得計數時的操作序列
    t.reset()
    scenario.prepare(t)
    existing_before = set(Repository.open(t.repo, PASSWORD).list_snapshot_ids())

    backend = FaultInjectingBackend(t.repo, crash_at, power_loss=power_loss)
    crashed = False
    try:
        scenario.run(Repository.open(t.repo, PASSWORD, backend=backend), t)
    except SimulatedCrash:
        crashed = True
    mismatch = sequence_mismatch(
        _op_signatures[scenario.name], op_signature(backend.log), crashed=crashed, crash_at=crash_at
    )
    assert mismatch is None, mismatch

    try:
        _verify_after_crash(t, scenario, existing_before, backend)
    except Exception as exc:
        raise AssertionError(
            f"[{scenario.name}] 在第 {crash_at} 個操作 {backend.log[-1]} 中斷後驗證失敗："
            f"{type(exc).__name__}: {exc}\n中斷前最後 8 個操作：{backend.log[-8:]}"
        ) from exc


def _verify_after_crash(
    t: Template, scenario: Scenario, existing_before: set[bytes], backend: FaultInjectingBackend
) -> None:
    # 1. 完整性檢查通過
    result = run_check(Repository.open(t.repo, PASSWORD), read_data=True)
    assert result.ok, f"中斷於 {backend.log[-1]} 後 check 失敗：{result.errors}"

    # 2. 中斷前已存在、且仍在的快照都能完整還原
    repo = Repository.open(t.repo, PASSWORD)
    remaining = set(repo.list_snapshot_ids())
    if scenario.name != "forget":
        assert remaining >= existing_before, "非 forget 的流程不可讓既有快照消失"
    for sid in remaining & existing_before:
        snap = repo.load_snapshot(sid)
        out = t.live / f"verify-{sid.hex()[:8]}"
        restored = run_restore(repo, snap, snap.paths[0], out)
        assert restored.errors == []
        assert file_manifest(out / "src") == t.manifests[sid]

    # 3. 重新執行原本的指令可以成功完成
    scenario.run(Repository.open(t.repo, PASSWORD), t)
    assert run_check(Repository.open(t.repo, PASSWORD), read_data=True).ok
    scenario.verify_after_rerun(t, Repository.open(t.repo, PASSWORD))


# 預期的最大操作數；實際數量在執行時計算，超出部分直接通過（見 test_operation_counts）
MAX_POINTS = {"backup": 36, "backup-checkpoint": 40, "forget": 8, "prune": 36}
CASES = [(name, k) for name, n in MAX_POINTS.items() for k in range(1, n + 1)]


@pytest.mark.parametrize(("scenario_name", "crash_at"), CASES)
def test_crash_at_every_operation(template, scenario_name, crash_at):
    scenario = SCENARIOS[scenario_name]
    total, _ = count_ops(template, scenario)
    if crash_at > total:
        pytest.skip(f"{scenario_name} 只有 {total} 個操作")
    crash_and_verify(template, scenario, crash_at, power_loss=False)


@pytest.mark.parametrize("fraction", [0.1, 0.3, 0.5, 0.7, 0.9, 1.0])
@pytest.mark.parametrize("scenario_name", ["backup", "prune"])
def test_power_loss_truncates_unsynced_files(template, scenario_name, fraction):
    """斷電模式：尚未 fsync 的檔案內容遺失。"""
    scenario = SCENARIOS[scenario_name]
    total, _ = count_ops(template, scenario)
    crash_and_verify(template, scenario, max(1, round(total * fraction)), power_loss=True)


def test_operation_counts_cover_all_kinds_and_fit_parametrization(template):
    """確保參數化涵蓋了每一個操作，且三個流程合計遠超過 20 個中斷點。"""
    total_points = 0
    for name, scenario in SCENARIOS.items():
        total, kinds = count_ops(template, scenario)
        assert total <= MAX_POINTS[name], f"{name} 有 {total} 個操作，請調高 MAX_POINTS"
        total_points += total
        assert {"write", "fsync", "rename"} <= kinds
    checkpoint_log_kinds = count_ops(template, SCENARIOS["backup-checkpoint"])[0]
    assert checkpoint_log_kinds > count_ops(template, SCENARIOS["backup"])[0]
    assert {"delete"} <= count_ops(template, SCENARIOS["prune"])[1]
    assert {"delete"} <= count_ops(template, SCENARIOS["forget"])[1]
    assert total_points >= 20


def test_interrupted_backup_reuses_written_packs(template):
    """需求 7：備份中斷後重跑，已寫入的區塊會被沿用，不必從頭來過。"""
    t = template
    scenario = SCENARIOS["backup"]
    total, _ = count_ops(t, scenario)

    t.reset()
    scenario.prepare(t)
    full = run_backup(Repository.open(t.repo, PASSWORD), [str(t.src)])
    full_added = full.snapshot.summary["data_added_stored"]

    t.reset()
    scenario.prepare(t)
    backend = FaultInjectingBackend(t.repo, total - 3)  # 在寫 snapshot 之前中斷
    with pytest.raises(SimulatedCrash):
        run_backup(Repository.open(t.repo, PASSWORD, backend=backend), [str(t.src)])
    resumed = run_backup(Repository.open(t.repo, PASSWORD), [str(t.src)])
    assert resumed.snapshot.summary["data_added_stored"] < full_added * 0.2


# ── 診斷工具本身的測試 ───────────────────────────────────


def test_sequence_check_accepts_identical_prefix():
    counted = [("write", "tmp"), ("fsync", "tmp"), ("rename", "data"), ("delete", "locks")]
    assert sequence_mismatch(counted, counted[:3], crashed=True, crash_at=3) is None


def test_sequence_check_reports_run_that_ended_before_crash_point():
    counted = [("write", "tmp"), ("fsync", "tmp"), ("rename", "data")]
    message = sequence_mismatch(counted, counted[:2], crashed=False, crash_at=3)
    assert "沒有到達第 3 個操作" in message
    assert "計數時共 3 個操作，這次只有 2 個" in message


def test_sequence_check_reports_first_divergence():
    counted = [("write", "tmp"), ("fsync", "tmp"), ("rename", "data")]
    actual = [("write", "tmp"), ("delete", "locks"), ("rename", "data")]
    message = sequence_mismatch(counted, actual, crashed=True, crash_at=3)
    assert "第 2 個操作" in message
    assert "('fsync', 'tmp')" in message
    assert "('delete', 'locks')" in message
