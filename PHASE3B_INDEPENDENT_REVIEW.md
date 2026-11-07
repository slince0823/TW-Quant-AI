# 第三階段 B StrategyEngine 獨立程式碼審查與第三階段 C 前置驗收報告 (PHASE3B_INDEPENDENT_REVIEW.md)

最後審查時間：2026-10-21  
審查標的：`src/tw_quant/strategy/` 及關聯交易閘道、風控與模擬券商模組  
審查角色：獨立量化系統與程式碼安全審查員  
驗收結論：**具備進入第三階段 C（歷史回測管線設計）之條件，但須嚴格落實 A 類前置整備項目**

---

## 一、專案現況與基準審核

### 1. 實際審查之核心檔案
* 策略核心：
  * [`src/tw_quant/strategy/engine.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/engine.py) (StrategyEngine 決策、選股、資金配置、時序與門禁)
  * [`src/tw_quant/strategy/config.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/config.py) (StrategyConfig 不可變設定與 CostModel 成本模型)
  * [`src/tw_quant/strategy/snapshot.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/snapshot.py) (PortfolioSnapshot, PositionSnapshot 唯讀快照)
  * [`src/tw_quant/strategy/decision.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/decision.py) (StrategyAction, StrategyReason, PositionTarget, StrategyResult)
  * [`src/tw_quant/strategy/dispatch.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/dispatch.py) (dispatch_signals 安全派送管線)
  * [`src/tw_quant/strategy/trading_calendar.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/strategy/trading_calendar.py) (TradingCalendar 協定與 StaticTradingCalendar)
* 關聯執行與風控模組：
  * [`src/tw_quant/gateway.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/gateway.py) (TradingGateway 原子鎖與門禁)
  * [`src/tw_quant/risk/engine.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/risk/engine.py) (RiskEngine 10 道防線與熔斷)
  * [`src/tw_quant/broker/broker.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/broker.py) (TaiwanPaperBroker 模擬撮合主體)
  * [`src/tw_quant/broker/ledger.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/broker/ledger.py) (PortfolioLedger 帳本與持倉記帳)

### 2. 原始子專案 Commit 與工作目錄狀態
經命令列獨立檢驗：
* **`anti-gambling-trader-tw`**：
  * HEAD Commit: `8609cef73718a056fddb74201fc304f174fa1553` (與審查基準 100% 一致)
  * 工作目錄：`clean` (零修改、零未追蹤檔案)
* **`qlib-tw-trader`**：
  * HEAD Commit: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` (與審查基準 100% 一致)
  * 工作目錄：`clean` (零修改、零未追蹤檔案)
* **實盤環境安全性**：
  * 無任何外部券商實盤 API 調用，無連線憑證讀取，全系統嚴格鎖定在 `SignalMode.PAPER`。

---

## 二、資金配置審查結論

| 審查項目 | 審查結果 | 具體程式碼依據與量化分析 |
| :--- | :---: | :--- |
| **1. 等權重配置是否符合目標權重** | **符合（受離散股數截斷）** | `target_value = _q2_floor(nav * eff_ratio)`，每檔目標市值相同。但實際買進股數向下取整至 `share_unit`（如一張 1,000 股），故高價股（如台積電 1,000 元，一張 100 萬）在小資金帳戶中可能因超出單檔預算而向下取整為 0 股，產生離散截斷偏差。 |
| **2. 股票輸入順序對配置之影響** | **完全無影響（具確定性）** | 程式碼 line 268 `ranked = sorted(batch.predictions, key=lambda p: p.rank)`，候選標的強制依 AI 預測 rank 升序處理，且 `PredictionBatch.validate()` 強制要求 rank 唯一且連續（1..N），輸入清單的順序被完全重排，保證確定性。 |
| **3. 逐筆扣減資金之順序偏差** | **存在順序優先級（刻意設計）** | Line 376 `for cand in sorted(buy_candidates, key=lambda c: c.pred.rank):`。當可用資金不足以買齊所有目標檔數時，優先滿足 rank 1、2 等高確信度標的，次要名次將被減額買進或跳過（`INSUFFICIENT_CASH`）。這在資金緊縮時會造成「高名次配置比重高於低名次」的非嚴格等權現象。 |
| **4. 交易成本估算與 PaperBroker 一致性** | **完全一致** | `CostModel` 經由 `matches_paper_config` 嚴格比對。買進預留金額（價格 × (1+滑價率) × 股數 + 手續費）與 `TaiwanPaperBroker._submit_buy_order` 採用的公式和 Decimal 四捨五入完全同構（見 `test_27b`）。 |
| **5. 手續費低消與折扣** | **完全正確** | 手續費計算為 `round(notional * 0.001425 * 0.6)`，並與 `min_commission = 20` 取最大值（四捨五入至元）。完全符合台灣證券市場常規券商 6 折牌告費率。 |
| **6. 買賣方向及交易稅** | **完全正確** | 買進不課稅；賣出股票課徵 0.3%（無條件捨去至元），ETF（00 開頭）課徵 0.1%。未納入當沖降稅 0.15%（因本策略為隔日以上波段，禁止當沖）。 |
| **7. 股數取整後突破現金/持倉限制** | **嚴格杜絕（雙重迴圈保護）** | Line 585-596 之 `while shares > 0:` 迴圈：在向下取整後，持續以含滑價及手續費之 `total` 檢驗 `total <= cash_budget`、`notional <= desired_value` 及 `order_cap`，若有超額則逐次扣減一個 `unit`，絕不超買。賣出減碼則強制 `shares <= pos.quantity - 1`，絕不超賣。 |
| **8. 最低現金比例與單檔上限** | **同時生效（取嚴格交集）** | `_limits()` 中同時考量 `StrategyConfig` 與 `RiskConfig`：`eff_ratio = min(target, single_cap)`，且 `cash_left = cash - nav * min_cash_ratio`。兩者同時拘束資金配置。 |
| **9. 資金不足時產生負現金風險** | **絕對不可能** | 策略端預留餘額檢核 `total <= max(cash_left, 0)`；下游 `TradingGateway` 執行 `RiskEngine.check_cash_reserve`；最底層 `TaiwanPaperBroker` 凍結帳本現金時若 `cash < required` 立即拒單。三層防禦徹底防杜負現金。 |
| **10. 賣出所得未計入買進預算** | **符合保守風控設計** | 策略明確標註不計入同批賣出所得。因台股採 T+2 交割制度，盤前送出之賣單當下尚未成交亦未結算，若預先透支賣出所得買進，一旦盤中賣單遇跌停鎖死未成交，將引發違約交割（穿倉）。此保守設計完全合理。 |

---

## 三、交易語意與時序審查結論

| 審查項目 | 審查結果 | 具體依據與潛在漏洞分析 |
| :--- | :---: | :--- |
| **1. BUY / SELL / CLOSE 語意** | **語意清晰嚴格隔離** | `CLOSE` 專指持有期滿或被剔除組合時之清倉（股數 = 全部持股）；`SELL` 專指組合再平衡時之部分減碼（股數 < 全部持股）；`HOLD` 與 `SKIP` 僅記載審計軌跡，絕不轉發成券商委託。 |
| **2. 部分減倉與全部平倉** | **正確無誤** | `_trim_intent` 設有保險 `min(raw, pos.quantity - 1)`，確保減碼後帳戶必定留有至少 1 股，絕不會將部位誤平倉。 |
| **3. 訊號建立、進場與出場時間** | **時間鏈因果嚴謹** | 訊號 `created_at = decision_time`，`expires_at = 次日 13:30`。要求 `data_cutoff <= generated_at <= decision_time < expires_at`，時間遞增因果完整。 |
| **4. 持有期限計算** | **依交易日曆計算** | 呼叫 `calendar.trading_days_between(pos.entry_date, planned)`，計算區間交易日數（不含進場日，含預定出場日），精確符合 T+N 交易日要求。 |
| **5. 收盤後訊號以同日收盤價成交** | **不可能（防前視閘門阻擋）** | `_gate()` 檢查：預定進場日 `planned` 必須嚴格等於 `calendar.next_trading_day(cutoff_date)`，且 `as_of_date == planned`。盤後產生的訊號絕無法在當日撮合成交。 |
| **6. 未來資料洩漏** | **嚴格防禦** | 策略門禁會阻擋未來時間戳記之快照 (`snapshot.as_of > decision_time`) 與參考價格 (`prices_as_of > decision_time`)。持倉之 `entry_date` 亦不得晚於快照日期。 |
| **7. 交易日曆來源** | **日曆外部注入** | `StrategyEngine` 本身無內嵌日曆，強制由外部注入 `TradingCalendar`。目前測試使用 `StaticTradingCalendar`，已明文禁止混淆中國與台灣休市日。 |
| **8. 重複訊號抑制** | **安全無誤** | 去重鍵包含 `(strategy_name, version, action, symbol, planned_date)` 之 SHA-256。同日同方向重複生成會被抑制；但不同日期或不同動作（BUY vs SELL）具有不同雜湊鍵，不會誤刪合法交易。 |
| **9. 模擬成交價格與實際資料欄位** | **完全對齊** | 限價單以參考價報出；市價單不設限價，模擬撮合時以開盤價加減滑價成交，與 `MarketBar` 欄位一致。 |

---

## 四、AI 預測與資料品質審查結論

| 審查項目 | 審查結果 | 關鍵分析與潛在風險說明 |
| :--- | :---: | :--- |
| **1. Mock 模型進入正式交易流程** | **程式碼層面完全阻斷** | `PredictionMetadata.validate()` 雙重校驗：`is_mock=False` 嚴禁包含 `MOCK` 關鍵字；`StrategyEngine._gate()` 於生產模式下見 `is_mock=True` 一律拋出 `MOCK_MODEL_NOT_ALLOWED`。 |
| **2. failed_symbols / missing_features** | **正確跳過並記錄** | 缺失股票一律轉為 `StrategyAction.SKIP` 並註明原因，持倉股遭遇缺失則保持 `HOLD`（不因推論伺服器臨時斷線而恐慌平倉）。標的同時出現在預測與失敗清單時整批拒絕 (`INCONSISTENT_BATCH`)。 |
| **3. 預測分數與排名矛盾** | **整批嚴格拒絕** | `_gate()` 逐項檢查相鄰 rank 之 score 是否嚴格非遞增，若發現 `lo.score > hi.score` 即拒絕該批預測 (`RANK_SCORE_INCONSISTENT`)。 |
| **4. 缺失資料靜默補零** | **完全杜絕** | 快照缺少持倉價格拒絕建立快照；特徵缺失誠實記載；未涵蓋標的拒絕給予 0 分或均價。 |
| **5. 存活者偏差 (Survivorship Bias)** | **⚠️ 存在潛在風險 (資料層)** | `StrategyEngine` 本身不產生存活者偏差，但下游若直接使用「當前台股 50 / 中型 100 成分股」回溯至 2018 年回測，將遺漏歷史下市或降級個股。需在第三階段 C 導入時點截面標的池 (Point-In-Time Universe)。 |
| **6. 除權息還原方式造成錯誤報酬** | **⚠️ 存在實質缺口 (會計層)** | 目前 `PortfolioLedger` 與 `StrategyEngine` 未處理配息與配股。個股除息時股價直接下跌，若未補償現金股利，帳面 NAV 將出現假性虧損，甚至可能誤觸風控日虧損限額。 |
| **7. 歷史資訊的時間截點 (Point-In-Time)** | **⚠️ 需在回測資料端嚴格要求** | 財報公佈日與財報所屬季度存在時滯（例如 Q1 財報最晚 5/15 公佈），若資料庫將 3/31 財報直接對齊至 3/31 交易日，即構成嚴重前視洩漏。此要求必須在第三階段 C 的特徵管線中強制校驗。 |

---

## 五、測試驗證結果 (全六大測試套件)

### 執行命令
```powershell
pytest -v tests/test_signal.py tests/test_paper_broker.py tests/test_risk_engine.py tests/test_integration.py tests/test_prediction.py tests/test_strategy_engine.py
```

### 實際執行輸出確認
* **測試收集數**：156 個測試案例、50 個 subtests
* **通過數**：**156 passed, 50 subtests passed**
* **失敗數**：**0 failed**
* **跳過數**：**0 skipped**
* **耗時**：0.23 秒

---

## 六、發現問題分級與分類清單

依據任務要求，將本次審查與驗收發現之架構問題、實施缺口及潛在風險分為 A、B、C 三類：

### A 類：必須在進入正式歷史回測前修正/補齊之問題 (Must Fix Before Backtest)

#### A-1. 持倉進場日追蹤缺失 (Entry Date Tracking in Backtest Loop)
* **問題描述**：
  `PositionSnapshot.entry_date` 為判斷持有期是否屆滿 (`holding_trading_days`) 的唯一依據。但 `TaiwanPaperBroker` 內部的 `PortfolioLedger` 只以加權平均成本法記錄持倉數量，**並不儲存各筆持倉的買進成交日期**。
* **重現案例**：
  若呼叫端直接呼叫 `PortfolioSnapshot.from_broker(broker, ..., entry_dates={})`（如 `test_strategy_engine.py` line 700），所有持倉的 `entry_date` 均為 `None`，`_expiry_state()` 一律回傳 `UNKNOWN`，固定天數到期出場邏輯將完全失效！
* **嚴重程度**：**高 (High)**
* **建議處置方案**：
  在第三階段 C 建立回測控制器（Backtest Controller）時，必須建立一個專門維護 `current_positions_entry_date: Dict[str, dt.date]` 的追蹤結構，每次 `BUY` 訊號成交時記錄日期，每次 `CLOSE` 時清除，並於每日生成快照時傳入 `from_broker`。

#### A-2. 除權息與股利缺口 (Ex-Dividend / Corporate Action Cash Credit)
* **問題描述**：
  台股每年 6~9 月為除權息高峰，個股在除息日開盤價將直接扣除現金股利。若回測環境直接使用真實未還原收盤價進行帳本估值，而帳本未將現金股利補入可用現金，帳戶 NAV 將產生無中生有的斷崖式下跌，導致虛假最大回撤（Drawdown），甚至誤觸 `RiskEngine` 熔斷。
* **嚴重程度**：**高 (High)**
* **建議處置方案**：
  在進入第三階段 C 進行長週期（跨年度）回測前，必須於資料管線中建立除權息日曆與現金股利配發資料表。當持倉股遭遇除息日，回測引擎必須在開盤前將股利現金入帳至帳本（`cash += shares * dividend_per_share`）。

---

### B 類：可在第三階段 C 實裝處理之問題 (Address in Phase 3C)

#### B-1. 真實台股交易日曆注入 (TWSE Official Trading Calendar)
* **問題描述**：
  目前測試均使用週一至週五合成日曆。真實台股具備長假補班、颱風停市、農曆新年封關提前休市（通常封關日後仍有 2 個工作日僅結算不交易）等特殊日曆規則。
* **嚴重程度**：**中 (Medium)**
* **建議處置方案**：
  在第三階段 C 實作 `TwseTradingCalendar` 類別，讀取 TWSE 歷年官方休市日資料檔，注入給 `StrategyEngine` 與回測主循環。

#### B-2. 動態市場衝擊與成交量限制 (Dynamic Market Impact & Volume Cap)
* **問題描述**：
  當前模擬券商採用靜態滑價（0.05%），且允許市價單全數成交（除非停牌）。在台股中小型股中，若單筆委託量超過該檔個股當日成交量的 5%~10%，將產生嚴重的市場衝擊成本。
* **嚴重程度**：**中 (Medium)**
* **建議處置方案**：
  在第三階段 C 撮合引擎中，加入成交量佔比上限檢查（例如單根 Bar 最多撮合該 Bar 總成交量之 10%），其餘部分順延或取消。

#### B-3. 歷史資料時點對齊與特徵點防護 (Point-in-Time Data Alignment)
* **問題描述**：
  季報、月營收資料必須嚴格依據實際發布日（公告時點）進行截面特徵計算，嚴禁依據財報季度截止日（如 3/31 財報於 5/15 才公告）。
* **嚴重程度**：**中 (Medium)**
* **建議處置方案**：
  在 Qlib 特徵工程轉換為自研 `PredictionBatch` 時，核對特徵產出時間戳記與公告日。

---

### C 類：暫不影響研究但必須記錄之限制 (Documented Limitations)

#### C-1. 換股時的資金周轉時滯 (Turnover Cash Buffer Requirement)
* **限制說明**：
  策略引擎為求安全，嚴禁透支同批未成交的賣出所得。因此，當投資組合滿載且當日欲進行成分股換股時，買單會因資金被現有持倉佔用而無法於當日簽出，必須等 T 日賣單成交、T+1/T+2 資金到位後才能補買新股票。
* **影響評估**：
  在回測中會觀察到 1 個交易日的持倉空窗期，此為台股無融資券現股交割之正常現象。

#### C-2. 資金不足時高確信度優先配置 (Rank-Prioritized Allocation)
* **限制說明**：
  在資金有限的情況下，等權重配置會退化為「高排名標的拿足 100% 預算，低排名標的減額或為 0」，而非全體等比例等權縮小。

#### C-3. 僅支援現股多頭 (Long-Only Equity)
* **限制說明**：
  目前策略引擎不支援融券賣出或期貨避險，在台股系統性空頭行情中無法建立空頭保護。

---

## 七、第三階段 C 前置驗收結論

經獨立審查，第三階段 B 的 `StrategyEngine` 在軟體工程品質、防禦性程式設計、時序前視隔離與風控門禁整合上**完全合規且結構健全**：

1. **模組純度**：全套策略程式碼無任何 ML/pandas 黑盒子依賴，維持標準庫純粹性。
2. **門禁嚴密**：策略無任何繞過 `RiskEngine` 直達 Broker 的管道，完全透過 `TradingGateway`。
3. **測試真實度**：全系統 156 項測試全部為真實斷言執行，測試耗時 0.23 秒，無偽造通過。
4. **雙專案乾淨度**：`anti-gambling-trader-tw` 與 `qlib-tw-trader` 均維持原始基線 Commit 且工作目錄完全乾淨。

### 驗收判定：
> [!IMPORTANT]
> **本專案具備進入第三階段 C（回測引擎設計與離線回測管線建立）之條件**。  
> 進入第三階段 C 時，應優先落實 **A-1（回測控制器之進場日追蹤結構）** 與 **A-2（除權息現金股利補償機制）**，以確保回測數值之真實度與會計正確性。

---

審查完成，已主動停止，未啟動回測引擎，未連接實盤券商。
