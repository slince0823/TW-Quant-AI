# 第三階段 C-2.3：歷史撮合模擬器與投資組合估值器實作與驗收報告 (PHASE3C2_3_REPORT.md)

**完成時間**：2026-10-21  
**階段名稱**：第三階段 C-2.3 — 歷史日線撮合模擬器 (`HistoricalExecutionSimulator`) 與投資組合估值器 (`PortfolioValuator`)  
**驗證狀態**：全系統 209 項測試全數通過（既有 188 項 + 本次新增 21 項，0 失敗、0 錯誤、0 跳過）

---

## 一、新舊檔案清單與實際介面摘要

本次實作嚴格遵守架構隔離與零外部套件規範，所有新增與修改均限於自建核心 `src/tw_quant/backtest/`、對應測試及專案導航文件，未觸動 `anti-gambling-trader-tw`、`qlib-tw-trader` 或既有 `broker` / `risk` / `strategy` 核心程式碼。

| 檔案路徑 | 變更類型 | 核心類別／介面摘要 |
| :--- | :--- | :--- |
| [`src/tw_quant/backtest/exceptions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/exceptions.py) | 擴充 | 新增 C-2.3 歷史撮合例外（`BacktestExecutionError`、`PriceLimitError`、`PriceLimitDataMissingError`、`TickSizeViolationError`、`UnknownTradingStatusError`、`DuplicateOrderExecutionError`、`OrderReservationError`）與估值例外（`BacktestValuationError`、`ValuationPriceMissingError`、`StaleValuationPriceError`、`IncompleteValuationError`） |
| [`src/tw_quant/backtest/simulator.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py) | 新增 | 實作 `HistoricalExecutionSimulator`、`ExecutionSimConfig`、`OrderExecutionOutcome`、`SymbolExecutionContext`、`ExecutionCostModel`、`TaiwanHistoricalPriceLimitRule` (`PriceLimitRule`)、`TaiwanTickSizeRule` (`TickSizeRule`)、列舉 `ExecutionMode`、`AmbiguousLimitPolicy`、`VolumeCapBasis`、`InstrumentCategory`、`ListingPhase`、`TradingStatus`、`TickSizeMode`、`PriceLimitStatus` |
| [`src/tw_quant/backtest/valuator.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/valuator.py) | 新增 | 實作 `PortfolioValuator`、`ValuationConfig`、`DailyNAVRecord`、`PositionValuationItem`、`ValuationWarning`、列舉 `ValuationPriceSource`、`MissingPricePolicy`、`CashAccountingBasis` |
| [`src/tw_quant/backtest/__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py) | 更新 | 匯出 C-2.1、C-2.2 與 C-2.3 全部公開類別、列舉、Protocol 與例外 |
| [`tests/test_backtest_execution.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_execution.py) | 新增 | 新增 21 個獨立測試方法，完整覆蓋歷史日線撮合、漲跌停／Tick Size 制度、預留釋放、FIFO 批次銜接與每日估值守恆 |
| [`PROJECT_MAP.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PROJECT_MAP.md) | 更新 | 更新第三階段 C-2.3 完成狀態、模組清單與 209 項測試統計 |
| [`PHASE3C2_3_REPORT.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PHASE3C2_3_REPORT.md) | 新增 | 本階段實作與驗收報告 |

---

## 二、設計文件與既有程式碼差異及處理方式

在實作前比對 `PHASE3C_DESIGN.md`、`BACKTEST_DATA_AUDIT.md`、`BACKTEST_METHODOLOGY.md` 與實際程式碼後，發現以下 4 項關鍵差異並採納相容且嚴謹的處理方式：

1. **委託狀態枚舉名稱差異（`PaperOrderStatus`）**：
   - **差異**：`PHASE3C_DESIGN.md` 第 4.2 節草稿曾寫為 `NEW -> SUBMITTED -> RESTING -> PARTIALLY_FILLED -> FILLED / EXPIRED`，但 [`src/tw_quant/broker/order.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/order.py#L22-L31) 的實際枚舉為 `NEW`、`ACCEPTED`、`PARTIALLY_FILLED`、`FILLED`、`CANCELLED`、`REJECTED`、`EXPIRED`。
   - **處理方式**：不修改既有 `PaperOrderStatus`，`HistoricalExecutionSimulator` 直接以實際枚舉 `NEW -> ACCEPTED -> PARTIALLY_FILLED / FILLED / EXPIRED / REJECTED / CANCELLED` 驅動狀態轉移，確保與 `PaperOrder.transition_to()` 100% 相容。
2. **既有 `FillEngine` 基準價與回測開盤保守模式差異**：
   - **差異**：既有 [`FillEngine.match_order()`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/engine.py#L254-L321) 內部寫死 `ref_price = bar.close` 作為前收參考價、固定單一 `price_limit_rate`，且市價單與限價單在無分筆行情時易受日線全天高低價影響。
   - **處理方式**：建立獨立的 [`HistoricalExecutionSimulator`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py#L728-L1150)，以 `HistoricalBarRecord`（含 `yesterday_close`、處置與停牌欄位）與 `SymbolExecutionContext` 為輸入，預設採用 `ExecutionMode.DAILY_OPEN_CONSERVATIVE` 次日開盤保守撮合，並產生完全相容於 `PortfolioLedger.apply_fill()` 與 `BacktestLotLedger.record_fill()` 的標準 [`FillRecord`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/order.py#L51-L72)。
3. **既有 `PortfolioLedger.reserve_buy_funds()` 現金預留口徑**：
   - **差異**：在 [`PortfolioLedger`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/ledger.py#L120-L157) 中，`reserve_buy_funds(amount)` 會從 `ledger.cash` 扣除並加進 `ledger.reserved_cash`；而部分成交後到期時，若像 `OrderManager._release_buy_reservation` 以「估算單價 × 剩餘股數」釋放，會因最低手續費 20 元或開盤滑價價差留下幾元的 `reserved_cash` 殘留。
   - **處理方式**：
     - `HistoricalExecutionSimulator` 按 `order_id` 精確追蹤每筆委託的**實際剩餘預留金額**（`_remaining_reserved_cash[order_id]`），部分成交時扣除該次 `actual_cost`，日終到期或撤單時精確釋放剩餘預留金額，確保 `reserved_cash` 歸零且絕不殘留。
     - `PortfolioValuator` 提供明確的 [`CashAccountingBasis`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/valuator.py#L84-L98)，預設 `SEPARATED_AVAILABLE_AND_RESERVED`（對應 `PortfolioLedger`，`total_cash = available_cash + reserved_cash`），亦支援 `TOTAL_CASH_INCLUDES_RESERVED`（當外部總現金已含預留時不重複加總）。
4. **策略快照與風控上下文的銜接**：
   - **處理方式**：[`DailyNAVRecord`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/valuator.py#L168-L320) 同時保存完整估值明細、歷史峰值 `peak_nav`、當日回撤 `drawdown_pct`、可交易收盤價字典 `tradable_close_prices`（排除停牌過期價），並內建 `to_portfolio_snapshot()` 與 `to_risk_context()` 轉換方法，供後續 C-2.4 主迴圈直接傳遞給 `StrategyEngine`（含 FIFO 最早進場日 `entry_date`）與 `RiskEngine`。

---

## 三、撮合價格、滑價、手續費、證交稅與預留釋放規則

### 1. 執行時點與防前視保護
- 預設執行模式為 `ExecutionMode.DAILY_OPEN_CONSERVATIVE`（次日開盤保守撮合）。
- $T-1$ 日盤後產生的訊號與委託（`created_at` 位於 $T-1$ 日 13:30 收盤後，或所屬訊號交易日 $\ge$ 目標撮合日 $T$ 且晚於 09:00 開盤），在 $T-1$ 日撮合時會被明確攔截（`order_created_after_bar_open`），只能在 $T$ 日或之後的交易日撮合。
- 嚴格禁止以 $T$ 日收盤資訊在 $T$ 日收盤價成交。

### 2. 市價單與限價單成交價規則
- **市價買單（MARKET BUY）**：以當日開盤價加計買方滑價並向上對齊 Tick Size：
  $$\text{fill\_price} = \min\left(\text{align\_tick\_up}\left(O_{i,t} \times (1 + s_{\text{bps}})\right),\ \text{limit\_up\_price}\right)$$
- **市價賣單（MARKET SELL / CLOSE）**：以當日開盤價扣除賣方滑價並向下對齊 Tick Size：
  $$\text{fill\_price} = \max\left(\text{align\_tick\_down}\left(O_{i,t} \times (1 - s_{\text{bps}})\right),\ \text{limit\_down\_price}\right)$$
- **限價買單（LIMIT BUY）**：
  - 委託價不得高於漲停價或低於跌停價（否則拒絕或依設定報錯），且必須符合 Tick Size。
  - 若當日最低價 $L_{i,t} > P_{\text{limit}}$：未觸價，不成交。
  - 若開盤向下跳空穿價（$O_{i,t} \le P_{\text{limit}}$）：以開盤價加計滑價後向上對齊 Tick Size，並以 $\min(\cdot, P_{\text{limit}})$ 為上限，確保成交價 $\le P_{\text{limit}}$。
  - 若開盤高於限價但盤中最低價觸及限價（$O_{i,t} > P_{\text{limit}} \ge L_{i,t}$）：依 `AmbiguousLimitPolicy` 處理：
    - `CONSERVATIVE_FILL_AT_LIMIT`（預設）：僅當 $L_{i,t}$ 足夠觸及限價時以限價 $P_{\text{limit}}$ 成交。
    - `STRICT_REQUIRE_PENETRATION`：必須嚴格穿透（$L_{i,t} < P_{\text{limit}}$）才以 $P_{\text{limit}}$ 成交，僅觸價（$L_{i,t} == P_{\text{limit}}$）保守不成交。
    - `REJECT_AMBIGUOUS`：日線無法確認盤中先後順序時保守不成交。
- **限價賣單（LIMIT SELL / CLOSE）**：
  - 若當日最高價 $H_{i,t} < P_{\text{limit}}$：未觸價，不成交。
  - 若開盤向上跳空穿價（$O_{i,t} \ge P_{\text{limit}}$）：以開盤價扣除滑價後向下對齊 Tick Size，並以 $\max(\cdot, P_{\text{limit}})$ 為下限，確保成交價 $\ge P_{\text{limit}}$。
  - 若開盤低於限價但盤中最高價觸及限價（$O_{i,t} < P_{\text{limit}} \le H_{i,t}$）：依 `AmbiguousLimitPolicy` 以 $P_{\text{limit}}$ 成交或保守不成交，絕不低於 $P_{\text{limit}}$。

### 3. 漲跌停鎖死、停牌、處置股與成交量參與上限
- **漲跌停鎖死**：
  - 買單遇到開盤即漲停（$O_{i,t} \ge P_{\text{up}}$）或全天一字漲停鎖死（$O=H=L=C=P_{\text{up}}$）時保守阻擋不成交（`blocked_by_limit_up`）。
  - 賣單遇到開盤即跌停（$O_{i,t} \le P_{\text{down}}$）或全天一字跌停鎖死（$O=H=L=C=P_{\text{down}}$）時保守阻擋不成交（`blocked_by_limit_down`）。
- **停牌與處置股**：
  - 停牌（`is_halted=True` 或 `TradingStatus.HALTED / SUSPENDED`）：禁止買賣撮合。
  - 未知交易狀態（`TradingStatus.UNKNOWN`）：立即拋出 `UnknownTradingStatusError`，拒絕自動視為正常交易。
  - 處置股（`is_disposition=True` 或 `TradingStatus.DISPOSITION`）：預設拒絕新買單與市價單；若開啟允許處置股交易，可成交上限額外乘上處置流動性折扣（預設 `0.50`）。
- **成交量參與率上限（Volume Participation Cap）**：
  - 支援三種量能基準 `VolumeCapBasis`：
    1. `HISTORICAL_ADV_ONLY`：僅使用截至 $T-1$ 日之歷史平均成交量（如 `adv_20_shares`），完全不讀取 $T$ 日未知全天量。
    2. `BAR_VOLUME_EX_POST_CAP`：以當日實際成交量 `bar.volume` 作為事後流動性硬上限。
    3. `MIN_OF_ADV_AND_BAR_VOLUME`（預設最保守）：同時受截至 $T-1$ 日之 `adv_20_shares`（若有提供）與當日實際成交量 `bar.volume` 之較小值限制。
  - 預設單日單筆參與率上限為 `5%`，處置股再折半，並對齊交易單位（整股 1,000 股、零股 1 股）。

### 4. 單一職責成本計算與單次預留釋放
- **單一職責成本計算（[`ExecutionCostModel`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py#L574-L667)）**：
  - 手續費：$\max\left(\text{round}(\text{Notional} \times 0.001425 \times \text{discount}),\ \text{min\_commission}\right)$（整股預設最低 20 元、零股最低 1 元）。
  - 證交稅（僅賣出計收）：明確區分一般股票 `COMMON_STOCK`（`0.3%`）、ETF（`0.1%`）與當沖減半（`0.15%`）。若商品類別為 `InstrumentCategory.UNKNOWN`，立即拋出 `BacktestAccountingError`，拒絕猜測稅率。
  - 滑價與手續費、證交稅僅在 `HistoricalExecutionSimulator` 建立 `FillRecord` 時計算一次；`PortfolioLedger.apply_fill()` 與 `BacktestLotLedger.record_fill()` 直接讀取 `FillRecord` 欄位入帳，絕不二次扣費。
- **預留資金／股數單次釋放保證**：
  - 委託為當日有效（ROD），日終收盤時透過 `expire_unfilled_orders()` 將未成交或部分成交剩餘委託轉為 `EXPIRED`，並精確釋放該筆委託剩餘的預留現金或預留股數。
  - 內部以 `_reservation_released_order_ids` 集合鎖定，重複呼叫到期或撤單會拋出 `OrderReservationError`，保證每筆委託最多只釋放一次預留。

---

## 四、漲跌停與 Tick Size 規則（含支援範圍與資料缺漏處理）

### 1. 可替換漲跌停規則介面 [`PriceLimitRule`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py#L283-L452)
- 預設實作 [`TaiwanHistoricalPriceLimitRule`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py#L305-L452) 支援：
  - **2015-06-01 起**：台股漲跌幅限制 $\pm 10\%$，漲停價按前日收盤參考價 $\times 1.10$ 並依對應 Tick Size 向下取整（floor），跌停價按前日收盤參考價 $\times 0.90$ 並依對應 Tick Size 向上取整（ceil）。
  - **1989-10-11 至 2015-05-31**：台股歷史漲跌幅限制 $\pm 7\%$。
  - **無漲跌幅限制階段（`ListingPhase`）**：新股上市首五日（`IPO_NO_LIMIT_PERIOD`）、興櫃無漲跌幅（`EMERGING_NO_LIMIT`）、恢復買賣首日無漲跌幅（`RELISTING_NO_LIMIT`）回傳 `PriceLimitStatus.NO_PRICE_LIMIT`（`limit_up_price=None, limit_down_price=None`）。
  - **資料缺漏與未知階段防禦**：當 `reference_price <= 0`、`ListingPhase.UNKNOWN`、`InstrumentCategory.UNKNOWN` 或交易日早於規則支援起點時，回傳 `PriceLimitStatus.UNKNOWN_DATA`，撮合器立即拋出 `PriceLimitDataMissingError`，絕不猜測漲跌停價。

### 2. 台股 Tick Size 規則介面 [`TickSizeRule`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py#L172-L280)
- 預設實作 [`TaiwanTickSizeRule`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py#L194-L280) 支援：
  - **一般股票（`COMMON_STOCK` / `REIT`）六級距**：
    - $< 10$ 元：`0.01` 元
    - $10 \sim < 50$ 元：`0.05` 元
    - $50 \sim < 100$ 元：`0.10` 元
    - $100 \sim < 500$ 元：`0.50` 元
    - $500 \sim < 1000$ 元：`1.00` 元
    - $\ge 1000$ 元：`5.00` 元
  - **ETF（`ETF`）二級距**：
    - $< 50$ 元：`0.01` 元
    - $\ge 50$ 元：`0.05` 元
- 支援 `TickSizeMode.STRICT_REJECT`（限價單不符 Tick Size 直接拋出 `TickSizeViolationError` 或拒絕委託）與 `ALIGN_CONSERVATIVE`（買單向下對齊、賣單向上對齊）。

---

## 五、估值、現金、預留、持倉與應收股利一致性說明

[`PortfolioValuator.evaluate_daily_nav()`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/valuator.py#L368-L625) 在每個交易日收盤時執行五重一致性檢核與 NAV 守恆驗證：

1. **現金與預留現金不重複計價**：
   - 針對 `PortfolioLedger` 的資金結構（`reserve_buy_funds` 已從 `ledger.cash` 移入 `ledger.reserved_cash`），採用 `SEPARATED_AVAILABLE_AND_RESERVED`：
     $$\text{total\_cash} = \text{available\_cash} + \text{reserved\_cash}$$
   - 若外部傳入之現金已包含預留現金（`TOTAL_CASH_INCLUDES_RESERVED`），則：
     $$\text{available\_cash} = \text{total\_cash} - \text{reserved\_cash}$$
   - 兩種口徑均嚴格驗證 $\text{available\_cash} + \text{reserved\_cash} == \text{total\_cash}$，杜絕預留現金被重複加計或遺漏。
2. **可用股數與預留賣出股數不重複計價**：
   - 賣單預留股數 `reserved_shares[sym]` 屬於目前總持倉股數 `positions[sym].quantity` 的子集：
     $$\text{total\_quantity} = \text{available\_quantity} + \text{reserved\_quantity}$$
   - 估值時以 `total_quantity * valuation_price` 計算一次持倉市值，若發現 `reserved_quantity > total_quantity`，立即拋出 `BacktestValuationError`。
3. **`PortfolioLedger` 與 `BacktestLotLedger` 雙帳本交叉對帳**：
   - 當同時傳入 `lot_ledger` 時，強制驗證兩邊每檔持倉總股數與總投入成本完全一致，並提取每檔持倉的 FIFO `oldest_entry_date` 寫入 `PositionValuationItem`。
4. **除權息應收股利（`dividend_receivable`）與現金轉換守恆**：
   - 整合 `CorporateActionProcessor.get_pending_receivables(as_of_date)`，將除息日至發放日前的在途應收股利納入總資產：
     $$\text{NAV}_t = \text{available\_cash}_t + \text{reserved\_cash}_t + \text{market\_value}_t + \text{dividend\_receivable}_t$$
   - 股利發放日當天，應收股利結清歸零並轉入 `available_cash`，總 NAV 保持等額守恆，絕不發生二次入帳或除息日假性暴跌。
5. **停牌最後已知價警告與拒絕假精確 NAV**：
   - 正常交易日優先使用當日收盤價（`BAR_CLOSE`）。
   - 若股票當日停牌且無當日收盤價，僅在 `MissingPricePolicy.USE_LAST_CLOSE_WITH_WARNING` 下允許使用過去已知收盤價（`LAST_KNOWN_CLOSE`），同時：
     - 產生結構化 `ValuationWarning`（記錄 `price_date` 與過期交易日數 `staleness_trading_days`）；
     - 標記 `PositionValuationItem.is_tradable_price = False`，並將該價格排除於 `DailyNAVRecord.tradable_close_prices` 之外，**嚴禁把停牌最後已知價當作當日可成交價**。
   - 若停牌過期天數超過 `max_stale_trading_days`（預設 20 個交易日），或完全缺失價格，依設定拋出 `StaleValuationPriceError` / `ValuationPriceMissingError` 或回傳 `is_complete=False, nav=None`，**絕不以 `0` 填充缺失價格或偽造精確 NAV**。

---

## 六、測試清單與結果（既有測試 + 新增測試）

### 1. 新增測試清單（[`tests/test_backtest_execution.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_execution.py)，共 21 項）

| 測試編號 | 測試方法名稱 | 驗證情境與斷言重點 | 結果 |
| :--- | :--- | :--- | :---: |
| 01 | `test_01_market_orders_matched_at_open_benchmark` | 市價買單與市價賣單依當日開盤價加計滑價並對齊 Tick Size 撮合，絕不使用當日收盤價 | PASS |
| 02 | `test_02_post_market_order_blocked_on_same_day_and_no_same_day_close_execution` | $T-1$ 盤後產生之委託不得在 $T-1$ 成交，且在 $T$ 日開盤撮合時不得使用 $T$ 日收盤資訊 | PASS |
| 03 | `test_03_limit_orders_never_violate_limit_price_or_daily_bounds` | 限價買單不以高於限價成交、限價賣單不以低於限價成交；未觸價或超出漲跌停不成交 | PASS |
| 04 | `test_04_opening_gap_through_limit_price_and_ambiguous_ohlc_policies` | 開盤跳空穿越限價時以開盤價加滑價且不劣於限價成交；驗證三種 `AmbiguousLimitPolicy` | PASS |
| 05 | `test_05_limit_up_buy_and_limit_down_sell_conservative_blocking` | 漲停鎖死時買單不得成交（`blocked_by_limit_up`）、跌停鎖死時賣單不得成交（`blocked_by_limit_down`） | PASS |
| 06 | `test_06_price_limit_rule_interface_special_phases_and_missing_data` | 驗證 IPO 首五日無漲跌幅、2015-06-01 前 7% 漲跌幅，以及缺少參考價或未知階段時拋出 `PriceLimitDataMissingError` | PASS |
| 07 | `test_07_tick_size_validation_and_alignment` | 驗證台股普通股六級距與 ETF 二級距 Tick Size、非法限價單攔截與保守方向對齊 | PASS |
| 08 | `test_08_halted_disposition_and_unknown_trading_status` | 停牌不成交、處置股拒絕市價單／新買單、未知交易狀態拋出 `UnknownTradingStatusError` | PASS |
| 09 | `test_09_volume_participation_cap_and_causal_adv_rules` | 驗證 5% 成交量參與上限、`HISTORICAL_ADV_ONLY` 防前視量能上限與處置股折半 | PASS |
| 10 | `test_10_partial_fill_and_unfilled_quantity_accounting` | 明確區分可成交上限（`max_fillable_quantity`）、實際成交（`filled_quantity`）與未成交（`unfilled_quantity`） | PASS |
| 11 | `test_11_unfilled_order_expiry_and_single_reservation_release` | 部分成交與完全未成交委託於當日收盤到期時精確釋放剩餘預留資金與股數（`0.00` 殘留），且重複釋放報錯 | PASS |
| 12 | `test_12_sell_order_reserved_shares_and_oversell_protection` | 賣單預留股數隔離，同一標的重複下賣單超出可用股數時拒絕，絕不超賣 | PASS |
| 13 | `test_13_single_responsibility_commission_tax_slippage_no_double_charge` | 手續費、證交稅（普通股 0.3% vs ETF 0.1%）與滑價只計算一次；未知商品類別拋出 `BacktestAccountingError` | PASS |
| 14 | `test_14_fill_record_seamless_integration_with_backtest_lot_ledger` | 撮合器產生的 `FillRecord` 直接交由 `BacktestLotLedger` 分批買進與 FIFO 部分賣出，與 `PortfolioLedger` 對帳 100% 一致 | PASS |
| 15 | `test_15_cash_reserved_positions_receivables_and_nav_consistency` | 驗證可用現金、預留現金、可用持倉、預留持倉與應收股利之 NAV 守恆分解，杜絕重複計算 | PASS |
| 16 | `test_16_dividend_receivable_to_cash_nav_conservation` | 除息日建立應收股利、發放日轉為現金後，應收股利歸零且總 NAV 不因帳務轉換跳變 | PASS |
| 17 | `test_17_missing_price_stale_price_and_halted_valuation_warnings` | 停牌股票最後已知價只作明確標記的估值（附 `ValuationWarning`），且排除於 `tradable_close_prices` | PASS |
| 18 | `test_18_refuse_fake_exact_nav_when_unvaluable` | 缺失價格絕不預設為 0；過期超限或無法估值時拋出明確例外或回傳 `is_complete=False, nav=None` | PASS |
| 19 | `test_19_timezone_and_trading_day_boundary_enforcement` | 無時區時間戳攔截、跨時區 UTC 時間戳正確轉為 `Asia/Taipei` 交易日，以及非交易日拒絕撮合與估值 | PASS |
| 20 | `test_20_duplicate_order_and_fill_processing_guards` | 同一委託重複提交、同日重複撮合、終態委託再次撮合或同一 `FillRecord` 重複入帳均拋出明確例外 | PASS |
| 21 | `test_21_subprojects_clean_and_pure_stdlib_verification` | 驗證兩個原始子專案 HEAD commit 與工作目錄完全未修改，且新模組僅使用 Python 標準庫 | PASS |

### 2. 全系統測試統計

執行指令：
```powershell
python -m unittest tests/test_backtest_execution.py -v
python -m unittest discover -s tests -p "test_*.py"
```

| 測試檔案 | 測試階段 | 測試數量 | 狀態 |
| :--- | :--- | :---: | :---: |
| `tests/test_signal.py` | Phase 2A (`UnifiedTradeSignal`) | 19 | PASS |
| `tests/test_paper_broker.py` | Phase 2B (`TaiwanPaperBroker`) | 26 | PASS |
| `tests/test_risk_engine.py` | Phase 2C (`RiskEngine`) | 22 | PASS |
| `tests/test_integration.py` | Phase 2D (`TradingGateway` 端到端整合) | 23 | PASS |
| `tests/test_prediction.py` | Phase 3A (`PredictionBatch` & 離線推論) | 14 | PASS |
| `tests/test_strategy_engine.py` | Phase 3B (`StrategyEngine`) | 52 | PASS |
| `tests/test_backtest_data_layer.py` | Phase 3C-2.1 (回測基礎資料層) | 14 | PASS |
| `tests/test_backtest_accounting.py` | Phase 3C-2.2 (FIFO 持倉批次與除權息會計) | 18 | PASS |
| `tests/test_backtest_execution.py` | **Phase 3C-2.3 (歷史撮合模擬器與投資組合估值器)** | **21** | **PASS** |
| **總計** | **全系統測試套件** | **209** | **100% PASS (0.353s)** |

---

## 七、尚未實作範圍與已知限制

1. **尚未實作之元件（留待後續 C-2.4 等步驟）**：
   - `PredictionProvider`、`StrategyEngineAdapter`、`RiskEngineAdapter`
   - `WalkForwardRunner`（完整回測主迴圈、Walk-Forward 滾動視窗與 Embargo 隔離推進）
   - `PerformanceAnalyzer` 與 `BacktestReport`（17 項績效指標、統計顯著性檢定與 Benchmark 比較）
2. **已知限制與誠實揭露**：
   - 目前的日線撮合模擬器以日頻 OHLCV 與可選歷史 ADV 為基礎，採保守假設（開盤基準 + 滑價、漲跌停鎖死不成交、盤中穿價保守處理），不能取代逐筆委託簿（Level-2 Order Book）微觀結構模擬。
   - 現階段所有 209 項測試均為使用合成／記憶體內資料之離線單元與整合測試，尚未執行真實台股歷史資料的完整回測，不得將測試通過宣稱為真實策略回測績效。
