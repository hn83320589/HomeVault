# 07 程式內容導讀

這一章帶你實際讀一遍程式碼。主線是「一次每晚備份」的完整生命週期，之後再逐一介紹各模組值得學的地方，以及可以改進之處。

## 1. 建議的閱讀順序

| 順序 | 檔案 | 為什麼先讀 |
|---|---|---|
| 1 | `src/homevault/errors.py` | 30 行讀完。了解每種錯誤對應的 exit code，之後讀到 `raise` 時就知道使用者會看到什麼 |
| 2 | `src/homevault/storage.py` | 所有寫入的出口，「只新增、不覆寫」的實作都在這裡 |
| 3 | `src/homevault/repository.py` | 把 pack、index、snapshot 串起來；`save_blob()` 與 `_write_pack_locked()` 是去重與寫入順序的核心 |
| 4 | `src/homevault/backup.py` | 最長的模組，主線流程 |
| 5 | `src/homevault/prune.py` | 最危險的流程，讀它的模組說明就能理解刪除順序 |
| 6 | `src/homevault/chunker.py` | 演算法最有趣的地方，搭配 [08 章](08-algorithms.md) 一起讀 |
| 7 | `tests/faults.py`、`tests/test_fault_injection.py` | 理解上面這些設計是怎麼被驗證的 |

## 2. 進入點

`pyproject.toml` 的 `[project.scripts]` 把 `homevault` 指令指向 `homevault.cli:run`；`python -m homevault` 則經由 `src/homevault/__main__.py` 進入。兩者最後都呼叫 `main()`：

```python
# src/homevault/cli.py
def main(argv: list[str] | None = None) -> int:
    ensure_utf8_output()                                   # ①
    parser = build_parser()
    args = parser.parse_args(argv)
    # ...
    try:
        return args.func(args)
    except HomeVaultError as exc:                          # ②
        sys.stderr.write(f"錯誤：{exc}\n")
        return exc.exit_code
    except KeyboardInterrupt:                              # ③
        sys.stderr.write("\n已中斷。備份庫不受影響；已寫入的資料下次執行時會自動沿用。\n")
        return 130
```

① 這一行是 CI 抓到 Windows bug 之後加上的（commit `5938516`）。Windows 上輸出被導向時，Python 會改用 cp1252，輸出中文就會拋出 `UnicodeEncodeError`。

② 只攔截「預期內」的錯誤（`HomeVaultError` 的子類別）。程式本身的 bug 會照常拋出 traceback，不會被吞掉。這符合全域規範中「禁止無聲吞掉 exception」的要求。

③ `main(argv)` 接受參數清單並回傳 exit code，而不是直接呼叫 `sys.exit()`。所以測試可以直接呼叫 `main([...])` 並檢查回傳值（見 `tests/test_cli.py`）。這是選用 argparse 時的考量之一。

各指令的實作都延遲匯入（例如 `cmd_backup()` 內才 `from homevault.backup import ...`），所以 `homevault --help` 不必載入 numpy。

## 3. 一次備份的生命週期

### 第一站：開啟備份庫，不寫入任何東西

`Repository.open()` 只讀備份庫的 config 檔與 keys/ 目錄，用密碼解開 master key。密碼錯誤時拋出 `WrongPasswordError`，這時連 lock 都還沒建立，所以備份庫完全不變。`tests/test_security.py` 對 10 個指令逐一驗證了這一點：執行前後，備份庫所有檔案的雜湊都完全相同。

### 第二站：取得 lock、載入 index 並復原

```python
# src/homevault/repository.py（Repository.load_index 節錄）
recovered = []
if recover_orphans:
    for pack_id in sorted(existing - indexed):              # ①
        try:
            entries = read_pack_header(self.backend, pack_id, self.key)
        except IntegrityError as exc:
            log.warning("pack %s 損毀，略過：%s", pack_id.hex()[:16], exc)
            continue
        index.add_pack(pack_id, entries)
        self._unsaved[pack_id] = entries                    # ②
        recovered.append(pack_id)
```

① `existing - indexed` 是「在備份庫的 data/ 目錄裡、但沒有任何 index 記錄」的 pack。這些 pack 是上次中斷前寫好的，只要讀它們檔尾的 header，就能把內容加回索引。這就是 FR-20「中斷後重跑要沿用已寫入的區塊」的實作。

② 復原的項目放進 `_unsaved`，下一次 `flush()` 時會寫成正式的 index 檔。唯讀的指令（例如 restore）傳入 `recover_orphans=False`，不會觸發寫入。

### 第三站：走訪與增量判斷

`_Archiver._dir_node()` 遞迴走訪資料夾。它會先把本層的檔案交給執行緒池，再遞迴子資料夾，最後才等待本層檔案的結果，讓 I/O 與 CPU 重疊進行。每個檔案先經過 `_file_future()` 的增量判斷：

```python
# src/homevault/backup.py（_Archiver._file_future 節錄）
if (
    not self.opt.full_scan
    and parent is not None
    and parent.type == FILE
    and parent.size == st.st_size
    and parent.mtime_ns == st.st_mtime_ns
    and not parent.changing                     # ①
    and all(self.repo.has_blob(c) for c in parent.chunks)   # ②
):
    # 沿用 parent.chunks，完全不開檔
```

① 上次標記為「內容可能不一致」的檔案，即使 size 與 mtime 相同也一定重讀。

② 多一道保險：上一個快照的區塊如果已經不在索引裡（例如 pack 損毀被移除），就重新讀檔，不讓損毀一代一代地繼承下去。

### 第四站：讀檔、處理被鎖住與變動中的檔案

```python
# src/homevault/backup.py（_Archiver._store_file 節錄）
for attempt in range(2):
    try:
        chunks, size = self._read_chunks(path)
    except OSError as exc:
        return self._reuse_previous(path, name, parent, str(exc))   # ①
    try:
        after = os.stat(path)
    except OSError:
        break
    if after.st_mtime_ns == st.st_mtime_ns and after.st_size == size:
        break                                                       # ②
    if attempt == 0:
        st = after                                                  # ③
    else:
        changing = True
```

① 讀不到時沿用上一版，而不是略過。這是使用者貼上的審查回饋中指出的最嚴重問題：原本 `return None` 會讓每晚都被鎖住的 Outlook 資料檔從之後的每一個快照中消失（commit `e876344`）。

② 讀取前後的 size 與 mtime 都相同，才算讀到一致的內容。

③ 第一次不一致時，以讀完後的 stat 作為新的基準重讀一次；第二次仍不一致，就標記為 `changing`。

`_reuse_previous()` 的核心只有一行：`dataclasses.replace(parent, name=name, stale_since=since)`。它複製上一版的節點，再加上「從哪個快照時間開始無法更新」的標記。`since` 優先沿用 `parent.stale_since`，所以連續被鎖三晚時，標記指向的仍然是最後一次成功讀取的那一晚。

### 第五站：存入 blob，去重與並行

```python
# src/homevault/repository.py（Repository.save_blob 節錄）
blob_id = self.key.blob_id(data)
with self._lock:
    if blob_id in self.index or blob_id in self._pending:    # ①
        return blob_id, 0
    self._pending.add(blob_id)
try:
    sealed = encode_blob(self.key, blob_type, blob_id, data)  # ②
except BaseException:
    with self._lock:
        self._pending.discard(blob_id)
    raise
with self._lock:
    self._packer.add(blob_type, blob_id, sealed, len(data))
    # ...
    if self._packer.size >= self.config.pack_size:
        self._write_pack_locked()
```

① `_pending` 記錄「已經在處理、但還沒寫進 pack」的 blob。兩個執行緒同時遇到相同內容時，只有一個會真的寫入。

② 壓縮與加密是最耗 CPU 的部分，放在鎖外進行。zstandard、AES-GCM、hashlib 都會釋放 GIL，所以多執行緒確實能加速。

`_write_pack_locked()` 的順序是關鍵：先 `backend.write_atomic()` 把 pack 寫到硬碟，然後才 `self.index.add_pack()`。**pack 持久化之後才讓它出現在索引中**，所以 tree 永遠不會引用到還沒寫入的區塊。

### 第六站：原子寫入

```python
# src/homevault/storage.py（LocalBackend.write_atomic）
tmp = f"{TMP_DIR}/{secrets.token_hex(16)}.tmp"
with self._io(name):
    try:
        self._op_write(tmp, data)
        self._op_fsync(tmp)
        self._op_rename(tmp, name)
    except BaseException:
        self._discard_tmp(tmp)  # 例如 Ctrl+C：盡量不留下暫存檔
        raise
```

三個 `_op_*` 都是可以被覆寫的方法，這就是故障注入的接縫。`_io()` 這個 context manager 負責把 `OSError` 翻譯成有意義的錯誤：

```python
# src/homevault/storage.py（LocalBackend._io）
except FileNotFoundError:
    self._check_root()                 # 根目錄不見了 → RepositoryUnavailableError
    raise ObjectMissingError(f"備份庫缺少檔案：{name}") from None
except OSError as exc:
    self._check_root()
    raise RepositoryUnavailableError(f"存取備份庫檔案 {name} 時發生錯誤：{exc}") from exc
```

同樣是 `FileNotFoundError`，會依「備份庫根目錄還在不在」分成兩種完全不同的情況：硬碟被拔除（exit 12，下次重跑即可），或備份庫缺了某個檔案（資料損毀，需要 check）。

### 第七站：提交快照

```python
# src/homevault/backup.py（run_backup 節錄）
lock.check_held()
repo.flush()
lock.check_held()  # 寫 pack 與 index 期間也可能失去 lock
snapshot = Snapshot(...)
lock.check_held()
snap_id = repo.save_snapshot(snapshot)  # ← commit point
```

`check_held()` 是獨立程式碼審查之後加上的（commit `3097131`）。情境是：Mac 備份到一半進入睡眠超過 30 分鐘，Windows 上的 prune 把 Mac 的 lock 判定為殘留並刪除，接著把 Mac 已寫入、但還沒被快照引用的 pack 當成垃圾刪掉。Mac 醒來後如果直接提交快照，這個快照就會引用不存在的資料。

## 4. 各模組重點

### `src/homevault/locking.py`

- **職責：** 共享鎖與獨佔鎖、殘留判定、lock 失效偵測。
- **值得學：** `acquire()` 先寫自己的 lock，再重新列出所有 lock 檢查衝突，處理兩台電腦同時建立 lock 的競爭。`_active_sessions` 用來分辨「同一個 pid，但不是我持有的 lock」，這是故障注入測試中同一個行程內模擬當機時需要的。
- **可以改進：** 殘留判定依賴各電腦的時鐘（`time.time()`）。兩台電腦時鐘差太多時，判定會失準。

### `src/homevault/prune.py`

- **職責：** 計算仍在使用的 blob，把 pack 分成保留、刪除、重新打包三類，再依安全順序寫入與刪除。
- **值得學：** `plan_prune()` 是純函式，不碰 I/O，可以單獨測試。刪除步驟前都會呼叫 `check_held()`：

```python
# src/homevault/prune.py（run_prune 節錄）
lock.check_held()
for name in old_index_names:          # ⑥ 先刪舊 index
    repo.backend.delete(name)
lock.check_held()
for pack_id in [*plan.delete, *plan.repack]:
    repo.backend.delete(pack_name(pack_id))   # ⑦ 再刪 pack
```

- **可以改進：** 重新打包時整個 pack 讀進記憶體（16 MiB），一般情況沒問題；但如果 pack 因為巨大的 tree blob 而遠超過 16 MiB，記憶體用量會跟著上升。

### `src/homevault/restore.py`

- **值得學：** 先寫 `.hv-partial` 再 `os.replace()`，中斷時不會留下「看起來完整、其實只寫了一半」的檔案。設定 mtime 的失敗與寫入內容的失敗分開處理：檔案已經寫好，就算 mtime 設不上也照樣計入檔案數。
- **可以改進：** 逐一讀取區塊，沒有平行化，也沒有快取同一個 pack 的讀取。

### `src/homevault/check.py`

- **值得學：** 回報受影響的檔案時，`map_affected()` 以 tree id 做 memoization。多個快照共用的子樹只走訪一次，效率接近只走訪一個快照（見 [08 章](08-algorithms.md)）。
- **可以改進：** 結構檢查會讀取每一個 pack 的 header，在 HDD 上有十萬個 pack 時會很慢。

### `src/homevault/crypto.py`

- **值得學：** 密碼先做 Unicode NFC 正規化再推導金鑰（`_derive_kek()`），避免 Mac 輸入法（NFD）與 Windows（NFC）讓同一個密碼對不上。
- **可以改進：** 格式已經支援更改密碼（重新加密 key file 即可），但沒有提供指令。
