"""持倉與帳戶快照 (snapshot.py).

核心原則：
1. 所有欄位皆為必填且沒有預設值：缺少持倉、現金或價格時不得以「空持倉 / 預設值」繼續。
2. NAV 由現金 + 預留現金 + 持股市值「計算」而得 (與 RiskContext.calculate_current_nav 同義)，
   不接受獨立傳入，也不允許在缺少持股報價時估算。
3. 時間戳記強制 timezone-aware。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Any, FrozenSet, Mapping, Optional

from .errors import StrategyInputError

_PLAIN_SYMBOL = re.compile(r"^[0-9A-Z]{4,6}$")
_TAIPEI = dt.timezone(dt.timedelta(hours=8))
_TWOPLACES = Decimal("0.01")


def is_plain_symbol(symbol: object) -> bool:
    """策略內部只接受不含 .TW/.TWO 後綴的 4~6 碼代號，避免同一檔股票出現兩種鍵值。"""
    return isinstance(symbol, str) and bool(_PLAIN_SYMBOL.match(symbol))


def _require_aware(value: object, name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise StrategyInputError(f"{name} 必須是具備明確時區的 datetime (timezone-aware)")
    return value


@dataclass(frozen=True)
class PositionSnapshot:
    """單一標的持倉快照。

    entry_date 為「買進成交的交易日」。PortfolioLedger 目前不記錄進場日，須由呼叫端另行提供；
    若為 None，策略無法判斷持有期限是否屆滿，會明確標示而不是假設。
    """

    symbol: str
    quantity: int
    reserved_quantity: int
    entry_date: Optional[dt.date]

    def __post_init__(self) -> None:
        if not is_plain_symbol(self.symbol):
            raise StrategyInputError(f"持倉代號必須為 4~6 碼不含後綴的台股代號，收到 {self.symbol!r}")
        for name in ("quantity", "reserved_quantity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise StrategyInputError(f"{name} 必須是整數股數，收到 {value!r}")
        if self.quantity <= 0:
            raise StrategyInputError(f"持倉 {self.symbol} 數量必須 > 0 (無持倉請不要放入 positions)")
        if not (0 <= self.reserved_quantity <= self.quantity):
            raise StrategyInputError(f"持倉 {self.symbol} 預留股數必須介於 0 與持倉數量之間")
        if self.entry_date is not None and (
            isinstance(self.entry_date, dt.datetime) or not isinstance(self.entry_date, dt.date)
        ):
            raise StrategyInputError("entry_date 必須是 datetime.date 或 None")

    @property
    def available_quantity(self) -> int:
        return self.quantity - self.reserved_quantity


@dataclass(frozen=True)
class PortfolioSnapshot:
    """帳戶快照 (不可變)。"""

    as_of: dt.datetime
    cash: Decimal                              # 可用現金 (不含已預留)
    reserved_cash: Decimal                     # 買單已預留現金
    positions: Mapping[str, PositionSnapshot]  # 必填；空 dict 代表「明確確認無持倉」
    prices: Mapping[str, Decimal]              # 參考價 (例如最近收盤價)
    prices_as_of: dt.datetime                  # 參考價所屬時間
    pending_buy_symbols: FrozenSet[str]        # 已有未結買單的標的 (必填；空集合代表明確確認無)

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        _require_aware(self.prices_as_of, "prices_as_of")

        for name in ("cash", "reserved_cash"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < Decimal("0"):
                raise StrategyInputError(f"{name} 必須是非負有限 Decimal，收到 {value!r}")

        if self.positions is None or not isinstance(self.positions, Mapping):
            raise StrategyInputError("positions 為必填 (None 不代表無持倉；無持倉請明確傳入空 dict)")
        if self.prices is None or not isinstance(self.prices, Mapping):
            raise StrategyInputError("prices 為必填 Mapping")
        if self.pending_buy_symbols is None:
            raise StrategyInputError("pending_buy_symbols 為必填 (無未結買單請明確傳入空集合)")

        positions = dict(self.positions)
        for key, pos in positions.items():
            if not isinstance(pos, PositionSnapshot) or pos.symbol != key:
                raise StrategyInputError(f"positions[{key!r}] 必須是 symbol 相同的 PositionSnapshot")

        prices = dict(self.prices)
        for sym, px in prices.items():
            if not is_plain_symbol(sym):
                raise StrategyInputError(f"prices 代號格式不合法: {sym!r}")
            if not isinstance(px, Decimal) or not px.is_finite() or px <= Decimal("0"):
                raise StrategyInputError(f"prices[{sym!r}] 必須是 > 0 的有限 Decimal (禁止 float / 0 / NaN)，收到 {px!r}")

        pending = frozenset(self.pending_buy_symbols)
        for sym in pending:
            if not is_plain_symbol(sym):
                raise StrategyInputError(f"pending_buy_symbols 代號格式不合法: {sym!r}")

        as_of_date = self.as_of.astimezone(_TAIPEI).date()
        for sym, pos in positions.items():
            if sym not in prices:
                raise StrategyInputError(f"持倉 {sym} 缺少參考價，無法計算 NAV；拒絕以 0 或均價代替")
            if pos.entry_date is not None and pos.entry_date > as_of_date:
                raise StrategyInputError(f"持倉 {sym} 的 entry_date ({pos.entry_date}) 晚於快照日期 ({as_of_date})")

        object.__setattr__(self, "positions", MappingProxyType(positions))
        object.__setattr__(self, "prices", MappingProxyType(prices))
        object.__setattr__(self, "pending_buy_symbols", pending)

    @property
    def market_value(self) -> Decimal:
        total = Decimal("0.00")
        for sym, pos in self.positions.items():
            total += (self.prices[sym] * Decimal(pos.quantity)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
        return total

    @property
    def nav(self) -> Decimal:
        """總資產淨值 = 可用現金 + 預留現金 + 持股市值。"""
        return self.cash + self.reserved_cash + self.market_value

    @classmethod
    def from_broker(
        cls,
        broker: Any,
        as_of: dt.datetime,
        prices: Mapping[str, Decimal],
        prices_as_of: dt.datetime,
        entry_dates: Mapping[str, dt.date],
    ) -> "PortfolioSnapshot":
        """讀取 TaiwanPaperBroker 目前帳本建立快照 (唯讀，不呼叫任何下單方法)。

        entry_dates 必填：帳本不記錄進場日，缺少的標的 entry_date 會是 None。
        """
        ledger = broker.ledger
        positions = {
            sym: PositionSnapshot(
                symbol=sym,
                quantity=pos.quantity,
                reserved_quantity=ledger.reserved_shares.get(sym, 0),
                entry_date=entry_dates.get(sym),
            )
            for sym, pos in ledger.positions.items()
            if pos.quantity > 0
        }
        pending = frozenset(
            o.symbol for o in broker.order_manager.get_open_orders() if o.action.value == "BUY"
        )
        return cls(
            as_of=as_of,
            cash=ledger.cash,
            reserved_cash=ledger.reserved_cash,
            positions=positions,
            prices=prices,
            prices_as_of=prices_as_of,
            pending_buy_symbols=pending,
        )
