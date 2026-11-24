"""故障注入：在第 k 個會改變狀態的操作上模擬行程當機或斷電。"""

from __future__ import annotations

import os
import random

from homevault.storage import LocalBackend


class SimulatedCrash(BaseException):
    """模擬行程突然死亡。繼承 BaseException，不會被一般的 except Exception 攔下。"""


class FaultInjectingBackend(LocalBackend):
    """計算 write / fsync / rename / delete / mkdir 操作，在第 crash_at 個操作時當機。

    * 當機的操作若是 write，只寫入前半段資料（torn write）。
    * power_loss=True：當機時，尚未 fsync 的檔案被截斷成隨機長度（寫入快取遺失）。
    * 當機後所有操作（包含讀取）都會失敗，就像行程已經不存在。
    """

    def __init__(self, root, crash_at: int | None = None, *, power_loss: bool = False) -> None:
        super().__init__(root)
        self.crash_at = crash_at
        self.power_loss = power_loss
        self.ops = 0
        self.crashed = False
        self.log: list[tuple[str, str]] = []
        self._unsynced: set[str] = set()
        self._rng = random.Random(crash_at)

    # ── 計數與當機 ───────────────────────────────────────

    def _hit(self, kind: str, name: str) -> bool:
        self._ensure_alive()
        self.ops += 1
        self.log.append((kind, name))
        return self.crash_at is not None and self.ops == self.crash_at

    def _ensure_alive(self) -> None:
        if self.crashed:
            raise SimulatedCrash("行程已當機")

    def _crash(self) -> None:
        self.crashed = True
        if self.power_loss:
            for name in self._unsynced:
                path = self._path(name)
                if path.exists():
                    size = path.stat().st_size
                    with open(path, "r+b") as f:
                        f.truncate(self._rng.randint(0, size))
        raise SimulatedCrash(f"在第 {self.ops} 個操作模擬當機：{self.log[-1]}")

    # ── 會改變狀態的操作 ─────────────────────────────────

    def _op_write(self, name: str, data: bytes) -> None:
        if self._hit("write", name):
            super()._op_write(name, data[: len(data) // 2])
            self._unsynced.add(name)
            self._crash()
        super()._op_write(name, data)
        self._unsynced.add(name)

    def _op_fsync(self, name: str) -> None:
        if self._hit("fsync", name):
            self._crash()
        super()._op_fsync(name)
        self._unsynced.discard(name)

    def _op_rename(self, src: str, dst: str) -> None:
        if self._hit("rename", dst):
            self._crash()
        super()._op_rename(src, dst)
        if src in self._unsynced:
            self._unsynced.discard(src)
            self._unsynced.add(dst)

    def _op_delete(self, name: str) -> None:
        if self._hit("delete", name):
            self._crash()
        super()._op_delete(name)

    def _discard_tmp(self, name: str) -> None:
        if not self.crashed:  # 已當機的行程沒有機會清理
            super()._discard_tmp(name)

    def _op_mkdir(self, directory: str) -> None:
        if self._hit("mkdir", directory):
            self._crash()
        super()._op_mkdir(directory)

    # ── 讀取：當機後一律失敗 ─────────────────────────────

    def read(self, name):
        self._ensure_alive()
        return super().read(name)

    def read_range(self, name, offset, length):
        self._ensure_alive()
        return super().read_range(name, offset, length)

    def size(self, name):
        self._ensure_alive()
        return super().size(name)

    def exists(self, name):
        self._ensure_alive()
        return super().exists(name)

    def list(self, directory):
        self._ensure_alive()
        return super().list(directory)


def op_kinds(log: list[tuple[str, str]]) -> set[str]:
    return {kind for kind, _ in log}


def op_signature(log: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """操作序列的簽章：(操作種類, 作用的目錄)。檔名含隨機值與雜湊，每次都不同，不列入比對。"""
    return [(kind, name.split("/", 1)[0]) for kind, name in log]


def file_count(root) -> int:
    return sum(len(files) for _, _, files in os.walk(root))
