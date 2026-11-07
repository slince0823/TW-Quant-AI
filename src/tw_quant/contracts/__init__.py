"""TW Quant AI Contracts Package.

匯出統一交易訊號規格與轉換函式。
"""

from .conversion import is_finite_positive_number, lots_to_shares, shares_to_lots
from .dedup import DuplicateSignalError, SignalDeduplicator
from .signal import (
    QuantityUnit,
    SignalAction,
    SignalMode,
    SignalOrderType,
    UnifiedTradeSignal,
)

__all__ = [
    "DuplicateSignalError",
    "QuantityUnit",
    "SignalAction",
    "SignalDeduplicator",
    "SignalMode",
    "SignalOrderType",
    "UnifiedTradeSignal",
    "is_finite_positive_number",
    "lots_to_shares",
    "shares_to_lots",
]
