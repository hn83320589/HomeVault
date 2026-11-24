# 04 技術選型

版本以 `uv.lock` 為準（文件對應版本當時的鎖定版本）。

## 1. 執行期相依

| 技術 | 版本 | 用途 | 選用理由 | 替代方案 |
|---|---|---|---|---|
| Python | ≥ 3.12（CI 測 3.12 與 3.13） | 語言 | 📄 題目規定 | — |
| numpy | 2.5.5 | 向量化計算 gear hash | 📄 開發前實測：純 Python 只有 15 MiB/s，numpy 版 173 MiB/s。純 Python 版本在一般家用電腦上會讓 500 MiB / 60 秒的驗收有風險。使用者確認採用 | C 擴充（Windows 上建置麻煩）、multiprocessing（速度仍不夠且不穩定） |
| cryptography | 50.0.2 | AES-256-GCM、scrypt | 📄 題目指定「加密可以使用 cryptography 套件」。scrypt 用它的 `Scrypt` 而不是 `hashlib.scrypt`，因為 🔍 `hashlib.scrypt` 是否可用取決於 Python 連結的 OpenSSL | `hashlib.scrypt`、Argon2（需要額外套件） |
| zstandard | 0.25.0 | 壓縮 | 📄 題目指定「標準函式庫或 zstandard」。🔍 Python 3.12 的標準函式庫沒有 zstd（3.14 才加入 `compression.zstd`），而 zlib 在相同速度下壓縮率較差 | zlib、lzma |
| argparse | 標準函式庫 | CLI | 📄 設計討論中提出、使用者確認：不增加相依套件，`main(argv)` 可以直接測試 | Typer、Click |

## 2. 開發工具

| 工具 | 版本 | 用途 | 選用理由 |
|---|---|---|---|
| uv | 0.13（本機） | 虛擬環境、相依鎖定（`uv.lock`）、建置（`uv_build`） | 📄 設計討論中提出、使用者確認；題目要求「鎖定的相依版本」 |
| pytest | 9.2.0 | 測試 | 📄 題目規定 |
| hypothesis | 6.170.17 | 性質測試 | 📄 題目建議；用於切塊等價性與保留政策的冪等性 |
| ruff | 0.17.6 | lint 與格式化 | 📄 設計討論中提出、使用者確認 |
| GitHub Actions | checkout v7、setup-uv v10.4.0、upload-artifact v7、download-artifact v8 | CI：macOS × Windows × Python 3.12/3.13，加上跨 OS 去重 | 📄 題目加分項；版本已升級以消除 Node.js 20 淘汰警告（commit `0b2d5f4`） |

## 3. 「沒有選」的技術

這些是題目明確禁止，或刻意避開的：

- **現成的備份或去重函式庫**（例如 restic 的格式、borg 的程式碼）：📄 題目規定切塊、pack 格式、索引、快照格式、保留政策、清理流程都必須自行實作。設計上參考了 restic 的概念（pack header 放在檔尾、index、lock、保留政策的語意），但沒有使用任何程式碼。
- **資料庫（例如 SQLite）**：📄 題目規定不需要資料庫。🔍 另一個好處是：SQLite 放在 exFAT 外接硬碟上，被拔除時的行為比自訂的「只新增」格式更難掌握。
- **psutil**：判斷 pid 是否存活時改用 `ctypes` 呼叫 `OpenProcess` 與 `GetExitCodeProcess`（`platform_utils.pid_alive()`），避免增加相依套件。🔍 依據的是全域規範「沒告知就引入新的 library」需要避免。

## 4. 版本相關注意事項

- **setup-uv 自 v8 起不再提供主版本標籤**（例如 `v10`），所以 workflow 指定完整版本 `v10.4.0`，不會自動取得新版。
- **download-artifact v8** 遇到雜湊不符時預設直接讓 job 失敗。
- **zstandard 的 compressor 物件不是執行緒安全的**，`pack.py` 讓每個執行緒使用自己的 compressor（`threading.local`）。
