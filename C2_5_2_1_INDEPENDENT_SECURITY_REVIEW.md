# 任務 C-2.5.2.1 獨立安全複核報告（唯讀審查）

> **審查日期**：2026-10-21  
> **審查性質**：唯讀獨立技術複核（Read-Only Security Audit，未修改任何原始碼或測試）  
> **前置依據**：`C2_5_2_1_REMEDIATION_REPORT.md`、`C2_5_2_INDEPENDENT_REVIEW.md`、`C2_5_2_REMEDIATION_REPORT.md`  
> **審查標的**：
> - `src/tw_quant/backtest/crypto.py`
> - `src/tw_quant/backtest/report.py`
> - `src/tw_quant/backtest/__init__.py`
> - `tests/test_backtest_metrics.py`
> - `C2_5_2_1_REMEDIATION_REPORT.md`  
> **最終判定**：**FAIL**（發現 2 項 CRITICAL 漏洞，呼叫端仍能自建公鑰庫自封 OFFICIAL 或注入自製 Verifier 取得 `REAL_HISTORICAL_PIT_VERIFIED`）

---

## 一、審查範圍與實際執行的命令

本次審查採嚴格唯讀方式，未變更任何程式碼、未安裝套件、未連線外部網路或券商。

### 實際執行的檢驗命令與環境
1. **子專案 Git 狀態與 HEAD 查核**：
   ```powershell
   git -C anti-gambling-trader-tw rev-parse HEAD
   git -C anti-gambling-trader-tw status --short
   git -C qlib-tw-trader rev-parse HEAD
   git -C qlib-tw-trader status --short
   ```
2. **全系統測試集回歸執行**：
   ```powershell
   $env:PYTHONPATH="src"; python -m unittest discover -s tests -p "test_*.py"
   ```
   - 執行結果：288 項測試全數通過（0 失敗、0 錯誤、0 跳過，耗時 8.975 秒）。
3. **密碼學演算法與決策閘門獨立 PoC 驗證**（見各章節說明）。

---

## 二、原始子專案 HEAD 和 Git 狀態確認

在審查開始與結束時，確認兩個原始子專案處於乾淨無修改狀態：

| 子專案名稱 | 預期 HEAD Commit Hash | 實際 HEAD Commit Hash | 工作目錄狀態 | 結論 |
| :--- | :--- | :--- | :--- | :--- |
| `anti-gambling-trader-tw` | `8609cef73718a056fddb74201fc304f174fa1553` | `8609cef73718a056fddb74201fc304f174fa1553` | Clean (無任何變更) | **符合要求** |
| `qlib-tw-trader` | `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` | `774b3fb60d3b20b5c9ee86d25052ad2ec08e2011` | Clean (無任何變更) | **符合要求** |

---

## 三、審查發現總覽（Findings Summary）

| 發現編號 | 標題 | 嚴重性 | 類別 | 狀態 |
| :--- | :--- | :---: | :---: | :---: |
| **FINDING-01** | 呼叫端可自建 `TrustedPublicKeyStore` 並自封 `KeyAuthorityLevel.OFFICIAL` 取得 `REAL_HISTORICAL_PIT_VERIFIED` | **CRITICAL** | 信任根邊界缺陷 | **已證實** |
| **FINDING-02** | 呼叫端自製 Verifier 可直接設定 `is_official_attested=True` 繞過決策閘門 | **CRITICAL** | 決策閘門繞過 | **已證實** |
| **FINDING-03** | 缺少持久化 Replay Store，且上下文比對為選擇性參數 (Opt-in)，無法完全防止跨任務重放 | **HIGH** | 重放防護缺陷 | **已證實** |
| **FINDING-04** | 簽章時間戳缺乏時序因果檢查（未校驗 `signed_at <= current_time` 與 `expires_at > signed_at`） | **MEDIUM** | 時序因果校驗缺漏 | **已證實** |
| **FINDING-05** | Ed25519 點解碼允許非規範編碼（RFC 8032 Non-canonical Point Encoding Malleability） | **MEDIUM** | 密碼學實作細節 | **已證實** |
| **FINDING-06** | 純 Python 大數實作非常數時間運算，具備潛在側信道風險 (Timing Side-Channel) | **LOW** | 密碼學實作細節 | **已證實** |
| **FINDING-07** | 永久測試套件遺漏 RFC 8032 官方測試向量 1 之單元測試 | **INFO** | 測試覆蓋缺漏 | **已證實** |

---

## 四、詳細技術審查

### 審查 A：Ed25519 密碼學實作分析

#### 1. 算法正確性與常數比對
- [`src/tw_quant/backtest/crypto.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py) 中定義之曲線常數：
  - 素數體模數：$p = 2^{255} - 19$ (`_P = 2**255 - 19`)，正確。
  - 基點階數：$L = 2^{252} + 27742317777372353535851937790883648493$ (`_Q`)，正確。
  - Edwards 曲線係數：$d = -121665 / 121666 \pmod p$，正確。
  - 基點 $B = (B_x, 4/5 \pmod p)$，正確。
- **純量鉗位 (Scalar Clamping)**：
  - `ed25519_publickey` 與 `ed25519_sign` 中使用：
    `a = 2**254 + sum(2**i * _bit(h, i) for i in range(3, 254))`
  - 清除了最低 3 個 bit、第 255 bit，並將第 254 bit 置 1，完全符合 RFC 8032 Section 5.1.5。
- **純 Ed25519 雜湊前綴**：
  - 隨機純量推導：$r = \text{SHA-512}(h[32..63] \parallel M) \pmod L$。
  - 簽章純量比對：$S = (r + \text{SHA-512}(R \parallel A \parallel M) \cdot a) \pmod L$。
  - 符合 RFC 8032 pureEd25519 標準。

#### 2. RFC 8032 Test Vector 1 校驗
經獨立命令以標準測試向量驗證：
- 私鑰種子：`9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60`
- 預期公鑰：`d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a`
- 訊息：空位元組 `b""`
- 預期簽章：`e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b`
- 執行比對結果：**100% 精確匹配**。但此向量測試未包含於 `tests/test_backtest_metrics.py` 中（**FINDING-07**）。

#### 3. 異常輸入與非規範解碼可塑性 (FINDING-05)
- 在 `ed25519_verify` 中：
  - 簽章長度不是 64 bytes 或公鑰長度不是 32 bytes 時直接回傳 `False`。
  - $S \ge L$ 時直接回傳 `False`，有效防範純量 $S$ 的可塑性。
- **然而在點解碼 `_decodepoint` 中**：
  ```python
  def _decodepoint(s: bytes) -> Tuple[int, int]:
      if len(s) != 32:
          raise ValueError("Ed25519 點編碼長度必須為 32 位元組")
      y = int.from_bytes(s, "little") & ((1 << 255) - 1)
      x = _xrecover(y)
  ```
  - **缺失規範性檢查**：未依 RFC 8032 Section 5.1.3 檢查 `if y >= _P: raise ValueError()`。
  - **可塑性 PoC 驗證**：對於曲線點 $y = 0$，將其加上 $p$ 得到非規範值 $y' = 0 + p < 2^{255}$，傳入 `_decodepoint` 時成功解碼且未報錯。此為非嚴格編碼可塑性問題。

#### 4. 側信道風險 (FINDING-06)
- 純 Python 實作中使用標準 `pow()` 與分支運算，不是常數時間（constant-time）演算法。雖在離線回測中不影響功能，但不可宣稱已達生產級密碼學認證標準。

---

### 審查 B：TrustedPublicKeyStore 信任根分析 (FINDING-01 & FINDING-02)

#### 1. 信任根架構機制缺陷分析
C-2.5.2.1 修補報告宣稱「徹底移除布林信任根，改採非對稱 Ed25519 與受控公鑰庫」。  
然而深入追查 [`src/tw_quant/backtest/crypto.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py) 與 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py)：
1. `TrustedPublicKeyStore` 僅是一個普通的記憶體 Python 類別，**內部沒有任何內建或不可變的根公鑰**。
2. `enroll_key` 函式沒有任何權限限制，任何呼叫端均可呼叫：
   ```python
   attacker_store = TrustedPublicKeyStore()
   attacker_store.enroll_key("ANY_KEY", attacker_pubkey, KeyAuthorityLevel.OFFICIAL)
   ```
3. `DatasetChecksumVerifier` 與 `FileDigestEvidenceVerifier` 的 `trusted_key_store` 是由**呼叫端作為參數傳入**的。
4. `BacktestReport.from_json` 的 `trusted_key_store` 也是由**呼叫端作為參數傳入**的。

#### 2. 可重現的最小 PoC 1：呼叫端自建 Store 自封 OFFICIAL (FINDING-01)
攻擊者使用以下 20 行腳本，即可自建私鑰、自建公鑰庫自封 OFFICIAL、簽署偽造 manifest，讓回測報告在 `build()` 與 `from_json()` 兩端均輸出 `REAL_HISTORICAL_PIT_VERIFIED`：

```python
# Minimal PoC 1: Self-proclaimed OFFICIAL Key Store
attacker_kp = generate_ed25519_keypair('ATTACKER_KEY', KeyAuthorityLevel.OFFICIAL)
attacker_store = TrustedPublicKeyStore()
attacker_store.enroll_key(attacker_kp.signer_id, attacker_kp.public_key_bytes, KeyAuthorityLevel.OFFICIAL)

m_hashes = {
    'CANDIDATE_MARKET_FEED': 'sha256:fake_market',
    'CANDIDATE_PRED_FEED': 'sha256:fake_pred',
    'BENCH_SRC': 'sha256:fake_bench',
}
h_body = json.dumps(m_hashes, sort_keys=True, separators=(',', ':')).encode('utf-8')
verifier = DatasetChecksumVerifier(
    expected_manifest_hashes=m_hashes,
    trusted_key_store=attacker_store,
    manifest_signer_id=attacker_kp.signer_id,
    manifest_signature_hex=attacker_kp.sign(h_body).hex(),
)

# 1. 成功在 build() 產出 REAL_HISTORICAL_PIT_VERIFIED
rep = BacktestReport.build(real_ok, metrics, attestation=full_att, benchmark=bench, evidence_verifier=verifier)
assert rep.validation_verdict == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED

# 2. 成功在 from_json() 維持 REAL_HISTORICAL_PIT_VERIFIED
rep.sign(attacker_kp)
restored = BacktestReport.from_json(rep.to_json(), trusted_key_store=attacker_store)
assert restored.validation_verdict == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED
```
**執行結果**：**100% 成功繞過**。  
**根本原因**：系統將「依賴注入（Dependency Injection）」誤作為「信任邊界（Trust Boundary）」。在沒有全域不可變錨點或受控硬體/環境憑證的情況下，傳入的 Store 可以被完全偽造。

#### 3. 可重現的最小 PoC 2：自製 Verifier 直接設定 `is_official_attested=True` (FINDING-02)
檢視 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py) 第 590-608 行：
```python
independent_verified = False
if verifier_result is not None and verifier_result.is_verified:
    is_official = getattr(verifier_result, "is_official_attested", False)
    is_test_only = verifier_result.evidence_type == "TEST_ONLY_EVIDENCE"
    if is_official and not is_test_only:
        independent_verified = True
```
系統完全未檢查 `evidence_verifier` 是否為系統認可的驗證器類別，也未在 `build()` 中驗證其簽章證據！  
攻擊者只需自建一個簡單類別：
```python
class MaliciousCustomVerifier:
    def verify_evidence(self, result, *, benchmark=None, attestation=None):
        return EvidenceVerificationResult(
            is_verified=True,
            verifier_name="AttackerBypass",
            verified_at=dt.datetime.now(dt.timezone.utc),
            evidence_type="OFFICIAL_EVIDENCE",
            is_official_attested=True, # 直接自宣告官方公證
        )

rep = BacktestReport.build(real_ok, metrics, attestation=full_att, benchmark=bench, evidence_verifier=MaliciousCustomVerifier())
assert rep.validation_verdict == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED
```
**執行結果**：**100% 成功取得 `REAL_HISTORICAL_PIT_VERIFIED`**。  
此結果直接推翻了修補報告中「不存在任何呼叫端自製 verifier 就能讓 verdict 成為 REAL_HISTORICAL_PIT_VERIFIED 的路徑」之宣稱。

---

### 審查 C：簽章範圍與上下文綁定分析

#### 1. 簽章範圍覆蓋度（14 項關鍵欄位）
逐一核對 [`src/tw_quant/backtest/crypto.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/crypto.py) 中 `SignatureContext` 之欄位：
1. `report_id`：已納入
2. `run_id`：已納入
3. `strategy_id`：已納入
4. `project_id`：已納入
5. `environment_id`：已納入
6. `source_git_commit`：已納入
7. `content_hash`：已納入（為整份報告不含 signature 之 canonical SHA-256）
8. `validation_verdict`：已納入
9. `claims_real_historical_verified`：已納入
10. `model_versions`：已納入
11. `data_versions`：已納入
12. `signed_at`：已納入
13. `expires_at`：已納入
14. `nonce`：已納入

#### 2. Canonical JSON 穩定度與型別
- 序列化使用 `json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")`。
- 上下文內無浮點數（`float`），所有數值均以字串或整數表示，無 NaN/Inf 歧義風險。
- `content_hash` 涵蓋了整份報告所有指標、帳務、NAV 序列與中繼資料，若報告中任何數值被篡改，`calc_hash` 將與簽章中的 `content_hash` 不符，防竄改保護確實有效。

---

### 審查 D：有效期與重放防護分析 (FINDING-03 & FINDING-04)

#### 1. 有效期限檢查邏輯
檢視 [`src/tw_quant/backtest/report.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/src/tw_quant/backtest/report.py) 第 1063-1074 行：
```python
expires_str = sig_info.get("expires_at", "")
try:
    exp_dt = dt.datetime.fromisoformat(expires_str)
    now = current_time or dt.datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=TAIPEI_TZ)
    if now > exp_dt:
        fail_reason = f"數位簽章已過期 (過期時間: {expires_str}, 目前時間: {now.isoformat()})"
```
- **時區處理**：當 naive datetime 出現時強制以 `TAIPEI_TZ` 補足，避免 offset-naive vs offset-aware 比較異常。
- **過期偵測**：當 `now > exp_dt` 時正確判定過期並強制降級。

#### 2. 時序因果檢查缺漏 (FINDING-04)
- 系統**完全未檢查 `signed_at` 是否位於未來**（例如 `signed_at = 2090-01-01`，而檢驗時間為 `2026-01-01`）。
- 系統**未檢查 `expires_at > signed_at`**。
- **PoC 驗證**：簽署未來時間（2090 年）的報告，在 2026 年執行 `from_json`，報告順利載入並維持 `REAL_HISTORICAL_PIT_VERIFIED`。

#### 3. 重放防護機制評估 (FINDING-03)
- **Nonce 僅為簽章 Payload 熵值**：
  系統未提供任何已使用 nonce 的持久化資料庫或快取。在有效期內，相同的已簽章報告可以在不同時間、不同進程中被重複載入無數次。
- **跨任務上下文比對為選擇性參數 (Opt-in)**：
  `expected_run_id`、`expected_strategy_id` 等參數均為可選（預設為 `None`）。若載入端呼叫 `from_json(signed_json, trusted_key_store=store)` 而未傳入 `expected_run_id`，則任務 A 的報告會直接被其他任務載入，系統不會主動拒絕。
- **結論**：目前系統僅具備「簽章上下文一致性校驗」與「呼叫端指定時的任務上下文比對」，**尚未具備完整的防重放體系（Replay Protection System）**。修補報告不應宣稱已徹底解決重放攻擊。

---

### 審查 E：Verdict 決策閘門 13 項 PoC 檢驗結果

針對系統中產生或保留 `REAL_HISTORICAL_PIT_VERIFIED` 的所有路徑，執行 13 項專門驗證：

| 情境編號 | 測試情境描述 | 預期安全行為 | 實際行為 | 判定 |
| :---: | :--- | :--- | :--- | :---: |
| **S1** | `LineageAttestation` 全數設為 `True`，未配置獨立驗證器 | 保守輸出 `REAL_HISTORICAL_NOT_VERIFIED` | 輸出 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS (Fail-Closed)** |
| **S2** | 自訂 Verifier 回傳 `is_verified=True` 與 `is_official_attested=True` | 保守輸出 `REAL_HISTORICAL_NOT_VERIFIED` | **輸出 `REAL_HISTORICAL_PIT_VERIFIED`** | **FAIL (Bypass - FINDING-02)** |
| **S3** | 呼叫端自建公鑰庫並自封 `KeyAuthorityLevel.OFFICIAL` 簽署 Manifest | 保守輸出 `REAL_HISTORICAL_NOT_VERIFIED` | **輸出 `REAL_HISTORICAL_PIT_VERIFIED`** | **FAIL (Bypass - FINDING-01)** |
| **S4** | 呼叫端自建公鑰庫並傳入 `from_json(..., trusted_key_store=attacker_store)` | 拒絕載入或強制降級 | **維持 `REAL_HISTORICAL_PIT_VERIFIED`** | **FAIL (Bypass - FINDING-01)** |
| **S5** | 載入舊版 HMAC 簽章報告 | 拒絕維持已驗證，強制降級為 NOT_VERIFIED | 強制降級為 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS (Fail-Closed)** |
| **S6** | 修改 JSON 欄位（例如 `final_nav`）並重算 SHA-256 `content_hash` | Ed25519 簽章不符，強制降級或報錯 | 強制降級為 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS (Fail-Closed)** |
| **S7** | 將合成資料報告之 verdict 竄改為 `REAL_HISTORICAL_PIT_VERIFIED` | 缺少受信任簽章，強制降級或報錯 | 強制降級為 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS (Fail-Closed)** |
| **S8** | 載入時未提供 `expected_run_id` 任務上下文 | 自動防禦跨任務挪用 | **未傳入時略過比對，允許跨任務載入** | **PASS WITH FINDINGS (FINDING-03)** |
| **S9a** | 數位簽章已過期（`now > expires_at`） | 判定簽章過期失效並強制降級 | 強制降級為 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS (Fail-Closed)** |
| **S9b** | 數位簽章簽署時間位於未來（`signed_at > now`） | 判定時序異常並強制降級 | **接受未來簽署時間並維持已驗證** | **FAIL (FINDING-04)** |
| **S10** | 缺少公鑰庫、未知 signer_id、或金鑰為 `TEST_ONLY` | 強制降級為 `REAL_HISTORICAL_NOT_VERIFIED` | 強制降級為 `REAL_HISTORICAL_NOT_VERIFIED` | **PASS (Fail-Closed)** |
| **S11** | 傳入 `is_trusted_authority=True` 或 `is_trusted_root=True` 但未附簽章 | 布林 flag 失效，標註 `TRUST_ROOT_NOT_CONFIGURED` | 判定未通過並標註 `TRUST_ROOT_NOT_CONFIGURED` | **PASS (Fail-Closed)** |
| **S12** | 包含合成資料/Mock 模型之回測結果，即使配置官方簽章 | 結論恆為 `SYNTHETIC_TEST_RESULT_ONLY` | 結論恆為 `SYNTHETIC_TEST_RESULT_ONLY` | **PASS (Fail-Closed)** |
| **S13** | 簽章 Hex 格式損毀、點解碼異常或非 JSON 字串 | 捕捉例外並保守降級或拒絕載入 | 捕捉例外並強制降級，未發生未處理崩潰 | **PASS (Fail-Closed)** |

---

### 審查 F：資料真實性與報告簽章的信任邊界分析

在量化交易架構中，必須嚴格釐清以下密碼學邊界：

1. **數位簽章與資料真偽之邊界**：
   - Ed25519 非對稱數位簽章只能證明「這份回測報告由掌握特定私鑰的人簽署，且內容未被第三方竄改」。
   - 它**完全不能證明**輸入的歷史 K 棒、除權息公告、特徵指標或財務報表在真實世界中確實發生過。
2. **本機雜湊計算與客觀證明之邊界**：
   - `FileDigestEvidenceVerifier` 逐 64KB 讀取磁碟實體檔案計算 SHA-256，只能證明「目前磁碟上的檔案與指定的 SHA-256 吻合」。
   - 若檔案本身是由回測者自行生成的合成資料或偽造數據，計算其 SHA-256 並以本機私鑰簽名，絕不等於「該資料已通過台灣證券交易所（TWSE）審核」。
3. **內部標籤與外部公證之邊界**：
   - 將記憶體中的 Enum 成員標記為 `KeyAuthorityLevel.OFFICIAL`，只是本機軟體架構層級的符號標記，**不具備任何法律或外部監管機關授權**。
   - 專案在未接入真實歷史行情、未接入正式 Walk-Forward 窗口、未取得第三方時間戳服務（RFC 3161 TSA）前，任何對外宣稱「真實歷史 PIT 回測已驗證」均屬虛假陳述。

---

## 五、修補報告聲稱與實際程式碼之差異

| 項目 | 修補報告宣稱內容 (`C2_5_2_1_REMEDIATION_REPORT.md`) | 實際程式碼真實狀況 | 差異與審查意見 |
| :--- | :--- | :--- | :--- |
| **自製 Verifier 阻斷** | 宣稱「不存在任何呼叫端自製 verifier 就能讓 verdict 成為 REAL_HISTORICAL_PIT_VERIFIED 的路徑」。 | 程式碼僅檢查 `is_official_attested` 布林屬性，自製類別只要回傳 `is_official_attested=True` 即可直接取得該結論。 | **不符（存在嚴重安全漏洞）** |
| **公鑰庫信任根** | 宣稱「驗證端只能使用受控公鑰庫，杜絕自簽自驗」。 | `TrustedPublicKeyStore` 缺少固化根憑證，任何人可在記憶體中自由實例化並登記 OFFICIAL 金鑰傳給驗證端。 | **不符（信任根未固化，可自簽自驗）** |
| **重放攻擊防禦** | 宣稱「數位簽章全面綁定任務上下文與 nonce，防禦跨任務重放」。 | 系統未實作 nonce 去重儲存庫；任務上下文比對為載入端選擇性傳入（Opt-in），預設略過比對。 | **部分不符（尚未完全防止重放）** |
| **RFC 8032 測試向量** | 宣稱「通過 RFC 8032 Section 7.1 Test Vector 1 之位元組級精確比對」。 | 算法演算法確實相符，但該向量未作為單元測試保存於自動化測試集中。 | **輕微不符（測試套件缺漏）** |
| **全系統測試數量** | 宣稱「288 項測試全數通過」。 | 經實際執行 `python -m unittest discover`，確實為 288 項測試全數通過。 | **完全相符** |
| **子專案狀態** | 宣稱兩個原始子專案保持 Clean 與原始 HEAD。 | 經實際檢查 Git HEAD 與工作目錄，確實完全無修改。 | **完全相符** |

---

## 六、目前尚未證實的安全性主張

依據「不把未經證實的主張當作事實」之原則，以下主張在目前的系統架構下**明確尚未證實**：
1. **尚未證實「非授權者無法產出 REAL_HISTORICAL_PIT_VERIFIED」**：已由 PoC 1 與 PoC 2 證實可以輕易繞過。
2. **尚未證實「回測報告已防止重放」**：無持久化 nonce 追蹤，且預設允許無上下文限制載入。
3. **尚未證實「密碼學實作具備生產級安全」**：純 Python 實作非常數時間，且存在點編碼非規範可塑性（Malleability）。
4. **尚未證實「策略具備真實歷史獲利能力」**：目前所有測試皆運行於合成資料或測試場景，真實資料管線尚未接入。

---

## 七、最終判定與後續修補建議

### 最終判定：**FAIL**

**判定理由**：
儘管 C-2.5.2.1 成功以純 Python 實作了 RFC 8032 Ed25519 演算法並將測試擴充至 288 項，但由於核心信任邊界（Trust Anchor）未受硬性約束，且決策閘門對自製 Verifier 缺乏類別檢驗與密碼學證明回溯，攻擊者仍能透過最小程式碼自建 OFFICIAL 金鑰庫或自製 Verifier，讓未授權的合成回測結果被標記為 `REAL_HISTORICAL_PIT_VERIFIED`。

### 建議之修補方向（供下一階段參考，本任務不進行修改）：
1. **固化受信任公鑰庫（Immutable / Pinned Trust Store）**：
   - 官方受信任公鑰庫不應由呼叫端隨意實例化注入，而應作為模組級受保護單例，或從唯讀配置檔/環境變數載入預設的官方公鑰指紋白名單。
2. **Verifier 決策閘門型別約束與簽章鏈回溯**：
   - `BacktestReport.build` 不應信任傳入之 `verifier_result.is_official_attested` 布林值，而必須強制檢驗 `evidence_verifier` 必須為 `(DatasetChecksumVerifier, FileDigestEvidenceVerifier)` 等特定受信任型別，且由回測報告核心親自校驗該 Verifier 所持有的官方簽章。
3. **時序因果補充校驗**：
   - 在 `from_json` 中補充校驗 `signed_at <= current_time` 與 `expires_at > signed_at`。
4. **補齊 RFC 8032 Test Vector 1 單元測試**：
   - 將標準向量測試加入 `test_backtest_metrics.py` 以防後續迴歸。
5. **修補 `_decodepoint` 之 RFC 8032 規範性檢查**：
   - 增加 `if y >= _P: raise ValueError("y >= p")` 以消除可塑性。

---

**審查結束**：本複核未修改專案既有程式碼，全系統 288 項測試保持可重現通過，停止進一步開發，靜候人工決策。
