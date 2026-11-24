# HomeVault 教學文件

**HomeVault 是一個家用增量備份工具**：以 FastCDC 內容切塊去重、以 AES-GCM 加密，備份庫放在 exFAT 外接硬碟上，Windows 與 macOS 共用，而且任何時間點中斷都不會損毀。

**技術棧：** Python 3.12、numpy、cryptography、zstandard、argparse、uv、pytest、Hypothesis、ruff、GitHub Actions（細節見 [04 章](04-tech-stack.md)）

**文件對應版本：** commit `7495d8f`（2026-11-24）

**標記說明：**
- 📄 **依據**：有明確來源，例如題目、對話中的決策、commit、`docs/design.md`。
- 🔍 **推斷**：從程式碼或情境推論，請自行確認。

## 閱讀路線

| 路線 | 章節 | 時間 |
|---|---|---|
| 快速掌握 | 00 → [03 架構](03-architecture.md) → [07 程式導讀](07-code-walkthrough.md) | 約 30 分鐘 |
| 完整學習 | 依序閱讀 01 → 12 | 約 2.5 小時 |
| 想學演算法 | [08 核心演算法](08-algorithms.md)（FastCDC 向量化、保留政策、prune 分類） | 15 分鐘 |
| 想學中斷安全的設計 | [05 資料儲存設計](05-database.md) → [06 資料流](06-api-dataflow.md) → [11 取捨與踩坑](11-tradeoffs-pitfalls.md) | 40 分鐘 |
| 想學測試設計 | [09 測試](09-testing.md)（故障注入、性質測試） | 15 分鐘 |
| 想學跨平台的坑 | [11 章](11-tradeoffs-pitfalls.md)踩坑紀錄第 1、9、10、11、13 項 | 10 分鐘 |

## 章節清單

| 章節 | 檔案 | 狀態 |
|---|---|---|
| 01 需求分析 | [01-requirements.md](01-requirements.md) | 已產生 |
| 02 系統分析 | [02-system-analysis.md](02-system-analysis.md) | 已產生 |
| 03 架構設計 | [03-architecture.md](03-architecture.md) | 已產生 |
| 04 技術選型 | [04-tech-stack.md](04-tech-stack.md) | 已產生 |
| 05 資料庫設計 | [05-database.md](05-database.md) | **改寫**：本專案沒有資料庫，改寫為備份庫格式（資料層）的設計 |
| 06 API 規格與資料流 | [06-api-dataflow.md](06-api-dataflow.md) | **改寫**：本專案沒有 HTTP API，改寫為 CLI 介面與資料流 |
| 07 程式內容導讀 | [07-code-walkthrough.md](07-code-walkthrough.md) | 已產生 |
| 08 核心演算法 | [08-algorithms.md](08-algorithms.md) | 已產生 |
| 09 測試 | [09-testing.md](09-testing.md) | 已產生 |
| 10 部署與環境設定 | [10-deployment.md](10-deployment.md) | 已產生（安裝到家用電腦與排程，沒有伺服器部署） |
| 11 設計取捨與踩坑紀錄 | [11-tradeoffs-pitfalls.md](11-tradeoffs-pitfalls.md) | 已產生 |
| 12 延伸練習與自我檢核 | [12-exercises.md](12-exercises.md) | 已產生 |
| 名詞對照表 | [glossary.md](glossary.md) | 已產生 |

## 相關文件

- [README](../../README.md)：使用方式、備份庫格式、中斷安全時序圖、效能量測
- [docs/design.md](../design.md)：開發前的系統分析與架構設計（經使用者確認的版本）

## 修訂紀錄

| 日期 | 對應 commit | 更新章節 |
|---|---|---|
| 2026-11-24 | `7495d8f` | 初版，全部章節 |
