"""跨作業系統去重驗證（驗收 4），供 CI 使用。

macOS job 執行 ``first``：產生固定資料、建立備份庫並備份，再把備份庫上傳成 artifact。
Windows job 執行 ``second``：下載備份庫、產生完全相同的資料並備份，
驗證新增資料量 < 原始資料量的 1%，且兩個快照都能正確還原。
"""

from __future__ import annotations

import argparse
import hashlib
import random
import socket
import sys
from pathlib import Path

from homevault.backup import run_backup
from homevault.repository import Repository
from homevault.restore import run_restore

PASSWORD = "cross-os-password"
MiB = 1024 * 1024


def generate(root: Path) -> int:
    rng = random.Random(2024)
    total = 0
    for i in range(60):
        size = rng.randint(0, 2 * MiB)
        path = root / ["照片", "報稅資料", "影片 🎬"][i % 3] / f"檔案_{i:03d}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rng.randbytes(size))
        total += size
    return total


def digests(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["first", "second"])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--data", required=True)
    args = parser.parse_args()

    data = Path(args.data)
    total = generate(data)
    if args.step == "first":
        repo = Repository.init(args.repo, PASSWORD)
    else:
        repo = Repository.open(args.repo, PASSWORD)
    result = run_backup(repo, [str(data)])
    added = result.snapshot.summary["data_added"]
    print(f"主機 {socket.gethostname()}：資料 {total / MiB:.1f} MiB，新增 {added / MiB:.3f} MiB")

    if args.step == "second":
        ratio = added / total
        print(f"第二台電腦的新增比例：{ratio:.4%}")
        snaps = repo.list_snapshots()
        hosts = {s.hostname for _, s in snaps}
        if len(hosts) != 2:
            print(f"預期兩台不同的主機，實際為 {hosts}")
            return 1
        expected = digests(data)
        for sid, snap in snaps:
            out = Path(args.repo).parent / f"restore-{sid.hex()[:8]}"
            run_restore(repo, snap, snap.paths[0], out)
            if digests(out / data.name) != expected:
                print(f"快照 {sid.hex()[:8]}（{snap.hostname}）還原結果不符")
                return 1
        if ratio >= 0.01:
            print("新增資料量未低於 1%")
            return 1
        print("✓ 跨作業系統去重與還原驗證通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
