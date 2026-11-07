# 第三階段 C-2.5.2-R 獨立技術複核報告 (Independent Technical Audit)

> **審查日期**：2026-10-21  
> **審查任務**：C-2.5.2-R — 資料血統信任根與 HMAC 防偽機制獨立複核 (Read-Only Technical Audit)  
> **複核目標**：針對 C-2.5.2 所修補之 R01（UTC 跨午夜時區轉換）、R05（`data_cutoff_time` 強制校驗）、R04（測試 Mock 隔離）、R02（資料指紋校驗與實體檔案摘要）與 R06（HMAC 反序列化防偽與信任邊界）進行唯讀程式碼審查、理論因果推演與可重現 PoC 驗證。  
> **基線狀態**：
> - 全系統測試：**275 / 275 Passed** (0 失敗, 0 錯誤, 0 跳過，耗時 0.454s)
> - 回測模組測試：119 / 119 Passed
> - `test_backtest_metrics.py`：50 / 50 Passed
> - `anti-gambling-trader-tw`: `8609cef73718a056fddb74201fc304f174fa1553` (Clean，未修改)
> - `qlib-tw-trader`: `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` (Clean，未修改)
> - 本次任務遵守嚴格唯讀限制：**不修改任何程式碼、不修改任何測試、不修改 `PROJECT_MAP.md`、不修改既有報告、不下載外部資料、不訓練模型**。
> 
> **重要聲明**：  
> **本次審查為唯讀獨立技術複核。全系統測試全數通過代表底層運算邏輯、時序阻斷與會計守恆在合成資料上運作正常，但絕對不代表真實台股歷史回測績效已獲驗證，亦不代表所有密碼學信任邊界已達到生產級公鑰公證水準。嚴禁將現有測試輸出宣稱為真實策略有效性或實盤依據。**

---

## 目錄

1. [審查範圍與未能檢查的部分](#一審查範圍與未能檢查的部分)
2. [R02：資料血統與信任根深度複核（含 10 大問題逐項解答）](#二r02資料血統與信任根深度複核)
3. [R06：HMAC 與反序列化信任邊界深度複核（含 10 大問題逐項解答）](#三r06hmac-與反序列化信任邊界深度複核)
4. [R01、R05、R04 回歸修補有效性檢查](#四r01r05r04-回歸修補有效性檢查)
5. [可重現的最小 PoC 驗證與實際終端執行結果](#五可重現的最小-poc-驗證與實際終端執行結果)
6. [最終 Verdict 決策閘門全路徑追蹤表](#六最終-verdict-決策閘門全路徑追蹤表)
7. [發現分級清單 (Findings Severity Matrix)](#七發現分級清單)
8. [每項發現之詳細說明與具體修補建議](#八每項發現之詳細說明與具體修補建議)
9. [已確認問題、待確認項目與推測性風險分類](#九已確認問題待確認項目與推測性風險分類)
10. [實際測試執行紀錄](#十實際測試執行紀錄)
11. [系統防偽邊界與真實歷史績效聲明](#十一系統防偽邊界與真實歷史績效聲明)

---

## 一、審查範圍與未能檢查的部分

### 1.1 複核範圍 (Audited Scope)
本次審查以靜態原始碼結構分析、呼叫路徑追蹤、密碼學信任邊界拆解與本機臨時測試 PoC，深入檢查以下模組：
1. **資料血統與證據驗證器**：
   - [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)：
     - `DatasetChecksumVerifier`（記憶體指紋比對與授權檢查）。
     - `FileDigestEvidenceVerifier`（實體檔案逐 chunk 串流 SHA-256 計算）。
     - `EvidenceVerificationResult` 與 `IndependentEvidenceVerifier` 協定。
     - `LineageAttestation` 呼叫端聲明與邊界。
2. **反序列化與 HMAC 防偽**：
   - `BacktestReport.sign`（HMAC-SHA256 簽署實作）。
   - `BacktestReport.from_json`（反序列化完整性校驗與驗證結論降級閘門）。
   - `BacktestReport.build`（最終驗證結論 verdict 聚合邏輯）。
3. **回歸因果防禦**：
   - `verify_prediction_consumption_pit`（UTC 跨午夜時區轉換、`data_cutoff_time` 必要性、時序關係）。
   - [`src/tw_quant/backtest/__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py)：公開匯出清單，確認 Mock 驗證器完全隔離。
4. **測試套件**：
   - [`tests/test_backtest_metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py) 中既有 50 項測試。

### 1.2 未能檢查與待驗證項目 (Out of Scope / Pending Verification)
1. **外部非對稱數位簽章硬體模組 (HSM / KMS)**：目前系統未串接任何外部金鑰管理服務，無實體硬體權杖可供檢驗。
2. **臺灣證券交易所 (TWSE) 官方資料集之真實 Manifest**：目前官方尚未發布公開之 Parquet SHA-256 Manifest，無真實公證清單比對。
3. **分散式大檔 I/O 效能**：未對數十 GB 以上歷史 Tick 資料進行串流雜湊壓測。

---

## 二、R02：資料血統與信任根深度複核

針對任務要求之 10 大關鍵問題，結合原始碼進行逐項獨立剖析：

### 1. `is_trusted_root=True` 是否只是呼叫端可自行指定的布林值？
**【是】**。  
檢查 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L221-L227)：
```python
@dataclass(frozen=True)
class FileDigestEvidenceVerifier:
    source_file_paths: Mapping[str, str] = field(default_factory=dict)
    expected_file_hashes: Mapping[str, str] = field(default_factory=dict)
    trusted_authority: Optional[str] = None
    is_trusted_root: bool = False
    verifier_name: str = "FileDigestEvidenceVerifier"
```
`is_trusted_root` 是建構子中的一個純 Python `bool` 欄位。呼叫端在呼叫 `FileDigestEvidenceVerifier(..., is_trusted_root=True)` 時，可完全隨意設定為 `True`。程式內部並未要求提供數位憑證或第三方公證簽章來證實該信任根的合法性。

### 2. `is_trusted_authority=True` 是否具有相同問題？
**【是】**。  
檢查 `DatasetChecksumVerifier`（第 134 行）：`is_trusted_authority: bool = False` 同樣由呼叫端於記憶體建構時傳入。只要呼叫端將其設為 `True`，即可繞過第 156 行的 `if not self.is_trusted_authority:` 檢查。

### 3. 呼叫端能否自行提供檔案路徑、預期 SHA-256、Manifest 與信任標記？
**【能】**。  
呼叫端可以建立本機任意臨時檔案，自行計算該檔案之 SHA-256，將其填入 `expected_file_hashes`，並傳入 `is_trusted_root=True`。驗證器會判定比對相符並回傳 `is_verified=True`（見第五章 PoC A2）。

### 4. 驗證器是否實際讀取檔案並重新計算 SHA-256？
**【是】**。  
檢查 `report.py` 第 280-289 行：
```python
hasher = hashlib.sha256()
file_size = 0
with open(path, "rb") as f:
    while chunk := f.read(65536):
        hasher.update(chunk)
        file_size += len(chunk)
actual_hex = hasher.hexdigest().lower()
actual_digest = f"sha256:{actual_hex}"
```
驗證器確實逐 64KB 區塊開啟並讀取磁碟上的二進位檔案，以標準函式庫 `hashlib.sha256()` 進行串流計算。這成功修補了 C-2.5.1 中「完全不讀取磁碟檔案」的重大缺陷。

### 5. 實際計算的摘要是否與可信任的預期摘要比對？
**【否】**。  
驗證器比對的對象是 `self.expected_file_hashes.get(src_name)`。該字典完全來自呼叫端記憶體輸入，並非讀取自受保護的官方設定檔或已簽署的公證 Manifest。因此，摘要的比對僅保證「實體檔案與呼叫端輸入之字串一致」，不能證明該預期摘要本身是「可信任」的。

### 6. 預期摘要的來源是否經過可驗證的數位簽章或其他可信任機制？
**【否】**。  
目前系統尚未實作非對稱數位簽章校驗機制（如 RSA / ECDSA / Ed25519）。呼叫端傳入之 Manifest 字典沒有任何電子簽章保護，亦未綁定公鑰認證體系。

### 7. 驗證公鑰或信任根是否可能由呼叫端任意替換？
**【是】**。  
`trusted_authority` 僅為自由字串（如 `"TWSEOfficial"`），呼叫端可傳入任意值，系統並無官方預埋的公鑰庫或憑證授權單位 (CA) 來校驗此授權字串。

### 8. 未經授權的 Manifest 是否可能透過其他 verifier 或 `BacktestReport.build` 路徑取得 `REAL_HISTORICAL_PIT_VERIFIED`？
**【能】**。  
任何呼叫端只要：
1. 傳入 `LineageAttestation(True, True, True, True, ("notes",))`。
2. 傳入 `FileDigestEvidenceVerifier(..., is_trusted_root=True)` 或自訂一個實作 `IndependentEvidenceVerifier` 協定且回傳 `is_verified=True` 的驗證器。
3. 資料標記為非合成、預測通過因果時序檢查。  
`BacktestReport.build` 即會輸出 `ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED`。

### 9. `FileDigestEvidenceVerifier` 是否只證明檔案內容一致，卻未證明來源真實性？
**【完全正確】**。  
`FileDigestEvidenceVerifier` 在密碼學定位上屬於**檔案完整性檢查器 (Data Integrity Verifier)**，而非**來源真實性公證器 (Origin Authenticity Verifier)**。它能保證磁碟檔案在回測時未遭意外修改或單元損毀（1 byte 變更可被攔截），但無法證明該檔案中的數據是臺灣證交所公布的真實歷史交易。

### 10. 是否存在將驗證器回傳的 `is_verified=True` 當成充分條件，而忽略其他必要證據的路徑？
**【不存在單一充分條件，但存在複合繞過路徑】**。  
追蹤 `BacktestReport.build`（第 557-562 行）：
```python
if any_synth:
    verdict = ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY
elif all_attested and nav_complete and run_completed and independent_verified:
    verdict = ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED
else:
    verdict = ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED
```
除了 `independent_verified` 外，還必須滿足 `all_attested`（各資料源皆為 `REAL_PIT_ATTESTED`）、`nav_complete`、`run_completed` 且 `not any_synth`。若呼叫端傳入 `LineageAttestation(False, ...)`，即便驗證器為 True，結論依然會保守降級為 `REAL_HISTORICAL_NOT_VERIFIED`（見第五章 PoC A6）。然而，因為 `all_attested` 也是由呼叫端傳入之布林聲明所驅動，因此只要呼叫端將所有布林值全設為 True，即可同時解開所有門禁。

---

## 三、R06：HMAC 與反序列化信任邊界深度複核

針對反序列化信任邊界之 10 大問題進行逐項剖析：

### 1. HMAC 是否涵蓋所有會影響驗證結論的欄位？
**【是】**。  
檢查 `BacktestReport.sign`（第 779-783 行）：
```python
c = copy.deepcopy(self._content)
c.pop("signature", None)
body = json.dumps(c, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
sig = hmac.new(secret.encode("utf-8"), payload_hash.encode("utf-8"), hashlib.sha256).hexdigest()
```
`sign` 方法將除了 `signature` 之外的整份報告字典 `_content`（包含 `data_lineage`、`metrics`、`run`、`models_and_versions` 等）全部進行確定性序列化並計算 SHA-256，再對此雜湊計算 HMAC。因此，報告內任何一個欄位遭到竄改，HMAC 均會失效。

### 2. 簽章計算與驗證是否使用完全一致且具明確規格的 canonical serialization？
**【是】**。  
`sign` 與 `from_json`（第 781 行與第 842 行）皆採用：
`json.dumps(c, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
排序鍵、停用 ASCII 轉義並移除空白字元，序列化字串完全確定且具跨平台一致性。

### 3. `trusted_secret` 由誰提供？一般呼叫端是否能自行指定或替換？
**【由呼叫端直接於函式引數傳入】**。  
在 `BacktestReport.from_json(text, *, trusted_secret=None, ...)` 中，密鑰完全作為函式參數提供。系統內部沒有安全密鑰儲存區（如安全環境變數、OS Keyring 或 KMS），呼叫端可傳入任意自選的密鑰。

### 4. 未知密鑰、錯誤密鑰、空密鑰與缺少簽章是否全部保守拒絕？
**【是】**。  
檢查第 835-855 行：
- 若未提供 `trusted_secret`（為 `None`），`signature_valid` 為 `False`。
- 若提供錯誤密鑰，`hmac.compare_digest` 回傳 `False`。
- 若 JSON 中缺少 `signature` 欄位，`signature_valid` 為 `False`。
- 當 `signature_valid` 為 `False` 時：
  * 若 `strict_reject_unverified_load=True` $\to$ 拋出 `BacktestMetricsError` 拒絕載入。
  * 預設情況下 $\to$ **強制降級為 `REAL_HISTORICAL_NOT_VERIFIED`**，將 `claims_real_historical_verified` 設為 `False`，並寫入 `[DESERIALIZATION_TRUST]` 警告。
  此部分處理嚴格且保守（見第五章 PoC B2、B3）。

### 5. 攻擊者修改 verdict、claims flag、資料血統或驗證證據後，能否重算無密鑰 SHA-256 並通過？
**【不能】**。  
若攻擊者僅修改 JSON 內容並以標準 `hashlib.sha256` 重新計算 `content_hash`，在 `from_json` 時，由於攻擊者無法出具與 `trusted_secret` 相符的 HMAC 簽章，第 856 行會強制將其降級為 `REAL_HISTORICAL_NOT_VERIFIED`。C-2.5.1 的無密鑰 SHA-256 篡改漏洞（案例 3）已被成功封堵。

### 6. 攻擊者若自行建立新的報告並自行簽署，是否可能讓系統誤以為它來自可信任的發行者？
**【CRITICAL：若呼叫端自行提供驗證密鑰，全盤防偽將被突破】**。  
HMAC 是**對稱式加密訊息鑑別碼 (Symmetric MAC)**。在對稱密碼學中，「簽署金鑰」與「驗證金鑰」是完全相同的。  
如果驗證端允許由呼叫端輸入 `trusted_secret`：
攻擊者可以使用自己選定的密鑰 `attacker_secret` 簽署一份假報告，然後呼叫：
`BacktestReport.from_json(tampered_json, trusted_secret="attacker_secret")`
系統比對兩者相符，即會判定簽章有效並維持 `REAL_HISTORICAL_PIT_VERIFIED`（見第五章 PoC B4b）！  
**結論**：HMAC 只能證明「驗證者持有的密鑰與簽署者相同」，**無法證明「簽署者是官方授權發行者」**。

### 7. 舊版 JSON 是否能在缺少可信證據的情況下恢復已驗證結論？
**【不能】**。  
舊版未簽署之 JSON 在反序列化時，因缺少 `signature` 物件，會自動觸發非簽章降級，安全降為 `REAL_HISTORICAL_NOT_VERIFIED`。

### 8. 已簽章報告是否存在重放、跨任務複用或簽章上下文不足的問題？
**【HIGH：存在重放攻擊風險】**。  
檢查 `signature` 結構（第 784-789 行）：
```python
"signature": {
    "algorithm": "HMAC-SHA256",
    "signature_hex": sig,
    "signed_payload_hash": payload_hash,
    "signed_at": dt.datetime.now(TAIPEI_TZ).isoformat(),
}
```
簽章內**完全缺少過期時間 (Expiration Time / TTL)**，亦缺少環境唯一識別碼（如伺服器 Instance ID、專案識別碼、隨機 Nonce）。一份合法的已簽章報告可以在 1 年後、在另一個不相干的系統或回測任務中被無限制地重新載入，且依然被判定為驗證有效（見第五章 PoC B5）。

### 9. 嚴格模式是否在所有無法驗證的情況下拒絕載入？
**【是】**。  
當 `strict_reject_unverified_load=True` 時，任何宣稱已驗證但簽章不符或缺簽章的報告，均直接拋出 `BacktestMetricsError` 阻斷執行。

### 10. 非嚴格模式是否確實降級，且沒有其他程式碼再次把結論提升？
**【是】**。  
降級時直接修改記憶體中字典之 `validation_verdict` 與 `claims_real_historical_verified`，並重新計算 `rep._hash`。此屬不可變物件的最終回傳狀態，無二次提升路徑。

---

## 四、R01、R05、R04 回歸修補有效性檢查

### 4.1 R01：時區跨午夜因果防護
檢查 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L380-L413)：
- `trained_cut.astimezone(TAIPEI_TZ).date()`：正確轉換為台北市場日期。
- `dec_date.astimezone(TAIPEI_TZ).date()`：正確轉換為台北市場日期。
- naive datetime 檢查：`val.tzinfo is None or val.tzinfo.utcoffset(val) is None` 正確拒絕。
- `trained_cut >= dec_cut`：在台北時區下進行精確時間戳比較。
- **結論傳遞**：若檢查失敗，回傳 `(False, reason)`，`pred_pit_ok = False`，`BacktestReport.build` 強制將 verdict 降級為 `REAL_HISTORICAL_NOT_VERIFIED`。修補完全正確。

### 4.2 R05：`data_cutoff_time` 必要性檢查
檢查第 349-370 行：
- `if data_cut is None:` 強制返回 `False, f"批次 {bid} 缺少 data_cutoff_time 時間戳"`。
- naive datetime 檢查納入 `("data_cutoff_time", data_cut)`。
- `data_cut > gen_at` 因果檢核正常運作。
- **結論傳遞**：缺少特徵時間戳時，絕對無法輸出 `REAL_HISTORICAL_PIT_VERIFIED`。修補完全正確。

### 4.3 R04：Mock 驗證器生產隔離
- 檢索 `src/` 目錄：確認 `MockIndependentEvidenceVerifier` 已無任何定義或匯入。
- 檢索 `tw_quant.backtest.__all__`：確認已無該符號。
- 測試層：僅在 `tests/test_backtest_metrics.py` 頂層定義，僅供測試沙盒使用。修補完全正確。

---

## 五、可重現的最小 PoC 驗證與實際終端執行結果

本次複核透過獨立腳本（執行於本機沙盒環境，不接觸外部資料）實測所有關鍵攻擊情境：

### 5.1 PoC 執行結果總覽表

| PoC 編號 | 測試情境說明 | 觸發條件 | 預期防護行為 | 實際執行結果 | 安全評估 |
| :---: | :--- | :--- | :--- | :--- | :---: |
| **PoC-A1** | 呼叫端自行指定 `is_trusted_authority=True` | `DatasetChecksumVerifier` 傳入偽造 hash + `is_trusted_authority=True` | 應辨識為未授權並拒絕 | **`REAL_HISTORICAL_PIT_VERIFIED`** | **CRITICAL (繞過)** |
| **PoC-A2** | 呼叫端自製檔案並指定 `is_trusted_root=True` | `FileDigestEvidenceVerifier` 傳入自製檔案 + `is_trusted_root=True` | 應辨識自製 Manifest 並拒絕 | **`REAL_HISTORICAL_PIT_VERIFIED`** | **CRITICAL (繞過)** |
| **PoC-A3** | 實體檔案內容與 expected_hash 不符 | 實體檔案篡改 1 byte | 串流雜湊不符，降級為 NOT_VERIFIED | **`REAL_HISTORICAL_NOT_VERIFIED`** | **PASSED (成功防禦)** |
| **PoC-A4** | 實體檔案不存在 | 指定不存在之路徑 | 檔案不存在，降級為 NOT_VERIFIED | **`REAL_HISTORICAL_NOT_VERIFIED`** | **PASSED (成功防禦)** |
| **PoC-A5** | 未知資料來源 | 檔案路徑字典未配置必要來源 | 缺少來源設定，降級為 NOT_VERIFIED | **`REAL_HISTORICAL_NOT_VERIFIED`** | **PASSED (成功防禦)** |
| **PoC-A6** | Verifier 成功但缺少 `LineageAttestation` | 驗證器通過，但聲明布林值為 False | 缺少聲明，降級為 NOT_VERIFIED | **`REAL_HISTORICAL_NOT_VERIFIED`** | **PASSED (成功防禦)** |
| **PoC-B1** | 修改 JSON 但不重算 SHA-256 | 直接竄改 strategy_id | 拋出 `BacktestMetricsError` (雜湊不符) | **拋出例外阻斷** | **PASSED (成功防禦)** |
| **PoC-B2** | 竄改結論並重算 SHA-256 (無密鑰) | 竄改 verdict 為 PIT_VERIFIED 並重算 hash | 無密鑰無法過 HMAC，降級為 NOT_VERIFIED | **`REAL_HISTORICAL_NOT_VERIFIED`** | **PASSED (成功防禦)** |
| **PoC-B3** | 嚴格模式下無密鑰反序列化 | 竄改 verdict，`strict_reject=True` | 拋出 `BacktestMetricsError` | **拋出例外阻斷** | **PASSED (成功防禦)** |
| **PoC-B4a** | 攻擊者密鑰簽名 vs 官方管理密鑰驗證 | 攻擊者用自選 key 簽名，驗證端用官方 key 驗證 | 簽章比對失敗，降級為 NOT_VERIFIED | **`REAL_HISTORICAL_NOT_VERIFIED`** | **PASSED (成功防禦)** |
| **PoC-B4b** | 攻擊者自選密鑰簽名並注入驗證端 | 呼叫端對稱傳入相同的攻擊者密鑰 | 應辨識非官方發行者並拒絕 | **`REAL_HISTORICAL_PIT_VERIFIED`** | **HIGH (對稱密鑰缺陷)** |
| **PoC-B5** | 已簽章合法報告之跨任務重放 | 取得合法簽章 JSON，於另一個任務還原 | 應檢測過期或任務綁定 | **`REAL_HISTORICAL_PIT_VERIFIED`** | **MEDIUM (重放漏洞)** |

### 5.2 終端實際輸出截圖紀錄
執行命令：
```powershell
python "C:\Users\USER\.gemini\antigravity\brain\774d286b-448d-4b37-af33-f3ede61e8ce2\scratch\verify_pocs.py"
```
實際終端輸出：
```text
=== Starting PoC Verifications ===

--- PoC Group A: R02 Trust Root & File Digest ---
PoC A1 (Caller is_trusted_authority=True): verdict = REAL_HISTORICAL_PIT_VERIFIED
PoC A2 (Caller creates fake files + is_trusted_root=True): verdict = REAL_HISTORICAL_PIT_VERIFIED
PoC A3 (1-byte tampered file content): verdict = REAL_HISTORICAL_NOT_VERIFIED
PoC A4 (Missing file): verdict = REAL_HISTORICAL_NOT_VERIFIED
PoC A5 (Unknown source): verdict = REAL_HISTORICAL_NOT_VERIFIED
PoC A6 (Verifier OK but missing attestation): verdict = REAL_HISTORICAL_NOT_VERIFIED

--- PoC Group B: R06 HMAC & Deserialization ---
PoC B1 (Modified JSON, unadjusted SHA-256): Caught exception -> 報告 content_hash 不符，內容可能遭傳輸損毀或未一致性修改
PoC B2 (Modified JSON + recomputed SHA-256, no secret): verdict = REAL_HISTORICAL_NOT_VERIFIED, claims = False
PoC B3 (Strict mode rejection): Caught exception -> 反序列化報告宣稱 REAL_HISTORICAL_PIT_VERIFIED 但缺少受信任之數位簽章或簽章無效，依安全原則拒絕載入
PoC B4a (Attacker signed, official verifies with official secret): verdict = REAL_HISTORICAL_NOT_VERIFIED
PoC B4b (Attacker signed, caller supplies attacker secret to from_json): verdict = REAL_HISTORICAL_PIT_VERIFIED
PoC B5 (Replay of signed report in different context): verdict = REAL_HISTORICAL_PIT_VERIFIED, run_id = WF-RUN-0001
=== PoC Verifications Completed ===
```

---

## 六、最終 Verdict 決策閘門全路徑追蹤表

本表完整追蹤資料從輸入、撮合、估值、預測、檔案校驗到反序列化的所有條件如何影響最終回測報告之結論：

| 檢查階段 | 觸發條件／系統狀態 | `validation_verdict` | `claims_real_historical_verified` | 備註與附加狀態 |
| :--- | :--- | :---: | :---: | :--- |
| **來源合成標記** | 回測結果標記 `is_synthetic=True`，或行情／日曆／預測／股利／基準任一來源為合成 | `SYNTHETIC_TEST_RESULT_ONLY` | `False` | 門禁最優先，直接短路所有驗證器 |
| **模型型態檢查** | `consumed_predictions` 任一記錄標記 `is_mock=True` | `SYNTHETIC_TEST_RESULT_ONLY` | `False` | Mock 模型強制視為合成測試 |
| **預測時序 (PIT)** | `verify_prediction_consumption_pit` 失敗（如 UTC 跨日逃逸、`data_cutoff_time=None`、時序倒流） | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | `all_attested` 判定失敗，記入 `warnings` |
| **呼叫端聲明** | `LineageAttestation` 任一布林欄位為 `False` | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | 聲明不完整，判定為未證實 |
| **估值完整性** | `nav_complete=False`（NAV 有遺漏交易日或 NAV <= 0） | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | NAV 不完整，記入 `unverified_items` |
| **回測執行狀態** | `result.status != BacktestRunStatus.COMPLETED` | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | 中途終止或報錯之回測不可驗證 |
| **驗證器配置** | `evidence_verifier=None` | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | 標記為 `NOT_CONFIGURED` |
| **指紋授權根** | `DatasetChecksumVerifier` 之 `is_trusted_authority=False` | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | 標記 `UNTRUSTED_CALLER_MANIFEST` |
| **實體檔案信任根** | `FileDigestEvidenceVerifier` 之 `is_trusted_root=False` | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | 標記 `UNTRUSTED_MANIFEST_ROOT` |
| **實體檔案校驗** | 實體檔案不存在、非檔案，或串流 SHA-256 與預期不符 (哪怕 1 byte) | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | 標記 `FAILED`，列出實際與預期雜湊 |
| **授權驗證通過** | 檔案與雜湊全相符，非合成，因果合規，NAV 完整，且設定信任標記 | `REAL_HISTORICAL_PIT_VERIFIED` | `True` | 記憶體建構通過之正式驗證結論 |
| **反序列化傳輸** | JSON 欄位被改動且未重新計算 SHA-256 | **拋出例外阻斷** | - | `BacktestMetricsError` (雜湊損毀) |
| **反序列化無簽章** | JSON 宣稱 PIT_VERIFIED，但無簽章或簽章密鑰錯誤（預設模式） | `REAL_HISTORICAL_NOT_VERIFIED` | `False` | **強制保守降級**，附加 `[DESERIALIZATION_TRUST]` 警告 |
| **反序列化無簽章** | JSON 宣稱 PIT_VERIFIED，但無簽章或簽章密鑰錯誤（嚴格模式） | **拋出例外阻斷** | - | `BacktestMetricsError` (拒絕載入) |
| **反序列化合法** | JSON 具備 HMAC-SHA256 且傳入匹配之 `trusted_secret` | `REAL_HISTORICAL_PIT_VERIFIED` | `True` | 驗證通過，保留已驗證結論 |

---

## 七、發現分級清單 (Findings Severity Matrix)

| 發現代號 | 嚴重程度 | 模組與位置 | 核心問題摘要 |
| :---: | :---: | :--- | :--- |
| **FINDING-2.5.2-01** | `HIGH` | `report.py` (L134, L225) | 信任根由呼叫端純布林旗標控制 (`is_trusted_root` / `is_trusted_authority`)，任何人皆可自行置為 True 繞過防線 |
| **FINDING-2.5.2-02** | `HIGH` | `report.py` (L783, L845) | 對稱金鑰 HMAC 無法防範呼叫端自製密鑰自簽自驗，缺乏發行者公鑰證書鏈與身份隔離 |
| **FINDING-2.5.2-03** | `MEDIUM` | `report.py` (L784-L789) | 數位簽章缺少過期時間 (TTL) 與任務綁定 Nonce，已簽署報告可被永久跨任務重放複用 |
| **FINDING-2.5.2-04` | `LOW` | `report.py` (L280-L305) | `FileDigestEvidenceVerifier` 僅能證明檔案內容與預期一致，無法證明該檔案真正採集自官方 TWSE |
| **FINDING-2.5.2-05` | `INFO` | `contract.py` / `report.py` | 延續未解限制：訓練標籤未來跨期洩漏盲區（Label Horizon Blindspot），特徵契約尚無 `label_cutoff_date` |

---

## 八、每項發現之詳細說明與具體修補建議

### 8.1 針對 FINDING-2.5.2-01：布林信任根漏洞
- **問題位置**：`src/tw_quant/backtest/report.py` 第 134 行與第 225 行。
- **重現方式**：呼叫端於 Python 記憶體建立 `FileDigestEvidenceVerifier(..., is_trusted_root=True)`（見 PoC A2）。
- **實際風險**：任何程式碼呼叫端只要傳入 `is_trusted_root=True`，系統便視其為官方公證根，導致自行製造的測試資料能直接獲得 `REAL_HISTORICAL_PIT_VERIFIED`。
- **具體建議**：
  在進入真實歷史回測階段（C-3）時，**廢除布林旗標授權**。改為要求驗證器讀取實體 `manifest.json` 與其對應的官方非對稱數位簽章檔案 `manifest.json.sig`，並使用系統預置且唯讀的官方公鑰（Public Key）進行驗證。未通過公鑰簽章檢驗之 Manifest，一律不得授權。

### 8.2 針對 FINDING-2.5.2-02：對稱金鑰 HMAC 身份偽造
- **問題位置**：`src/tw_quant/backtest/report.py` 第 783 行與第 845 行。
- **重現方式**：呼叫端自行呼叫 `rep.sign("attacker_secret")`，再以 `from_json(..., trusted_secret="attacker_secret")` 載入（見 PoC B4b）。
- **實際風險**：HMAC 屬於對稱式演算法。當 `trusted_secret` 暴露給呼叫端或由呼叫端自行提供時，呼叫端既是簽署者又是驗證者，無法建立真正的第三方審計信任邊界。
- **具體建議**：
  報告簽署應升級為**非對稱數位簽章（如 ECDSA / Ed25519 或 RSA-PSS）**：
  1. 回測驗證伺服器持有**私鑰 (Private Key)**，負責在回測通過時為報告簽署。
  2. 報告反序列化與審計端僅持有公開的**公鑰 (Public Key)**，只能驗證簽章，無法自行偽造或重新簽署。

### 8.3 針對 FINDING-2.5.2-03：簽章重放與缺少上下文
- **問題位置**：`src/tw_quant/backtest/report.py` 第 784-789 行。
- **重現方式**：將過去合法簽署的 JSON 檔案，置入另一個策略或回測流程中執行 `from_json`（見 PoC B5）。
- **實際風險**：一份曾經通過的報告可以被無限期重複提交，冒充為最新回測結果。
- **具體建議**：
  在 `signature` 物件中強制加入：
  1. `valid_until` 或 `expires_at`：設定簽章有效期（例如 30 天）。
  2. `system_environment_hash`：將程式碼 Git Commit Hash 與相依版本納入簽章內容，防止程式碼改動後舊報告依然有效。

---

## 九、已確認問題、待確認項目與推測性風險分類

依據審查嚴謹性規範，明確劃分本次技術複核之發現屬性：

### 9.1 已確認之邏輯與架構問題 (Confirmed Vulnerabilities)
1. **布林信任根可由呼叫端直接指定**（經 PoC A1、A2 程式實證）。
2. **對稱密鑰 HMAC 無法防止自簽自驗**（經 PoC B4b 程式實證）。
3. **已簽章報告無過期時間與重放保護**（經 PoC B5 程式實證）。
4. **實體檔案校驗器無法證明來源真實性**（經原始碼架構推演確認）。

### 9.2 未能確認之項目 (Pending Verification / Inconclusive)
1. **TWSE 官方未來發布之 Manifest 二進位格式**：目前尚無官方標準，無法確認是否採用 JSON、XML 或二進位 ASN.1。
2. **大檔串流雜湊對回測效能之實質影響**：在合成資料上耗時 < 1ms，但對數十 GB 之逐筆資料仍待量測。

### 9.3 推測性與跨階段架構風險 (Speculative / Cross-Phase Architectural Risks)
1. **模型標籤前視洩漏 (Label Lookahead)**：若模型特徵截止於 T 日，但預測標籤跨越至 T+10 日，模型訓練期實質上看過未來價格走勢。此限制屬於離線機器學習訓練階段範疇，回測層目前受限於資料契約未定義該欄位而無法攔截。

---

## 十、實際測試執行紀錄

本次審查執行之全系統測試輸出（絕無虛構）：
```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
```
**終端實際輸出**：
```text
...................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 275 tests in 0.454s

OK
```
- 275 項測試全數通過，無任何失敗、錯誤或跳過。
- 回測模組測試（119 項）全數通過。
- 效能耗時 0.454 秒，系統整體運作穩定。

---

## 十一、系統防偽邊界與真實歷史績效聲明

> [!CAUTION]
> ### 系統防偽邊界嚴正重申
> 1. **全合成測試環境**：本系統目前所有測試案例均在顯式標記 `is_synthetic=True` 之沙盒資料上執行。
> 2. **零真實資料接入**：本系統**尚未接入任何臺灣證券交易所 (TWSE) 或證券櫃檯買賣中心 (TPEx) 之真實歷史行情**，**尚未接入真實除權息資料**。
> 3. **零真實策略驗證**：目前產出之所有報酬率、夏普值與最大回撤等指標，僅用於檢驗數學公式與對帳邏輯之正確性，**絕不代表任何真實台股獲利能力，亦嚴禁作為投資決策依據**。
> 4. **信任邊界過渡期性質**：目前實作之 `FileDigestEvidenceVerifier` 與 HMAC 簽章機制屬於「防範非預期損毀與本機呼叫邊界隔離」之過渡實作，**尚未具備抵抗惡意偽造的生產級 PKI 信任根**。在完成 C-3 資料規格定義與非對稱公鑰公證機制前，不得宣稱資料血統已達生產級安全。

---
*(報告完成，嚴格遵守唯讀限制，未修改任何程式碼與專案設定，請靜待人工審查指示)*