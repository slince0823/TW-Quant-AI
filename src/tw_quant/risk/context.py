"""風控情境快照模型 (context.py).

核心原則：
1. 傳入風控引擎之資料必須是一致性的記憶體狀態快照。
2. 所有金額與價格全程採用 Decimal 精確計量。
3. 嚴格隔離可用與預留之現金與部位。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Set


@dataclass
class RiskContext:
    """風控評估情境快照 (包含帳戶狀態、未成交委託與市場盤態)."""

    # 1. 評估時間
    current_time: dt.datetime

    # 2. 現金帳戶
    cash: Decimal                                         # 可用現金 (尚未凍結)
    reserved_cash: Decimal = Decimal("0.00")              # 買入委託已凍結現金

    # 3. 部位與在途委託
    positions: Dict[str, int] = field(default_factory=dict)         # 目前持有股數 (symbol -> shares)
    reserved_shares: Dict[str, int] = field(default_factory=dict)   # 賣出委託已凍結股數 (symbol -> shares)
    pending_buy_notionals: Dict[str, Decimal] = field(default_factory=dict)  # 在途未成交買單預估金額 (symbol -> notional)

    # 4. 市場行情與盤態
    market_prices: Dict[str, Decimal] = field(default_factory=dict) # 最新有效報價 (symbol -> price)
    halted_symbols: Set[str] = field(default_factory=set)           # 停牌暫停交易標的集合
    disposition_symbols: Set[str] = field(default_factory=set)      # 處置股票標的集合

    # 5. 基準與績效追蹤
    baseline_nav: Decimal = Decimal("1000000.00")         # 當日基準日初 NAV (供計算當日虧損率)
    daily_realized_pnl: Decimal = Decimal("0.00")         # 當日累計已實現損益
    daily_unrealized_pnl: Decimal = Decimal("0.00")       # 當日未實現損益
    peak_nav: Decimal = Decimal("1000000.00")             # 歷史最高資產淨值

    def __post_init__(self) -> None:
        """檢查時間時區與金額有效性。"""
        if not isinstance(self.current_time, dt.datetime) or self.current_time.tzinfo is None:
            raise ValueError("current_time 必須是具備明確時區的 datetime (timezone-aware)")

        for name, dec_val in [
            ("cash", self.cash),
            ("reserved_cash", self.reserved_cash),
            ("baseline_nav", self.baseline_nav),
            ("peak_nav", self.peak_nav),
        ]:
            if not isinstance(dec_val, Decimal):
                raise TypeError(f"{name} 必須是 Decimal 物件，收到 {type(dec_val).__name__}")
            if dec_val < Decimal("0"):
                raise ValueError(f"{name} 不能小於 0，收到 {dec_val}")

    def get_available_shares(self, symbol: str) -> int:
        """取得標的目前尚未預留的可用股數。"""
        held = self.positions.get(symbol, 0)
        res = self.reserved_shares.get(symbol, 0)
        return max(0, held - res)

    def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """查詢標的有效市價，無報價或無效時回傳 None。"""
        px = self.market_prices.get(symbol)
        if px is None or not isinstance(px, Decimal) or px <= Decimal("0"):
            return None
        return px

    def get_total_market_value(self) -> Optional[Decimal]:
        """計算所有持股之總市值。若有持股但缺少有效市價，回傳 None（表示無法準確估值）。"""
        total = Decimal("0.00")
        for sym, qty in self.positions.items():
            if qty > 0:
                px = self.get_symbol_price(sym)
                if px is None:
                    return None
                val = (px * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                total += val
        return total

    def calculate_current_nav(self) -> Optional[Decimal]:
        """計算目前總 NAV = 可用現金 + 預留現金 + 持股市值。缺失報價時回傳 None。"""
        mkt = self.get_total_market_value()
        if mkt is None:
            return None
        return self.cash + self.reserved_cash + mkt

    def get_daily_loss_amount(self) -> Decimal:
        """取得當日總虧損金額（當日損益為負時取絕對值，為正時為 0）。"""
        total_daily_pnl = self.daily_realized_pnl + self.daily_unrealized_pnl
        if total_daily_pnl < Decimal("0"):
            return abs(total_daily_pnl)
        return Decimal("0.00")
