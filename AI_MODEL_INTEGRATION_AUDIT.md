# TW Quant AI — Taiwan Stock Quantitative Trading Platform
# AI 模型與資料管線深度審查報告 (AI_MODEL_INTEGRATION_AUDIT.md)

審查日期：2026-10-21  
審查對象：`qlib-tw-trader`（Commit SHA: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011`）  
審查目的：評估將其 AI 選股模型、特徵工程與資料管線整合至台股量化交易系統之可行性、風險與技術缺口。

---

## 一、審查總結與相容性矩陣

`qlib-tw-trader` 是一個基於微軟 Qlib 框架針對台股設計的機器學習選股專案。經過完整原始碼審查，確認其具備先進的 `DoubleEnsemble` 模型架構與因子正交化（RD-Agent IC 去重）概念，但**在資料日曆相容性、缺失值處理、依賴套件環境以及推論合約規格上存在顯著缺陷與風險**。

| 審查維度 | 現狀評估 | 風險等級 | 整合處理策略 |
| :--- | :--- | :---: | :--- |
| **1. 資料獲取方式** | TWSE OpenAPI + FinMind + yfinance | 低 | 保留適配器邏輯，但需離線解耦 |
| **2. 股票代碼格式** | 純 4 碼數字 (`2330`) 與 Yahoo 代號 (`2330.TW`) 混用 | 中 | 於 `contracts` 與 `prediction` 層統一清洗為純台股代碼 |
| **3. 交易日曆與區域** | 硬編碼 `region=REG_CN`（中國市場日曆） | **嚴重** | **與台股假日完全不相容**，必須抽離並替換為 TWSE 交易日曆 |
| **4. 特徵工程流程** | Qlib Alpha158 表達式 + 台股三大法人/融資/營收 | 中 | 特徵定義可用，但需脫離 Qlib 底層 C++ 執行期 |
| **5. 訓練標籤定義** | $T$ 日特徵預測 $T+1 \to T+3$ 收益率 | 低 | 時序因果定義清楚，買入 $T+1$、賣出 $T+3$ |
| **6. 模型訓練方式** | DoubleEnsemble (LightGBM) + Optuna 超參搜索 | 低 | 演算法成熟，但需要 GPU / LightGBM 訓練環境 |
| **7. 預測輸出格式** | 無型別保護之簡單字典清單 `[{"symbol", "score", "rank"}]` | 高 | 建立 `StockPrediction` 與 `PredictionBatch` 強制合約 |
| **8. 模型版本管理** | 本地目錄 `data/models/YYYYMM-hash/` | 中 | 缺乏集中式模型登錄與特徵版本關聯 |
| **9. 訓練/驗證分割** | 滾動 Walk-Forward 切分（504天訓練、100天驗證、7天隔離） | 低 | 7 天隔離期（Embargo）有效防止標籤重疊 |
| **10. 潛在 Lookahead Bias** | 截面標準化與月營收公告日存在潛在時序偏差 | 高 | 建立嚴格 `data_cutoff_time` 屏障與真實營收公布時間戳 |
| **11. 缺失資料處理** | 截面特徵採用 `df.fillna(0)` 靜默填零 | **嚴重** | **嚴格禁止隱式補零**，特徵缺失標的必須記錄失敗 |
| **12. 除權息與還原價** | 依賴 yfinance 抓取 `AdjClose` 計算還原因子 | 中 | 外部網路爬蟲穩定性受限，長期需改接官方除權息資料 |
| **13. 推論所需套件** | `pyqlib`, `lightgbm`, `optuna`, `pandas`, `sqlalchemy` | **阻斷** | 在當前 Python 3.13 環境下缺件且 C++ wheel 編譯困難 |
| **14. 現有測試可執行性** | 執行 `pytest` 立即因缺少 `sqlalchemy` 報錯中斷 | **阻斷** | 建立獨立於環境依賴的 Mock 測試與抽象推論協定 |

---

## 二、14 項核心問題深度審查分析

### 1. 台股資料取得方式
- **程式碼依據**：`qlib-tw-trader/src/adapters/`
  - `twse.py`：使用 `TwseBulkOHLCVAdapter` 批次爬取 TWSE 官網 RWD API (`https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL`)，並實作每 5 秒 3 次請求的限流保護。
  - `finmind.py`：調用 FinMind 開放資料 API 取得三大法人、融資融券、借券與外資持股比例。
  - `yfinance.py`：使用 `yfinance.Ticker(f"{stock_id}.TW").history()` 取得還原收盤價。
- **結論**：資料抓取腳本完整，但極度依賴公開網頁爬蟲與外部 API，不適合直接作為實時交易行情來源，應嚴格限制於離線研究與特徵批次同步。

### 2. 股票代碼格式
- **程式碼依據**：`twse.py` 輸出為純 4 位代號（如 `"2330"`）；`yfinance.py` 補上 `".TW"`。
- **結論**：兩者混用可能導致資料庫索引不匹配。自建套件統一以純英數字代碼（`"2330"`）為內部標準，對外串接時再依需求轉接。

### 3. 交易日曆及日期處理（嚴重架構缺陷）
- **程式碼依據**：`qlib-tw-trader/src/services/predictor.py` 第 37-42 行：
  ```python
  import qlib
  from qlib.config import REG_CN
  qlib.init(provider_uri=str(self.data_dir), region=REG_CN)
  ```
- **重大矛盾**：
  - 專案名稱為 `qlib-tw-trader`，但初始化 Qlib 時直接使用 `region=REG_CN`（中國 A 股日曆）。
  - 中國 A 股市場與台灣加權指數市場的休假日存在顯著差異（例如中國有十一黃金週、五一長假；台灣有 228 紀念日、雙十國慶、颱風天休市等）。
  - 若使用 `REG_CN`，Qlib 在計算滾動因子（如 `Ref($close, 1)` 或 `EMA($close, 20)`）時，會強制套用中國交易日索引，導致台股開市日特徵丟失或休市日出現虛假空值。

### 4. 特徵工程流程
- **程式碼依據**：`src/services/qlib_exporter.py` 與 `src/services/factor_selection/`
  - 包含價量因子（Alpha158）、三大法人買賣超（外資、投信、自營商）、融資融券餘額、外資持股比與月營收年增率（YoY）。
  - 使用 RD-Agent IC 去重機制（`ic_dedup.py`），過濾皮爾森相關係數超過 0.99 之共線性冗餘因子。
- **結論**：特徵設計具備高學術價值與量化代表性。

### 5. 訓練標籤定義 (Label Definition)
- **程式碼依據**：`src/shared/constants.py` 第 39 行：
  ```python
  LABEL_EXPR = "Ref($close, -3) / Ref($close, -1) - 1"
  ```
- **分析**：
  - 標籤定義為 $T+1$ 收盤價到 $T+3$ 收盤價之 2 日累計報酬率。
  - 特徵資料截止於 $T$ 日收盤（$T$ 日盤後生成預測），買入預計於 $T+1$ 日執行，持有至 $T+3$ 日平倉。
  - 時序邏輯嚴密，無標籤預知當日收益的 Lookahead Bias。

### 6. 模型訓練方式
- **程式碼依據**：`src/services/double_ensemble.py`
  - 採用 DoubleEnsemble 演算法（LightGBM 為 Base Estimator）。
  - 通過多輪次動態調整樣本權重（Sample Reweighting）與特徵子空間加權，抑制金融時間序列的資料概念漂移（Concept Drift）。
  - 整合 Optuna 進行貝葉斯超參數尋優（學習率、樹深度、葉子數、正則化參數）。

### 7. 預測輸出格式（高風險問題）
- **程式碼依據**：`src/services/predictor.py` 第 190-197 行：
  ```python
  signals = [
      {"symbol": row["symbol"], "score": float(row["score"]), "rank": int(row["rank"])}
      for _, row in result_df.iterrows()
  ]
  ```
- **重大風險**：
  - 輸出僅為簡單字典清單，命名為 `signals` 容易誘使開發者將分數誤認為直接下單信號。
  - 缺乏任何時間戳記（時區）、資料截止時間、模型版本與特徵版本標記。
  - 一旦下游模組收到舊日期的快取評分，無法察覺時間過期。

### 8. 模型版本管理
- **程式碼依據**：`MODELS_DIR / model_name`，命名慣例為 `YYYYMM-hash`。
- **結論**：僅有檔案系統級別的命名，缺乏集中式 Model Registry、特徵依賴哈希與 Git Commit 關聯。

### 9. 訓練集與測試集切分
- **程式碼依據**：`constants.py`
  - `TRAIN_DAYS = 504`（約 2 年）、`VALID_DAYS = 100`（約 4 個月）、`EMBARGO_DAYS = 7`（7 天隔離期）。
- **結論**：採用時間序列 Walk-Forward 滾動切分，搭配 7 日 Embargo 防止標籤向前洩漏，切分邏輯符合量化最佳實踐。

### 10. 潛在 Lookahead Bias（未來資料洩漏分析）
- **分析發現兩處隱患**：
  1. **截面標準化洩漏**：在 `predictor.py` 中執行 `(df[col] - mean) / std`。若傳入的 `instruments` 股票池包含未來才上市的股票，或未依據歷史當下成分股進行過濾，會引入存活者偏差（Survivorship Bias）。
  2. **月營收公布日固定假設**：`qlib_exporter.py` 設定 `REVENUE_ANNOUNCE_DAY = 10`。台股法規雖規定次月 10 日前公告，但部分公司會提前於 5~8 日公布，亦有延後或遇假日順延之情形。固定假定 10 日公告會導致資訊滯後或提前洩漏。

### 11. 缺失資料處理（嚴重安全隱患）
- **程式碼依據**：`predictor.py` 第 172 行：
  ```python
  df = df.fillna(0)
  ```
- **重大危害**：
  - 若某檔股票當日停牌無成交量，或財務特徵（如本益比、殖利率）缺失，系統靜默填補為 0。
  - 對於樹狀模型（LightGBM），0 是一個具有明確幾何意義的數值，會直接扭曲模型預測，甚至被誤判為極低估值或異常賣壓。
  - **整改要求**：在自建預測架構中，必須嚴格記錄缺失因子與失敗標的，禁止隱式補零。

### 12. 除權息與價格還原方式
- **程式碼依據**：`yfinance` 抓取已還原價格，除以原始未還原收盤價得出累計除權息係數。
- **結論**：邏輯正確，但 yfinance 在抓取台股除權除息行情時偶有延遲或缺失，長期建議整合 TWSE 官方除權息計算表。

### 13. 模型推論所需套件與環境
- **套件清單**：`pyqlib`, `lightgbm`, `optuna`, `pandas`, `sqlalchemy`, `pydantic`。
- **現狀**：當前系統環境為 Windows + Python 3.13.4。`pyqlib` 官方輪子（wheels）僅支援到 Python 3.10/3.11，且依賴 MSVC C++ 編譯器。直接在主環境安裝極易遭遇編譯阻斷。

### 14. 現有測試可執行性
- **實測結果**：執行 `python -m pytest qlib-tw-trader/tests/`，在收集測試（Collection phase）即拋出 `ModuleNotFoundError: No module named 'sqlalchemy'` 中斷退出。
- **結論**：原專案測試無法在當前純標準庫 Python 環境下直接運行。

---

## 三、整體架構改進與隔離方案

為確保交易系統核心（`src/tw_quant/`）維持高可靠度、高可測試性與零重型套件污染，實施以下隔離架構：

```mermaid
flowchart TD
    subgraph Hostile_ML_Env ["重型 ML 環境 (qlib-tw-trader) [隔離區]"]
        Q1[TWSE / FinMind 原始資料庫] --> Q2[Qlib .bin 二進位特徵庫]
        Q2 --> Q3[DoubleEnsemble LightGBM 訓練]
        Q3 --> Q4[離線產出模型與特徵權重]
    end

    subgraph Prediction_Contract ["統一預測契約 (src/tw_quant/prediction/) [自建核心]"]
        Q4 -.->|依循合約格式導出| PB[PredictionBatch]
        PB --> MD[PredictionMetadata\n(model_version, feature_version, is_mock)]
        PB --> SP[StockPrediction\n(symbol, score, rank, data_cutoff_time)]
        PB --> FL[failed_symbols & missing_features\n(明確記錄缺失，嚴禁補零)]
    end

    subgraph Strategy_Execution ["交易決策與執行層 [未接入]"]
        SP -.->|下一階段由 StrategyEngine 讀取| SE[策略生成訊號 StrategyEngine]
        SE -.->|產出標準買賣訊號| UTS[UnifiedTradeSignal]
        UTS -.-> GW[TradingGateway]
    end
```

### 關鍵決策：
1. **嚴格禁止 AI 模型直接下單**：模型僅產出包含 `score` 與 `rank` 的 `PredictionBatch`。
2. **Mock 模型顯式標記**：為確保離線單元測試可在任何純 Python 環境運行，提供 `MockTestPredictor`，強制宣告 `is_mock = True` 與 `model_type = "MOCK_FOR_TESTING"`。
3. **時序與截止時間強制屏障**：所有預測物件內建 `data_cutoff_time` 與時序因果校驗，杜絕任何未來資料洩漏。
