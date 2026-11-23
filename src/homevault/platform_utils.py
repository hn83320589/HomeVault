"""作業系統相依的細節：持久化寫入、行程優先權、行程存活檢查。"""

from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def fsync_fd(fd: int) -> None:
    """確保資料真正寫到儲存裝置。

    macOS 的 fsync 只把資料交給磁碟，不會清除磁碟本身的寫入快取；
    要用 F_FULLFSYNC 才能在斷電時保證資料存在。檔案系統不支援時退回 fsync。
    """
    if IS_MACOS:
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def fsync_dir(path: str) -> None:
    """讓目錄項目（新增、rename、刪除）持久化。Windows 無法開啟目錄做 fsync，略過。"""
    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        fsync_fd(fd)
    finally:
        os.close(fd)


def replace_durable(src: str, dst: str) -> None:
    """以原子方式把 src 改名為 dst，並確保改名結果寫入裝置。"""
    if IS_WINDOWS:
        import ctypes

        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        ok = ctypes.windll.kernel32.MoveFileExW(  # type: ignore[attr-defined]
            src, dst, movefile_replace_existing | movefile_write_through
        )
        if not ok:
            raise ctypes.WinError()  # type: ignore[attr-defined]
        return
    os.replace(src, dst)
    fsync_dir(os.path.dirname(dst))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lower_process_priority() -> None:
    """降低優先權，避免晚上 11 點備份時拖慢家人正在使用的電腦。"""
    try:
        if IS_WINDOWS:
            import ctypes

            below_normal_priority_class = 0x4000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal_priority_class)
        else:
            os.nice(10)
    except OSError:
        pass  # 降低優先權失敗不影響備份正確性
