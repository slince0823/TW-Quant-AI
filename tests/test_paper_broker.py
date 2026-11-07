"""台股模擬交易引擎單元測試 (tests/test_paper_broker.py).

完全離線執行，使用自行建立的 Mock 行情與 Mock 交易資料，
不依賴外部 API、網路或真實券商連線。
全面驗證 TaiwanPaperBroker 的 26 項核心場景。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest

from src.tw_quant.broker import (
    DailyNAV,
    FillRecord,
    MarketBar,
    PaperOrder,
    PaperOrderStatus,
    Position,
    TaiwanPaperBroker,
    TaiwanPaperConfig,
)
from src.tw_quant.contracts import (
    DuplicateSignalError,
    QuantityUnit,
    SignalAction,
    SignalOrderType,
    UnifiedTradeSignal,
)


class TestTaiwanPaperBroker(unittest.TestCase):
    """TaiwanPaperBroker 交易撮合、會計與狀態機測試套件。"""

    def setUp(self) -> None:
        self.taipei_tz = dt.timezone(dt.timedelta(hours=8))
        self.t_date = dt.date(2026, 10, 21)
        self.base_time = dt.datetime(2026, 10, 21, 9, 0, 0, tzinfo=self.taipei_tz)
        self.expire_time = dt.datetime(2026, 10, 21, 13, 30, 0, tzinfo=self.taipei_tz)

        # 預設配置：起始 2,000,000 元，折讓 6 折，最低 20 元，股票證交稅 0.3%，滑價 0.05%
        self.config = TaiwanPaperConfig(
            initial_cash=Decimal("2000000.00"),
            commission_rate=Decimal("0.001425"),
            commission_discount=Decimal("0.60"),
            min_commission=Decimal("20.00"),
            stock_tax_rate=Decimal("0.0030"),
            etf_tax_rate=Decimal("0.0010"),
            slippage_rate=Decimal("0.0005"),
            max_volume_participation=Decimal("0.10"),
        )
        self.broker = TaiwanPaperBroker(self.config)

    def _create_signal(self, **overrides) -> UnifiedTradeSignal:
        params = {
            "signal_id": f"SIG-{dt.datetime.now().timestamp()}-{overrides.get('symbol', '2330')}",
            "symbol": "2330",
            "action": SignalAction.BUY,
            "quantity": 1000,
            "quantity_unit": QuantityUnit.SHARES,
            "order_type": SignalOrderType.LIMIT,
            "limit_price": 1000.0,
            "created_at": self.base_time,
            "expires_at": self.expire_time,
            "strategy_id": "test_strat",
            "model_version": "v1.0",
            "reason": "Test signal",
        }
        params.update(overrides)
        return UnifiedTradeSignal(**params)

    def _create_bar(self, **overrides) -> MarketBar:
        params = {
            "symbol": "2330",
            "timestamp": self.base_time + dt.timedelta(minutes=5),
            "open": Decimal("1000.00"),
            "high": Decimal("1005.00"),
            "low": Decimal("995.00"),
            "close": Decimal("1000.00"),
            "volume": 50000,  # 50,000 股
            "yesterday_close": Decimal("1000.00"),
            "is_halted": False,
            "is_disposition": False,
            "is_limit_up_locked": False,
            "is_limit_down_locked": False,
        }
        params.update(overrides)
        return MarketBar(**params)

    # 1. 正常買入
    def test_01_normal_buy(self) -> None:
        sig = self._create_signal(signal_id="SIG-01", quantity=1000, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)

        bar = self._create_bar(volume=100000)  # 10% 可撮合 10,000 股，足以完全成交 1,000 股
        fills = self.broker.process_bar(bar)
        self.assertEqual(len(fills), 1)
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertIn("2330", self.broker.get_positions())
        self.assertEqual(self.broker.get_positions()["2330"].quantity, 1000)

    # 2. 正常賣出
    def test_02_normal_sell(self) -> None:
        # 先買進
        self.test_01_normal_buy()
        sell_sig = self._create_signal(
            signal_id="SIG-02",
            action=SignalAction.SELL,
            quantity=1000,
            limit_price=1005.0,
            created_at=self.base_time + dt.timedelta(minutes=10),
            expires_at=self.expire_time,
        )
        sell_order = self.broker.submit_signal(sell_sig)
        self.assertEqual(sell_order.status, PaperOrderStatus.ACCEPTED)

        # 以 1010 元行情撮合賣出
        bar = self._create_bar(
            timestamp=self.base_time + dt.timedelta(minutes=15),
            close=Decimal("1010.00"),
            high=Decimal("1015.00"),
            volume=100000,
        )
        fills = self.broker.process_bar(bar)
        self.assertEqual(len(fills), 1)
        self.assertEqual(sell_order.status, PaperOrderStatus.FILLED)
        self.assertNotIn("2330", self.broker.get_positions())  # 已清倉

    # 3. 現金不足
    def test_03_insufficient_cash(self) -> None:
        # 現金 2,000,000，企圖買進 3,000 股 (需 > 3,000,000)
        sig = self._create_signal(signal_id="SIG-03", quantity=3000, limit_price=1200.0)
        order = self.broker.submit_signal(sig)
        self.assertEqual(order.status, PaperOrderStatus.REJECTED)
        self.assertIn("現金不足", order.rejection_reason)

    # 4. 可賣持倉不足
    def test_04_insufficient_shares_to_sell(self) -> None:
        # 無持倉直接嘗試賣出
        sig = self._create_signal(signal_id="SIG-04", action=SignalAction.SELL, quantity=1000)
        order = self.broker.submit_signal(sig)
        self.assertEqual(order.status, PaperOrderStatus.REJECTED)
        self.assertIn("可用持倉不足", order.rejection_reason)

    # 5. 手續費計算
    def test_05_commission_calculation(self) -> None:
        # 成交金額 1,000,000 元，折讓 6 折: 1,000,000 * 0.001425 * 0.6 = 855 元
        notional = Decimal("1000000.00")
        fee = self.broker.fill_engine.calculate_commission(notional)
        self.assertEqual(fee, Decimal("855.00"))

    # 6. 最低手續費
    def test_06_minimum_commission(self) -> None:
        # 買進零股 10 股，價格 10 元 (成交額 100 元)
        # 100 * 0.001425 * 0.6 = 0.0855 元 -> 低於最低 20 元，以 20 元計收
        notional = Decimal("100.00")
        fee = self.broker.fill_engine.calculate_commission(notional)
        self.assertEqual(fee, Decimal("20.00"))

    # 7. 證交稅計算
    def test_07_tax_calculation(self) -> None:
        # 股票 1,000,000 元，稅率 0.3% -> 3,000 元
        tax_stock = self.broker.fill_engine.calculate_tax("2330", Decimal("1000000.00"))
        self.assertEqual(tax_stock, Decimal("3000.00"))

        # ETF 0050，成交 1,000,000 元，稅率 0.1% -> 1,000 元
        tax_etf = self.broker.fill_engine.calculate_tax("0050", Decimal("1000000.00"))
        self.assertEqual(tax_etf, Decimal("1000.00"))

        # 當沖現股賣出，成交 1,000,000 元，稅率 0.15% -> 1,500 元
        tax_day = self.broker.fill_engine.calculate_tax("2330", Decimal("1000000.00"), is_day_trading=True)
        self.assertEqual(tax_day, Decimal("1500.00"))

    # 8. 買入資金預留
    def test_08_buy_cash_reservation(self) -> None:
        init_cash = self.broker.get_cash()
        sig = self._create_signal(signal_id="SIG-08", quantity=500, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)
        self.assertGreater(order.reserved_cash, Decimal("500000.00"))
        self.assertEqual(self.broker.get_cash(), init_cash - order.reserved_cash)
        self.assertEqual(self.broker.get_reserved_cash(), order.reserved_cash)

    # 9. 賣出持倉預留
    def test_09_sell_shares_reservation(self) -> None:
        # 先買進 1000 股
        self._execute_buy("SIG-09-BUY", 1000, Decimal("1000.00"))
        self.assertEqual(self.broker.ledger.get_available_shares("2330"), 1000)

        # 賣出 600 股，產生預留
        sig = self._create_signal(
            signal_id="SIG-09-SELL",
            action=SignalAction.SELL,
            quantity=600,
            limit_price=1010.0,
        )
        order = self.broker.submit_signal(sig)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)
        self.assertEqual(self.broker.ledger.get_available_shares("2330"), 400)
        self.assertEqual(self.broker.ledger.reserved_shares["2330"], 600)

    # 10. 漲跌停價格限制
    def test_10_price_limits(self) -> None:
        # 漲停鎖死：買單不得成交
        sig_buy = self._create_signal(signal_id="SIG-10-B", quantity=1000, limit_price=1100.0)
        order_buy = self.broker.submit_signal(sig_buy)
        bar_limit_up = self._create_bar(
            close=Decimal("1100.00"),
            high=Decimal("1100.00"),
            is_limit_up_locked=True,
        )
        fills_b = self.broker.process_bar(bar_limit_up)
        self.assertEqual(len(fills_b), 0)
        self.assertEqual(order_buy.status, PaperOrderStatus.ACCEPTED)

        # 跌停鎖死：賣單不得成交
        self._execute_buy("SIG-10-SETUP", 1000, Decimal("1000.00"))
        sig_sell = self._create_signal(
            signal_id="SIG-10-S",
            action=SignalAction.SELL,
            quantity=1000,
            limit_price=900.0,
        )
        order_sell = self.broker.submit_signal(sig_sell)
        bar_limit_down = self._create_bar(
            close=Decimal("900.00"),
            low=Decimal("900.00"),
            is_limit_down_locked=True,
        )
        fills_s = self.broker.process_bar(bar_limit_down)
        self.assertEqual(len(fills_s), 0)
        self.assertEqual(order_sell.status, PaperOrderStatus.ACCEPTED)

    # 11. 停牌股票禁止成交
    def test_11_halted_stock(self) -> None:
        sig = self._create_signal(signal_id="SIG-11", quantity=1000)
        order = self.broker.submit_signal(sig)
        bar_halted = self._create_bar(is_halted=True, volume=0)
        fills = self.broker.process_bar(bar_halted)
        self.assertEqual(len(fills), 0)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)

    # 12. 處置股票限制
    def test_12_disposition_stock_restriction(self) -> None:
        # 委託 1,000 股，市場 Bar 總量 10,000 股
        # 常態下 10% 可撮合 1,000 股；處置股票減半撮合 500 股
        sig = self._create_signal(signal_id="SIG-12", quantity=1000, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        bar_disp = self._create_bar(is_disposition=True, volume=10000)
        fills = self.broker.process_bar(bar_disp)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_quantity, 500)
        self.assertEqual(order.filled_quantity, 500)
        self.assertEqual(order.status, PaperOrderStatus.PARTIALLY_FILLED)

    # 13. 部分成交
    def test_13_partial_fill(self) -> None:
        # 委託 1,000 股，當前市場量 5,000 股，10% 上限為 500 股
        sig = self._create_signal(signal_id="SIG-13", quantity=1000, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        bar = self._create_bar(volume=5000)
        fills = self.broker.process_bar(bar)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fill_quantity, 500)
        self.assertEqual(order.status, PaperOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(order.remaining_quantity, 500)

    # 14. 完全成交
    def test_14_full_fill_after_partial(self) -> None:
        self.test_13_partial_fill()
        order = self.broker.order_manager.get_open_orders()[0]
        # 第二根 Bar，成交剩餘 500 股
        bar2 = self._create_bar(volume=10000, timestamp=self.base_time + dt.timedelta(minutes=10))
        fills2 = self.broker.process_bar(bar2)
        self.assertEqual(len(fills2), 1)
        self.assertEqual(fills2[0].fill_quantity, 500)
        self.assertEqual(order.status, PaperOrderStatus.FILLED)
        self.assertEqual(order.remaining_quantity, 0)

    # 15. 未成交委託
    def test_15_unfilled_order(self) -> None:
        # 買入限價 980，但 Bar 最低價為 995，不滿足限價條件
        sig = self._create_signal(signal_id="SIG-15", limit_price=980.0)
        order = self.broker.submit_signal(sig)
        bar = self._create_bar(low=Decimal("995.00"), close=Decimal("1000.00"))
        fills = self.broker.process_bar(bar)
        self.assertEqual(len(fills), 0)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)
        self.assertEqual(order.filled_quantity, 0)

    # 16. 委託取消
    def test_16_order_cancellation(self) -> None:
        sig = self._create_signal(signal_id="SIG-16", quantity=1000, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        res_cash = order.reserved_cash

        ok = self.broker.cancel_order(order.order_id, reason="測試撤單")
        self.assertTrue(ok)
        self.assertEqual(order.status, PaperOrderStatus.CANCELLED)
        self.assertEqual(self.broker.get_reserved_cash(), Decimal("0.00"))

    # 17. 訂單到期
    def test_17_order_expiry_at_close(self) -> None:
        sig = self._create_signal(signal_id="SIG-17", quantity=1000, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)

        # 執行每日收盤結算
        close_prices = {"2330": Decimal("1000.00")}
        nav = self.broker.process_daily_close(self.t_date, close_prices)
        self.assertEqual(order.status, PaperOrderStatus.EXPIRED)
        self.assertEqual(self.broker.get_reserved_cash(), Decimal("0.00"))
        self.assertIsInstance(nav, DailyNAV)

    # 18. 重複 signal_id 攔截
    def test_18_duplicate_signal_id(self) -> None:
        sig = self._create_signal(signal_id="SIG-18-DUP")
        self.broker.submit_signal(sig)
        with self.assertRaises(DuplicateSignalError):
            self.broker.submit_signal(sig)

    # 19. 重複成交防護
    def test_19_prevent_double_fill_on_same_bar(self) -> None:
        sig = self._create_signal(signal_id="SIG-19", quantity=500, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        bar = self._create_bar(volume=100000)
        fills1 = self.broker.process_bar(bar)
        self.assertEqual(len(fills1), 1)
        self.assertEqual(order.status, PaperOrderStatus.FILLED)

        # 再次對相同 Bar 撮合，已 FILLED 訂單不得再次撮合
        fills2 = self.broker.process_bar(bar)
        self.assertEqual(len(fills2), 0)

    # 20. Decimal 金額精度
    def test_20_decimal_precision(self) -> None:
        sig = self._create_signal(signal_id="SIG-20", quantity=333, limit_price=33.33)
        order = self.broker.submit_signal(sig)
        bar = self._create_bar(
            open=Decimal("33.33"),
            high=Decimal("33.50"),
            low=Decimal("33.00"),
            close=Decimal("33.33"),
            yesterday_close=Decimal("33.33"),
            volume=100000,
        )
        fills = self.broker.process_bar(bar)
        self.assertEqual(len(fills), 1)
        self.assertIsInstance(fills[0].notional, Decimal)
        self.assertIsInstance(fills[0].commission, Decimal)
        self.assertIsInstance(self.broker.get_cash(), Decimal)

    # 21. 已實現損益精算
    def test_21_realized_pnl_accuracy(self) -> None:
        # 買進 1000 股 @ 1000 元 (成本 = 1,000,000 + 855 手續費 = 1,000,855)
        self._execute_buy("SIG-21-B", 1000, Decimal("1000.00"))

        # 賣出 1000 股 @ 1050 元 (未稅成價 = 1,050,000)
        # 賣出手續費: 1,050,000 * 0.001425 * 0.6 = 898 元
        # 賣出證交稅: 1,050,000 * 0.003 = 3,150 元
        # 淨回收金額 = 1,050,000 - 898 - 3,150 = 1,045,952 元
        # 原成本 = 1,000,855 元
        # 已實現損益 = 1,045,952 - 1,000,855 = +45,097 元
        sell_sig = self._create_signal(
            signal_id="SIG-21-S",
            action=SignalAction.SELL,
            quantity=1000,
            limit_price=1050.0,
            created_at=self.base_time + dt.timedelta(minutes=10),
        )
        self.broker.submit_signal(sell_sig)
        bar = self._create_bar(
            timestamp=self.base_time + dt.timedelta(minutes=15),
            close=Decimal("1050.00"),
            high=Decimal("1055.00"),
            volume=100000,
        )
        self.broker.process_bar(bar)
        realized = self.broker.get_realized_pnl()
        self.assertEqual(realized, Decimal("45097.00"))

    # 22. 未實現損益
    def test_22_unrealized_pnl(self) -> None:
        self._execute_buy("SIG-22-B", 1000, Decimal("1000.00"))
        # 價格漲到 1020 元
        nav = self.broker.process_daily_close(
            self.t_date, {"2330": Decimal("1020.00")}
        )
        # 市值 1,020,000 元，成本 1,000,855 元，未實現 = +19,145 元
        self.assertEqual(nav.unrealized_pnl, Decimal("19145.00"))

    # 23. 每日 NAV 計算
    def test_23_daily_nav(self) -> None:
        self._execute_buy("SIG-23-B", 1000, Decimal("1000.00"))
        nav = self.broker.process_daily_close(
            self.t_date, {"2330": Decimal("1000.00")}
        )
        self.assertGreater(nav.total_equity, Decimal("0.00"))
        self.assertEqual(len(self.broker.get_nav_history()), 1)

    # 24. 缺少行情資料嚴格拋錯
    def test_24_missing_market_data_on_close(self) -> None:
        self._execute_buy("SIG-24-B", 1000, Decimal("1000.00"))
        # 收盤結算時未提供 2330 報價，嚴禁靜默當成 0
        with self.assertRaises(ValueError) as ctx:
            self.broker.process_daily_close(self.t_date, {})
        self.assertIn("缺少標的 2330 之有效收盤估值報價", str(ctx.exception))

    # 25. T 日訊號不得使用 T 日盤後尚未可得的價格成交
    def test_25_post_market_timing_guard(self) -> None:
        post_signal = self._create_signal(
            signal_id="SIG-25-POST",
            created_at=dt.datetime(2026, 10, 21, 15, 0, 0, tzinfo=self.taipei_tz),
            expires_at=dt.datetime(2026, 10, 22, 13, 30, 0, tzinfo=self.taipei_tz),
        )
        order = self.broker.submit_signal(post_signal)

        # 企圖拿 T 日盤中的 Bar 撮合
        bar_t_intraday = self._create_bar(
            timestamp=dt.datetime(2026, 10, 21, 11, 0, 0, tzinfo=self.taipei_tz)
        )
        fills = self.broker.process_bar(bar_t_intraday)
        self.assertEqual(len(fills), 0)  # 時序不符直接略過，不撮合
        self.assertEqual(order.status, PaperOrderStatus.ACCEPTED)

    # 26. 已終止訂單不得再次成交
    def test_26_terminal_order_cannot_fill(self) -> None:
        sig = self._create_signal(signal_id="SIG-26", quantity=1000, limit_price=1000.0)
        order = self.broker.submit_signal(sig)
        self.broker.cancel_order(order.order_id)
        self.assertEqual(order.status, PaperOrderStatus.CANCELLED)

        # 企圖再次對已撤銷訂單手動添加成交
        dummy_fill = FillRecord(
            fill_id="F-DUMMY",
            order_id=order.order_id,
            signal_id=order.signal_id,
            symbol="2330",
            fill_quantity=100,
            fill_price=Decimal("1000.00"),
            notional=Decimal("100000.00"),
            commission=Decimal("20.00"),
            tax=Decimal("0.00"),
            slippage=Decimal("0.00"),
            timestamp=self.base_time,
        )
        with self.assertRaises(ValueError) as ctx:
            order.add_fill(dummy_fill)
        self.assertIn("處於終態", str(ctx.exception))

    # ── 輔助買入並撮合 ──
    def _execute_buy(self, signal_id: str, shares: int, price: Decimal) -> None:
        sig = self._create_signal(
            signal_id=signal_id, quantity=shares, limit_price=float(price)
        )
        self.broker.submit_signal(sig)
        bar = self._create_bar(
            close=price,
            high=price + Decimal("5.00"),
            low=price - Decimal("5.00"),
            volume=shares * 10,
        )
        self.broker.process_bar(bar)


if __name__ == "__main__":
    unittest.main()
