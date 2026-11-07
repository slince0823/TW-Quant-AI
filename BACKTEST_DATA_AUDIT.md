# 第三階段 C-1：台股歷史回測資料全面盤點報告（BACKTEST_DATA_AUDIT.md）

> **文件狀態**：第三階段 C-1 歷史資料與既有 Adapter 實地審查正式報告  
> **審查時間**：2026-10-21  
> **審查範圍**：`src/tw_quant/`、`qlib-tw-trader/`（Commit `774b3fb6`）、`anti-gambling-trader-tw/`（Commit `8609cef7`）及工作區本地檔案系統。  
> **誠實揭露原則**：本報告完全依據實際檢視之原始碼與磁碟檔案撰寫，絕不假設未下載之資料已存在，亦不隱瞞既有子專案程式碼中的前視偏差與倖存者偏差缺陷。

---

## 一、本地磁碟資料實體盤點結果（Executive Summary）

經實際掃描工作區磁碟目錄，目前本地資料狀態如下：

1. **`qlib-tw-trader/data/` 目錄**：
   - 僅包含 [`.gitignore`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/data/.gitignore) 與 [`.gitkeep`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/data/.gitkeep)（**0 Bytes 實際資料**）。
   - 本地**不存在**任何 SQLite 資料庫（無 `quant.db`）、**不存在**任何 Qlib 二進位資料檔（無 `.bin` 特徵檔、無 `calendars/day.txt`、無 `instruments/all.txt`），亦**不存在**任何已訓練完成的 LightGBM 模型權重檔（無 `.pkl` 檔案）。
2. **`anti-gambling-trader-tw/` 目錄**：
   - 僅於 [`core/examples/tw_stock_gambling.csv`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/anti-gambling-trader-tw/core/examples/tw_stock_gambling.csv) 存在一份 2,165 Bytes（26 筆交易紀錄）的策略交易結果範例檔，屬於交易後績效評估測試樣本，並非市場歷史 OHLCV 行情資料。
3. **`src/tw_quant/` 目錄**：
   - 包含完整之記憶體內合成資料結構（`MarketBar`、`StockPrediction`、`PredictionBatch`）與 156 項自動化測試 Fixture，但尚未包含真實歷史行情資料檔。

> [!WARNING]
> **關鍵結論**：目前本地磁碟**沒有任何可直接用於真實市場多年期回測的台股歷史行情與基本面資料庫**。所有在 `qlib-tw-trader` 中的資料獲取邏輯皆為「需連線外部 API（FinMind、TWSE OpenAPI、Yahoo Finance）動態抓取後寫入本地 SQLite」的程式碼介面，且我們在審查其原始碼時發現多處嚴重的回測方法論缺陷（詳見第二、三節）。

---

## 二、11 大回測資料領域分級盤點總表

依據任務要求，我們將每項回測所需資料項目嚴格分為以下四級：
- **A 級**：已存在且可直接用於回測（本地已有資料或純邏輯模組已完備且無偏差）。
- **B 級**：已有介面，但資料不完整、存在方法論缺陷或尚未下載至本地。
- **C 級**：尚未實作（既無本地資料，亦無擷取/處理介面）。
- **D 級**：目前無法驗證（需依賴外部付費/憑證 API 或特定歷史源方能驗證）。

| 項次 | 資料項目 | 評定等級 | 本地實體資料 | 既有程式碼位置 | 核心問題與風險摘要 |
| :---: | :--- | :---: | :---: | :--- | :--- |
| **1** | **台股歷史日線 OHLCV 資料** | **B** | 無（0 Bytes） | [`finmind.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/adapters/finmind.py#L148-L216)<br/>[`yfinance.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/adapters/yfinance.py#L19-L68)<br/>[`StockDailyPrice`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/models/stock_daily_price.py#L13-L39) | 1. 本地未下載任何歷史日線。<br/>2. `yfinance.py` 僅抓 `Close` 作為 `adj_close`（預設為**前復權價**，將未來配息乘入過去價格，且四捨五入至小數點後 2 位），並硬編碼 `.TW` 後綴（上櫃 `.TWO` 會失敗）。<br/>3. `QlibExporter` 在發現有 `adj_close` 時，會用 `adj_close / close` 等比例縮放 `open/high/low`，破壞原始成交價與 Tick Size。 |
| **2** | **股票清單與歷史可交易股票池（Universe）** | **B（嚴重倖存者偏差）** | 無 | [`universe.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/routers/universe.py#L101-L190)<br/>[`qlib_exporter.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/qlib_exporter.py#L205-L217) | **`qlib-tw-trader` 存在 100% 倖存者偏差與未來市值前視偏差**：`sync_universe` 抓取「執行當下最新一日」全市場收盤價乘以當下發行股數，取當下市值前 100 大股票覆寫整張 `StockUniverse` 表；`QlibExporter._write_instruments` 再將這 100 檔「今日贏家」套用至過去所有歷史年份。 |
| **3** | **上市、下市、變更交易、終止上市資料** | **C** | 無 | 無 | 完全沒有歷史下市櫃股票（Delisted Stocks）清單、上市日期（IPO Date）、終止上市日期與最後清算價格表。 |
| **4** | **停牌、恢復買賣、處置股資料** | **B / C** | 無 | 撮合端已實作：[`FillEngine`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/engine.py#L162-L165)、[`RiskEngine`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/risk/engine.py#L316-L329)<br/>資料端：**C（未實作）** | `src/tw_quant/` 已完整實作處置股（`is_disposition`）風控攔截與量能折半邏輯，但 `qlib-tw-trader` 完全沒有抓取證交所「公布處置有價證券表（`PUNISH`）」或歷史停牌/減資停牌期間的資料表。 |
| **5** | **漲跌停價格與計算基準（前收/除權息參考價）** | **A（演算法） / C（除權息平盤價資料）** | 無 | 演算法已實作：[`CostModel.calculate_limit_prices`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/engine.py#L54-L66) | `src/tw_quant/` 已具備精確的台股 $\pm 10\%$ 漲跌停與 6 級 Tick Size 向下取整演算法（**A 級**）。但歷史資料端缺少除權息當日的「除權息平盤參考價」（在除權息日，漲跌停基準價並非前一日收盤價，而是除權息參考價）。 |
| **6** | **成交量資料（支援 5% Volume Cap 與 ADV 過濾）** | **B** | 無 | 邏輯已實作：[`FillEngine`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/engine.py#L153-L165)、[`selector.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/selector.py#L121-L129)<br/>資料介面：[`finmind.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/adapters/finmind.py#L186) | `FinMindPriceAdapter` 有擷取 `Trading_Volume`（股數），且 `src/tw_quant/` 已實作 5% 量能上限與 `min_adv` 過濾；惟需注意 TWSE 官方 API 成交量單位為「股」或「張」之轉換一致性，且本地尚未下載歷史成交量資料。 |
| **7** | **除權息、現金股利、股票股利、減資、拆股資料** | **C** | 無 | 無 | `qlib-tw-trader` 的資料庫 Schema（[`models/`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/models/__init__.py)）僅有日線、三大法人、融資融券、月營收、股票池與交易日曆共 6 張表，**完全沒有除權息明細表（Ex-dividend/Ex-rights）、現金股利金額、股利發放日、股票分割或減資換股資料表**。 |
| **8** | **財報、月營收與特徵之實際可得時間（Point-in-Time）** | **B（月營收有前視瑕疵） / C（季報財報未實作）** | 無 | [`finmind.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/adapters/finmind.py#L343-L407)<br/>[`qlib_exporter.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/qlib_exporter.py#L355-L400) | 1. **季報財報（资产負債表、損益表、EPS）完全未實作（C 級）**。<br/>2. **月營收存在 Point-in-Time 缺陷（B 級）**：`FinMindMonthlyRevenueAdapter` 丟棄了原始公告日期欄位，僅儲存 `revenue_year` 與 `revenue_month`；`QlibExporter._expand_revenue_pit` 則硬編碼將所有公司之月營收視為「次月 10 日（`REVENUE_ANNOUNCE_DAY = 10`）起可用」，忽略了遇假日順延（如農曆春節 1 月營收順延至 2 月 15 日）以及 10 日盤中/盤後發布時點差異。 |
| **9** | **TWSE 交易日曆（開休市、農曆年、颱風假）** | **B** | 無 | [`sync_service.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/sync_service.py#L98-L128)<br/>[`walk_forward_backtester.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/walk_forward_backtester.py#L133-L138) | 1. `sync_trading_calendar` 並非抓取證交所官方開休市行事曆，而是呼叫 `yfinance` 抓取 `0050.TW` 的歷史 K 棒日期反推交易日！因此**沒有未來交易日**（無法在最新交易日查詢 `next_trading_day(T)`），且受制於 Yahoo Finance 資料缺漏。<br/>2. `WalkForwardBacktester._init_qlib` 甚至將 Qlib 地區誤設為中國大陸市場 `region=REG_CN`。 |
| **10** | **基準指數資料（加權指數 `^TWII`、報酬指數、`0050`）** | **C** | 無 | 無（僅借用 `0050.TW` 日期作日曆） | `qlib-tw-trader` 並未儲存 `^TWII`（發行量加權股價指數）、`IR0001`（加權股價報酬指數）或 `0050.TW` 的完整價格與配息序列，且其既有 `WalkForwardBacktester` 完全沒有進行 Benchmark 相對績效與超額報酬比較。 |
| **11** | **資料更新流程與本地資料快照版本控制** | **C** | 無 | [`sync_service.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/sync_service.py) | 既有同步服務採「直接 `INSERT OR REPLACE` 或 `DELETE` 覆寫 SQLite 表並重新匯出覆蓋 `.bin` 檔」之設計，沒有資料快照雜湊（Dataset Snapshot Hash）、沒有匯出版本號，無法保證不同時間點執行的回測使用完全相同的歷史資料集。 |

---

## 三、既有子專案原始碼深度審查發現之重大缺陷（Code-Level Evidence）

在本次 C-1 審查中，我們逐行檢視了 `qlib-tw-trader` 的資料管線與回測器原始碼，發現以下 **5 項若直接沿用將導致回測結果完全失真的重大缺陷**。這些證據再次證明：**我們絕不能直接沿用 `qlib-tw-trader` 的 `WalkForwardBacktester`，必須在 `src/tw_quant/backtest/` 建立獨立、嚴謹的回測引擎**。

### 缺陷 1：`qlib-tw-trader` 股票池同步邏輯具有 100% 倖存者偏差與未來市值前視偏差
- **程式碼位置**：[`qlib-tw-trader/backend/app/routers/universe.py` 第 101–190 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/routers/universe.py#L101-L190) 及 [`qlib_exporter.py` 第 205–217 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/qlib_exporter.py#L205-L217)。
- **實際行為**：
  1. `sync_universe()` 呼叫證交所當日即時 API `STOCK_DAY_ALL`（今日收盤價）與 `MI_QFIIS`（今日發行股數），計算**今日市值**並取前 100 名。
  2. 第 172 行執行 `await db.execute(delete(StockUniverse))` 清空整張資料表，僅寫入今日前 100 大公司。
  3. `QlibExporter._write_instruments()` 將這 100 檔「今日市值前百大」股票直接寫入 Qlib `instruments/all.txt`，並將時間區間設為整個歷史回測期間（例如 2018–2026）。
- **嚴重後果**：在 2018 年進行回測選股時，候選池裡只有「活到 2026 年且在 2026 年市值擠進前 100 名」的未來贏家，完全排除了期間衰退跌出百大或下市的公司。

### 缺陷 2：月營收 Point-in-Time 展開硬編碼為次月 10 日（忽略假日順延與實際公告時點）
- **程式碼位置**：[`qlib-tw-trader/backend/app/services/qlib_exporter.py` 第 40 行與第 355–400 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/qlib_exporter.py#L355-L400)。
- **實際行為**：
  - 常數定義 `REVENUE_ANNOUNCE_DAY = 10`。
  - `_expand_revenue_pit()` 將 $Y$ 年 $M$ 月的營收，一律設定為 `announce_date = date(Y, M+1, 10)`，並在 `announce_date <= cal_date` 時直接填入。
- **嚴重後果**：
  1. 依台灣證交所規定，每月 10 日若遇假日（特別是農曆春節連假，1 月營收公告截止日經常順延至 2 月 12 日～15 日），多數公司會在收假後才公告；硬編碼 10 日會在連假後第一個交易日提前讀到尚未公告的月營收。
  2. 部分公司在 10 日當天下午盤後（`13:30` 以後）才上傳公開資訊觀測站；若將 `announce_date` 視為 10 日開盤即已知，並在 10 日當天交易，即構成日內前視偏差。

### 缺陷 3：`yfinance` 前復權價格（Forward-Adjusted Close）混入價格管線
- **程式碼位置**：[`qlib-tw-trader/backend/app/adapters/yfinance.py` 第 42–64 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/adapters/yfinance.py#L42-L64) 及 [`qlib_exporter.py` 第 250–268 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/qlib_exporter.py#L250-L268)。
- **實際行為**：
  - `YFinanceAdjCloseAdapter` 呼叫 `ticker.history()`（`yfinance` 預設 `auto_adjust=True`），將回傳的 `Close` 四捨五入至小數點後兩位（`round(adj_close, 2)`）存入 `StockDailyPrice.adj_close`。
  - `QlibExporter._export_stock_features` 若發現 `adj_close > 0`，即計算 `factor = adj_close / close`，並將歷史 `open, high, low, close` 全部乘上 `factor`。
- **嚴重後果**：
  1. `yfinance` 的調整價是從「最新日期」往過去連乘所有除權息因子的**前復權價**。對於幾年前的股價（例如台積電或高息股），前復權後的價格會大幅縮小且被 `round(..., 2)` 截斷，造成歷史報酬率計算出現捨入誤差。
  2. 若將此縮放後的價格拿去執行撮合，會完全破壞台股真實股價區間對應的升降單位（Tick Size）與每張所需資金成本。

### 缺陷 4：既有 `WalkForwardBacktester` 存在「單週重複累乘 5 次雙日報酬（2 倍槓桿膨脹）」之重大數學錯誤
- **程式碼位置**：[`qlib-tw-trader/backend/app/services/walk_forward_backtester.py` 第 879–941 行与第 1041 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/walk_forward_backtester.py#L879-L941)。
- **實際行為**：
  - 在 `_backtest_week()` 中，迴圈遍歷一週內的每一個交易日 $d_i$（週一至週五共 5 天）。
  - 在每一天 $d_i$，模型選出 Top-K 股票，並計算 **未來 2 個交易日的持有報酬** `ret = close[i+3] / close[i+1] - 1`（第 911 行）。
  - 接著在第 934 行，將這 5 個互相重疊的「雙日報酬率」直接連乘作為該週報酬率：`week_compound *= (1.0 + avg_ret)`！
  - 此外，該回測器**完全不扣除手續費、完全不扣除 0.3% 證交稅、完全不考慮漲跌停與資金限制**，甚至在第 1041 行將總交易次數硬編碼為 `total_trades = len(weekly_results) * 10 # 估計值`。
- **嚴重後果**：在 5 個交易日的時間內累乘了 $5 \times 2 = 10$ 個交易日的重疊報酬，相當於在不知情下開了 200% 的無成本槓桿，且零交易稅費，導致其宣稱的回測績效在金融邏輯上完全無效。

### 缺陷 5：特徵缺失值無條件補 0（`df.fillna(0)`）
- **程式碼位置**：[`qlib-tw-trader/backend/app/services/walk_forward_backtester.py` 第 407 行](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/qlib-tw-trader/backend/app/services/walk_forward_backtester.py#L407)。
- **實際行為**：在 `_prepare_features()` 中對所有特徵直接執行 `df = df.fillna(0)`。
- **嚴重後果**：對於未標準化的比率或移動平均比率（如 `close / ma20` 正常值約為 `1.0`），補 `0` 代表股價暴跌 100% 的極端異常值，會嚴重扭曲樹模型分裂點與預測分數。

---

## 四、第三階段 C 資料整備路線圖與分階段緩解策略

鑑於上述盤點結果，為了在不造假、不掩蓋資料限制的前提下推進第三階段 C-2（回測引擎實作），我們制定以下資料分級驗證與補強策略：

1. **C-2 階段（回測引擎核心實作與合成資料驗證）**：
   - 使用 **100% 確定性合成資料集（Deterministic Synthetic Datasets）** 與內建之範例 TWSE 歷史交易日曆切片，完整實作並驗證 `PHASE3C_DESIGN.md` 中的 12 大元件與 17 項邊界測試案例（BT-01 至 BT-17）。
   - 在 `BacktestReport` 中強制標記資料等級：當使用合成資料或未含完整下市/除權息之資料集時，報告自動標註 `data_readiness_grade = "SYNTHETIC_TEST_ONLY"` 或 `"PARTIAL_HISTORICAL_WITH_LIMITATIONS"`，從結構上杜絕將測試結果誤認為真實市場實盤驗證。
2. **正式歷史資料接入前必須完成之資料管線補強清單（Data Pipeline Remediation Checklist）**：
   - [ ] **D-1（TWSE 交易日曆）**：匯入證交所官方歷年開休市日期表（含未來 1 年已公告休市日），取代 `0050.TW` `yfinance` 反推法。
   - [ ] **D-2（無倖存者偏差股票池）**：建立包含歷史上下市日期（`listed_date`, `delisted_date`）與每日歷史真實市值的 Point-in-Time 股票池表。
   - [ ] **D-3（除權息與公司行動表）**：接入證交所「除權除息計算結果表（`TWT49U`）」與 FinMind `TaiwanStockDividendResult` / `TaiwanStockDividend`（含現金股利、股票股利、除息日、發放日、除權息參考價）。
   - [ ] **D-4（月營收與財報真實公告日）**：保留公開資訊觀測站實際公告日（或對齊法定截止日遇假日順延後之盤後時間 `16:30+08:00`）。
   - [ ] **D-5（基準指數）**：接入台灣加權股價指數（`^TWII`）、加權股價報酬指數（`IR0001`）與元大台灣 50（`0050.TW`）之原始與含息序列。
