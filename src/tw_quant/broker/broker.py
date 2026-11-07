"""台股高擬真模擬券商核心 (broker.py).

整合 OrderManager、FillEngine、PortfolioLedger 與 TaiwanPaperConfig，
提供完整生命週期之台股 Paper Trading 撮合、預留款券、狀態機轉換與會計結算。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Set

from .config import TaiwanPaperConfig
from .engine import FillEngine, MarketBar
from .ledger import DailyNAV, PortfolioLedger, Position
from .order import FillRecord, OrderManager, PaperOrder, PaperOrderStatus
from ..contracts.conversion import lots_to_shares
from ..contracts.dedup import DuplicateSignalError, SignalDeduplicator
from ..contracts.signal import QuantityUnit, SignalAction, SignalOrderType, UnifiedTradeSignal


class TaiwanPaperBroker:
    """台股模擬券商交易引擎 (TaiwanPaperBroker)."""

    def __init__(
        self,
        config: Optional[TaiwanPaperConfig] = None,
        risk_engine: Optional[Any] = None,
    ) -> None:
        self.config = config or TaiwanPaperConfig()
        self.risk_engine = risk_engine
        self.order_manager = OrderManager()
        self.fill_engine = FillEngine(self.config)
        self.ledger = PortfolioLedger(self.config.initial_cash)

        # 永久記錄已處理的 signal_id，保證當前生命週期內不重複生成訂單
        self._processed_signals: Set[str] = set()
        self._signal_dedup = SignalDeduplicator(max_size=200_000)
        self.all_fills: List[FillRecord] = []

    def build_risk_context(
        self,
        current_time: dt.datetime,
        market_prices: Optional[Dict[str, Decimal]] = None,
        halted_symbols: Optional[Set[str]] = None,
        disposition_symbols: Optional[Set[str]] = None,
        baseline_nav: Optional[Decimal] = None,
    ) -> Any:
        """依當前模擬券商帳本、未成交委託與市場報價建立不可變的 RiskContext 快照。"""
        from ..risk.context import RiskContext

        cash = self.ledger.cash
        reserved_cash = self.ledger.reserved_cash
        positions = {sym: pos.quantity for sym, pos in self.ledger.positions.items()}
        reserved_shares = {sym: qty for sym, qty in self.ledger.reserved_shares.items()}

        pending_buy_notionals: Dict[str, Decimal] = {}
        for o in self.order_manager.get_open_orders():
            if o.action == SignalAction.BUY and o.reserved_cash > Decimal("0.00"):
                pending_buy_notionals[o.symbol] = (
                    pending_buy_notionals.get(o.symbol, Decimal("0.00")) + o.reserved_cash
                )

        prices = dict(market_prices or {})
        for sym, pos in self.ledger.positions.items():
            if sym not in prices and pos.avg_price > Decimal("0"):
                prices[sym] = pos.avg_price

        if baseline_nav is not None:
            b_nav = baseline_nav
        elif self.ledger.nav_history:
            b_nav = self.ledger.nav_history[-1].total_equity
        else:
            b_nav = self.ledger.initial_cash

        peak_nav = self.ledger.peak_equity

        daily_realized_pnl = self.ledger.cumulative_realized_pnl
        if self.ledger.nav_history:
            daily_realized_pnl -= self.ledger.nav_history[-1].realized_pnl_cum

        return RiskContext(
            current_time=current_time,
            cash=cash,
            reserved_cash=reserved_cash,
            positions=positions,
            reserved_shares=reserved_shares,
            pending_buy_notionals=pending_buy_notionals,
            market_prices=prices,
            halted_symbols=set(halted_symbols or ()),
            disposition_symbols=set(disposition_symbols or ()),
            baseline_nav=b_nav,
            daily_realized_pnl=daily_realized_pnl,
            daily_unrealized_pnl=Decimal("0.00"),
            peak_nav=peak_nav,
        )

    # ── 訊號接收與委託建立 ──────────────────────────────────────

    def submit_signal(
        self,
        signal: UnifiedTradeSignal,
        ref_price: Optional[Decimal] = None,
        market_prices: Optional[Dict[str, Decimal]] = None,
    ) -> PaperOrder:
        """接收 UnifiedTradeSignal 轉化為模擬委託單，執行資金或股票預留並進入狀態機。

        Raises:
            DuplicateSignalError: 當 signal_id 已被處理過時。
            ValueError: 規格違規或零股限制。
        """
        # 1. 重複訊號檢驗 (永久集合 + 緩存檢驗)
        if signal.signal_id in self._processed_signals:
            raise DuplicateSignalError(f"訊號已處理過，拒絕重複下單: {signal.signal_id}")

        self._signal_dedup.register(signal.signal_id)
        self._processed_signals.add(signal.signal_id)

        # 2. 數量單位標準化為整數「股 (SHARES)」
        if signal.quantity_unit == QuantityUnit.LOTS:
            shares = lots_to_shares(signal.quantity)
        else:
            shares = int(signal.quantity)

        limit_dec = (
            Decimal(str(signal.limit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if signal.limit_price is not None
            else None
        )

        # 3. 風控前置審核閘門 (若有注入 RiskEngine 且該訊號尚未由外部閘道審核過)
        if self.risk_engine is not None and signal.signal_id not in self.risk_engine._approved_signals:
            ctx = self.build_risk_context(
                current_time=signal.created_at,
                market_prices=market_prices,
            )
            decision = self.risk_engine.evaluate(signal, ctx)
            if not decision.allowed:
                order = PaperOrder(
                    order_id=self.order_manager.generate_order_id(),
                    signal_id=signal.signal_id,
                    symbol=signal.symbol,
                    action=signal.action,
                    order_type=signal.order_type,
                    order_quantity=shares,
                    limit_price=limit_dec,
                    created_at=signal.created_at,
                )
                order.reject(f"風控拒絕: {', '.join(decision.reason_codes)}")
                self.order_manager.register_order(order)
                return order

        # 4. 零股檢查
        if not self.config.allow_odd_lots and (shares % 1000 != 0):
            order = PaperOrder(
                order_id=self.order_manager.generate_order_id(),
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                action=signal.action,
                order_type=signal.order_type,
                order_quantity=shares,
                limit_price=limit_dec,
                created_at=signal.created_at,
            )
            order.reject("系統設定不允許零股委託")
            self.order_manager.register_order(order)
            return order

        order = PaperOrder(
            order_id=self.order_manager.generate_order_id(),
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            action=signal.action,
            order_type=signal.order_type,
            order_quantity=shares,
            limit_price=limit_dec,
            created_at=signal.created_at,
        )

        # 5. 預留款券與資金完整性防護 (Hold Funds / Hold Shares)
        if order.action == SignalAction.BUY:
            # 估算預留資金
            est_price = limit_dec or ref_price
            if est_price is None or est_price <= Decimal("0"):
                order.reject("市價買單必須提供有效參考價 ref_price 以計算預留資金")
                self.order_manager.register_order(order)
                return order

            # 加上滑價與手續費預估
            est_notional = (est_price * (Decimal("1") + self.config.slippage_rate) * Decimal(shares)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            est_fee = self.fill_engine.calculate_commission(est_notional)
            required_cash = est_notional + est_fee

            try:
                self.ledger.reserve_buy_funds(required_cash)
                order.accept(reserved_cash=required_cash, reserved_shares=0)
            except ValueError as e:
                order.reject(f"買入資金預留失敗: {e}")

        elif order.action in {SignalAction.SELL, SignalAction.CLOSE}:
            # 預留持倉部位
            try:
                self.ledger.reserve_sell_shares(order.symbol, shares)
                order.accept(reserved_cash=Decimal("0.00"), reserved_shares=shares)
            except ValueError as e:
                order.reject(f"賣出持倉預留失敗: {e}")

        self.order_manager.register_order(order)
        return order

    # ── 行情驅動與模擬撮合 ──────────────────────────────────────

    def process_bar(self, bar: MarketBar) -> List[FillRecord]:
        """以單一 Bar 行情撮合所有對應標的之開立中訂單。

        Returns:
            本輪撮合產生的所有成交紀錄清單。
        """
        active_orders = [
            o for o in self.order_manager.get_open_orders()
            if o.symbol.split(".")[0] == bar.symbol.split(".")[0]
        ]
        fills_this_bar: List[FillRecord] = []

        for order in active_orders:
            # 時序因果檢查：撮合行情時間不得早於委託建立時間
            if bar.timestamp < order.created_at:
                continue

            fill = self.fill_engine.match_order(order, bar)
            if fill is None:
                continue

            # 推進訂單狀態
            order.add_fill(fill)
            self.all_fills.append(fill)
            fills_this_bar.append(fill)

            # 更新帳本
            if order.action == SignalAction.BUY:
                # 依成交比例計算扣減預留款
                prop = Decimal(fill.fill_quantity) / Decimal(order.order_quantity)
                reserved_deduct = (order.reserved_cash * prop).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                self.ledger.record_buy_fill(
                    symbol=order.symbol,
                    fill_shares=fill.fill_quantity,
                    fill_price=fill.fill_price,
                    notional=fill.notional,
                    fee=fill.commission,
                    reserved_cash_deduct=reserved_deduct,
                )
            elif order.action in {SignalAction.SELL, SignalAction.CLOSE}:
                self.ledger.record_sell_fill(
                    symbol=order.symbol,
                    fill_shares=fill.fill_quantity,
                    fill_price=fill.fill_price,
                    notional=fill.notional,
                    fee=fill.commission,
                    tax=fill.tax,
                    reserved_shares_deduct=fill.fill_quantity,
                )

        return fills_this_bar

    # ── 委託取消與每日收盤結算 ──────────────────────────────────

    def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """撤銷開立中訂單，並返還未成交數量所對應的預留款券。"""
        order = self.order_manager.get_order(order_id)
        if order is None or not order.is_active:
            return False

        # 計算剩餘未成交比例並退還預留
        remaining_ratio = Decimal(order.remaining_quantity) / Decimal(order.order_quantity)

        if order.action == SignalAction.BUY:
            refund_cash = (order.reserved_cash * remaining_ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            self.ledger.release_buy_funds(refund_cash)
        elif order.action in {SignalAction.SELL, SignalAction.CLOSE}:
            refund_shares = int(Decimal(order.reserved_shares) * remaining_ratio)
            self.ledger.release_sell_shares(order.symbol, refund_shares)

        order.cancel(reason or "手動取消")
        return True

    def process_daily_close(
        self,
        as_of_date: dt.date,
        close_prices: Dict[str, Decimal],
    ) -> DailyNAV:
        """收盤作業：將所有未成交訂單過期作廢（釋放預留款券），並以收盤價結算當日資產淨值。"""
        # 1. 過期所有開立中委託單
        for order in self.order_manager.get_open_orders():
            remaining_ratio = Decimal(order.remaining_quantity) / Decimal(order.order_quantity)
            if order.action == SignalAction.BUY:
                refund_cash = (order.reserved_cash * remaining_ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                self.ledger.release_buy_funds(refund_cash)
            elif order.action in {SignalAction.SELL, SignalAction.CLOSE}:
                refund_shares = int(Decimal(order.reserved_shares) * remaining_ratio)
                self.ledger.release_sell_shares(order.symbol, refund_shares)
            order.expire("當日收盤過期自動作廢")

        # 2. 逐日收盤估值
        return self.ledger.mark_to_market(close_prices, as_of_date)

    # ── 查詢 API ───────────────────────────────────────────────

    def get_cash(self) -> Decimal:
        """取得目前可用現金。"""
        return self.ledger.cash

    def get_reserved_cash(self) -> Decimal:
        """取得目前凍結預留現金。"""
        return self.ledger.reserved_cash

    def get_positions(self) -> Dict[str, Position]:
        """取得目前持倉部位。"""
        return self.ledger.positions

    def get_total_equity(self, current_prices: Optional[Dict[str, Decimal]] = None) -> Decimal:
        """取得目前投資組合總權益 (現金 + 預留現金 + 持股市值)."""
        mkt_val = Decimal("0.00")
        if current_prices:
            for sym, pos in self.ledger.positions.items():
                px = current_prices.get(sym, pos.avg_price)
                mkt_val += (px * Decimal(pos.quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return self.ledger.cash + self.ledger.reserved_cash + mkt_val

    def get_realized_pnl(self) -> Decimal:
        """取得累計已實現損益。"""
        return self.ledger.cumulative_realized_pnl

    def get_nav_history(self) -> List[DailyNAV]:
        """取得每日資產淨值歷史走勢。"""
        return self.ledger.nav_history
