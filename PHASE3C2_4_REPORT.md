# 第三階段 C-2.4：策略／風控橋接與事件驅動 Walk-Forward 主迴圈實作與驗收報告 (`PHASE3C2_4_REPORT.md`)

**報告日期**：2026-10-21  
**階段定位**：第三階段 C-2.4（`PredictionFeed` 歷史 AI 預測流供應器、`StrategyEngineAdapter` / `RiskEngineAdapter` 橋接器、`WalkForwardRunner` 事件驅動 12 步因果時序主迴圈與整合測試）  
**驗證性質聲明**：**本階段所有測試均運行於顯式標記 `is_synthetic=True` 之合成測試行情、合成交易日曆與合成預測資料上，僅用於驗證事件驅動回測主迴圈、因果時序、策略／風控橋接與跨日帳務守恆邏輯，絕不代表真實台股歷史回測已完成或真實策略績效已獲驗證。**

---

## 一、新增與修改檔案清單

| 檔案路徑 | 變更類型 | 用途與核心職責說明 |
| :--- | :--- | :--- |
| [`src/tw_quant/backtest/exceptions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/exceptions.py) | 擴充 | 新增 C-2.4 專屬例外：`PredictionFeedError`、`PredictionBatchUnavailableError`、`DuplicatePredictionConsumptionError`、`PredictionProvenanceError`、`EmbargoViolationError`、`BacktestRunnerError`、`DuplicateRunnerExecutionError`。 |
| [`src/tw_quant/backtest/prediction_feed.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/prediction_feed.py) | 新增 | 實作 `PredictionFeed` Protocol、`InMemoryPredictionFeed`、`PredictionFeedEntry`、`WalkForwardWindowSpec`、`UnverifiedPredictionPolicy`、`QuarantinedPredictionRecord`、`PredictionConsumptionRecord`。 |
| [`src/tw_quant/backtest/adapters.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/adapters.py) | 新增 | 實作 `BacktestRiskContext`（繼承 `RiskContext` 並納入 `dividends_receivable` 於風控 NAV 與未實現損益計算）、`StrategyEngineAdapter`、`RiskEngineAdapter`、`StrategyEvaluationSummary`、`SignalDispatchAuditRecord`、`DailySignalDispatchSummary`。 |
| [`src/tw_quant/backtest/runner.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/runner.py) | 新增 | 實作 `WalkForwardRunner`（單日 12 步因果時序與多日 Walk-Forward 迴圈）、`WalkForwardRunConfig`、`DailyBacktestLog`、`BacktestRunResult`、`DailyTimelineStep`、`EXPECTED_DAILY_TIMELINE_SEQUENCE`、`MissingPredictionPolicy`、`ErrorHandlingMode`。 |
| [`src/tw_quant/backtest/simulator.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py) | 最小擴充 | 於 `match_order(...)` 新增預設相容參數 `settle_immediately: bool = True` 並新增 `settle_outcome_fill(...)` 與 `_settled_fill_ids` 單次交割防護集合，使 Step 03（開盤撮合）與 Step 04（帳本／批次帳本交割）可拆分為獨立步驟且完全向後相容。 |
| [`src/tw_quant/backtest/corporate_actions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/corporate_actions.py) | 最小擴充 | 新增 `process_due_ex_dividends(lot_ledger, *, as_of_time)` 批次輔助方法，供 Step 02 自動處理當日到期的已註冊除息事件。 |
| [`src/tw_quant/backtest/__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py) | 更新 | 匯出 C-2.4 所有公開類別、枚舉、例外與常數。 |
| [`tests/test_backtest_runner.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_runner.py) | 新增 | 16 項端到端與單元測試，完整覆蓋 C-2.4 所有時序、防前視、除息風控保護、應收股利現金隔離、漲跌停／部分成交／停牌與帳務守恆情境。 |
| [`PROJECT_MAP.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PROJECT_MAP.md) | 更新 | 更新專案狀態、模組索引與 225 項測試統計。 |
| [`PHASE3C2_4_REPORT.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PHASE3C2_4_REPORT.md) | 新增 | 本階段實作與驗收報告。 |

> **零侵入原則確認**：未修改任何既有非 `backtest` 模組（`src/tw_quant/contracts/`、`src/tw_quant/broker/`、`src/tw_quant/risk/`、`src/tw_quant/strategy/`、`src/tw_quant/prediction/`、`src/tw_quant/gateway.py` 均維持原狀 0 修改）。

---

## 二、現有模組實際 API 核對結果

在實作前已逐一核對既有核心模組之實際簽章、回傳型別與副作用，避免依賴假設：

1. **`PredictionBatch`、`StockPrediction`、`PredictionMetadata`**（[`src/tw_quant/prediction/contract.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/prediction/contract.py)）：
   - `PredictionMetadata` 包含 `model_name`, `model_version`, `feature_version`, `model_type`, `is_mock`, `trained_cutoff_date: Optional[dt.date] = None`。
   - `PredictionBatch` 包含 `batch_id`, `as_of_date`（注意：在 `StrategyEngine._gate()` 中，`batch.as_of_date` 代表 `self.calendar.next_trading_day(cutoff_date)`，即預定進場交易日 $T+1$，而 `data_cutoff_time` 所屬日期為決策日 $T$）、`data_cutoff_time`、`generated_at`、`metadata`、`predictions`、`failed_symbols`、`missing_features`。
2. **`StrategyEngine`、`PortfolioSnapshot`、`PositionSnapshot`**（[`src/tw_quant/strategy/engine.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/engine.py)、[`snapshot.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/snapshot.py)）：
   - `StrategyEngine.evaluate(batch, snapshot, decision_time) -> StrategyResult` 為純函數式評估（除了內部 `_emitted_keys` 去重集合），不直接讀取或修改 `TaiwanPaperBroker`。
   - `PortfolioSnapshot` 透過 `positions: Mapping[str, PositionSnapshot]` 接收每檔持倉之 `entry_date: Optional[dt.date]`，並由 `cash + reserved_cash + market_value` 計算 `snapshot.nav`。
   - `StrategyEngine` 明確區分三種賣出／出場行為：
     - `StrategyAction.CLOSE` + `StrategyReason.HOLDING_PERIOD_EXPIRED`：當 `calendar.trading_days_between(pos.entry_date, planned) >= config.holding_trading_days` 時強制全數平倉。
     - `StrategyAction.CLOSE` + `StrategyReason.NOT_IN_TARGET_PORTFOLIO`：當 `exit_when_not_selected=True` 且持倉不在當日入選之 Top-$K$ 目標組合時全數平倉。
     - `StrategyAction.SELL` + `StrategyReason.OVER_TARGET_TRIM`：當持倉仍在目標組合內，但現值超出 `target_value + tolerance_value` 達至少 1 個交易單位（`share_unit`）時部分減碼賣出。
3. **`TradingGateway`、`RiskEngine`、`RiskContext`、`TaiwanPaperBroker`**（[`src/tw_quant/gateway.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/gateway.py)、[`src/tw_quant/risk/engine.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/risk/engine.py)、[`src/tw_quant/broker/broker.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/broker.py)）：
   - `TradingGateway.submit_signal(signal, ref_price=..., market_prices=..., halted_symbols=..., disposition_symbols=...) -> Tuple[bool, RiskDecision, Optional[PaperOrder]]`。
   - 核准後會呼叫 `risk_engine.commit_approval(...)` 並委由 `broker.submit_signal(...)` 建立 `PaperOrderStatus.ACCEPTED` 之 `PaperOrder` 及執行 `ledger.reserve_buy_funds(...)` / `ledger.reserve_sell_shares(...)`，最後在 `finally` 區塊釋放 `risk_engine.release_committed(...)`。
   - `HistoricalExecutionSimulator.submit_order(...)` 已支援接收 `ACCEPTED` 狀態之 `PaperOrder`，僅同步登記 `_remaining_reserved_cash` 與 `_remaining_reserved_shares` 而不重複扣減預留。

---

## 三、每日 12 步因果時序表 (`EXPECTED_DAILY_TIMELINE_SEQUENCE`)

`WalkForwardRunner.run_single_day(trade_date)` 在每個有效台股交易日 $T$ 嚴格按照以下 12 個步驟順序執行，並在 Step 12 強制斷言 `executed_steps == EXPECTED_DAILY_TIMELINE_SEQUENCE`：

| 步驟序號 | 枚舉常數 (`DailyTimelineStep`) | 台北時間 (`Asia/Taipei`) | 執行內容與因果不變量 |
| :---: | :--- | :---: | :--- |
| **Step 01** | `STEP_01_DIVIDEND_PAYOUT_0800` | `T 08:00:00+08:00` | 處理 `payable_date <= T` 之應收現金股利發放：轉入 `PortfolioLedger.cash`，同時將該筆 `DividendReceivable` 標記為 `PAID`（應收股利餘額同步歸零，絕不重複計入 NAV）。 |
| **Step 02** | `STEP_02_EX_DIVIDEND_REGISTRATION_0800` | `T 08:00:00+08:00` | 依據截至 `T 08:00`（開盤前）已持有之 `BacktestLotLedger` 批次股數（`entry_date < T`），登記 `ex_date == T` 之應收現金股利 `DividendReceivable`（不增加 `available_cash`）。 |
| **Step 03** | `STEP_03_OPEN_MATCHING_0900` | `T 09:00:00+08:00` | 僅針對 `T-1` 盤後排入之待執行委託佇列，於 `T 09:00` 開盤使用 `HistoricalExecutionSimulator.match_order(..., settle_immediately=False)` 執行保守撮合（套用漲跌停、Tick Size、停牌、處置股、5% 量能上限與單一滑價／稅費計算）。 |
| **Step 04** | `STEP_04_FILL_SETTLEMENT_0900` | `T 09:00:00+08:00` | 透過 `simulator.settle_outcome_fill(...)` 將 Step 03 產生之 `FillRecord` 單次同步寫入 `PortfolioLedger` 與 `BacktestLotLedger`（買進建立 `PositionLot` 並記錄 `entry_date = T`；賣出按 FIFO 扣減最早批次並結算 `ClosedLotTrade`）。 |
| **Step 05** | `STEP_05_EXPIRE_UNFILLED_AT_CLOSE_1330` | `T 13:30:00+08:00` | 於當日收盤將所有當日未成交或部分成交剩餘之有效委託轉為 `PaperOrderStatus.EXPIRED`，並單次釋放剩餘之 `reserved_cash` 或 `reserved_shares`。 |
| **Step 06** | `STEP_06_CLOSE_VALUATION_1500` | `T 15:00:00+08:00` | 以 `T 15:00` 盤後時點透過 `HistoricalDataProvider.get_daily_bar(..., as_of_time=T 15:00)` 取得當日收盤行情，由 `PortfolioValuator.evaluate_daily_nav(...)` 計算 `DailyNAVRecord`（`total_nav = available_cash + reserved_cash + market_value + dividends_receivable`；停牌標的採用最後已知收盤價並附結構化警告）。 |
| **Step 07** | `STEP_07_POST_MARKET_RISK_UPDATE_1500` | `T 15:00:00+08:00` | 由 `RiskEngineAdapter.update_post_market_risk_state(...)` 更新歷史峰值 `peak_nav = max(prev_peak_nav, total_nav)`，並構造含 `dividends_receivable` 之 `BacktestRiskContext` 檢查是否觸發最大回撤熔斷（`circuit_breaker_active`）。 |
| **Step 08** | `STEP_08_CONSUME_PREDICTION_1800` | `T 18:00:00+08:00` | 由 `PredictionFeed.consume_batch(decision_date=T, decision_time=T 18:00)` 取得並驗證當日盤後 AI 預測批次（驗證 `available_at <= T 18:00`、`as_of_date == next_trading_day(T)`、`trained_cutoff_date` 與 Embargo 隔離天數）。 |
| **Step 09** | `STEP_09_STRATEGY_EVALUATION_1800` | `T 18:00:00+08:00` | 由 `StrategyEngineAdapter.evaluate_post_market(...)` 建立含 FIFO `entry_date` 之唯讀 `PortfolioSnapshot`（其中 `snapshot.cash = available_cash`，不含 `dividends_receivable`），交由 `StrategyEngine.evaluate(...)` 產生 `BUY` / `SELL` / `CLOSE` / `HOLD` / `SKIP` 決策與 `UnifiedTradeSignal`。 |
| **Step 10** | `STEP_10_RISK_GATE_AND_RESERVATION_1800` | `T 18:00:00+08:00` | 由 `RiskEngineAdapter.dispatch_signals_for_next_day(...)` 將策略訊號逐筆送入 `TradingGateway.submit_signal(...)` 進行 10 道風控審核；核准者建立 `ACCEPTED` 委託並立即預留買進現金或賣出股數，拒絕者絕不建立有效委託或預留。 |
| **Step 11** | `STEP_11_QUEUE_NEXT_DAY_ORDERS_1800` | `T 18:00:00+08:00` | 將 Step 10 核准之 `ACCEPTED` 委託排入下一交易日（$T+1$）開盤待撮合佇列，嚴格禁止於當日（$T$）回溯撮合成交。 |
| **Step 12** | `STEP_12_DAILY_AUDIT_LOG_1800` | `T 18:00:00+08:00` | 驗證當日 `executed_steps == EXPECTED_DAILY_TIMELINE_SEQUENCE`，封裝不可變之 `DailyBacktestLog`，並將下一交易日之風控基準 `baseline_nav` 更新為今日 `nav_record.total_nav`。 |

---

## 四、`PredictionFeed` 的防前視與未驗證預測處理規則

[`src/tw_quant/backtest/prediction_feed.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/prediction_feed.py) 實作了以下 5 層防前視與血統隔離規則：

1. **未驗證預測（缺少 `available_at`）之明確攔截與隔離**：
   - 針對如 `qlib-tw-trader` 中缺少真實產生時間與可用時間（`available_at=None`）之歷史預測 CSV，`InMemoryPredictionFeed` 提供 `UnverifiedPredictionPolicy`：
     - `REJECT`（預設）：註冊時若 `available_at is None` 立即拋出 `PITViolationError`。
     - `QUARANTINE`：註冊時若 `available_at is None` 則拒絕放入可消費池，改寫入 `quarantined_records`（`QuarantinedPredictionRecord`），確保未驗證預測永遠不會在回測中被消費。
2. **嚴格 PIT 時間鏈驗證**：
   - 強制驗證 `batch.data_cutoff_time <= batch.generated_at <= available_at <= decision_time`。
   - 若 `available_at` 早於決策日收盤時間（`13:30`），拋出 `LookaheadBiasError`（防止將盤後才完成的推論偽裝為盤中可用）。
   - 若消費時 `available_at > decision_time`（例如 `20:00` 才完成推論，但試圖在 `18:00` 決策消費），立即拋出 `LookaheadBiasError`。
3. **決策日與預定進場日對齊驗證**：
   - `batch.data_cutoff_time` 所屬台北日期必須等於決策日 $T$（且 $T$ 必須為有效交易日）。
   - `batch.as_of_date` 必須嚴格等於 `calendar.next_trading_day(T)`（即下一有效交易日 $T+1$），防止同日回溯成交或跨日錯位。
4. **Walk-Forward 訓練截止日 (`trained_cutoff_date`) 與 Embargo 隔離期驗證**：
   - 當設定 `require_trained_cutoff_date=True` 或註冊 `WalkForwardWindowSpec` 時，強制要求 `batch.metadata.trained_cutoff_date` 非空。
   - 若 `trained_cutoff_date >= decision_date`（模型使用了決策日當日或未來的標籤訓練），立即拋出 `LookaheadBiasError`。
   - 若 `calendar.trading_days_between(trained_cutoff_date, decision_date) <= embargo_trading_days`（侵入 Embargo 緩衝期），立即拋出 `EmbargoViolationError`。
5. **單次註冊與單次消費防重**：
   - 同一 `batch_id` 或同一 `decision_date` 重複註冊，或同一 `decision_date` 重複呼叫 `consume_batch(...)`，立即拋出 `DuplicatePredictionConsumptionError`。

---

## 五、`StrategyEngineAdapter` 與 `RiskEngineAdapter` 的橋接方式

[`src/tw_quant/backtest/adapters.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/adapters.py) 採用**零侵入橋接模式（Non-Intrusive Adapter Pattern）**，完全不需修改既有 `StrategyEngine`、`RiskEngine`、`TradingGateway` 或 `TaiwanPaperBroker`：

1. **為什麼不需要修改 `StrategyEngine`（解決問題 A-1）**：
   - 既有 `PortfolioSnapshot` 與 `PositionSnapshot` 已具備 `entry_date: Optional[dt.date]` 欄位，先前問題 A-1 的根源僅在於 `PortfolioLedger` 只記加權平均成本而不記每批次進場日。
   - `StrategyEngineAdapter.build_portfolio_snapshot(...)` 在建立 `PortfolioSnapshot` 時，首先驗證 `PortfolioLedger` 與 `BacktestLotLedger` 的持倉股數 100% 一致，接著直接從 `lot_ledger.get_oldest_entry_date(symbol)` 取出 FIFO 最早未平倉批次之進場日填入 `PositionSnapshot.entry_date`。
   - 因此 `StrategyEngine` 原生的 `_expiry_state(...)` 即可透過 `calendar.trading_days_between(pos.entry_date, planned) >= config.holding_trading_days` 精確觸發 `HOLDING_PERIOD_EXPIRED` 平倉訊號。
   - 同時，`PortfolioSnapshot.cash` 嚴格傳入 `portfolio_ledger.cash`（不含 `dividends_receivable`），確保應收股利在 `payable_date` 入帳前絕不會被 `StrategyEngine` 當作可買股現金。
2. **為什麼不需要修改 `RiskEngine`（解決問題 A-2）**：
   - 既有 `RiskEngine.evaluate(signal, ctx)` 讀取 `ctx.calculate_current_nav()` 與 `ctx.daily_total_pnl` 來檢核單日最大虧損（Check 8）與最大回撤熔斷（Check 9）。
   - 若直接使用 `TaiwanPaperBroker.build_risk_context(...)`，其 `RiskContext.calculate_current_nav()` 僅計算 `cash + reserved_cash + market_value` 且將 `daily_unrealized_pnl` 硬編碼為 `0.00`，在除息日會因股價除息下調而短少 `dividends_receivable`，導致誤觸日虧損或最大回撤熔斷。
   - `adapters.py` 定義了繼承自 `RiskContext` 的 `BacktestRiskContext(RiskContext)`，新增 `dividends_receivable: Decimal` 並覆寫 `calculate_current_nav()` 為 `cash + reserved_cash + market_value + dividends_receivable`，同時將 `daily_unrealized_pnl` 精確設為 `(total_nav - baseline_nav) - daily_realized_pnl`。
   - 在 `RiskEngineAdapter.dispatch_signals_for_next_day(...)` 執行 `gateway.submit_signal(...)` 期間，透過 `try ... finally` 暫時將 `broker.build_risk_context` 導向回傳 `BacktestRiskContext`，使 `RiskEngine`：
     - 在 Check 8（日虧損）與 Check 9（最大回撤）中看見包含 `dividends_receivable` 的真實總資產 `total_nav` 與真實當日損益，**除息日絕不誤觸風控熔斷**；
     - 在 Check 7（買進現金與最低現金保留檢查）中，仍檢查 `ctx.cash`（僅含可用現金，不含 `dividends_receivable`），**確保應收股利在發放日前亦不可能通過風控用於買股**。

---

## 六、測試清單與通過結果

執行指令：

```powershell
$env:PYTHONPATH="src"; python -m unittest tests/test_backtest_runner.py -v
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py" -v
```

### 1. `tests/test_backtest_runner.py`（16 項測試全數通過）

| 測試編號 | 測試方法名稱 | 驗證目標與斷言內容 | 結果 |
| :---: | :--- | :--- | :---: |
| 1 | `test_01_daily_12_step_causal_sequence` | 驗證單日 12 個步驟嚴格依照 `EXPECTED_DAILY_TIMELINE_SEQUENCE` 順序執行，無跳步或逆序。 | PASS |
| 2 | `test_02_t_minus_1_signal_matches_at_t_open_and_valued_at_t_close` | 驗證 $T-1$ 盤後 (`18:00`) 產生訊號並預留現金 $\rightarrow$ $T$ 開盤 (`09:00`) 含滑價與 Tick 對齊撮合成交 $\rightarrow$ $T$ 收盤 (`15:00`) 依收盤價估值。 | PASS |
| 3 | `test_03_prevent_same_day_execution_of_post_market_signal` | 驗證 $T$ 盤後產生的委託不會在 $T$ 成交；若強制將盤後委託送入當日行情撮合，立即拋出 `LookaheadBiasError`。 | PASS |
| 4 | `test_04_prevent_future_prediction_leakage_and_missing_available_at` | 驗證缺少 `available_at` 在 `REJECT` 下拋出 `PITViolationError`、在 `QUARANTINE` 下被隔離不可消費；且 `available_at > decision_time` 或同日 `as_of_date == decision_date` 拋出 `LookaheadBiasError`。 | PASS |
| 5 | `test_05_reject_trained_cutoff_date_lookahead_and_embargo_violation` | 驗證 `trained_cutoff_date >= decision_date` 拋出 `LookaheadBiasError`，侵入 Embargo 隔離天數拋出 `EmbargoViolationError`，合規模型順利通過。 | PASS |
| 6 | `test_06_fixed_holding_trading_days_expiry_exit_on_next_day` | 驗證持倉滿 `holding_trading_days=2` 個交易日時，即使 AI 分數仍高，仍強制產生 `CLOSE (HOLDING_PERIOD_EXPIRED)` 並於下一交易日開盤平倉。 | PASS |
| 7 | `test_07_ex_dividend_drop_with_receivable_does_not_trigger_risk_circuit_breaker` | 驗證除息日股價下跌 `-12%` 但含 `dividends_receivable` 時，`total_nav` 不減損、不誤觸 `RiskEngine` 單日虧損 (`3%`) 或最大回撤 (`4%`) 熔斷，當日新買單正常核准。 | PASS |
| 8 | `test_08_dividends_receivable_cannot_fund_buys_until_payable_date` | 驗證除息日產生之 `dividends_receivable` 在 `payable_date` 前不可用於買進新股（`SKIP: INSUFFICIENT_CASH`）；待 `payable_date 08:00` 入帳至 `cash` 後即可買進並於次日成交。 | PASS |
| 9 | `test_09_risk_rejected_signal_creates_no_order_or_reservation_or_fill` | 驗證 `RiskEngine`（如啟動 Kill Switch）拒絕訊號時，不產生有效排隊委託、`reserved_cash == 0.00`、次日無成交。 | PASS |
| 10 | `test_10_limit_up_buy_and_limit_down_sell_unfilled_expire_and_release_reservations` | 驗證一字漲停買不進 (`UNFILLED_LIMIT_UP_LOCKED`)、一字跌停賣不出 (`UNFILLED_LIMIT_DOWN_LOCKED`) 時，收盤轉為 `EXPIRED` 並單次完整釋放預留現金／預留股數。 | PASS |
| 11 | `test_11_partial_fill_at_open_then_remainder_expires_at_close_with_exact_accounting` | 驗證開盤受 5% 量能上限部分成交 (`PARTIALLY_FILLED_VOLUME_CAP`) 後，收盤將剩餘未成交股數轉為 `EXPIRED` 並精確釋放剩餘預留金。 | PASS |
| 12 | `test_12_halted_stock_blocks_execution_and_values_at_last_known_close_with_warning` | 驗證停牌股當日不可成交 (`UNFILLED_HALTED`)、委託收盤到期釋放預留股數，且收盤估值使用最後已知收盤價並附 `HALTED_LAST_KNOWN_PRICE_USED` 警告。 | PASS |
| 13 | `test_13_weekends_and_holidays_automatically_skipped` | 驗證 `runner.run()` 自動跳過國定休市日與週末；若直接對非交易日呼叫 `run_single_day` 立即拋出 `BacktestCalendarError`。 | PASS |
| 14 | `test_14_missing_prediction_batch_fail_fast_vs_skip_strategy_hold` | 驗證缺少當日 `PredictionBatch` 時，`FAIL_FAST` 立即拋出 `PredictionBatchUnavailableError`，`SKIP_STRATEGY_HOLD` 記錄警告並安全續抱。 | PASS |
| 15 | `test_15_multi_day_end_to_end_accounting_and_nav_conservation` | 驗證 6 個交易日連續回測（涵蓋新買進 `BUY`、超額權重部分減碼 `SELL OVER_TARGET_TRIM`、除息與股利實收、全數平倉 `CLOSE`）每日 NAV 恆等式與期末損益 100% 守恆。 | PASS |
| 16 | `test_16_duplicate_guards_prediction_runner_and_settlement` | 驗證重複註冊／重複消費同一 `PredictionBatch`、重複執行 `runner.run()` 或同一交易日、重複交割同一 `FillRecord` 均立即拋出對應防重例外。 | PASS |

### 2. 全系統測試總覽

- **總測試數**：**225 項測試**（含 C-2.1 資料層 14 項、C-2.2 帳務層 18 項、C-2.3 撮合與估值層 21 項、C-2.4 主迴圈與橋接層 16 項，以及既有第二階段與第三階段 A/B 測試 156 項）
- **執行結果**：**225 passed, 0 failed, 0 errors, 0 skipped**（耗時 `0.338s`）

---

## 七、原始子專案未變更證明

經實際執行 Git 檢查確認兩個原始候選子專案維持在預期基線 Commit 且工作目錄完全乾淨：

- `anti-gambling-trader-tw`：
  - `HEAD Commit`：`8609cef73718a056fddb74201fc304f174fa1553`
  - `git status -s`：無任何變更（Clean）
- `qlib-tw-trader`：
  - `HEAD Commit`：`774b3fb60d3b20b5c9ee86d25052ad2ec08e2011`
  - `git status -s`：無任何變更（Clean）

---

## 八、已知限制

1. **僅為合成資料之引擎邏輯驗證**：目前所有測試皆使用顯式標記 `is_synthetic=True` 之合成測試資料，尚未載入經 PIT 驗證之真實台股歷史日線、真實除權息公告或正式 Walk-Forward 重訓練之 Qlib 模型預測。
2. **尚未實作 `PerformanceAnalyzer`（留待 C-2.5）**：`WalkForwardRunner` 已輸出完整 `BacktestRunResult`（含 `nav_history`、`daily_logs`、`all_fills`、`closed_lot_trades`、`paid_dividends` 等），但年化報酬、Sharpe / Sortino / Calmar Ratio、滾動視窗回撤、毛利／淨利成本歸因與 Benchmark 超額報酬分析尚未實作。
3. **日線開盤保守撮合近似限制**：在缺少歷史盤中逐筆／分 K 與開盤競價量資料時，預設採用 `DAILY_OPEN_CONSERVATIVE` 次日開盤價加計滑價與 5% 日成交量上限之保守近似。
4. **公司行動支援範圍**：目前僅支援現金股利（`CASH_DIVIDEND`）；股票股利、拆併股與現金增減資仍由 `CorporateActionProcessor` 強制拋出 `UnsupportedCorporateActionError` 拒絕，絕不靜默忽略。

---

## 九、下一步 C-2.5（`PerformanceAnalyzer`、回測報告與驗收）的前置條件

1. **輸入資料結構已就緒**：`BacktestRunResult` 已完整提供：
   - 每日估值序列 `nav_history: Tuple[DailyNAVRecord, ...]`（含 `total_nav`、`available_cash`、`reserved_cash`、`market_value`、`dividends_receivable` 與 `is_complete` 標記）；
   - 已平倉批次明細 `closed_lot_trades: Tuple[ClosedLotTrade, ...]`（含每筆 `gross_pnl`、`net_pnl`、`allocated_buy_commission`、`allocated_sell_commission`、`allocated_sell_tax`、`holding_trading_days`）；
   - 每日執行日誌 `daily_logs: Tuple[DailyBacktestLog, ...]` 與完整資料血統標記 `is_synthetic`。
2. **C-2.5 實作要求**：
   - 建立 `src/tw_quant/backtest/metrics.py`（或 `analyzer.py`）與 `report.py`，直接消費 `BacktestRunResult` 計算 `BACKTEST_METHODOLOGY.md` 定義之 17 項核心績效、風險、交易行為與成本歸因指標；
   - 強制保留 `is_synthetic` 與未驗證資料警示，防止合成回測報告被誤讀為真實台股歷史回測結論。
