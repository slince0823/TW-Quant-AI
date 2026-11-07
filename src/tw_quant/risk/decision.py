"""風控決策結果模型 (decision.py).

核心原則：
1. 風控結果不可只是一個布林值，所有檢查過程與數值門檻必須具備 100% 可溯源性。
2. 明確定義標準化拒絕原因代碼 (RiskRejectCode)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RiskRejectCode(str, Enum):
    """風控拒絕原因標準代碼。"""
    SIGNAL_INVALID = "SIGNAL_INVALID"                               # 訊號無效或欄位非法
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"                               # 訊號已過期
    SYMBOL_HALTED = "SYMBOL_HALTED"                                 # 標的停牌暫停交易
    SYMBOL_RESTRICTED = "SYMBOL_RESTRICTED"                         # 標的屬於受限或黑名單
    MISSING_MARKET_DATA = "MISSING_MARKET_DATA"                     # 缺少必要市場估值或報價
    SINGLE_ORDER_LIMIT_EXCEEDED = "SINGLE_ORDER_LIMIT_EXCEEDED"     # 超過單筆委託金額上限
    SINGLE_POSITION_LIMIT_EXCEEDED = "SINGLE_POSITION_LIMIT_EXCEEDED"  # 超過單一股票持倉市值比例上限
    TOTAL_EXPOSURE_LIMIT_EXCEEDED = "TOTAL_EXPOSURE_LIMIT_EXCEEDED" # 超過總投資股票持倉比例上限
    INSUFFICIENT_CASH_RESERVE = "INSUFFICIENT_CASH_RESERVE"         # 違反最低可用現金保留比例或現金不足
    DAILY_LOSS_LIMIT_EXCEEDED = "DAILY_LOSS_LIMIT_EXCEEDED"         # 觸發每日虧損上限門檻
    CIRCUIT_BREAKER_ACTIVE = "CIRCUIT_BREAKER_ACTIVE"               # 系統處於最大回撤熔斷狀態
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"                       # 系統處於緊急停止狀態
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"                           # 重複訊號識別碼
    SHORT_SELLING_DISALLOWED = "SHORT_SELLING_DISALLOWED"           # 系統未啟用放空，禁止賣出超過持有部位


@dataclass(frozen=True)
class RiskCheckItem:
    """單道安全防線檢核紀錄。"""
    name: str
    passed: bool
    reason_code: Optional[RiskRejectCode] = None
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    """獨立風控最終裁決報告。"""

    allowed: bool
    signal_id: str
    reason_codes: List[str]
    checks: Dict[str, Dict[str, Any]]
    evaluated_at: dt.datetime
    risk_config_version: str
    is_risk_reducing: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """序列化為字典，以供審計記錄與風控日誌使用。"""
        return {
            "allowed": self.allowed,
            "signal_id": self.signal_id,
            "reason_codes": self.reason_codes,
            "checks": self.checks,
            "evaluated_at": self.evaluated_at.isoformat(),
            "risk_config_version": self.risk_config_version,
            "is_risk_reducing": self.is_risk_reducing,
        }
