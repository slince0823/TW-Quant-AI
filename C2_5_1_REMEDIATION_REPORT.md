# 第三階段 C-2.5.1 修補驗收報告 (C2_5_1_REMEDIATION_REPORT.md)

> **任務名稱**：C-2.5.1 — PIT 防前視與資料血統驗證修補  
> **執行日期**：2026-10-21  
> **基線審查**：[`PHASE3C2_5_INDEPENDENT_REVIEW.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PHASE3C2_5_INDEPENDENT_REVIEW.md)（發現 FINDING-01 與 FINDING-02 等 HIGH 等級問題）  
> **子專案基線**：
> - `anti-gambling-trader-tw`: `8609cef73718a056fddb74201fc304f174fa1553` (Clean)
> - `qlib-tw-trader`: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` (Clean)
> 
> **全系統測試結果**：**260 / 260 項測試全數通過 (Ran 260 tests in 0.435s, OK)**

---

## 一、修補背景與問題定義

在完成第三階段 C-2.5 之獨立技術審查後，發現兩項重大技術與架構隱患：

1. **FINDING-01 (HIGH)：預測 PIT 時序因果未實質驗證**
   - **原狀**：[`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py) 僅檢查 `available_at >= generated_at` 以及 `trained_cutoff_date is not None`。
   - **漏洞**：若上游提供的模型訓練截止日晚於或等於決策日（例如 `trained_cutoff_date = 2025-06-30`，但決策日為 `2025-01-10`），系統仍會將預測資料判定為通過 PIT 檢查，並授予 `LineageStatus.REAL_PIT_ATTESTED`。這讓偷看未來的模型在報告層級偽裝成無前視偏差。
2. **FINDING-02 (HIGH)：資料血統過度信任呼叫端聲明與雜湊誤解**
   - **原狀**：`LineageAttestation` 僅由呼叫端提供四個布林值旗標及非空 `evidence_notes` 字串，若皆為 True 即直接將報告驗證結論判定為 `ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED`。
   - **漏洞**：
     - 任意字串（如 `"test note"`）即可充當來源公證證明。
     - 呼叫端自我保證（Caller Attestation）與系統獨立驗證（Independent Verification）混淆。
     - 報告的 SHA-256 內容雜湊僅能保證 JSON 序列化產出後未被竄改，無法單獨證明外部資料源頭的客觀真實性。
     - 缺乏真正的「獨立證據驗證器」協定與實作。

---

## 二、修正內容與架構改進

### 2.1 FINDING-01 修補：`verify_prediction_consumption_pit` 專屬驗證器

在 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py) 實作獨立函式 `verify_prediction_consumption_pit(record)`，對回測消費之每一筆 `PredictionConsumptionRecord` 執行嚴格的時序因果防禦檢驗：

1. **必要時間欄位完整性檢驗 (Requirement E)**：
   - 檢查 `generated_at`、`available_at`、`decision_cutoff_time`、`decision_date`、`trained_cutoff_date` 不得為 `None`。
   - 缺漏任一項直接拒絕，並明確記錄缺漏之欄位名稱。
2. **時區有效性與型別檢驗 (Requirement F)**：
   - 所有 datetime 欄位（`generated_at`、`available_at`、`decision_cutoff_time`、`data_cutoff_time`）必須為 **timezone-aware** (`tzinfo is not None and tzinfo.utcoffset(...) is not None`)。
   - 遇到 naive datetime 直接拒絕，絕不猜測時區。
   - 驗證 `decision_date` 與 `decision_cutoff_time.astimezone(TAIPEI_TZ).date()` 必須一致，防範日期錯位。
3. **推論因果檢驗 (Requirement D)**：
   - `data_cutoff_time <= generated_at`：模型特徵截斷時間不得晚於生成時間。
   - `generated_at <= available_at`：預測發布可用時間不得早於生成時間（拒絕可用時間倒流）。
   - `available_at <= decision_cutoff_time`：預測必須在回測盤後決策時間點之前就緒。若 `available_at > decision_cutoff_time`，代表決策當下該預測尚未公開，依前視違規拒絕。
4. **訓練截止日與決策時間因果比對 (Requirements A, B, C)**：
   - **嚴格早於原則 (`t_date < d_date`)**：只有當 `trained_cutoff_date` 嚴格早於 `decision_date` 時，始能判定通過因果條件。
   - **晚於決策日 (`t_date > d_date`)**：明確判定為未來資料洩漏違規 (`Lookahead Violation`)，直接拒絕。
   - **等於決策日 (`t_date == d_date`) 保守拒絕**：因 `trained_cutoff_date` 僅具備日期精度，無法證明當日模型訓練在盤後決策前完成，且無法排除標籤重疊洩漏，依專案規範嚴格採保守拒絕。
   - 若 `trained_cutoff_date` 為 datetime，其時間戳亦必須嚴格早於 `decision_cutoff_time`。

---

### 2.2 FINDING-02 修補：資料血統四維度架構與獨立證據驗證器

重構 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py) 之驗證評級與報告結構：

1. **資料血統四維度明確拆分**：
   在回測報告的 `data_lineage` 區塊中，正式建立 `verification_framework` 結構：
   - **維度一：呼叫端聲明 (Caller Attestation)**：
     - 記錄呼叫端傳入之布林旗標與備註文字。
     - 明確附註聲明：`"呼叫端主觀聲明，非第三方或系統獨立驗證"`。
   - **維度二：資料內容完整性 (Data Integrity)**：
     - 明確標記 `hash_algorithm: "SHA-256"`、`hash_scope: "REPORT_CONTENT_INTEGRITY_ONLY"`。
     - 明確免責註記：`"SHA-256 雜湊僅保證回測報告產出後未遭竄改，不單獨證明外部資料來源之真實性"`。
   - **維度三：來源追溯 (Provenance Tracking)**：
     - 追溯記錄行情來源、預測來源、交易日曆來源、資料集版本與 `is_synthetic` 旗標。
   - **維度四：獨立證據驗證 (Independent Verification)**：
     - 記錄獨立驗證器名稱、驗證時間戳、驗證狀態 (`VERIFIED` / `FAILED` / `NOT_CONFIGURED`) 與失敗原因清單。
2. **新增獨立驗證器介面與公證指紋清單實作**：
   - 定義 `EvidenceVerificationResult` 與 `@runtime_checkable` 協定 `IndependentEvidenceVerifier`。
   - 實作參考驗證器 `DatasetChecksumVerifier`：依據已公證之資料集 SHA-256 指紋清單比對來源雜湊。若未配置清單或雜湊不符，判定為未驗證。
   - 實作測試驗證器 `MockIndependentEvidenceVerifier`：供單元與回歸測試模擬驗證流程。
3. **終端判定閘門嚴格收緊**：
   只有在同時滿足以下所有條件時，始能給出 `ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED`：
   - 所有資料來源非合成 (`is_synthetic == False`) 且非 Mock。
   - 所有預測紀錄通過 `verify_prediction_consumption_pit` 檢驗。
   - 每日收盤 NAV 完整 (`nav_complete == True`)。
   - 回測正常完成 (`run_completed == True`)。
   - 損益對帳完全吻合 (`reconciliation.status == OK`)。
   - **配置了獨立證據驗證器 (`evidence_verifier is not None`) 且實質驗證通過 (`verifier_result.is_verified == True`)**。
   - **若缺少獨立證據驗證器，即使呼叫端提供全 True 聲明與任何字串 notes，結論強制保守為 `REAL_HISTORICAL_NOT_VERIFIED`，且 `claims_real_historical_verified` 必須為 `False`**。

---

## 三、修改檔案清單

本次修補嚴格遵循限制條件，未修改任何子專案核心、交易策略、撮合、帳本或 NAV 模組：

| 檔案路徑 | 修改性質 | 變更說明 |
| :--- | :---: | :--- |
| [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py) | **核心修改** | 新增 `verify_prediction_consumption_pit`、`EvidenceVerificationResult`、`IndependentEvidenceVerifier`、`DatasetChecksumVerifier`、`MockIndependentEvidenceVerifier`；更新 `BacktestReport.build` 閘門邏輯與四維度報告結構。 |
| [`src/tw_quant/backtest/__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py) | **匯出更新** | 公開匯出新增之驗證協定、驗證器實作與驗證函式。 |
| [`tests/test_backtest_metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py) | **測試擴充** | 更新既有 `test_16` 使其驗證缺少獨立驗證器時強制保守為 `NOT_VERIFIED`；新增 `TestPitCausalVerificationAndLineageRemediation` 類別，包含 12 項針對 FINDING-01 與 FINDING-02 之專屬驗收測試。 |
| [`PROJECT_MAP.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/PROJECT_MAP.md) | **文件更新** | 更新專案狀態至 C-2.5.1，記錄新增之審查報告與修補驗收報告。 |

---

## 四、新增測試項目與實際執行結果

在 [`tests/test_backtest_metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py) 中新增 `TestPitCausalVerificationAndLineageRemediation` 測試套件，涵蓋任務要求之所有 12 項測試情境：

### 4.1 FINDING-01 PIT 因果驗證測試

| 測試方法名稱 | 對應要求 | 測試情境與預期行為 | 實際測試結果 |
| :--- | :---: | :--- | :---: |
| `test_finding01_A_trained_cutoff_before_decision_date_conforms` | 要求 A | `trained_cutoff_date` (2025-01-05) 早於決策日 (2025-01-06)，因果檢驗通過。 | **PASSED** |
| `test_finding01_B_trained_cutoff_after_decision_date_rejected` | 要求 B | `trained_cutoff_date` (2025-01-07) 晚於決策日 (2025-01-06)，檢驗失敗，標記未來資料洩漏。 | **PASSED** |
| `test_finding01_C_trained_cutoff_equals_decision_date_conservatively_rejected` | 要求 C | `trained_cutoff_date` (2025-01-06) 等於決策日 (2025-01-06)，因無時分秒精度保守拒絕。 | **PASSED** |
| `test_finding01_D_available_at_before_generated_at_rejected` | 要求 D | `available_at` (17:00) 早於 `generated_at` (17:30)，可用時間倒流直接拒絕。 | **PASSED** |
| `test_finding01_E_missing_required_timestamps_not_attested` | 要求 E | 逐一測試缺漏各項時間戳，確保一律回傳 False 且不可被標記為 `REAL_PIT_ATTESTED`。 | **PASSED** |
| `test_finding01_F_timezone_naive_and_type_mismatch_handled` | 要求 F | 測試 naive datetime、`decision_date` 與台北截止時間日期不符、可用時間晚於決策截止時間，皆被拒絕。 | **PASSED** |

### 4.2 FINDING-02 資料血統驗證測試

| 測試方法名稱 | 對應要求 | 測試情境與預期行為 | 實際測試結果 |
| :--- | :---: | :--- | :---: |
| `test_finding02_A_arbitrary_evidence_notes_insufficient` | 要求 A | 傳入任意非空備註文字，無獨立驗證器時，結論仍保守為 `REAL_HISTORICAL_NOT_VERIFIED`。 | **PASSED** |
| `test_finding02_B_caller_boolean_attestation_insufficient` | 要求 B | 呼叫端將所有聲明設為 True，無獨立驗證器時不可宣稱真實歷史驗證，並提示主觀聲明。 | **PASSED** |
| `test_finding02_C_synthetic_or_mock_never_promoted` | 要求 C | 合成資料與 Mock 模型即使配置了獨立驗證器，結論仍強制為 `SYNTHETIC_TEST_RESULT_ONLY`。 | **PASSED** |
| `test_finding02_D_sha256_protects_content_not_data_truth` | 要求 D | 驗證報告 SHA-256 僅確保序列化未被竄改，報告明確標註 `REPORT_CONTENT_INTEGRITY_ONLY`。 | **PASSED** |
| `test_finding02_E_missing_independent_verifier_is_conservative` | 要求 E | 缺少獨立驗證器時，`independent_verification` 狀態為 `NOT_CONFIGURED`，結論保守。 | **PASSED** |
| `test_finding02_F_full_evidence_verification_pipeline` | 要求 F | 使用 `DatasetChecksumVerifier`：指紋吻合時通過；指紋缺漏時失敗降級。 | **PASSED** |

---

## 五、實際測試執行輸出摘要

### 5.1 績效分析與報告測試套件
```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_backtest_metrics.py"
...................................
----------------------------------------------------------------------
Ran 35 tests in 0.039s

OK
```
*(35 項測試全數通過，包含 23 項既有測試與 12 項新增修補測試)*

### 5.2 全次回測模組整合測試
```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_backtest_*.py"
........................................................................................................
----------------------------------------------------------------------
Ran 104 tests in 0.409s

OK
```
*(104 項回測核心測試全數通過)*

### 5.3 全系統端到端回歸測試
```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
....................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 260 tests in 0.435s

OK
```
*(全系統 260 項測試全數通過，測試總數自 248 增加至 260，零失敗、零錯誤)*

---

## 六、尚未解決的限制 (Remaining Limitations)

依據唯讀審查與本階段範圍界定，以下項目仍屬於目前架構之已知限制，保留至後續階段處理：

1. **真實資料庫官方指紋清單尚未串接**：
   目前 `DatasetChecksumVerifier` 具備完整的指紋驗證協定與邏輯，但由於本專案尚未正式接入台股 TWSE 歷史行情與財報資料庫，目前並無真實行情資料集公證指紋清單。在正式接入真實資料前，回測報告將持續保守輸出 `REAL_HISTORICAL_NOT_VERIFIED`。
2. **股票股利與分割減資**：
   `CorporateActionProcessor` 目前僅支援現金股利會計，遇股票股利或減資將拋出異常，尚未支援股數與除權參考價的自動調整（規劃於第三階段 C-3 實作）。
3. **動態 Point-In-Time 股票池**：
   `WalkForwardRunConfig` 目前使用靜態 `universe_symbols`，尚無法動態排除歷史下市股或未上市標的（倖存者偏差防護規劃於第三階段 C-3 實作）。
4. **離線特徵管道申報延遲**：
   本回測引擎已能保證推論預測在決策時間點前可用，但 Qlib 離線特徵產生若直接取用財務季度結束日而非實際公開申報日，該類前視偏差需由特徵管線端防護。

---

## 七、為何目前仍不能宣稱真實台股歷史回測已驗證？

專案團隊在此做出明確且負責任之技術結論：

1. **所有測試皆運行於合成測試情境**：
   目前全系統測試所使用的行情 Bar、AI 預測批次與除權息事件，皆為顯式標記 `is_synthetic=True` 之確定性測試假資料。
2. **防偽降級原則嚴格執行**：
   依據本修補後之架構，只要資料來源標記為合成，`ValidationVerdict` 永遠只會是 `SYNTHETIC_TEST_RESULT_ONLY`，絕不可能被偽造為真實歷史已驗證。
3. **結論與免責聲明**：
   **本次修補僅代表「回測分析器與資料血統防偽閘門之時序因果保護邏輯已修正完備」，絕對不代表任何真實台股歷史策略績效已經獲得驗證。在正式接入真實台股歷史行情、官方交易日曆與公證資料集之前，嚴禁宣稱任何真實台股量化超額報酬或實盤收益率。**

---
*(報告完畢，全系統測試 260/260 通過，等待獨立審查)*
