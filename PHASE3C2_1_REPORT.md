# 第三階段 C-2.1：回測基礎資料層實作與驗收報告（PHASE3C2_1_REPORT.md）

> **文件狀態**：第三階段 C-2.1 正式驗收報告  
> **完成時間**：2026-10-21  
> **實作範圍**：`src/tw_quant/backtest/` 基礎資料層（`exceptions.py`、`calendar.py`、`data_provider.py`、`__init__.py`）與專屬測試套件 `tests/test_backtest_data_layer.py`  
> **邊界聲明**：本階段僅實作回測基礎資料層與 Point-in-Time (PIT) 防護機制，尚未實作 `BacktestLotLedger`、`CorporateActionProcessor`、`HistoricalExecutionSimulator`、`PortfolioValuator`、`WalkForwardRunner` 與 `PerformanceAnalyzer`。所有通過之測試均使用標記為 `is_synthetic=True` 之合成測試資料，不代表真實台股歷史回測績效。

---

## 1. 實際完成項目

1. **實作前實際程式碼與介面核對**：
   - 實地檢查 `src/tw_quant/strategy/`，確認實際交易日曆協定檔案為 [`src/tw_quant/strategy/trading_calendar.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/trading_calendar.py)（含 `TradingCalendar` Protocol 與 `CalendarError`），而非設計草稿中簡寫之 `calendar.py`；並確認 [`src/tw_quant/broker/engine.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/engine.py#L22-L37) 之 `MarketBar` 欄位為 `yesterday_close`。
   - 實測發現 Windows Python 3.13 標準環境在未安裝第三方 `tzdata` 套件時，直接呼叫 `zoneinfo.ZoneInfo("Asia/Taipei")` 會拋出 `ZoneInfoNotFoundError`；因此於 `calendar.py` 實作安全回退機制（優先載入 `ZoneInfo("Asia/Taipei")`，無 IANA 資料庫時回退使用具名固定偏移時區 `dt.timezone(dt.timedelta(hours=8), name="Asia/Taipei")`），確保 100% 純標準庫零外部依賴運作。
2. **完成 [`src/tw_quant/backtest/exceptions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/exceptions.py)**：
   - 建立結構化回測例外階層，涵蓋資料缺漏、交易日曆異常、PIT 時序違規、未來資訊洩漏、不支援格式/非法 OHLCV 數值及資料血統（來源與版本）缺失，並保留完整診斷屬性（`error_code`、`context`、`symbol`、`trade_date`、`cutoff_time`、`available_at` 等）。
3. **完成 [`src/tw_quant/backtest/calendar.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/calendar.py)**：
   - 建立綁定台灣時區 `Asia/Taipei` 的 `TradingCalendar`，明確區分 `TRADING_DAY`、`NON_TRADING_DAY` 與 `UNKNOWN`，相容於策略層 `TradingCalendar` Protocol，並攔截 `0050.TW` K 棒代理冒充官方日曆、未就緒日曆與未知日期猜測。
4. **完成 [`src/tw_quant/backtest/data_provider.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/data_provider.py)**：
   - 建立 `HistoricalDataProvider` Protocol、`InMemoryHistoricalDataProvider`、`HistoricalBarRecord` 與 `DataProvenance`，落實事件時間（`event_time`）、發布時間（`published_at`）與可用時間（`available_at`）三階段驗證、OHLCV 數值與時間序列升冪檢查、盤後資料禁止回填盤中，以及阻止 T 日收盤資訊於 T 日收盤成交。
5. **完成 [`tests/test_backtest_data_layer.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_data_layer.py)**：
   - 新增 14 個測試方法（含 25 個 subtests），完整覆蓋全部 12 項必測場景與跨模組相容性驗證。

---

## 2. 新增及修改檔案

### 新增檔案（5 個）
- [`src/tw_quant/backtest/__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py)：回測基礎資料層公開匯出介面。
- [`src/tw_quant/backtest/exceptions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/exceptions.py)：回測例外階層與結構化診斷資訊。
- [`src/tw_quant/backtest/calendar.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/calendar.py)：台灣時區 `Asia/Taipei` 交易日曆 `TradingCalendar` 與 `CalendarMetadata`。
- [`src/tw_quant/backtest/data_provider.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/data_provider.py)：`HistoricalDataProvider`、`InMemoryHistoricalDataProvider`、`HistoricalBarRecord` 與 `DataProvenance`。
- [`tests/test_backtest_data_layer.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_data_layer.py)：第三階段 C-2.1 基礎資料層 14 項單元與驗收測試。
- [`PHASE3C2_1_REPORT.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PHASE3C2_1_REPORT.md)：本驗收報告。

### 修改檔案（1 個）
- [`PROJECT_MAP.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PROJECT_MAP.md)：更新第三階段 C-2.1 完成狀態、修正策略模組實際檔名索引，並如實標記已完成與尚未完成之回測元件。

---

## 3. 介面與資料結構

### 3.1 例外階層（`exceptions.py`）
- `BacktestError(Exception)`：回測基底例外，內含 `error_code: str` 與 `context: Dict[str, Any]`。
  - `BacktestCalendarError(BacktestError, CalendarError)`：交易日曆錯誤（同時繼承策略層 `CalendarError`）。
    - `CalendarNotReadyError`：交易日曆未載入或未就緒。
    - `UnknownTradingDateError`（別名 `CalendarBoundaryError`）：查詢日期超出涵蓋範圍或落入未驗證之 `UNKNOWN` 缺漏區間。
  - `BacktestTemporalError(BacktestError)`：時序與因果錯誤。
    - `PITViolationError`：時間戳違反 PIT 規則（缺少 `published_at`/`available_at`、發布早於事件、盤後資料回填盤中、無時區）。
    - `LookaheadBiasError`（別名 `LookaheadViolationError`、`FutureDataLeakageError`）：未來資料洩漏（`available_at > cutoff_time`、查詢未來日期、試圖以 T 日盤後收盤資訊在 T 日收盤成交）。
  - `BacktestDataError(BacktestError)`：歷史資料錯誤。
    - `HistoricalDataMissingError`（別名 `IncompleteHistoricalDataError`）：指定股票/日期行情不存在或必要欄位缺漏。
    - `InvalidOHLCVError`（別名 `DataIntegrityError`）：OHLC 高低關係矛盾、`NaN`/`Infinity`、非正數價格、非法成交量、時間序列亂序或重複日期。
    - `UnsupportedDataFormatError`：不支援的資料頻率或前復權價格（`FORWARD_ADJUSTED`）。
    - `DataProvenanceError`：資料來源（`source`）、版本（`dataset_version`）、擷取時間（`retrieved_at`）缺漏，或合成／真實資料標記混淆。

### 3.2 交易日曆（`calendar.py`）
- `TradingDayStatus(str, Enum)`：`TRADING_DAY`、`NON_TRADING_DAY`、`UNKNOWN`。
- `CalendarMetadata(frozen=True)`：`source`、`version`、`is_synthetic`、`retrieved_at`、`description`。
- `TradingCalendar`：
  - `get_day_status(day, allow_datetime=False) -> TradingDayStatus`
  - `is_trading_day(day, allow_datetime=False) -> bool`
  - `previous_trading_day(day, allow_datetime=False) -> dt.date`（別名 `prev_trading_day`）
  - `next_trading_day(day, allow_datetime=False) -> dt.date`
  - `trading_days_between(start, end) -> int`
  - `get_trading_days_in_range(start, end) -> Tuple[dt.date, ...]`
  - `shift_trading_days(day, offset) -> dt.date`
  - `assert_ready(allow_synthetic=True) -> None`

### 3.3 歷史資料供應器（`data_provider.py`）
- `DataProvenance(frozen=True)`：`source`、`dataset_version`、`retrieved_at`、`is_synthetic`、`description`。
- `HistoricalBarRecord(frozen=True)`：
  - 行情欄位：`symbol`、`trade_date`、`open`、`high`、`low`、`close`、`volume`、`yesterday_close`、`frequency`、`price_basis`、`is_halted`、`is_disposition`、`is_limit_up_locked`、`is_limit_down_locked`。
  - 時序與血統欄位：`event_time`、`published_at`、`available_at`、`provenance`。
  - 驗證屬性與方法：`pit_status`、`is_pit_verified`、`allows_same_day_close_execution`（固定為 `False`）、`assert_pit_verified()`、`assert_available_as_of(cutoff_time)`、`assert_not_same_day_execution(proposed_execution_time)`、`to_market_bar()`。
- `HistoricalDataProvider(Protocol)` & `InMemoryHistoricalDataProvider`：
  - `add_bar(bar)` / `add_bars(bars)`
  - `get_bar(symbol, trade_date, *, cutoff_time, require_pit_verified=True, require_real_data=False)`
  - `get_available_bars(symbol, *, cutoff_time, start_date=None, end_date=None, require_pit_verified=True, require_real_data=False)`
  - `get_latest_available_bar(symbol, *, cutoff_time, require_pit_verified=True, require_real_data=False)`
  - `get_cross_section_bars(symbols, trade_date, *, cutoff_time, require_pit_verified=True, require_real_data=False)`
  - `validate_execution_timing_for_bar(bar, proposed_execution_time)`

---

## 4. PIT 與交易日曆防護機制

1. **三階段時間戳嚴格分離與驗證**：
   - 強制區分 `event_time`（市場收盤事件完成時間，$\ge T\text{ 13:30+08:00}$）、`published_at`（資料發布時間）與 `available_at`（策略可讀取時間），且強制要求 `event_time <= published_at <= available_at`。
2. **缺失發布／可用時間絕不自動推定為可用**：
   - 當 `published_at is None` 或 `available_at is None` 時，系統**絕不**自動將其補為 `event_time`，而是標記 `pit_status = PITVerificationStatus.UNVERIFIED_MISSING_TIMESTAMPS`，並在預設 `require_pit_verified=True` 下拋出 `PITViolationError` 拒絕進入正式 PIT 回測。
3. **禁止盤後資料回填盤中與禁止同日收盤成交**：
   - 日線行情若標記 `event_time < T 13:30`、`published_at < T 13:30` 或 `available_at < T 13:30`，建立時即拋出 `PITViolationError`。
   - 若策略基於 $T$ 日收盤行情（盤後取得）試圖於 $T$ 日盤中或 $T$ 日 `13:30` 收盤執行成交，`validate_execution_timing_for_bar()` / `assert_not_same_day_execution()` 立即拋出 `LookaheadBiasError`。
4. **交易日曆未知日期與代理資料攔截**：
   - 超出 `[coverage_start, coverage_end]`、日曆未就緒（`uninitialized()`）或內部缺漏日期（`unknown_dates` / `strict_explicit_schedule` 下未宣告之日期）一律視為 `UNKNOWN`，呼叫 `is_trading_day`、`next_trading_day`、`previous_trading_day` 或 `trading_days_between` 時立即拋出 `UnknownTradingDateError` / `CalendarNotReadyError`，絕不猜測或退回週末規則。
   - 若嘗試將包含 `0050`、`YFINANCE`、`KBAR`、`SYNTHETIC`、`MOCK`、`TEST` 的日曆或資料標記為真實官方資料（`is_synthetic=False`），立即拋出 `DataProvenanceError`。

---

## 5. 測試命令、實際結果及未通過項目

### 5.1 新增之基礎資料層專屬測試執行結果
- **測試命令**：
  ```powershell
  python -m unittest tests/test_backtest_data_layer.py -v
  ```
- **實際結果**：
  - 執行測試數：**14 個測試方法（含 25 個 subtests）**
  - 通過數：**14 通過**
  - 失敗數：**0 失敗、0 錯誤、0 跳過**
  - 執行耗時：`0.111s`

### 5.2 全系統回歸測試執行結果
- **測試命令**：
  ```powershell
  python -m unittest discover -s tests -p "test_*.py"
  ```
- **實際結果**：
  - 執行測試總數：**170 個測試方法（含 75 個 subtests）**
    - `test_signal.py`：19 項通過
    - `test_paper_broker.py`：26 項通過
    - `test_risk_engine.py`：22 項通過
    - `test_integration.py`：23 項通過
    - `test_prediction.py`：14 項通過
    - `test_strategy_engine.py`：52 項通過
    - `test_backtest_data_layer.py`：14 項通過
  - 失敗數：**0 失敗、0 錯誤、0 跳過**
  - 執行耗時：`0.125s`
- **未通過項目**：**無（0 項）**。

---

## 6. 原始專案完整性驗證

透過 Git 命令與自動化測試 `test_12_subprojects_unchanged_and_pure_stdlib_imports` 雙重驗證：

1. **`anti-gambling-trader-tw`**：
   - `HEAD` Commit：`8609cef73718a056fddb74201fc304f174fa1553`（與基準完全一致）
   - `git status --porcelain`：空輸出（Working tree 100% clean，未受任何修改）
2. **`qlib-tw-trader`**：
   - `HEAD` Commit：`774b3fb60d3b20b5c9ee86d25052ad2ec08e2011`（與基準完全一致）
   - `git status --porcelain`：空輸出（Working tree 100% clean，未受任何修改）
3. **純標準庫依賴驗證**：
   - `src/tw_quant/backtest/` 僅使用 Python 標準庫（`datetime`, `dataclasses`, `decimal`, `enum`, `math`, `re`, `typing`, `zoneinfo`），未引入 `qlib`、`sqlalchemy`、`lightgbm`、`yfinance` 等任何外部套件。

---

## 7. 尚未解決的風險

1. **本地尚無真實台股歷史行情與官方完整交易日曆資料檔**：
   - 如 `BACKTEST_DATA_AUDIT.md` 所述，本地磁碟尚未匯入經審核之 TWSE 歷年休市日表與無倖存者偏差之真實歷史 OHLCV 資料；目前 `TradingCalendar` 與 `InMemoryHistoricalDataProvider` 在未注入真實資料前，僅能透過 `is_synthetic=True` 執行合成資料驗證。
2. **持倉批次（Lot）與除權息帳務尚未實作**：
   - 問題 A-1（`BacktestLotLedger` FIFO 持倉批次與 `entry_date` 追蹤）與問題 A-2（`CorporateActionProcessor` 現金除息應收股利與除權配股成本調整）按計畫留待下一階段 C-2.2 實作。
3. **歷史日線撮合模擬與 Walk-Forward 執行器尚未實作**：
   - 本階段僅在 `HistoricalBarRecord` 中提供阻止同日收盤成交之因果檢查（`assert_not_same_day_execution`），實際將 $T$ 日盤後訊號排入 $T+1$ 開盤撮合的 `HistoricalExecutionSimulator` 與 `WalkForwardRunner` 留待後續步驟實作。

---

## 8. 下一步 C-2.2 的實作建議

建議在 **第三階段 C-2.2** 聚焦實作 **「持倉批次帳本（`BacktestLotLedger`）與除權息公司行動處理器（`CorporateActionProcessor`）」**（對應 `PHASE3C_DESIGN.md` 第四節與第五節，徹底解決問題 A-1 與 A-2）：

1. **`src/tw_quant/backtest/lot_ledger.py`**：
   - 實作 `PositionLot`、`ClosedLotTrade`、`DividendReceivable` 與 `BacktestLotLedger`。
   - 實作 FIFO 分批買進、部分賣出、部分成交（Partial Fill）之 `entry_date` 推進邏輯，以及未成交委託不建立批次的保護。
   - 實作 `get_oldest_entry_dates()` 無縫供給 `PortfolioSnapshot.from_broker(entry_dates=...)`。
2. **`src/tw_quant/backtest/corporate_actions.py`**：
   - 實作 `CorporateActionEvent` 與 `CorporateActionProcessor`。
   - 實作現金除息日（建立 `DividendReceivable` 維持除息日 NAV 連續不誤觸風控）、現金股利發放日（轉入可用現金且不重複計算）、以及股票除權配股/分割減資之股數與單位成本調整。
3. **專屬測試 `tests/test_backtest_lot_and_corporate_actions.py`**：
   - 涵蓋 `PHASE3C_DESIGN.md` 中的測試案例 BT-03 至 BT-10。
