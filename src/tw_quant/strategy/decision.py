"""策略決策層資料模型 (decision.py).

HOLD / SKIP 屬於策略決策層狀態，UnifiedTradeSignal 的 SignalAction 只有 BUY / SELL / CLOSE，
因此 HOLD 與 SKIP 永遠不會被轉成交易訊號。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..contracts.signal import UnifiedTradeSignal


class StrategyAction(str, Enum):
    """策略決策動作。"""

    BUY = "BUY"      # 建議新增 / 增加持倉 -> 轉為 SignalAction.BUY
    SELL = "SELL"    # 建議減少既有持倉 (部分賣出) -> 轉為 SignalAction.SELL
    CLOSE = "CLOSE"  # 建議平掉全部既有持倉 -> 轉為 SignalAction.CLOSE
    HOLD = "HOLD"    # 維持目前持倉，不產生訊號
    SKIP = "SKIP"    # 不交易，不產生訊號


class StrategyReason(str, Enum):
    """決策原因代碼 (供稽核與回測分析使用)."""

    # 進場 / 調整
    SELECTED_BY_RANK = "SELECTED_BY_RANK"
    BELOW_TARGET_REBALANCE = "BELOW_TARGET_REBALANCE"
    OVER_TARGET_TRIM = "OVER_TARGET_TRIM"
    # 出場
    HOLDING_PERIOD_EXPIRED = "HOLDING_PERIOD_EXPIRED"
    NOT_IN_TARGET_PORTFOLIO = "NOT_IN_TARGET_PORTFOLIO"
    # HOLD
    AT_TARGET = "AT_TARGET"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    HOLDING_PERIOD_NOT_REACHED = "HOLDING_PERIOD_NOT_REACHED"
    NO_VALID_PREDICTION = "NO_VALID_PREDICTION"
    UNKNOWN_ENTRY_DATE = "UNKNOWN_ENTRY_DATE"
    REDUCE_DISABLED = "REDUCE_DISABLED"
    PENDING_SELL_ORDER = "PENDING_SELL_ORDER"
    # SKIP
    SCORE_BELOW_MIN = "SCORE_BELOW_MIN"
    RANK_BEYOND_MAX_POSITIONS = "RANK_BEYOND_MAX_POSITIONS"
    NEW_POSITIONS_DISABLED = "NEW_POSITIONS_DISABLED"
    PENDING_BUY_ORDER = "PENDING_BUY_ORDER"
    MISSING_PRICE = "MISSING_PRICE"
    PREDICTION_FAILED = "PREDICTION_FAILED"
    MISSING_FEATURES = "MISSING_FEATURES"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    ROUNDS_TO_ZERO = "ROUNDS_TO_ZERO"
    DUPLICATE_SIGNAL_SUPPRESSED = "DUPLICATE_SIGNAL_SUPPRESSED"
    SIGNAL_CONTRACT_ERROR = "SIGNAL_CONTRACT_ERROR"


class BatchRejectCode(str, Enum):
    """整批預測 / 輸入層級的拒絕代碼 (拒絕時不產生任何進場訊號)."""

    CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
    NOT_A_PREDICTION_BATCH = "NOT_A_PREDICTION_BATCH"
    BATCH_INVALID = "BATCH_INVALID"
    MOCK_MODEL_NOT_ALLOWED = "MOCK_MODEL_NOT_ALLOWED"
    MODEL_VERSION_MISSING = "MODEL_VERSION_MISSING"
    FEATURE_VERSION_MISSING = "FEATURE_VERSION_MISSING"
    INCONSISTENT_BATCH = "INCONSISTENT_BATCH"
    RANK_SCORE_INCONSISTENT = "RANK_SCORE_INCONSISTENT"
    AS_OF_DATE_MISMATCH = "AS_OF_DATE_MISMATCH"
    DECISION_BEFORE_PREDICTION = "DECISION_BEFORE_PREDICTION"
    PRICES_FROM_FUTURE = "PRICES_FROM_FUTURE"
    SNAPSHOT_FROM_FUTURE = "SNAPSHOT_FROM_FUTURE"
    DECISION_TOO_LATE = "DECISION_TOO_LATE"
    INVALID_SYMBOL = "INVALID_SYMBOL"


@dataclass(frozen=True)
class PositionTarget:
    """單一標的目標持倉與調整估算 (預估值，不是實際成交帳務)."""

    symbol: str
    reference_price: Optional[Decimal]
    current_quantity: int
    current_value: Optional[Decimal]
    target_ratio: Decimal            # 目標持倉佔 NAV 比例 (固定設定值，與 AI score 無關)
    target_value: Optional[Decimal]  # 目標持倉市值
    adjust_quantity: int             # 預計調整股數 (>0 買進 / <0 賣出 / 0 不調整)
    trade_notional: Decimal          # 預計交易名目金額 (含滑價估算)
    estimated_fee: Decimal           # 預計手續費
    estimated_tax: Decimal           # 預計證交稅 (僅賣出)
    estimated_cash_flow: Decimal     # 預計現金變動 (買進為負、賣出為正)

    def to_dict(self) -> Dict[str, Any]:
        def d(v: Optional[Decimal]) -> Optional[str]:
            return None if v is None else str(v)

        return {
            "symbol": self.symbol,
            "reference_price": d(self.reference_price),
            "current_quantity": self.current_quantity,
            "current_value": d(self.current_value),
            "target_ratio": str(self.target_ratio),
            "target_value": d(self.target_value),
            "adjust_quantity": self.adjust_quantity,
            "trade_notional": str(self.trade_notional),
            "estimated_fee": str(self.estimated_fee),
            "estimated_tax": str(self.estimated_tax),
            "estimated_cash_flow": str(self.estimated_cash_flow),
        }


@dataclass(frozen=True)
class StrategyDecision:
    """單一標的的策略決策 (含原因)。"""

    symbol: str
    action: StrategyAction
    reason: StrategyReason
    detail: str
    rank: Optional[int] = None
    score: Optional[float] = None
    target: Optional[PositionTarget] = None
    signal_id: Optional[str] = None   # 僅 BUY / SELL / CLOSE 且實際簽出訊號時有值

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "rank": self.rank,
            "score": self.score,
            "target": None if self.target is None else self.target.to_dict(),
            "signal_id": self.signal_id,
        }


@dataclass(frozen=True)
class StrategyResult:
    """一次策略評估的完整結果。"""

    strategy_id: str
    batch_id: Optional[str]
    decision_time: dt.datetime
    planned_entry_date: Optional[dt.date]
    model_version: Optional[str]
    feature_version: Optional[str]
    is_mock_source: bool
    test_mode: bool                                        # 以 Mock 預測進行測試時為 True
    batch_rejection: Optional[BatchRejectCode]
    batch_rejection_detail: str
    decisions: Tuple[StrategyDecision, ...] = field(default_factory=tuple)
    signals: Tuple[UnifiedTradeSignal, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def executable(self) -> bool:
        """是否含可送交風控的訊號 (不代表風控會核准，更不代表會成交)."""
        return bool(self.signals)

    def decisions_by_symbol(self) -> Dict[str, StrategyDecision]:
        return {d.symbol: d for d in self.decisions}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "batch_id": self.batch_id,
            "decision_time": self.decision_time.isoformat(),
            "planned_entry_date": None if self.planned_entry_date is None else self.planned_entry_date.isoformat(),
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "is_mock_source": self.is_mock_source,
            "test_mode": self.test_mode,
            "batch_rejection": None if self.batch_rejection is None else self.batch_rejection.value,
            "batch_rejection_detail": self.batch_rejection_detail,
            "decisions": [d.to_dict() for d in self.decisions],
            "signals": [s.to_dict() for s in self.signals],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DispatchRecord:
    """透過 TradingGateway 送出一筆策略訊號後的結果紀錄 (核准 != 成交)."""

    signal_id: str
    symbol: str
    action: str
    risk_allowed: bool
    reason_codes: Tuple[str, ...]
    order_id: Optional[str]
    order_status: Optional[str]
    note: str = ""
