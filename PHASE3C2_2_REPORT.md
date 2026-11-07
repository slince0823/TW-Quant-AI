# 第三階段 C-2.2：FIFO 持倉批次與除權息會計實作報告 (`PHASE3C2_2_REPORT.md`)

> **階段狀態**：已完成（Phase 3C-2.2 驗收通過）
> **執行日期**：2026-10-21
> **測試總數**：188 項全數通過（含本次新增 18 項 FIFO 持倉批次與除權息會計單元測試）
> **實作邊界**：僅實作 `src/tw_quant/backtest/lot_ledger.py`、`src/tw_quant/backtest/corporate_actions.py`、`src/tw_quant/backtest/exceptions.py`（擴充會計與公司行動例外）、`src/tw_quant/backtest/__init__.py`、`tests/test_backtest_accounting.py` 與 `PROJECT_MAP.md` 更新。未修改任何既有核心模組，亦未實作 `HistoricalExecutionSimulator`、`PortfolioValuator`、`WalkForwardRunner`、`PerformanceAnalyzer` 或完整回測主迴圈。

---

## 一、新增與修改的檔案清單

| 檔案路徑 | 狀態 | 說明 |
| :--- | :---: | :--- |
| `src/tw_quant/backtest/lot_ledger.py` | **新增** | 實作 `LotFillEvent`、`PositionLot`、`ClosedLotTrade` 與 `BacktestLotLedger`（FIFO 持倉批次分類帳、逐批成本與已實現損益精確分攤、對接 `StrategyEngine` 之 `open_positions` 與 `entry_dates` 匯出、與 `PortfolioLedger` 交叉對帳檢查） |
| `src/tw_quant/backtest/corporate_actions.py` | **新增** | 實作 `CorporateActionType`、`ReceivableStatus`、`CorporateActionEvent`、`DividendReceivable`、`AccountNAVBreakdown` 與 `CorporateActionProcessor`（台股現金股利 T-1 持倉基準、除息日應收股利入帳、發放日現金入帳、NAV 拆解與防偽檢核） |
| `src/tw_quant/backtest/exceptions.py` | **修改** | 新增 `BacktestAccountingError`、`DuplicateFillError`、`OversellPositionError`、`InvalidFillRecordError`、`CorporateActionError`、`DuplicateCorporateActionError`、`CorporateActionOrderError`、`UnsupportedCorporateActionError` |
| `src/tw_quant/backtest/__init__.py` | **修改** | 匯出 C-2.2 新增之批次分類帳、公司行動處理器與對應例外類別 |
| `tests/test_backtest_accounting.py` | **新增** | 新增 18 項單元測試（涵蓋題目指定之 17 項必測情境與除息前後 NAV 連續性驗證） |
| `PROJECT_MAP.md` | **修改** | 更新專案進度、模組地圖與測試統計（188 tests） |
| `PHASE3C2_2_REPORT.md` | **新增** | 本階段實作與驗收報告 |

---

## 二、設計文件與實際程式碼的差異說明

在實作前比對 `PHASE3C_DESIGN.md`、`BACKTEST_DATA_AUDIT.md`、`BACKTEST_METHODOLOGY.md` 與既有核心程式碼（`PortfolioLedger`、`TaiwanPaperBroker`、`FillRecord`、`PortfolioSnapshot`、`RiskEngine`），確認並處理了以下介面細節與規格精煉：

1. **`FillRecord` 結構與交易方向 / 策略 ID 傳遞方式**
   - **實際程式碼檢視**：`src/tw_quant/broker/order.py` 中的 `FillRecord` 欄位為 `fill_id`、`order_id`、`signal_id`、`symbol`、`fill_quantity`、`fill_price`、`notional`、`commission`、`tax`、`slippage`、`timestamp`（`datetime`），並未直接包含 `action`（`BUY` / `SELL` / `CLOSE`）或 `strategy_id`（這兩個欄位位於 `PaperOrder` 與 `UnifiedTradeSignal`）。
   - **對齊設計**：為遵守「不修改既有 `order.py` 與 `PortfolioLedger`」的限制，`BacktestLotLedger` 同時支援：
     - `record_buy_fill(fill, strategy_id=..., trade_date=...)`
     - `record_sell_fill(fill, strategy_id=..., trade_date=...)`
     - `record_order_fill(fill, order, strategy_id=..., trade_date=...)`（直接讀取 `PaperOrder.action`）
     - `record_fill(fill, action=..., strategy_id=..., trade_date=...)`（支援 `FillRecord` 或包裝後的 `LotFillEvent`）
   - 進場日期預設由 `fill.timestamp.date()` 自動推導，若傳入 `trade_date` 則嚴格驗證須與 `fill.timestamp.date()` 一致，否則拋出 `InvalidFillRecordError`。

2. **真實歷史資料缺少 `payable_date` 的嚴格阻斷規則（精煉 `PHASE3C_DESIGN.md` 第 5.5 節草案）**
   - **差異說明**：`PHASE3C_DESIGN.md` 第 5.5 節 Rule C-2 原先曾將「若歷史資料源缺少發放日，是否允許以 `ex_date + 28 個交易日` 作為明確標記之近似值」列為待確認事項。
   - **C-2.2 明確規範落地**：依本階段任務規範，**真實歷史資料（`is_synthetic=False`）若缺少 `payable_date`，在驗證與建構階段即直接拋出 `HistoricalDataMissingError`，絕不私自預設為 28 個交易日後或假裝為已知發放日**。
   - **合成測試資料防偽**：只有在 `is_synthetic=True` 的測試情境中，若使用模擬發放日，才允許顯式指定 `payable_date` 並標記 `is_synthetic_payable_date=True`；若 `is_synthetic=False` 卻標記 `is_synthetic_payable_date=True`，立即拋出 `DataProvenanceError`。

3. **與 `PortfolioLedger` 的職責分工與零侵入對帳**
   - 既有 `src/tw_quant/broker/ledger.py` 的 `PortfolioLedger` 使用移動平均成本法（Moving Average Cost），且 `Position` 僅記錄單一彙總部位而無分批 `entry_date`（審查問題 A-1）。
   - `BacktestLotLedger` 作為回測專用之獨立 FIFO 批次帳本，可在不改動 `PortfolioLedger` 一行程式碼的前提下，提供精確的批次成本、持有交易日數與 `StrategyEngine` 所需之 `(open_positions, entry_dates)`，並透過 `reconcile_with_portfolio_ledger()` 驗證兩者在每檔股票總股數、總成本基礎與全平倉後累積已實現損益達到 `0.01` 元精確一致。

---

## 三、FIFO 批次會計規則與實作細節 (`src/tw_quant/backtest/lot_ledger.py`)

### 1. 資料結構
- **`PositionLot`**：記錄單一進場批次之 `lot_id`、`symbol`、`entry_date`、`original_quantity`、`remaining_quantity`、`entry_price`、`entry_notional`、`entry_commission`、`initial_total_cost`（`entry_notional + entry_commission`）、`remaining_cost_basis`、`remaining_entry_commission`、`source_fill_id`、`source_order_id`、`source_signal_id`、`strategy_id`。
- **`ClosedLotTrade`**：記錄每次賣出配對單一 FIFO 批次後的已平倉明細，包含 `closed_trade_id`、`lot_id`、`symbol`、`entry_date`、`exit_date`、`quantity`、`entry_price`、`exit_price`、`gross_proceeds`、`entry_cost_basis`、`entry_commission_allocated`、`exit_commission_allocated`、`exit_tax_allocated`、`net_proceeds`、`realized_pnl`、`holding_calendar_days`、`holding_trading_days`（整合 `TaiwanTradingCalendar.Session_count_between`）與來源買賣 `fill_id`。

### 2. 核心會計與分攤演算法
- **進場建批**：僅接受實際買進成交記錄（`fill_quantity > 0`），總成本基礎含買進手續費：
  $$\text{InitialTotalCost} = \text{notional} + \text{commission}$$
- **FIFO 扣減與部分賣出比例分攤**：
  - 同一股票存在多個批次時，嚴格依進場順序（FIFO）由最早批次開始扣減。
  - 當單一批次被部分賣出 $q_{\text{take}} < q_{\text{rem}}$ 時，按比例分攤該批次剩餘成本基礎與買進手續費（四捨五入至 `0.01`）：
    $$\text{CostAllocated} = \text{round}\left(\text{remaining\_cost\_basis} \times \frac{q_{\text{take}}}{q_{\text{rem}}},\ 0.01\right)$$
  - 當該批次剩餘股數被完全出清（$q_{\text{take}} == q_{\text{rem}}$）時，直接吸收該批次所有剩餘成本基礎（`lot.remaining_cost_basis`），確保單一批次分次賣出後的成本總和 **100% 等於原始進場總成本，無任何四捨五入殘差**。
  - 當單筆賣出成交跨越 $k$ 個 FIFO 批次時，賣出價金（`notional`）、賣出手續費（`commission`）與證交稅（`tax`）前 $k-1$ 個配對批次按股數比例四捨五入分攤，第 $k$ 個（最後一個）配對批次吸收餘額，確保單筆賣出成交產生之所有 `ClosedLotTrade` 的價金與稅費總和完全等於 `FillRecord` 原始金額。
- **出場規則對接（解決問題 A-1）**：
  - `get_earliest_entry_date(symbol)` 回傳該股票尚有剩餘股數之最早 FIFO 批次 `entry_date`。
  - `export_for_strategy_engine()` 直接回傳 `(open_positions, entry_dates)`，無縫供給 `StrategyEngine.generate_signals(..., open_positions=..., entry_dates=...)` 進行 `max_holding_days` 強制出場判斷。
- **防呆與不變量驗證**：
  - 相同 `fill_id` 重複記帳 $\rightarrow$ 拋出 `DuplicateFillError`。
  - 賣出股數超過目前持倉總股數 $\rightarrow$ 拋出 `OversellPositionError`（且原批次狀態完全不被部分污染）。
  - 零股數、負股數、負價格或 `notional != round(fill_price * fill_quantity, 2)` $\rightarrow$ 拋出 `InvalidFillRecordError`。

---

## 四、現金股利與應收股利會計規則與實作細節 (`src/tw_quant/backtest/corporate_actions.py`)

### 1. 台股除息日（T）與發放日（`payable_date`）兩階段帳務
1. **參與除息資格判定（T-1 收盤持倉基準）**：
   - 在除息日 $T$ 開盤前（或當日買賣成交發生前）呼叫 `process_ex_date(ex_date, lot_ledger)`。
   - 僅計入 `lot.entry_date < ex_date` 且 `lot.remaining_quantity > 0` 的持倉批次；若批次為除息日當日（`entry_date == ex_date`）或之後進場，嚴格排除於參與除息股數之外。
   - 若呼叫端在已寫入除息日當日（或之後）的成交記錄後才呼叫 `process_ex_date()`，`CorporateActionProcessor` 會偵測到帳本存在 `fill_date >= ex_date` 的記錄並立即拋出 `CorporateActionOrderError`，防止當日買賣污染 T-1 除息基準股數。
2. **除息日（T）建立應收股利（`DividendReceivable`）**：
   - 當參與除息股數 $Q_{\text{eligible}} > 0$ 時：
     $$\text{GrossDividend} = \text{round}(Q_{\text{eligible}} \times \text{cash\_dividend\_per\_share},\ 0.01)$$
   - 建立狀態為 `PENDING` 的 `DividendReceivable`，計入 `pending_dividend_receivable`（應收股利），此時 **不增加可用現金（`available_cash`）**，防止策略在股利實際發放前拿應收股利買股。
3. **發放日（`payable_date`）應收轉現金**：
   - 在 `current_date >= payable_date` 時呼叫 `settle_payable_dividends(current_date, cash_credit_callback=...)`。
   - 將到期之 `PENDING` 應收股利轉為 `PAID`，自 `pending_dividend_receivable` 扣除並轉入 `collected_cash_dividends`，同時可透過 callback 同步增加現金帳戶餘額。

### 2. 帳戶總淨值（NAV）拆解與避免誤觸風控熔斷（解決問題 A-2）
- `compute_nav_breakdown(as_of_date, available_cash, market_value, reserved_cash=0.0)` 回傳 `AccountNAVBreakdown`：
  $$\text{TotalNAV} = \text{AvailableCash} + \text{MarketValue} + \text{PendingDividendReceivable}$$
  $$\text{SpendableCash} = \text{AvailableCash} - \text{ReservedCash}$$
- **消除假性淨值暴跌**：在除息日 $T$，雖然股票參考價下調導致 `MarketValue` 減少 $\Delta V = Q \times D$，但同額的 $\Delta V$ 立即進入 `PendingDividendReceivable`，因此 $\text{TotalNAV}$ 在除息日前後保持連續（跳空為 $0.00$），不會因除息蒸發市值而誤觸 `RiskEngine` 的單日虧損限制或最大回撤熔斷；而在 $T$ 至 `payable_date` 期間，$\text{SpendableCash}$ 維持不變，不會超額下單。
- **不支援之股數變動事件明確阻斷**：
  - 對於 `STOCK_DIVIDEND`（股票股利）、`STOCK_SPLIT`（拆股）、`REVERSE_SPLIT`（併股）、`CAPITAL_REDUCTION`（減資），`register_event()` 與 `CorporateActionEvent` 驗證會直接拋出 `UnsupportedCorporateActionError`，絕不暗中忽略或只改股數不調成本。

---

## 五、測試執行指令與完整輸出

### 1. 本階段專屬測試：`python -m unittest tests/test_backtest_accounting.py -v`

涵蓋全部 18 項測試案例（對應任務要求之 17 項必測情境 + 除息日 NAV 連續性與可動用現金隔離驗證）：

```text
test_01_single_buy_single_full_sell (tests.test_backtest_accounting.TestBacktestAccounting.test_01_single_buy_single_full_sell)
1. 單次買進、單次全部賣出。 ... ok
test_02_multiple_buys_single_partial_sell_fifo (tests.test_backtest_accounting.TestBacktestAccounting.test_02_multiple_buys_single_partial_sell_fifo)
2. 多次分批買進、單次部分賣出，驗證 FIFO 順序。 ... ok
test_03_multiple_buys_multiple_partial_sells_across_lots (tests.test_backtest_accounting.TestBacktestAccounting.test_03_multiple_buys_multiple_partial_sells_across_lots)
3. 多次分批買進、多次部分賣出，跨批次扣減。 ... ok
test_04_earliest_entry_date_updates_after_oldest_lot_exhausted (tests.test_backtest_accounting.TestBacktestAccounting.test_04_earliest_entry_date_updates_after_oldest_lot_exhausted)
4. 最早批次賣光後，earliest_entry_date 正確更新為下一批進場日。 ... ok
test_05_commission_and_tax_exact_allocation_and_reconciliation (tests.test_backtest_accounting.TestBacktestAccounting.test_05_commission_and_tax_exact_allocation_and_reconciliation)
5. 買賣含手續費與證交稅時，已實現損益計算正確，且與 PortfolioLedger 對帳一致。 ... ok
test_06_oversell_raises_exception (tests.test_backtest_accounting.TestBacktestAccounting.test_06_oversell_raises_exception)
6. 超賣持倉必須拋出明確例外 (OversellPositionError)。 ... ok
test_07_duplicate_fill_id_raises_exception (tests.test_backtest_accounting.TestBacktestAccounting.test_07_duplicate_fill_id_raises_exception)
7. 重複 fill_id 必須拋出明確例外 (DuplicateFillError)。 ... ok
test_08_zero_or_negative_quantity_or_invalid_price_raises_exception (tests.test_backtest_accounting.TestBacktestAccounting.test_08_zero_or_negative_quantity_or_invalid_price_raises_exception)
8. 零股數或負股數/異常價格成交必須拋出明確例外 (InvalidFillRecordError)。 ... ok
test_09_ex_dividend_held_before_ex_date_creates_receivable_not_cash (tests.test_backtest_accounting.TestBacktestAccounting.test_09_ex_dividend_held_before_ex_date_creates_receivable_not_cash)
9. 除息日前一日持有部位，在除息日正確產生應收股利但現金不立即增加。 ... ok
test_10_buy_on_ex_date_does_not_receive_dividend (tests.test_backtest_accounting.TestBacktestAccounting.test_10_buy_on_ex_date_does_not_receive_dividend)
10. 除息日當日新買進部位，不得領取該次股利；且若先記錄當日成交才執行除息會拋出順序錯誤。 ... ok
test_11_sell_before_ex_date_does_not_receive_dividend (tests.test_backtest_accounting.TestBacktestAccounting.test_11_sell_before_ex_date_does_not_receive_dividend)
11. 除息日前已賣出部位，不得領取該次股利。 ... ok
test_12_sell_on_or_after_ex_date_still_keeps_receivable (tests.test_backtest_accounting.TestBacktestAccounting.test_12_sell_on_or_after_ex_date_still_keeps_receivable)
12. 除息日當日或之後賣出原持有部位，仍保留應收股利。 ... ok
test_13_payable_date_converts_receivable_to_cash_without_nav_double_count (tests.test_backtest_accounting.TestBacktestAccounting.test_13_payable_date_converts_receivable_to_cash_without_nav_double_count)
13. 發放日應收股利正確轉入可用現金，且應收股利歸零，不重複計入 NAV。 ... ok
test_14_duplicate_corporate_action_event_raises_exception (tests.test_backtest_accounting.TestBacktestAccounting.test_14_duplicate_corporate_action_event_raises_exception)
14. 同一除息事件重複套用必須拋出明確例外 (DuplicateCorporateActionError)。 ... ok
test_15_missing_payable_date_in_real_data_raises_exception (tests.test_backtest_accounting.TestBacktestAccounting.test_15_missing_payable_date_in_real_data_raises_exception)
15. 真實資料模式缺少 payable_date 必須拋出明確例外 (HistoricalDataMissingError)。 ... ok
test_16_synthetic_payable_date_provenance_validation (tests.test_backtest_accounting.TestBacktestAccounting.test_16_synthetic_payable_date_provenance_validation)
16. 合成測試資料使用模擬發放日時，必須顯式標記 is_synthetic=True 與 is_synthetic_payable_date=True。 ... ok
test_17_unsupported_corporate_actions_raise_exception (tests.test_backtest_accounting.TestBacktestAccounting.test_17_unsupported_corporate_actions_raise_exception)
17. 股票股利/拆併股/減資事件若未實作，必須拋出明確例外 (UnsupportedCorporateActionError)。 ... ok
test_18_ex_dividend_nav_continuity_and_no_false_drawdown (tests.test_backtest_accounting.TestBacktestAccounting.test_18_ex_dividend_nav_continuity_and_no_false_drawdown)
18. 驗證除息日股價下調時，加計應收股利後的 Total NAV 不會產生假性跳空下跌，且不動用未發放股利。 ... ok

----------------------------------------------------------------------
Ran 18 tests in 0.008s

OK
```

### 2. 全系統回歸測試：`python -m unittest discover -s tests -p "test_*.py"`

```text
----------------------------------------------------------------------
Ran 188 tests in 0.212s

OK
```

**各測試檔案通過明細（共 188 項）**：
- `tests/test_signal.py`：19 項通過
- `tests/test_paper_broker.py`：26 項通過
- `tests/test_risk_engine.py`：22 項通過
- `tests/test_integration.py`：23 項通過
- `tests/test_prediction.py`：14 項通過
- `tests/test_strategy_engine.py`：52 項通過
- `tests/test_backtest_data_layer.py`：14 項通過
- `tests/test_backtest_accounting.py`：18 項通過

---

## 六、目前已知限制與 C-2.3 前置條件

1. **股數變動類公司行動尚未實作**：
   - 目前 `CorporateActionProcessor` 完整支援台股最常發生的現金股利（`CASH_DIVIDEND`）；針對股票股利（`STOCK_DIVIDEND`）、拆股（`STOCK_SPLIT`）、併股（`REVERSE_SPLIT`）與減資（`CAPITAL_REDUCTION`），目前採顯式拋出 `UnsupportedCorporateActionError` 阻斷，避免在未同步調整歷史價格基準與批次單位成本的情況下污染帳務。
2. **歷史撮合與漲跌停／流動性約束將於 C-2.3 實作**：
   - `BacktestLotLedger` 與 `CorporateActionProcessor` 負責成交後的批次帳務與除息會計，尚未包含歷史日線撮合器（`HistoricalExecutionSimulator`）之開盤/收盤價決定、$\pm 10\%$ 漲跌停鎖死檢查、停牌檢查與成交量參與率上限。
3. **C-2.3 前置條件已全數具備**：
   - 基礎資料層（`HistoricalMarketDataProvider`、`TaiwanTradingCalendar`、`UniverseMembershipProvider`）與帳務層（`BacktestLotLedger`、`CorporateActionProcessor`）皆已完成並通過嚴格單元測試，可直接供下一階段 C-2.3 建立歷史執行模擬器（`HistoricalExecutionSimulator`）與投資組合估值器（`PortfolioValuator`）。

---

## 七、兩個原始子專案的 Git Status 驗證結果

執行 Git 檢查確認兩個原始開源專案 Commit 雜湊與工作目錄完全乾淨（Clean），未受任何修改：

- **`anti-gambling-trader-tw`**：
  - `HEAD` Commit：`8609cef73718a056fddb74201fc304f174fa1553`
  - `git status --short`：（空輸出，100% Clean）
- **`qlib-tw-trader`**：
  - `HEAD` Commit：`774b3fb60d3b20b5c9ee86d25052ad2ec08e2011`
  - `git status --short`：（空輸出，100% Clean）
