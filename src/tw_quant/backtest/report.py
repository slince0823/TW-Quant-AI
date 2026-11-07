"""回測結構化報告與資料血統防偽驗收 (report.py) — 第三階段 C-2.5.2.1 修補版.

核心原則：
1. 報告同時檢查「行情、預測、交易日曆、公司行動、Benchmark」各自的來源與驗證狀態，
   不以單一 `is_synthetic` 布林值推定全部真實。
2. 預設永遠不宣稱「真實歷史回測已驗證」；唯有所有來源皆為非合成、預測通過嚴格 PIT 時序因果檢驗
   (`trained_cutoff_date < decision_date`、`available_at <= decision_cutoff_time`、
   `data_cutoff_time <= generated_at` 且全數具備 Asia/Taipei 時區)、
   呼叫端提供 `LineageAttestation`、官方公鑰授權之獨立證據驗證器 (`IndependentEvidenceVerifier`)
   實質審核通過、NAV 完整、回測完整完成且模型非 Mock 時，始輸出 `REAL_HISTORICAL_PIT_VERIFIED`。
3. 廢除呼叫端布林信任根授權：
   - 徹底廢除 `is_trusted_root` 與 `is_trusted_authority` 布林旗標之授權效力。
   - 呼叫端自製檔案、自製 Manifest、自行計算 SHA-256，絕不可取得已驗證結論。
   - 自訂 verifier 即使回傳 `is_verified=True`，缺少官方受信任公鑰庫 (`TrustedPublicKeyStore`)
     之簽章驗證時，一律視為未授權，保守輸出 `REAL_HISTORICAL_NOT_VERIFIED`。
4. 升級非對稱數位簽章 (Ed25519) 與受控公鑰庫：
   - 棄用對稱 HMAC 簽章，改採 RFC 8032 Ed25519 非對稱數位簽章。
   - 簽署端使用私鑰；驗證端只能使用受控公鑰庫中登記之官方公鑰，嚴禁直接接受報告自我攜帶之公鑰。
   - 測試金鑰 (`TEST_ONLY`) 僅供測試沙盒使用，絕不可授予真實歷史已驗證結論。
5. 完整報告上下文綁定與防重放 (Context Binding & Replay Protection)：
   - 數位簽章強制綁定：`report_id`、`run_id`、`strategy_id`、`project_id`、`environment_id`、
     `source_git_commit`、模型版本、資料版本、`content_hash`、`validation_verdict`、
     `signed_at`、`expires_at` 與隨機數 `nonce`。
   - 反序列化時嚴格校驗有效期限與任務上下文，防範跨任務、跨環境之重放攻擊。
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ..prediction.contract import PredictionMetadata
from .calendar import TAIPEI_TZ
from .crypto import (
    Ed25519KeyPair,
    KeyAuthorityLevel,
    OfficialTrustAuthority,
    OfficialTrustStatus,
    ReplayStore,
    ReportSignature,
    SignatureContext,
    TrustedPublicKeyStore,
    ed25519_verify,
    get_default_replay_store,
    validate_signature_timestamps,
)
from .exceptions import BacktestMetricsError
from .metrics import (
    METHODOLOGY_DEVIATIONS,
    METHODOLOGY_METRICS,
    SUPPLEMENTARY_METRICS,
    BenchmarkSeries,
    MetricStatus,
    PerformanceMetrics,
)
from .runner import BacktestRunResult, BacktestRunStatus, WalkForwardRunConfig

REPORT_SCHEMA_VERSION = "1.0.3"


class LineageStatus(str, Enum):
    """單一資料來源之血統與驗證狀態。"""

    SYNTHETIC = "SYNTHETIC"                    # 合成／Mock 測試資料
    REAL_UNVERIFIED = "REAL_UNVERIFIED"        # 非合成，但缺乏 PIT／覆蓋驗證證據或未通過因果檢驗
    REAL_PIT_ATTESTED = "REAL_PIT_ATTESTED"    # 非合成，通過 PIT 因果檢驗且呼叫端提供驗證聲明
    NOT_PROVIDED = "NOT_PROVIDED"              # 未提供該資料來源
    NO_EVENTS_RECORDED = "NO_EVENTS_RECORDED"  # 帳本無事件，且未驗證「確實無事件」(覆蓋度未知)


class ValidationVerdict(str, Enum):
    """整份報告之驗證結論。"""

    SYNTHETIC_TEST_RESULT_ONLY = "SYNTHETIC_TEST_RESULT_ONLY"
    REAL_HISTORICAL_NOT_VERIFIED = "REAL_HISTORICAL_NOT_VERIFIED"
    REAL_HISTORICAL_PIT_VERIFIED = "REAL_HISTORICAL_PIT_VERIFIED"


@dataclass(frozen=True)
class LineageAttestation:
    """呼叫端提供之驗證聲明 (預設全部為 False：未證實)。

    注意：此為呼叫端主觀聲明，非系統或第三方獨立公證。
    即使全部設為 True 且附上 evidence_notes，在缺少獨立證據驗證器審核時，
    系統結論仍保守標記為 REAL_HISTORICAL_NOT_VERIFIED。
    """

    market_data_pit_verified: bool = False
    calendar_verified: bool = False
    corporate_actions_coverage_verified: bool = False
    benchmark_verified: bool = False
    evidence_notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "market_data_pit_verified", "calendar_verified",
            "corporate_actions_coverage_verified", "benchmark_verified",
        ):
            if not isinstance(getattr(self, name), bool):
                raise BacktestMetricsError(f"LineageAttestation.{name} 必須為明確布林值")


@dataclass(frozen=True)
class EvidenceVerificationResult:
    """獨立證據驗證結果。"""

    is_verified: bool
    verifier_name: str
    verified_at: dt.datetime
    evidence_type: str = "INDEPENDENT_EVIDENCE"
    details: Mapping[str, Any] = field(default_factory=dict)
    failure_reasons: Tuple[str, ...] = ()
    is_official_attested: bool = False


@runtime_checkable
class IndependentEvidenceVerifier(Protocol):
    """獨立資料血統與真實性驗證器協定。"""

    def verify_evidence(
        self,
        result: BacktestRunResult,
        *,
        benchmark: Optional[BenchmarkSeries] = None,
        attestation: Optional[LineageAttestation] = None,
    ) -> EvidenceVerificationResult:
        """執行獨立驗證，回傳驗證結果。"""
        ...


@dataclass(frozen=True)
class DatasetChecksumVerifier:
    """基於資料集公證指紋清單之獨立證據驗證器 (C-2.5.2.1 修補)。

    安全原則：
    1. 廢除呼叫端布林信任旗標授權 (不再採信 is_trusted_authority 之授權效力)。
    2. 呼叫端記憶體傳入之指紋清單一律視為未受信任聲明 (UNTRUSTED_CALLER_MANIFEST)。
    3. 在未配置官方受信任公鑰庫 (`trusted_key_store`) 或缺少有效非對稱數位簽章時，
       一律回傳 is_verified=False，並標記 TRUST_ROOT_NOT_CONFIGURED。
    """

    expected_manifest_hashes: Mapping[str, str] = field(default_factory=dict)
    trusted_key_store: Optional[TrustedPublicKeyStore] = None
    manifest_signer_id: Optional[str] = None
    manifest_signature_hex: Optional[str] = None
    trusted_authority: Optional[str] = None
    is_trusted_authority: bool = False  # 已廢止布林授權，不產生任何授權效果
    verifier_name: str = "DatasetChecksumVerifier"

    def verify_evidence(
        self,
        result: BacktestRunResult,
        *,
        benchmark: Optional[BenchmarkSeries] = None,
        attestation: Optional[LineageAttestation] = None,
    ) -> EvidenceVerificationResult:
        now = dt.datetime.now(TAIPEI_TZ)

        if result.is_synthetic:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=("回測結果標記為合成 (is_synthetic=True)，不可標記為真實歷史驗證",),
            )

        active_store, is_test_ctx = OfficialTrustAuthority.get_active_trust_store()
        effective_keystore = self.trusted_key_store or active_store

        if effective_keystore is None or not self.manifest_signer_id or not self.manifest_signature_hex:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=(
                    "指紋清單未配置官方受信任公鑰庫或缺少非對稱數位簽章 (TRUST_ROOT_NOT_CONFIGURED: UNTRUSTED_CALLER_MANIFEST)",
                ),
            )

        if not getattr(effective_keystore, "has_official_root", False):
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=(
                    "受信任公鑰庫未配置官方信任根錨點 (OFFICIAL_TRUST_ROOT_NOT_CONFIGURED)",
                ),
                evidence_type="UNTRUSTED_STORE",
                is_official_attested=False,
            )

        # 驗證非對稱數位簽章
        h_body = json.dumps(dict(self.expected_manifest_hashes), sort_keys=True, separators=(",", ":"))
        try:
            sig_bytes = bytes.fromhex(self.manifest_signature_hex)
        except Exception:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=("Manifest 數位簽章 Hex 格式無效",),
            )

        ok, reason, level = effective_keystore.verify_signature(
            self.manifest_signer_id, sig_bytes, h_body.encode("utf-8")
        )
        if not ok:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=(f"Manifest 非對稱數位簽章驗證失敗 ({reason})",),
            )

        failures: List[str] = []
        details: Dict[str, Any] = {}
        mkt_src = result.market_data_provenance.source
        if not self.expected_manifest_hashes.get(mkt_src):
            failures.append(f"行情資料來源 {mkt_src} 缺少已登記之公證指紋雜湊")
        pred_src = result.prediction_provenance.source
        if not self.expected_manifest_hashes.get(pred_src):
            failures.append(f"預測資料來源 {pred_src} 缺少已登記之公證指紋雜湊")
        if benchmark is not None:
            bench_src = benchmark.provenance.source
            if not self.expected_manifest_hashes.get(bench_src):
                failures.append(f"基準資料來源 {bench_src} 缺少已登記之公證指紋雜湊")

        # 官方權限驗證：只有在測試隔離環境或有效庫具備系統官方認證時，才能授予 OFFICIAL_EVIDENCE
        is_official = False
        if level == KeyAuthorityLevel.OFFICIAL:
            if is_test_ctx or getattr(effective_keystore, "is_system_official", False):
                is_official = True
        evidence_type = "OFFICIAL_EVIDENCE" if is_official else "TEST_ONLY_EVIDENCE"

        return EvidenceVerificationResult(
            is_verified=len(failures) == 0,
            verifier_name=self.verifier_name,
            verified_at=now,
            evidence_type=evidence_type,
            details=details,
            failure_reasons=tuple(failures),
            is_official_attested=is_official and len(failures) == 0,
        )


@dataclass(frozen=True)
class FileDigestEvidenceVerifier:
    """實體檔案串流 SHA-256 摘要獨立證據驗證器 (C-2.5.2.1 修補).

    核心原則：
    1. 廢除布林信任根 (移除 is_trusted_root 之授權效力)，杜絕呼叫端自行設為 True 之漏洞。
    2. 實體開檔逐 64KB chunk 串流計算磁碟檔案真實 SHA-256。
    3. 必須配置受信任公鑰庫 (trusted_key_store) 並由官方公鑰簽章。
       若無官方公鑰庫或簽署者為 TEST_ONLY，不可授予 REAL_HISTORICAL_PIT_VERIFIED。
    """

    source_file_paths: Mapping[str, str] = field(default_factory=dict)
    expected_file_hashes: Mapping[str, str] = field(default_factory=dict)
    trusted_key_store: Optional[TrustedPublicKeyStore] = None
    manifest_signer_id: Optional[str] = None
    manifest_signature_hex: Optional[str] = None
    trusted_authority: Optional[str] = None
    is_trusted_root: bool = False  # 已廢止布林授權，不產生任何授權效果
    verifier_name: str = "FileDigestEvidenceVerifier"

    def verify_evidence(
        self,
        result: BacktestRunResult,
        *,
        benchmark: Optional[BenchmarkSeries] = None,
        attestation: Optional[LineageAttestation] = None,
    ) -> EvidenceVerificationResult:
        now = dt.datetime.now(TAIPEI_TZ)
        failures: List[str] = []
        details: Dict[str, Any] = {}

        if result.is_synthetic:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=("回測結果標記為合成 (is_synthetic=True)，不可標記為真實歷史驗證",),
            )

        active_store, is_test_ctx = OfficialTrustAuthority.get_active_trust_store()
        effective_keystore = self.trusted_key_store or active_store

        if effective_keystore is None or not self.manifest_signer_id or not self.manifest_signature_hex:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=(
                    "實體檔案校驗指紋缺少受信任官方公鑰庫或缺少非對稱數位簽章 (TRUST_ROOT_NOT_CONFIGURED: UNTRUSTED_MANIFEST_ROOT)",
                ),
            )

        if not getattr(effective_keystore, "has_official_root", False):
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=(
                    "受信任公鑰庫未配置官方信任根錨點 (OFFICIAL_TRUST_ROOT_NOT_CONFIGURED)",
                ),
                evidence_type="UNTRUSTED_STORE",
                is_official_attested=False,
            )

        h_body = json.dumps(dict(self.expected_file_hashes), sort_keys=True, separators=(",", ":"))
        try:
            sig_bytes = bytes.fromhex(self.manifest_signature_hex)
        except Exception:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=("Manifest 數位簽章 Hex 格式無效",),
            )

        ok, reason, level = effective_keystore.verify_signature(
            self.manifest_signer_id, sig_bytes, h_body.encode("utf-8")
        )
        if not ok:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=(f"Manifest 非對稱數位簽章驗證失敗 ({reason})",),
            )

        # 官方權限驗證：只有在測試隔離環境或有效庫具備系統官方認證時，才能授予 OFFICIAL_EVIDENCE
        is_official = False
        if level == KeyAuthorityLevel.OFFICIAL:
            if is_test_ctx or getattr(effective_keystore, "is_system_official", False):
                is_official = True
        evidence_type = "OFFICIAL_EVIDENCE" if is_official else "TEST_ONLY_EVIDENCE"

        sources_to_check = [
            ("market_data", result.market_data_provenance.source),
            ("prediction", result.prediction_provenance.source),
        ]
        if benchmark is not None:
            sources_to_check.append(("benchmark", benchmark.provenance.source))

        for category, src_name in sources_to_check:
            path = self.source_file_paths.get(src_name)
            expected_hash = self.expected_file_hashes.get(src_name)

            if not path:
                failures.append(f"來源 {src_name} ({category}) 缺少實體檔案路徑設定")
                continue
            if not expected_hash:
                failures.append(f"來源 {src_name} ({category}) 缺少預期之公證指紋雜湊")
                continue

            if not os.path.exists(path):
                failures.append(f"來源 {src_name} 實體檔案不存在: {path}")
                continue
            if not os.path.isfile(path):
                failures.append(f"來源 {src_name} 路徑非一般檔案: {path}")
                continue

            try:
                hasher = hashlib.sha256()
                file_size = 0
                with open(path, "rb") as f:
                    while chunk := f.read(65536):
                        hasher.update(chunk)
                        file_size += len(chunk)
                actual_hex = hasher.hexdigest().lower()
                actual_digest = f"sha256:{actual_hex}"

                exp_clean = expected_hash.strip().lower()
                if not exp_clean.startswith("sha256:"):
                    exp_clean = f"sha256:{exp_clean}"

                details[f"{category}_file_path"] = path
                details[f"{category}_file_size"] = file_size
                details[f"{category}_actual_hash"] = actual_digest

                if actual_digest != exp_clean:
                    failures.append(
                        f"來源 {src_name} 實體檔案 SHA-256 不符 (預期 {exp_clean}, 實際 {actual_digest})"
                    )
            except Exception as exc:
                failures.append(f"讀取來源 {src_name} 實體檔案異常: {exc}")

        return EvidenceVerificationResult(
            is_verified=len(failures) == 0,
            verifier_name=self.verifier_name,
            verified_at=now,
            evidence_type=evidence_type,
            details=details,
            failure_reasons=tuple(failures),
            is_official_attested=is_official and len(failures) == 0,
        )


def verify_prediction_consumption_pit(record: Any) -> Tuple[bool, str]:
    """檢驗單一預測消費紀錄之 PIT 時序因果關係 (FINDING-01 & R01 & R05 修補).

    防禦規則：
    1. 必要時間戳存在性：generated_at, available_at, decision_cutoff_time, decision_date,
       data_cutoff_time, trained_cutoff_date 不得為 None (R05 修補)。
    2. 時區有效性：所有 datetime 必須具備明確時區 (timezone-aware)，naive datetime 一律拒絕。
    3. 時間順序：
       - data_cutoff_time <= generated_at
       - generated_at <= available_at
       - available_at <= decision_cutoff_time (預測發布可用時間不得晚於決策時間)
    4. 訓練截止日與決策因果：
       - trained_cutoff_date (若為 datetime) 必須轉換為 Asia/Taipei 時區後取得台北市場日期 (R01 修補)
       - trained_cutoff_date 必須早於 decision_date (t_pure_date < d_pure_date)
       - 若 t_pure_date >= d_pure_date，依保守原則拒絕 (不可假裝可證明當日盤後訓練於決策前就緒且無未來標籤洩漏)
       - 若 trained_cutoff_date 為 datetime，其時間亦必須嚴格早於 decision_cutoff_time
    """
    bid = getattr(record, "batch_id", "UNKNOWN_BATCH")

    # 1. 必要時間欄位檢查 (Requirement E & R05)
    gen_at = getattr(record, "generated_at", None)
    avail_at = getattr(record, "available_at", None)
    dec_cut = getattr(record, "decision_cutoff_time", None)
    dec_date = getattr(record, "decision_date", None)
    data_cut = getattr(record, "data_cutoff_time", None)
    trained_cut = getattr(record, "trained_cutoff_date", None)

    if gen_at is None:
        return False, f"批次 {bid} 缺少 generated_at 時間戳"
    if avail_at is None:
        return False, f"批次 {bid} 缺少 available_at 時間戳"
    if dec_cut is None:
        return False, f"批次 {bid} 缺少 decision_cutoff_time 時間戳"
    if dec_date is None:
        return False, f"批次 {bid} 缺少 decision_date 決策日"
    if data_cut is None:
        return False, f"批次 {bid} 缺少 data_cutoff_time 時間戳"
    if trained_cut is None:
        return False, f"批次 {bid} 缺少 trained_cutoff_date 模型訓練截止日"

    # 2. 時區與型別檢查 (Requirement F & R05)
    for name, val in (
        ("generated_at", gen_at),
        ("available_at", avail_at),
        ("decision_cutoff_time", dec_cut),
        ("data_cutoff_time", data_cut),
    ):
        if not isinstance(val, dt.datetime):
            return False, f"批次 {bid} 之 {name} 型別錯誤 (預期 datetime，收到 {type(val).__name__})"
        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
            return False, f"批次 {bid} 之 {name} 缺少時區資訊 (naive datetime: {val})"

    if data_cut > gen_at:
        return False, f"批次 {bid} data_cutoff_time ({data_cut}) 晚於 generated_at ({gen_at})"

    # 3. 生成與可用時間因果 (Requirement D)
    if avail_at < gen_at:
        return False, f"批次 {bid} available_at ({avail_at}) 早於 generated_at ({gen_at})"

    # 4. 可用時間與決策時間因果
    if avail_at > dec_cut:
        return False, f"批次 {bid} available_at ({avail_at}) 晚於決策截止時間 ({dec_cut})，決策時預測尚未就緒"

    # 5. 決策日解析與一致性
    if isinstance(dec_date, dt.datetime):
        if dec_date.tzinfo is None or dec_date.tzinfo.utcoffset(dec_date) is None:
            return False, f"批次 {bid} decision_date 為 datetime 但缺少時區資訊 (naive datetime)"
        d_pure_date = dec_date.astimezone(TAIPEI_TZ).date()
    elif isinstance(dec_date, dt.date):
        d_pure_date = dec_date
    else:
        return False, f"批次 {bid} decision_date 型別無效 ({type(dec_date).__name__})"

    dec_cut_date = dec_cut.astimezone(TAIPEI_TZ).date()
    if d_pure_date != dec_cut_date:
        return False, f"批次 {bid} decision_date ({d_pure_date}) 與 decision_cutoff_time 台北日期 ({dec_cut_date}) 不一致"

    # 6. 訓練截止日與決策因果比對 (Requirements A, B, C & R01)
    if isinstance(trained_cut, dt.datetime):
        if trained_cut.tzinfo is None or trained_cut.tzinfo.utcoffset(trained_cut) is None:
            return False, f"批次 {bid} trained_cutoff_date 為 datetime 但缺少時區資訊 (naive datetime)"
        t_pure_date = trained_cut.astimezone(TAIPEI_TZ).date()
        if trained_cut >= dec_cut:
            return False, f"批次 {bid} trained_cutoff_time ({trained_cut}) 大於等於決策截止時間 ({dec_cut})，存在未來資料洩漏"
    elif isinstance(trained_cut, dt.date):
        t_pure_date = trained_cut
    else:
        return False, f"批次 {bid} trained_cutoff_date 型別無效 ({type(trained_cut).__name__})"

    if t_pure_date > d_pure_date:
        return False, f"批次 {bid} trained_cutoff_date ({t_pure_date}) 晚於決策日 ({d_pure_date})，存在未來資料洩漏"
    if t_pure_date == d_pure_date:
        return False, (
            f"批次 {bid} trained_cutoff_date ({t_pure_date}) 等於決策日 ({d_pure_date})，"
            "僅具日期精度無法證明訓練於決策前完成，依保守原則拒絕"
        )

    return True, ""


def _real_status(is_synthetic: bool, attested: bool) -> LineageStatus:
    if is_synthetic:
        return LineageStatus.SYNTHETIC
    return LineageStatus.REAL_PIT_ATTESTED if attested else LineageStatus.REAL_UNVERIFIED


class BacktestReport:
    """可序列化之結構化回測報告。"""

    def __init__(self, content: Mapping[str, Any]) -> None:
        self._content: Dict[str, Any] = copy.deepcopy(dict(content))
        body = json.dumps(self._content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self._hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    # ── 建構 ──────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        result: BacktestRunResult,
        metrics: PerformanceMetrics,
        *,
        benchmark: Optional[BenchmarkSeries] = None,
        attestation: Optional[LineageAttestation] = None,
        run_config: Optional[WalkForwardRunConfig] = None,
        model_metadata: Sequence[PredictionMetadata] = (),
        evidence_verifier: Optional[IndependentEvidenceVerifier] = None,
        report_id: Optional[str] = None,
        project_id: str = "TW_QUANT_AI",
        environment_id: str = "PRODUCTION",
        source_git_commit: str = "UNKNOWN_COMMIT",
    ) -> "BacktestReport":
        if not isinstance(result, BacktestRunResult):
            raise BacktestMetricsError("result 必須為 BacktestRunResult")
        if not isinstance(metrics, PerformanceMetrics):
            raise BacktestMetricsError("metrics 必須為 PerformanceMetrics")
        att = attestation or LineageAttestation()
        unverified: List[str] = []
        warnings: List[str] = list(metrics.warnings) + [f"[RUN] {w}" for w in result.warnings]

        # ── 各資料來源血統 ──
        mkt = result.market_data_provenance
        market_status = _real_status(mkt.is_synthetic, att.market_data_pit_verified)

        pred_prov = result.prediction_provenance
        records = result.consumed_predictions
        any_mock = any(r.is_mock for r in records)
        any_pred_synth = pred_prov.is_synthetic or any(r.is_synthetic for r in records)
        if not records:
            pred_status = LineageStatus.NOT_PROVIDED
            pred_pit_ok = False
        elif any_mock or any_pred_synth:
            pred_status = LineageStatus.SYNTHETIC
            pred_pit_ok = False
        else:
            pit_failures: List[str] = []
            for r in records:
                ok, reason = verify_prediction_consumption_pit(r)
                if not ok:
                    pit_failures.append(reason)
            pred_pit_ok = len(pit_failures) == 0
            if pred_pit_ok:
                pred_status = LineageStatus.REAL_PIT_ATTESTED
            else:
                pred_status = LineageStatus.REAL_UNVERIFIED
                for f in pit_failures:
                    warnings.append(f"[PREDICTION_PIT] {f}")
                    unverified.append(f"prediction_pit_check: {f}")

        cal = result.calendar_metadata_summary
        cal_synth = bool(cal.get("is_synthetic", True))  # 缺值時保守視為合成
        calendar_status = _real_status(cal_synth, att.calendar_verified)

        divs = tuple(result.paid_dividends) + tuple(result.pending_dividends_at_end)
        if any(d.is_synthetic or d.is_synthetic_payable_date for d in divs):
            ca_status = LineageStatus.SYNTHETIC
        elif att.corporate_actions_coverage_verified:
            ca_status = LineageStatus.REAL_PIT_ATTESTED
        elif divs:
            ca_status = LineageStatus.REAL_UNVERIFIED
        else:
            ca_status = LineageStatus.NO_EVENTS_RECORDED

        if benchmark is None:
            bench_status = LineageStatus.NOT_PROVIDED
        else:
            bench_status = _real_status(benchmark.is_synthetic, att.benchmark_verified)

        sources = {
            "market_data": market_status,
            "predictions": pred_status,
            "trading_calendar": calendar_status,
            "corporate_actions": ca_status,
            "benchmark": bench_status,
        }
        for key, st in sources.items():
            if st in (LineageStatus.NOT_PROVIDED, LineageStatus.NO_EVENTS_RECORDED, LineageStatus.REAL_UNVERIFIED):
                unverified.append(f"{key}: {st.value}")
            if st == LineageStatus.SYNTHETIC:
                warnings.append(f"{key} 為合成／Mock 資料")

        nav_complete = metrics.nav_complete
        run_completed = result.status == BacktestRunStatus.COMPLETED
        if not nav_complete:
            unverified.append("nav: INCOMPLETE")
        if not run_completed:
            unverified.append(f"run_status: {result.status.value}")
        if metrics.reconciliation.status != MetricStatus.OK:
            unverified.append(f"pnl_reconciliation: {metrics.reconciliation.status.value}")
        if metrics.benchmark_status not in ("OK",):
            unverified.append(f"benchmark_comparison: {metrics.benchmark_status}")
        # 呼叫端聲明備註
        if not att.evidence_notes:
            unverified.append("lineage_attestation_evidence: NOT_PROVIDED")

        # ── 獨立證據驗證器 (FINDING-02 & C-2.5.2.2 修補) ──
        verifier_result: Optional[EvidenceVerificationResult] = None
        if evidence_verifier is not None:
            if not isinstance(evidence_verifier, (DatasetChecksumVerifier, FileDigestEvidenceVerifier)):
                verifier_result = EvidenceVerificationResult(
                    is_verified=False,
                    verifier_name=getattr(evidence_verifier, "verifier_name", type(evidence_verifier).__name__),
                    verified_at=dt.datetime.now(TAIPEI_TZ),
                    failure_reasons=("未授權之驗證器型別 (UNAUTHORIZED_VERIFIER_TYPE: LACKS_OFFICIAL_AUTHORITY: 僅允許系統受控驗證器)",),
                    is_official_attested=False,
                    evidence_type="UNAUTHORIZED",
                )
            else:
                try:
                    verifier_result = evidence_verifier.verify_evidence(
                        result, benchmark=benchmark, attestation=att
                    )
                except Exception as exc:
                    verifier_result = EvidenceVerificationResult(
                        is_verified=False,
                        verifier_name=getattr(evidence_verifier, "verifier_name", type(evidence_verifier).__name__),
                        verified_at=dt.datetime.now(TAIPEI_TZ),
                        failure_reasons=(f"驗證器執行異常: {exc}",),
                    )

        # ── 驗證結論 (FINDING-01, FINDING-02 & C-2.5.2.2 修補：嚴格決策閘門) ──
        any_synth = (
            result.is_synthetic or any(st == LineageStatus.SYNTHETIC for st in sources.values())
        )
        all_attested = all(st == LineageStatus.REAL_PIT_ATTESTED for st in sources.values())
        independent_verified = False
        is_test_ctx = OfficialTrustAuthority.is_test_isolated()

        if evidence_verifier is None:
            unverified.append("independent_evidence_verifier: NOT_CONFIGURED (呼叫端聲明與註記不可替代獨立公證驗證)")
        elif not isinstance(evidence_verifier, (DatasetChecksumVerifier, FileDigestEvidenceVerifier)):
            unverified.append("independent_evidence_verifier: UNAUTHORIZED_VERIFIER_TYPE (LACKS_OFFICIAL_AUTHORITY: 自製或未授權之驗證器不可授予已驗證結論)")
        else:
            # 安全修復 (C-2.5.2.2.1)：多層信任邊界與縱深防禦檢查。
            # 1. 驗證器本身所附之 keystore 必須具備根錨點，且簽署者必須在該庫中有效登記。
            # 2. 若簽署者在驗證器庫中即為 TEST_ONLY，直接降級為 TEST_ONLY_EVIDENCE。
            # 3. 官方授權狀態以系統 OfficialTrustAuthority 為唯一權威信任源頭，系統必須配置官方根且有效登記 OFFICIAL。
            keystore = getattr(evidence_verifier, "trusted_key_store", None)
            signer_id = getattr(evidence_verifier, "manifest_signer_id", None)
            official_store, is_test_ctx = OfficialTrustAuthority.get_active_trust_store()

            if keystore is None or not getattr(keystore, "has_official_root", False):
                unverified.append(
                    "independent_evidence_verifier: OFFICIAL_TRUST_ROOT_NOT_CONFIGURED "
                    "(公鑰庫未配置官方信任根錨點，保守降級為 REAL_HISTORICAL_NOT_VERIFIED)"
                )
            elif not signer_id or not keystore.is_enrolled(signer_id):
                unverified.append(f"independent_evidence_verifier: UNENROLLED_SIGNER ({signer_id})")
            else:
                key_info = keystore.get_key(signer_id)
                level = key_info[1] if key_info else None
                if level != KeyAuthorityLevel.OFFICIAL:
                    unverified.append(f"independent_evidence_verifier: TEST_ONLY_EVIDENCE (簽署者為 {level})")
                elif not official_store.has_official_root:
                    unverified.append(
                        "independent_evidence_verifier: OFFICIAL_TRUST_ROOT_NOT_CONFIGURED "
                        "(系統尚未配置受信任官方信任根錨點，依安全原則正式驗證保守維持 REAL_HISTORICAL_NOT_VERIFIED)"
                    )
                elif not official_store.is_enrolled(signer_id):
                    unverified.append(f"independent_evidence_verifier: UNENROLLED_OFFICIAL_SIGNER ({signer_id})")
                else:
                    off_info = official_store.get_key(signer_id)
                    off_level = off_info[1] if off_info else None
                    if off_level != KeyAuthorityLevel.OFFICIAL:
                        unverified.append(f"independent_evidence_verifier: TEST_ONLY_EVIDENCE (簽署者於官方庫中為 {off_level})")
                    elif (
                        verifier_result is not None
                        and verifier_result.is_verified
                        and verifier_result.is_official_attested
                        and verifier_result.evidence_type == "OFFICIAL_EVIDENCE"
                    ):
                        independent_verified = True
                    else:
                        reasons = verifier_result.failure_reasons if verifier_result else ("未通過獨立驗證",)
                        unverified.append(f"independent_evidence_verifier: FAILED ({'; '.join(reasons)})")

        if any_synth:
            verdict = ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY
        elif all_attested and nav_complete and run_completed and independent_verified:
            verdict = ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED
        else:
            verdict = ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED

        # ── 模型資訊 ──
        models = sorted(
            {(r.model_name, r.model_version, r.feature_version, r.is_mock) for r in records}
        )
        model_types = sorted({m.model_type for m in model_metadata})
        model_section = {
            "models": [
                {"model_name": n, "model_version": v, "feature_version": f, "is_mock": mock}
                for n, v, f, mock in models
            ],
            "model_type": model_types if model_types else "NOT_AVAILABLE",
            "consumed_prediction_batches": len(records),
            "trained_cutoff_dates_present": all(r.trained_cutoff_date is not None for r in records) if records else None,
            "all_predictions_pit_passed": pred_pit_ok,
        }

        # ── 指標區塊 ──
        m = metrics.metrics
        implemented = {key for _, key, *_ in METHODOLOGY_METRICS}
        registry = [
            {
                "no": no, "key": key, "name": name, "formula": formula, "inputs": inputs,
                "implemented": key in m,
                "status": m[key].status.value if key in m else MetricStatus.NOT_AVAILABLE.value,
            }
            for no, key, name, formula, inputs in METHODOLOGY_METRICS
        ]
        grouped = {
            "return_and_risk": [
                "total_return", "cagr", "annualized_volatility", "sharpe_ratio", "sortino_ratio",
                "max_drawdown", "max_drawdown_duration", "calmar_ratio",
            ],
            "trade_statistics": [
                "closed_lot_trade_count", "fill_count", "win_rate", "average_win", "average_loss",
                "payoff_ratio", "profit_factor", "average_holding_days", "turnover", "daily_win_rate",
                "unfilled_order_ratio", "volume_shortfall_ratio", "risk_rejection_ratio",
            ],
            "cost_attribution": [
                "buy_commission", "sell_commission", "securities_transaction_tax", "slippage_cost_total",
                "total_traded_notional", "commission_tax_cost_drag", "cost_drag_annualized",
                "effective_fee_rate", "slippage_cost_drag", "realized_pnl_net",
                "realized_pnl_gross_beforefees", "dividend_income_paid", "dividend_receivable_end",
            ],
            "benchmark_relative": [
                "excess_return_total", "excess_return_annualized", "tracking_error", "information_ratio", "beta",
            ],
        }
        metric_blocks = {
            g: {k: m[k].to_dict() for k in keys if k in m} for g, keys in grouped.items()
        }

        if verdict == ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY:
            scope = (
                "本報告為合成／Mock 測試結果，僅用於驗證回測引擎邏輯、帳務與因果時序，"
                "不代表任何真實台股歷史績效，亦不可用於策略有效性、容量或實盤決策。"
            )
        elif verdict == ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED:
            scope = (
                "資料來源為非合成，但未通過獨立證據驗證器審核（或缺少 PIT／覆蓋度／Benchmark 獨立驗證證據、或缺少官方公鑰授權），"
                "呼叫端聲明不足以證明外部資料真實性，結果不得標記為「真實歷史回測已驗證」，僅供研究參考。"
            )
        else:
            scope = (
                "所有資料來源皆通過獨立證據驗證器檢驗且 NAV 完整；結論僅適用於所述樣本區間、日線保守撮合假設與已建模之公司行動，"
                "不保證未來績效。"
            )
        limitations = [
            "日線開盤保守撮合近似，非逐筆／分 K 撮合。",
            "僅支援現金股利；股票股利、拆併股、增減資會被明確拒絕。",
            "未做參數最佳化；單一樣本區間之績效不具統計顯著性判斷。",
            "SHA-256 僅確保本回測報告序列化內容未被竄改，不單獨證明外部資料來源之真實性。",
        ]

        trust_env = (
            "TEST_ISOLATED_ONLY" if is_test_ctx else
            "OFFICIAL_PRODUCTION" if OfficialTrustAuthority.is_official_production_configured() else
            "UNCONFIGURED"
        )

        verification_framework = {
            "caller_attestation": {
                "status": "ATTESTED" if all_attested else "INCOMPLETE",
                "is_attested": all_attested,
                "notes": list(att.evidence_notes),
                "disclaimer": "呼叫端主觀聲明，非第三方或系統獨立驗證",
            },
            "data_integrity": {
                "hash_algorithm": "SHA-256",
                "hash_scope": "REPORT_CONTENT_INTEGRITY_ONLY",
                "disclaimer": "SHA-256 雜湊僅保證回測報告產出後未遭竄改，不單獨證明外部資料來源之真實性",
            },
            "provenance_tracking": {
                "market_data_source": mkt.source,
                "predictions_source": pred_prov.source,
                "calendar_source": cal.get("source"),
                "is_synthetic": result.is_synthetic,
            },
            "independent_verification": {
                "is_verified": independent_verified,
                "verifier_name": None if verifier_result is None else verifier_result.verifier_name,
                "status": (
                    "OFFICIAL_VERIFIED" if independent_verified else
                    "TEST_ONLY_VERIFIED" if (verifier_result is not None and verifier_result.is_verified and verifier_result.evidence_type == "TEST_ONLY_EVIDENCE") else
                    "NOT_CONFIGURED" if evidence_verifier is None else "FAILED"
                ),
                "is_official_attested": getattr(verifier_result, "is_official_attested", False) if verifier_result else False,
                "is_test_isolated": bool(is_test_ctx),
                "trust_environment": trust_env,
                "evidence_type": verifier_result.evidence_type if verifier_result else "NONE",
                "failure_reasons": list(verifier_result.failure_reasons) if verifier_result else ["未配置獨立證據驗證器"],
            },
        }

        gen_rep_id = report_id or f"REP-{result.run_id}-{result.strategy_id}"

        content: Dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_id": gen_rep_id,
            "project_id": project_id,
            "environment_id": environment_id,
            "source_git_commit": source_git_commit,
            "run": {
                "run_id": result.run_id,
                "strategy_id": result.strategy_id,
                "status": result.status.value,
                "start_date": result.start_date.isoformat(),
                "end_date": result.end_date.isoformat(),
                "trading_days_executed": len(result.trading_days_executed),
                "initial_cash": str(result.initial_cash),
                "final_nav": str(result.final_nav),
                "peak_nav": str(result.peak_nav),
                "config": (
                    None if run_config is None else {
                        "universe_symbols": list(run_config.universe_symbols),
                        "missing_prediction_policy": run_config.missing_prediction_policy.value,
                        "error_handling_mode": run_config.error_handling_mode.value,
                        "skip_prediction_on_final_day": run_config.skip_prediction_on_final_day,
                    }
                ),
                "abort_error": None if result.abort_error is None else str(result.abort_error),
            },
            "models_and_versions": model_section,
            "data_lineage": {
                "is_synthetic_flag": result.is_synthetic,
                "market_data": {
                    "source": mkt.source, "dataset_version": mkt.dataset_version,
                    "is_synthetic": mkt.is_synthetic, "status": market_status.value,
                },
                "predictions": {
                    "source": pred_prov.source, "dataset_version": pred_prov.dataset_version,
                    "is_synthetic": pred_prov.is_synthetic, "any_mock": any_mock, "status": pred_status.value,
                    "all_pit_passed": pred_pit_ok,
                },
                "trading_calendar": {
                    "source": cal.get("source"), "version": cal.get("version"),
                    "is_synthetic": cal.get("is_synthetic"), "status": calendar_status.value,
                },
                "corporate_actions": {
                    "dividend_records": len(divs), "status": ca_status.value,
                    "any_synthetic": any(d.is_synthetic or d.is_synthetic_payable_date for d in divs),
                },
                "benchmark": (
                    {"name": None, "source": None, "status": bench_status.value}
                    if benchmark is None else {
                        "name": benchmark.name, "source": benchmark.provenance.source,
                        "dataset_version": benchmark.provenance.dataset_version,
                        "is_synthetic": benchmark.is_synthetic, "basis": benchmark.basis.value,
                        "includes_dividends": benchmark.includes_dividends, "status": bench_status.value,
                    }
                ),
                "attestation": {
                    "market_data_pit_verified": att.market_data_pit_verified,
                    "calendar_verified": att.calendar_verified,
                    "corporate_actions_coverage_verified": att.corporate_actions_coverage_verified,
                    "benchmark_verified": att.benchmark_verified,
                    "evidence_notes": list(att.evidence_notes),
                },
                "source_statuses": {k: v.value for k, v in sources.items()},
                "pit_verification_status": (
                    "SYNTHETIC" if any_synth else
                    "VERIFIED" if independent_verified and all_attested else
                    "CALLER_ATTESTED_ONLY" if all_attested else "NOT_VERIFIED"
                ),
                "nav_integrity_status": metrics.nav_status,
                "validation_verdict": verdict.value,
                "claims_real_historical_verified": verdict == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED,
                "is_test_isolated_verification": bool(is_test_ctx),
                "trust_environment": trust_env,
                "verification_framework": verification_framework,
            },
            "metrics": metric_blocks,
            "pnl_reconciliation": metrics.reconciliation.to_dict(),
            "breakdowns": metrics.to_dict()["breakdowns"],
            "benchmark_comparison_status": metrics.benchmark_status,
            "methodology": {
                "registry": registry,
                "all_17_implemented": all(k in m for k in implemented),
                "supplementary": [{"key": k, "description": d} for k, d in SUPPLEMENTARY_METRICS],
                "deviations": list(METHODOLOGY_DEVIATIONS),
                "trading_days_per_year": metrics.config.trading_days_per_year,
                "risk_free_rate_annual": metrics.config.risk_free_rate_annual,
            },
            "data_quality_warnings": warnings,
            "unverified_items": unverified,
            "conclusion_scope": scope,
            "limitations": limitations,
        }
        return cls(content)

    # ── 存取、簽章與序列化 ───────────────────────────────────────

    @property
    def content(self) -> Dict[str, Any]:
        return copy.deepcopy(self._content)

    @property
    def content_hash(self) -> str:
        return self._hash

    @property
    def report_id(self) -> str:
        return str(self._content.get("report_id", ""))

    @property
    def project_id(self) -> str:
        return str(self._content.get("project_id", ""))

    @property
    def environment_id(self) -> str:
        return str(self._content.get("environment_id", ""))

    @property
    def source_git_commit(self) -> str:
        return str(self._content.get("source_git_commit", ""))

    @property
    def run_id(self) -> str:
        return str(self._content.get("run", {}).get("run_id", ""))

    @property
    def strategy_id(self) -> str:
        return str(self._content.get("run", {}).get("strategy_id", ""))

    @property
    def validation_verdict(self) -> ValidationVerdict:
        return ValidationVerdict(self._content["data_lineage"]["validation_verdict"])

    @property
    def is_synthetic_result(self) -> bool:
        return self.validation_verdict == ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY

    @property
    def claims_real_historical_verified(self) -> bool:
        return bool(self._content["data_lineage"]["claims_real_historical_verified"])

    @property
    def is_test_isolated_verification(self) -> bool:
        """是否為測試隔離環境下產出之驗證結論 (P0)."""
        return bool(self._content.get("data_lineage", {}).get("is_test_isolated_verification", False))

    @property
    def is_official_production_verified(self) -> bool:
        """是否具備非測試隔離之正式生產官方 REAL_HISTORICAL_PIT_VERIFIED 身分 (P0)."""
        lineage = self._content.get("data_lineage", {})
        return (
            self.validation_verdict == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED
            and not bool(lineage.get("is_test_isolated_verification", False))
            and lineage.get("trust_environment") == "OFFICIAL_PRODUCTION"
            and OfficialTrustAuthority.is_official_production_configured()
        )

    @property
    def signature(self) -> Optional[Dict[str, Any]]:
        return self._content.get("signature")

    def sign(
        self,
        signer: Ed25519KeyPair,
        *,
        valid_days: int = 30,
        current_time: Optional[dt.datetime] = None,
        nonce: Optional[str] = None,
    ) -> ReportSignature:
        """使用非對稱私鑰為報告簽署 Ed25519 數位簽章並綁定完整上下文 (C-2.5.2.1 修補).

        簽章涵蓋範圍：
        - 整份報告內容雜湊 (payload_hash)
        - 驗證結論 (validation_verdict)
        - claims_real_historical_verified 旗標
        - report_id, run_id, strategy_id, project_id, environment_id, source_git_commit
        - 模型版本與資料版本
        - 簽章時間 (signed_at)、有效期限 (expires_at)、隨機數 (nonce)
        """
        if not isinstance(signer, Ed25519KeyPair):
            raise BacktestMetricsError(
                f"signer 必須為 Ed25519KeyPair (HMAC 對稱密鑰簽章已棄用)，收到 {type(signer).__name__}"
            )
        if valid_days <= 0:
            raise BacktestMetricsError(f"valid_days 必須大於 0 (收到 {valid_days})")

        now = current_time or dt.datetime.now(TAIPEI_TZ)
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            now = now.replace(tzinfo=TAIPEI_TZ)
        expires = now + dt.timedelta(days=valid_days)
        now_iso = now.isoformat()
        expires_iso = expires.isoformat()
        gen_nonce = nonce or secrets.token_hex(16)

        # 1. 取得不含 signature 的報告 canonical payload hash
        c = copy.deepcopy(self._content)
        c.pop("signature", None)
        body = json.dumps(c, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        # 2. 彙整版本資訊
        models = self._content.get("models_and_versions", {}).get("models", [])
        model_versions_tuple = tuple(
            (
                str(m.get("model_name", "")),
                str(m.get("model_version", "")),
                str(m.get("feature_version", "")),
                bool(m.get("is_mock", False)),
            )
            for m in models
        )
        dl = self._content.get("data_lineage", {})
        data_versions = {
            "market_data": dl.get("market_data", {}).get("dataset_version"),
            "predictions": dl.get("predictions", {}).get("dataset_version"),
            "trading_calendar": dl.get("trading_calendar", {}).get("version"),
            "benchmark": dl.get("benchmark", {}).get("dataset_version"),
        }

        # 3. 建立 SignatureContext 並計算簽章
        sig_context = SignatureContext(
            report_id=self.report_id,
            run_id=self.run_id,
            strategy_id=self.strategy_id,
            project_id=self.project_id,
            environment_id=self.environment_id,
            source_git_commit=self.source_git_commit,
            content_hash=payload_hash,
            validation_verdict=self.validation_verdict.value,
            claims_real_historical_verified=self.claims_real_historical_verified,
            model_versions=model_versions_tuple,
            data_versions=data_versions,
            signed_at=now_iso,
            expires_at=expires_iso,
            nonce=gen_nonce,
        )
        message_bytes = sig_context.canonical_bytes()
        sig_bytes = signer.sign(message_bytes)

        rep_sig = ReportSignature(
            algorithm="Ed25519-RFC8032",
            signer_id=signer.signer_id,
            public_key_hex=signer.public_key_hex,
            public_key_fingerprint=hashlib.sha256(signer.public_key_bytes).hexdigest()[:16],
            signature_hex=sig_bytes.hex(),
            signed_at=now_iso,
            expires_at=expires_iso,
            nonce=gen_nonce,
            context=sig_context.to_dict(),
        )

        self._content["signature"] = rep_sig.to_dict()
        full_body = json.dumps(self._content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self._hash = hashlib.sha256(full_body.encode("utf-8")).hexdigest()
        return rep_sig

    def to_dict(self) -> Dict[str, Any]:
        d = self.content
        d["content_hash"] = self._hash
        return d

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, indent=indent)

    def _downgrade_verdict(self, reason: str) -> None:
        """依保守安全原則將驗證結論降級為 REAL_HISTORICAL_NOT_VERIFIED 並記錄審計日誌。"""
        self._content["data_lineage"]["validation_verdict"] = ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED.value
        self._content["data_lineage"]["claims_real_historical_verified"] = False
        vf = self._content["data_lineage"].setdefault("verification_framework", {})
        vf_iv = vf.setdefault("independent_verification", {})
        vf_iv["is_verified"] = False
        vf_iv["status"] = "UNAUTHENTICATED_DESERIALIZATION_DOWNGRADE"
        vf_iv.setdefault("failure_reasons", []).append(reason)

        self._content.setdefault("data_quality_warnings", []).append(
            f"[DESERIALIZATION_TRUST] 反序列化報告未通過非對稱簽章或上下文驗證 ({reason})，"
            "依保守信任邊界強制降級為 REAL_HISTORICAL_NOT_VERIFIED"
        )
        self._content.setdefault("unverified_items", []).append(
            f"deserialization_unauthenticated: {reason}"
        )
        new_body = json.dumps(self._content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self._hash = hashlib.sha256(new_body.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(
        cls,
        text: str,
        *,
        trusted_key_store: Optional[TrustedPublicKeyStore] = None,
        replay_store: Optional[ReplayStore] = None,
        expected_run_id: Optional[str] = None,
        expected_strategy_id: Optional[str] = None,
        expected_project_id: Optional[str] = None,
        expected_environment_id: Optional[str] = None,
        current_time: Optional[dt.datetime] = None,
        strict_reject_unverified_load: bool = False,
        trusted_secret: Optional[str] = None,
    ) -> "BacktestReport":
        """自 JSON 還原並驗證 `content_hash`、非對稱數位簽章與任務上下文 (C-2.5.2.2 修補)。

        安全原則：
        1. 序列化完整性校驗：比對 `content_hash`，若不符則判定內容傳輸損毀或遭竄改。
        2. 信任邊界防禦：無密鑰 SHA-256 僅能檢測傳輸損毀，無法防範主動重算偽造。
        3. 若還原之報告宣稱 REAL_HISTORICAL_PIT_VERIFIED 或 claims_real_historical_verified=True：
           - 呼叫端必須明確提供預期任務上下文 (expected_run_id, expected_strategy_id,
             expected_project_id, expected_environment_id)，缺一不可 (FINDING-03)。
           - 受信任公鑰庫必須具備官方信任根錨點 (has_official_root)，且公鑰具備 OFFICIAL 等級 (FINDING-01)。
           - 數位簽章必須通過嚴格時間因果與有效期限檢驗 (validate_signature_timestamps) (FINDING-04)。
           - 數位簽章上下文必須與報告內容及預期任務上下文完全相符。
           - 隨機數 (nonce) 必須經由 ReplayStore 原子性檢查與登記，嚴防重放攻擊 (FINDING-03)。
           - 任何驗證失敗：
             * 若 strict_reject_unverified_load=True，直接拋出 BacktestMetricsError 拒絕載入。
             * 若 strict_reject_unverified_load=False (預設)，依保守信任邊界強制降級為
               REAL_HISTORICAL_NOT_VERIFIED，並加註審計警告，杜絕任何提升或維持偽造已驗證之可能。
        """
        data = json.loads(text)
        claimed = data.pop("content_hash", None)
        rep = cls(data)
        if claimed != rep.content_hash:
            raise BacktestMetricsError(
                "報告 content_hash 不符，內容可能遭傳輸損毀或未一致性修改",
                context={"claimed": claimed, "actual": rep.content_hash},
            )

        lineage = rep._content.get("data_lineage", {})
        verdict_str = lineage.get("validation_verdict")
        claims_real = bool(lineage.get("claims_real_historical_verified"))

        if verdict_str == ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED.value or claims_real:
            sig_info = rep._content.get("signature")
            fail_reason: Optional[str] = None
            now = current_time or dt.datetime.now(TAIPEI_TZ)
            if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
                now = now.replace(tzinfo=TAIPEI_TZ)

            # 1. 檢查簽章是否存在與格式
            if not isinstance(sig_info, dict):
                fail_reason = "缺少受信任之數位簽章"
            elif sig_info.get("algorithm") != "Ed25519-RFC8032":
                fail_reason = f"簽章演算法不支援或已棄用 ({sig_info.get('algorithm')})"

            # 2. 檢查任務上下文參數是否完整提供 (FINDING-03)
            if fail_reason is None:
                if (
                    expected_run_id is None
                    or expected_strategy_id is None
                    or expected_project_id is None
                    or expected_environment_id is None
                ):
                    fail_reason = (
                        "反序列化宣稱 REAL_HISTORICAL_PIT_VERIFIED 之報告時，"
                        "必須明確提供完整預期任務上下文 (expected_run_id, expected_strategy_id, "
                        "expected_project_id, expected_environment_id)"
                    )

            # 3. 檢查受信任公鑰庫與信任根錨點 (FINDING-01 & C-2.5.2.2.1 加固)
            if fail_reason is None:
                active_store, is_test_store = OfficialTrustAuthority.get_active_trust_store()

                # 若非處於受控測試隔離環境，且呼叫端傳入外部自製 trusted_key_store，一律拒絕
                if trusted_key_store is not None and not is_test_store:
                    fail_reason = (
                        f"UNAUTHORIZED_TRUST_STORE_INJECTION: 正式驗證端不得接受呼叫端注入之自製 Trust Store "
                        f"({type(trusted_key_store).__name__})，禁止自簽自授權"
                    )
                elif not is_test_store and (
                    bool(lineage.get("is_test_isolated_verification", False))
                    or lineage.get("trust_environment") == "TEST_ISOLATED_ONLY"
                ):
                    fail_reason = (
                        "TEST_ISOLATED_REPORT_REJECTED_IN_PRODUCTION: "
                        "測試隔離環境產出之報告不可於正式驗證端取得官方 REAL_HISTORICAL_PIT_VERIFIED 身分"
                    )
                elif not active_store.has_official_root:
                    fail_reason = "受信任公鑰庫未配置官方信任根錨點 (OFFICIAL_TRUST_ROOT_NOT_CONFIGURED: 系統尚未建立正式信任根，反序列化保守維持未驗證結論)"
                else:
                    assert isinstance(sig_info, dict)
                    signer_id = sig_info.get("signer_id", "")
                    effective_store = trusted_key_store if (is_test_store and trusted_key_store is not None) else active_store
                    if not effective_store.is_enrolled(signer_id):
                        fail_reason = f"簽署者 {signer_id} 未在受信任公鑰庫中登記"
                    else:
                        info = effective_store.get_key(signer_id)
                        assert info is not None
                        pk_bytes, level = info
                        if level != KeyAuthorityLevel.OFFICIAL:
                            fail_reason = f"簽署者 {signer_id} 為測試金鑰 ({level.value})，不可授予真實歷史已驗證結論"
                        elif pk_bytes.hex() != sig_info.get("public_key_hex"):
                            fail_reason = "簽章攜帶之公鑰與受信任公鑰庫中登記之公鑰不符"
                        else:
                            # 4. 嚴格時鐘因果與簽章有效期限檢查 (FINDING-04)
                            signed_at_str = sig_info.get("signed_at", "")
                            expires_str = sig_info.get("expires_at", "")
                            valid_ts, ts_reason = validate_signature_timestamps(
                                signed_at_str, expires_str, current_time=now
                            )
                            if not valid_ts:
                                fail_reason = ts_reason

                            # 5. 檢查任務上下文綁定
                            if fail_reason is None:
                                ctx_dict = sig_info.get("context", {})
                                if ctx_dict.get("run_id") != expected_run_id:
                                    fail_reason = f"任務上下文 run_id 不符 (預期 {expected_run_id}, 簽章為 {ctx_dict.get('run_id')})"
                                elif ctx_dict.get("strategy_id") != expected_strategy_id:
                                    fail_reason = f"任務上下文 strategy_id 不符 (預期 {expected_strategy_id}, 簽章為 {ctx_dict.get('strategy_id')})"
                                elif ctx_dict.get("project_id") != expected_project_id:
                                    fail_reason = f"任務上下文 project_id 不符 (預期 {expected_project_id}, 簽章為 {ctx_dict.get('project_id')})"
                                elif ctx_dict.get("environment_id") != expected_environment_id:
                                    fail_reason = f"任務上下文 environment_id 不符 (預期 {expected_environment_id}, 簽章為 {ctx_dict.get('environment_id')})"
                                elif ctx_dict.get("report_id") != rep.report_id:
                                    fail_reason = "報告 report_id 與簽章上下文不符"
                                elif ctx_dict.get("run_id") != rep.run_id:
                                    fail_reason = "報告 run_id 與簽章上下文不符"
                                elif ctx_dict.get("strategy_id") != rep.strategy_id:
                                    fail_reason = "報告 strategy_id 與簽章上下文不符"
                                elif ctx_dict.get("project_id") != rep.project_id:
                                    fail_reason = "報告 project_id 與簽章上下文不符"
                                elif ctx_dict.get("environment_id") != rep.environment_id:
                                    fail_reason = "報告 environment_id 與簽章上下文不符"

                            # 6. 檢查報告 payload_hash 與簽章上下文一致性
                            if fail_reason is None:
                                c = copy.deepcopy(rep._content)
                                c.pop("signature", None)
                                calc_body = json.dumps(c, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                                calc_hash = hashlib.sha256(calc_body.encode("utf-8")).hexdigest()
                                if ctx_dict.get("content_hash") != calc_hash:
                                    fail_reason = "簽章上下文之 content_hash 與報告本體不符"
                                else:
                                    # 7. 驗證 Ed25519 數位簽章數學有效性
                                    try:
                                        sig_bytes = bytes.fromhex(sig_info.get("signature_hex", ""))
                                        ctx_canon_bytes = json.dumps(
                                            ctx_dict, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                                        ).encode("utf-8")
                                        if not ed25519_verify(sig_bytes, ctx_canon_bytes, pk_bytes):
                                            fail_reason = "Ed25519 數位簽章數學驗證失敗"
                                    except Exception as e:
                                        fail_reason = f"驗證 Ed25519 簽章異常: {e}"

                            # 8. 隨機數 (Nonce) 重放攻擊防禦檢驗 (FINDING-02 & C-2.5.2.2.1 加固)
                            if fail_reason is None:
                                nonce_val = sig_info.get("nonce", "")
                                authoritative_store, is_test_rstore = OfficialTrustAuthority.get_active_replay_store()

                                # 檢查呼叫端是否嘗試注入外部自訂 ReplayStore (PoC-2 對抗防禦)
                                if replay_store is not None and not is_test_rstore:
                                    fail_reason = (
                                        f"UNAUTHORIZED_REPLAY_STORE_INJECTION: 正式驗證端不得接受呼叫端注入之自製 ReplayStore "
                                        f"({type(replay_store).__name__})，禁止略過防重放機制"
                                    )
                                else:
                                    effective_rstore = replay_store if (is_test_rstore and replay_store is not None) else authoritative_store

                                    # 嚴格檢查：effective_rstore 必須是真正的 ReplayStore 實例
                                    if not isinstance(effective_rstore, ReplayStore):
                                        fail_reason = (
                                            f"INVALID_REPLAY_STORE_TYPE: effective_rstore 必須為 ReplayStore 實例，"
                                            f"收到 {type(effective_rstore).__name__}"
                                        )
                                    else:
                                        try:
                                            effective_rstore.consume_nonce(
                                                signer_id=signer_id,
                                                project_id=rep.project_id,
                                                environment_id=rep.environment_id,
                                                run_id=rep.run_id,
                                                report_id=rep.report_id,
                                                nonce=nonce_val,
                                                signed_at=signed_at_str,
                                                expires_at=expires_str,
                                                current_time=now,
                                            )
                                        except BacktestMetricsError as exc:
                                            fail_reason = str(exc)
                                        except sqlite3.Error as sqle:
                                            fail_reason = f"ReplayStore 存取異常 (Fail-Closed: {sqle})"
                                        except Exception as exc:
                                            fail_reason = f"ReplayStore 非預期異常 (Fail-Closed: {exc})"

            if fail_reason is not None:
                if strict_reject_unverified_load:
                    raise BacktestMetricsError(
                        f"反序列化報告宣稱 REAL_HISTORICAL_PIT_VERIFIED 但未通過數位簽章驗證 ({fail_reason})，依安全原則拒絕載入"
                    )
                rep._downgrade_verdict(fail_reason)

        return rep

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BacktestReport) and self._hash == other._hash

    def __hash__(self) -> int:
        return hash(self._hash)
