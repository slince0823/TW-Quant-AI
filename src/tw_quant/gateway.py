"""台股量化交易執行閘道 (gateway.py).

整合 UnifiedTradeSignal、RiskEngine 與 TaiwanPaperBroker：
1. 嚴格強制所有交易訊號必須 100% 通過風控閘門後方能抵達 PaperBroker。
2. 自動維護原子性在途承諾資金與部位，杜絕高頻並發突破現金或持倉限制。
3. 嚴密監控訂單生命週期、成交狀態與帳本全局恆等式。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from .broker.broker import TaiwanPaperBroker
from .broker.engine import MarketBar
from .broker.ledger import DailyNAV
from .broker.order import FillRecord, PaperOrder, PaperOrderStatus
from .contracts.conversion import lots_to_shares
from .contracts.signal import QuantityUnit, SignalAction, UnifiedTradeSignal
from .risk.decision import RiskDecision
from .risk.engine import RiskEngine


class TradingGateway:
    """統一交易執行閘道 (TradingGateway)."""

    def __init__(
        self,
        broker: TaiwanPaperBroker,
        risk_engine: RiskEngine,
    ) -> None:
        self.broker = broker
        self.risk_engine = risk_engine

        # 同步掛載至 broker 進行雙重防禦 (Defense-in-Depth)
        self.broker.risk_engine = risk_engine

    def submit_signal(
        self,
        signal: UnifiedTradeSignal,
        ref_price: Optional[Decimal] = None,
        market_prices: Optional[Dict[str, Decimal]] = None,
        halted_symbols: Optional[Set[str]] = None,
        disposition_symbols: Optional[Set[str]] = None,
    ) -> Tuple[bool, RiskDecision, Optional[PaperOrder]]:
        """安全通道：審核訊號並轉發至模擬券商。

        Returns:
            Tuple[bool, RiskDecision, Optional[PaperOrder]]:
                (是否成功立單, 風控決策物件, 建立之訂單物件)
        """
        # 1. 訊號基礎合法性防護
        signal.validate()

        # 2. 自動由 Broker 當前狀態建立一致性快照
        ctx = self.broker.build_risk_context(
            current_time=signal.created_at,
            market_prices=market_prices,
            halted_symbols=halted_symbols,
            disposition_symbols=disposition_symbols,
        )

        # 3. 執行 10 道安全防線檢核
        decision = self.risk_engine.evaluate(signal, ctx)

        if not decision.allowed:
            # 拒絕：絕不發送有效委託至券商建立開立中訂單
            return False, decision, None

        # 4. 風控核准：原子鎖定在途資金 (Pre-commitment)
        req_cash = Decimal("0.00")
        est_shares = (
            lots_to_shares(signal.quantity)
            if signal.quantity_unit == QuantityUnit.LOTS
            else int(signal.quantity)
        )

        if signal.action == SignalAction.BUY:
            limit_dec = (
                Decimal(str(signal.limit_price))
                if signal.limit_price is not None
                else (ref_price or Decimal("0.00"))
            )
            slipped = limit_dec * (Decimal("1") + self.broker.config.slippage_rate)
            notional = slipped * Decimal(est_shares)
            fee = self.broker.fill_engine.calculate_commission(notional)
            req_cash = notional + fee
            self.risk_engine.commit_approval(signal, required_cash=req_cash, shares=est_shares)

        # 5. 送交券商建單並鎖定款券
        try:
            order = self.broker.submit_signal(signal, ref_price=ref_price, market_prices=market_prices)
        finally:
            # 6. 券商已由其 ledger 完成款券預留，釋放風控在途暫存資金
            if signal.action == SignalAction.BUY:
                self.risk_engine.release_committed(signal, required_cash=req_cash, shares=est_shares)

        is_success = order.status == PaperOrderStatus.ACCEPTED
        return is_success, decision, order

    def process_bar(self, bar: MarketBar) -> List[FillRecord]:
        """驅動撮合行情。"""
        return self.broker.process_bar(bar)

    def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """撤銷開立中訂單。"""
        return self.broker.cancel_order(order_id, reason=reason)

    def process_daily_close(
        self,
        as_of_date: dt.date,
        close_prices: Dict[str, Decimal],
    ) -> DailyNAV:
        """收盤結算。"""
        return self.broker.process_daily_close(as_of_date, close_prices)
