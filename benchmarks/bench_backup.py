"""效能量測：首次備份 500 MiB 混合資料（驗收 12）。

用法::

    uv run python benchmarks/bench_backup.py [--data-dir bench-data] [--keep]

資料以固定亂數種子產生，可重現：
* 照片：300 MiB，120 個 1~4 MiB 的不可壓縮檔案
* 影片：100 MiB，2 個 50 MiB 的不可壓縮檔案
* 文件：100 MiB，2,000 個可壓縮的文字檔
透過真正的 CLI 子行程執行，時間包含行程啟動與 scrypt 金鑰推導。
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from homevault.cli import ensure_utf8_output

MiB = 1024 * 1024
PASSWORD = "benchmark-password"
WORDS = ["報稅", "收據", "家庭", "旅行", "照片", "影片", "合約", "保險", "帳單", "學校"]
WORDS += ["the", "of", "and", "family", "photo", "receipt", "invoice", "income", "tax"]


def generate(root: Path) -> int:
    rng = random.Random(12)
    total = 0
    photos = root / "照片"
    for i in range(120):
        size = rng.randint(1 * MiB, 4 * MiB) if i < 119 else max(MiB, 300 * MiB - total)
        _write(photos / f"{2010 + i % 15}" / f"IMG_{i:04d}.jpg", rng.randbytes(size))
        total += size
    for i in range(2):
        _write(root / "影片" / f"家庭影片_{i}.mp4", rng.randbytes(50 * MiB))
        total += 50 * MiB
    doc_size = 100 * MiB // 2000
    for i in range(2000):
        words = rng.choices(WORDS, k=20000)
        data = " ".join(words).encode()[:doc_size]
        assert len(data) == doc_size
        _write(root / "文件" / f"{i % 20:02d}" / f"doc_{i:04d}.txt", data)
        total += len(data)
    return total


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def run_cli(*args: str) -> tuple[float, str]:
    env = {**os.environ, "HOMEVAULT_PASSWORD": PASSWORD}
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "homevault", *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        raise SystemExit(f"指令失敗：{args}\n{proc.stdout}\n{proc.stderr}")
    return elapsed, proc.stdout


def machine_info() -> str:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        brand = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        ).stdout.strip()
        cpu = brand or cpu
    py = platform.python_version()
    return f"{platform.platform()}；CPU {cpu}（{os.cpu_count()} 核）；Python {py}"


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    ensure_utf8_output()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="bench-data")
    parser.add_argument("--keep", action="store_true", help="保留產生的資料與備份庫")
    parser.add_argument("--threads", type=int, default=None, help="傳給 backup 的執行緒數")
    args = parser.parse_args()

    root = Path(args.data_dir).resolve()
    if root.exists():
        shutil.rmtree(root)
    src, repo = root / "src", root / "repo"
    print("產生測試資料…", flush=True)
    total = generate(src)
    files = sum(1 for f in src.rglob("*") if f.is_file())

    run_cli("init", "--repo", str(repo))
    extra = ["--threads", str(args.threads)] if args.threads else []
    first, out1 = run_cli("backup", str(src), "--repo", str(repo), "-q", *extra)
    second, _ = run_cli("backup", str(src), "--repo", str(repo), "-q")
    check, _ = run_cli("check", "--repo", str(repo))

    print(f"測試機器：{machine_info()}")
    print(f"資料：{files:,} 個檔案，{total / MiB:.0f} MiB")
    print(f"執行緒：{args.threads or '預設'}")
    print(f"首次備份：{first:.1f} 秒（{total / MiB / first:.0f} MiB/s）")
    print(f"增量備份（無變動）：{second:.1f} 秒")
    print(f"check：{check:.1f} 秒")
    print(f"備份庫大小：{dir_size(repo) / MiB:.0f} MiB")
    print(out1.strip())
    if not args.keep:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
