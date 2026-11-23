"""備份庫：開啟、初始化，以及 blob / tree / 快照的存取。

寫入順序（中斷安全的核心）::

    pack 檔（fsync）→ index 檔（fsync）→ snapshot 檔（fsync）

被引用者永遠先寫入，所以任何時間點中斷，備份庫裡的引用都指向已存在的檔案。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from homevault import crypto
from homevault.chunker import DEFAULT_PARAMS, ChunkerParams
from homevault.crypto import MasterKey, create_key_file, open_key_file, seal, unseal
from homevault.errors import (
    IntegrityError,
    NotFoundError,
    RepositoryNotFoundError,
    UsageError,
    WrongPasswordError,
)
from homevault.index import Index, PackMap, decode_index_file, encode_index_file
from homevault.locking import Lock
from homevault.pack import (
    BlobType,
    PackBuilder,
    decode_blob,
    encode_blob,
    pack_name,
    read_pack_header,
)
from homevault.snapshot import Snapshot
from homevault.storage import TMP_DIR, LocalBackend
from homevault.tree import Node, decode_tree, encode_tree

log = logging.getLogger("homevault")

CONFIG = "config"
DEFAULT_PACK_SIZE = 16 * 1024 * 1024
CHECKPOINT_EVERY_PACKS = 64  # 約每 1 GiB 寫一次 index checkpoint
TMP_MAX_AGE = 24 * 3600
_SNAPSHOT_AAD = b"homevault-snapshot-v1"
_DIRS = ["data", "index", "snapshots", "keys", "locks", TMP_DIR]


@dataclass(frozen=True)
class RepoConfig:
    id: str
    chunker: ChunkerParams
    pack_size: int

    def encode(self) -> bytes:
        doc = {
            "format": "homevault",
            "version": 1,
            "id": self.id,
            "chunker": self.chunker.to_dict(),
            "pack_size": self.pack_size,
        }
        return json.dumps(doc, indent=2).encode()

    @classmethod
    def decode(cls, raw: bytes) -> RepoConfig:
        try:
            doc = json.loads(raw)
            if doc.get("format") != "homevault" or doc.get("version") != 1:
                raise IntegrityError("不支援的備份庫格式版本")
            return cls(doc["id"], ChunkerParams(**doc["chunker"]), int(doc["pack_size"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrityError(f"config 檔格式損毀：{exc}") from None


@dataclass
class WriteStats:
    blobs_added: int = 0
    raw_bytes_added: int = 0
    stored_bytes_added: int = 0
    packs_written: int = 0


def _hash_name(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Repository:
    def __init__(self, backend: LocalBackend, config: RepoConfig, key: MasterKey) -> None:
        self.backend = backend
        self.config = config
        self.key = key
        self.index = Index()
        self.index_loaded = False
        self.stats = WriteStats()
        self._lock = threading.Lock()
        self._packer = PackBuilder(key)
        self._pending: set[bytes] = set()
        self._unsaved: PackMap = {}
        self._packs_since_checkpoint = 0

    # ── 建立與開啟 ────────────────────────────────────────

    @classmethod
    def init(
        cls,
        path: str | Path,
        password: str,
        *,
        chunker_params: ChunkerParams = DEFAULT_PARAMS,
        pack_size: int = DEFAULT_PACK_SIZE,
        kdf_n: int | None = None,
        backend: LocalBackend | None = None,
    ) -> Repository:
        root = Path(path)
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise UsageError(f"{root} 已存在且不是空資料夾，無法建立備份庫")
        if not password:
            raise UsageError("密碼不可為空白")
        root.mkdir(parents=True, exist_ok=True)
        backend = backend or LocalBackend(root)
        for d in _DIRS:
            backend.makedirs(d)
        for i in range(256):
            backend.makedirs(f"data/{i:02x}")
        key = MasterKey.generate()
        key_file = create_key_file(password, key, kdf_n or crypto.DEFAULT_KDF_N)
        backend.write_atomic(f"keys/{_hash_name(key_file)}", key_file)
        config = RepoConfig(secrets.token_hex(16), chunker_params, pack_size)
        backend.write_atomic(CONFIG, config.encode())  # config 最後寫入，代表建立完成
        return cls(backend, config, key)

    @classmethod
    def open(
        cls, path: str | Path, password: str, *, backend: LocalBackend | None = None
    ) -> Repository:
        """開啟備份庫並驗證密碼。此步驟不會寫入任何檔案。"""
        backend = backend or LocalBackend(path)
        if not Path(path).is_dir() or not backend.exists(CONFIG):
            raise RepositoryNotFoundError(f"{path} 不是 HomeVault 備份庫（找不到 config）")
        config = RepoConfig.decode(backend.read(CONFIG))
        key_names = backend.list("keys")
        if not key_names:
            raise IntegrityError("備份庫缺少 key file")
        for name in key_names:
            try:
                return cls(backend, config, open_key_file(password, backend.read(name)))
            except WrongPasswordError:
                continue
        raise WrongPasswordError("密碼錯誤，無法開啟備份庫（備份庫未做任何變更）")

    def lock(self, *, exclusive: bool) -> Lock:
        return Lock(self.backend, self.key, exclusive=exclusive)

    # ── Index ─────────────────────────────────────────────

    def list_pack_ids(self) -> set[bytes]:
        ids = set()
        for name in self.backend.list("data"):
            stem = name.rsplit("/", 1)[-1]
            if len(stem) == 64:
                try:
                    ids.add(bytes.fromhex(stem))
                except ValueError:
                    continue
        return ids

    def read_index_files(self) -> tuple[dict[str, PackMap], list[str]]:
        """讀取所有 index 檔。回傳（檔名 → 內容，無法讀取的檔名）。"""
        files: dict[str, PackMap] = {}
        broken: list[str] = []
        for name in self.backend.list("index"):
            try:
                files[name] = decode_index_file(self.key, self.backend.read(name))
            except IntegrityError as exc:
                log.warning("index 檔 %s 無法讀取：%s", name, exc)
                broken.append(name)
        return files, broken

    def load_index(self, *, recover_orphans: bool = True) -> list[bytes]:
        """載入 index。

        * 指向不存在 pack 的項目會被忽略，避免去重時引用到已刪除的資料。
        * recover_orphans：讀取「沒有被任何 index 記錄」的 pack header 並加入索引，
          讓中斷的備份重新執行時可以沿用已寫入的資料。回傳被復原的 pack id。
        """
        existing = self.list_pack_ids()
        files, _ = self.read_index_files()
        indexed: set[bytes] = set()
        index = Index()
        for packs in files.values():
            for pack_id, entries in packs.items():
                if pack_id in existing:
                    index.add_pack(pack_id, entries)
                    indexed.add(pack_id)
        recovered = []
        if recover_orphans:
            for pack_id in sorted(existing - indexed):
                try:
                    entries = read_pack_header(self.backend, pack_id, self.key)
                except IntegrityError as exc:
                    log.warning("pack %s 損毀，略過：%s", pack_id.hex()[:16], exc)
                    continue
                index.add_pack(pack_id, entries)
                self._unsaved[pack_id] = entries
                recovered.append(pack_id)
        self.index = index
        self.index_loaded = True
        return recovered

    def write_index(self, packs: PackMap) -> str:
        raw = encode_index_file(self.key, packs)
        name = f"index/{_hash_name(raw)}"
        self.backend.write_atomic(name, raw)
        return name

    # ── Blob ──────────────────────────────────────────────

    def has_blob(self, blob_id: bytes) -> bool:
        return blob_id in self.index

    def save_blob(self, blob_type: BlobType, data: bytes) -> tuple[bytes, int]:
        """儲存 blob（若尚不存在）。回傳 (blob_id, 新寫入的加密後大小，已存在則為 0)。

        可由多個執行緒同時呼叫：壓縮與加密在鎖外進行，只有加入 pack 時才持有鎖。
        """
        if not self.index_loaded:
            raise RuntimeError("save_blob 之前必須先 load_index")
        blob_id = self.key.blob_id(data)
        with self._lock:
            if blob_id in self.index or blob_id in self._pending:
                return blob_id, 0
            self._pending.add(blob_id)
        try:
            sealed = encode_blob(self.key, blob_type, blob_id, data)
        except BaseException:
            with self._lock:
                self._pending.discard(blob_id)
            raise
        with self._lock:
            self._packer.add(blob_type, blob_id, sealed, len(data))
            self.stats.blobs_added += 1
            self.stats.raw_bytes_added += len(data)
            self.stats.stored_bytes_added += len(sealed)
            if self._packer.size >= self.config.pack_size:
                self._write_pack_locked()
        return blob_id, len(sealed)

    def copy_sealed_blob(
        self, blob_type: BlobType, blob_id: bytes, sealed: bytes, raw_length: int
    ) -> None:
        """把已驗證的加密 blob 原樣放進新的 pack（prune 重新打包用）。

        不檢查 blob 是否已在 index 中：舊位置即將被刪除，必須另存一份。
        """
        with self._lock:
            if blob_id in self._pending:
                return
            self._pending.add(blob_id)
            self._packer.add(blob_type, blob_id, sealed, raw_length)
            if self._packer.size >= self.config.pack_size:
                self._write_pack_locked()

    def load_blob(self, blob_type: BlobType, blob_id: bytes) -> bytes:
        entry = self.index.get(blob_id)
        if entry is None:
            raise IntegrityError(f"索引中找不到 blob {blob_id.hex()[:16]}")
        sealed = self.backend.read_range(pack_name(entry.pack_id), entry.offset, entry.length)
        return decode_blob(self.key, blob_type, blob_id, sealed)

    def flush(self) -> None:
        """寫出尚未封檔的 pack，以及所有尚未記錄到 index 檔的項目。"""
        with self._lock:
            if len(self._packer):
                self._write_pack_locked()
            if self._unsaved:
                self.write_index(self._unsaved)
                self._unsaved = {}
                self._packs_since_checkpoint = 0

    def _write_pack_locked(self) -> None:
        pack_id, raw, entries = self._packer.finish()
        self.backend.write_atomic(pack_name(pack_id), raw)
        # pack 已持久化，才讓它出現在索引中
        self.index.add_pack(pack_id, entries)
        self._unsaved[pack_id] = entries
        self._pending.difference_update(e.id for e in entries)
        self._packer = PackBuilder(self.key)
        self.stats.packs_written += 1
        self._packs_since_checkpoint += 1
        if self._packs_since_checkpoint >= CHECKPOINT_EVERY_PACKS:
            self.write_index(self._unsaved)
            self._unsaved = {}
            self._packs_since_checkpoint = 0

    # ── Tree ──────────────────────────────────────────────

    def save_tree(self, nodes: list[Node]) -> bytes:
        blob_id, _ = self.save_blob(BlobType.TREE, encode_tree(nodes))
        return blob_id

    def load_tree(self, tree_id: bytes) -> list[Node]:
        return decode_tree(self.load_blob(BlobType.TREE, tree_id))

    # ── 快照 ──────────────────────────────────────────────

    def save_snapshot(self, snapshot: Snapshot) -> bytes:
        sealed = seal(self.key.enc_key, snapshot.encode(), _SNAPSHOT_AAD)
        snap_id = hashlib.sha256(sealed).digest()
        self.backend.write_atomic(f"snapshots/{snap_id.hex()}", sealed)
        return snap_id

    def load_snapshot(self, snap_id: bytes) -> Snapshot:
        raw = self.backend.read(f"snapshots/{snap_id.hex()}")
        return Snapshot.decode(unseal(self.key.enc_key, raw, _SNAPSHOT_AAD))

    def list_snapshot_ids(self) -> list[bytes]:
        ids = []
        for name in self.backend.list("snapshots"):
            try:
                ids.append(bytes.fromhex(name.rsplit("/", 1)[-1]))
            except ValueError:
                continue
        return ids

    def list_snapshots(self) -> list[tuple[bytes, Snapshot]]:
        """依時間排序回傳所有快照。"""
        snaps = [(sid, self.load_snapshot(sid)) for sid in self.list_snapshot_ids()]
        return sorted(snaps, key=lambda item: (item[1].datetime, item[0]))

    def find_snapshot(self, ref: str) -> tuple[bytes, Snapshot]:
        """以 id 前綴或 "latest" 找出快照。"""
        snaps = self.list_snapshots()
        if not snaps:
            raise NotFoundError("備份庫中沒有任何快照")
        if ref == "latest":
            return snaps[-1]
        matches = [s for s in snaps if s[0].hex().startswith(ref.lower())]
        if not matches:
            raise NotFoundError(f"找不到快照 {ref}")
        if len(matches) > 1:
            raise UsageError(f"快照 id 前綴 {ref} 不唯一，請輸入更長的 id")
        return matches[0]

    def delete_snapshot(self, snap_id: bytes) -> None:
        self.backend.delete(f"snapshots/{snap_id.hex()}")

    def walk_blobs(self, snapshots: Iterable[Snapshot]) -> set[bytes]:
        """回傳快照引用到的所有 blob id（tree 與 data）。共用的子樹只走訪一次。"""
        seen: set[bytes] = set()
        stack = [s.tree for s in snapshots]
        while stack:
            tree_id = stack.pop()
            if tree_id in seen:
                continue
            seen.add(tree_id)
            for node in self.load_tree(tree_id):
                if node.subtree:
                    stack.append(node.subtree)
                seen.update(node.chunks)
        return seen
