# 第三階段 C-2.5 獨立技術審查報告 (Read-Only Technical Audit)

> **審查日期**：2026-10-21  
> **審查類型**：第三階段 C-2.5 績效分析、回測報告與防偽驗收獨立唯讀程式碼審查 (Read-Only Audit)  
> **審查基準**：
> - 系統測試全數通過基準 (248/248 Passed)
> - `BACKTEST_METHODOLOGY.md`（台股歷史回測方法論與因果防護規範）
> - `PHASE3C_DESIGN.md`、`BACKTEST_DATA_AUDIT.md`、`PHASE3C2_5_REPORT.md`、`PROJECT_MAP.md`
> - `anti-gambling-trader-tw`（Commit: `8609cef73718a056fddb74201fc304f174fa1553`）
> - `qlib-tw-trader`（Commit: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011`）
> 
> **重要聲明**：
> **本審查報告僅針對回測績效分析器、會計一致性、時序因果、資料血統防偽與指標數學邏輯進行靜態審查與手算驗證。本次審查絕對不代表任何真實台股歷史績效已獲得驗證，亦不可作為任何實盤交易或策略有效性之背書。**

---

## 目錄

1. [審查範圍與未能檢查的部分](#一審查範圍與未能檢查的部分)
2. [發現清單 (附檔案、函式、程式碼位置與具體反例)](#二發現清單)
3. [17 項核心指標與補充指標逐項審查結果](#三17-項核心指標與補充指標逐項審查結果)
4. [三個會計情境手算核對](#四三個會計情境手算核對)
5. [防偽與資料血統風險分析](#五防偽與資料血統風險分析)
6. [前視偏差及回測時序風險分析](#六前視偏差及回測時序風險分析)
7. [發現嚴重程度與修正建議彙總](#七發現嚴重程度與修正建議彙總)
8. [需要人工確認之關鍵問題](#八需要人工確認之關鍵問題)
9. [建議修正順序 (Remediation Priority)](#九建議修正順序)
10. [審查結論與正式聲明](#十審查結論與正式聲明)

---

## 一、審查範圍與未能檢查的部分

### 1.1 審查範圍 (Audited Scope)
本次審查以靜態程式碼分析 (Static Code Analysis)、時序邊界推演 (Causal Order Tracing) 與手算反例 (Counter-example Verification) 方式，覆蓋以下核心模組與相關合約：
1. **績效分析核心**：[`src/tw_quant/backtest/metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/metrics.py)
   - 17 項方法論核心指標與 10 項補充指標之計算公式、自由度、年化係數與邊界狀況。
   - `PerformanceAnalyzer`、`AnalyzerConfig`、`PnLReconciliation`、`BenchmarkSeries`。
2. **報告與防偽合約**：[`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)
   - `BacktestReport`、`LineageAttestation`、`LineageStatus`、`ValidationVerdict`。
   - 資料來源狀態判定、SHA-256 防偽雜湊、未驗證項目過濾。
3. **回測執行主迴圈與時序因果**：[`src/tw_quant/backtest/runner.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/runner.py)
   - 單日 12 步因果時序（08:00 現金股利入帳 $\to$ 08:30 應收股利登記 $\to$ 09:00 撮合與交割 $\to$ 13:30 逾期撤單 $\to$ 15:00 估值 $\to$ 18:00 預測消費與訊號產生）。
4. **帳務、除權息與撮合**：
   - [`src/tw_quant/backtest/lot_ledger.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/lot_ledger.py)（FIFO 批次扣減、成本分攤、已平倉損益）
   - [`src/tw_quant/backtest/corporate_actions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/corporate_actions.py)（除息應收與入帳）
   - [`src/tw_quant/backtest/valuator.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/valuator.py)（每日收盤 NAV 估值）
   - [`src/tw_quant/backtest/simulator.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/simulator.py)（開盤保守撮合與漲跌停限制）
5. **既有測試套件**：[`tests/test_backtest_metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py) 及全系統 248 項測試。

### 1.2 未能檢查與待驗證的部分 (Out of Scope / Pending Verification)
1. **真實台股歷史行情資料品質**：目前 `qlib-tw-trader/data/` 為空目錄 (0 Bytes)，全系統測試皆使用合成資料 (Synthetic Data)，尚未接入真實證交所 (TWSE/TPEx) 歷史 Tick/Bar 進行實測。
2. **Qlib LightGBM 訓練模型權重與特徵發布延遲**：尚未實測離線 AI 模型推論在真實財報與月營收公布日（Point-In-Time Disclosure Lag）下的特徵可用性。
3. **多窗口 Walk-Forward 滾動回測效能與記憶體壓力**：尚未在跨 5 年以上、全市場 1,800+ 檔標的之大規模行情下進行長時間壓力測試。
4. **實盤券商 API 網路延遲與撮合滑價分佈**：尚未對接真實券商環境，滑價模型目前為保守固定點數或比例近似。

---

## 二、發現清單

以下依據審查所發現之程式邏輯漏洞、方法論偏離與邊界情境，逐項列出詳細檔案、函式、程式碼片段、具體反例與建議修復方案。

---

### [FINDING-01] (HIGH) 預測血統 PIT 驗證閘門未檢查 `trained_cutoff_date <= decision_date`，允許未來資料模型取得真實證明

- **檔案路徑**：[`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L126-L128)
- **函式名稱**：`BacktestReport.build`
- **相關程式碼**：
  ```python
  # report.py 第 126-128 行
  else:
      pit_ok = all(r.available_at >= r.generated_at and r.trained_cutoff_date is not None for r in records)
      pred_status = LineageStatus.REAL_PIT_ATTESTED if pit_ok else LineageStatus.REAL_UNVERIFIED
  ```
- **具體反例**：
  假設某策略在回測日期 `2025-01-10` 進行決策，上游提供之預測批次中，模型訓練資料截止日為 `trained_cutoff_date = 2025-06-30`（即模型在訓練時已看過未來半年的價格走勢）。
  當前程式碼僅檢查：
  1. `available_at >= generated_at`（例如皆為 `2025-01-10 18:00`）
  2. `trained_cutoff_date is not None`（此條件滿足，因日期存在）
  由於程式未檢查 `trained_cutoff_date <= r.decision_date`，`pit_ok` 將被判定為 `True`，預測血統直接被授予 `LineageStatus.REAL_PIT_ATTESTED`！
- **嚴重程度**：`HIGH`（資料可信度與防前視保護之重大漏洞）
- **建議修正方向**：
  在 `report.py` 檢查 `pit_ok` 時，嚴格加上決策日與訓練截止日的因果關係比對：
  ```python
  pit_ok = all(
      r.available_at >= r.generated_at
      and r.trained_cutoff_date is not None
      and r.trained_cutoff_date < r.decision_date
      for r in records
  )
  ```

---

### [FINDING-02] (HIGH) Lineage 認證閘門純依賴呼叫端字串，SHA-256 僅能防偽報告內容而非資料源頭真實性

- **檔案路徑**：[`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L57-L78) 及 [L180-L186](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L180-L186)
- **函式名稱**：`LineageAttestation` 與 `BacktestReport.build`
- **相關程式碼**：
  ```python
  # report.py 第 183-186 行
  elif all_attested and nav_complete and run_completed and att.evidence_notes:
      verdict = ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED
  ```
- **具體反例**：
  呼叫端若自行建立非合成資料物件，並傳入：
  ```python
  att = LineageAttestation(
      market_data_pit_verified=True,
      calendar_verified=True,
      corporate_actions_coverage_verified=True,
      benchmark_verified=True,
      evidence_notes=("verified by developer",),  # 任意非空字串
  )
  ```
  系統即會給出 `ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED`。
  同時，`content_hash` 是對生成的 JSON 進行雜湊，只能防止「報告產出後的竄改」，無法在數學上證明「餵入回測引擎的原始 CSV/Parquet 確實來自臺灣證券交易所官方」。
- **嚴重程度**：`HIGH`（可能導致使用者誤信系統已完成第三方資料源真偽驗證）
- **建議修正方向**：
  1. 報告結構中應將 `claims_real_historical_verified` 修正標註為 `caller_attested_real_historical_pit`。
  2. 於報告中明記：SHA-256 雜湊僅代表「回測執行結果之快照防竄改性」，不代表「外部資料源頭的公證背書」。

---

### [FINDING-03] (MEDIUM) 最長回撤期間與恢復標記在多重同長度回撤情境下可能誤判 `is_recovered=True`

- **檔案路徑**：[`src/tw_quant/backtest/metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/metrics.py#L312-L314)
- **函式名稱**：`max_drawdown_and_duration`
- **相關程式碼**：
  ```python
  # metrics.py 第 312-314 行
  if episodes:
      best_tau = max(t for t, _ in episodes)
      best_recovered = next(rec for t, rec in episodes if t == best_tau)
  return mdd, best_tau, best_recovered
  ```
- **具體反例**：
  初始 NAV 為 100。
  淨值序列為：
  - 第 1 天：90 ($\tau=1$)
  - 第 2 天：90 ($\tau=2$)
  - 第 3 天：100 ($\tau=0$，創新高，產生回撤區間 A: `(tau=2, recovered=True)`)
  - 第 4 天：90 ($\tau=1$)
  - 第 5 天：90 ($\tau=2$，回測結束，產生回撤區間 B: `(tau=2, recovered=False)`)
  此時 `episodes = [(2, True), (2, False)]`，最大持續天數 `best_tau = 2`。
  執行 `next(rec for t, rec in episodes if t == best_tau)` 時，Python 的 `next()` 會回傳清單中的**第一個符合項目**，即 `True`！
  **結果**：回測明明在期末處於尚未恢復的 2 天最長回撤中，指標註記卻宣稱最長回撤 `is_recovered=True`！
- **嚴重程度**：`MEDIUM`（在平手長度的邊界案例下誤導風控分析）
- **建議修正方向**：
  若存在多個相同長度的最長回撤區間，且期末進行中的回撤亦達到該最長長度，應優先判定尚未恢復 (`best_recovered = False`)，或明確記錄 `active_at_end = True`。

---

### [FINDING-04] (MEDIUM) FIFO 批次拆分使得經濟決策被切碎，扭曲交易次數與勝率顆粒度

- **檔案路徑**：[`src/tw_quant/backtest/metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/metrics.py#L656-L672)
- **函式名稱**：`compute_trade_metrics`
- **相關程式碼**：
  ```python
  # metrics.py 第 660-672 行
  k = len(trades)
  out["closed_lot_trade_count"] = _ok("closed_lot_trade_count", k)
  ...
  out["win_rate"] = _ok(
      "win_rate", Decimal(len(wins)) / Decimal(k),
      f"wins={len(wins)}, losses={len(losses)}, flat={k - len(wins) - len(losses)}, K={k}",
  )
  ```
- **具體反例**：
  策略發出 1 筆賣出訊號，平倉過去在不同日期分 5 次買入的 5 個 Lot（例如各 1,000 股）。
  這在經濟學上是**「1 次投資平倉決策」**，但 `BacktestLotLedger` 會產出 5 筆 `ClosedLotTrade`。
  若這 5 個批次皆為獲利，指標中的 $K$ 增加了 5，勝率分子增加了 5。
  反之，若 1 筆單次買進行為被後續多筆賣出分次部分平倉，亦會被拆分成多筆 trade。
- **嚴重程度**：`MEDIUM`（方法論顆粒度差異；雖已明確命名為「已平倉批次」，但容易使量化研究員與「訊號勝率」混淆）
- **建議修正方向**：
  保留現有批次層級統計，但於報告中增加註解，未來應補充「訂單層級／信號循環層級 (Round-Trip Trade)」之聚合勝率與交易次數。

---

### [FINDING-05] (MEDIUM) 公司行動僅支援現金股利，遇股票股利或減資將直接引發異常中止

- **檔案路徑**：[`src/tw_quant/backtest/corporate_actions.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/corporate_actions.py#L167-L175)
- **函式名稱**：`CorporateActionProcessor.process_ex_dividend`
- **相關程式碼**：
  ```python
  if event.action_type in (
      CorporateActionType.STOCK_DIVIDEND,
      CorporateActionType.STOCK_SPLIT,
      CorporateActionType.REVERSE_SPLIT,
  ):
      raise UnsupportedCorporateActionError(
          f"尚不支援股票股利/分割/減資: {event.action_type.value}",
          context={"symbol": event.symbol, "ex_date": event.ex_date.isoformat()},
      )
  ```
- **具體反例**：
  台股歷史上金融股（如玉山金、台新金）及部分成長股常態性配發股票股利（如 0.5 元股票股利）。
  若直接餵入台股歷史真實除權息資料，回測主迴圈遇到除權日時將直接拋出 `UnsupportedCorporateActionError` 中止執行。
  若資料清洗階段強制濾除除權事件，則除權日股價將自然因除權而開盤下挫，而帳本持股數未增加，導致淨值無故暴跌。
- **嚴重程度**：`MEDIUM`（限制了後續真實歷史資料的接入能力）
- **建議修正方向**：
  在進入真實資料階段前，必須於待辦事項中規劃「股票股利與分割股數調整」模組，不可僅依賴現金股利會計。

---

### [FINDING-06] (LOW) 日週轉率分母使用當日收盤 NAV ($V_t$)，在特定暴跌情境下可能拉高週轉率

- **檔案路徑**：[`src/tw_quant/backtest/metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/metrics.py#L920-L923)
- **函式名稱**：`_turnover_and_daily_win`
- **相關程式碼**：
  ```python
  # metrics.py 第 920-922 行
  traded = sum((f.notional for f in log.settled_fills), _ZERO)
  nav = log.nav_record.total_nav
  daily.append(float(traded / (Decimal(2) * nav)))
  ```
- **具體分析**：
  撮合成交發生於 09:00（開盤），但分母採用的是當日 15:00 收盤估值之 `total_nav` ($V_t$)。
  雖然這符合 `BACKTEST_METHODOLOGY.md` 第四章公式：
  $$\text{Turnover}_{\text{daily}} = \frac{1}{N} \sum_{t=1}^{N} \frac{B_t + S_t}{2 \cdot V_t}$$
  但在投資組合理論中，更常見的做法是使用前一日收盤（即當日初淨值 $V_{t-1}$）作為分母。若當日淨值因市場波動大幅縮水，使用當日末淨值為分母會造成單日週轉率被數學放大。
- **嚴重程度**：`LOW`（符合專案既有方法論定義，但屬於度量慣例上的細微差別）
- **建議修正方向**：
  保留現行計算，但在補充指標中可評估加入以期初 NAV 為基準之週轉率比較。

---

### [FINDING-07] (LOW / INFO) 靜態股票池配置可能引發倖存者偏差與上市時間偏差

- **檔案路徑**：[`src/tw_quant/backtest/runner.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/runner.py#L156)
- **函式名稱**：`WalkForwardRunConfig`
- **相關程式碼**：
  ```python
  universe_symbols: Tuple[str, ...] = ()
  ```
- **具體分析**：
  目前 `runner.py` 接受固定的 `universe_symbols` 靜態清單。
  在未來接入真實台股資料時，若直接以「今日之台灣 50 成分股」回測過去 5 年，將排除過去 5 年中曾被剔除、甚至下市之公司，引入嚴重的「倖存者偏差 (Survivorship Bias)」。
- **嚴重程度**：`INFO`（屬於回測架構尚未實作動態股票池之架構性限制）
- **建議修正方向**：
  未來階段應建立 `PointInTimeUniverseProvider`，支援逐日取得當日歷史成分股清單。

---

## 三、17 項核心指標與補充指標逐項審查結果

依據 `BACKTEST_METHODOLOGY.md` 第四章規範，逐一檢核目前 `metrics.py` 的實作狀態：

| 編號 | 指標名稱 | 方法論數學公式 | `metrics.py` 實作函式 / 行號 | 邊界狀況處理 ($N=0, 1, 2$, 零分母) | 審查判定 |
| :---: | :--- | :--- | :--- | :--- | :---: |
| **1** | 累積報酬率 (Total Return) | $\frac{V_N - V_0}{V_0}$ | `compute_nav_metrics` (L515) | $N=0$ 回傳 `NOT_AVAILABLE`。使用 Decimal 精確計算。 | **合格** |
| **2** | 年化報酬率 (CAGR) | $(\frac{V_N}{V_0})^{\frac{A}{N}} - 1$ | `compute_nav_metrics` (L520) | $N=0$ 為 `NOT_AVAILABLE`；$N < 21$ 加註短窗口外推警語；數值溢位回傳 `UNDEFINED`。 | **合格** |
| **3** | 年化波動率 (Annualized Volatility) | $s_r \times \sqrt{A}$ ($s_r$ 自由度 $N-1$) | `compute_nav_metrics` (L531) | $N < 2$ 回傳 `INSUFFICIENT_DATA`；真實零波動回傳 `0.0`。 | **合格** |
| **4** | 夏普比率 (Sharpe Ratio) | $\frac{\bar{e}}{s_e} \times \sqrt{A}$ | `compute_nav_metrics` (L541) | $N < 2$ 回傳 `INSUFFICIENT_DATA`；$s_e \le 10^{-12}$ 回傳 `UNDEFINED` ("Zero Volatility")。 | **合格** |
| **5** | 索提諾比率 (Sortino Ratio) | $\frac{\bar{e} \times A}{\text{DD}_{\text{ann}}}$ | `compute_nav_metrics` (L552) | 下行風險 $\le 10^{-15}$ 回傳 `UNDEFINED`；分母除以 $N$ 開根號符合方法論。 | **合格** |
| **6** | 最大回撤 (MDD) | $\max_t \frac{M_t - V_t}{M_t}$ | `max_drawdown_and_duration` (L274) | $M_t$ 納入 $V_0$；淨值只漲不跌時回傳 `0.00`。 | **合格** |
| **7** | 最長回撤期間 (MDD Duration) | $\max_t \tau_t$ | `max_drawdown_and_duration` (L274) | 計算連續低於高水位交易日數；惟同長度回撤之 `is_recovered` 存在偏向判定 (見 Finding-03)。 | **有瑕疵 (MEDIUM)** |
| **8** | 卡瑪比率 (Calmar Ratio) | $\frac{\text{CAGR}}{\text{MDD}}$ | `compute_nav_metrics` (L568) | $\text{MDD} = 0$ 回傳 `UNDEFINED` ("Zero Drawdown")；CAGR 未定義回傳 `UNDEFINED`。 | **合格** |
| **9** | 勝率 (Win Rate) | $\frac{K_{\text{win}}}{K}$ | `compute_trade_metrics` (L669) | $K=0$ 回傳 `NOT_AVAILABLE`；零損益交易不計入勝或負，但計入分母 $K$。 | **合格 (批次口徑)** |
| **10** | 盈虧比 (Payoff Ratio) | $\frac{\text{AvgWin}}{\text{AvgLoss}}$ | `compute_trade_metrics` (L684) | 無獲利或無虧損交易時回傳 `UNDEFINED`。 | **合格** |
| **11** | 獲利因子 (Profit Factor) | $\frac{\sum \text{Win}}{\sum \|\text{Loss}\|}$ | `compute_trade_metrics` (L688) | 無虧損交易時分母為 0，回傳 `UNDEFINED`。 | **合格** |
| **12** | 平均持有交易日數 | $\frac{1}{K}\sum h_c$ 與加權平均 | `compute_trade_metrics` (L693) | 若任一批次缺失持有天數則回報 `INCOMPLETE`，不靜默忽略；同時提供股數加權天數。 | **合格** |
| **13** | 週轉率 (Turnover) | $\frac{1}{N} \sum \frac{B_t + S_t}{2 V_t} \times A$ | `_turnover_and_daily_win` (L906) | 每日單邊週轉率均值乘以 $A=252$；分母採用當日末 $V_t$。 | **合格** |
| **14** | 稅費拖累 (Cost Drag) | $\frac{C_{\text{total}}}{V_0}$ 等 3 種維度 | `_cost_attribution` (L847) | 無成交時回傳 `0.0` (非缺失)；同時提供年化拖累與有效費率。 | **合格** |
| **15** | 滑價成本佔比 | $\frac{\sum \text{Slippage}}{V_0}$ | `_cost_attribution` (L867) | 滑價金額已內含於成交價，僅作歸因揭露，不重扣。 | **合格** |
| **16** | 未成交委託比例 | $\frac{O_{\text{unfilled}}}{O_{\text{resolved}}}$ | `_order_metrics` (L946) | 期末尚未結案之委託另列 `pending`，不計入分母；提供股數短缺率與原因分佈。 | **合格** |
| **17** | 風控攔截比例 | $\frac{S_{\text{rejected}}}{S_{\text{total}}}$ | `_risk_metrics` (L983) | 統計 `allowed=False` 比例；依 12 項規則分列攔截原因。 | **合格** |

### 補充指標審查結論
- `excess_return_total` 與 `excess_return_annualized`：強制要求基準口徑必須為 `TOTAL_RETURN`（含息），若口徑不符則直接標記 `BASIS_MISMATCH`，有效防範台股「不含息指數 vs 含息 NAV」之虛假超額報酬。
- `tracking_error`、`information_ratio`、`beta`：數學公式完全符合標準定義，當 Benchmark 變異數為 0 或追蹤誤差為 0 時，皆正確回傳 `UNDEFINED`。

---

## 四、三個會計情境手算核對

為驗證會計一致性與對帳邏輯（`PnLReconciliation`），針對以下 3 個具體情境進行嚴謹手算與對帳核實。

### 情境 A：完全無交易 (Flat Cash)
- **設定條件**：
  - 初始資金：\$1,000,000（現金）。
  - 期間天數：5 個交易日，無任何下單與成交。
- **手算演推**：
  - 每日現金：\$1,000,000，持倉市值：\$0，應收股利：\$0。
  - 每日 NAV：\$1,000,000。
  - 期末 NAV：$V_5 = \$1,000,000$。
- **指標與損益對帳核對**：
  - 累積報酬率：$\frac{1,000,000 - 1,000,000}{1,000,000} = 0.00\%$
  - 年化波動率：日報酬皆為 0，波動率 $= 0.00\%$
  - 已平倉批次：$K=0 \to$ 勝率、盈虧比皆為 `NOT_AVAILABLE`
  - 會計各項金額：
    - 已實現淨損益：\$0.00
    - 未平倉未實現損益：\$0.00
    - 已發放股利：\$0.00
    - 期末應收股利：\$0.00
    - 解釋損益總額：$\$0 + \$0 + \$0 + \$0 = \$0.00$
    - 殘差 (Residual)：$(V_5 - V_0) - \text{explained} = 0.00 - 0.00 = \$0.00$
- **結論**：對帳狀態 `status = OK`，無任何殘差。

---

### 情境 B：買入後持有至期末 (Buy & Hold, 未平倉)
- **設定條件**：
  - 初始資金：\$1,000,000。
  - 第 1 天：產生買入 1,000 股 2330 委託。
  - 第 2 天：09:00 開盤以 \$600.00 成交 1,000 股。
    - 買進價金 (Notional)：$1,000 \times 600 = \$600,000$
    - 買進手續費 (0.1425%)：$\$600,000 \times 0.001425 = \$855$
    - 扣減現金：$\$600,855$，剩餘現金：$\$399,145$
    - 建立 PositionLot：股數 1,000，成本基礎 `remaining_cost_basis` = \$600,855（含手續費）
    - 第 2 天收盤價 \$620.00：
      - 市值：$1,000 \times 620 = \$620,000$
      - $V_2 = \$399,145 + \$620,000 = \$1,019,145$
  - 第 3 天 (期末)：收盤價 \$650.00：
    - 市值：$1,000 \times 650 = \$650,000$
    - $V_3 = \$399,145 + \$650,000 = \$1,049,145$
- **手算損益對帳核對**：
  - 淨值變動：$\Delta \text{NAV} = 1,049,145 - 1,000,000 = +\$49,145$
  - 已實現損益：\$0.00（無平倉，$K=0$）
  - 未平倉成本基礎：\$600,855
  - 未平倉未實現損益：$\text{MarketValue} - \text{OpenCost} = 650,000 - 600,855 = +\$49,145$
  - 股利總額：\$0.00
  - 解釋總額：$\text{Realized}(0) + \text{Unrealized}(49,145) + \text{Dividends}(0) = +\$49,145$
  - 殘差 (Residual)：$49,145 - 49,145 = \$0.00$
- **手算成本交叉檢核**：
  - 成交手續費總額：\$855
  - 已平倉分攤手續費：\$0
  - 未平倉剩餘買進手續費：\$855
  - 成本殘差：$855 - 0 - 855 = \$0.00$
- **結論**：買進手續費已精確資本化計入未實現損益中，未平倉損益完全解釋 NAV 變動，對帳狀態 `status = OK`。

---

### 情境 C：買入、除息、賣出平倉並收取股利
- **設定條件**：
  - 初始資金：\$1,000,000。
  - 第 2 天：買進 1,000 股 2330 @ \$600.00，手續費 \$855，扣款 \$600,855，現金 \$399,145。
  - 第 3 天 (除息日)：每股除息現金股利 \$10.00。
    - 登記應收股利：$1,000 \times 10 = \$10,000$（發放日訂為第 5 天）。
    - 當日收盤除息價 \$590.00，持股市值：\$590,000。
    - 當日 NAV：$\text{Cash}(399,145) + \text{MarketValue}(590,000) + \text{Receivable}(10,000) = \$999,145$。
    - *（注意：NAV 並未因除息產生跳空虧損，完全由應收股利補平）*
  - 第 4 天：全數賣出 1,000 股 2330 @ \$630.00。
    - 賣出價金：$1,000 \times 630 = \$630,000$
    - 賣出手續費 (0.1425%)：$\$630,000 \times 0.001425 = \$897.75 \to \$898$
    - 證券交易稅 (0.3%)：$\$630,000 \times 0.003 = \$1,890$
    - 淨入帳價金：$\$630,000 - \$898 - \$1,890 = \$627,212$
    - 現金更新：$\$399,145 + \$627,212 = \$1,026,357$
    - FIFO 平倉已實現淨損益：
      $$\text{Realized PnL} = \text{NetProceeds}(\$627,212) - \text{CostBasis}(\$600,855) = +\$26,357$$
    - 當日收盤 NAV：$\text{Cash}(1,026,357) + \text{Receivable}(10,000) = \$1,036,357$。
  - 第 5 天 (發放日兼期末)：
    - 08:00 現金股利 \$10,000 入帳，現金增為 \$1,036,357，應收股利歸零。
    - 期末 NAV：$V_5 = \$1,036,357$。
- **手算損益對帳核對**：
  - 淨值變動：$\Delta \text{NAV} = 1,036,357 - 1,000,000 = +\$36,357$
  - 已平倉已實現淨損益：+\$26,357
  - 未平倉未實現損益：\$0.00
  - 已入帳股利：+\$10,000
  - 期末應收股利：\$0.00
  - 解釋總額：$\$26,357 + \$0 + \$10,000 + \$0 = +\$36,357$
  - 殘差 (Residual)：$36,357 - 36,357 = \$0.00$
- **防止重複計算檢驗**：
  - 累積報酬率直接以 $(V_5 - V_0) / V_0 = +3.6357\%$ 計算。
  - `metrics.py` 的 `dividend_income_paid` 僅作歸因揭露，**絕未與 NAV 報酬重複加總**。
- **結論**：會計帳務、除權息應收轉實收、交易稅費與淨損益對帳完全吻合，殘差精確為 0。

---

## 五、防偽與資料血統風險分析

1. **五大資料來源血統分類**：
   `report.py` 將行情、預測、交易日曆、公司行動與基準等 5 項資料來源進行分立檢查，只要其中任一項為 `is_synthetic=True`，最終驗證結論即強制降級為 `ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY`。此項設計非常嚴謹。
2. **防偽漏洞（前述 Finding-01 & Finding-02）**：
   - 系統目前的防偽弱點在於「真實資料驗證的充分條件」過於寬鬆：`LineageAttestation` 的證據欄位僅需填入非空字串（如 `evidence_notes=("ok",)`），且預測資料僅檢查欄位非空，尚未能在本機對外部資料進行加密指紋與歷史公告日實質對帳。
3. **無公司行動與未提供之區別**：
   當回測期間沒有任何除息事件時，`report.py` 能正確標註為 `LineageStatus.NO_EVENTS_RECORDED`，並將其列入 `unverified_items`，防止因「回測期間無除息」而草率宣告覆蓋率已驗證。

---

## 六、前視偏差及回測時序風險分析

1. **單日 12 步因果時序完整性**：
   靜態檢查 `runner.py` 的主迴圈流程，時序順序嚴格固定：
   - 08:00/08:30 先行處理當日股利入帳與除息除權，保證 09:00 開盤前券商現金與應收部位完全就緒。
   - 09:00 僅撮合「前一日盤後」送出之委託，當日產生的委託絕對不會在當日撮合成交。
   - 15:00 估值完成後，18:00 始消費預測並觸發策略評估。
2. **盤後訊號時序因果**：
   - 策略在 $T$ 日 18:00 取得的資料為 $T$ 日收盤價與 $T$ 日盤後預測，產生的委託排入佇列，於 $T+1$ 日 09:00 開盤執行撮合，無盤中偷看未來收盤價之嫌。
3. **殘存前視風險點**：
   - **財報發布日偏離**：離線 AI 特徵若是使用季度結束日（如 3/31）而非實際申報截止日（如 5/15），此種前視偏差存在於資料前處理端，目前回測引擎本身無法在下游檢測出來。

---

## 七、發現嚴重程度與修正建議彙總

| 發現代號 | 嚴重程度 | 模組與檔案 | 核心問題摘要 | 建議修復順序 |
| :---: | :---: | :--- | :--- | :---: |
| **FINDING-01** | `HIGH` | `report.py` (L127) | `pit_ok` 漏查 `trained_cutoff_date < decision_date` | 優先級 P1 |
| **FINDING-02** | `HIGH` | `report.py` (L183) | LineageAttestation 純文字易偽造；SHA-256 範圍易誤解 | 優先級 P1 |
| **FINDING-03** | `MEDIUM` | `metrics.py` (L313) | 最長回撤同長度時 `is_recovered` 偏向先期判斷 | 優先級 P2 |
| **FINDING-04** | `MEDIUM` | `metrics.py` (L660) | FIFO Lot 平倉切碎造成交易次數與勝率非訂單級 | 優先級 P2 |
| **FINDING-05** | `MEDIUM` | `corporate_actions.py` (L167) | 僅支援現金股利，遇股票股利/減資直接拋出異常 | 優先級 P3 (C-3) |
| **FINDING-06** | `LOW` | `metrics.py` (L922) | 週轉率分母使用當日末 NAV 而非期初 NAV | 優先級 P4 |
| **FINDING-07** | `INFO` | `runner.py` (L156) | 靜態 `universe_symbols` 於跨年回測具倖存者偏差 | 優先級 P3 (C-3) |

---

## 八、需要人工確認之關鍵問題

在進入後續階段（如 C-3 真實資料接入或 Qlib 模型整合）前，需架構師或專案負責人確認以下設計決策：

1. **交易勝率的業務定義**：
   目前系統的 `win_rate` 是以「FIFO 平倉批次 (Closed Lot)」計算，而非「策略進出場信號 (Signal/Order Round-trip)」計算。專案是否需要於第四階段新增訂單級聚合指標？
2. **真實資料驗證的簽署架構**：
   目前的 `LineageAttestation` 是以布林值與文字註記。未來是否應引入資料集雜湊清單（Data Manifest Hash）或離線校驗簽章，以避免人為誤標？
3. **股票股利與減資的處理策略**：
   目前遇股票股利會直接報錯中斷。在進入台股 10 年歷史回測前，是否必須先完成「除權（股票股利）股數自動除權調整」？

---

## 九、建議修正順序 (Remediation Priority)

為確保系統在下一階段具備最高可信度，建議依照以下順序進行維護：

### 優先級 P1（防偽與前視阻斷，建議於進入 C-3 前修復）
1. 修復 `report.py` 中 `pit_ok` 的比對條件，強制檢查 `r.trained_cutoff_date < r.decision_date`。
2. 調整 `report.py` 結論說明，明確區分「呼叫端保證 (Attested)」與「獨立第三方資料指紋校驗」。

### 優先級 P2（指標精確度修補）
1. 修復 `metrics.py` 的 `max_drawdown_and_duration`，當最大長度與期末未恢復長度相同時，標註未恢復。
2. 在報告的 `trade_statistics` 中明確標註批次層級特性，並預留訂單級欄位。

### 優先級 P3（真實台股回測必備功能，排入 C-3 階段）
1. 實作 `PointInTimeUniverseProvider`，支援台股動態股票池以消除倖存者偏差。
2. 擴充 `CorporateActionProcessor`，支援股票股利與現金增減資之股數調整。

---

## 十、審查結論與正式聲明

### 10.1 審查總結
第三階段 C-2.5 在「會計一致性」、「單日 12 步因果時序」以及「17 項方法論指標公式」之實作水準極高：
1. **會計嚴密**：損益對帳 `PnLReconciliation` 邏輯清晰，買進手續費資本化處理妥善，現金股利未發生重複計入 NAV 報酬之重大錯誤。
2. **時序正確**：開盤撮合、收盤估值、盤後決策界線分明，未發現代碼層級的即時前視偷看問題。
3. **指標完整**：17 項核心指標全數實作，除少數邊界註記與批次粒度外，公式均完全符合方法論。

### 10.2 正式免責與驗收聲明
> **【正式聲明】**  
> 本次審查為**唯讀技術審查 (Read-Only Audit)**，全部測試皆在合成測試資料下執行。
> 1. 本次審查**絕不代表真實台股歷史績效已獲得驗證**。
> 2. 目前系統尚未接入台股真實撮合行情，亦未載入真實 Qlib 模型權重。
> 3. 任何基於目前程式碼輸出之年化報酬率、夏普比率或勝率，皆屬**模組架構測試數值**，嚴禁作為真實投資決策、策略上線或對外募資之依據。

---
*(本報告結束，遵循唯讀審查規範，未修改專案程式碼與測試，請靜待人工架構審查)*
