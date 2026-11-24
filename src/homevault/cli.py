"""命令列介面。

密碼來源（依序）：``--password-file``、環境變數 ``HOMEVAULT_PASSWORD``、互動式輸入。
排程執行時沒有終端機，必須使用前兩者之一。
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import stat
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from homevault import __version__
from homevault.errors import HomeVaultError, UsageError

log = logging.getLogger("homevault")


# ── 格式化 ───────────────────────────────────────────────


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")


def fmt_mtime(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")


def short_id(snap_id: bytes) -> str:
    return snap_id.hex()[:8]


def out(text: str = "") -> None:
    print(text, flush=True)


# ── 共用 ─────────────────────────────────────────────────


def read_password(args: argparse.Namespace, *, confirm: bool = False) -> str:
    if args.password_file:
        try:
            return Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise UsageError(f"無法讀取密碼檔 {args.password_file}：{exc}") from None
    env = os.environ.get("HOMEVAULT_PASSWORD")
    if env:
        return env
    if sys.stdin.isatty():
        password = getpass.getpass("備份庫密碼：")
        if confirm and getpass.getpass("再輸入一次密碼：") != password:
            raise UsageError("兩次輸入的密碼不一致")
        return password
    raise UsageError(
        "需要密碼：請使用 --password-file、環境變數 HOMEVAULT_PASSWORD，或在終端機中互動輸入"
    )


def repo_path(args: argparse.Namespace) -> str:
    path = args.repo or os.environ.get("HOMEVAULT_REPO")
    if not path:
        raise UsageError("請以 --repo 或環境變數 HOMEVAULT_REPO 指定備份庫路徑")
    return path


def open_repo(args: argparse.Namespace):
    from homevault.repository import Repository

    return Repository.open(repo_path(args), read_password(args))


class ProgressReporter:
    """在背景定期輸出備份進度。終端機上以單行更新，否則每 30 秒輸出一行。"""

    def __init__(self, progress, enabled: bool) -> None:
        self.progress = progress
        self.tty = sys.stderr.isatty()
        self.interval = 0.5 if self.tty else 30.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True) if enabled else None

    def __enter__(self) -> ProgressReporter:
        if self._thread:
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
            if self.tty:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()

    def line(self) -> str:
        p = self.progress
        speed = p.bytes_read / p.elapsed if p.elapsed > 0 else 0
        return (
            f"已處理 {p.files_done:,} 個檔案 / {fmt_bytes(p.bytes_done)} | "
            f"讀取 {fmt_bytes(speed)}/s | 新增 {fmt_bytes(p.raw_new)} | "
            f"去重率 {p.dedup_ratio:.1%}"
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self.tty:
                sys.stderr.write("\r\033[K" + self.line())
            else:
                sys.stderr.write(self.line() + "\n")
            sys.stderr.flush()


# ── 指令 ─────────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    from homevault.repository import Repository

    path = repo_path(args)
    repo = Repository.init(path, read_password(args, confirm=True))
    out(f"已建立備份庫 {path}（id {repo.config.id[:8]}）")
    out("請妥善保存密碼：遺失密碼將無法還原任何資料。")
    return 0


def _read_exclude_file(path: str) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise UsageError(f"無法讀取排除規則檔 {path}：{exc}") from None


def cmd_backup(args: argparse.Namespace) -> int:
    from homevault.backup import BackupOptions, Progress, run_backup
    from homevault.platform_utils import lower_process_priority

    excludes = list(args.exclude or [])
    for f in args.exclude_file or []:
        excludes.extend(_read_exclude_file(f))
    repo = open_repo(args)
    if not args.no_low_priority:
        lower_process_priority()
    options = BackupOptions(
        excludes=excludes,
        full_scan=args.full_scan,
        hostname=args.host,
        tags=args.tag or [],
        threads=args.threads,
    )
    progress = Progress()
    with ProgressReporter(progress, enabled=not args.quiet):
        result = run_backup(repo, args.sources, options, progress)
    s = result.snapshot.summary
    total_files = result.files_new + result.files_changed + result.files_unmodified
    elapsed = max(progress.elapsed, 1e-9)
    total_size = fmt_bytes(s["total_bytes"])
    speed = fmt_bytes(progress.bytes_read / elapsed)
    added, stored = fmt_bytes(s["data_added"]), fmt_bytes(s["data_added_stored"])
    out(f"快照 {short_id(result.snapshot_id)} 已建立")
    out(
        f"  檔案：新增 {result.files_new:,}、修改 {result.files_changed:,}、"
        f"未變動 {result.files_unmodified:,}（共 {total_files:,} 個，{total_size}）"
    )
    out(f"  讀取：{fmt_bytes(progress.bytes_read)}，速度 {speed}/s")
    out(f"  新增資料：{added}（壓縮加密後 {stored}），去重率 {progress.dedup_ratio:.1%}")
    out(f"  耗時：{elapsed:.1f} 秒")
    if result.stale:
        out(f"⚠ {len(result.stale)} 個項目無法讀取（可能正被其他程式開啟），沿用先前的版本：")
        for path, since in result.stale:
            out(f"    {path}（{_stale_age(since)}）")
    if result.changing:
        out(f"⚠ {len(result.changing)} 個檔案在備份過程中持續被修改，保存的內容可能不一致：")
        for path in result.changing:
            out(f"    {path}（下次備份會重新讀取）")
    if result.errors:
        out(f"⚠ {len(result.errors)} 個項目無法備份，且沒有先前的版本可沿用：")
        for message in result.errors:
            out(f"    {message}")
    return 3 if result.has_problems else 0


def _stale_age(since: str) -> str:
    """「自 2026-11-20 起無法更新，已 3 天」。"""
    try:
        then = datetime.fromisoformat(since)
    except ValueError:
        return f"沿用 {since} 的版本"
    days = (datetime.now(then.tzinfo) - then).days
    return f"自 {then:%Y-%m-%d} 起無法更新，已 {days} 天"


def cmd_snapshots(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    snaps = repo.list_snapshots()
    if args.host:
        snaps = [s for s in snaps if s[1].hostname == args.host]
    out(f"{'ID':<10}{'時間':<21}{'主機':<18}{'大小':>12}  路徑")
    for sid, snap in snaps:
        size = fmt_bytes(int(snap.summary.get("total_bytes", 0)))
        out(
            f"{short_id(sid):<10}{fmt_time(snap.time):<21}{snap.hostname:<18}{size:>12}  "
            + ", ".join(snap.paths)
            + _snapshot_warnings(snap.summary)
        )
    out(f"共 {len(snaps)} 個快照")
    return 0


def _snapshot_warnings(summary: dict) -> str:
    notes = []
    if summary.get("files_stale"):
        notes.append(f"沿用舊版 {summary['files_stale']}")
    if summary.get("files_changing"):
        notes.append(f"內容變動 {summary['files_changing']}")
    if summary.get("errors"):
        notes.append(f"未備份 {summary['errors']}")
    return f"  ⚠ {'、'.join(notes)}" if notes else ""


def _resolve(repo, ref: str):
    from homevault.restore import split_snapshot_ref

    snap_ref, path = split_snapshot_ref(ref)
    sid, snap = repo.find_snapshot(snap_ref)
    return sid, snap, path


def cmd_ls(args: argparse.Namespace) -> int:
    repo = open_repo(args)
    with repo.lock(exclusive=False):
        return _list_snapshot(repo, args)


def _list_snapshot(repo, args: argparse.Namespace) -> int:
    from homevault.restore import find_node
    from homevault.tree import DIR, SYMLINK

    repo.load_index(recover_orphans=False)
    _sid, snap, path = _resolve(repo, args.target)
    node = find_node(repo, snap, path)
    base = "/" + "/".join(p for p in path.replace("\\", "/").split("/") if p)

    def show(n, full: str) -> None:
        suffix = "/" if n.type == DIR else (f" -> {n.target}" if n.type == SYMLINK else "")
        if n.stale_since:
            suffix += f"  ⚠ 沿用 {fmt_time(n.stale_since)} 的版本（當時無法讀取）"
        if n.changing:
            suffix += "  ⚠ 備份時持續被修改，內容可能不一致"
        if args.long:
            size = "" if n.type == DIR else f"{n.size:,}"
            out(
                f"{stat.filemode(n.mode) if n.mode else '----------'} {size:>14} "
                f"{fmt_mtime(n.mtime_ns)}  {full}{suffix}"
            )
        else:
            out(f"{full}{suffix}")

    def walk(tree_id: bytes, prefix: str) -> None:
        for child in repo.load_tree(tree_id):
            full = f"{prefix.rstrip('/')}/{child.name}"
            show(child, full)
            if args.recursive and child.type == DIR and child.subtree:
                walk(child.subtree, full)

    if node.type == DIR and node.subtree:
        walk(node.subtree, base)
    else:
        show(node, base)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from homevault.restore import run_restore

    repo = open_repo(args)
    sid, snap, path = _resolve(repo, args.target_ref)
    result = run_restore(repo, snap, path, args.target, overwrite=args.overwrite)
    out(
        f"已從快照 {short_id(sid)} 還原 {result.files:,} 個檔案、{result.dirs:,} 個資料夾"
        f"（{fmt_bytes(result.bytes)}）到 {args.target}"
    )
    if result.errors:
        out(f"⚠ 有 {len(result.errors)} 個項目未還原，詳見上方警告")
        return 3
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from homevault.browse import ADDED, MODIFIED, REMOVED, diff_snapshots

    repo = open_repo(args)
    id_a, snap_a = repo.find_snapshot(args.snapshot_a)
    id_b, snap_b = repo.find_snapshot(args.snapshot_b)
    entries = diff_snapshots(repo, snap_a, snap_b)
    out(f"比較快照 {short_id(id_a)} → {short_id(id_b)}")
    for e in entries:
        out(f"{e.kind}  {e.path}{'/' if e.is_dir else ''}")
    count = {k: sum(1 for e in entries if e.kind == k and not e.is_dir) for k in "+-M"}
    out(f"檔案：新增 {count[ADDED]}、刪除 {count[REMOVED]}、修改 {count[MODIFIED]}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from homevault.check import run_check

    repo = open_repo(args)

    def progress(message: str) -> None:
        if not args.quiet and sys.stderr.isatty():
            sys.stderr.write("\r\033[K" + message)
            sys.stderr.flush()

    result = run_check(repo, read_data=args.read_data, progress=progress)
    if not args.quiet and sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
    for info in result.infos:
        out(f"ℹ {info}")
    if result.ok:
        detail = f"，已解密驗證 {result.blobs_verified:,} 個區塊" if args.read_data else ""
        out(f"✓ 備份庫完好：{len(result.snapshots)} 個快照{detail}")
        return 0
    out(f"✗ 發現 {len(result.errors)} 個問題：")
    for err in result.errors:
        out(f"  - {err}")
    if result.affected:
        out("受影響的快照與檔案：")
        for snap_id, paths in sorted(result.affected.items(), key=lambda kv: kv[0]):
            snap = result.snapshots[snap_id]
            out(f"  快照 {short_id(snap_id)}（{fmt_time(snap.time)}，{snap.hostname}）")
            for path in paths:
                out(f"    {path}")
    else:
        out("所有快照引用的資料都完好，仍可完整還原。")
    return 13


def _policy_from_args(args: argparse.Namespace):
    from homevault.retention import KeepPolicy

    return KeepPolicy(
        last=args.keep_last,
        hourly=args.keep_hourly,
        daily=args.keep_daily,
        weekly=args.keep_weekly,
        monthly=args.keep_monthly,
        yearly=args.keep_yearly,
    )


def cmd_forget(args: argparse.Namespace) -> int:
    from homevault.forget import forget_ids, run_forget

    repo = open_repo(args)
    if args.snapshot_ids:
        for sid in forget_ids(repo, args.snapshot_ids):
            out(f"已刪除快照 {short_id(sid)}")
    else:
        plans = run_forget(repo, _policy_from_args(args), dry_run=args.dry_run)
        verb = "將刪除" if args.dry_run else "已刪除"
        for group in plans:
            out(f"主機 {group.hostname}，路徑 {', '.join(group.paths)}")
            out(f"  保留 {len(group.keep)} 個快照：")
            for sid, snap, reasons in group.keep:
                out(f"    {short_id(sid)}  {fmt_time(snap.time)}  {', '.join(reasons)}")
            out(f"  {verb} {len(group.remove)} 個快照：")
            for sid, snap in group.remove:
                out(f"    {short_id(sid)}  {fmt_time(snap.time)}")
    if args.prune and not args.dry_run:
        return _prune(repo, args)
    if not args.dry_run:
        out("快照已刪除；執行 `homevault prune` 才會實際釋放空間。")
    return 0


def _prune(repo, args: argparse.Namespace) -> int:
    from homevault.prune import run_prune

    result = run_prune(repo, threshold=args.repack_threshold / 100, dry_run=args.dry_run)
    plan = result.plan
    prefix = "（試算）" if args.dry_run else ""
    out(
        f"{prefix}刪除 {len(plan.delete)} 個 pack、重新打包 {len(plan.repack)} 個、"
        f"保留 {len(plan.keep)} 個；移除 {plan.unused_blobs:,} 個未使用的區塊"
    )
    freed = result.size_before - result.size_after
    out(
        f"{prefix}備份庫資料由 {fmt_bytes(result.size_before)} 變為 "
        f"{fmt_bytes(result.size_after)}（釋放 {fmt_bytes(freed)}）"
    )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    return _prune(open_repo(args), args)


def cmd_stats(args: argparse.Namespace) -> int:
    from homevault.stats import run_stats

    st = run_stats(open_repo(args))
    out(f"備份庫大小：{fmt_bytes(st.repo_size)}（pack {fmt_bytes(st.pack_bytes)}）")
    out(f"快照數量：{len(st.snapshots)}")
    out(f"原始資料總量（所有快照合計）：{fmt_bytes(st.total_logical)}")
    out(f"去重後的原始資料：{fmt_bytes(st.unique_raw)}（{st.blob_count:,} 個區塊）")
    out(f"去重、壓縮、加密後：{fmt_bytes(st.unique_stored)}（節省 {st.space_saving:.1%}）")
    if st.snapshots:
        out("")
        out(f"{'ID':<10}{'時間':<21}{'主機':<18}{'原始大小':>12}{'獨有資料':>14}")
        for s in st.snapshots:
            out(
                f"{short_id(s.snapshot_id):<10}{fmt_time(s.snapshot.time):<21}"
                f"{s.snapshot.hostname:<18}{fmt_bytes(s.logical_size):>12}"
                f"{fmt_bytes(s.unique_stored):>14}"
            )
        out("獨有資料：只被該快照引用的資料，刪除該快照並 prune 後約可釋放的空間。")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    from homevault.locking import is_stale, read_locks

    repo = open_repo(args)
    removed = 0
    for name, info in read_locks(repo.backend, repo.key):
        if args.all or info is None or is_stale(info):
            repo.backend.delete(name)
            removed += 1
            out(f"已移除 lock：{info.describe() if info else name}")
        else:
            out(f"保留仍在使用中的 lock：{info.describe()}（使用 --all 強制移除）")
    out(f"共移除 {removed} 個 lock")
    return 0


# ── 解析器 ───────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-r", "--repo", help="備份庫路徑（或環境變數 HOMEVAULT_REPO）")
    common.add_argument("--password-file", help="從檔案讀取密碼（排程執行時使用）")
    common.add_argument("-q", "--quiet", action="store_true", help="不顯示進度")
    common.add_argument("-v", "--verbose", action="store_true", help="顯示詳細訊息")

    parser = argparse.ArgumentParser(prog="homevault", description="家用增量備份工具")
    parser.add_argument("--version", action="version", version=f"homevault {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<指令>")

    p = sub.add_parser("init", parents=[common], help="建立新的備份庫")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("backup", parents=[common], help="備份資料夾，建立新快照")
    p.add_argument("sources", nargs="+", metavar="來源資料夾")
    p.add_argument("-e", "--exclude", action="append", metavar="規則", help="排除規則，可重複")
    p.add_argument("--exclude-file", action="append", metavar="檔案", help="每行一條排除規則")
    p.add_argument("--full-scan", action="store_true", help="忽略增量判斷，重新讀取所有檔案")
    p.add_argument("--host", help="覆寫快照記錄的主機名稱")
    p.add_argument("--tag", action="append", help="為快照加上標籤")
    p.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 1), help="執行緒數")
    p.add_argument("--no-low-priority", action="store_true", help="不要降低行程優先權")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("snapshots", parents=[common], help="列出快照")
    p.add_argument("--host", help="只列出指定主機的快照")
    p.set_defaults(func=cmd_snapshots)

    p = sub.add_parser("ls", parents=[common], help="瀏覽快照內的目錄")
    p.add_argument("target", metavar="快照[:路徑]", help="例如 latest 或 a1b2c3d4:/Users/mom")
    p.add_argument("-l", "--long", action="store_true", help="顯示權限、大小與修改時間")
    p.add_argument("-R", "--recursive", action="store_true", help="遞迴列出子資料夾")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("restore", parents=[common], help="還原快照、資料夾或單一檔案")
    p.add_argument("target_ref", metavar="快照[:路徑]")
    p.add_argument("-t", "--target", required=True, help="還原到哪個資料夾")
    p.add_argument("--overwrite", action="store_true", help="覆寫已存在的檔案")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("diff", parents=[common], help="比較兩個快照的差異")
    p.add_argument("snapshot_a", metavar="快照A")
    p.add_argument("snapshot_b", metavar="快照B")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("check", parents=[common], help="檢查備份庫完整性")
    p.add_argument("--read-data", action="store_true", help="解密並驗證每一個區塊")
    p.set_defaults(func=cmd_check)

    prune_opts = argparse.ArgumentParser(add_help=False)
    prune_opts.add_argument("--dry-run", action="store_true", help="只顯示會做什麼，不實際刪除")
    prune_opts.add_argument(
        "--repack-threshold",
        type=float,
        default=20.0,
        metavar="百分比",
        help="pack 中未使用的資料超過此比例才重新打包（預設 20）",
    )

    p = sub.add_parser("forget", parents=[common, prune_opts], help="依保留政策刪除舊快照")
    p.add_argument("snapshot_ids", nargs="*", metavar="快照ID", help="直接刪除指定快照")
    for rule, desc in [
        ("last", "最近 N 個"),
        ("hourly", "最近 N 個小時各一份"),
        ("daily", "最近 N 天各一份"),
        ("weekly", "最近 N 週各一份"),
        ("monthly", "最近 N 個月各一份"),
        ("yearly", "最近 N 年各一份"),
    ]:
        p.add_argument(f"--keep-{rule}", type=int, default=0, metavar="N", help=f"保留{desc}")
    p.add_argument("--prune", action="store_true", help="刪除後立即執行 prune")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("prune", parents=[common, prune_opts], help="移除不再使用的資料")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("stats", parents=[common], help="顯示備份庫統計")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("unlock", parents=[common], help="清除殘留的 lock")
    p.add_argument("--all", action="store_true", help="連同看起來仍在使用中的 lock 一併清除")
    p.set_defaults(func=cmd_unlock)
    return parser


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname_zh)s%(message)s"))
    handler.addFilter(_ZhLevelFilter())
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO if verbose else logging.WARNING)
    log.propagate = False


class _ZhLevelFilter(logging.Filter):
    _names: ClassVar[dict[str, str]] = {
        "WARNING": "警告：",
        "ERROR": "錯誤：",
        "INFO": "",
        "DEBUG": "",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        record.levelname_zh = self._names.get(record.levelname, "")
        return True


def ensure_utf8_output() -> None:
    """Windows 上輸出被導向檔案或 pipe 時（例如排程寫入 log），Python 會改用 cp1252/cp950，
    遇到中文或 emoji 檔名就拋出 UnicodeEncodeError。改以 UTF-8 輸出。"""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except HomeVaultError as exc:
        sys.stderr.write(f"錯誤：{exc}\n")
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("\n已中斷。備份庫不受影響；已寫入的資料下次執行時會自動沿用。\n")
        return 130


def run() -> None:
    sys.exit(main())
