# 09 測試

## 1. 測試策略

共收集 290 個測試。本機執行時 259 passed、31 skipped；skipped 的是故障注入參數化中，超過實際操作數而預留的中斷點。

| 層級 | 測什麼 | 檔案 |
|---|---|---|
| 單元測試 | 純函式與格式：切塊、加密、pack 與 index 格式、保留政策 | `tests/test_chunker.py`、`tests/test_crypto.py`、`tests/test_pack_index.py`、`tests/test_retention.py`、`tests/test_storage.py` |
| 性質測試（Hypothesis） | 切塊的 numpy 版與參考實作等價、大小上下限、保留政策與參考答案一致、部分刪除後的冪等性 | `tests/test_chunker.py`、`tests/test_retention.py` |
| 整合測試 | 備份與還原、增量、排除、跨主機去重、diff、check、forget、prune、lock、被鎖住或變動中的檔案 | `tests/test_backup_restore.py`、`tests/test_backup_problems.py`、`tests/test_check.py`、`tests/test_forget_prune.py`、`tests/test_locking.py`、`tests/test_repository.py`、`tests/test_diff.py` |
| 安全性測試 | 錯誤密碼（10 個指令）、備份庫不洩漏檔名與雜湊 | `tests/test_security.py` |
| 故障注入 | backup、帶 checkpoint 的 backup、forget、prune 每個操作都中斷一次，加上斷電模式 | `tests/test_fault_injection.py` |
| 端對端 | CLI 完整流程、真實子行程被 SIGKILL 或 Ctrl+C、輸出編碼 | `tests/test_cli.py`、`tests/test_interrupt_process.py` |
| 效能與跨 OS | 500 MiB 量測、macOS 與 Windows 共用備份庫 | `benchmarks/bench_backup.py`、`benchmarks/cross_os_dedup.py`（CI） |

## 2. 執行指令

```bash
uv run pytest                      # 全部（本機約 60 秒）
uv run pytest -m "not slow"        # 略過大檔案與子行程測試
uv run pytest tests/test_fault_injection.py
uv run ruff check . && uv run ruff format --check .
uv run python benchmarks/bench_backup.py --threads 1
```

測試後要 commit 時，以 pytest 的 exit code 把關。不要只看 `| tail -1` 的摘要，原因見 [11 章](11-tradeoffs-pitfalls.md)「失敗被管線吞掉」：

```bash
set -o pipefail; uv run pytest -q --tb=long -rf > test.log 2>&1 && git commit ...
```

## 3. 測試替身與工具

| 工具 | 位置 | 用途 |
|---|---|---|
| `FaultInjectingBackend` | `tests/faults.py` | 在第 k 個寫入、fsync、rename、刪除或建目錄操作上模擬當機；可選斷電模式 |
| `CountingOpener` | `tests/test_backup_restore.py` | 記錄哪些檔案被開啟，用來證明增量備份沒有重新讀取（驗收 7） |
| `locked_opener()` | `tests/test_backup_problems.py` | 讓指定檔案拋出 `PermissionError`，模擬被 Outlook 鎖住 |
| `ChangingFile` | `tests/test_backup_problems.py` | 讀完之後改寫檔案，模擬備份途中被修改 |
| `fast_kdf` fixture | `tests/conftest.py` | 把 scrypt 參數降為 2¹⁰，加快測試速度 |
| `file_manifest()` | `tests/conftest.py` | 產生 `{路徑: (sha256, mtime_ns)}`，比對還原結果 |

## 4. 代表性測試走讀

### 故障注入：窮舉每一個中斷點

`tests/test_fault_injection.py` 的流程：

1. **範本**（module scope 的 `template` fixture）：建立備份庫與來源資料夾，含 4 個既有快照，並記錄每個快照的 manifest。
2. **計數**（`count_ops()`）：從範本複製一份，執行一次情境，記下操作總數與操作序列簽章。
3. **每個中斷點 k**（`crash_and_verify()`）：
   - 再從範本複製一份；
   - 在第 k 個操作當機；
   - **先比對操作序列**（`sequence_mismatch()`）：前 k 個操作必須與計數時一致，而且真的在第 k 個操作當機；
   - 驗證 `check --read-data` 通過；
   - 驗證中斷前的快照都能完整還原（內容與 mtime 都相同）；
   - 重跑原指令必須成功，之後 check 仍然通過。

**為什麼從範本複製，而不是每次重新建立：** 範本的金鑰與資料固定，所以第 k 個操作永遠是同一個動作，才能「窮舉」而不只是抽樣。

**為什麼 `SimulatedCrash` 繼承 `BaseException`：** 一般的 `except Exception` 攔不住它，最接近行程突然死亡的情況。當機之後，`FaultInjectingBackend` 的所有操作（包括讀取與清理暫存檔）都會失敗，模擬「行程已經不存在」。

### 被鎖住三晚的檔案

`test_file_locked_three_nights_keeps_last_readable_version` 依照審查回饋中的情境：

1. 第 0 晚正常備份。
2. 之後三晚，檔案內容都改變，而且都被鎖住。斷言每一晚的快照中，檔案都在，而且 `stale_since` 都指向第 0 晚。
3. 執行 `forget --keep-last 1` 與 `prune`。斷言還原最新快照時，仍能取得第 0 晚的內容。

最後一步證明了「舊版本不會因為保留政策而永遠消失」。

## 5. 覆蓋範圍與缺口

**已涵蓋：** 題目的 13 項驗收標準全部有對應的測試或量測，對照表見 README。

**缺口**（照實列出）：

- **沒有程式碼覆蓋率量測**（沒有使用 coverage.py），無法用數字說明覆蓋了多少。
- **Windows 符號連結的還原**：Windows 上建立符號連結需要權限，相關測試在 Windows 上被略過。
- **網路磁碟**：沒有測試 SMB 或 NFS 上的 lock 原子性。
- **Windows 上刪除檔案的斷電持久性**：故障注入的斷電模式只模擬「沒有 fsync 的檔案內容遺失」，沒有模擬「刪除被回復」。
- **大規模**：沒有測試 2 TB、200 萬個檔案的記憶體與時間（只有估算）。
- **曾經有一次無法重現的失敗**：本機一次完整執行出現 7 個失敗，之後三次嘗試都無法重現。目前已加上操作序列診斷，等待再次出現（見 [11 章](11-tradeoffs-pitfalls.md)）。

## 6. 建議補強（建議）

- 加入 `pytest-cov`，在 CI 中產生覆蓋率報告。
- 加入「大量檔案」的測試（例如 10 萬個小檔案），量測 tree 與 index 的記憶體用量。
- 加入模擬「刪除在斷電後被回復」的故障模式，驗證 [01 章](01-requirements.md) 列出的 Windows 限制，並設計對策。
- 加入兩個行程同時執行 backup 與 prune 的競爭測試（目前只以 lock 的單元測試涵蓋）。
