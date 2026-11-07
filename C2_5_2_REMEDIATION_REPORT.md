# 第三階段 C-2.5.2 修補報告：PIT 與資料血統驗證安全性修補

> **報告日期**：2026-10-21  
> **任務名稱**：第三階段 C-2.5.2 — PIT 與資料血統驗證安全性修補 (Point-in-Time & Lineage Security Remediation)  
> **依據文件**：
> - `C2_5_1_INDEPENDENT_REVIEW.md`（獨立技術複核報告）
> - `C2_5_1_REMEDIATION_REPORT.md`（前次修補報告）
> - `PHASE3C2_5_INDEPENDENT_REVIEW.md`（原始審查報告）
> - `BACKTEST_METHODOLOGY.md`（回測方法論第四章）
> 
> **子專案基準狀態**：
> - `anti-gambling-trader-tw`: `8609cef73718a056fddb74201fc304f174fa1553` (Clean，未修改)
> - `qlib-tw-trader`: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` (Clean，未修改)
> - **全系統測試結果**：**275 / 275 Passed** (0 失敗, 0 錯誤, 0 跳過，耗時 0.432s)
> 
> **重要聲明**：  
> **本階段所有端到端測試均使用顯式標記 `is_synthetic=True` 之合成資料。本報告完成之修補僅代表回測引擎邏輯、時序因果防線、信任邊界與防偽機制之強化，絕不代表台股真實歷史回測績效已獲驗證，嚴禁將測試結果宣稱為真實策略有效性或實盤獲利能力。**

---

## 目錄

1. [修補任務概述與背景](#一修補任務概述與背景)
2. [漏洞與缺陷根因分析 (R01, R05, R04, R02, R06)](#二漏洞與缺陷根因分析)
3. [具體修補架構與實作變更](#三具體修補架構與實作變更)
4. [漏洞重現與防護驗證 (3 個可重現反例修補前後對比)](#四漏洞重現與防護驗證)
5. [測試套件執行結果與覆蓋率分析](#五測試套件執行結果與覆蓋率分析)
6. [標籤洩漏與 Walk-Forward 現存未解限制聲明 (嚴肅揭露)](#六標籤洩漏與-walk-forward-現存未解限制聲明)
7. [系統防偽邊界與資料真實性聲明](#七系統防偽邊界與資料真實性聲明)
8. [後續建議與人工審查確認點](#八後續建議與人工審查確認點)

---

## 一、修補任務概述與背景

在完成第三階段 C-2.5.1 修補後，獨立技術複核報告 `C2_5_1_INDEPENDENT_REVIEW.md` 透過靜態原始碼分析與可重現反例，指出了數項關鍵安全性與信任邊界缺陷：
1. **FINDING-R01 (`HIGH`)**：時區跨午夜截斷缺陷。`trained_cutoff_date` 遇 UTC 或海外時區時間戳時未轉換為台北市場時區即取 `.date()`，導致台灣時間已是決策日清晨的訓練時間戳，其 UTC 日期被誤判為前一日，繞過因果防禦。
2. **FINDING-R05 (`LOW`)**：必要欄位缺漏。`data_cutoff_time` 缺乏強制非空檢驗，為 `None` 時依然判定通過。
3. **FINDING-R04 (`MEDIUM`)**：Mock 驗證器未與生產隔離。`MockIndependentEvidenceVerifier` 置於生產模組並由 `tw_quant.backtest.__init__.py` 公開匯出，存在正式環境被誤用之風險。
4. **FINDING-R02 (`HIGH`)**：資料集公證指紋信任邊界缺陷。`DatasetChecksumVerifier` 僅比對呼叫端於記憶體傳入的字串字典，完全未讀取磁碟實體資料計算 SHA-256，且缺少信任根驗證，使得偽造清單可輕易取得 `REAL_HISTORICAL_PIT_VERIFIED`。
5. **FINDING-R06 (`INFO`)**：報告反序列化信任邊界缺陷。無密鑰 SHA-256 僅能檢驗傳輸損毀，無法防範主動重算。攻擊者篡改 JSON 中的結論為已驗證並重算 SHA-256，反序列化時會無阻攔載入。

本任務（C-2.5.2）針對上述五項確認缺陷進行系統性安全修補，並遵守不可違反之專案約束：不碰觸原始子專案核心、不下載外部資料、不訓練模型、不宣稱真實獲利。

---

## 二、漏洞與缺陷根因分析

### 2.1 R01：UTC 跨午夜時間戳導致同日訓練繞過 PIT 檢驗 (FINDING-R01)
- **根因**：`src/tw_quant/backtest/report.py` 舊版第 302 行：
  ```python
  if isinstance(trained_cut, dt.datetime):
      ...
      t_pure_date = trained_cut.date()
  ```
  Python `datetime.date()` 取得的是該時間戳物件其**本地時區之日期**。若傳入之時間戳為 `2025-01-05 17:00:00 UTC`，在台北時區（`UTC+8`）其實際對應時間為 `2025-01-06 01:00:00+08:00`，即決策日 `2025-01-06` 當日清晨。然而 `.date()` 產出 `2025-01-05`，使得 `t_pure_date < d_pure_date` 判斷成立，徹底繞過了「訓練截止日等於決策日應保守拒絕」之因果防線。
- **naive datetime 盲區**：若決策日或訓練截止日傳入缺少時區資訊之 naive datetime，未能提供一致的台北時區對齊。

### 2.2 R05：`data_cutoff_time` 缺漏非空檢查 (FINDING-R05)
- **根因**：在 `report.py` 舊版中：
  ```python
  data_cut = getattr(record, "data_cutoff_time", None)
  ...
  # 第 246-255 行必要性檢查清單漏掉了 data_cut
  if data_cut is not None:
      ...
  ```
  當上游傳入 `data_cutoff_time=None` 時，因 `if data_cut is not None:` 為 False，驗證器不會報錯，造成特徵資料截止時間可能遺失卻依然判定通過。

### 2.3 R04：`MockIndependentEvidenceVerifier` 未自生產隔離 (FINDING-R04)
- **根因**：`MockIndependentEvidenceVerifier` 定義在生產程式碼 `src/tw_quant/backtest/report.py` 中，並由 `src/tw_quant/backtest/__init__.py` 公開匯出至 `__all__`。若正式環境呼叫端誤引或刻意注入此 Mock，將使系統繞過實質獨立驗證。

### 2.4 R02：資料指紋校驗未讀取磁碟實體且無授權信任根 (FINDING-R02)
- **根因**：`DatasetChecksumVerifier` 僅做字串鍵值比對：
  ```python
  mkt_src = result.market_data_provenance.source
  expected_mkt_hash = self.expected_manifest_hashes.get(mkt_src)
  ```
  1. 它未曾開啟磁碟實體行情檔或預測檔計算 SHA-256，無法偵測磁碟檔案遭篡改或損毀。
  2. 指紋清單完全來自呼叫端記憶體傳入，無官方授權根，任何任意字串皆可觸發 Verified。

### 2.5 R06：無密鑰 SHA-256 無法防範反序列化主動篡改重算 (FINDING-R06)
- **根因**：`BacktestReport.from_json` 僅檢查 JSON 內所附的 `content_hash` 是否等於該 JSON 內容之 SHA-256。無密鑰雜湊無法區分「合法發行者計算」與「惡意攻擊者篡改後重新計算」。攻擊者修改 `validation_verdict` 為 `REAL_HISTORICAL_PIT_VERIFIED` 並重算 SHA-256 即可通過校驗。

---

## 三、具體修補架構與實作變更

### 3.1 `src/tw_quant/backtest/report.py` 變更

1. **R01 修補：強制台北時區對齊與 Naive Datetime 攔截**：
   ```python
   # 決策日解析強制轉為台北時區日期
   if isinstance(dec_date, dt.datetime):
       if dec_date.tzinfo is None or dec_date.tzinfo.utcoffset(dec_date) is None:
           return False, f"批次 {bid} decision_date 為 datetime 但缺少時區資訊 (naive datetime)"
       d_pure_date = dec_date.astimezone(TAIPEI_TZ).date()
   elif isinstance(dec_date, dt.date):
       d_pure_date = dec_date
   ...
   # 訓練截止時間強制轉換為台北時區日期後比對
   if isinstance(trained_cut, dt.datetime):
       if trained_cut.tzinfo is None or trained_cut.tzinfo.utcoffset(trained_cut) is None:
           return False, f"批次 {bid} trained_cutoff_date 為 datetime 但缺少時區資訊 (naive datetime)"
       t_pure_date = trained_cut.astimezone(TAIPEI_TZ).date()
       if trained_cut >= dec_cut:
           return False, f"批次 {bid} trained_cutoff_time ({trained_cut}) 大於等於決策截止時間 ({dec_cut})，存在未來資料洩漏"
   ...
   if t_pure_date >= d_pure_date:
       # 保守拒絕同日訓練或未來訓練
   ```
2. **R05 修補：`data_cutoff_time` 強制非空、Aware 時區與時序因果**：
   ```python
   if data_cut is None:
       return False, f"批次 {bid} 缺少 data_cutoff_time 時間戳"
   for name, val in (
       ("generated_at", gen_at), ("available_at", avail_at),
       ("decision_cutoff_time", dec_cut), ("data_cutoff_time", data_cut),
   ):
       if not isinstance(val, dt.datetime):
           return False, f"批次 {bid} 之 {name} 型別錯誤 (預期 datetime，收到 {type(val).__name__})"
       if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
           return False, f"批次 {bid} 之 {name} 缺少時區資訊 (naive datetime: {val})"
   if data_cut > gen_at:
       return False, f"批次 {bid} data_cutoff_time ({data_cut}) 晚於 generated_at ({gen_at})"
   ```
3. **R04 修補：生產模組完全移除 `MockIndependentEvidenceVerifier`**：
   - 自 `report.py` 徹底刪除 `MockIndependentEvidenceVerifier` 類別定義。
4. **R02 修補：重構 `DatasetChecksumVerifier` 並新增 `FileDigestEvidenceVerifier`**：
   - `DatasetChecksumVerifier` 增加 `is_trusted_authority: bool = False` 檢驗。若為 False，直接判定為 `UNTRUSTED_CALLER_MANIFEST`，拒絕授予真實歷史驗證。
   - 新增 [`FileDigestEvidenceVerifier`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)：
     - 具備 `source_file_paths: Mapping[str, str]` 與 `expected_file_hashes: Mapping[str, str]`。
     - 必須具備受信任之公證根 (`is_trusted_root=True`)。
     - 逐一針對磁碟實體檔案開啟串流讀取（以 64KB chunk 讀取），計算真實 `hashlib.sha256()` 摘要。
     - 檔案不存在、路徑非檔案、讀取異常或雜湊有任何 1 byte 不符，立即判定驗證失敗。
5. **R06 修補：`BacktestReport.from_json` 信任邊界防護與 HMAC 數位簽章**：
   - 新增 `sign(secret: str) -> str` 方法，可使用密鑰為報告本體計算 `HMAC-SHA256` 驗證簽章。
   - 重構 `from_json(text, *, trusted_secret=None, strict_reject_unverified_load=False)`：
     - 若還原之報告宣稱 `REAL_HISTORICAL_PIT_VERIFIED` 或 `claims_real_historical_verified=True`：
       - 若附有合規 HMAC 簽章且與傳入之 `trusted_secret` 比對相符，保留驗證結論。
       - 若缺少數位簽章或簽章無效：
         * `strict_reject_unverified_load=True` 時直接拋出 `BacktestMetricsError` 拒絕載入。
         * 預設情況下，依保守信任邊界**強制降級為 `REAL_HISTORICAL_NOT_VERIFIED`**，並於 `data_quality_warnings` 附加 `[DESERIALIZATION_TRUST]` 警告，於 `unverified_items` 登記 `deserialization_unauthenticated`。

### 3.2 `src/tw_quant/backtest/__init__.py` 變更
- 移除 `MockIndependentEvidenceVerifier` 之匯入與匯出。
- 新增 `FileDigestEvidenceVerifier` 之匯入與公開匯出。

### 3.3 `tests/test_backtest_metrics.py` 變更
- 將 `MockIndependentEvidenceVerifier` 移入測試模組頂層定義，僅供測試情境模擬。
- 新增 15 項專屬回歸測試覆蓋 R01、R05、R04、R02、R06。

---

## 四、漏洞重現與防護驗證

本次修補方針依據獨立技術複核報告中之 3 個具體反例，逐一進行修補前重現確認與修補後防護驗證：

### 4.1 案例 1 重現與修補驗證 (R01：UTC 跨午夜時間戳)
- **漏洞場景**：決策日為 `2025-01-06`。模型於 `2025-01-05 17:00:00 UTC` 訓練完成（台北時間為 `2025-01-06 01:00:00+08:00`，即決策日當日清晨）。
- **修補前行為**：`trained_cut.date()` 取得 UTC 日期 `2025-01-05`，小於決策日 `2025-01-06`，回傳 `ok = True`，判定通過（漏失防禦）。
- **修補後行為**：
  - 程式碼：
    ```python
    t_pure_date = trained_cut.astimezone(TAIPEI_TZ).date() # 轉為 2025-01-06
    if t_pure_date == d_pure_date:
        return False, "批次 TZ-BYPASS trained_cutoff_date (2025-01-06) 等於決策日 (2025-01-06)，依保守原則拒絕"
    ```
  - 測試驗證：[`test_r01_cross_midnight_utc_bypass_rejected`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L964) 驗證回傳 `ok = False`，成功攔截。
  - 對照組：[`test_r01_cross_midnight_utc_valid_prior_day_accepted`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L980)（`15:59 UTC` = 台北 `23:59` 前一日）正常通過。

### 4.2 案例 2 重現與修補驗證 (R02：偽造 In-Memory 指紋清單)
- **漏洞場景**：呼叫端傳入任意偽造的字串指紋清單 `DatasetChecksumVerifier(expected_manifest_hashes={"...": "sha256:fake_string"})`。
- **修補前行為**：系統比對字典 key 存在即判定 `is_verified = True`，輸出 `REAL_HISTORICAL_PIT_VERIFIED`。
- **修補後行為**：
  - 未經授權之記憶體清單預設 `is_trusted_authority=False`，驗證器直接回傳 `is_verified=False`，拒絕理由明載 `UNTRUSTED_CALLER_MANIFEST`。
  - 測試驗證：[`test_r02_untrusted_caller_manifest_hashes_rejected`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L1071) 驗證結論維持 `REAL_HISTORICAL_NOT_VERIFIED`。
  - 實體檔案校驗：新增 [`FileDigestEvidenceVerifier`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)，在 [`test_r02_file_digest_verifier_physical_files`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L1099) 中建立真實臨時檔案：
    * Case A（正確檔案與雜湊）$\to$ `REAL_HISTORICAL_PIT_VERIFIED`。
    * Case B（檔案內容篡改 1 byte）$\to$ 串流 SHA-256 不符，降級為 `NOT_VERIFIED`。
    * Case C（實體檔案遺失）$\to$ 檔案不存在，降級為 `NOT_VERIFIED`。
    * Case D（未授權信任根）$\to$ `UNTRUSTED_MANIFEST_ROOT`，降級為 `NOT_VERIFIED`。

### 4.3 案例 3 重現與修補驗證 (R06：無密鑰 SHA-256 篡改重算)
- **漏洞場景**：攻擊者將回測 JSON 中的 `validation_verdict` 改為 `REAL_HISTORICAL_PIT_VERIFIED`，並以 `hashlib.sha256` 重新計算內容雜湊填入 `content_hash`。
- **修補前行為**：`BacktestReport.from_json` 比對 `content_hash` 相符，毫無阻攔還原為 `REAL_HISTORICAL_PIT_VERIFIED`。
- **修補後行為**：
  - 反序列化檢測到報告宣稱已驗證，檢查其數位簽章。因攻擊者無受信任密鑰，簽章缺失或不符。
  - 系統自動執行保守降級：
    * `validation_verdict` 強制重設為 `REAL_HISTORICAL_NOT_VERIFIED`。
    * `claims_real_historical_verified` 強制設為 `False`。
    * 附加警告：`[DESERIALIZATION_TRUST] 反序列化報告缺少受信任之數位簽章，無密鑰 SHA-256 無法防範主動偽造，依保守信任邊界強制降級為 REAL_HISTORICAL_NOT_VERIFIED`。
  - 測試驗證：[`test_r06_tampered_json_unkeyed_sha256_downgraded`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L1200) 驗證降級成功。
  - 嚴格模式：[`test_r06_tampered_json_strict_mode_raises`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L1233) 驗證直接拋出 `BacktestMetricsError`。
  - 合法簽章比對：[`test_r06_signed_report_roundtrip`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L1252) 驗證在具備正確密鑰時，合法的 `REAL_HISTORICAL_PIT_VERIFIED` 報告可完整保留。

---

## 五、測試套件執行結果與覆蓋率分析

### 5.1 完整測試執行紀錄
執行指令：
```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```
實際終端輸出：
```text
Ran 275 tests in 0.432s

OK
```

### 5.2 各模組測試清單統計

| 測試檔案 | 測試項目數 | 涵蓋範疇 | 狀態 |
| :--- | :---: | :--- | :---: |
| `test_signal.py` | 19 | 統一交易訊號合約、序列化與驗證 | **PASSED** |
| `test_paper_broker.py` | 26 | 模擬券商撮合、交割與部位會計 | **PASSED** |
| `test_risk_engine.py` | 22 | 前置風控、熔斷、Kill Switch 與資金鎖定 | **PASSED** |
| `test_integration.py` | 23 | 端到端系統整合 | **PASSED** |
| `test_prediction.py` | 14 | AI 預測資料合約、時序因果與防洩漏 | **PASSED** |
| `test_strategy_engine.py` | 52 | AI 排名選股、等權配置、出場與訊號分發 | **PASSED** |
| `test_backtest_data_layer.py` | 14 | 回測日曆、PIT 資料供應器與例外階層 | **PASSED** |
| `test_backtest_accounting.py` | 18 | FIFO 持倉帳本與除權息應收會計 | **PASSED** |
| `test_backtest_execution.py` | 21 | 日線歷史撮合模擬器與每日投資組合估值器 | **PASSED** |
| `test_backtest_runner.py` | 16 | 策略／風控橋接與事件驅動 Walk-Forward 主迴圈 | **PASSED** |
| `test_backtest_metrics.py` | 50 | 績效分析、資料血統、PIT 因果與防偽安全校驗 | **PASSED** |
| **全系統總計** | **275** | **全系統無跳過、無警告、零失敗** | **100% OK** |

---

## 六、標籤洩漏與 Walk-Forward 現存未解限制聲明 (嚴肅揭露)

雖然 C-2.5.2 修補成功關閉了時區轉換、必要欄位與信任邊界等程式級漏洞，但依據量化機器學習方法論與 `C2_5_1_INDEPENDENT_REVIEW.md` 第六章分析，本專案在**模型訓練端**依然存在一項無法單靠回測主迴圈消弭的重大架構限制：

> [!CAUTION]
> ### 訓練標籤跨期未來洩漏盲區 (Label Horizon Lookahead Blindspot)
> 1. **現象**：若 AI 選股模型以未來 $H$ 天（如 10 日或 20 日）之累積超額報酬作為訓練預測標籤（Label），在訓練特徵截止日 $T_{\text{train}}$ 之訓練樣本，其標籤實際需要觀察至 $T_{\text{train}} + H$ 日之收盤價。
> 2. **盲區**：目前資料契約 `PredictionMetadata` 僅記錄 `trained_cutoff_date`（特徵截止日），未包含 `label_horizon_days` 或 `label_cutoff_date`。
> 3. **後果**：若回測決策日在 $T_{\text{train}}$ 與 $T_{\text{train}} + H$ 之間，即便 `trained_cutoff_date < decision_date` 判定通過，模型在訓練期實際上已經「看過」這段期間的市場價格。
> 4. **處理限制**：此缺陷屬於**離線機器學習特徵管線與資料契約層**之議題，需在第四階段實作 Qlib LightGBM 離線訓練管線時，擴充資料契約加入 `label_cutoff_date` 與 Purging 證明，回測引擎始能執行實質校驗。目前絕不可宣稱「所有訓練標籤洩漏已被杜絕」。

---

## 七、系統防偽邊界與資料真實性聲明

本專案自建立以來嚴格遵守防偽承諾，在此鄭重重申當前系統能力邊界：

1. **合成沙盒性質**：
   目前所有的回測端到端測試均在顯式標記 `is_synthetic=True` 之合成行情與預測資料上執行。回測引擎輸出之結論均為 `SYNTHETIC_TEST_RESULT_ONLY`。
2. **禁止宣稱真實績效**：
   目前**尚未接入任何真實臺灣證券交易所 (TWSE) 歷史行情**，**尚未接入真實除權息資料庫**，**尚未執行正式 AI 策略回測**。所有指標數值僅供驗證計算公式與系統守恆性，絕不可作為策略有效性、容量或實盤交易之依據。
3. **無密鑰 SHA-256 邊界明確**：
   報告內附之 SHA-256 雜湊僅代表「報告生成後的序列化完整性」，防止檔案在儲存或傳輸中損毀，不單獨證明外部資料來源之真實性。反序列化時若無金鑰簽章，系統一律保守拒絕或降級處理。

---

## 八、後續建議與人工審查確認點

### 8.1 建議後續步驟
1. **停止自動化執行**：本次 C-2.5.2 修補已完全收斂所有複核發現之漏洞，請停步並等待人工技術審查核准。
2. **進入第四階段 C-3 資料層之前置規範**：
   - 制定 TWSE 官方 Parquet 行情與除權息資料庫之 Manifest Checksum 與 GPG / HMAC 數位簽章規範。
   - 擴充 `PredictionMetadata` 加入 `label_cutoff_date` 與 `label_horizon_days` 標籤隔離欄位。
   - 實作支援股票股利（除權）與現金增減資之完整除權息處理器。

### 8.2 人工審查確認檢核點 (Human Review Checklist)
- [x] 子專案 `anti-gambling-trader-tw` 保持 clean，未修改任何核心程式碼。
- [x] 子專案 `qlib-tw-trader` 保持 clean，未修改任何核心程式碼。
- [x] 時區跨午夜漏洞（UTC 17:00 等於台北隔日 01:00）已由 `.astimezone(TAIPEI_TZ).date()` 徹底阻擋。
- [x] `data_cutoff_time` 已列為強制非空且具備時序因果防線。
- [x] `MockIndependentEvidenceVerifier` 已自生產模組完全分離，僅留存於測試層。
- [x] `DatasetChecksumVerifier` 未經授權之記憶體清單預設拒絕，實體檔案校驗由 `FileDigestEvidenceVerifier` 執行真實串流比對。
- [x] `BacktestReport.from_json` 具備反序列化信任邊界降級防護與 HMAC 簽章驗證，篡改重算 PoC 被徹底封堵。
- [x] 全系統測試 275 項全數通過，無任何回歸破壞。
- [x] 遵守唯讀報告原則，未刪除或覆寫既有審查報告。
- [x] 正式建立 `C2_5_2_REMEDIATION_REPORT.md`。

---
*(報告完成，遵循流程規範停步，靜待人工審查指示)*