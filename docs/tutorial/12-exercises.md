# 12 延伸練習與自我檢核

## 1. 理解檢核

1. 為什麼 prune 一定要「先刪舊 index，再刪 pack」？反過來會發生什麼事？
   <details><summary>答案</summary>
   如果先刪 pack 而在刪 index 之前中斷，舊 index 仍然指向已刪除的 pack。下次備份去重時會以為這些 blob 已經存在而跳過寫入，新快照就會引用不存在的資料，造成無聲的資料遺失。見 07 章 prune 小節、`src/homevault/prune.py` 的模組說明。另外，`Repository.load_index()` 會忽略指向不存在 pack 的項目，這是第二道防線。
   </details>

2. 備份寫入 pack 時，為什麼要先 `write_atomic()`，再 `index.add_pack()`？
   <details><summary>答案</summary>
   順序反過來的話，pack 還沒寫到硬碟，這些 blob 就已經出現在索引中。另一個執行緒去重時會以為它們存在，而 tree 可能在 pack 寫入失敗後，仍然引用這些 blob。見 07 章第五站、`Repository._write_pack_locked()`。
   </details>

3. numpy 版的切塊為什麼和「每個 chunk 起點把 hash 歸零」的逐 byte 版本嚴格等價？
   <details><summary>答案</summary>
   gear hash 每次左移 1 bit，64 bytes 之前的資料會被移出 64-bit 暫存器，所以 hash 只取決於最近 64 bytes。而判斷切點的位置至少在起點之後 min_size（256 KiB）處，這時的 hash 與從哪裡開始算無關。見 08 章。
   </details>

4. 區塊 id 為什麼用 HMAC，而不是 SHA-256？
   <details><summary>答案</summary>
   如果用 SHA-256，攻擊者可以拿已知檔案（例如某張網路上流傳的照片）算出雜湊，再到備份庫中比對，確認「這個家庭有這個檔案」。HMAC 需要只有持有密碼的人才拿得到的 `mac_key`。見 05 章、`MasterKey.blob_id()`。不過 id 只出現在加密的 header 與 index 中，這是第二層保護。
   </details>

5. forget 刪到一半中斷，重跑時為什麼會得到相同的結果？
   <details><summary>答案</summary>
   保留政策的性質：若 K = policy(S)，則對任何 K ⊆ S′ ⊆ S 都有 policy(S′) = K。被刪除的都是沒有被保留的快照，刪掉它們不會改變任何一條規則的掃描結果。見 08 章、`test_policy_is_idempotent_after_partial_forget`。
   </details>

6. 檔案連續三晚被鎖住時，快照中的 `stale_since` 會指向哪一晚？為什麼？
   <details><summary>答案</summary>
   指向最後一次成功讀取的那一晚（第 0 晚），因為 `_reuse_previous()` 優先沿用 `parent.stale_since`。這樣使用者看到的「自某日起無法更新」才是正確的日期。見 07 章第四站。
   </details>

7. `SimulatedCrash` 為什麼要繼承 `BaseException`，而不是 `Exception`？
   <details><summary>答案</summary>
   程式中有一些 `except Exception`（例如 `Lock.release()` 刪除 lock 失敗時只記錄警告）。如果模擬的當機被這些地方攔下，程式會繼續執行清理動作，這不是真實當機時會發生的事。見 09 章、`tests/faults.py`。
   </details>

8. 獨立審查發現的「lock 失效」問題，為什麼故障注入測試沒有抓到？
   <details><summary>答案</summary>
   故障注入模擬的是「單一行程在某個操作當機」，而那個問題需要「另一台電腦在這個行程停頓期間移除它的 lock 並刪除資料」，屬於並行的故障模型。見 11 章踩坑紀錄第 7 項。
   </details>

## 2. 動手練習

### 練習 1：新增 `passwd` 指令（更改密碼）

需要修改哪些檔案？提示如下：

- `src/homevault/crypto.py` 已經有 `create_key_file()` 與 `open_key_file()`；
- 新的 key file 要先寫入，確認新密碼能開啟後，才刪除舊的 key file。為什麼要這個順序？
- 想一想中斷點：如果在兩個 key file 並存時中斷，`Repository.open()` 會怎麼處理？（它會逐一嘗試備份庫 keys/ 目錄下的每個檔案。）
- 記得在 `tests/test_security.py` 的錯誤密碼測試中，加入這個新指令。

### 練習 2：讓 restore 平行化

`_Restorer` 目前逐一讀取區塊。提示如下：

- 可以以檔案為單位，交給執行緒池處理；
- 目錄的 mtime 要在所有子項目完成之後才能設定，原本的後序清單要怎麼維持？
- 同一個 pack 中的多個區塊，能不能合併成一次讀取？

### 練習 3：故障注入新增一種故障模式

模擬「刪除在斷電後被回復」：`FaultInjectingBackend` 當機時，把最近幾個被刪除的檔案還原回來。提示如下：

- 刪除前先把內容備份到記憶體；
- 用它驗證 01 章列出的 Windows 限制：forget 之後的 prune 是否會中止？訊息是否清楚？

### 練習 4：連續 N 晚無法更新時提高嚴重性

目前沿用舊版本一律以 exit 3 結束。提示如下：

- `stale_since` 已經記錄日期，可以在 `cmd_backup()` 中計算天數；
- 超過例如 7 天時，改用另一個 exit code，讓排程系統可以發送通知。

## 3. 延伸學習

| 主題 | 本專案的位置 | 搜尋關鍵字 |
|---|---|---|
| 內容定義切塊 | `src/homevault/chunker.py` | `FastCDC paper Xia 2016`、`gear hash content defined chunking` |
| 去重型備份的格式設計 | 整個 repository | `restic repository format design`、`borg backup internals` |
| 崩潰一致性 | `src/homevault/storage.py` | `crash consistency file systems`、`ALICE application-level crash consistency` |
| macOS 的 fsync | `platform_utils.fsync_fd()` | `F_FULLFSYNC macOS fsync durability` |
| 性質測試 | `tests/test_retention.py` | `hypothesis property based testing stateful` |
| Merkle 樹 | `src/homevault/tree.py` | `merkle tree content addressable storage git objects` |
| AEAD 與 nonce | `src/homevault/crypto.py` | `AES-GCM random nonce limit birthday bound` |
