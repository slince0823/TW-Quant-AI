# TW Quant AI — Taiwan Stock Quantitative Trading Platform
# 第二階段 C 實作報告：RiskEngine 獨立交易風控引擎 (PHASE2C_IMPLEMENTATION_REPORT.md)

完成日期：2026-10-21  
狀態：完成  
負責模組：`src/tw_quant/risk/`、`tests/test_risk_engine.py`

---

## 一、開發總結與核心原則遵循

本階段已成功實作符合台股交易特性之獨立交易風控閘門 `RiskEngine`，並完成涵蓋 10 道安全防線、最大回撤熔斷、人工緊急中斷（Kill Switch）、在途原子並發鎖定與單元測試套件。

全實作嚴格遵循各項安全與架構原則：
1. **純 Python 標準函式庫**：無任何外部重型第三方依賴（無引入 `qlib`、`LightGBM`、`pandas` 等）。
2. **高精度貨幣計算**：資金金額、比例運算、保證金與保留款全程採用 `decimal.Decimal` 與 `ROUND_HALF_UP`，完全杜絕浮點累加誤差。
3. **職責分離**：`RiskEngine` 僅進行唯讀風控審查與在途額度鎖定，絕不直接下單、不擅自修改 `PortfolioLedger`、不調用任何券商 API。
4. **原始代碼 0 改動**：`anti-gambling-trader-tw`（commit `8609cef`）與 `qlib-tw-trader`（commit `774b3fb`）均保持 100% 原始乾淨狀態。
5. **安全閘門制度**：完全禁止真實下單，測試全程離線進行。

---

## 二、新增與修改檔案清單

| 檔案路徑 | 模組職責 | 關鍵型別 / 函式 |
| :--- | :--- | :--- |
| `src/tw_quant/risk/config.py` | 風控參數不可變配置 | `RiskConfig` (單筆上限、單檔持倉比、總持股比、最低現金保留比、日虧損比、最大回撤比、黑名單、是否允許放空、滑價與手續費預估參數) |
| `src/tw_quant/risk/decision.py` | 風控審查結果與拒絕代碼標準合約 | `RiskRejectCode` (13 種枚舉代碼)、`RiskCheckItem`、`RiskDecision` (`allowed`, `reason_codes`, `checks`, `is_risk_reducing`, `to_dict()`) |
| `src/tw_quant/risk/context.py` | 唯讀帳戶與市場現況快照 | `RiskContext` (現金、在途保留現金、持倉、未成交買單、市場估值、停牌名單、基準 NAV、當日已實現/未實現損益、歷史最高 NAV) |
| `src/tw_quant/risk/engine.py` | 10 道防線檢核核心與在途鎖定機制 | `RiskEngine` (`evaluate()`, `commit_approval()`, `release_commitment()`, `activate_kill_switch()`, `deactivate_kill_switch()`, `reset_circuit_breaker()`) |
| `src/tw_quant/risk/__init__.py` | 套件公開介面匯出 | 匯出上述所有風控類別與代碼 |
| `tests/test_risk_engine.py` | 風控引擎單元測試套件 | 22 個測試案例（涵蓋 10 道防線、並發鎖定、熔斷重設日誌、費用精算等） |
| `PROJECT_MAP.md` | 專案導航與全景地圖更新 | 更新第二階段 C 完成狀態與架構圖 |

---

## 三、10 道安全防線檢核規格

`RiskEngine.evaluate(signal, context)` 依序執行以下 10 道安全防線檢核：

| 防線編號 | 安全防線名稱 | 檢核標的與邏輯 | 拒絕代碼 (`RiskRejectCode`) | 賣出平倉例外處理 |
| :---: | :--- | :--- | :--- | :--- |
| **1** | **訊號合法性與時效** | 呼叫 `signal.validate()`，並檢查 `signal.is_expired(as_of=current_time)` | `SIGNAL_INVALID`, `SIGNAL_EXPIRED` | 格式錯誤或過期一律拒絕 |
| **2** | **標的狀態與黑名單** | 檢查是否在 `context.halted_symbols` 或 `config.restricted_symbols` | `SYMBOL_HALTED`, `SYMBOL_RESTRICTED` | 停牌無法交易一律拒絕 |
| **3** | **單筆委託金額上限** | 買入預估總支出（含滑價與手續費）是否大於 `config.max_order_amount` | `SINGLE_ORDER_LIMIT_EXCEEDED` | 賣出不適用 |
| **4** | **單檔持倉比例上限** | （既有部位市值 + 在途買單 + 本筆買單）/ NAV 是否大於 `config.max_single_position_ratio`（預設 10%） | `SINGLE_POSITION_LIMIT_EXCEEDED` | 賣出為減碼，不觸發 |
| **5** | **總投資曝險上限** | （全體持股市值 + 全體在途買單 + 本筆買單）/ NAV 是否大於 `config.max_total_exposure_ratio`（預設 80%） | `TOTAL_EXPOSURE_LIMIT_EXCEEDED` | 賣出為減碼，不觸發 |
| **6** | **最低現金保留水準** | 買入後剩餘可用現金（扣除在途原子承諾款）是否小於 NAV * `min_cash_reserve_ratio`（預設 20%） | `INSUFFICIENT_CASH_RESERVE` | 賣出為增加現金，不觸發 |
| **7** | **每日累計虧損上限** | 當日累計虧損（已實現 + 未實現）/ `baseline_nav` 是否達到 `max_daily_loss_ratio`（預設 2%） | `DAILY_LOSS_LIMIT_EXCEEDED` | **降低風險之平倉賣出例外放行** |
| **8** | **最大回撤熔斷機制** | 累計回撤 `(peak_nav - current_nav) / peak_nav` 是否達到 `max_drawdown_limit`（預設 15%）。觸發時自動啟動熔斷 | `CIRCUIT_BREAKER_ACTIVE` | **降低風險之平倉賣出例外放行** |
| **9** | **重複訊號防護** | 檢查 `signal.signal_id` 是否已存在於 `_approved_signals` 或 `_rejected_signals` | `DUPLICATE_SIGNAL` | 一律拒絕重複送審 |
| **10** | **緊急中斷 (Kill Switch)** | 檢查系統是否處於手動/外部中斷狀態 | `KILL_SWITCH_ACTIVE` | **降低風險之平倉賣出例外放行** |

### 特殊機制：賣出與降低風險（Risk-Reducing）放行原則
當委託為賣出（`SELL` / `CLOSE`）且數量不超過可用現貨持倉時，標記為 `is_risk_reducing = True`。在此狀態下：
1. 即使系統觸發「每日虧損超標」、「最大回撤熔斷」或「緊急停止 Kill Switch」，**平倉減碼賣單均依規放行**，確保系統具備自發去槓桿與部位變現止血之能力。
2. 若賣出數量超過可用持倉（且 `allow_short_selling = False`），則判定為裸放空，立即攔截並回傳 `SHORT_SELLING_DISALLOWED`。

---

## 四、熔斷與 Kill Switch 審計與人工重設機制

為避免演算法模型「球員兼裁判」自行關閉風控，本系統實施嚴格的審計與人工授權機制：

1. **不可由 AI 訊號重設**：
   - 熔斷與 Kill Switch 狀態完全與交易訊號流隔離，即便 AI 訊號夾帶指令亦無法變更狀態。
2. **人工授權具名重設**：
   - `RiskEngine.reset_circuit_breaker(authorized_by, reason)`：需傳入授權者與原因。
   - `RiskEngine.deactivate_kill_switch(authorized_by, reason)`：需傳入授權者與原因。
3. **不可篡改之審計軌跡**：
   - 所有緊急啟動、解除、重設均記入 `RiskEngine.audit_log`，包含操作類型、授權人員、原因說明與 UTC ISO 時間戳記。

---

## 五、在途並發預扣防護 (Atomic Pre-commitment)

為解決高頻或連續訊號情境下，多筆委託同時到達突破現金保留限制之競態問題（Race Condition）：
1. 風控審核通過後，調用 `RiskEngine.commit_approval(signal, required_cash, shares)`，在風控內部預扣在途資金（`_pending_committed_cash`）。
2. 下一筆訊號送審時，可用現金需扣除在途已核准但尚未成交之款項。
3. 當訂單於券商端完全成交或取消時，調用 `release_commitment()` 釋放預扣額度，完美防禦並發超額委託。

---

## 六、測試套件執行結果

本階段建立之測試套件 `tests/test_risk_engine.py` 涵蓋 22 項測試案例，全部順利通過：

```text
Ran 22 tests in 0.002s
OK
```

### 全平台測試匯總（累計 66 項測試）：
- `tests/test_signal.py`: 18/18 通過 (100%)
- `tests/test_paper_broker.py`: 26/26 通過 (100%)
- `tests/test_risk_engine.py`: 22/22 通過 (100%)
- **總計：66 項離線單元測試，通過率 100%，執行時間 0.005 秒。**

---

## 七、邊界限制與後續階段銜接規劃

1. **行程內記憶體限制**：
   - 目前 `RiskEngine` 之在途承諾資金與審計日誌保存在記憶體中。若未來進行跨行程（Multi-process）部署，需接入 Redis 或共享記憶體做分散式鎖定。
2. **行情與快照提供者（Context Provider）**：
   - 目前 `RiskContext` 由呼叫端傳入，下一階段（端到端整合）需實作由 `PortfolioLedger` 自動匯出 `RiskContext` 之轉接器。
3. **AI 模型接入**：
   - 目前尚未接入 `qlib-tw-trader`，待第三階段串聯資料管線與預測服務。

---

## 八、原始專案狀態檢查

```text
anti-gambling-trader-tw (8609cef): working tree clean (0 改動)
qlib-tw-trader (774b3fb): working tree clean (0 改動)
```
確認兩個原始模組完全未被污染或修改。
