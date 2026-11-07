"""策略設定與交易成本模型 (config.py).

核心原則：
1. 策略參數 (持股數、分數門檻、目標比例、持有期限、成本) 一律由呼叫端明確提供，不提供「通用預設值」。
2. 所有金額與比例採用 Decimal。
3. 成本估算公式與 TaiwanPaperBroker / FillEngine 對齊，並提供 matches_paper_config() 供偵測差異。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Tuple

from ..contracts.signal import SignalOrderType
from .errors import StrategyConfigError

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWOPLACES = Decimal("0.01")


def _check_ratio(name: str, value: Any, *, allow_zero: bool, allow_one: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StrategyConfigError(f"{name} 必須是有限的 Decimal，收到 {value!r}")
    low_ok = value >= _ZERO if allow_zero else value > _ZERO
    high_ok = value <= _ONE if allow_one else value < _ONE
    if not (low_ok and high_ok):
        raise StrategyConfigError(f"{name} 超出允許範圍，收到 {value}")


@dataclass(frozen=True)
class CostModel:
    """交易成本估算模型 (預估用途，非實際成交帳務)."""

    slippage_rate: Decimal
    commission_rate: Decimal
    commission_discount: Decimal
    min_commission: Decimal
    stock_tax_rate: Decimal
    etf_tax_rate: Decimal

    def __post_init__(self) -> None:
        for name in ("slippage_rate", "commission_rate", "commission_discount", "stock_tax_rate", "etf_tax_rate"):
            _check_ratio(name, getattr(self, name), allow_zero=True)
        if not isinstance(self.min_commission, Decimal) or not self.min_commission.is_finite() or self.min_commission < _ZERO:
            raise StrategyConfigError(f"min_commission 必須是非負有限 Decimal，收到 {self.min_commission!r}")

    @classmethod
    def from_paper_config(cls, cfg: Any) -> "CostModel":
        """由 TaiwanPaperConfig 建立，確保估算費率與模擬券商一致。"""
        return cls(
            slippage_rate=cfg.slippage_rate,
            commission_rate=cfg.commission_rate,
            commission_discount=cfg.commission_discount,
            min_commission=cfg.min_commission,
            stock_tax_rate=cfg.stock_tax_rate,
            etf_tax_rate=cfg.etf_tax_rate,
        )

    def matches_paper_config(self, cfg: Any) -> bool:
        """是否與模擬券商設定完全一致 (不一致時估算只能視為近似)."""
        return self == CostModel.from_paper_config(cfg)

    def buy_notional(self, price: Decimal, shares: int) -> Decimal:
        """買入預估名目金額 = price*(1+滑價)*shares，取至 0.01 (運算順序與 TaiwanPaperBroker 預留邏輯相同)."""
        return (price * (_ONE + self.slippage_rate) * Decimal(shares)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)

    def commission(self, notional: Decimal) -> Decimal:
        """手續費 (與 FillEngine.calculate_commission 相同的取整與最低手續費規則)."""
        raw = notional * self.commission_rate * self.commission_discount
        return max(self.min_commission, raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def sell_tax(self, symbol: str, notional: Decimal) -> Decimal:
        """賣出證交稅 (00 開頭視為 ETF；無條件捨去；未涵蓋當沖優惠)."""
        rate = self.etf_tax_rate if symbol.split(".")[0].startswith("00") else self.stock_tax_rate
        return (notional * rate).quantize(Decimal("1"), rounding=ROUND_FLOOR)

    def estimate_buy(self, price: Decimal, shares: int) -> Tuple[Decimal, Decimal, Decimal]:
        """回傳 (名目金額, 手續費, 預估現金支出)."""
        notional = self.buy_notional(price, shares)
        fee = self.commission(notional)
        return notional, fee, notional + fee

    def estimate_sell(
        self, symbol: str, price: Decimal, shares: int, order_type: SignalOrderType
    ) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
        """回傳 (名目金額, 手續費, 證交稅, 預估淨回收現金)。

        限價賣單以限價估算；市價賣單扣除滑價。實際成交價由 FillEngine 決定。
        """
        px = price if order_type == SignalOrderType.LIMIT else price * (_ONE - self.slippage_rate)
        notional = (px * Decimal(shares)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
        fee = self.commission(notional)
        tax = self.sell_tax(symbol, notional)
        return notional, fee, tax, notional - fee - tax


@dataclass(frozen=True)
class StrategyConfig:
    """「AI 排名選股 + 等權重 + 固定持有期限」策略設定。

    必填欄位沒有預設值：持股數、分數門檻、目標比例、持有期限與成本模型都因情境而異，
    不得由系統代為假設。
    """

    strategy_name: str
    strategy_version: str
    max_positions: int
    min_score: float
    target_position_ratio: Decimal
    holding_trading_days: int
    cost_model: CostModel

    max_single_position_ratio: Decimal = Decimal("0.10")
    min_cash_ratio: Decimal = Decimal("0.20")
    allow_new_positions: bool = True
    allow_reduce_positions: bool = True
    exit_when_not_selected: bool = True
    allow_mock_predictions: bool = False      # 僅測試模式可設為 True
    order_type: SignalOrderType = SignalOrderType.LIMIT
    signal_validity: dt.timedelta = dt.timedelta(hours=4, minutes=30)  # 自預定進場日 09:00 起算
    share_unit: int = 1000                    # 取整單位 (股)：1000=整張，1=允許零股
    rebalance_tolerance_ratio: Decimal = Decimal("0.01")  # 目標與現況差距低於 NAV 此比例不調整

    def __post_init__(self) -> None:
        for name in ("strategy_name", "strategy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _NAME_RE.match(value):
                raise StrategyConfigError(f"{name} 僅允許英數、底線、點與連字號 (1~64 字元)，收到 {value!r}")

        for name in ("max_positions", "holding_trading_days", "share_unit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise StrategyConfigError(f"{name} 必須是 >= 1 的整數，收到 {value!r}")

        if isinstance(self.min_score, bool) or not isinstance(self.min_score, (int, float)) or not math.isfinite(self.min_score):
            raise StrategyConfigError(f"min_score 必須是有限數值，收到 {self.min_score!r}")

        _check_ratio("target_position_ratio", self.target_position_ratio, allow_zero=False)
        _check_ratio("max_single_position_ratio", self.max_single_position_ratio, allow_zero=False)
        _check_ratio("min_cash_ratio", self.min_cash_ratio, allow_zero=True, allow_one=False)
        _check_ratio("rebalance_tolerance_ratio", self.rebalance_tolerance_ratio, allow_zero=True)

        if self.target_position_ratio > self.max_single_position_ratio:
            raise StrategyConfigError("target_position_ratio 不得大於 max_single_position_ratio")
        if self.rebalance_tolerance_ratio >= self.target_position_ratio:
            raise StrategyConfigError("rebalance_tolerance_ratio 必須小於 target_position_ratio")
        if Decimal(self.max_positions) * self.target_position_ratio > _ONE - self.min_cash_ratio:
            raise StrategyConfigError(
                "max_positions * target_position_ratio 不得超過 (1 - min_cash_ratio)，否則滿倉配置必然違反最低現金比例"
            )

        for name in ("allow_new_positions", "allow_reduce_positions", "exit_when_not_selected", "allow_mock_predictions"):
            if not isinstance(getattr(self, name), bool):
                raise StrategyConfigError(f"{name} 必須是 bool")

        if not isinstance(self.order_type, SignalOrderType):
            raise StrategyConfigError("order_type 必須是 SignalOrderType")
        if not isinstance(self.cost_model, CostModel):
            raise StrategyConfigError("cost_model 必須是 CostModel")
        if not isinstance(self.signal_validity, dt.timedelta) or not (dt.timedelta(0) < self.signal_validity <= dt.timedelta(days=1)):
            raise StrategyConfigError("signal_validity 必須介於 (0, 1 天]")

    @property
    def strategy_id(self) -> str:
        """寫入 UnifiedTradeSignal.strategy_id 的識別字串 (名稱:版本)."""
        return f"{self.strategy_name}:{self.strategy_version}"
