# 第三階段 C-2.5.2.2：信任根與 Verdict 決策閘門安全修補報告
# (C2_5_2_2_REMEDIATION_REPORT.md)

**執行日期**：2026-10-21  
**任務階段**：第三階段 C-2.5.2.2 安全加固與防偽修補  
**修補狀態**：全數修補完成，全系統 319 項測試通過（含 28 項安全 PoC 測試與 3 項 RFC 8032 官方向量測試），0 失敗，0 錯誤  
**審查判定依據**：`C2_5_2_1_INDEPENDENT_SECURITY_REVIEW.md`（原始判定：FAIL）

---

## 一、修補任務背景與依據

在第三階段 C-2.5.2.1 引入純 Python Ed25519 非對稱數位簽章與受控公鑰庫後，獨立安全複核（`C2_5_2_1_INDEPENDENT_SECURITY_REVIEW.md`）判定為 **FAIL**，指出 7 項安全漏洞與邊界缺陷：

1. **FINDING-01 [CRITICAL] 信任根未閉環與 OFFICIAL 等級可自封**：呼叫端可自行建構 `TrustedPublicKeyStore` 並將自製金鑰登記為 `KeyAuthorityLevel.OFFICIAL`，使偽造報告取得 `REAL_HISTORICAL_PIT_VERIFIED`。
2. **FINDING-02 [CRITICAL] 決策閘門依賴物件屬性（Duck Typing）**：`BacktestReport.build()` 僅依賴 `verifier_result` 之 `is_verified` 與 `is_official_attested` 屬性，自製類別可透過設定屬性繞過決策閘門。
3. **FINDING-03 [HIGH] 防重放機制缺乏持久化與強制上下文比對**：未實作跨進程持久化 Replay Store，隨機數 Nonce 無重複使用檢查，`expected_run_id` 等比對參數為選擇性輸入。
4. **FINDING-04 [MEDIUM] 簽章時間戳檢查不完整**：缺少 `signed_at <= current_time` 與 `expires_at > signed_at` 等必要因果時序檢驗，容許 naive datetime。
5. **FINDING-05 [MEDIUM] Ed25519 點解碼未拒絕非規範編碼**：`_decodepoint` 未檢驗 $y \ge p$，存在簽章延展性風險（RFC 8032 Section 5.1.3）。
6. **FINDING-06 [LOW] 純 Python 大數運算時序側信道邊界**：未在文件與報告中完整闡述純 Python 大數模運算之非常數時間側信道特性與防範建議。
7. **FINDING-07 [INFO] 缺少 RFC 8032 官方測試向量**：缺少 RFC 8032 Section 7.1 Test Vector 1 之永久單元測試。

本階段（C-2.5.2.2）針對上述 7 項問題進行徹底修補，並實施 28 項專屬安全 PoC 測試以驗證信任邊界。

---

## 二、Git 狀態與原始子專案 HEAD 核實

本修補嚴格遵守專案限制，絕不改動外部子專案核心：

| 專案名稱 | 角色定位 | 目錄路徑 | 期望 Commit SHA | 實際 Commit SHA | 工作區狀態 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **anti-gambling-trader-tw** | 防賭與風控核心 | `./anti-gambling-trader-tw/` | `8609cef73718a056fddb74201fc304f174fa1553` | `8609cef73718a056fddb74201fc304f174fa1553` | Clean (未改動) |
| **qlib-tw-trader** | 資料管線與特徵庫 | `./qlib-tw-trader/` | `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` | `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` | Clean (未改動) |

---

## 三、7 項 Finding 修補前後對比

| Finding ID | 嚴重度 | 修補前缺陷 | 修補後防禦邏輯 | 所在檔案與位置 |
| :--- | :--- | :--- | :--- | :--- |
| **FINDING-01** | **CRITICAL** | `TrustedPublicKeyStore.enroll_key()` 允許呼叫端直接傳入 `authority_level=OFFICIAL` 自簽自驗。 | (1) `TrustedPublicKeyStore` 必須配置官方根信任錨點（Root Trust Anchor）；(2) 登記 `OFFICIAL` 金鑰必須附有官方根私鑰之有效背書簽章；(3) 未配置官方根錨點時，一律拋出 `ValueError("OFFICIAL_ROOT_NOT_CONFIGURED")`；(4) `generate_ed25519_keypair` 預設等級強制為 `TEST_ONLY`。在未配置官方根金鑰前，回測結論一律保守輸出 `REAL_HISTORICAL_NOT_VERIFIED`。 | `src/tw_quant/backtest/crypto.py` L236-L305 |
| **FINDING-02** | **CRITICAL** | `BacktestReport.build()` 僅依賴 `verifier_result.is_verified` 與 `is_official_attested` 屬性，自製類別可偽造通過。 | (1) 強制型別門禁：`evidence_verifier` 必須為受控型別 `(DatasetChecksumVerifier, FileDigestEvidenceVerifier)`，非受控類別一律判定 `UNAUTHORIZED_VERIFIER_TYPE` 並拒絕；(2) 多因子交叉校驗：直接自驗證器內部讀取 `trusted_key_store`、`manifest_signer_id`，檢驗其官方根錨點與背書等級；(3) 即使驗證器回傳 True，缺乏官方根錨點時一律輸出 `REAL_HISTORICAL_NOT_VERIFIED`。 | `src/tw_quant/backtest/report.py` L606-L655 |
| **FINDING-03** | **HIGH** | 無持久化重放防護，Nonce 記憶體管理易失效，反序列化 `expected_run_id` 等參數為選擇性。 | (1) 實作 SQLite WAL 模式、EXCLUSIVE 鎖定之 `ReplayStore`，持久化跨進程儲存已使用 Nonce；(2) 構造六元組 `scope_key`（`signer_id:project_id:environment_id:run_id:report_id:nonce`），重複消費原子性拋出 `BacktestMetricsError`；(3) 反序列化宣稱 `REAL_HISTORICAL_PIT_VERIFIED` 報告時，強制要求 `expected_run_id`、`expected_strategy_id`、`expected_project_id`、`expected_environment_id` 缺一不可。 | `src/tw_quant/backtest/crypto.py` L400-L490, `src/tw_quant/backtest/report.py` L1090-L1190 |
| **FINDING-04** | **MEDIUM** | 簽章時間戳檢驗不完整，缺少未來簽章拒絕與簽署期限正向檢查。 | 實作 `validate_signature_timestamps`：(1) 支援字串與 datetime；(2) 嚴格拒絕 naive datetime；(3) 強制 `expires_at > signed_at`；(4) 強制 `signed_at <= current_time + max_skew`（防範未來簽章）；(5) 強制 `current_time <= expires_at`（防範過期簽章）。 | `src/tw_quant/backtest/crypto.py` L337-L394 |
| **FINDING-05** | **MEDIUM** | `_decodepoint` 未檢驗 $y \ge p$，存在非規範點編碼與延展性漏洞。 | 依 RFC 8032 Section 5.1.3 規範，在 `_decodepoint` 中增加嚴格校驗：若 $y \ge p$ 立即拋出 `ValueError("RFC 8032 5.1.3: y coordinate must be strictly less than p")`。 | `src/tw_quant/backtest/crypto.py` L64-L68 |
| **FINDING-06** | **LOW** | 未闡述純 Python 大數運算時序側信道邊界與防護措施。 | 在文件、模組 Docstring 與報告中明確記錄：純 Python 任意精度大數運算非常數時間，易受時序側信道分析；定位為離線驗收防偽，生產環境簽章私鑰應存於隔離環境或 HSM。 | `src/tw_quant/backtest/crypto.py` L24-L38, 本報告第七節 |
| **FINDING-07** | **INFO** | 缺少 RFC 8032 官方測試向量單元測試。 | 於 `tests/test_backtest_metrics.py` 新增 `TestRFC8032OfficialVectors`，永久覆蓋 RFC 8032 Section 7.1 Test Vector 1（空訊息簽章、公鑰生成、訊息竄改 1 位元失敗、簽章竄改 1 位元失敗）。 | `tests/test_backtest_metrics.py` L1780-L1820 |

---

## 四、信任根架構與受控公鑰庫運作原理

### 1. 官方信任根（Root Trust Anchor）機制
- 系統不再信任呼叫端在記憶體中自行建立的任何公鑰為 OFFICIAL。
- `TrustedPublicKeyStore` 支援在初始化時傳入 `root_trust_anchor` / `official_root_public_key`，或由環境變數 `TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY` 載入官方根信任錨點（32 位元組 Ed25519 公鑰）。
- 若未配置根錨點，`store.has_official_root` 為 `False`。

### 2. 官方背書簽章（Root Endorsement Signature）
- 欲登記 `KeyAuthorityLevel.OFFICIAL` 之公鑰，必須提供根私鑰對規範字串 `f"OFFICIAL_GRANT:{signer_id}:{pk_bytes.hex()}".encode("utf-8")` 之 Ed25519 簽章。
- 若無根背書簽章或簽章數學驗證失敗，`enroll_key()` 拋出 `ValueError` 阻斷登記。

### 3. 保守降級原則（Conservative Fallback）
- 當前台股生態系尚未接入外部 TWSE 官方離線 PKI 根金鑰，系統在預設無根錨點時，所有非合成資料回測報告一律保守輸出：
  $$\text{validation\_verdict} = \text{REAL\_HISTORICAL\_NOT\_VERIFIED}$$
  並在審計明細中加註 `OFFICIAL_TRUST_ROOT_NOT_CONFIGURED`。
- 絕不硬編碼假官方金鑰冒充官方授權。

---

## 五、Verdict 決策閘門之多因子獨立檢驗流程

回測報告產出與驗證遵循以下決策樹：

```
                    [BacktestRunResult 輸入]
                               |
                   result.is_synthetic == True
                   或任一來源標記為 SYNTHETIC?
                               |
                 +-------------+-------------+
                 | 是                        | 否
                 v                           v
   [SYNTHETIC_TEST_RESULT_ONLY]     [檢查呼叫端聲明與 NAV 完整性]
                                             |
                                 all_attested == True
                                 nav_complete == True
                                 run_completed == True?
                                             |
                               +-------------+-------------+
                               | 否                        | 是
                               v                           v
                 [REAL_HISTORICAL_NOT_VERIFIED]  [檢查獨立驗證器型別]
                                                           |
                                       isinstance(evidence_verifier,
                                       (DatasetChecksumVerifier,
                                        FileDigestEvidenceVerifier))?
                                                           |
                                             +-------------+-------------+
                                             | 否                        | 是
                                             v                           v
                               [UNAUTHORIZED_VERIFIER_TYPE]   [檢查公鑰庫官方根錨點]
                                             |                           |
                                             |               keystore.has_official_root?
                                             |                           |
                                             |             +-------------+-------------+
                                             |             | 否                        | 是
                                             v             v                           v
                               [REAL_HISTORICAL_NOT_VERIFIED]  [檢查簽署者 OFFICIAL 等級]
                                                                         |
                                                            signer_level == OFFICIAL?
                                                                         |
                                                           +-------------+-------------+
                                                           | 否                        | 是
                                                           v                           v
                                             [REAL_HISTORICAL_NOT_VERIFIED]  [驗證 Manifest Ed25519 簽章]
                                                                                       |
                                                                         sig_verified == True?
                                                                                       |
                                                                         +-------------+-------------+
                                                                         | 否                        | 是
                                                                         v                           v
                                                           [REAL_HISTORICAL_NOT_VERIFIED]  [REAL_HISTORICAL_PIT_VERIFIED]
```

---

## 六、ReplayStore 設計與防重放機制

### 1. SQLite Schema 設計
```sql
CREATE TABLE IF NOT EXISTS used_nonces (
    scope_key TEXT PRIMARY KEY,
    signer_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);
```

### 2. 跨維度 Scope 鍵值
為避免不同任務、不同環境間發生 Nonce 衝突，`scope_key` 格式定義為：
$$\text{scope\_key} = \text{signer\_id} : \text{project\_id} : \text{environment\_id} : \text{run\_id} : \text{report\_id} : \text{nonce}$$

### 3. 原子性與過期自動清理
- 採用 SQLite `WAL`（Write-Ahead Logging）模式與 `isolation_level="EXCLUSIVE"` 事務鎖，確保跨進程並發檢驗之原子性。
- 在登記前，自動執行 `DELETE FROM used_nonces WHERE expires_at < ?`，防止資料表無限膨脹。
- 每次資料庫連線在 `finally` 區塊中明確關閉，防範 Windows 平台之檔案鎖定問題。

---

## 七、RFC 8032 標準測試向量與密碼學實作邊界說明

### 1. RFC 8032 Section 7.1 Test Vector 1 驗證結果
- **Secret Key**: `9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60`
- **Expected Public Key**: `d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a`
- **Message**: `b""`（0 位元組空訊息）
- **Expected Signature**: `e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b`
- **實測結果**：公鑰、簽章與驗證結果與 RFC 8032 官方規範 100% 完全相符。

### 2. 密碼學實作邊界與安全性宣告
1. **純 Python 大數運算時序側信道（Timing Side-Channels）**：
   Python 內建之任意精度整數大數模運算（`pow(x, e, m)`、大數乘加除）不保證常數時間執行（Constant-Time Execution）。在具有物理旁路監聽或共用快取（Shared Cache）之不可信虛擬機環境中，私鑰簽署過程理論上可能洩漏時序資訊。
2. **定位與生產環境建議**：
   - 本模組純 Python Ed25519 實作定位於回測結果驗收、歷史資料指紋公證及防範內部呼叫端偽造結論。
   - 正式生產環境（如券商實盤下單私鑰簽署）建議將私鑰隔離於硬體安全模組（HSM）、安全保密區（Enclave）或呼叫經審核之 C 擴展庫（如 `libsodium` / `cryptography`）。

---

## 八、28 項安全 PoC 測試清單與執行結果

所有 28 項安全 PoC 測試均已實作於 `tests/test_backtest_metrics.py` 的 `TestC2522TrustRootAndVerdictSecurityPoCs` 類別中：

| PoC 編號 | 測試方法名稱 | 測試情境與攻擊意圖 | 預期安全防禦行為 | 實測結果 |
| :---: | :--- | :--- | :--- | :---: |
| **01** | `test_poc_c2522_01_caller_instantiate_keystore_self_enroll_official_without_root_fails` | 呼叫端自建 Store 試圖自封 OFFICIAL 權限 | 拋出 `ValueError("OFFICIAL_ROOT_NOT_CONFIGURED")` 拒絕 | **PASS** |
| **02** | `test_poc_c2522_02_caller_self_enroll_official_without_root_endorsement_fails` | 呼叫端登記 OFFICIAL 但缺少官方根私鑰背書 | 拋出 `ValueError("root_endorsement_signature")` 拒絕 | **PASS** |
| **03** | `test_poc_c2522_03_caller_self_enroll_official_with_invalid_root_endorsement_fails` | 呼叫端提供偽造／全零之根背書簽章 | 拋出 `ValueError("官方根背書簽章驗證失敗")` 拒絕 | **PASS** |
| **04** | `test_poc_c2522_04_official_enrollment_with_valid_root_endorsement_succeeds` | 提供合法官方根信任錨點與有效背書簽章 | 成功登記為 `KeyAuthorityLevel.OFFICIAL` | **PASS** |
| **05** | `test_poc_c2522_05_default_keypair_authority_level_is_test_only` | 生成金鑰未指定權限等級 | 預設權限嚴格限制為 `TEST_ONLY` | **PASS** |
| **06** | `test_poc_c2522_06_unconfigured_root_store_has_official_root_is_false` | 檢查未配置根錨點之 Store 狀態 | `has_official_root` 嚴格為 `False` | **PASS** |
| **07** | `test_poc_c2522_07_env_var_root_anchor_enrollment` | 透過環境變數配置官方信任根錨點 | 正確載入根錨點並完成官方金鑰登記驗收 | **PASS** |
| **08** | `test_poc_c2522_08_custom_verifier_class_duck_typing_rejected` | 自製自訂驗證器以 Duck Typing 偽造屬性 | 決策閘門攔截並標記 `UNAUTHORIZED_VERIFIER_TYPE` | **PASS** |
| **09** | `test_poc_c2522_09_mock_verifier_duck_typing_rejected` | Mock 驗證器回傳 `is_verified=True` | 決策閘門攔截並標記 `UNAUTHORIZED_VERIFIER_TYPE` | **PASS** |
| **10** | `test_poc_c2522_10_lineage_attestation_all_true_cannot_bypass_gate` | 呼叫端聲明全 True 但未配置受控獨立驗證器 | 決策閘門保守輸出 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS** |
| **11** | `test_poc_c2522_11_dataset_checksum_verifier_without_root_anchor_fails` | DatasetChecksumVerifier 缺乏官方根錨點 | 回傳 FAILED 並標記 `OFFICIAL_TRUST_ROOT_NOT_CONFIGURED` | **PASS** |
| **12** | `test_poc_c2522_12_file_digest_verifier_without_root_anchor_fails` | FileDigestEvidenceVerifier 缺乏官方根錨點 | 回傳 FAILED 並標記 `OFFICIAL_TRUST_ROOT_NOT_CONFIGURED` | **PASS** |
| **13** | `test_poc_c2522_13_dataset_checksum_verifier_with_test_only_key_fails` | DatasetChecksumVerifier 使用 TEST_ONLY 簽章 | 決策閘門攔截，不可獲得真實已驗證結論 | **PASS** |
| **14** | `test_poc_c2522_14_file_digest_verifier_with_test_only_key_fails` | FileDigestEvidenceVerifier 使用 TEST_ONLY 簽章 | 決策閘門攔截，不可獲得真實已驗證結論 | **PASS** |
| **15** | `test_poc_c2522_15_from_json_missing_expected_run_id_fails` | 反序列化已驗證報告缺少 `expected_run_id` | 拋出 `BacktestMetricsError("必須明確提供完整預期任務上下文")` | **PASS** |
| **16** | `test_poc_c2522_16_from_json_missing_expected_strategy_id_fails` | 反序列化已驗證報告缺少 `expected_strategy_id` | 拋出 `BacktestMetricsError("必須明確提供完整預期任務上下文")` | **PASS** |
| **17** | `test_poc_c2522_17_from_json_missing_expected_project_id_fails` | 反序列化已驗證報告缺少 `expected_project_id` | 拋出 `BacktestMetricsError("必須明確提供完整預期任務上下文")` | **PASS** |
| **18** | `test_poc_c2522_18_from_json_missing_expected_environment_id_fails` | 反序列化已驗證報告缺少 `expected_environment_id` | 拋出 `BacktestMetricsError("必須明確提供完整預期任務上下文")` | **PASS** |
| **19** | `test_poc_c2522_19_from_json_replay_store_atomic_deduplication` | 二次載入相同 Nonce 報告（重放攻擊） | ReplayStore 攔截拋出 `Replay Attack Detected` | **PASS** |
| **20** | `test_poc_c2522_20_from_json_replay_store_scoped_by_context` | 跨不同 run_id 任務使用相同 Nonce 字元 | 依 `scope_key` 維度獨立隔離允許 | **PASS** |
| **21** | `test_poc_c2522_21_validate_timestamps_rejects_naive_datetime` | 傳入缺少時區資訊之 naive datetime | 驗證攔截回傳 `缺少時區資訊` | **PASS** |
| **22** | `test_poc_c2522_22_validate_timestamps_rejects_expires_before_signed` | 傳入 `expires_at <= signed_at` 逆時序簽章 | 驗證攔截回傳 `expires_at 必須晚於 signed_at` | **PASS** |
| **23** | `test_poc_c2522_23_validate_timestamps_rejects_future_signed_at` | 傳入未來時間戳簽章（超出時鐘容許偏差） | 驗證攔截回傳 `signed_at 晚於目前時間` | **PASS** |
| **24** | `test_poc_c2522_24_validate_timestamps_rejects_expired_signature` | 傳入有效期限早於當前時間之過期簽章 | 驗證攔截回傳 `數位簽章已過期` | **PASS** |
| **25** | `test_poc_c2522_25_rfc8032_non_canonical_y_greater_than_p_rejected` | 傳入 $y \ge p$ 非規範 Ed25519 點編碼 | 拋出 `ValueError("RFC 8032 5.1.3")` 拒絕 | **PASS** |
| **26** | `test_poc_c2522_26_rfc8032_vector_1_exact_match` | RFC 8032 Section 7.1 Test Vector 1 標準向量 | 空訊息公鑰、簽章與驗證結果完全相符 | **PASS** |
| **27** | `test_poc_c2522_27_rfc8032_vector_1_tampered_message_fails` | 標準向量訊息遭篡改 1 位元組 | Ed25519 驗證結果嚴格回傳 `False` | **PASS** |
| **28** | `test_poc_c2522_28_rfc8032_vector_1_tampered_signature_fails` | 標準向量簽章遭篡改 1 位元組 | Ed25519 驗證結果嚴格回傳 `False` | **PASS** |

---

## 九、全系統測試結果總表

執行全系統單元測試與端到端測試套件（`python -m unittest discover -s tests -p "test_*.py"`）：

| 測試檔案 | 涵蓋模組與測試項目 | 測試案例數 | 執行結果 |
| :--- | :--- | :---: | :---: |
| `test_signal.py` | UnifiedTradeSignal 契約、時區、單位防禦 | 19 | **OK** |
| `test_paper_broker.py` | TaiwanPaperBroker 撮合、稅費、加權持倉、NAV 估值 | 26 | **OK** |
| `test_risk_engine.py` | RiskEngine 10 道風控防線、熔斷、資金鎖定 | 22 | **OK** |
| `test_integration.py` | Gateway、Broker、Risk 端到端整合 | 23 | **OK** |
| `test_prediction.py` | AI 預測合約、時序因果、Mock 防偽標記 | 14 | **OK** |
| `test_strategy_engine.py` | StrategyEngine 選股規則、持有期出場、快照隔離 | 52 | **OK** |
| `test_backtest_data_layer.py` | 回測例外階層、台北交易日曆、PIT 歷史資料供應器 | 14 | **OK** |
| `test_backtest_accounting.py` | FIFO 批次帳本、除權息應收股利、NAV 分解對帳 | 18 | **OK** |
| `test_backtest_execution.py` | 次日開盤撮合模擬器、Tick Size、漲跌停規則、投資組合估值器 | 21 | **OK** |
| `test_backtest_runner.py` | 策略／風控橋接器、WalkForwardRunner 12 步因果主迴圈 | 16 | **OK** |
| `test_backtest_metrics.py` | 績效指標、血統驗證、Ed25519、RFC8032 向量 (3 項) 與安全 PoC (28 項) | 94 | **OK** |
| **總計** | **全系統全量測試套件** | **319** | **ALL PASSED (319/319)** |

---

## 十、目前系統邊界與未解決問題

依據本專案防偽與科學回測原則，誠實揭露目前系統邊界與後續規劃：

1. **未接入真實台股歷史資料**：
   目前所有端到端測試均在合成資料（`is_synthetic=True`）上執行。在未完成真實台股資料全面清洗與公證前，系統絕不宣告任何實盤策略績效。
2. **尚未完成多窗口 Walk-Forward 彙總**：
   目前完成單窗口之主迴圈與單一報告驗收，多窗口交叉驗證與過度擬合檢定留待後續階段。
3. **未進行模型訓練與策略參數最佳化**：
   本階段專注於回測引擎與報告之密碼學信任邊界加固，未訓練真實 LightGBM / DoubleEnsemble 模型。
4. **未接入外部真實 TWSE PKI 根私鑰**：
   因目前尚未取得外部公證單位之 PKI 根金鑰，系統在生產預設模式下保守將所有回測結論輸出為 `REAL_HISTORICAL_NOT_VERIFIED`（加註 `OFFICIAL_TRUST_ROOT_NOT_CONFIGURED`），徹底杜絕偽造。

---

## 十一、結論

第三階段 C-2.5.2.2 安全修補工作已徹底消除 `C2_5_2_1_INDEPENDENT_SECURITY_REVIEW.md` 提出之 7 項安全 Finding：
- 信任根與官方授權等級已建立完整公鑰背書鏈，徹底阻斷自簽自驗。
- 決策閘門完成受控型別封裝，徹底杜絕 Duck Typing 偽造。
- 實作持久化 SQLite `ReplayStore` 與強制上下文比對，消除重放攻擊風險。
- 完整校驗簽章時間因果與有效期限，修補點解碼規範性，並補齊 RFC 8032 標準測試向量與 28 項安全 PoC。
- 兩個原始子專案維持 Clean 與原始 HEAD 狀態。
- 全系統 319 項測試全數通過。

本階段工作已達到停機檢查點，停止後續開發，等待人工獨立審查。
