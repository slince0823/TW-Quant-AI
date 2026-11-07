"""台股歷史回測非對稱數位簽章與上下文防偽模組 (crypto.py) — 第三階段 C-2.5.2.1.

核心原則：
1. 非對稱密鑰隔離 (Asymmetric Key Separation)：
   - 簽署端使用私鑰 (Private Key) 簽署；驗證端僅持有受信任公鑰 (Public Key) 進行驗證。
   - 杜絕對稱金鑰 HMAC 之自簽自驗漏洞。
2. 受控公鑰庫 (Controlled Trusted Public Key Store)：
   - 驗證端之公鑰必須來自受控之信任配置 (`TrustedPublicKeyStore`)，嚴禁直接接受報告自行攜帶之公鑰作為信任根。
   - 明確區分「正式官方公鑰 (`OFFICIAL`)」與「測試臨時公鑰 (`TEST_ONLY`)」。測試金鑰絕不可授予真實歷史已驗證結論。
3. 報告完整上下文綁定 (Context Binding)：
   - 簽章內容強制涵蓋：`report_id`、`run_id`、`strategy_id`、`project_id`、`environment_id`、
     `source_git_commit`、模型版本、資料版本、`content_hash`、`validation_verdict`、
     `signed_at`、`expires_at` 以及隨機數 `nonce`。
   - 杜絕跨任務、跨專案、跨環境之重放攻擊 (Replay Attack)。
4. 確定性規範序列化 (Canonical Serialization)：
   - 採用完全確定性之 JSON 編碼（排序鍵、無多餘空白、UTF-8），保證跨平台雜湊與簽章一致性。
5. 零外部依賴純 Python 實作 (Zero External Dependencies)：
   - 以 Python 標準函式庫純數學實作 RFC 8032 Ed25519 簽章與驗證，不引入非標準 C 擴展或第三方套件。
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import inspect
import json
import logging
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .exceptions import BacktestMetricsError

logger = logging.getLogger(__name__)

# ── RFC 8032 Ed25519 純 Python 數學常數與輔助運算 ────────────────────────────

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(z: int) -> int:
    return pow(z, _P - 2, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_BX = _xrecover(_BY)
_B = (_BX, _BY)


def _edwards(P: Tuple[int, int], Q: Tuple[int, int]) -> Tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2) % _P
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2) % _P
    return (x3, y3)


def _scalarmult(P: Tuple[int, int], e: int) -> Tuple[int, int]:
    if e == 0:
        return (0, 1)
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _encodeint(y: int) -> bytes:
    return int(y).to_bytes(32, "little")


def _decodeint(b: bytes) -> int:
    return int.from_bytes(b, "little")


def _encodepoint(P: Tuple[int, int]) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _isoncurve(P: Tuple[int, int]) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _P == 0


def _decodepoint(s: bytes) -> Tuple[int, int]:
    if not isinstance(s, bytes) or len(s) != 32:
        raise ValueError("Ed25519 點編碼長度必須為 32 位元組")
    sign = (s[31] >> 7) & 1
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError(f"Ed25519 非規範點編碼：y 座標 ({y}) 大於等於曲線模數 p ({_P})，依 RFC 8032 5.1.3 嚴格拒絕")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _P - x
    P = (x, y)
    if not _isoncurve(P):
        raise ValueError("解碼點不在 Edwards 曲線上")
    return P


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _hint(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def ed25519_publickey(sk_seed: bytes) -> bytes:
    """自 32 位元組密鑰種子推導 32 位元組 Ed25519 公鑰。"""
    if not isinstance(sk_seed, bytes) or len(sk_seed) != 32:
        raise ValueError("Ed25519 私鑰種子必須為 32 位元組 (bytes)")
    h = hashlib.sha512(sk_seed).digest()
    a = 2**254 + sum(2**i * _bit(h, i) for i in range(3, 254))
    return _encodepoint(_scalarmult(_B, a))


def ed25519_sign(m: bytes, sk_seed: bytes, pk_bytes: Optional[bytes] = None) -> bytes:
    """以 Ed25519 私鑰對訊息進行數位簽章，產出 64 位元組簽章。"""
    if not isinstance(sk_seed, bytes) or len(sk_seed) != 32:
        raise ValueError("Ed25519 私鑰種子必須為 32 位元組 (bytes)")
    if not isinstance(m, bytes):
        raise ValueError("簽署訊息 m 必須為 bytes")
    if pk_bytes is None:
        pk_bytes = ed25519_publickey(sk_seed)
    h = hashlib.sha512(sk_seed).digest()
    a = 2**254 + sum(2**i * _bit(h, i) for i in range(3, 254))
    r = _hint(h[32:] + m)
    R = _scalarmult(_B, r)
    S = (r + _hint(_encodepoint(R) + pk_bytes + m) * a) % _Q
    return _encodepoint(R) + _encodeint(S)


def ed25519_verify(sig_bytes: bytes, m: bytes, pk_bytes: bytes) -> bool:
    """驗證 64 位元組 Ed25519 簽章。若簽章格式錯誤或不匹配回傳 False。"""
    if not isinstance(sig_bytes, bytes) or len(sig_bytes) != 64:
        return False
    if not isinstance(pk_bytes, bytes) or len(pk_bytes) != 32:
        return False
    if not isinstance(m, bytes):
        return False
    try:
        R = _decodepoint(sig_bytes[:32])
        A = _decodepoint(pk_bytes)
    except Exception:
        return False
    S = _decodeint(sig_bytes[32:])
    if S >= _Q or S < 0:
        return False
    h = _hint(_encodepoint(R) + pk_bytes + m)
    v1 = _scalarmult(_B, S)
    v2 = _edwards(R, _scalarmult(A, h))
    return v1 == v2


# ── 金鑰授權級別與密鑰管理資料結構 ──────────────────────────────────────────


class KeyAuthorityLevel(str, Enum):
    """金鑰授權級別。"""

    OFFICIAL = "OFFICIAL"          # 官方受信任簽署者（必須持有受信任官方根錨點之背書簽章）
    TEST_ONLY = "TEST_ONLY"        # 測試沙盒專用金鑰（絕不可授予真實歷史已驗證結論）
    UNAUTHORIZED = "UNAUTHORIZED"  # 未授權／未登記金鑰


@dataclass(frozen=True)
class Ed25519KeyPair:
    """Ed25519 金鑰對。"""

    signer_id: str
    private_key_bytes: bytes
    public_key_bytes: bytes
    authority_level: KeyAuthorityLevel = KeyAuthorityLevel.TEST_ONLY
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()

    @property
    def private_key_hex(self) -> str:
        return self.private_key_bytes.hex()

    def sign(self, message: bytes) -> bytes:
        return ed25519_sign(message, self.private_key_bytes, self.public_key_bytes)


def generate_ed25519_keypair(
    signer_id: str,
    authority_level: KeyAuthorityLevel = KeyAuthorityLevel.TEST_ONLY,
    seed: Optional[bytes] = None,
) -> Ed25519KeyPair:
    """生成或自種子衍生 Ed25519 金鑰對（預設為 TEST_ONLY 授權等級）。"""
    if not signer_id or not isinstance(signer_id, str):
        raise ValueError("signer_id 必須為非空字串")
    if seed is None:
        seed = secrets.token_bytes(32)
    elif len(seed) != 32:
        raise ValueError("seed 必須恰為 32 位元組")
    pk = ed25519_publickey(seed)
    return Ed25519KeyPair(
        signer_id=signer_id,
        private_key_bytes=seed,
        public_key_bytes=pk,
        authority_level=authority_level,
    )


class InvalidRootAnchorError(TypeError, ValueError):
    """root_trust_anchor 型別、長度或曲線點無效之例外（同時相容 TypeError 與 ValueError）。"""


def _get_secure_replay_dir() -> str:
    """取得具備權限隔離與防符號連結檢查之 ReplayStore 預設儲存目錄 (C-2.5.2.2.2 P2 加固)."""
    base_dir = os.path.join(os.path.expanduser("~"), ".tw_quant_security", "replay_store")
    parent_dir = os.path.dirname(base_dir)
    for d in (parent_dir, base_dir):
        if os.path.islink(d):
            raise BacktestMetricsError(
                f"ReplayStore 安全目錄檢查失敗：路徑不可為符號連結 ({d})",
                context={"path": d},
            )
    os.makedirs(base_dir, mode=0o700, exist_ok=True)
    if os.path.islink(base_dir):
        raise BacktestMetricsError(
            f"ReplayStore 安全目錄檢查失敗：目錄遭替換為符號連結 ({base_dir})",
            context={"path": base_dir},
        )
    try:
        os.chmod(base_dir, stat.S_IRWXU)
    except OSError:
        pass
    return base_dir


class OfficialTrustStatus(str, Enum):
    """系統官方信任錨點狀態 (C-2.5.2.2.1 信任根加固)."""
    NOT_CONFIGURED = "OFFICIAL_ROOT_NOT_CONFIGURED"
    CONFIGURED = "OFFICIAL_ROOT_CONFIGURED"


class OfficialTrustAuthority:
    """受保護的官方信任權威管理器 (C-2.5.2.2.2 信任根、執行緒隔離與防重放加固).

    核心安全邊界：
    1. 官方信任根 (Official Root Trust Anchor) 是整個系統授權 REAL_HISTORICAL_PIT_VERIFIED 的唯一信任源頭。
    2. 正式驗證端所信任的根公鑰由受保護的部署環境決定，禁止策略提交者於執行期任意指定或透過環境變數竄改。
    3. 系統當前尚未接入正式外部台股 PKI，預設維持在 OFFICIAL_ROOT_NOT_CONFIGURED 安全狀態，
       所有驗證流程嚴格遵守 fail-closed 原則，保守維持 REAL_HISTORICAL_NOT_VERIFIED。
    4. 測試隔離機制 (isolated_test_context)：
       - 採用 threading.local() 執行緒本地儲存，徹底杜絕多執行緒環境下測試狀態外溢污染 (P1)。
       - 啟動時強制查驗呼叫堆疊，拒絕來自策略模組、回測執行器或非測試腳本之呼叫 (P0)。
       - 測試隔離下產出之報告明確標記 is_test_isolated_verification=True，無法取得正式生產環境身分 (P0)。
       - 注意：Python 進程內防護屬於縱深防禦，不可替代作業系統層級之進程沙盒隔離。
    5. 狀態來源保護：正式驗證端使用的持久化 ReplayStore 均由本管理器單例管控，
       拒絕呼叫端注入之任意 Dummy 物件或自訂子類。
    """

    _status: OfficialTrustStatus = OfficialTrustStatus.NOT_CONFIGURED
    _official_root_anchor: Optional[bytes] = None
    _official_store: Optional["TrustedPublicKeyStore"] = None
    _official_replay_store: Optional["ReplayStore"] = None

    _tls = threading.local()

    @classmethod
    def _get_tls_isolated(cls) -> bool:
        return bool(getattr(cls._tls, "is_test_isolated", False))

    @classmethod
    def _get_tls_store(cls) -> Optional["TrustedPublicKeyStore"]:
        return getattr(cls._tls, "test_store", None)

    @classmethod
    def _get_tls_replay_store(cls) -> Optional["ReplayStore"]:
        return getattr(cls._tls, "test_replay_store", None)

    @classmethod
    def _verify_test_caller(cls) -> None:
        """確認呼叫端來自合法之測試套件，且非源自策略或回測執行迴圈 (P0 縱深防禦)."""
        frames = inspect.stack()[1:]
        caller_verified = False
        for frame_info in frames:
            norm_path = frame_info.filename.replace("\\", "/")
            base_name = os.path.basename(norm_path)
            # 嚴格禁止策略層或回測主迴圈在堆疊中啟動測試隔離
            if (
                "/tw_quant/strategy/" in norm_path
                or norm_path.endswith("/tw_quant/backtest/runner.py")
                or norm_path.endswith("/tw_quant/backtest/adapters.py")
            ):
                raise BacktestMetricsError(
                    "SECURITY_VIOLATION: 禁止於策略執行或回測主迴圈內啟動測試信任隔離環境",
                    context={"caller_file": frame_info.filename, "function": frame_info.function},
                )
            if base_name in ("crypto.py", "contextlib.py"):
                continue
            if not caller_verified:
                # 第一個外部呼叫端必須位於 tests 目錄下且為 test_*.py 檔案
                path_parts = [p.lower() for p in norm_path.split("/") if p]
                if (
                    base_name.startswith("test_")
                    and base_name.endswith(".py")
                    and "tests" in path_parts
                    and "/src/tw_quant/" not in norm_path
                ):
                    caller_verified = True
                else:
                    raise BacktestMetricsError(
                        "SECURITY_VIOLATION: 測試信任隔離環境僅允許由測試套件 (tests/test_*.py) 啟動，"
                        f"禁止非測試程式或外部腳本呼叫 (來源: {frame_info.filename})",
                        context={"caller_file": frame_info.filename, "function": frame_info.function},
                    )
        if not caller_verified:
            raise BacktestMetricsError("SECURITY_VIOLATION: 無法驗證測試隔離呼叫端來源")

    @classmethod
    def is_configured(cls) -> bool:
        """是否已配置有效之官方根信任錨點。"""
        if cls._get_tls_isolated():
            t_store = cls._get_tls_store()
            if t_store is not None:
                return t_store.has_official_root
        return (
            cls._status == OfficialTrustStatus.CONFIGURED
            and cls._official_root_anchor is not None
            and len(cls._official_root_anchor) == 32
        )

    @classmethod
    def is_official_production_configured(cls) -> bool:
        """是否為非測試隔離之正式生產官方根配置狀態 (P0)。"""
        if cls._get_tls_isolated():
            return False
        return (
            cls._status == OfficialTrustStatus.CONFIGURED
            and cls._official_root_anchor is not None
            and len(cls._official_root_anchor) == 32
        )

    @classmethod
    def has_official_root(cls) -> bool:
        """同 is_configured。"""
        return cls.is_configured()

    @classmethod
    def status(cls) -> str:
        """取得目前信任權威狀態字串。"""
        if cls._get_tls_isolated():
            t_store = cls._get_tls_store()
            if t_store is not None:
                return "TEST_ISOLATED_CONFIGURED" if t_store.has_official_root else "TEST_ISOLATED_UNCONFIGURED"
        return cls._status.value

    @classmethod
    def get_official_store(cls) -> "TrustedPublicKeyStore":
        """取得官方受信任公鑰庫單例（若未配置則為無根錨點之空庫，遵循 Fail-Closed）。"""
        if cls._official_store is None:
            cls._official_store = TrustedPublicKeyStore(
                root_trust_anchor=cls._official_root_anchor,
                _is_system_official=True,
            )
        return cls._official_store

    @classmethod
    def get_official_replay_store(cls) -> "ReplayStore":
        """取得受官方驗證環境管制的持久化 SQLite ReplayStore 單例。"""
        if cls._official_replay_store is None:
            secure_dir = _get_secure_replay_dir()
            cls._official_replay_store = ReplayStore(
                db_path=os.path.join(secure_dir, "tw_quant_official_replay_store.sqlite3")
            )
        return cls._official_replay_store

    @classmethod
    def get_active_trust_store(cls) -> Tuple["TrustedPublicKeyStore", bool]:
        """取得當前有效之公鑰庫，回傳 (store, is_test_isolated)。"""
        if cls._get_tls_isolated():
            t_store = cls._get_tls_store()
            if t_store is not None:
                return t_store, True
            return cls.get_official_store(), True
        return cls.get_official_store(), False

    @classmethod
    def get_active_replay_store(cls) -> Tuple["ReplayStore", bool]:
        """取得當前有效之 ReplayStore，回傳 (replay_store, is_test_isolated)。"""
        if cls._get_tls_isolated():
            t_rstore = cls._get_tls_replay_store()
            if t_rstore is not None:
                return t_rstore, True
            return cls.get_official_replay_store(), True
        return cls.get_official_replay_store(), False

    @classmethod
    def is_test_isolated(cls) -> bool:
        return cls._get_tls_isolated()

    @classmethod
    @contextlib.contextmanager
    def isolated_test_context(
        cls,
        *,
        store: Optional["TrustedPublicKeyStore"] = None,
        replay_store: Optional["ReplayStore"] = None,
        test_root: Optional[bytes] = None,
    ):
        """受控測試隔離上下文管理員（僅供單元測試與安全 PoC 驗證，禁止在生產驗證路徑使用）。"""
        cls._verify_test_caller()
        prev_isolated = cls._get_tls_isolated()
        prev_store = cls._get_tls_store()
        prev_rstore = cls._get_tls_replay_store()
        try:
            cls._tls.is_test_isolated = True
            cls._tls.test_store = store
            cls._tls.test_replay_store = replay_store
            yield
        finally:
            cls._tls.is_test_isolated = prev_isolated
            cls._tls.test_store = prev_store
            cls._tls.test_replay_store = prev_rstore

    @classmethod
    def activate_isolated_test_context(
        cls,
        *,
        store: Optional["TrustedPublicKeyStore"] = None,
        replay_store: Optional["ReplayStore"] = None,
        test_root: Optional[bytes] = None,
    ) -> None:
        """手動啟動受控測試隔離狀態（供 TestCase setUp 使用）。"""
        cls._verify_test_caller()
        cls._tls.is_test_isolated = True
        cls._tls.test_store = store
        cls._tls.test_replay_store = replay_store

    @classmethod
    def deactivate_isolated_test_context(cls) -> None:
        """手動停用受控測試隔離狀態（供 TestCase tearDown 使用）。"""
        cls._tls.is_test_isolated = False
        cls._tls.test_store = None
        cls._tls.test_replay_store = None


class TrustedPublicKeyStore:
    """受控信任公鑰庫 (Trusted Public Key Store) — C-2.5.2.2.2 信任根與建構校驗加固版.

    安全原則：
    1. 杜絕呼叫端自封 OFFICIAL：呼叫端不可任意傳入 authority_level=OFFICIAL。
       登記 OFFICIAL 金鑰必須提供官方根信任錨點 (Root Trust Anchor) 之數位背書簽章。
    2. 若系統未配置官方根信任錨點 (當前台股 PKI 尚未接入之狀態)，
       禁止登記任何 OFFICIAL 金鑰，所有非合成資料驗證結論保守維持 REAL_HISTORICAL_NOT_VERIFIED。
    3. 明確區分 TEST_ONLY (測試金鑰) 與 OFFICIAL (正式官方金鑰)。
    4. 徹底移除透過環境變數 (os.environ) 置換官方根錨點之後門，防止提交端污染驗證信任鏈。
    5. 明確標示自製本地庫 (_is_system_official=False)，正式決策閘門不採納未授權之本地庫。
    6. 建構時立即嚴格驗證 root_trust_anchor 之型別、32 位元組長度與 Ed25519 規範曲線點有效性 (P3)。
    """

    def __init__(
        self,
        root_trust_anchor: Optional[bytes | str] = None,
        *,
        official_root_public_key: Optional[bytes | str] = None,
        _is_system_official: bool = False,
    ) -> None:
        self._keys: Dict[str, Tuple[bytes, KeyAuthorityLevel]] = {}
        self._is_system_official = bool(_is_system_official)
        # 安全修復：徹底禁止讀取 os.environ["TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY"]
        if (
            root_trust_anchor is not None
            and official_root_public_key is not None
            and root_trust_anchor != official_root_public_key
        ):
            raise InvalidRootAnchorError("root_trust_anchor 與 official_root_public_key 同時提供但內容不一致")

        effective = root_trust_anchor if root_trust_anchor is not None else official_root_public_key
        if effective is not None:
            if isinstance(effective, bool) or not isinstance(effective, (bytes, str)):
                raise InvalidRootAnchorError(
                    f"root_trust_anchor 型別錯誤：必須為 32 位元組 bytes 或 64 字元 hex 字串，收到 {type(effective).__name__}"
                )
            if isinstance(effective, str):
                cleaned = effective.strip()
                if len(cleaned) != 64:
                    raise InvalidRootAnchorError(
                        f"Ed25519 root_trust_anchor Hex 字串長度必須為 64 字元 (32 位元組)，收到 {len(cleaned)} 字元"
                    )
                try:
                    raw_bytes = bytes.fromhex(cleaned)
                except ValueError as exc:
                    raise InvalidRootAnchorError(f"root_trust_anchor Hex 字串格式無效: {exc}") from exc
            else:
                raw_bytes = bytes(effective)

            if len(raw_bytes) != 32:
                raise InvalidRootAnchorError(
                    f"Ed25519 root_trust_anchor 公鑰長度必須為 32 位元組，收到 {len(raw_bytes)} 位元組"
                )
            try:
                _decodepoint(raw_bytes)
            except Exception as exc:
                raise InvalidRootAnchorError(
                    f"Ed25519 root_trust_anchor 非有效之 RFC 8032 規範曲線點: {exc}"
                ) from exc
            self._root_anchor: Optional[bytes] = raw_bytes
        else:
            self._root_anchor = None

    @property
    def is_system_official(self) -> bool:
        """是否為系統官方信任庫。"""
        return self._is_system_official

    @property
    def has_official_root(self) -> bool:
        """是否已配置有效之受信任官方根金鑰。"""
        return self._root_anchor is not None and len(self._root_anchor) == 32

    @property
    def root_public_key_hex(self) -> Optional[str]:
        return self._root_anchor.hex() if self._root_anchor else None

    def enroll_key(
        self,
        signer_id: str,
        public_key: bytes | str,
        authority_level: KeyAuthorityLevel = KeyAuthorityLevel.TEST_ONLY,
        *,
        root_endorsement_signature: Optional[bytes | str] = None,
    ) -> None:
        """登記公鑰。

        安全約束：
        - 預設授權等級為 TEST_ONLY。
        - 欲登記為 OFFICIAL，系統必須具備官方根錨點，且必須附有根私鑰對
          f"OFFICIAL_GRANT:{signer_id}:{pk_hex}" 之有效 Ed25519 背書簽章。
        - 呼叫端自建 Store 或未具有效根背書簽章者，一律拋出例外拒絕登記 OFFICIAL。
        """
        if not signer_id or not isinstance(signer_id, str):
            raise ValueError("signer_id 必須為非空字串")
        if isinstance(public_key, str):
            pk_bytes = bytes.fromhex(public_key)
        elif isinstance(public_key, bytes):
            pk_bytes = public_key
        else:
            raise ValueError("public_key 必須為 hex 字串或 bytes")
        if len(pk_bytes) != 32:
            raise ValueError(f"Ed25519 公鑰長度必須為 32 位元組，收到 {len(pk_bytes)}")

        if authority_level == KeyAuthorityLevel.OFFICIAL:
            if not self.has_official_root:
                raise ValueError(
                    "目前系統未配置受信任官方根金鑰 (OFFICIAL_ROOT_NOT_CONFIGURED)，"
                    "依安全原則禁止將金鑰登記為 OFFICIAL 授權等級"
                )
            if not root_endorsement_signature:
                raise ValueError("登記 OFFICIAL 金鑰必須提供官方根信任錨點之背書簽章 (root_endorsement_signature)")
            if isinstance(root_endorsement_signature, str):
                sig_bytes = bytes.fromhex(root_endorsement_signature)
            elif isinstance(root_endorsement_signature, bytes):
                sig_bytes = root_endorsement_signature
            else:
                raise ValueError("root_endorsement_signature 必須為 hex 字串或 bytes")
            endorsement_msg = f"OFFICIAL_GRANT:{signer_id}:{pk_bytes.hex()}".encode("utf-8")
            assert self._root_anchor is not None
            if not ed25519_verify(sig_bytes, endorsement_msg, self._root_anchor):
                raise ValueError("官方根背書簽章驗證失敗，禁止登記 OFFICIAL 授權等級")

        self._keys[signer_id] = (pk_bytes, authority_level)

    def is_enrolled(self, signer_id: str) -> bool:
        return signer_id in self._keys

    def get_key(self, signer_id: str) -> Optional[Tuple[bytes, KeyAuthorityLevel]]:
        return self._keys.get(signer_id)

    def verify_signature(
        self,
        signer_id: str,
        signature_bytes: bytes,
        message: bytes,
    ) -> Tuple[bool, str, KeyAuthorityLevel]:
        """驗證簽章，回傳 (是否通過, 原因代碼, 授權級別)。"""
        info = self.get_key(signer_id)
        if info is None:
            return False, "SIGNER_NOT_ENROLLED", KeyAuthorityLevel.UNAUTHORIZED
        pk_bytes, level = info
        ok = ed25519_verify(signature_bytes, message, pk_bytes)
        if not ok:
            return False, "INVALID_ED25519_SIGNATURE", level
        return True, "VERIFIED", level


def validate_signature_timestamps(
    signed_at: Union[str, dt.datetime],
    expires_at: Union[str, dt.datetime],
    *,
    current_time: Optional[dt.datetime] = None,
    max_clock_skew_seconds: int = 300,
) -> Tuple[bool, str]:
    """檢驗數位簽章時間戳之時區、時序因果與有效期 (C-2.5.2.2 修補).

    驗證規則：
    1. signed_at 與 expires_at 必須為有效之 ISO-8601 格式字串或 datetime。
    2. 兩者皆必須為 timezone-aware datetime (嚴格拒絕 naive datetime)。
    3. 轉換至統一基準 (UTC) 後比對：
       - expires_at 必須嚴格晚於 signed_at (expires_at > signed_at)。
       - signed_at 不得晚於目前驗證時間加上容許時鐘偏差 (signed_at <= current_time + max_clock_skew)。
       - 目前時間不得晚於 expires_at (current_time <= expires_at)。
    """
    if isinstance(signed_at, dt.datetime):
        s_dt = signed_at
        signed_at_str = signed_at.isoformat()
    elif isinstance(signed_at, str) and signed_at.strip():
        signed_at_str = signed_at.strip()
        try:
            s_dt = dt.datetime.fromisoformat(signed_at_str)
        except Exception as e:
            return False, f"解析 signed_at 時間戳異常: {e}"
    else:
        return False, "signed_at 必須為非空字串或 datetime"

    if isinstance(expires_at, dt.datetime):
        e_dt = expires_at
        expires_at_str = expires_at.isoformat()
    elif isinstance(expires_at, str) and expires_at.strip():
        expires_at_str = expires_at.strip()
        try:
            e_dt = dt.datetime.fromisoformat(expires_at_str)
        except Exception as e:
            return False, f"解析 expires_at 時間戳異常: {e}"
    else:
        return False, "expires_at 必須為非空字串或 datetime"

    if s_dt.tzinfo is None or s_dt.tzinfo.utcoffset(s_dt) is None:
        return False, f"signed_at 時間戳缺少時區資訊 (naive datetime: {signed_at_str})"
    if e_dt.tzinfo is None or e_dt.tzinfo.utcoffset(e_dt) is None:
        return False, f"expires_at 時間戳缺少時區資訊 (naive datetime: {expires_at_str})"

    # 統一轉換為 UTC 基準進行精確比較
    s_utc = s_dt.astimezone(dt.timezone.utc)
    e_utc = e_dt.astimezone(dt.timezone.utc)

    if e_utc <= s_utc:
        return False, f"簽章有效截止時間 ({expires_at_str}) 必須嚴格晚於簽署時間 ({signed_at_str}) (expires_at 必須晚於 signed_at)"

    now = current_time or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now_utc = now.astimezone(dt.timezone.utc)

    skew = dt.timedelta(seconds=max_clock_skew_seconds)
    if s_utc > now_utc + skew:
        return False, f"簽署時間 ({signed_at_str}) 晚於目前驗證時間 ({now_utc.isoformat()}) 超出容許時鐘偏差 (signed_at 晚於目前時間)"

    if now_utc > e_utc:
        return False, f"數位簽章已過期 (過期時間: {expires_at_str}, 目前時間: {now_utc.isoformat()})"

    return True, ""


class ReplayStore:
    """可持久化、具備跨進程事務與原子性之 Nonce 重放防護儲存庫 (C-2.5.2.2.2 P2 加固).

    安全特性：
    1. 持久化後端與安全目錄：預設採用受權限保護之使用者專屬安全目錄 (`~/.tw_quant_security/replay_store`)，
       拒絕符號連結 (Symlink) 與共用目錄覆寫風險。
    2. 跨進程原子性：SQLite EXCLUSIVE 事務鎖與 WAL 模式，防範多進程併發重放。
    3. 全維度 Scope 綁定：signer_id, project_id, environment_id, run_id, report_id, nonce。
    4. 資料庫完整性與防替換／防刪除／防回滾保護 (P2)：
       - 綁定資料庫實例唯一識別碼 (`store_instance_token`) 與單調遞增序號 (`nonce_seq_count`)。
       - 若運行期間資料庫遭刪除、截斷為空檔、替換為新庫或還原為舊備份，立即偵測並拋出受控例外 (Fail-Closed)。
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            secure_dir = _get_secure_replay_dir()
            db_path = os.path.join(secure_dir, "tw_quant_nonce_replay_store.sqlite3")
        self.db_path = db_path
        self._expected_db_token: Optional[str] = None
        self._min_expected_seq: int = 0
        self._validate_path_security()
        self._init_db()

    def _validate_path_security(self) -> None:
        """驗證資料庫路徑非符號連結且非目錄 (Fail-Closed)."""
        if not self.db_path or not isinstance(self.db_path, str):
            raise BacktestMetricsError("ReplayStore db_path 必須為非空字串")
        abs_path = os.path.abspath(self.db_path)
        parent_dir = os.path.dirname(abs_path)
        if os.path.islink(self.db_path) or os.path.islink(abs_path) or (parent_dir and os.path.islink(parent_dir)):
            raise BacktestMetricsError(
                f"ReplayStore 路徑安全檢查失敗 (Fail-Closed)：禁止使用符號連結 ({self.db_path})",
                context={"db_path": self.db_path},
            )
        if os.path.exists(abs_path) and os.path.isdir(abs_path):
            raise BacktestMetricsError(
                f"ReplayStore 路徑安全檢查失敗 (Fail-Closed)：目標路徑為目錄而非檔案 ({self.db_path})",
                context={"db_path": self.db_path},
            )

    def _init_db(self) -> None:
        try:
            self._validate_path_security()
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level="EXCLUSIVE")
            try:
                with conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS store_metadata (
                            meta_key TEXT PRIMARY KEY,
                            meta_value TEXT NOT NULL
                        );
                    """)
                    conn.execute("""
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
                    """)
                    cur_tok = conn.execute(
                        "SELECT meta_value FROM store_metadata WHERE meta_key = 'store_instance_token'"
                    ).fetchone()
                    if cur_tok is None:
                        new_tok = secrets.token_hex(16)
                        conn.execute(
                            "INSERT INTO store_metadata (meta_key, meta_value) VALUES ('store_instance_token', ?)",
                            (new_tok,),
                        )
                        self._expected_db_token = new_tok
                    else:
                        self._expected_db_token = str(cur_tok[0])

                    cur_seq = conn.execute(
                        "SELECT meta_value FROM store_metadata WHERE meta_key = 'nonce_seq_count'"
                    ).fetchone()
                    if cur_seq is None:
                        conn.execute(
                            "INSERT INTO store_metadata (meta_key, meta_value) VALUES ('nonce_seq_count', '0')"
                        )
                        self._min_expected_seq = 0
                    else:
                        self._min_expected_seq = int(cur_seq[0])
            finally:
                conn.close()
        except BacktestMetricsError:
            raise
        except sqlite3.Error as sqle:
            logger.error("ReplayStore 初始化 SQLite 失敗 (Fail-Closed): %s", sqle)
            raise BacktestMetricsError(
                f"ReplayStore 資料庫初始化失敗 (Fail-Closed: SQLite Error: {sqle})",
                context={"db_path": self.db_path, "error_type": type(sqle).__name__},
            ) from sqle
        except Exception as exc:
            logger.error("ReplayStore 初始化異常 (Fail-Closed): %s", exc)
            raise BacktestMetricsError(
                f"ReplayStore 資料庫初始化異常 (Fail-Closed: {type(exc).__name__}: {exc})",
                context={"db_path": self.db_path, "error_type": type(exc).__name__},
            ) from exc

    def consume_nonce(
        self,
        *,
        signer_id: str,
        project_id: str,
        environment_id: str,
        run_id: str,
        report_id: str,
        nonce: str,
        signed_at: str,
        expires_at: str,
        current_time: Optional[dt.datetime] = None,
    ) -> bool:
        """原子性檢查並登記 Nonce。若已使用、資料庫遭刪除／替換／回滾或異常則拋出受控例外 (BacktestMetricsError)。"""
        if not nonce or not isinstance(nonce, str):
            raise BacktestMetricsError("Nonce 必須為非空字串")

        scope_key = f"{signer_id}:{project_id}:{environment_id}:{run_id}:{report_id}:{nonce}"
        now_dt = current_time or dt.datetime.now(dt.timezone.utc)
        if now_dt.tzinfo is None or now_dt.tzinfo.utcoffset(now_dt) is None:
            now_dt = now_dt.replace(tzinfo=dt.timezone.utc)
        now_iso = now_dt.astimezone(dt.timezone.utc).isoformat()

        try:
            self._validate_path_security()
            if not os.path.exists(self.db_path):
                raise BacktestMetricsError(
                    f"ReplayStore 資料庫檔案已遭刪除或不存在 (Fail-Closed: {self.db_path})",
                    context={"db_path": self.db_path, "scope_key": scope_key},
                )
            if os.path.getsize(self.db_path) == 0:
                raise BacktestMetricsError(
                    f"ReplayStore 資料庫檔案已遭截斷為空檔案 (Fail-Closed: {self.db_path})",
                    context={"db_path": self.db_path, "scope_key": scope_key},
                )

            conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level="EXCLUSIVE")
            try:
                with conn:
                    # 0. 若實例具備預期 Token，驗證資料庫未被外部替換、重建或回滾 (P2)
                    expected_token = getattr(self, "_expected_db_token", None)
                    min_seq = getattr(self, "_min_expected_seq", 0)
                    if expected_token is not None:
                        tok_row = conn.execute(
                            "SELECT meta_value FROM store_metadata WHERE meta_key = 'store_instance_token'"
                        ).fetchone()
                        if tok_row is None or str(tok_row[0]) != expected_token:
                            raise BacktestMetricsError(
                                "ReplayStore 偵測到資料庫遭外部替換、重建或元資料遺失 (Token Mismatch, Fail-Closed)",
                                context={"db_path": self.db_path, "scope_key": scope_key},
                            )
                        seq_row = conn.execute(
                            "SELECT meta_value FROM store_metadata WHERE meta_key = 'nonce_seq_count'"
                        ).fetchone()
                        if seq_row is None or int(seq_row[0]) < min_seq:
                            raise BacktestMetricsError(
                                "ReplayStore 偵測到資料庫狀態回滾或遭還原為舊備份 (Sequence Regression, Fail-Closed)",
                                context={"db_path": self.db_path, "scope_key": scope_key},
                            )
                        current_seq = int(seq_row[0])
                    else:
                        current_seq = 0

                    # 1. 自動清理過期 Nonce
                    conn.execute("DELETE FROM used_nonces WHERE expires_at < ?", (now_iso,))
                    # 2. 查詢是否已存在
                    cur = conn.execute("SELECT consumed_at FROM used_nonces WHERE scope_key = ?", (scope_key,))
                    row = cur.fetchone()
                    if row is not None:
                        raise BacktestMetricsError(
                            f"偵測到 Nonce 重放攻擊 (Replay Attack Detected)：Nonce {nonce} 於 {row[0]} 已被登記消費",
                            context={"scope_key": scope_key, "nonce": nonce},
                        )
                    # 3. 登記 Nonce 並更新單調遞增序號
                    conn.execute(
                        """
                        INSERT INTO used_nonces (
                            scope_key, signer_id, project_id, environment_id, run_id, report_id,
                            nonce, signed_at, expires_at, consumed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (scope_key, signer_id, project_id, environment_id, run_id, report_id, nonce, signed_at, expires_at, now_iso),
                    )
                    if expected_token is not None:
                        new_seq = current_seq + 1
                        conn.execute(
                            "UPDATE store_metadata SET meta_value = ? WHERE meta_key = 'nonce_seq_count'",
                            (str(new_seq),),
                        )
                        self._min_expected_seq = new_seq
            finally:
                conn.close()
        except BacktestMetricsError:
            raise
        except sqlite3.Error as sqle:
            logger.error("ReplayStore SQLite 異常 (Fail-Closed): %s", sqle)
            raise BacktestMetricsError(
                f"ReplayStore 持久化防重放存取異常 (Fail-Closed: {type(sqle).__name__}: {sqle})，無法保證 Nonce 原子性登記",
                context={"error_type": type(sqle).__name__, "db_path": self.db_path, "scope_key": scope_key},
            ) from sqle
        except Exception as exc:
            logger.error("ReplayStore 非預期異常 (Fail-Closed): %s", exc)
            raise BacktestMetricsError(
                f"ReplayStore 存取遭遇非預期異常 (Fail-Closed: {type(exc).__name__}: {exc})",
                context={"error_type": type(exc).__name__, "db_path": self.db_path},
            ) from exc

        return True


_DEFAULT_REPLAY_STORE: Optional[ReplayStore] = None


def get_default_replay_store(db_path: Optional[str] = None) -> ReplayStore:
    global _DEFAULT_REPLAY_STORE
    if db_path is not None:
        return ReplayStore(db_path=db_path)
    if _DEFAULT_REPLAY_STORE is None:
        _DEFAULT_REPLAY_STORE = ReplayStore()
    return _DEFAULT_REPLAY_STORE


# ── 報告上下文綁定與簽章結構 ──────────────────────────────────────────────


@dataclass(frozen=True)
class SignatureContext:
    """回測報告數位簽章綁定之完整上下文。"""

    report_id: str
    run_id: str
    strategy_id: str
    project_id: str
    environment_id: str
    source_git_commit: str
    content_hash: str
    validation_verdict: str
    claims_real_historical_verified: bool
    model_versions: Tuple[Tuple[str, str, str, bool], ...]
    data_versions: Mapping[str, Any]
    signed_at: str
    expires_at: str
    nonce: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "project_id": self.project_id,
            "environment_id": self.environment_id,
            "source_git_commit": self.source_git_commit,
            "content_hash": self.content_hash,
            "validation_verdict": self.validation_verdict,
            "claims_real_historical_verified": self.claims_real_historical_verified,
            "model_versions": [list(item) for item in self.model_versions],
            "data_versions": dict(self.data_versions),
            "signed_at": self.signed_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    def canonical_bytes(self) -> bytes:
        """確定性規範 JSON 位元組。"""
        s = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return s.encode("utf-8")


@dataclass(frozen=True)
class ReportSignature:
    """完整非對稱數位簽章封包。"""

    algorithm: str
    signer_id: str
    public_key_hex: str
    signature_hex: str
    signed_at: str
    expires_at: str
    nonce: str
    context: Dict[str, Any]
    public_key_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        fp = self.public_key_fingerprint
        if not fp:
            try:
                fp = hashlib.sha256(bytes.fromhex(self.public_key_hex)).hexdigest()[:16]
            except Exception:
                fp = ""
        return {
            "algorithm": self.algorithm,
            "signer_id": self.signer_id,
            "public_key_hex": self.public_key_hex,
            "public_key_fingerprint": fp,
            "signature_hex": self.signature_hex,
            "signed_at": self.signed_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "context": copy.deepcopy(self.context),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ReportSignature":
        for k in ("algorithm", "signer_id", "public_key_hex", "signature_hex", "signed_at", "expires_at", "nonce", "context"):
            if k not in d:
                raise ValueError(f"ReportSignature 缺少必要欄位: {k}")
        return cls(
            algorithm=str(d["algorithm"]),
            signer_id=str(d["signer_id"]),
            public_key_hex=str(d["public_key_hex"]),
            public_key_fingerprint=str(d.get("public_key_fingerprint", "")),
            signature_hex=str(d["signature_hex"]),
            signed_at=str(d["signed_at"]),
            expires_at=str(d["expires_at"]),
            nonce=str(d["nonce"]),
            context=dict(d["context"]),
        )
