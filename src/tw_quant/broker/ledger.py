"""台股投資組合會計與帳本管理 (ledger.py).

核心原則：
1. 全程採用 Decimal 精確運算，杜絕任何浮點數累加誤差。
2. 嚴格區分可用現金 (Cash) 與已預留現金 (Reserved Cash)。
3. 嚴格區分可用持倉 (Available Shares) 與已預留持倉 (Reserved Shares)。
4. 採用一致的「加權平均成本法」計算已實現與未實現損益。
5. 逐日結算 NAV，標的估值行情缺失時嚴禁靜默當成 0。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional


@dataclass
class Position:
    """單一標的持倉紀錄。"""
    symbol: str
    quantity: int = 0
    avg_price: Decimal = Decimal("0.00")      # 每股加權平均買入成本 (含手續費)
    total_cost: Decimal = Decimal("0.00")     # 累計總買入成本

    def add_shares(self, shares: int, notional: Decimal, fee: Decimal) -> None:
        """同向買入加碼，計算加權平均成本。"""
        if shares <= 0:
            raise ValueError(f"買入股數必須大於 0，收到 {shares}")
        cost_addition = notional + fee
        new_quantity = self.quantity + shares
        new_total_cost = self.total_cost + cost_addition
        self.quantity = new_quantity
        self.total_cost = new_total_cost
        self.avg_price = (new_total_cost / Decimal(new_quantity)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )

    def reduce_shares(self, shares: int) -> Decimal:
        """賣出減碼或完全平倉，持倉均價維持不變，回傳扣減之原成本基準。"""
        if shares <= 0 or shares > self.quantity:
            raise ValueError(
                f"賣出股數 ({shares}) 必須大於 0 且不高於現有持倉 ({self.quantity})"
            )
        cost_basis_reduced = (self.avg_price * Decimal(shares)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        self.quantity -= shares
        self.total_cost -= cost_basis_reduced
        if self.quantity == 0:
            self.avg_price = Decimal("0.00")
            self.total_cost = Decimal("0.00")
        return cost_basis_reduced


@dataclass(frozen=True)
class DailyNAV:
    """每日資產淨值結算紀錄。"""
    date: dt.date
    cash: Decimal
    reserved_cash: Decimal
    market_value: Decimal
    total_equity: Decimal
    realized_pnl_cum: Decimal
    unrealized_pnl: Decimal
    peak_equity: Decimal
    drawdown: Decimal


class PortfolioLedger:
    """投資組合完整會計帳本。"""

    def __init__(self, initial_cash: Decimal):
        if not isinstance(initial_cash, Decimal) or initial_cash <= Decimal("0"):
            raise ValueError("初始資金必須為大於 0 的 Decimal")
        self.initial_cash: Decimal = initial_cash
        self.cash: Decimal = initial_cash                     # 目前可用現金
        self.reserved_cash: Decimal = Decimal("0.00")         # 買入委託已凍結預留現金
        self.positions: Dict[str, Position] = {}              # 各股票持倉部位
        self.reserved_shares: Dict[str, int] = {}             # 各股票已凍結預留賣出股數
        self.cumulative_realized_pnl: Decimal = Decimal("0.00")  # 累計已實現損益
        self.nav_history: List[DailyNAV] = []                 # 每日結算歷史
        self.peak_equity: Decimal = initial_cash              # 歷史最高淨值
        self.max_drawdown: Decimal = Decimal("0.00")          # 歷史最大回撤

    # ── 資金與持倉預留機制 (Hold / Release) ────────────────────

    def reserve_buy_funds(self, amount: Decimal) -> None:
        """凍結預留買入資金。"""
        if amount <= Decimal("0"):
            raise ValueError(f"預留資金必須大於 0，收到 {amount}")
        if self.cash < amount:
            raise ValueError(f"可用現金不足以預留: 需 {amount}，目前可用 {self.cash}")
        self.cash -= amount
        self.reserved_cash += amount

    def release_buy_funds(self, amount: Decimal) -> None:
        """釋放買入預留資金 (委託取消、失敗或成交差額返還)。"""
        if amount <= Decimal("0"):
            return
        if self.reserved_cash < amount:
            amount = self.reserved_cash
        self.reserved_cash -= amount
        self.cash += amount

    def reserve_sell_shares(self, symbol: str, shares: int) -> None:
        """凍結預留賣出持倉。"""
        if shares <= 0:
            raise ValueError(f"預留股數必須大於 0，收到 {shares}")
        held = self.positions.get(symbol)
        held_qty = held.quantity if held else 0
        current_res = self.reserved_shares.get(symbol, 0)
        available = held_qty - current_res
        if available < shares:
            raise ValueError(
                f"標的 {symbol} 可用持倉不足: 需賣出 {shares} 股，目前可用僅 {available} 股 (總持倉 {held_qty} 股，已預留 {current_res} 股)"
            )
        self.reserved_shares[symbol] = current_res + shares

    def release_sell_shares(self, symbol: str, shares: int) -> None:
        """釋放賣出預留持倉 (委託取消或失敗返還)。"""
        if shares <= 0:
            return
        current_res = self.reserved_shares.get(symbol, 0)
        new_res = max(0, current_res - shares)
        self.reserved_shares[symbol] = new_res

    def get_available_shares(self, symbol: str) -> int:
        """查詢標的目前尚未預留的可用股數。"""
        held = self.positions.get(symbol)
        held_qty = held.quantity if held else 0
        res = self.reserved_shares.get(symbol, 0)
        return max(0, held_qty - res)

    # ── 成交記帳處理 ──────────────────────────────────────────

    def record_buy_fill(
        self,
        symbol: str,
        fill_shares: int,
        fill_price: Decimal,
        notional: Decimal,
        fee: Decimal,
        reserved_cash_deduct: Decimal,
    ) -> None:
        """買入成交記帳：從預留資金扣減實際款項，更新持倉與加權平均成本。"""
        actual_total_cost = notional + fee

        # 優先從 reserved_cash 扣除；若有多退少補則精準結算
        if self.reserved_cash >= reserved_cash_deduct:
            self.reserved_cash -= reserved_cash_deduct
            surplus = reserved_cash_deduct - actual_total_cost
            self.cash += surplus  # 差額返還可用現金
        else:
            # 無預留直接成交情境
            self.cash -= actual_total_cost

        # 更新部位
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        self.positions[symbol].add_shares(fill_shares, notional, fee)

    def record_sell_fill(
        self,
        symbol: str,
        fill_shares: int,
        fill_price: Decimal,
        notional: Decimal,
        fee: Decimal,
        tax: Decimal,
        reserved_shares_deduct: int,
    ) -> Decimal:
        """賣出成交記帳：扣除部位、回收現金淨額、精算已實現損益。

        Returns:
            本筆成交之已實現損益 (Realized PnL).
        """
        if symbol not in self.positions or self.positions[symbol].quantity < fill_shares:
            raise ValueError(f"持倉不足以賣出成交 {fill_shares} 股")

        # 解除預留並扣減部位
        current_res = self.reserved_shares.get(symbol, 0)
        self.reserved_shares[symbol] = max(0, current_res - reserved_shares_deduct)
        cost_basis_reduced = self.positions[symbol].reduce_shares(fill_shares)

        # 淨回收金額 = 成交金額 - 手續費 - 證交稅
        net_proceeds = notional - fee - tax
        self.cash += net_proceeds

        # 本筆已實現損益 = 淨回收金額 - 原持有成本
        trade_realized_pnl = net_proceeds - cost_basis_reduced
        self.cumulative_realized_pnl += trade_realized_pnl

        # 部位歸零清理
        if self.positions[symbol].quantity == 0:
            del self.positions[symbol]

        return trade_realized_pnl

    # ── 逐日收盤估值 (Daily Mark-to-Market) ─────────────────────

    def mark_to_market(
        self,
        close_prices: Dict[str, Decimal],
        as_of_date: dt.date,
    ) -> DailyNAV:
        """以當日收盤價進行資產淨值結算，嚴禁將缺失報價靜默當成 0。

        Raises:
            ValueError: 若有持倉股票但在 close_prices 中缺少有效報價。
        """
        market_value = Decimal("0.00")
        unrealized_pnl = Decimal("0.00")

        for sym, pos in self.positions.items():
            if pos.quantity > 0:
                if sym not in close_prices or close_prices[sym] is None or close_prices[sym] <= Decimal("0"):
                    raise ValueError(
                        f"結算日期 {as_of_date} 缺少標的 {sym} 之有效收盤估值報價，禁止靜默記為 0"
                    )
                px = close_prices[sym]
                val = (px * Decimal(pos.quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                market_value += val
                unrealized_pnl += (val - pos.total_cost)

        total_equity = self.cash + self.reserved_cash + market_value

        # 更新最高淨值與回撤
        if total_equity > self.peak_equity:
            self.peak_equity = total_equity
        drawdown = Decimal("0.00")
        if self.peak_equity > Decimal("0"):
            drawdown = ((self.peak_equity - total_equity) / self.peak_equity).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        nav_record = DailyNAV(
            date=as_of_date,
            cash=self.cash,
            reserved_cash=self.reserved_cash,
            market_value=market_value,
            total_equity=total_equity,
            realized_pnl_cum=self.cumulative_realized_pnl,
            unrealized_pnl=unrealized_pnl,
            peak_equity=self.peak_equity,
            drawdown=drawdown,
        )
        self.nav_history.append(nav_record)
        return nav_record
