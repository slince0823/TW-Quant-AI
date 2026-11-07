# TW Quant AI — Taiwan Stock Quantitative Trading Platform
# 第二階段 D 實作報告：端到端整合測試與交易安全驗收 (PHASE2D_INTEGRATION_REPORT.md)

完成日期：2026-10-21  
狀態：完成  
負責模組：`src/tw_quant/gateway.py`、`src/tw_quant/broker/broker.py`、`tests/test_integration.py`

---

## 一、既有測試執行結果

在進行端到端整合測試前，已對專案既有之 66 項單元測試進行回歸執行與基線驗證：

| 測試檔案 | 測試項目數 | 執行結果 | 執行時間 | 備註 |
| :--- | :---: | :---: | :---: | :--- |
| `tests/test_signal.py` | 18 | 全部通過 (100%) | 0.001s | 統一交易訊號規格、單位防護、時序因果、去重防護 |
| `tests/test_paper_broker.py` | 26 | 全部通過 (100%) | 0.002s | 7態訂單狀態機、台股撮合規則、預留款券、逐日會計帳本 |
| `tests/test_risk_engine.py` | 22 | 全部通過 (100%) | 0.002s | 10道安全防線、最大回撤熔斷、Kill Switch 人工審計重設 |
| **既有測試小計** | **66** | **全部通過** | **0.005s** | 結果與第二階段 C 報告完全一致 |

---

## 二、新增整合測試數量與執行結果

新增測試檔案 [`tests/test_integration.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_integration.py)，涵蓋 8 大整合與安全情境，共新增 **24 個全面整合測試案例**：

```text
........................
----------------------------------------------------------------------
Ran 24 tests in 0.003s

OK
```

### 全平台測試匯總（累計 90 項測試）：
- `tests/test_signal.py`: 18/18 通過
- `tests/test_paper_broker.py`: 26/26 通過
- `tests/test_risk_engine.py`: 22/22 通過
- `tests/test_integration.py`: 24/24 通過
- **全平台總計：90 項單元與整合測試，通過率 100%，總執行時間 0.009 秒。**

---

## 三、端到端交易整合流程架構

```mermaid
flowchart TD
    Sig[1. AI 交易訊號 UnifiedTradeSignal] --> GW[2. 統一交易執行閘道 TradingGateway]
    
    subgraph Risk_Gate ["風控前置審查門禁 (RiskEngine)"]
        GW -->|建立帳戶快照| CTX[RiskContext\n(可用現金/持倉/未成交在途/基準NAV/損益)]
        CTX --> EVAL[RiskEngine.evaluate 10道防線]
        EVAL -->|未通過| REJ[拒絕委託\n產生拒單紀錄 / 0資金部位變動]
        EVAL -->|核准通過| PRE[原子鎖定在途承諾資金 commit_approval]
    end
    
    subgraph Execution_Sim ["高擬真撮合與會計 (TaiwanPaperBroker)"]
        PRE --> BRK[broker.submit_signal 建立委託]
        BRK --> REL[釋放風控在途暫存 release_committed]
        BRK --> OM[OrderManager 狀態機進入 ACCEPTED]
        BRK --> HLD[PortfolioLedger 凍結預留款券]
        
        MKT[市場時序 K 線 MarketBar] --> FE[FillEngine 模擬撮合]
        FE -->|時序因果/停牌/漲跌停/處置檢查| MTCH{是否符合撮合?}
        MTCH -->|否| WAIT[排隊中 / 保持開立]
        MTCH -->|部分/全部成交| FILL[推進狀態 PARTIALLY_FILLED / FILLED]
        
        FILL --> ACC[PortfolioLedger 會計精算]
        ACC --> DED[扣減預留款 / 差額返還可用現金 / 增加加權平均成本持倉]
    end
    
    subgraph Settlement ["收盤與估值結算"]
        CLOSE[process_daily_close 收盤作業] --> EXP[未成交開立中訂單全部作廢 expire / 退回預留款券]
        EXP --> MTM[mark_to_market 逐日收盤估值 / 計算當日 NAV 與回撤]
    end
```

---

## 四、發現的架構問題與隱患

在深度審查實際程式碼時，發現以下架構與安全問題：

1. **Broker 與風控引擎原始完全解耦（潛在繞過路徑）**：
   - `TaiwanPaperBroker` 在第二階段 B 設計為獨立模組，未強制依賴 `RiskEngine`。若調用端直接持有 `broker` 實例，可直接調用 `broker.submit_signal()`，導致 10 道風控防線被完全繞過。
2. **`SignalAction.CLOSE` 動作介面不一致**：
   - `UnifiedTradeSignal` 與 `RiskEngine` 均明確支援 `SignalAction.CLOSE` 作為減碼平倉；但 `TaiwanPaperBroker` 原先僅檢查 `if order.action == SignalAction.SELL:`，導致以 `CLOSE` 為動作的訊號進入 broker 後既未預留持倉亦無法撮合，狀態停滯在 `NEW`。
3. **無外部持久化層之重啟安全邊界**：
   - 目前 `RiskEngine` 與 `TaiwanPaperBroker` 均為記憶體物件。若進程重啟，歷史審計日誌、熔斷狀態與已處理之 `signal_id` 集合皆會重置。在未外接 Redis/資料庫持久化前，不可宣稱具備跨重啟之去重安全性。
4. **雙重審核重複訊號阻擋競態**：
   - 若由外部 Gateway 執行風控審核通過並將 `signal_id` 記錄至 `_approved_signals` 後，Broker 內部再次調用 `evaluate()`，會誤將同一筆合法在途訊號判定為 `DUPLICATE_SIGNAL`。

---

## 五、已修正缺陷與對應驗證測試

針對上述發現，已完成以下精準修復並附帶對應測試：

| 修正項目 | 修改檔案 | 修正內容 | 對應整合測試 |
| :--- | :--- | :--- | :--- |
| **統一交易執行閘道** | `src/tw_quant/gateway.py` | 新增 `TradingGateway` 類別，強制訊號必經 `RiskEngine` 審查，管理在途承諾資金之鎖定與釋放 | `test_A01`, `test_C01`, `test_G01` |
| **Broker 內建雙重風控防禦** | `src/tw_quant/broker/broker.py` | `TaiwanPaperBroker` 支援注入 `risk_engine` 與 `build_risk_context`。即便直接調用 broker，亦自動防禦風險委託 | `test_B02_bypass_prevention_on_broker_directly` |
| **`SignalAction.CLOSE` 支援** | `broker.py`, `engine.py` | 撮合引擎、手續費/證交稅計算、持倉預留與平倉記帳全面支援 `CLOSE` 動作 | `test_E04`, `test_C02` |
| **雙重審核白名單協同** | `src/tw_quant/broker/broker.py` | 若訊號已在同一原子通道被風控核准（`signal_id in _approved_signals`），Broker 不重複引發去重錯誤 | `test_A01`, `test_D01` |
| **重啟持久化邊界驗證** | `tests/test_integration.py` | 編寫測試驗證記憶體重啟後的狀態邊界，誠實記錄架構限制 | `test_D04_restart_dedup_persistence_limitation_tested` |

---

## 六、尚未解決的問題與架構邊界

1. **跨進程狀態持久化 (Persistence)**：
   - 目前全系統未導入 SQLite 或 Redis。若交易伺服器異常崩潰或重啟，記憶體中的未成交訂單、持倉與已處理訊號會遺失。需在下一階段規劃狀態持久化中介層。
2. **分散式並發鎖 (Distributed Locks)**：
   - 目前在途承諾資金（`_pending_committed_cash`）採用行程內 Python 標準資料結構，適用於單行程非同步/多執行緒架構。若未來採多進程分散式部署，需升級為分散式鎖。
3. **AI 選股模型輸出轉接管線 (Model-to-Signal Pipeline)**：
   - 目前 `UnifiedTradeSignal` 由測試資料集生成，尚未與 `qlib-tw-trader` 的預測分數進行自動轉換與下單排程串接。

---

## 七、資金與帳本一致性驗證結果

在 [`test_G01_global_ledger_invariants_across_complex_lifecycle`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_integration.py#L571-L605) 中，模擬了多標的、部分成交、撤單、再買入、平倉與收盤結算等複雜路徑，每個節點均通過以下嚴格斷言：

- **恆等式 1 (總權益)**：`total_equity == cash + reserved_cash + market_value` (誤差 0.00)
- **恆等式 2 (持倉數量)**：所有持倉標的 `total_shares == available_shares + reserved_shares`
- **恆等式 3 (訂單數量)**：所有開立中委託 `order_quantity == filled_quantity + remaining_quantity`
- **恆等式 4 (現金守恆)**：`cash` 與 `reserved_cash` 變動完全符合成交金額、手續費（最低 20 元）、證券交易稅與多退少補，無任何金額憑空產生或丟失
- **恆等式 5 (精度保障)**：全系統金額與股數計算 100% 為 `Decimal` 與整數，不存在浮點誤差

---

## 八、重啟後的狀態保存能力評估

- **評估結論**：**尚未實作跨重啟持久化**。
- **測試證明**：`test_D04_restart_dedup_persistence_limitation_tested` 驗證新建實例會以全新初始現金（200 萬）啟動，不繼承上一實例的已處理 `signal_id` 集合。
- **合規承諾**：本報告明確標示此項限制，不誇大宣稱已具備跨重啟恢復能力。

---

## 九、是否仍有繞過風控的路徑

1. **使用 `TradingGateway` 執行交易**：**0 繞過路徑**。所有訊號強制通過 10 道防線。
2. **使用注入 `risk_engine` 之 `TaiwanPaperBroker`**：**0 繞過路徑**。Broker 內部立單時自動構建 Context 並調用風控。
3. **單獨實例化未配置 `risk_engine` 之 `TaiwanPaperBroker`**：
   - 僅供離線單元測試模擬券商本身之撮合邏輯使用。
   - **實盤/整合環境安全規範**：系統必須強制透過 `TradingGateway` 作為唯一交易通道，嚴禁向策略層暴露裸 Broker 實例。

---

## 十、驗收狀態與進入下一階段評估

| 評估維度 | 驗收狀態 | 說明 |
| :--- | :---: | :--- |
| **統一交易訊號合約 (contracts)** | **已通過驗證** | 單位防護、時序因果、不可變性、去重邏輯完全驗證 |
| **高擬真模擬券商 (broker)** | **已通過驗證** | 7 態狀態機、漲跌停/停牌/處置撮合限制、加權成本會計帳本完全驗證 |
| **獨立交易風控引擎 (risk)** | **已通過驗證** | 10 道防線、熔斷鎖定、Kill Switch 人工審計日誌完全驗證 |
| **端到端交易執行閘道 (gateway)** | **已通過驗證** | 在途承諾預扣、款券預留、異常回滾與帳務恆等式完全驗證 |
| **跨重啟資料持久化** | **尚未實作** | 目前為純標準函式庫記憶體架構，待第三階段系統整合層補充 |
| **實盤券商連接** | **未啟用** | 維持安全閘門，0 連接真實券商，0 使用真實憑證 |
| **原始子模組狀態** | **已通過驗證** | `anti-gambling-trader-tw` 與 `qlib-tw-trader` 維持 100% 原始乾淨狀態 (0 改動) |

### 結論
第二階段（核心交易與風控引擎開發）的所有目標與驗收標準已全面達成，全平台 90 項離線測試通過率 100%。具備安全進入第三階段（AI 選股模型整合、特徵管線串接與離線回測分析）的條件。
