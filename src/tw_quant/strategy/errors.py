"""策略模組例外型別 (errors.py)."""

from __future__ import annotations


class StrategyConfigError(ValueError):
    """策略設定不合法。"""


class StrategyInputError(ValueError):
    """呼叫端提供的持倉快照、時間或型別不合法 (屬呼叫端錯誤，策略拒絕繼續)."""
