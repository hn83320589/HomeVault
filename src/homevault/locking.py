"""備份庫的 lock。

* backup 只會新增檔案，使用共享鎖，可與其他 backup 同時執行。
* forget / prune 會刪除檔案，使用獨佔鎖。

殘留 lock（外接硬碟被拔除、斷電）的判定：
* 超過 STALE_AFTER 秒沒有更新 → 殘留（執行中的 lock 每 REFRESH_EVERY 秒更新一次）。
* 同一台主機、而該行程已不存在 → 殘留。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass

from homevault.crypto import MasterKey, seal, unseal
from homevault.errors import IntegrityError, RepositoryLockedError
from homevault.platform_utils import pid_alive
from homevault.storage import LocalBackend, ObjectMissingError

log = logging.getLogger("homevault")

LOCK_DIR = "locks"
STALE_AFTER = 30 * 60
REFRESH_EVERY = 5 * 60
_AAD = b"homevault-lock-v1"

# 本行程目前持有的 lock session。用來辨識「同一個 pid 但不是自己持有」的殘留 lock，
# 例如同一個行程內先前一次被中斷的操作（測試中的故障注入就屬於這種情況）。
_active_sessions: set[str] = set()


@dataclass
class LockInfo:
    hostname: str
    pid: int
    exclusive: bool
    created: float
    refreshed: float
    session: str

    def describe(self) -> str:
        kind = "獨佔" if self.exclusive else "共享"
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.refreshed))
        return f"{self.hostname}（pid {self.pid}，{kind}鎖，最後更新 {when}）"


class Lock:
    def __init__(
        self,
        backend: LocalBackend,
        key: MasterKey,
        *,
        exclusive: bool,
        hostname: str | None = None,
    ) -> None:
        self._backend = backend
        self._key = key
        now = time.time()
        self.info = LockInfo(
            hostname=hostname or socket.gethostname(),
            pid=os.getpid(),
            exclusive=exclusive,
            created=now,
            refreshed=now,
            session=secrets.token_hex(16),
        )
        self._name: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mutex = threading.Lock()  # refresh 執行緒與 check_held 不可同時更新
        self.lost = False

    def __enter__(self) -> Lock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self) -> None:
        self._remove_stale_and_check_conflicts()
        _active_sessions.add(self.info.session)
        try:
            self._name = self._write()
            self._remove_stale_and_check_conflicts()  # 處理兩台電腦同時建立 lock 的競爭
        except BaseException:
            # 包含當機與 Ctrl+C：不論成功與否都要讓 session 失效，殘留的 lock 才會被判定為殘留
            self.release()
            raise
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        # 先等 refresh 執行緒結束，否則它可能在我們刪除後又寫出新的 lock 檔
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
        _active_sessions.discard(self.info.session)
        if self._name is None:
            return
        name, self._name = self._name, None
        try:
            self._backend.delete(name)
        except Exception as exc:
            log.warning("無法移除 lock 檔 %s：%s（下次執行時會自動判定為殘留 lock）", name, exc)

    def _write(self) -> str:
        name = f"{LOCK_DIR}/{secrets.token_hex(32)}"
        payload = json.dumps(asdict(self.info)).encode()
        self._backend.write_atomic(name, seal(self._key.enc_key, payload, _AAD))
        return name

    def check_held(self) -> None:
        """在提交快照、刪除資料等關鍵步驟之前呼叫，確認 lock 仍然有效。

        電腦睡眠或硬碟卡住時，refresh 可能延遲超過 STALE_AFTER，另一台電腦就會把
        我們的 lock 當成殘留刪除，並開始 prune。所以：太久沒更新就立刻補更新，
        補更新時發現舊 lock 已被刪除，代表 lock 已失效，必須中止。
        """
        with self._mutex:
            if not self.lost and time.time() - self.info.refreshed > REFRESH_EVERY:
                self._refresh_locked()
            if not self.lost and (self._name is None or not self._backend.exists(self._name)):
                self.lost = True
            if self.lost:
                raise RepositoryLockedError(
                    "lock 已失效：本次執行期間，另一台電腦把這次操作的 lock 判定為殘留並移除"
                    "（可能是電腦睡眠或硬碟停頓太久）。為了安全已中止，請重新執行。"
                )

    def _refresh_locked(self) -> None:
        old = self._name
        self.info.refreshed = time.time()
        self._name = self._write()
        if old:
            try:
                self._backend.delete(old)
            except ObjectMissingError:
                self.lost = True  # 舊 lock 已被別人刪除：在這段期間我們沒有受到保護

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_EVERY):
            try:
                with self._mutex:
                    if self.lost:
                        return
                    self._refresh_locked()
            except Exception as exc:
                log.warning("更新 lock 失敗，稍後重試：%s", exc)  # 例如硬碟暫時無回應
            except BaseException:
                return  # 測試中的模擬當機：行程已不存在

    def _remove_stale_and_check_conflicts(self) -> None:
        for name, info in read_locks(self._backend, self._key):
            if name == self._name:
                continue
            if info is None or is_stale(info):
                log.info("移除殘留的 lock：%s", info.describe() if info else name)
                # 另一台電腦可能同時清掉了同一個殘留 lock
                with contextlib.suppress(ObjectMissingError):
                    self._backend.delete(name)
                continue
            if self.info.exclusive or info.exclusive:
                raise RepositoryLockedError(
                    f"備份庫正在被使用：{info.describe()}。\n"
                    "如果確定沒有其他電腦正在執行 homevault，可以執行 `homevault unlock` 清除。"
                )


def read_locks(backend: LocalBackend, key: MasterKey) -> list[tuple[str, LockInfo | None]]:
    """回傳所有 lock；無法解讀的 lock 以 None 表示。"""
    result: list[tuple[str, LockInfo | None]] = []
    for name in backend.list(LOCK_DIR):
        try:
            raw = unseal(key.enc_key, backend.read(name), _AAD)
            result.append((name, LockInfo(**json.loads(raw))))
        except IntegrityError:
            result.append((name, None))
        except (ValueError, TypeError):
            result.append((name, None))
    return result


def is_stale(info: LockInfo, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if now - info.refreshed > STALE_AFTER:
        return True
    if info.hostname == socket.gethostname():
        if info.session in _active_sessions:
            return False
        if info.pid == os.getpid():
            return True
        return not pid_alive(info.pid)
    return False
