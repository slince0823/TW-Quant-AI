# 第三階段 C-2.5.2.2.2 安全審查問題修補驗收報告
# (C2_5_2_2_2_REMEDIATION_REPORT.md)

**執行日期**：2026-10-21  
**任務名稱**：第三階段 C-2.5.2.2.2 — 針對 C-2.5.2.2.1 獨立安全審查四項問題（P0 ~ P3）之最小化修補  
**前置依據**：[`C2_5_2_2_1_INDEPENDENT_SECURITY_REVIEW.md`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/C2_5_2_2_1_INDEPENDENT_SECURITY_REVIEW.md)  
**修補狀態**：P0、P1、P2、P3 四項優先順序問題全數修正完成；新增 6 項針對性測試（含多重子測試），全系統 340 項測試全數通過（0 失敗、0 錯誤、0 跳過）。原始子專案 HEAD 與 Clean 狀態維持不變。

---

## 一、修改檔案清單

本次修補遵循「最小修改策略」，僅修改以下 4 個必要自建檔案，未重構其他模組，亦未觸碰兩個原始子專案：

1. [`src/tw_quant/backtest/crypto.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py)
   - 新增 [`InvalidRootAnchorError`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py#L233-L234)（同時繼承 `TypeError` 與 `ValueError`）。
   - 新增 [`_get_secure_replay_dir()`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py#L237-L256) 建立專屬安全目錄 `~/.tw_quant_security/replay_store`（`0700` 權限與防符號連結檢查）。
   - 於 [`OfficialTrustAuthority`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py#L265-L454) 實作 `_verify_test_caller()`、`is_official_production_configured()`，並將測試隔離狀態改為 `threading.local()` 儲存。
   - 於 [`TrustedPublicKeyStore.__init__`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py#L471-L519) 實作建構期 `root_trust_anchor` 型別、32 位元組長度與 RFC 8032 規範曲線點驗證。
   - 於 [`ReplayStore`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py#L708-L895) 實作 `_validate_path_security()`、`store_metadata` 實例識別碼 (`store_instance_token`) 與單調遞增序號 (`nonce_seq_count`)，偵測資料庫刪除、截斷、重建替換與舊備份回滾。
2. [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)
   - 於 [`BacktestReport.build`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L651-L905) 寫入 `is_test_isolated_verification` 與 `trust_environment`（區分 `"TEST_ISOLATED_ONLY"`、`"OFFICIAL_PRODUCTION"`、`"UNCONFIGURED"`）。
   - 新增 [`is_test_isolated_verification`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L965-L968) 與 [`is_official_production_verified`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L970-L979) 屬性。
   - 於 [`BacktestReport.from_json`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py#L1184-L1198) 攔截任何標記為 `TEST_ISOLATED_ONLY` 之報告在非測試隔離環境（正式驗證端）取得 `REAL_HISTORICAL_PIT_VERIFIED`。
3. [`src/tw_quant/backtest/__init__.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/__init__.py)
   - 匯出 `InvalidRootAnchorError`。
4. [`tests/test_backtest_metrics.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py)
   - 新增 [`TestC25222SecurityHardeningRemediation`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_backtest_metrics.py#L2895-L3185) 共 6 項針對性測試案例。

---

## 二、每項安全問題（P0 ~ P3）的修正方式

### 1. P0（對應 FINDING-C25221-01）：防止測試隔離機制讓不受信任程式取得正式 `REAL_HISTORICAL_PIT_VERIFIED` 身分

- **問題根因**：`activate_isolated_test_context()` 與 `isolated_test_context()` 為公開方法，且測試隔離下產出之報告未在資料結構中與正式生產驗證身分做不可逆區隔。
- **修正方式**：
  1. **呼叫堆疊來源查驗 (`OfficialTrustAuthority._verify_test_caller`)**：
     - 檢查整個呼叫堆疊，若堆疊中包含策略模組（`/tw_quant/strategy/`）或回測執行器（`runner.py`、`adapters.py`），立即拋出 `BacktestMetricsError("SECURITY_VIOLATION: 禁止於策略執行或回測主迴圈內啟動測試信任隔離環境")`。
     - 檢查直接外部呼叫端，必須源自 `tests/` 目錄下之 `test_*.py` 測試檔案，拒絕來自外部腳本（如 `<string>`、CLI 或生產程式碼）之呼叫。
  2. **報告環境標記與生產反序列化攔截**：
     - 在 `BacktestReport.build()` 中，凡處於測試隔離狀態下建構之報告，強制於 `data_lineage` 寫入 `"is_test_isolated_verification": True` 與 `"trust_environment": "TEST_ISOLATED_ONLY"`（已納入 SHA-256 `content_hash` 與 Ed25519 簽章保護範圍）。
     - 新增 `rep.is_official_production_verified` 屬性：在測試隔離狀態下永遠為 `False`。
     - 在 `BacktestReport.from_json()` 中，當正式驗證端（`not is_test_store`）載入報告時，若發現 `is_test_isolated_verification == True` 或 `trust_environment == "TEST_ISOLATED_ONLY"`，立即以 `TEST_ISOLATED_REPORT_REJECTED_IN_PRODUCTION` 拒絕或強制降級為 `REAL_HISTORICAL_NOT_VERIFIED`。
  3. **誠實揭露進程邊界限制（遵守限制 8）**：
     - 本報告明確聲明：**Python 進程內的私有方法、單例、`threading.local` / `ContextVar` 或堆疊檢查僅能作為進程內防誤用與縱深防禦機制，絕不等同於真正的惡意程式隔離邊界**（同進程惡意程式碼仍可透過 Python 反射、`sys._getframe`、`gc` 或記憶體操作繞過）。真正的惡意程式安全邊界必須透過作業系統層級之獨立進程／容器沙盒隔離，並確保正式驗證端與策略執行端分離。

### 2. P1（對應 FINDING-C25221-02）：修正測試隔離狀態的執行緒污染問題

- **問題根因**：`OfficialTrustAuthority._is_test_isolated`、`_test_store`、`_test_replay_store` 原為類別屬性（Class Attributes），在多執行緒環境下會污染其他並行執行緒。
- **修正方式**：
  - 改用 `cls._tls = threading.local()` 執行緒本地儲存管理 `is_test_isolated`、`test_store` 與 `test_replay_store`。
  - 當執行緒 A 啟動 `isolated_test_context` 或 `activate_isolated_test_context` 時，僅在執行緒 A 的 TLS 中生效；執行緒 B 查詢 `is_test_isolated()` 仍為 `False`、查詢 `status()` 仍為 `OFFICIAL_ROOT_NOT_CONFIGURED`，且在執行緒 B 中呼叫 `BacktestReport.build()` 仍維持 `REAL_HISTORICAL_NOT_VERIFIED`。

### 3. P2（對應 FINDING-C25221-03）：改善 `ReplayStore` 儲存路徑安全性，並處理資料庫遭刪除、替換、損毀及重建

- **問題根因**：預設路徑位於系統共用暫存目錄 `tempfile.gettempdir()`，且若運行中資料庫檔案遭外部刪除、截斷為 0 bytes、替換為新重建的空庫或還原為舊備份，原實例可能無法察覺狀態回滾。
- **修正方式**：
  1. **專屬安全目錄與防符號連結 (`_get_secure_replay_dir` & `_validate_path_security`)**：
     - 預設儲存路徑改為使用者專屬安全目錄 `~/.tw_quant_security/replay_store/`，建立時設定 `0700`（`stat.S_IRWXU`）權限。
     - 在初始化與每次 `consume_nonce` 前執行 `_validate_path_security()`，拒絕任何符號連結（`os.path.islink`）或目錄路徑。
  2. **刪除與空檔截斷偵測**：
     - `consume_nonce` 連線前明確檢查 `os.path.exists(self.db_path)` 與 `os.path.getsize(self.db_path) > 0`，防止 `sqlite3.connect` 在檔案被刪除後靜默建立新檔，立即拋出受控 `BacktestMetricsError (Fail-Closed)`。
  3. **實例 Token 與單調序號防替換／防回滾 (`store_metadata`)**：
     - `_init_db()` 建立 `store_metadata` 表，寫入隨機生成的唯一 `store_instance_token` 及單調遞增之 `nonce_seq_count`（即使過期 Nonce 被清理，該序號亦只增不減），並快取於 `ReplayStore` 實例的 `_expected_db_token` 與 `_min_expected_seq`。
     - 每次 `consume_nonce()` 在 `EXCLUSIVE` 事務內比對資料庫中的 `store_instance_token` 與 `nonce_seq_count`：
       - 若檔案遭刪除並以全新 `ReplayStore` 重建，觸發 `Token Mismatch` 並 Fail-Closed；
       - 若檔案遭覆蓋還原為舊備份，觸發 `Sequence Regression` 並 Fail-Closed；
       - 若檔案遭寫入垃圾位元組損毀，捕獲 `sqlite3.DatabaseError` 並 Fail-Closed。

### 4. P3（對應 FINDING-C25221-04）：在 `TrustedPublicKeyStore` 建構時驗證 `root_trust_anchor` 型別與 Ed25519 公鑰長度

- **問題根因**：`TrustedPublicKeyStore.__init__` 原先對非 32 位元組或非法型別未立即拋出例外，延遲至 `has_official_root` 才回傳 `False`。
- **修正方式**：
  - 新增 `InvalidRootAnchorError(TypeError, ValueError)`。
  - 在 `TrustedPublicKeyStore.__init__` 中，只要傳入非 `None` 之 `root_trust_anchor` 或 `official_root_public_key`：
    1. 檢查型別必須為 `bytes` 或 `str`（排除 `bool`、`int`、`list`、`dict` 等，否則立即拋出 `InvalidRootAnchorError`）。
    2. 若為 `str`，驗證必須為 64 字元合法 Hex 字串。
    3. 驗證解碼後位元組長度必須嚴格等於 32 位元組（`len(raw_bytes) == 32`）。
    4. 呼叫 `_decodepoint(raw_bytes)` 驗證該 32 位元組必須為合法的 RFC 8032 Ed25519 規範曲線點（拒絕 $y \ge p$ 或不在曲線上的無效點）。

---

## 三、實際執行的測試及結果

### 1. C-2.5.2.2.2 針對性測試 (`TestC25222SecurityHardeningRemediation`)

執行命令：
```powershell
$env:PYTHONPATH="src;tests"; python -m unittest -v tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation
```

實際輸出：
```text
test_p0_1_untrusted_strategy_or_non_test_caller_blocked_from_test_context (tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation.test_p0_1_untrusted_strategy_or_non_test_caller_blocked_from_test_context) ... ok
test_p0_2_test_isolated_report_lacks_production_official_identity_and_rejected_in_prod (tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation.test_p0_2_test_isolated_report_lacks_production_official_identity_and_rejected_in_prod) ... ok
test_p1_thread_local_test_isolation_prevents_cross_thread_pollution (tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation.test_p1_thread_local_test_isolation_prevents_cross_thread_pollution) ... ok
test_p2_1_replay_store_secure_default_directory_and_symlink_rejection (tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation.test_p2_1_replay_store_secure_default_directory_and_symlink_rejection) ... ok
test_p2_2_replay_store_detects_deletion_truncation_replacement_and_rollback (tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation.test_p2_2_replay_store_detects_deletion_truncation_replacement_and_rollback) ... ok
test_p3_trusted_public_key_store_validates_root_anchor_type_length_and_curve_point (tests.test_backtest_metrics.TestC25222SecurityHardeningRemediation.test_p3_trusted_public_key_store_validates_root_anchor_type_length_and_curve_point) ... ok

----------------------------------------------------------------------
Ran 6 tests in 7.009s

OK
```

### 2. C-2.5.2.2.1 既有對抗性安全測試 (`TestC25221TrustRootAndReplaySecurityRemediation`)

執行命令：
```powershell
$env:PYTHONPATH="src;tests"; python -m unittest -v tests.test_backtest_metrics.TestC25221TrustRootAndReplaySecurityRemediation
```

實際結果：15 項測試全數通過（耗時 33.049s，`OK`）。

### 3. 全系統完整回歸測試 (`unittest discover`)

執行命令：
```powershell
$env:PYTHONPATH="src;tests"; python -m unittest discover -s tests
```

實際輸出：
```text
Ran 340 tests in 157.823s

OK
```
- **總測試數**：340 項（原 334 項 + 新增 6 項）
- **結果**：**340 Passed, 0 Failed, 0 Errors, 0 Skipped**。

---

## 四、原始子專案 HEAD 與 Git 狀態

執行檢查確認兩個原始子專案完全未更動：
- `anti-gambling-trader-tw`：HEAD = `8609cef73718a056fddb74201fc304f174fa1553`，Working tree clean。
- `qlib-tw-trader`：HEAD = `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011`，Working tree clean。

---

## 五、尚未解決的安全風險與誠實揭露

1. **同進程惡意程式碼之本質限制（遵循限制 8 明確揭露）**：
   - Python 語言中任何基於私有方法、單例、`threading.local()`、`ContextVar` 或呼叫堆疊檢查（`inspect.stack()`）的機制，皆僅能防範非預期誤用或一般 API 層級的違規呼叫，**不能宣稱已建立真正的惡意程式隔離邊界**。若不受信任的惡意策略程式碼與驗證器運行於同一個作業系統進程（OS Process）中，惡意程式碼理論上仍可透過 Python 內部反射、`gc.get_objects()`、`ctypes` 或修改記憶體狀態繞過進程內檢查。
   - **正式生產架構要求**：未來若允許執行外部或不受信任之策略程式碼，必須將策略執行置於獨立的作業系統沙盒進程／容器中，並由獨立的受信任驗證進程執行 `BacktestReport` 驗證與簽章檢驗。
2. **官方離線 PKI 根金鑰尚未實際部署**：
   - 目前系統尚未配置正式官方根金鑰（預設維持 `OFFICIAL_ROOT_NOT_CONFIGURED`），所有非合成資料回測報告在正式環境下一律保守輸出 `REAL_HISTORICAL_NOT_VERIFIED`，不具備正式 `REAL_HISTORICAL_PIT_VERIFIED` 核發能力。
3. **跨程序重啟加檔案同時抹除之物理極限**：
   - `ReplayStore` 已能偵測運行期間的檔案刪除、截斷、重建替換與舊備份回滾；但若攻擊者具備作業系統檔案寫入權限，在進程完全終止後同時刪除資料庫檔案並重啟新進程，單機本地檔案系統無法憑空恢復已刪除之歷史記錄。在多節點或高安全生產環境中，應搭配作業系統嚴格 ACL 權限或外部具備稽核日誌之集中式資料庫。
4. **真實台股歷史資料與多窗口 Walk-Forward 尚未接入**：
   - 目前測試均基於合成資料，尚未接入真實台股歷史行情，亦不得宣稱任何實際交易策略獲利能力。
