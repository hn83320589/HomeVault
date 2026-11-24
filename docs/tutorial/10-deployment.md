# 10 部署與環境設定

HomeVault 不需要伺服器或資料庫，「部署」指的是安裝到家裡的兩台電腦，並設定每晚排程。

## 1. 環境需求

| 項目 | 需求 |
|---|---|
| 作業系統 | macOS 或 Windows（CI 測試 `macos-latest`、`windows-latest`） |
| Python | 3.12 以上（`.python-version` 為 3.12） |
| 套件管理 | uv（建議），或 pip |
| 備份庫位置 | 本機路徑：exFAT 外接硬碟或網路磁碟 |

## 2. 本機開發環境

```bash
git clone https://github.com/hn83320589/HomeVault.git
cd HomeVault
uv sync                              # 依 uv.lock 安裝鎖定版本
uv run homevault --help
uv run pytest
```

## 3. 安裝給家人使用

```bash
uv tool install .                    # 安裝成全域指令 homevault
homevault init --repo /Volumes/Backup/homevault
```

## 4. 設定項

HomeVault 沒有設定檔：備份庫的參數存在備份庫的 `config` 中，執行參數則由命令列與環境變數提供。

| 名稱 | 用途 | 範例值 | 必填 |
|---|---|---|---|
| `HOMEVAULT_REPO` | 預設的備份庫路徑（等同 `--repo`） | `<your-repo-path>` | 否 |
| `HOMEVAULT_PASSWORD` | 備份庫密碼 | `<your-password>` | 否（擇一提供） |
| `--password-file` | 從檔案讀取密碼，排程時建議使用 | `<path-to-password-file>` | 否（擇一提供） |
| `--exclude` / `--exclude-file` | 排除規則 | `*.tmp`、`node_modules` | 否 |
| `--threads` | 備份的執行緒數，預設 min(4, CPU 數) | `2` | 否 |
| `--no-low-priority` | 不要降低行程優先權 | — | 否 |

備份庫的 `config` 欄位（建立時決定，之後不會改變）：`chunker.min_size`、`chunker.avg_size`、`chunker.max_size`、`pack_size`。

## 5. 排程

範例見 README「排程每晚自動備份」：macOS 用 cron，Windows 用 `schtasks`。設計上的注意事項如下：

- **輸出導向 log 檔時，一律使用 UTF-8**（`ensure_utf8_output()`）。
- **行程優先權預設降低**：macOS 用 `os.nice(10)`，Windows 用 `BELOW_NORMAL_PRIORITY_CLASS`（`platform_utils.lower_process_priority()`）。
- **exit code 3 不代表失敗。** 它表示有沿用舊版本或備份時變動的檔案，log 中會列出這些檔案。可以據此設定通知，例如「連續多晚 exit 3 就提醒家人關掉 Outlook」。

## 6. CI（GitHub Actions）

`.github/workflows/ci.yml` 有三個 job：

| Job | 執行環境 | 內容 |
|---|---|---|
| `test` | macOS、Windows × Python 3.12、3.13 | `uv sync --locked` → ruff check → ruff format --check → pytest |
| `cross-os-first` | macOS | 產生固定資料、建立備份庫並備份，再上傳成 artifact |
| `cross-os-second` | Windows（在 first 之後） | 下載備份庫，備份相同的資料，驗證新增 < 1% 且兩個快照都能還原 |

📄 實際結果：Windows 第二次備份只新增了 0.0235%。

## 7. 常見問題與排除

| 症狀 | 原因 | 處理方式 |
|---|---|---|
| exit 10「密碼錯誤」 | 密碼錯誤；或密碼檔結尾有多餘的空白 | 程式只會去除結尾的換行。請檢查密碼檔的內容 |
| exit 11「備份庫正在被使用」 | 另一台電腦正在執行，或殘留的 lock 還沒超過 30 分鐘 | 確認沒有其他電腦在執行後，執行 `homevault unlock`（必要時加 `--all`） |
| exit 12「外接硬碟是否被移除」 | 硬碟在執行途中被拔除 | 接回硬碟後重跑即可，已寫入的資料會被沿用 |
| macOS cron 讀不到「文件」「照片」 | 隱私權限 | 在「完整磁碟取用權限」中加入 `/usr/sbin/cron` |
| Windows 上輸出中文時出現 `UnicodeEncodeError` | 舊版本的 bug（commit `5938516` 已修正） | 更新到最新版 |
| 每晚都 exit 3，同一個檔案「自某日起無法更新」 | 該檔案每晚都被程式開著 | 讓家人在睡前關掉該程式，或把排程時間改到更晚 |
