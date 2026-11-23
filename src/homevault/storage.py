"""備份庫的檔案存取層。

所有對備份庫的寫入都經過 ``LocalBackend``，並遵守「只新增、不覆寫」：
新檔先寫入 ``tmp/`` → fsync → 以原子 rename 放到正式位置。

會改變狀態的動作都拆成可覆寫的基本操作（``_op_*``），
測試用的 FaultInjectingBackend 可以在任一個基本操作上模擬當機。
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from homevault.errors import IntegrityError, RepositoryUnavailableError
from homevault.platform_utils import fsync_dir, fsync_fd, replace_durable

TMP_DIR = "tmp"


class ObjectMissingError(IntegrityError):
    """備份庫中應該存在的檔案不見了。"""


class LocalBackend:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    # ── 讀取 ─────────────────────────────────────────────

    def read(self, name: str) -> bytes:
        with self._io(name):
            return self._path(name).read_bytes()

    def read_range(self, name: str, offset: int, length: int) -> bytes:
        with self._io(name), open(self._path(name), "rb") as f:
            f.seek(offset)
            data = f.read(length)
        if len(data) != length:
            raise IntegrityError(f"{name} 長度不足，無法讀取 offset={offset} length={length}")
        return data

    def size(self, name: str) -> int:
        with self._io(name):
            return self._path(name).stat().st_size

    def exists(self, name: str) -> bool:
        with self._io(name):
            return self._path(name).exists()

    def list(self, directory: str) -> list[str]:
        """遞迴列出目錄下的所有檔案，回傳以 / 分隔的相對名稱。目錄不存在時回傳空清單。"""
        base = self._path(directory)
        with self._io(directory):
            if not base.is_dir():
                self._check_root()
                return []
            names = []
            for dirpath, _dirnames, filenames in os.walk(base):
                rel_dir = Path(dirpath).relative_to(self.root).as_posix()
                names.extend(f"{rel_dir}/{fn}" for fn in filenames)
            return sorted(names)

    # ── 寫入 ─────────────────────────────────────────────

    def write_atomic(self, name: str, data: bytes) -> None:
        tmp = f"{TMP_DIR}/{secrets.token_hex(16)}.tmp"
        with self._io(name):
            try:
                self._op_write(tmp, data)
                self._op_fsync(tmp)
                self._op_rename(tmp, name)
            except BaseException:
                self._discard_tmp(tmp)  # 例如 Ctrl+C：盡量不留下暫存檔
                raise

    def delete(self, name: str) -> None:
        with self._io(name):
            self._op_delete(name)

    def makedirs(self, directory: str) -> None:
        with self._io(directory):
            self._op_mkdir(directory)

    def clean_tmp(self, max_age_seconds: float) -> int:
        """刪除中斷後殘留在 tmp/ 的暫存檔。只刪舊檔，避免誤刪另一個行程正在寫的檔案。"""
        removed = 0
        now = time.time()
        for name in self.list(TMP_DIR):
            try:
                if now - self._path(name).stat().st_mtime > max_age_seconds:
                    self.delete(name)
                    removed += 1
            except (OSError, IntegrityError):
                continue  # 另一個行程剛好處理掉了
        return removed

    # ── 基本操作（故障注入的接縫）──────────────────────────

    def _op_write(self, name: str, data: bytes) -> None:
        path = self._path(name)
        self._ensure_parent(path)
        with open(path, "xb") as f:
            f.write(data)

    def _op_fsync(self, name: str) -> None:
        # Windows 的 fsync（FlushFileBuffers）需要可寫入的 handle
        fd = os.open(self._path(name), os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            fsync_fd(fd)
        finally:
            os.close(fd)

    def _op_rename(self, src: str, dst: str) -> None:
        dst_path = self._path(dst)
        self._ensure_parent(dst_path)
        replace_durable(str(self._path(src)), str(dst_path))

    def _op_delete(self, name: str) -> None:
        path = self._path(name)
        path.unlink()
        fsync_dir(str(path.parent))

    def _discard_tmp(self, name: str) -> None:
        # 清除失敗不影響正確性：tmp/ 的內容永遠不會被讀取，之後也會被清理
        with suppress(OSError):
            self._path(name).unlink()

    def _op_mkdir(self, directory: str) -> None:
        self._check_root()
        self._path(directory).mkdir(parents=True, exist_ok=True)

    # ── 內部工具 ─────────────────────────────────────────

    def _path(self, name: str) -> Path:
        return self.root.joinpath(*name.split("/"))

    def _ensure_parent(self, path: Path) -> None:
        # 先確認根目錄存在：硬碟被拔除後 macOS 的 /Volumes/<名稱> 會消失，
        # 若直接 mkdir(parents=True) 會在內建硬碟上建立同名目錄並把備份寫到錯的地方。
        self._check_root()
        path.parent.mkdir(parents=True, exist_ok=True)

    def _check_root(self) -> None:
        if not self.root.is_dir():
            raise RepositoryUnavailableError(
                f"無法存取備份庫 {self.root}（外接硬碟是否被移除？）"
                "已完成的部分下次執行時會自動沿用。"
            )

    @contextmanager
    def _io(self, name: str) -> Iterator[None]:
        """把 OSError 轉換成有意義的錯誤：硬碟被拔除 vs. 備份庫內缺少檔案。"""
        try:
            yield
        except FileNotFoundError:
            self._check_root()
            raise ObjectMissingError(f"備份庫缺少檔案：{name}") from None
        except OSError as exc:
            self._check_root()
            raise RepositoryUnavailableError(f"存取備份庫檔案 {name} 時發生錯誤：{exc}") from exc
