"""台股模擬撮合引擎 (engine.py).

核心原則：
1. 嚴格依據真實台股微結構進行模擬撮合。
2. 漲停鎖死買單不撮合、跌停鎖死賣單不撮合、停牌股票不撮合。
3. 處置股票與市場流動性上限（Volume Participation Cap）引導部分成交 (Partial Fill)。
4. 手續費折讓（最低 20 元）與證券交易稅（股票 0.3% / ETF 0.1%）精確取整。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
from typing import Optional

from .config import TaiwanPaperConfig
from .order import FillRecord, PaperOrder
from ..contracts.signal import SignalAction, SignalOrderType


@dataclass(frozen=True)
class MarketBar:
    """單一標的之時序行情 K 線與盤態資訊。"""
    symbol: str
    timestamp: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int                                # 成交股數
    yesterday_close: Decimal                  # 昨收價 (供計算漲跌停)
    is_halted: bool = False                   # 是否暫停交易 (停牌)
    is_disposition: bool = False              # 是否為處置股票
    is_limit_up_locked: bool = False          # 是否漲停鎖死無賣單
    is_limit_down_locked: bool = False        # 是否跌停鎖死無買單


class FillEngine:
    """台股模擬撮合引擎。"""

    def __init__(self, config: TaiwanPaperConfig) -> None:
        self.config = config
        self._fill_seq: int = 0

    def generate_fill_id(self) -> str:
        self._fill_seq += 1
        return f"FILL-{self._fill_seq:08d}"

    def calculate_commission(self, notional: Decimal) -> Decimal:
        """計算券商電子下單手續費 (法定 0.1425% * 折讓，四捨五入，低於最低手續費以最低手續費計)."""
        raw_fee = notional * self.config.commission_rate * self.config.commission_discount
        rounded_fee = raw_fee.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return max(self.config.min_commission, rounded_fee)

    def calculate_tax(
        self,
        symbol: str,
        notional: Decimal,
        is_day_trading: bool = False,
    ) -> Decimal:
        """計算賣出證券交易稅 (股票 0.3%, ETF 0.1%, 當沖 0.15%, 法定無條件捨去取整)."""
        # 台股 ETF 代號特徵：00 開頭 (如 0050, 0056)
        clean_sym = symbol.split(".")[0].strip()
        is_etf = clean_sym.startswith("00")

        if is_day_trading:
            tax_rate = self.config.day_trading_tax_rate
        elif is_etf:
            tax_rate = self.config.etf_tax_rate
        else:
            tax_rate = self.config.stock_tax_rate

        raw_tax = notional * tax_rate
        return raw_tax.quantize(Decimal("1"), rounding=ROUND_FLOOR)

    def match_order(
        self,
        order: PaperOrder,
        bar: MarketBar,
        is_day_trading: bool = False,
    ) -> Optional[FillRecord]:
        """依當下行情撮合訂單，回傳撮合成交紀錄；若無法成交則回傳 None。

        Raises:
            ValueError: 若訂單已處於終態或時序違規。
        """
        if order.is_terminal:
            raise ValueError(f"訂單 {order.order_id} 已處於終態 {order.status.value}，嚴禁再次撮合")

        # 1. 標的與時間一致性檢查
        if order.symbol.split(".")[0] != bar.symbol.split(".")[0]:
            return None  # 非此標的行情

        # 2. 停牌股票直接禁止成交
        if bar.is_halted or bar.volume <= 0:
            return None

        # 3. 漲跌停價格計算與防護
        limit_up = (bar.yesterday_close * (Decimal("1") + self.config.price_limit_rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        limit_down = (bar.yesterday_close * (Decimal("1") - self.config.price_limit_rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # 漲停買進阻擋：若漲停鎖死或最高價抵達漲停且標記鎖死，買單排隊無法撮合
        if order.action == SignalAction.BUY:
            if bar.is_limit_up_locked or (bar.close >= limit_up and bar.is_limit_up_locked):
                return None

        # 跌停賣出阻擋：若跌停鎖死或最低價抵達跌停且標記鎖死，賣單排隊無法撮合
        if order.action in {SignalAction.SELL, SignalAction.CLOSE}:
            if bar.is_limit_down_locked or (bar.close <= limit_down and bar.is_limit_down_locked):
                return None

        # 4. 價格撮合與滑價模擬
        ref_price = bar.close
        if order.order_type == SignalOrderType.MARKET:
            # 市價單：加減滑價
            if order.action == SignalAction.BUY:
                exec_price = (ref_price * (Decimal("1") + self.config.slippage_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                exec_price = min(limit_up, exec_price)  # 不得超出漲停
            else:
                exec_price = (ref_price * (Decimal("1") - self.config.slippage_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                exec_price = max(limit_down, exec_price)  # 不得低於跌停
        else:
            # 限價單 (LIMIT)
            limit = order.limit_price
            assert limit is not None
            if order.action == SignalAction.BUY:
                # 買進限價必須 >= 最低價方有機會撮合
                if limit < bar.low:
                    return None
                # 成交價取現價加滑價，但不超過限價與漲停價
                slipped = (ref_price * (Decimal("1") + self.config.slippage_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                exec_price = min(limit, slipped, limit_up)
            else:
                # 賣出限價必須 <= 最高價方有機會撮合
                if limit > bar.high:
                    return None
                slipped = (ref_price * (Decimal("1") - self.config.slippage_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                exec_price = max(limit, slipped, limit_down)

        # 5. 成交量限制與部分成交 (Volume Participation Cap)
        # 單次最大可撮合股數
        volume_cap = int(Decimal(bar.volume) * self.config.max_volume_participation)
        # 處置股票限制量額外減半
        if bar.is_disposition:
            volume_cap = max(1, volume_cap // 2)

        if volume_cap <= 0:
            return None

        fill_qty = min(order.remaining_quantity, volume_cap)
        if fill_qty <= 0:
            return None

        # 6. 精算各項費用 (全 Decimal)
        notional = (exec_price * Decimal(fill_qty)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        commission = self.calculate_commission(notional)
        tax = (
            self.calculate_tax(order.symbol, notional, is_day_trading)
            if order.action in {SignalAction.SELL, SignalAction.CLOSE}
            else Decimal("0.00")
        )
        slippage_cost = (abs(exec_price - ref_price) * Decimal(fill_qty)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        return FillRecord(
            fill_id=self.generate_fill_id(),
            order_id=order.order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            fill_quantity=fill_qty,
            fill_price=exec_price,
            notional=notional,
            commission=commission,
            tax=tax,
            slippage=slippage_cost,
            timestamp=bar.timestamp,
        )
