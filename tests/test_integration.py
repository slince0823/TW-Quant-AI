"""端到端整合測試與交易安全驗收套件 (test_integration.py).

涵蓋 8 大整合情境：
A. 正常交易流程 (Normal Trading Flow)
B. 風控拒絕流程 (Risk Rejection Flow)
C. 資金與持倉預留 (Cash & Position Reservation)
D. 重複訊號防禦 (Duplicate Signal Protection)
E. 熔斷與 Kill Switch (Circuit Breaker & Kill Switch)
F. 交易時序與市場限制 (Execution Timing & Market Constraints)
G. 帳務一致性檢驗 (Ledger Invariant Assertions)
H. 例外與恢復 (Exceptions & Recovery)
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Set
import unittest

from src.tw_quant.broker.broker import TaiwanPaperBroker
from src.tw_quant.broker.config import TaiwanPaperConfig
from src.tw_quant.broker.engine import MarketBar
from src.tw_quant.broker.order import PaperOrderStatus
from src.tw_quant.contracts.dedup import DuplicateSignalError
from src.tw_quant.contracts.signal import (
    QuantityUnit,
    SignalAction,
    SignalOrderType,
    UnifiedTradeSignal,
)
from src.tw_quant.gateway import TradingGateway
from src.tw_quant.risk.config import RiskConfig
from src.tw_quant.risk.context import RiskContext
from src.tw_quant.risk.decision import RiskRejectCode
from src.tw_quant.risk.engine import RiskEngine


class TestIntegrationTradingSafety(unittest.TestCase):
    """端到端整合與交易安全驗收測試類別。"""

    def setUp(self) -> None:
        self.tz = dt.timezone(dt.timedelta(hours=8))
        self.trade_date = dt.date(2026, 10, 20)
        self.t_open = dt.datetime(2026, 10, 20, 9, 0, 0, tzinfo=self.tz)
        self.t_mid = dt.datetime(2026, 10, 20, 10, 0, 0, tzinfo=self.tz)
        self.t_close = dt.datetime(2026, 10, 20, 13, 30, 0, tzinfo=self.tz)
        self.t_expire = dt.datetime(2026, 10, 20, 13, 35, 0, tzinfo=self.tz)

        # 初始資金 2,000,000 元
        self.initial_cash = Decimal("2000000.00")
        self.broker_config = TaiwanPaperConfig(
            initial_cash=self.initial_cash,
            slippage_rate=Decimal("0.0005"),          # 0.05%
            max_volume_participation=Decimal("0.10"),  # 10%
        )
        self.risk_config = RiskConfig(
            max_order_amount=Decimal("500000.00"),     # 50 萬單筆上限
            max_single_position_ratio=Decimal("0.10"), # 10% NAV
            max_total_exposure_ratio=Decimal("0.80"),  # 80% NAV
            min_cash_reserve_ratio=Decimal("0.20"),    # 20% NAV (40 萬保留)
            max_daily_loss_ratio=Decimal("0.02"),      # 2% NAV
            max_drawdown_limit=Decimal("0.15"),        # 15% 最大回撤
            restricted_symbols={"9999"},
        )

        self.broker = TaiwanPaperBroker(self.broker_config)
        self.risk_engine = RiskEngine(self.risk_config)
        self.gateway = TradingGateway(self.broker, self.risk_engine)

    def _create_signal(
        self,
        signal_id: str,
        symbol: str = "2317",
        action: SignalAction = SignalAction.BUY,
        quantity: int = 1000,
        limit_price: Optional[float] = 100.0,
        order_type: SignalOrderType = SignalOrderType.LIMIT,
        created_at: Optional[dt.datetime] = None,
        expires_at: Optional[dt.datetime] = None,
        reason: str = "Integration Test Signal",
    ) -> UnifiedTradeSignal:
        c_at = created_at or self.t_open
        e_at = expires_at or self.t_expire
        return UnifiedTradeSignal(
            signal_id=signal_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            quantity_unit=QuantityUnit.SHARES,
            order_type=order_type,
            limit_price=limit_price,
            created_at=c_at,
            expires_at=e_at,
            strategy_id="strat_integration",
            model_version="v1.0",
            reason=reason,
        )

    def _create_bar(
        self,
        symbol: str,
        timestamp: dt.datetime,
        price: float = 100.0,
        volume: int = 50000,
        is_halted: bool = False,
        is_limit_up: bool = False,
        is_limit_down: bool = False,
        is_disposition: bool = False,
    ) -> MarketBar:
        px = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return MarketBar(
            symbol=symbol,
            timestamp=timestamp,
            open=px,
            high=px,
            low=px,
            close=px,
            volume=volume,
            yesterday_close=px,
            is_halted=is_halted,
            is_disposition=is_disposition,
            is_limit_up_locked=is_limit_up,
            is_limit_down_locked=is_limit_down,
        )

    def _assert_ledger_invariants(
        self,
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> None:
        """全局帳本恆等式與完整性檢驗。"""
        ledger = self.broker.ledger
        # 1. 恆等式 1：總權益 == 可用現金 + 預留現金 + 股票持股市值
        mkt_val = Decimal("0.00")
        if current_prices:
            for sym, pos in ledger.positions.items():
                px = current_prices.get(sym, pos.avg_price)
                mkt_val += (px * Decimal(pos.quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        expected_equity = ledger.cash + ledger.reserved_cash + mkt_val
        actual_equity = self.broker.get_total_equity(current_prices)
        self.assertEqual(expected_equity, actual_equity)

        # 2. 恆等式 2：各標的總股數 == 可用股數 + 預留賣出股數
        for sym, pos in ledger.positions.items():
            avail = ledger.get_available_shares(sym)
            res = ledger.reserved_shares.get(sym, 0)
            self.assertEqual(pos.quantity, avail + res)
            self.assertGreaterEqual(avail, 0)
            self.assertGreaterEqual(res, 0)

        # 3. 恆等式 3：所有開立中訂單，order_quantity == filled_quantity + remaining_quantity
        for order in self.broker.order_manager.get_open_orders():
            self.assertEqual(order.order_quantity, order.filled_quantity + order.remaining_quantity)

        # 4. 數值型別檢驗：嚴格禁止 float 進入帳本金額
        self.assertIsInstance(ledger.cash, Decimal)
        self.assertIsInstance(ledger.reserved_cash, Decimal)
        self.assertGreaterEqual(ledger.cash, Decimal("0.00"))
        self.assertGreaterEqual(ledger.reserved_cash, Decimal("0.00"))

    # ══════════════════════════════════════════════════════════════
    # A. 正常交易流程 (Normal Trading Flow)
    # ══════════════════════════════════════════════════════════════

    def test_A01_normal_trading_flow_end_to_end(self) -> None:
        """驗證從訊號建立、驗證、風控核准、預留資金、分批撮合成交至收盤 NAV 之完整流程。"""
        # 1. 建立合法 BUY 訊號 (1000 股 2317 @ 100 元 = 10 萬元)
        sig = self._create_signal("SIG-A01", symbol="2317", quantity=1000, limit_price=100.0)

        # 2. 訊號驗證通過
        sig.validate()
        self.assertFalse(sig.is_expired(as_of=self.t_open))

        # 3. 透過 Gateway 提交訊號 (風控核准 + 資金預留 + 券商接單)
        prices = {"2317": Decimal("100.00")}
        ok, decision, order = self.gateway.submit_signal(sig, market_prices=prices)

        self.assertTrue(ok)
        self.assertTrue(decision.allowed)
        self.assertIsNotNone(order)
        assert order is not None

        # 4. 驗證資金預留成功
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)
        self.assertGreater(self.broker.get_reserved_cash(), Decimal("100000.00"))
        self.assertEqual(self.broker.get_cash(), self.initial_cash - self.broker.get_reserved_cash())
        self._assert_ledger_invariants(prices)

        # 5. 模擬第一筆行情：撮合成交 500 股 (部分成交)
        # 設定 volume=5000，10% participation cap 恰撮合 500 股
        bar1 = self._create_bar("2317", self.t_mid, price=100.0, volume=5000)
        fills1 = self.gateway.process_bar(bar1)

        self.assertEqual(len(fills1), 1)
        self.assertEqual(fills1[0].fill_quantity, 500)
        self.assertEqual(order.status, PaperOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(order.filled_quantity, 500)
        self.assertEqual(order.remaining_quantity, 500)
        self.assertEqual(self.broker.get_positions()["2317"].quantity, 500)
        self._assert_ledger_invariants(prices)

        # 6. 模擬第二筆行情：撮合成交剩餘 500 股 (完全成交)
        bar2 = self._create_bar("2317", self.t_close, price=100.0, volume=10000)
        fills2 = self.gateway.process_bar(bar2)

        self.assertEqual(len(fills2), 1)
        self.assertEqual(fills2[0].fill_quantity, 500)
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(order.filled_quantity, 1000)
        self.assertEqual(order.remaining_quantity, 0)

        # 7. 驗證帳本完全成交狀態
        self.assertEqual(self.broker.get_reserved_cash(), Decimal("0.00"))
        self.assertEqual(self.broker.get_positions()["2317"].quantity, 1000)
        self._assert_ledger_invariants(prices)

        # 8. 每日收盤作業，結算當日資產淨值 NAV (收盤價 105.00 元)
        close_prices = {"2317": Decimal("105.00")}
        daily_nav = self.gateway.process_daily_close(self.trade_date, close_prices)

        self.assertEqual(daily_nav.date, self.trade_date)
        self.assertEqual(daily_nav.reserved_cash, Decimal("0.00"))
        self.assertEqual(daily_nav.market_value, Decimal("105000.00")) # 1000 * 105
        # 未實現損益 = 市值 105,000 - 買入總成本 (含手續費)
        self.assertGreater(daily_nav.unrealized_pnl, Decimal("0.00"))
        self.assertEqual(daily_nav.total_equity, daily_nav.cash + daily_nav.market_value)
        self.assertEqual(len(self.broker.get_nav_history()), 1)

    # ══════════════════════════════════════════════════════════════
    # B. 風控拒絕流程 (Risk Rejection Flow)
    # ══════════════════════════════════════════════════════════════

    def test_B01_order_exceeding_single_limit_rejected(self) -> None:
        """超過單筆上限 (50萬) 之買單必須被拒絕，券商端不建立開立中訂單，帳務 0 變化。"""
        # 6000 股 * 100 元 = 600,000 元 > 500,000 元
        sig = self._create_signal("SIG-B01", quantity=6000, limit_price=100.0)
        ok, decision, order = self.gateway.submit_signal(sig)

        self.assertFalse(ok)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.SINGLE_ORDER_LIMIT_EXCEEDED.value, decision.reason_codes)
        self.assertIsNone(order)

        # 券商中無任何開立中委託
        self.assertEqual(len(self.broker.order_manager.get_open_orders()), 0)
        # 現金與部位維持初始狀態
        self.assertEqual(self.broker.get_cash(), self.initial_cash)
        self.assertEqual(self.broker.get_reserved_cash(), Decimal("0.00"))
        self.assertEqual(len(self.broker.get_positions()), 0)
        self._assert_ledger_invariants()

    def test_B02_bypass_prevention_on_broker_directly(self) -> None:
        """驗證若呼叫端直接向 Broker 送單，Broker 內建的風控閘門依然生效攔截。"""
        # 將 broker 綁定 risk_engine，直接調用 broker.submit_signal 測試深層防禦
        sig_risky = self._create_signal("SIG-B02", quantity=6000, limit_price=100.0)
        order = self.broker.submit_signal(sig_risky)

        # 訂單直接被拒絕，未進入開立狀態，未預留資金
        self.assertEqual(order.status, PaperOrderStatus.REJECTED)
        self.assertIn("風控拒絕", order.rejection_reason)
        self.assertEqual(self.broker.get_reserved_cash(), Decimal("0.00"))
        self.assertEqual(len(self.broker.order_manager.get_open_orders()), 0)

    # ══════════════════════════════════════════════════════════════
    # C. 資金與持倉預留 (Cash & Position Reservation)
    # ══════════════════════════════════════════════════════════════

    def test_C01_buy_funds_reservation_and_cancellation(self) -> None:
        """驗證買入資金預留、可用額度動態扣減、訂單取消後 100% 退回。"""
        sig1 = self._create_signal("SIG-C01-A", quantity=1000, limit_price=100.0)
        ok1, _, order1 = self.gateway.submit_signal(sig1)
        self.assertTrue(ok1)
        assert order1 is not None

        reserved = self.broker.get_reserved_cash()
        self.assertGreater(reserved, Decimal("0.00"))

        # 取消委託
        success = self.gateway.cancel_order(order1.order_id, reason="策略主動撤單")
        self.assertTrue(success)
        self.assertEqual(order1.status, PaperOrderStatus.CANCELLED)

        # 預留資金完全釋放回可用現金
        self.assertEqual(self.broker.get_reserved_cash(), Decimal("0.00"))
        self.assertEqual(self.broker.get_cash(), self.initial_cash)
        self._assert_ledger_invariants()

    def test_C02_sell_position_reservation_and_partial_fill_cancellation(self) -> None:
        """驗證賣出持倉預留、防範裸賣空、部分成交與剩餘持倉退回。"""
        # 先買入並完全成交 1000 股 2317
        buy_sig = self._create_signal("SIG-C02-BUY", quantity=1000, limit_price=100.0)
        self.gateway.submit_signal(buy_sig)
        bar = self._create_bar("2317", self.t_mid, price=100.0, volume=20000)
        self.gateway.process_bar(bar)
        self.assertEqual(self.broker.get_positions()["2317"].quantity, 1000)
        self.assertEqual(self.broker.ledger.get_available_shares("2317"), 1000)

        # 送出賣單 1000 股
        sell_sig = self._create_signal("SIG-C02-SELL", action=SignalAction.SELL, quantity=1000, limit_price=100.0)
        ok_sell, _, sell_order = self.gateway.submit_signal(sell_sig)
        self.assertTrue(ok_sell)
        assert sell_order is not None

        # 1000 股完全被凍結預留，可用持倉歸零
        self.assertEqual(self.broker.ledger.reserved_shares.get("2317", 0), 1000)
        self.assertEqual(self.broker.ledger.get_available_shares("2317"), 0)

        # 企圖再送出賣單 (裸賣空) -> 立即被攔截
        naked_sig = self._create_signal("SIG-C02-NAKED", action=SignalAction.SELL, quantity=100, limit_price=100.0)
        ok_naked, dec_naked, _ = self.gateway.submit_signal(naked_sig)
        self.assertFalse(ok_naked)
        self.assertIn(RiskRejectCode.SHORT_SELLING_DISALLOWED.value, dec_naked.reason_codes)

        # 部分成交 400 股
        bar_sell = self._create_bar("2317", self.t_close, price=100.0, volume=4000) # 10% cap = 400 股
        fills = self.gateway.process_bar(bar_sell)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_quantity, 400)
        self.assertEqual(sell_order.status, PaperOrderStatus.PARTIALLY_FILLED)
        # 剩餘預留 600 股，總持倉剩 600 股
        self.assertEqual(self.broker.ledger.reserved_shares.get("2317", 0), 600)
        self.assertEqual(self.broker.get_positions()["2317"].quantity, 600)

        # 撤銷剩餘 600 股賣單
        self.gateway.cancel_order(sell_order.order_id, reason="停止賣出")
        self.assertEqual(sell_order.status, PaperOrderStatus.CANCELLED)
        # 預留持倉釋放回可用持倉
        self.assertEqual(self.broker.ledger.reserved_shares.get("2317", 0), 0)
        self.assertEqual(self.broker.ledger.get_available_shares("2317"), 600)
        self._assert_ledger_invariants()

    # ══════════════════════════════════════════════════════════════
    # D. 重複訊號防禦 (Duplicate Signal Protection)
    # ══════════════════════════════════════════════════════════════

    def test_D01_duplicate_signal_id_rejected(self) -> None:
        """連續提交兩次相同 signal_id，第二次必須被拒絕，嚴禁重複下單。"""
        sig1 = self._create_signal("SIG-D01")
        ok1, _, _ = self.gateway.submit_signal(sig1)
        self.assertTrue(ok1)

        sig2 = self._create_signal("SIG-D01")
        ok2, dec2, order2 = self.gateway.submit_signal(sig2)
        self.assertFalse(ok2)
        self.assertIn(RiskRejectCode.DUPLICATE_SIGNAL.value, dec2.reason_codes)

    def test_D02_resubmit_after_partial_fill_rejected(self) -> None:
        """訂單部分成交後，再次提交相同 signal_id 依然被拒絕。"""
        sig = self._create_signal("SIG-D02")
        self.gateway.submit_signal(sig)
        bar = self._create_bar("2317", self.t_mid, price=100.0, volume=5000)
        self.gateway.process_bar(bar)

        # 重新提交
        sig_dup = self._create_signal("SIG-D02")
        ok, dec, _ = self.gateway.submit_signal(sig_dup)
        self.assertFalse(ok)
        self.assertIn(RiskRejectCode.DUPLICATE_SIGNAL.value, dec.reason_codes)

    def test_D03_resubmit_after_cancel_rejected(self) -> None:
        """訂單取消後，再次提交相同 signal_id 依然被拒絕。"""
        sig = self._create_signal("SIG-D03")
        _, _, order = self.gateway.submit_signal(sig)
        assert order is not None
        self.gateway.cancel_order(order.order_id)

        # 重新提交
        sig_dup = self._create_signal("SIG-D03")
        ok, dec, _ = self.gateway.submit_signal(sig_dup)
        self.assertFalse(ok)
        self.assertIn(RiskRejectCode.DUPLICATE_SIGNAL.value, dec.reason_codes)

    def test_D04_restart_dedup_persistence_limitation_tested(self) -> None:
        """邊界限制驗證：目前系統為純記憶體架構，重啟後實例無法跨進程獲知過去歷史 signal_id。

        此測試明確記錄架構邊界，證明在未外接 Redis/資料庫持久化前，不可宣稱已具備跨重啟去重安全性。
        """
        sig = self._create_signal("SIG-D04-MEMORY-ONLY")
        self.gateway.submit_signal(sig)

        # 模擬進程重啟：新建 broker 與 risk_engine 實例
        restarted_broker = TaiwanPaperBroker(self.broker_config)
        restarted_risk = RiskEngine(self.risk_config)
        restarted_gw = TradingGateway(restarted_broker, restarted_risk)

        # 驗證新實例無法自動感知上一實例的 signal_id (確認持久化限制真實存在)
        ok_restarted, _, _ = restarted_gw.submit_signal(sig)
        self.assertTrue(ok_restarted)  # 在純記憶體無持久化下會放行，證實架構限制！

    # ══════════════════════════════════════════════════════════════
    # E. 熔斷與 Kill Switch (Circuit Breaker & Kill Switch)
    # ══════════════════════════════════════════════════════════════

    def test_E01_daily_loss_limit_blocks_new_buys(self) -> None:
        """當日虧損達到 2% (40,000 元) 門檻時，新開倉買單全數被攔截。"""
        # 手動注入已實現虧損 45,000 元
        self.broker.ledger.cumulative_realized_pnl = Decimal("-45000.00")

        sig = self._create_signal("SIG-E01")
        ok, dec, _ = self.gateway.submit_signal(sig)
        self.assertFalse(ok)
        self.assertIn(RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED.value, dec.reason_codes)

    def test_E02_max_drawdown_circuit_breaker_persists(self) -> None:
        """最大回撤達 15% 觸發熔斷後，狀態自動維持鎖定，後續買單一律拒絕。"""
        # 峰值 200 萬，跌至 160 萬 (回撤 20% > 15%)
        # 藉由 context 注入並觸發評估
        ctx = RiskContext(
            current_time=self.t_open,
            cash=Decimal("1600000.00"),
            peak_nav=Decimal("2000000.00"),
            baseline_nav=Decimal("2000000.00"),
        )
        sig = self._create_signal("SIG-E02-TRIGGER")
        self.risk_engine.evaluate(sig, ctx)
        self.assertTrue(self.risk_engine.circuit_breaker_active)

        # 後續即使帳戶現金充裕，買單依舊拒絕
        sig_next = self._create_signal("SIG-E02-NEXT")
        ok, dec, _ = self.gateway.submit_signal(sig_next)
        self.assertFalse(ok)
        self.assertIn(RiskRejectCode.CIRCUIT_BREAKER_ACTIVE.value, dec.reason_codes)

    def test_E03_kill_switch_cannot_be_reset_by_ai_signal(self) -> None:
        """Kill Switch 啟動後，任何由 AI 訊號發送之請求皆無法解除中斷狀態。"""
        self.risk_engine.activate_kill_switch(authorized_by="CRO", reason="外部網路斷線 Hard Stop")
        self.assertTrue(self.risk_engine.kill_switch_active)

        # AI 偽造解除訊號
        sig = self._create_signal("SIG-E03", reason="AI model requests reset kill switch")
        ok, dec, _ = self.gateway.submit_signal(sig)
        self.assertFalse(ok)
        self.assertTrue(self.risk_engine.kill_switch_active)

    def test_E04_risk_reducing_sell_allowed_under_circuit_breaker_and_kill_switch(self) -> None:
        """在熔斷與 Kill Switch 雙重鎖定下，降低風險之平倉賣單依然依規放行，確保部位能變現止血。"""
        # 先以乾淨狀態買入 1000 股
        buy_sig = self._create_signal("SIG-E04-BUY", quantity=1000, limit_price=100.0)
        self.gateway.submit_signal(buy_sig)
        bar = self._create_bar("2317", self.t_mid, price=100.0, volume=20000)
        self.gateway.process_bar(bar)
        self.assertEqual(self.broker.get_positions()["2317"].quantity, 1000)

        # 啟動雙重鎖定：熔斷 + Kill Switch
        self.risk_engine._circuit_breaker_active = True
        self.risk_engine.activate_kill_switch(authorized_by="CRO", reason="系統緊急避險")

        # 送出平倉賣單 1000 股
        sell_sig = self._create_signal("SIG-E04-SELL", action=SignalAction.SELL, quantity=1000, limit_price=100.0)
        ok, dec, sell_order = self.gateway.submit_signal(sell_sig)

        # 驗證平倉賣單被放行
        self.assertTrue(ok)
        self.assertTrue(dec.allowed)
        self.assertTrue(dec.is_risk_reducing)
        assert sell_order is not None

        # 撮合成交，部位完全平倉
        fills = self.gateway.process_bar(bar)
        self.assertEqual(len(fills), 1)
        self.assertEqual(self.broker.get_positions().get("2317", None), None)

    def test_E05_manual_reset_requires_authorization_and_creates_audit_log(self) -> None:
        """人工解除熔斷與 Kill Switch 必須具名，並產生不可篡改之審計日誌。"""
        self.risk_engine.activate_kill_switch(authorized_by="AdminA", reason="例行維護")
        self.risk_engine._circuit_breaker_active = True

        # 具名授權解除
        self.risk_engine.deactivate_kill_switch(authorized_by="CRO", reason="維護完畢通過審批")
        self.risk_engine.reset_circuit_breaker(authorized_by="CRO", reason="重新調整策略基準線")

        self.assertFalse(self.risk_engine.kill_switch_active)
        self.assertFalse(self.risk_engine.circuit_breaker_active)

        # 檢查審計日誌
        log = self.risk_engine.audit_log
        self.assertGreaterEqual(len(log), 3)
        actions = [entry["action"] for entry in log]
        self.assertIn("ACTIVATE_KILL_SWITCH", actions)
        self.assertIn("DEACTIVATE_KILL_SWITCH", actions)
        self.assertIn("RESET_CIRCUIT_BREAKER", actions)

    # ══════════════════════════════════════════════════════════════
    # F. 交易時序與市場限制 (Execution Timing & Market Constraints)
    # ══════════════════════════════════════════════════════════════

    def test_F01_post_market_signal_cannot_fill_with_earlier_bar(self) -> None:
        """前視偏差防禦：T 日 14:00 (盤後) 產生的訊號，嚴禁用 T 日 13:30 (盤中/收盤) 行情成交。"""
        t_post_market = dt.datetime(2026, 10, 20, 14, 0, 0, tzinfo=self.tz)
        t_intraday_bar = dt.datetime(2026, 10, 20, 13, 30, 0, tzinfo=self.tz)

        sig = self._create_signal("SIG-F01", created_at=t_post_market, expires_at=t_post_market + dt.timedelta(hours=2))
        ok, _, order = self.gateway.submit_signal(sig)
        self.assertTrue(ok)
        assert order is not None

        # 送入時間戳早於委託時間之行情
        bar = self._create_bar("2317", timestamp=t_intraday_bar, price=100.0)
        fills = self.gateway.process_bar(bar)

        # 嚴禁成交
        self.assertEqual(len(fills), 0)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)
        self.assertEqual(order.filled_quantity, 0)

    def test_F02_halted_stock_cannot_trade(self) -> None:
        """停牌股票委託禁止成交。"""
        sig = self._create_signal("SIG-F02")
        self.gateway.submit_signal(sig)

        bar_halted = self._create_bar("2317", self.t_mid, price=100.0, is_halted=True, volume=0)
        fills = self.gateway.process_bar(bar_halted)
        self.assertEqual(len(fills), 0)

    def test_F03_limit_up_locked_buy_blocked(self) -> None:
        """漲停鎖死無賣量時，買單排隊不可撮合成交。"""
        sig = self._create_signal("SIG-F03", symbol="2317", quantity=1000, limit_price=110.0)
        self.gateway.submit_signal(sig)

        bar_limit_up = self._create_bar("2317", self.t_mid, price=110.0, is_limit_up=True)
        fills = self.gateway.process_bar(bar_limit_up)
        self.assertEqual(len(fills), 0)

    def test_F04_limit_down_locked_sell_blocked(self) -> None:
        """跌停鎖死無買量時，賣單排隊不可撮合成交。"""
        # 先買入持有
        buy_sig = self._create_signal("SIG-F04-B", quantity=1000, limit_price=100.0)
        self.gateway.submit_signal(buy_sig)
        self.gateway.process_bar(self._create_bar("2317", self.t_open, price=100.0, volume=20000))

        # 送出賣單
        sell_sig = self._create_signal("SIG-F04-S", action=SignalAction.SELL, quantity=1000, limit_price=90.0)
        self.gateway.submit_signal(sell_sig)

        bar_limit_down = self._create_bar("2317", self.t_mid, price=90.0, is_limit_down=True)
        fills = self.gateway.process_bar(bar_limit_down)
        self.assertEqual(len(fills), 0)

    def test_F05_missing_market_data_refuses_silent_zero(self) -> None:
        """當持股缺少收盤估值報價時，收盤作業拋出異常，嚴禁靜默當成 0 元計算。"""
        # 買入持有 2317
        buy_sig = self._create_signal("SIG-F05", quantity=1000, limit_price=100.0)
        self.gateway.submit_signal(buy_sig)
        self.gateway.process_bar(self._create_bar("2317", self.t_open, price=100.0, volume=20000))

        # 收盤行情完全遺失 2317
        missing_close = {"2330": Decimal("1000.00")}
        with self.assertRaises(ValueError) as cm:
            self.gateway.process_daily_close(self.trade_date, missing_close)
        self.assertIn("缺少標的 2317 之有效收盤估值報價", str(cm.exception))

    def test_F06_partial_fill_does_not_credit_unfilled_quantity(self) -> None:
        """部分成交時，未成交數量嚴禁記入持倉或多退預留款。"""
        sig = self._create_signal("SIG-F06", quantity=1000, limit_price=100.0)
        self.gateway.submit_signal(sig)

        # 處置股票限制量額外減半，volume=4000 -> 10% cap=400 -> disposition cap=200 股
        bar_disp = self._create_bar("2317", self.t_mid, price=100.0, volume=4000, is_disposition=True)
        fills = self.gateway.process_bar(bar_disp)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_quantity, 200)
        self.assertEqual(self.broker.get_positions()["2317"].quantity, 200)
        # 預留款僅依 200 股比例結算，剩餘 800 股預留款依然妥善凍結
        self.assertGreater(self.broker.get_reserved_cash(), Decimal("70000.00"))
        self._assert_ledger_invariants()

    # ══════════════════════════════════════════════════════════════
    # G. 帳務一致性檢驗 (Ledger Invariant Assertions)
    # ══════════════════════════════════════════════════════════════

    def test_G01_global_ledger_invariants_across_complex_lifecycle(self) -> None:
        """在多標的、多次部分成交、取消與平倉之複雜交易路徑下，驗證全域帳務一致性。"""
        # 買入 2317 1000 股
        sig1 = self._create_signal("SIG-G01-A", symbol="2317", quantity=1000, limit_price=100.0)
        self.gateway.submit_signal(sig1)
        self._assert_ledger_invariants()

        # 部分成交 300 股
        bar1 = self._create_bar("2317", self.t_open, price=100.0, volume=3000)
        self.gateway.process_bar(bar1)
        self._assert_ledger_invariants()

        # 再送出買入 2330 200 股 @ 1000 元 (20 萬元)
        sig2 = self._create_signal("SIG-G01-B", symbol="2330", quantity=200, limit_price=1000.0)
        self.gateway.submit_signal(sig2)
        self._assert_ledger_invariants()

        # 取消 2317 剩餘 700 股
        open_orders = self.broker.order_manager.get_open_orders()
        order_2317 = [o for o in open_orders if o.symbol == "2317"][0]
        self.gateway.cancel_order(order_2317.order_id)
        self._assert_ledger_invariants()

        # 完全成交 2330
        bar2 = self._create_bar("2330", self.t_mid, price=1000.0, volume=50000)
        self.gateway.process_bar(bar2)
        self._assert_ledger_invariants({"2317": Decimal("100.00"), "2330": Decimal("1000.00")})

        # 收盤結算
        close_px = {"2317": Decimal("102.00"), "2330": Decimal("1010.00")}
        self.gateway.process_daily_close(self.trade_date, close_px)
        self._assert_ledger_invariants(close_px)

    # ══════════════════════════════════════════════════════════════
    # H. 例外與恢復 (Exceptions & Recovery)
    # ══════════════════════════════════════════════════════════════

    def test_H01_terminal_order_cannot_be_rematched(self) -> None:
        """終態訂單嚴禁再次撮合，若意外被調用必須拋出 ValueError。"""
        sig = self._create_signal("SIG-H01", quantity=1000, limit_price=100.0)
        _, _, order = self.gateway.submit_signal(sig)
        assert order is not None

        # 完全成交至終態
        bar = self._create_bar("2317", self.t_open, price=100.0, volume=50000)
        self.gateway.process_bar(bar)
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertTrue(order.is_terminal)

        # 企圖再次對已 FILLED 訂單進行撮合
        with self.assertRaises(ValueError) as cm:
            self.broker.fill_engine.match_order(order, bar)
        self.assertIn("已處於終態 FILLED，嚴禁再次撮合", str(cm.exception))

    def test_H02_double_cancellation_safe(self) -> None:
        """已取消之訂單再次調用 cancel_order 回傳 False，不重複退還預留款。"""
        sig = self._create_signal("SIG-H02", quantity=1000, limit_price=100.0)
        _, _, order = self.gateway.submit_signal(sig)
        assert order is not None

        res1 = self.gateway.cancel_order(order.order_id)
        self.assertTrue(res1)
        cash_after_cancel = self.broker.get_cash()

        # 第二次取消
        res2 = self.gateway.cancel_order(order.order_id)
        self.assertFalse(res2)
        self.assertEqual(self.broker.get_cash(), cash_after_cancel)

    def test_H03_over_reduce_shares_rejected_by_position(self) -> None:
        """持倉扣減股數大於現有持股時，拋出 ValueError，保護帳本原子性。"""
        pos = self.broker.ledger.positions.get("2317")
        self.assertIsNone(pos)

        # 直接對帳本嘗試超賣
        with self.assertRaises(ValueError) as cm:
            self.broker.ledger.record_sell_fill(
                symbol="2317",
                fill_shares=100,
                fill_price=Decimal("100.00"),
                notional=Decimal("10000.00"),
                fee=Decimal("20.00"),
                tax=Decimal("30.00"),
                reserved_shares_deduct=100,
            )
        self.assertIn("持倉不足以賣出成交", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
