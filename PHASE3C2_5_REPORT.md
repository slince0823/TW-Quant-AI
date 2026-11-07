# 第三階段 C-2.5：績效分析、回測報告與防偽驗收報告 (`PHASE3C2_5_REPORT.md`)

**報告日期**：2026-10-21  
**驗證性質聲明**：本階段所有端到端測試皆使用顯式標記為合成 (`is_synthetic=True`) 的行情、日曆、預測與公司行動；僅驗證績效分析器與報告產生器之數學與防偽邏輯，**不代表任何真實台股歷史績效**。

---

## 一、17 項指標實際實作狀態（對照 `BACKTEST_METHODOLOGY.md` 第四章）

先對照方法論第四章清單（共 17 項）與使用者任務清單，**兩者並不一致**，差異如下並已寫入 `METHODOLOGY_DEVIATIONS` 與報告 `methodology.deviations`：

- 使用者清單有、方法論 17 項沒有：超額報酬、追蹤誤差（方法論第五章定義）、交易次數、買／賣手續費分列、證交稅、毛／淨損益、股利收入、平均獲利交易、平均虧損交易。→ 列為 **supplementary**，不改動 17 項定義。
- 方法論 17 項有、使用者清單沒有：第 16 項（未成交委託比例）、第 17 項（風控攔截比例）。→ 仍依方法論實作。

| # | key | 指標 | 狀態 |
| :-: | :--- | :--- | :---: |
| 1 | `total_return` | 累積報酬率 | 已實作 |
| 2 | `cagr` | 年化報酬率 | 已實作 |
| 3 | `annualized_volatility` | 年化波動率 | 已實作 |
| 4 | `sharpe_ratio` | Sharpe | 已實作 |
| 5 | `sortino_ratio` | Sortino | 已實作 |
| 6 | `max_drawdown` | 最大回撤 | 已實作 |
| 7 | `max_drawdown_duration` | 最長回撤期間（含 `is_recovered`） | 已實作 |
| 8 | `calmar_ratio` | Calmar | 已實作 |
| 9 | `win_rate` | 勝率（已平倉批次）＋ `daily_win_rate` 輔助 | 已實作 |
| 10 | `payoff_ratio` | 平均盈虧比（含 `average_win`/`average_loss`） | 已實作 |
| 11 | `profit_factor` | Profit Factor | 已實作 |
| 12 | `average_holding_days` | 平均持有交易日（簡單平均，股數加權寫入 note） | 已實作 |
| 13 | `turnover` | 年化週轉率 | 已實作 |
| 14 | `commission_tax_cost_drag` / `cost_drag_annualized` / `effective_fee_rate` | 稅費拖累三種口徑 | 已實作 |
| 15 | `slippage_cost_drag` | 滑價成本佔初始資金比 | 已實作 |
| 16 | `unfilled_order_ratio` / `volume_shortfall_ratio` | 未成交委託與股數短缺 | 已實作（原因分類以 `ExecutionOutcomeCode` 計） |
| 17 | `risk_rejection_ratio` | 風控攔截比例與原因拆解 | 已實作 |

補充：`excess_return_total`、`excess_return_annualized`、`tracking_error`、`information_ratio`、`beta`、`closed_lot_trade_count`、`fill_count`、`buy_commission`、`sell_commission`、`securities_transaction_tax`、`slippage_cost_total`、`realized_pnl_net`、`realized_pnl_gross_before_fees`、`dividend_income_paid`、`dividend_receivable_end`。

## 二、公式與輸入欄位

定義：$V_0$ = `BacktestRunResult.initial_cash`；$V_t$ = `DailyNAVRecord.total_nav`（= 可用現金＋預留現金＋持倉市值＋應收股利，已含股利）；$r_t=V_t/V_{t-1}-1$；$N$ = NAV 日數；$A$ = `trading_days_per_year`（預設 252）；$R_f$ = `risk_free_rate_annual`（預設 0.015，方法論值）；$r_f=(1+R_f)^{1/A}-1$。

| 指標 | 公式 | 輸入欄位 |
| :--- | :--- | :--- |
| 累積報酬 | $(V_N-V_0)/V_0$（`Decimal`） | `initial_cash`, `nav_history.total_nav` |
| CAGR | $(V_N/V_0)^{A/N}-1$；$N<21$ 於 note 標註外推 | 同上 |
| 波動率 | $s_r\sqrt{A}$（樣本標準差，n-1） | 同上 |
| Sharpe | $\bar e/s_e\cdot\sqrt A$，$e=r-r_f$ | 同上 |
| Sortino | $\bar e\cdot A/(DD_{daily}\sqrt A)$，$DD_{daily}=\sqrt{\tfrac1N\sum\min(0,e)^2}$ | 同上 |
| MDD | $\max (M_t-V_t)/M_t$，$M$ 含 $V_0$ | 同上 |
| 回撤期間 | $\tau_t=0$（創高）或 $\tau_{t-1}+1$，取最大值 | 同上 |
| Calmar | CAGR / MDD | 衍生 |
| 勝率 | $K_{win}/K$，$K$ 含淨損益為 0 的交易 | `ClosedLotTrade.realized_pnl` |
| 盈虧比 / PF | $\overline{win}/\overline{|loss|}$；$\sum win/\sum|loss|$ | 同上 |
| 平均持有 | $\sum h/K$、$\sum q h/\sum q$ | `holding_trading_days`, `quantity` |
| 週轉率 | $\overline{(B_t+S_t)/(2V_t)}\cdot A$ | `DailyBacktestLog.settled_fills.notional`, `total_nav` |
| 稅費拖累 | $C/V_0$；$C/\bar V\cdot A/N$；$C/\sum notional$，$C=\sum(commission+tax)$ | `FillRecord.commission/tax/notional` |
| 滑價 | $\sum FillRecord.slippage/V_0$ | `FillRecord.slippage`（=\|成交價−參考價\|×股數） |
| 未成交 | $O_{unfilled}/O_{resolved}$；$1-\sum filled/\sum qty$ | `approved_orders_queued`, `PaperOrder` |
| 風控攔截 | $S_{rejected}/S_{total}$ | `DailyBacktestLog.dispatch_summary.audit_records` |
| 超額報酬／TE／IR／Beta | 方法論第五章公式；需 `BenchmarkSeries` | Benchmark 水位 |

## 三、缺失資料與邊界處理

統一以 `MetricStatus`（`OK` / `NOT_AVAILABLE` / `INSUFFICIENT_DATA` / `UNDEFINED` / `INCOMPLETE` / `BASIS_MISMATCH`）表示，**非 OK 時 `value=None`（建構時強制檢查），絕不填 0**。

- **NAV 驗證**：日期必須嚴格遞增（重複、未排序皆拒絕）、NAV 為正的有限 `Decimal`；`nav_history` 日期必須等於 `trading_days_executed`（缺漏／重複拒絕）。
- **NAV 不完整**（`total_nav is None` / `is_complete=False` / `is_official_nav=False`）：預設 `REJECT` 拋 `InvalidNavSeriesError`；`MARK_INCOMPLETE` 模式下所有 NAV 型指標、週轉率、對帳皆標 `INCOMPLETE`。
- **N=0**：NAV 型指標 `NOT_AVAILABLE`。**N<2**：波動、Sharpe、Sortino、TE 等 `INSUFFICIENT_DATA`。
- **分母為零**：Sharpe（零波動）、Sortino（零下行）、Calmar（零回撤）、PF（無虧損）、盈虧比、IR、Beta 皆 `UNDEFINED`。浮點分母 ≤ 1e-12 視為零。
- **與方法論之差異**（全數記錄於 `METHODOLOGY_DEVIATIONS`）：N=0／N<2 不回傳 0.0 而改以狀態表示；未結案委託不納入第 16 項分母；第 17 項僅將 `risk_decision.allowed=False` 計為風控攔截，通過風控但被券商拒絕者另列 `non_risk_rejections`。
- **未平倉部位**：`PnLReconciliation` 驗證 `final_nav − initial = 已實現淨損益 + 未平倉未實現損益 + 已入帳股利 + 應收股利`（容許 0.01 元）；並檢查未平倉批次股數／標的與期末 NAV 持倉一致，不一致即拒絕，不忽略未實現損益。
- **費用不重複**：`realized_pnl` 已為淨值，毛損益採「淨＋已分攤費用」還原；成本交叉核對成交層級手續費／證交稅 = 已平倉分攤 + 未平倉剩餘；滑價內含於成交價，僅揭露。
- **股利**：僅取帳本 `paid_dividends`／`pending_dividends_at_end`；報酬完全來自 NAV，不另加股利。
- **Benchmark**：缺少 → `NOT_AVAILABLE`（明示無法計算真實超額報酬）；日期缺漏、區間內多出日期、無基準日 → `BenchmarkAlignmentError`（`strict_benchmark=False` 時標 `DATE_MISMATCH`）；價格指數 vs 含股利 NAV → `BASIS_MISMATCH`；合成 Benchmark 可計算但標註 `SYNTHETIC_BENCHMARK`。

## 四、新增與修改檔案

| 檔案 | 類型 | 內容 |
| :--- | :---: | :--- |
| `src/tw_quant/backtest/metrics.py` | 新增 | `PerformanceAnalyzer`、`AnalyzerConfig`、`MetricValue/MetricStatus`、`NavPoint`、`BenchmarkSeries`、`PnLReconciliation`、方法論對照表與差異記錄 |
| `src/tw_quant/backtest/report.py` | 新增 | `BacktestReport`、`LineageAttestation`、`LineageStatus`、`ValidationVerdict` |
| `src/tw_quant/backtest/exceptions.py` | 擴充 | `BacktestMetricsError`、`InvalidNavSeriesError`、`BenchmarkAlignmentError` |
| `src/tw_quant/backtest/__init__.py` | 更新 | 公開匯出 |
| `tests/test_backtest_metrics.py` | 新增 | 23 項測試 |
| `PROJECT_MAP.md`、`PHASE3C2_5_REPORT.md` | 更新／新增 | 文件 |

未修改任何既有非 backtest 模組，亦未修改 C-2.1~C-2.4 既有邏輯與測試。

## 五、測試命令與結果

```powershell
$env:PYTHONPATH="src"; python -m unittest tests/test_backtest_metrics.py
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```

- `test_backtest_metrics.py`：**23 項，23 通過，0 失敗，0 錯誤**。
- 全系統：**248 項，248 通過，0 失敗，0 錯誤**（原 225 + 新增 23）。
- 開發過程中修正之問題：測試 fixture 使用含 `FAKE` 字樣之資料來源名稱被既有 `DataProvenance` 防偽規則攔截（既有防偽機制運作正確，僅改測試用名稱）；未放寬任何既有斷言或規則。

任務要求之 18 類測試對應：正／負／零報酬(01)、年化與波動手算(02)、Sharpe 與無風險利率(03)、Sortino 零下行(04)、Calmar 零回撤(05)、MDD 與持續時間(06)、勝率／盈虧／PF(07)、無已平倉交易(08, 09)、未平倉與 NAV 一致性(09)、NAV 日期缺漏／重複／未排序(10, 11)、NAV 不完整(11)、費用不重複(12)、股利不重複(13)、Benchmark 不一致(14, 14b)、合成標記保留(15)、無 PIT 證據不得宣稱驗證(16)、序列化與重現(17)、全部回歸(248 通過)。另有 17 項登錄表完整性、週轉率手算、部分成交與風控攔截指標測試。

## 六、原始子專案狀態

| 專案 | HEAD | working tree |
| :--- | :--- | :--- |
| `anti-gambling-trader-tw` | `8609cef73718a056fddb74201fc304f174fa1553` | clean |
| `qlib-tw-trader` | `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` | clean |

## 七、合成資料與真實資料的明確區別

報告**分別**檢查五個來源並輸出各自 `LineageStatus`：行情、預測、交易日曆、公司行動、Benchmark。

- 任一來源為 `SYNTHETIC`（含 Mock 模型、`is_synthetic_payable_date`、`is_synthetic` 旗標）→ 結論固定為 `SYNTHETIC_TEST_RESULT_ONLY`，即使呼叫端提供完整 attestation 亦然（測試 15 驗證）。
- 全為非合成但缺證據 → `REAL_HISTORICAL_NOT_VERIFIED`。
- 僅當五來源皆為 `REAL_PIT_ATTESTED`、NAV 完整、回測 `COMPLETED`、非 Mock、且 `LineageAttestation.evidence_notes` 非空，才輸出 `REAL_HISTORICAL_PIT_VERIFIED`。
- `BacktestRunResult` 本身不含 PIT／覆蓋度證據，因此驗證旗標預設全為 `False`，必須由真實資料接入流程明確提供。公司行動「無事件」只標 `NO_EVENTS_RECORDED`，不推定為已驗證覆蓋。`model_type` 不在消費紀錄內，未提供 `PredictionMetadata` 時輸出 `NOT_AVAILABLE`，不虛構。
- 報告輸出：`is_synthetic`、模型名稱／版本／特徵版本、行情與 Benchmark 來源與版本、回測起訖、PIT 驗證狀態、NAV 完整性、警告清單、未驗證項目、結論適用範圍與限制，並附 SHA-256 `content_hash`（`from_json` 驗證，竄改即拒絕）。

## 八、尚未解決的風險

1. 測試 16(f) 以假想「非合成」結果驗證閘門邏輯，並非真實資料；閘門只能保證「不會在證據缺失時誤宣稱」，不能保證 attestation 自身為真，需獨立審查真實資料接入流程。
2. `trained_cutoff_date`、`available_at` 的真實性仍依賴預測供應端；`PredictionConsumptionRecord` 不含 `model_type`。
3. 滑價僅能以成交層級總額揭露，無法分攤到每個已平倉批次；成本交叉核對依賴 `open_lots_at_end` 之剩餘買進手續費。
4. 預設 $R_f$=1.5% 為方法論假設值，非實際歷史利率；真實回測應由資料提供各期利率。
5. 短窗口（N<21）年化指標僅標註不阻擋；單一樣本區間之績效無統計顯著性檢定（Walk-Forward 多窗口彙總未實作）。
6. 未建置內部等權 Buy-and-Hold 基準（方法論 5.1.3）與可投資 Benchmark 成本調整；目前只接受外部提供之 `BenchmarkSeries`。
7. 沿用既有限制：日線保守撮合近似、僅支援現金股利、倖存者偏差與財報公告時間未處理。

## 九、下一階段（真實歷史資料接入）前置條件

1. 取得含 `published_at` / `available_at` 的真實日線、官方 TWSE 交易日曆（含休市與未知日期）、含公告日與發放日的除權息資料，並以 `LineageAttestation` 逐項附證據。
2. 取得 `IR0001`／`0050.TW` 含息總報酬序列（或明確標示價格指數 `PRICE_ONLY` 僅供參考），並確認與策略相同交易日區間。
3. 取得歷史預測（含真實 `generated_at`、`available_at`、`trained_cutoff_date`、Walk-Forward 重訓練記錄），並禁止 Mock 模型。
4. 決定並固定無風險利率來源與年化規則；預先登錄回測假設，避免事後調參。
5. 補強多窗口 Walk-Forward 彙總、顯著性檢定與倖存者偏差處理後，才可考慮「真實歷史回測已驗證」之結論。

本階段完成後停止，等待獨立審查；未下載任何資料、未訓練模型、未執行參數最佳化。
