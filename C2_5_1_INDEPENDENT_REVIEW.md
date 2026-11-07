# 第三階段 C-2.5.1-R 獨立技術複核報告 (Independent Technical Audit)

> **審查日期**：2026-10-21  
> **審查任務**：C-2.5.1-R — PIT 防前視與資料血統修補獨立複核 (Read-Only Technical Audit)  
> **複核目標**：針對 C-2.5.1 所修補之 FINDING-01（預測 PIT 時序因果）與 FINDING-02（資料血統與獨立證據驗證）進行靜態程式碼、邊界情境、架構因果與可重現反例之獨立審查。  
> **基線狀態**：
> - 全系統測試：260 / 260 Passed
> - `anti-gambling-trader-tw`: `8609cef73718a056fddb74201fc304f174fa1553` (Clean)
> - `qlib-tw-trader`: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` (Clean)
> - 本次任務遵守嚴格限制：**不修改任何程式碼、不修改任何測試、不修改 `PROJECT_MAP.md`、不下載外部資料、不訓練模型**。
> 
> **重要聲明**：  
> **本次審查為唯讀靜態複核。測試全數通過並不代表所有前視偏差均已消除，亦絕對不代表真實台股歷史回測績效已獲驗證。嚴禁將目前的測試輸出宣稱為真實策略有效性或實盤依據。**

---

## 目錄

1. [審查範圍與未能檢查的部分](#一審查範圍與未能檢查的部分)
2. [FINDING-01（PIT 因果驗證器）深度複核](#二finding-01pit-因果驗證器深度複核)
3. [FINDING-02（資料血統與獨立證據驗證）深度複核（含 10 大問題解答）](#三finding-02資料血統與獨立證據驗證深度複核)
4. [具體可重現的漏洞案例 (Reproducible Vulnerabilities)](#四具體可重現的漏洞案例)
5. [測試覆蓋率與測試品質缺口分析](#五測試覆蓋率與測試品質缺口分析)
6. [訓練標籤洩漏與 Walk-Forward 尚未解決之架構問題](#六訓練標籤洩漏與-walk-forward-尚未解決之架構問題)
7. [發現分級清單 (Findings Severity Matrix)](#七發現分級清單)
8. [每項問題之具體修正建議](#八每項問題之具體修正建議)
9. [是否建議進入 C-2.5.2 修補步驟](#九是否建議進入-c-252-修補步驟)
10. [是否具備開始真實資料接入之前置條件](#十是否具備開始真實資料接入之前置條件)

---

## 一、審查範圍與未能檢查的部分

### 1.1 複核範圍 (Audited Scope)
本次審查以靜態原始碼推演、時序變數追蹤、型別轉換邊界檢驗與 Python 互動反例驗證，深入檢查以下模組：
1. **修補核心檔案**：
   - [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)：
     - `verify_prediction_consumption_pit` 時序檢驗函式。
     - `EvidenceVerificationResult` 與 `IndependentEvidenceVerifier` 協定。
     - `DatasetChecksumVerifier` 與 `MockIndependentEvidenceVerifier` 實作。
     - `BacktestReport.build` 閘門控制與反序列化 `from_json` 雜湊比對。
2. **上游契約與主迴圈關聯**：
   - [`src/tw_quant/backtest/prediction_feed.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/prediction_feed.py)：`PredictionConsumptionRecord`、`WalkForwardWindowSpec`、消費追蹤。
   - [`src/tw_quant/prediction/contract.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/prediction/contract.py)：`PredictionMetadata`、`PredictionBatch`、`StockPrediction`。
   - [`src/tw_quant/backtest/runner.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/runner.py)：12 步因果時序、主迴圈消費紀錄彙整。
3. **測試套件**：
   - [`tests/test_backtest_metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py)：既有測試與新增之 12 項 C-2.5.1 修補測試。

### 1.2 未能檢查與待驗證項目 (Out of Scope / Pending Verification)
1. **真實資料儲存層之 I/O 與效能**：目前無真實台股 Parquet/CSV，無法檢驗大檔案磁碟讀取時計算 SHA-256 的記憶體與 I/O 表現。
2. **分散式與離線特徵管線之訓練歷史紀錄**：Qlib LightGBM 訓練紀錄檔（MLflow / TensorBoard / pkl 模型檔）的真實二進位格式尚未納入驗證範疇。

---

## 二、FINDING-01：PIT 因果驗證器深度複核

### 2.1 A. 欄位完整性檢核
檢查 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L238-L255)：
```python
gen_at = getattr(record, "generated_at", None)
avail_at = getattr(record, "available_at", None)
dec_cut = getattr(record, "decision_cutoff_time", None)
dec_date = getattr(record, "decision_date", None)
data_cut = getattr(record, "data_cutoff_time", None)
trained_cut = getattr(record, "trained_cutoff_date", None)

if gen_at is None: return False, f"批次 {bid} 缺少 generated_at 時間戳"
if avail_at is None: return False, f"批次 {bid} 缺少 available_at 時間戳"
if dec_cut is None: return False, f"批次 {bid} 缺少 decision_cutoff_time 時間戳"
if dec_date is None: return False, f"批次 {bid} 缺少 decision_date 決策日"
if trained_cut is None: return False, f"批次 {bid} 缺少 trained_cutoff_date 模型訓練截止日"
```
- **審查結論**：`generated_at`、`available_at`、`decision_cutoff_time`、`decision_date`、`trained_cutoff_date` 皆具備強制非空檢驗。
- **發現缺口 (MEDIUM)**：`data_cut` (`data_cutoff_time`) 雖被讀取，但在必要性檢查中被遺漏（第 268 行為 `if data_cut is not None:`）。若上游傳入 `data_cutoff_time=None`，驗證器不會報錯，造成特徵資料截止時間可能遺失卻依然判定通過。

### 2.2 B. 時區與型別處理（重大邊界漏洞發現）
檢查第 258-297 行與第 299-308 行：
1. **Naive Datetime 攔截**：
   - 程式明確檢查 `val.tzinfo is None or val.tzinfo.utcoffset(val) is None`，naive datetime 均被正確拒絕，判定合格。
2. **Timezone-aware Datetime 比較**：
   - `avail_at < gen_at` 與 `avail_at > dec_cut` 使用 Python 內建感知比較，正常運作。
3. **跨時區日期截斷漏洞 (HIGH)**：
   - 檢查第 302 行：
     ```python
     if isinstance(trained_cut, dt.datetime):
         ...
         t_pure_date = trained_cut.date()
     ```
   - **重大錯誤**：若 `trained_cutoff_date` 為 UTC 或非台北時區之 `datetime`，調用 `.date()` 取得的是**該時區之本地日期**，而非**台北市場時間 (Asia/Taipei) 之日期**！
   - **可重現漏洞**：若決策日為 `2025-01-06`，模型在 UTC 時間 `2025-01-05 17:00:00 UTC` 訓練完成。在台灣時間，此時戳已是 `2025-01-06 01:00:00+08:00`（即決策日當日清晨）。然而 `trained_cut.date()` 產出 `2025-01-05`，使 `t_pure_date < d_pure_date` 判斷為 True！即使模型在當日完成訓練，依然繞過了「訓練截止日等於決策日應保守拒絕」之防禦規則！（見本報告第四章案例 1）。

### 2.3 C. 預測可用時間因果檢核
檢查第 273-283 行：
- `data_cut > gen_at` $\to$ 拒絕（合格）。
- `avail_at < gen_at` $\to$ 拒絕（合格）。
- `avail_at > dec_cut` $\to$ 拒絕（合格，保證決策時點前預測已公開可用）。

### 2.4 D. 訓練截止時間因果檢核
檢查第 310-316 行：
- `t_pure_date > d_pure_date` $\to$ 拒絕（合格，防未來資料）。
- `t_pure_date == d_pure_date` $\to$ 保守拒絕（合格，落實契約要求）。
- 缺欄位時第 254 行已提前攔截，無預設通過路徑。

### 2.5 E. 驗證器是否真正接入最終結論
追蹤 [`BacktestReport.build`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L372-L385)：
- 若任一預測記錄未通過，`pred_pit_ok = False`。
- `pred_status = LineageStatus.REAL_UNVERIFIED`。
- 第 451 行 `all_attested = all(st == LineageStatus.REAL_PIT_ATTESTED for st in sources.values())` 變為 `False`。
- 第 462 行無法進入 `REAL_HISTORICAL_PIT_VERIFIED` 分支，強制降級為 `REAL_HISTORICAL_NOT_VERIFIED`。
- **結論**：驗證器已 100% 串接至最終判定閘門，未通過因果之預測絕不可能被標記為真實歷史已驗證。

### 2.6 F. 訓練標籤洩漏與 Walk-Forward 未解之架構限制
經深入檢查 `PredictionMetadata`、`StockPrediction` 與 `PredictionConsumptionRecord`：
1. **標籤跨期未建模**：目前契約僅包含 `trained_cutoff_date`，未包含 `label_horizon_days`（預測標籤跨越天數）或 `label_cutoff_date`。若模型訓練特徵截止於 1 月 3 日，但使用未來 10 日報酬作為標籤（標籤價格涵蓋至 1 月 17 日），在 1 月 6 日進行決策時，因 `2025-01-03 < 2025-01-06`，驗證器判定通過，但實質上模型已看過未來價格標籤！
2. **Purging 未實作**：契約完全缺少樣本去重疊 (Purging) 欄位與審計日誌。
3. **Embargo 孤立於主迴圈外**：雖然 `prediction_feed.py` 具備 `WalkForwardWindowSpec`，但主迴圈與報告層級未強制要求傳入，多數情況下 Embargo 未被觸發。
- **結論**：本項為**重大架構未解風險**，目前絕不能宣稱「所有訓練前視與標籤洩漏已被消除」。

---

## 三、FINDING-02：資料血統與獨立證據驗證深度複核

針對任務要求之 10 大問題進行逐項獨立剖析：

### 1. `DatasetChecksumVerifier` 是否實際讀取資料內容並計算 SHA-256？
**【否】**。檢查 [`report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L141-L168)：
```python
mkt_src = result.market_data_provenance.source
expected_mkt_hash = self.expected_manifest_hashes.get(mkt_src)
if not expected_mkt_hash:
    failures.append(f"行情資料來源 {mkt_src} 缺少已登記之公證指紋雜湊")
else:
    details["market_data_manifest_hash"] = expected_mkt_hash
```
驗證器**完全沒有開啟或讀取磁碟上的任何檔案**，亦未針對 Bar、Tick 或 CSV 執行串流雜湊計算。它僅檢查 `expected_manifest_hashes` 字典中是否存在以 `source` 字串為 key 的項目。

### 2. 它比較的指紋清單從哪裡載入？
**【由呼叫端於記憶體建構時傳入】**。指紋清單為 `DatasetChecksumVerifier(expected_manifest_hashes=...)` 之建構參數，未對接任何受保護的官方配置檔或公證伺服器。

### 3. 如果呼叫端自行建立指紋清單，系統是否仍可能錯誤宣稱真實歷史已驗證？
**【是】**。呼叫端若傳入：
```python
verifier = DatasetChecksumVerifier(expected_manifest_hashes={
    "CANDIDATE_MARKET_FEED": "sha256:any_arbitrary_string",
    "CANDIDATE_PRED_FEED": "sha256:any_arbitrary_string",
    "BENCH_SRC": "sha256:any_arbitrary_string",
})
```
系統會直接判定 `is_verified=True`，並給出 `REAL_HISTORICAL_PIT_VERIFIED`（見本報告第四章案例 2）。

### 4. 指紋清單是否有可信的來源、版本與簽章機制？
**【否】**。目前僅為一般 Python `Mapping[str, str]`，無非對稱數位簽章（RSA/ECDSA）、無發行者認證、無有效期限與版本吊銷機制。

### 5. `MockIndependentEvidenceVerifier` 是否可能被正式流程誤用？
**【是 (MEDIUM)】**。`MockIndependentEvidenceVerifier` 直接放置於生產程式碼 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L180-L218)，且由 [`__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py) 正式公開匯出。若開發者在非測試程式碼中誤用，將導致未驗證資料被授信。

### 6. 驗證器回傳 VERIFIED，是否足以直接宣稱官方來源真實？
**【不足以】**。因驗證器僅比對記憶體字串，未驗證該指紋是否真正出具自臺灣證券交易所 (TWSE) 或櫃買中心 (TPEx)。

### 7. 是否存在只改變布林旗標就能繞過獨立驗證的路徑？
**【不存在直接繞過路徑，但可透過注入 Dummy Verifier 間接繞過】**：
- 若未提供 `evidence_verifier`（即 `None`），僅改變 `LineageAttestation` 布林值**無法**達成 Verified，系統會強制降級為 `REAL_HISTORICAL_NOT_VERIFIED`（此部分修補成功）。
- 但若呼叫端傳入 `evidence_verifier=MockIndependentEvidenceVerifier(is_verified=True)`，即可間接繞過。

### 8. 缺少證據、雜湊不一致、來源未知、合成資料或 Mock 時，最終結論是否保守？
**【是】**：
- 合成資料與 Mock 模型：強制降級為 `SYNTHETIC_TEST_RESULT_ONLY`。
- 缺少驗證器：強制標註 `NOT_CONFIGURED`，降級為 `REAL_HISTORICAL_NOT_VERIFIED`。
- 字典缺少 key：判定失敗，降級為 `REAL_HISTORICAL_NOT_VERIFIED`。

### 9. 序列化後再載入，是否可能把未驗證結果提升為已驗證？
**【是 (CRITICAL)】**。
- `BacktestReport.from_json` 僅檢查 JSON 內所附的 `content_hash` 是否等於該 JSON 內容之 SHA-256。
- **無密鑰雜湊致命缺陷**：攻擊者若將 JSON 中的 `validation_verdict` 改為 `REAL_HISTORICAL_PIT_VERIFIED`，並自行以標準函式庫 `hashlib.sha256` 重新計算雜湊替換 `content_hash`，`from_json` 將毫無阻攔地載入，使篡改後的報告生效！（見本報告第四章案例 3）。

### 10. SHA-256 的範圍是否明確區分報告完整性與資料來源可信度？
**【是】**。修補後的報告在 `verification_framework` 中明確加註：
`"hash_scope": "REPORT_CONTENT_INTEGRITY_ONLY"`
`"disclaimer": "SHA-256 雜湊僅保證回測報告產出後未遭竄改，不單獨證明外部資料來源之真實性"`。此部分揭露清晰合規。

---

## 四、具體可重現的漏洞案例

以下提供三個在本次審查中經 Python 實證之具體漏洞反例：

### 案例 1：UTC 跨午夜時間戳導致同日訓練繞過 PIT 檢驗 (FINDING-01)
- **觸發原因**：第 302 行未將 `trained_cutoff_date` 轉為台北時區即直接取 `.date()`。
- **重現程式碼**：
  ```python
  import datetime as dt
  from tw_quant.backtest.calendar import TAIPEI_TZ
  from tw_quant.backtest.report import verify_prediction_consumption_pit
  from tw_quant.backtest.prediction_feed import PredictionConsumptionRecord

  d1 = dt.date(2025, 1, 6)
  # 2025-01-05 17:00 UTC 等同於 2025-01-06 01:00 Taipei (即決策日當日清晨)
  trained_dt_utc = dt.datetime(2025, 1, 5, 17, 0, 0, tzinfo=dt.timezone.utc)

  rec = PredictionConsumptionRecord(
      batch_id="TZ-BYPASS", decision_date=d1, target_trade_date=dt.date(2025, 1, 7),
      decision_cutoff_time=dt.datetime.combine(d1, dt.time(18, 0, 0), tzinfo=TAIPEI_TZ),
      data_cutoff_time=dt.datetime.combine(d1, dt.time(16, 0, 0), tzinfo=TAIPEI_TZ),
      generated_at=dt.datetime.combine(d1, dt.time(17, 0, 0), tzinfo=TAIPEI_TZ),
      available_at=dt.datetime.combine(d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
      model_name="m", model_version="v", feature_version="f",
      trained_cutoff_date=trained_dt_utc,
      is_mock=False, is_synthetic=False, window_id="W1", prediction_count=10,
  )
  ok, reason = verify_prediction_consumption_pit(rec)
  # 實際輸出：ok = True！成功繞過保守拒絕規則！
  ```

### 案例 2：偽造 In-Memory 指紋清單虛構真實歷史驗證 (FINDING-02)
- **觸發原因**：`DatasetChecksumVerifier` 僅檢查字典 key 是否存在，未驗證檔案雜湊與公證簽章。
- **重現程式碼**：
  ```python
  from tw_quant.backtest import BacktestReport, DatasetChecksumVerifier, ValidationVerdict
  # 呼叫端任意傳入虛構的 manifest hash 字串
  fake_verifier = DatasetChecksumVerifier(expected_manifest_hashes={
      "CANDIDATE_MARKET_FEED": "sha256:fake_string_without_actual_data_check",
      "CANDIDATE_PRED_FEED": "sha256:fake_string_without_actual_data_check",
      "BENCH_SRC": "sha256:fake_string_without_actual_data_check",
  })
  rep = BacktestReport.build(real_ok, metrics, attestation=full_att, benchmark=bench, evidence_verifier=fake_verifier)
  # 實際輸出：ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED！完全虛構的驗證通過！
  ```

### 案例 3：無密鑰 SHA-256 遭到篡改並重算雜湊還原通過 (FINDING-02)
- **觸發原因**：`content_hash` 為無密鑰 SHA-256，篡改者可同步替換雜湊值。
- **重現程式碼**：
  ```python
  import json, hashlib
  from tw_quant.backtest import BacktestReport, ValidationVerdict

  rep = BacktestReport.build(synth, metrics) # 原為 SYNTHETIC_TEST_RESULT_ONLY
  data = json.loads(rep.to_json())

  # 篡改結論為已驗證
  data["data_lineage"]["validation_verdict"] = "REAL_HISTORICAL_PIT_VERIFIED"
  data["data_lineage"]["claims_real_historical_verified"] = True
  del data["content_hash"]

  # 惡意重算 SHA-256
  body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
  data["content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()

  # 載入反序列化
  restored = BacktestReport.from_json(json.dumps(data))
  # 實際輸出：restored.validation_verdict == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED！
  ```

---

## 五、測試覆蓋率與測試品質缺口分析

審查新增之 12 項測試（`test_finding01_A~F` 與 `test_finding02_A~F`）：

### 5.1 測試之有效性肯定
1. 測試確實完整呼叫了 `verify_prediction_consumption_pit` 與 `BacktestReport.build` 閘門。
2. 成功覆蓋了布林值聲明不能單獨生效、任意字串備註無效、合成與 Mock 降級等關鍵路徑。

### 5.2 測試品質缺口與盲點
1. **時區轉換邊界測試未包含跨日偏移**：
   - 既有 `test_finding01_F` 僅測試了 naive datetime，未測試非台北時區（如 UTC、EST）在跨午夜轉換至台北時間時的判定行為，導致前述案例 1 之漏洞未被測試發現。
2. **缺乏實體檔案 SHA-256 碰撞測試**：
   - 測試直接使用字串字典比對，未測試若實體行情檔案被修改 1 byte 時雜湊是否能成功攔截。
3. **未測試 JSON 篡改重算之防護邊界**：
   - 測試僅驗證「修改數值但不改雜湊」會引發錯誤，未測試「篡改數值並同步重算雜湊」之風險。

---

## 六、訓練標籤洩漏與 Walk-Forward 尚未解決之架構問題

在量化機器學習體系中，時間前視偏差主要有三大來源：
1. **特徵前視 (Feature Lookahead)**：本專案已透過 `data_cutoff_time <= generated_at` 解決。
2. **推論可用時序前視 (Inference Availability Lookahead)**：本專案已透過 `available_at <= decision_cutoff_time` 解決。
3. **標籤洩漏與自相關重疊 (Label Leakage & Overlapping Samples)**：**本專案目前完全未解決**。

### 現存未解架構缺陷：
- **缺陷 A (Label End Date 盲區)**：AI 選股模型通常預測未來 $H$ 天之累積超額報酬（例如 5 日或 20 日）。在訓練樣本截止日 $T_{\text{train}}$ 之樣本，其標籤實際需要觀察至 $T_{\text{train}} + H$ 日之收盤價。若回測決策日在 $T_{\text{train}} + H$ 之前，模型已在訓練期預知了這段期間的漲跌走勢。目前資料契約無任何欄位記載 $H$ 或標籤截止時間。
- **缺陷 B (Purging 機制缺漏)**：目前的 Walk-Forward 主迴圈未要求模型提供 Purging 證明。
- **結論**：即便通過了現有的 PIT 檢驗，**依然不能保證模型訓練無標籤前視洩漏**。

---

## 七、發現分級清單 (Findings Severity Matrix)

| 發現代號 | 嚴重程度 | 模組與位置 | 問題性質 |
| :---: | :---: | :--- | :--- |
| **FINDING-R01** | `HIGH` | `report.py` (L302) | `trained_cutoff_date` 遇 UTC 跨日時未轉台北時區，致使決策日當日訓練判定通過 |
| **FINDING-R02** | `HIGH` | `report.py` (L104-L168) | `DatasetChecksumVerifier` 未讀取實體資料計算雜湊，呼叫端可傳入偽造指紋清單通過驗證 |
| **FINDING-R03** | `MEDIUM` | `contract.py` / `report.py` | 契約缺少 `label_horizon_days`，完全無法檢驗訓練標籤未來洩漏與 Purging |
| **FINDING-R04` | `MEDIUM` | `report.py` (L180) | `MockIndependentEvidenceVerifier` 置於生產模組並公開匯出，存在正式環境被誤用之風險 |
| **FINDING-R05` | `LOW` | `report.py` (L268) | `data_cutoff_time` 缺漏非空檢查，欄位為 None 時依然判定通過 |
| **FINDING-R06` | `INFO` | `report.py` (L376) | 報告 SHA-256 為無密鑰雜湊，無法防範主動篡改重算 |

---

## 八、每項問題之具體修正建議

1. **針對 FINDING-R01（時區轉換修補）**：
   在 `report.py` 第 302 行解析 `trained_cut` 時，強制先轉為台北時區：
   ```python
   if isinstance(trained_cut, dt.datetime):
       t_pure_date = trained_cut.astimezone(TAIPEI_TZ).date()
   ```
2. **針對 FINDING-R02（真實資料雜湊校驗）**：
   `DatasetChecksumVerifier` 未來應擴充實體檔案校驗器 `FileDigestEvidenceVerifier`，由驗證器自行開啟行情檔案與預測檔案計算串流 SHA-256，而非僅比對記憶體字串字典。
3. **針對 FINDING-R03（標籤洩漏防護）**：
   在 `PredictionMetadata` 新增 `label_horizon_days: int` 與 `label_cutoff_date: Optional[dt.date]`，並於 `verify_prediction_consumption_pit` 強制驗證 `label_cutoff_date < decision_date`。
4. **針對 FINDING-R04（測試 Mock 隔離）**：
   將 `MockIndependentEvidenceVerifier` 移出 `report.py`，改置於 `tests/` 目錄中，並自 `tw_quant.backtest` 公開匯出中移除。
5. **針對 FINDING-R05（必要欄位補強）**：
   將 `data_cut is None` 納入必要欄位非空檢核中。

---

## 九、是否建議進入 C-2.5.2 修補步驟？

**【建議進行小版本 C-2.5.2 修補】**。  
理由：
1. **FINDING-R01**（時區跨日判斷漏洞）屬於明確的邏輯缺陷，若不修正，當接入海外資料源或使用 UTC 時間戳時，會直接破壞 PIT 保守防線。
2. **FINDING-R04**（Mock 驗證器公開匯出）應立即自生產 API 中剝離，避免未來模組產生錯誤依賴。
3. 修補範圍極小（僅幾行時區轉換與測試移動），不影響其他核心架構，可在短時間內完成收斂。

---

## 十、是否具備開始真實資料接入之前置條件？

**【條件尚未完全具備，需完成 C-2.5.2 與 C-3 資料規格定義】**。  
目前阻礙進入真實歷史回測的主要障礙包括：
1. **資料指紋公證機制未建立**：尚未定義 TWSE 歷史行情與財報之官方 Manifest 檔案格式與數位簽章規範。
2. **公司行動僅支援現金股利**：遇股票股利（除權）或減資會引發異常，無法承受台股多年歷史回測。
3. **倖存者偏差防護未就緒**：尚未實作 Point-In-Time 動態股票池供應器。
4. **標籤洩漏檢驗規範未納入資料契約**。

---
*(複核報告結束，遵循唯讀規範未修改任何既有程式碼與測試，請靜待人工審查指示)*
