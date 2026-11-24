# 08 核心演算法

本章拆解四個演算法：

1. FastCDC 切塊與它的向量化（最有技術含量）
2. 保留政策
3. prune 的 pack 分類
4. check 的「受影響檔案」對應

## 1. FastCDC 內容定義切塊

### 要解決的問題

把檔案切成區塊，相同的區塊才能只存一份。

最直覺的做法是固定每 1 MiB 切一刀，但只要在檔案開頭插入 10 個 byte，之後所有切點都會位移，每個區塊的內容都變了，去重完全失效。**內容定義切塊**（Content-Defined Chunking, CDC）改成依「內容」決定切點：在每個位置計算一個滾動雜湊（rolling hash），雜湊值符合條件的位置才切。插入資料只會影響附近的切點，之後的切點會重新同步。

> 📄 依據：題目驗收 3「50 MiB 檔案開頭插入 10 bytes，新增資料 < 5 MiB」

### 輸入與輸出

- **輸入：** 位元組串流、`ChunkerParams`（最小、平均、最大區塊大小）、每個備份庫專屬的 gear 表
- **輸出：** 依序產生的區塊（`Chunker.iter_chunks()`）

### 規格

gear hash 是 `h = (h << 1) + G[byte]`。從 chunk 起點 s 開始，候選長度 L 的判斷位置是 p = s + L − 1：

```
若剩餘長度 ≤ min_size → 整段就是一個 chunk
limit  = min(剩餘長度, max_size)
normal = min(avg_size, limit)
在 L ∈ [min_size, normal) 找第一個 W(p) & mask_s == 0   ← mask_s 多 2 bits，較難切
否則在 L ∈ [normal, limit) 找第一個 W(p) & mask_l == 0   ← mask_l 少 2 bits，較易切
都找不到 → L = limit
```

這就是 FastCDC 的 normalized chunking：在平均值之前不容易切，超過平均值之後容易切，讓區塊大小集中在平均值附近。

**為什麼 mask 取最高位元：** gear hash 每次左移 1 bit，第 k 個 bit 只受最近 k+1 個 byte 影響。如果取最低的 18 bits，切點實際上只由最近 18 個 byte 決定；取最高位元才能涵蓋完整的 64-byte 視窗。

### 向量化的關鍵洞察

逐 byte 的 Python 迴圈只有 15 MiB/s（📄 開發前在 M4 Pro 上實測）。關鍵在於：每次左移 1 bit，64 個 byte 之前的資料就會被移出 64-bit 暫存器，所以：

```
W(p) = Σ_{k=0..63} G[data[p-k]] << k   (mod 2^64)
```

這是一個**滑動視窗和**，可以用倍增法在 6 步內算完整段資料：

```python
# src/homevault/chunker.py（Chunker._rolling_hash）
h = self._gear[data]                      # H_1(i) = G[data[i]]
shift = 1
while shift < WINDOW:                     # shift = 1, 2, 4, 8, 16, 32
    shifted = h.copy()
    shifted[shift:] += h[:-shift] << np.uint64(shift)   # H_2k(i) = H_k(i) + H_k(i-k) << k
    h = shifted
    shift <<= 1
return h
```

以 H₄ 為例：H₄(i) = H₂(i) + H₂(i−2) << 2 = G(i) + G(i−1)<<1 + G(i−2)<<2 + G(i−3)<<3。每倍增一次，視窗長度就加倍。numpy 的 uint64 運算溢位時會自動取模 2⁶⁴，正好符合 gear hash 的定義。

算出整段的 W(p) 後，用 `np.flatnonzero` 找出所有候選位置。再由 `_find_cut()` 以二分搜尋（`np.searchsorted`）套用最小值、最大值與 normalization 規則：

```python
# src/homevault/chunker.py（Chunker._find_cut）
lo, mid, hi = base + p.min_size - 1, base + normal - 1, base + limit - 1
i = int(np.searchsorted(cand_s, lo))
if i < len(cand_s) and cand_s[i] < mid:
    return int(cand_s[i]) - base + 1
j = int(np.searchsorted(cand_l, mid))
if j < len(cand_l) and cand_l[j] < hi:
    return int(cand_l[j]) - base + 1
return limit
```

**為什麼是嚴格等價、不是近似：** 原版 FastCDC 在每個 chunk 起點會把 hash 歸零。但判斷切點的位置至少在起點之後 min_size（256 KiB）處，遠大於 64 bytes，那時的 hash 已經只包含視窗內的資料，與從哪裡開始算無關。`tests/test_chunker.py::test_fast_chunker_equals_reference` 用 Hypothesis 驗證兩個實作對任意輸入產生完全相同的切點。

### 實作細節

- **以 1 MiB 為單位分段計算**（`_SCAN_BLOCK`），限制暫存陣列的記憶體用量。每段前面帶 63 bytes 的前文。
- **只對新讀入的資料計算 W(p)**，候選位置以絕對座標累積，避免重算已經算過的部分。
- **前文不足 64 bytes 的位置**算出的值不正確，但這些位置一定小於「chunk 起點 + min_size − 1」，永遠不會被 `_find_cut()` 採用。

### 複雜度與實測

- **時間：** O(n)，每個 byte 約 20 次 numpy 陣列運算。實測約 **200 MiB/s**（單執行緒）。
- **空間：** 讀取緩衝 8 MiB，加上每段 1 MiB × 8 bytes × 數個暫存陣列。
- **區塊分布**（256 MiB 隨機資料）：平均 1.12 MiB、中位數 1.11 MiB、最小 284 KiB、最大 2.20 MiB，沒有任何區塊被迫切在 4 MiB 上限。

### 邊界條件

| 情況 | 處理方式 |
|---|---|
| 空檔案 | 沒有任何區塊，`chunks` 為空清單 |
| 檔案小於 min_size | 整個檔案是一個區塊 |
| 全部是相同 byte | 雜湊值固定，通常不會符合 mask，每 4 MiB 切一刀（`test_constant_data_cuts_at_max_size`） |
| 讀取緩衝的邊界 | 確保緩衝區至少有 max_size bytes 才做最終判斷 |

### 替代做法

- **Rabin fingerprint：** 題目也允許，但需要多項式運算，比 gear hash 慢。
- **C 擴充：** 更快，但 Windows 上的建置與發佈麻煩。
- **multiprocessing 平行跑純 Python 版：** 速度仍不足，而且會與備份本身的執行緒池搶 CPU。

## 2. 保留政策

### 問題

例如「保留最近 7 天每天一份、4 週每週一份、12 個月每月一份」，要決定哪些快照留下、哪些刪除。

### 演算法

```python
# src/homevault/retention.py（apply_policy 節錄）
ordered = sorted(items, key=lambda it: it[1], reverse=True)    # 由新到舊
for rule, count in policy.rules():
    remaining = count
    last_bucket = None
    for position, (key, when) in enumerate(ordered):
        if remaining <= 0:
            break
        bucket = position if rule == "last" else bucket_key(rule, when)
        if bucket != last_bucket:            # 遇到新的時間桶：保留這一桶最新的快照
            reasons.setdefault(key, []).append(rule)
            remaining -= 1
            last_bucket = bucket
```

### 邊界條件

| 情況 | 處理方式 |
|---|---|
| 跨年的 ISO 週 | 2021-01-01 屬於 2020-W53，用 `isocalendar()` 而不是 `strftime("%W")` |
| 時區 | 時間桶用快照**建立時的本地時間**；台北 2025-01-01 07:00（UTC 前一天）算在 2025-01-01 |
| 電腦關機兩週 | 「最近 7 天」指「有快照的 7 個日子」，所以舊快照不會一次被刪光（📄 使用者確認的語意） |
| 沒有任何規則 | `UsageError`，避免誤刪所有快照 |

### 一個重要的性質

設 K = policy(S)。對任何 K ⊆ S′ ⊆ S，都有 policy(S′) = K。也就是說，**只刪了一部分就中斷，重跑結果完全相同**，這是 forget 中斷安全的數學基礎。

為什麼成立：一個快照沒有被保留，只有兩種原因。一是它所在的時間桶已經有更新的快照被保留，而那個快照還在；二是配額已經用完。刪掉它不會改變任何一條規則的掃描結果。

這個性質由 `tests/test_retention.py::test_policy_is_idempotent_after_partial_forget` 以 Hypothesis 驗證。

**複雜度：** O(n log n)（排序），再加上 O(n × 規則數)。

## 3. prune 的 pack 分類

### 問題

已知哪些 blob 仍在使用（`used`），要決定每個 pack 該保留、刪除，還是重新打包；而且同一個 blob 可能重複存在於多個 pack。

```python
# src/homevault/prune.py（plan_prune 節錄）
order = sorted(packs, key=lambda p: (-sum(e.id in used for e in packs[p]), p))   # ①
for pack_id in order:
    entries = packs[pack_id]
    needed = [e for e in entries if e.id in used and e.id not in kept_blobs]     # ②
    total = sum(e.length for e in entries) or 1
    unused_bytes = total - sum(e.length for e in needed)
    if not needed:
        plan.delete.append(pack_id)
    elif len(needed) == len(entries) or unused_bytes / total <= threshold:
        plan.keep.append(pack_id)
    else:
        plan.repack.append(pack_id)
    kept_blobs.update(e.id for e in needed)
```

① 在使用中的 blob 越多的 pack 越優先處理，所以重複的 blob 會留在「最滿」的 pack，其他 pack 裡的副本則被當成未使用而回收。

② 已經在先前的 pack 中保留過的 blob，不再算作這個 pack 的需求。

**門檻（threshold）** 預設為 0.2：未使用的比例超過 20% 才重新打包，避免為了回收幾 KB 就重寫 16 MiB。🔍 題目原文是「部分區塊仍在使用時重新打包」，這個門檻是設計上的取捨，使用者尚未確認，見 [01 章](01-requirements.md) 待確認事項。

## 4. check 的受影響檔案對應

### 問題

已知一組損毀的 blob，要列出每個快照中受影響的檔案路徑。快照很多，而且大部分目錄彼此共用。

### 做法

`check.py` 的 `map_affected()` 內有一個遞迴函式 `damaged_paths(tree_id)`，它回傳「這棵子樹底下受影響的相對路徑」，並**以 tree id 做 memoization**。因為 tree id 由內容決定，相同 id 的子樹結果必定相同，所以一個被 30 個快照共用的目錄只會走訪一次。

處理損毀資料時還有一個細節：進入遞迴前先寫入 `memo[tree_id] = []`。萬一資料損毀造成循環引用，遞迴也不會無窮下去。

**複雜度：** O(不同 tree 的數量 + 節點數)，而不是 O(快照數 × 每個快照的節點數)。
