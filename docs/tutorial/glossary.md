# 名詞對照表

## 業務與領域術語

| 名詞 | 說明 | 出現位置 |
|---|---|---|
| 備份庫（repository） | 存放所有備份資料的目錄，通常在外接硬碟上 | `src/homevault/repository.py` |
| 快照（snapshot） | 某次備份當下的完整檔案狀態 | `src/homevault/snapshot.py` |
| 增量備份 | 只讀取有變動的檔案，其他沿用上一個快照 | `src/homevault/backup.py` |
| 去重（deduplication） | 相同內容只存一份 | `Repository.save_blob()` |
| 保留政策（retention policy） | 決定哪些舊快照要保留 | `src/homevault/retention.py` |
| 中斷安全（crash safety / crash consistency） | 任何時間點中斷，資料都不會損毀 | `src/homevault/storage.py`、README |

## 專案內的自訂名稱

| 名詞 | 說明 | 出現位置 |
|---|---|---|
| blob | 儲存的最小單位，分為 `DATA` 與 `TREE` | `BlobType`（`src/homevault/pack.py`） |
| pack | 把多個加密 blob 打包成的檔案，約 16 MiB | `src/homevault/pack.py` |
| index | blob id 對應到 pack 與位置的索引 | `src/homevault/index.py` |
| tree | 一個目錄的節點清單，也是一種 blob | `src/homevault/tree.py` |
| Node | tree 中的一個項目：檔案、目錄或符號連結 | `src/homevault/tree.py` |
| parent | 同主機、同來源路徑的上一個快照，增量判斷的依據 | `find_parent()`（`src/homevault/backup.py`） |
| commit point | 寫入 snapshot 的那一刻；之前中斷只留下未被引用的 pack | `run_backup()` |
| 孤兒 pack（orphan pack） | 已寫入備份庫的 data/ 目錄，但沒有被任何 index 記錄的 pack | `Repository.load_index()` |
| checkpoint | 備份途中每 64 個 pack 寫一次 index | `CHECKPOINT_EVERY_PACKS` |
| stale | 讀取失敗，沿用上一版的節點（`stale_since` 記錄版本時間） | `_Archiver._reuse_previous()` |
| changing | 備份時持續被修改，內容可能不一致的檔案 | `_Archiver._store_file()` |
| 重新打包（repack） | prune 把部分使用中的 pack 的有效 blob 搬到新 pack | `src/homevault/prune.py` |
| 殘留 lock（stale lock） | 執行中斷而留下的 lock | `is_stale()`（`src/homevault/locking.py`） |
| `check_held()` | 關鍵步驟前，確認 lock 沒有被別人移除 | `src/homevault/locking.py` |
| 範本（template） | 故障注入測試中，每個案例複製的初始狀態 | `tests/test_fault_injection.py` |
| 操作序列簽章 | (操作種類, 作用目錄) 的序列，用來確認故障注入的確定性 | `op_signature()`（`tests/faults.py`） |

## 技術縮寫

| 縮寫 | 全名 | 說明 |
|---|---|---|
| CDC | Content-Defined Chunking | 內容定義切塊 |
| FastCDC | — | 以 gear hash 加上 normalized chunking 的 CDC 演算法 |
| AEAD | Authenticated Encryption with Associated Data | 加密並驗證，例如 AES-GCM |
| AAD | Additional Authenticated Data | 不加密但受驗證的資料，此專案用來綁定 blob 的類型與 id |
| GCM | Galois/Counter Mode | AES 的一種 AEAD 模式 |
| HMAC | Hash-based Message Authentication Code | 帶金鑰的雜湊，用於 blob id |
| KDF | Key Derivation Function | 金鑰推導函式，此專案用 scrypt |
| KEK | Key-Encryption Key | 由密碼推導、用來加密 master key 的金鑰 |
| NFC／NFD | Unicode Normalization Form C／D | Unicode 的正規化形式，Mac 與 Windows 的輸入法常不同 |
| ADR | Architecture Decision Record | 架構決策紀錄（11 章） |
