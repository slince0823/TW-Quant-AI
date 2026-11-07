"""獨立交易風控引擎單元測試 (tests/test_risk_engine.py).

完全離線執行，使用自行建立的 Mock 資料，不依賴外部 API、網路或真實券商連線。
全面驗證 RiskEngine 的 22 項核心風控場景與 10 道安全防線。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import unittest

from src.tw_quant.contracts import (
    QuantityUnit,
    SignalAction,
    SignalOrderType,
    UnifiedTradeSignal,
)
from src.tw_quant.risk import (
    RiskConfig,
    RiskContext,
    RiskDecision,
    RiskEngine,
    RiskRejectCode,
)


class TestRiskEngine(unittest.TestCase):
    """RiskEngine 10 道安全防線與決策機制單元測試套件。"""

    def setUp(self) -> None:
        self.taipei_tz = dt.timezone(dt.timedelta(hours=8))
        self.now = dt.datetime(2026, 10, 21, 9, 30, 0, tzinfo=self.taipei_tz)
        self.expire = dt.datetime(2026, 10, 21, 13, 30, 0, tzinfo=self.taipei_tz)

        # 預設風控配置：
        # 單筆上限 50 萬、單股上限 10% NAV、總持倉上限 80% NAV、最低現金保留 20% NAV、
        # 每日虧損上限 2%、最大回撤熔斷 15%、嚴禁放空
        self.config = RiskConfig(
            max_order_amount=Decimal("500000.00"),
            max_single_position_ratio=Decimal("0.10"),
            max_total_exposure_ratio=Decimal("0.80"),
            min_cash_reserve_ratio=Decimal("0.20"),
            max_daily_loss_ratio=Decimal("0.02"),
            max_drawdown_limit=Decimal("0.15"),
            allow_short_selling=False,
            restricted_symbols={"9999"},
        )
        self.engine = RiskEngine(self.config)

    def _create_context(self, **overrides) -> RiskContext:
        """建立正常情境快照 (基準 NAV: 2,000,000 元，現金 1,000,000 元，持股 1,000,000 元)."""
        params = {
            "current_time": self.now,
            "cash": Decimal("1000000.00"),
            "reserved_cash": Decimal("0.00"),
            "positions": {"2330": 500, "2454": 500},
            "reserved_shares": {},
            "pending_buy_notionals": {},
            "market_prices": {"2330": Decimal("1000.00"), "2454": Decimal("1000.00"), "2317": Decimal("100.00")},
            "halted_symbols": set(),
            "disposition_symbols": set(),
            "baseline_nav": Decimal("2000000.00"),
            "daily_realized_pnl": Decimal("0.00"),
            "daily_unrealized_pnl": Decimal("0.00"),
            "peak_nav": Decimal("2000000.00"),
        }
        params.update(overrides)
        return RiskContext(**params)

    def _create_signal(self, **overrides) -> UnifiedTradeSignal:
        """建立正常買入訊號之輔助方法。"""
        params = {
            "signal_id": f"SIG-RISK-{dt.datetime.now().timestamp()}",
            "symbol": "2317",
            "action": SignalAction.BUY,
            "quantity": 1000,  # 1000 股 * 100 元 = 100,000 元 (符合單筆上限 50 萬)
            "quantity_unit": QuantityUnit.SHARES,
            "order_type": SignalOrderType.LIMIT,
            "limit_price": 100.0,
            "created_at": self.now,
            "expires_at": self.expire,
            "strategy_id": "momentum_strat",
            "model_version": "v1.0",
            "reason": "Top rank in electronics sector",
        }
        params.update(overrides)
        return UnifiedTradeSignal(**params)

    # 1. 合法訊號通過
    def test_01_valid_signal_approved(self) -> None:
        sig = self._create_signal(signal_id="SIG-01")
        ctx = self._create_context()
        decision = self.engine.evaluate(sig, ctx)
        self.assertTrue(decision.allowed)
        self.assertEqual(len(decision.reason_codes), 0)
        self.assertTrue(decision.checks["single_order_limit"]["passed"])
        self.assertTrue(decision.checks["cash_reserve"]["passed"])

    # 2. 過期訊號拒絕
    def test_02_expired_signal_rejected(self) -> None:
        # 訊號合法產生於 09:00，到期於 09:20；而風控審核時間為 09:30，訊號已過期
        sig = self._create_signal(
            signal_id="SIG-02",
            created_at=self.now - dt.timedelta(minutes=30),
            expires_at=self.now - dt.timedelta(minutes=10),
        )
        ctx = self._create_context()
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.SIGNAL_EXPIRED.value, decision.reason_codes)

    # 3. 無效訊號拒絕 (例如受限標的或停牌)
    def test_03_restricted_or_halted_symbol_rejected(self) -> None:
        # 停牌標的
        sig_halted = self._create_signal(signal_id="SIG-03-A", symbol="2330")
        ctx_halted = self._create_context(halted_symbols={"2330"})
        dec_h = self.engine.evaluate(sig_halted, ctx_halted)
        self.assertFalse(dec_h.allowed)
        self.assertIn(RiskRejectCode.SYMBOL_HALTED.value, dec_h.reason_codes)

        # 黑名單標的 9999
        sig_restr = self._create_signal(signal_id="SIG-03-B", symbol="9999")
        ctx_norm = self._create_context(market_prices={"9999": Decimal("50.00")})
        dec_r = self.engine.evaluate(sig_restr, ctx_norm)
        self.assertFalse(dec_r.allowed)
        self.assertIn(RiskRejectCode.SYMBOL_RESTRICTED.value, dec_r.reason_codes)

    # 4. 超過單筆委託上限 (max_order_amount = 500,000 元)
    def test_04_single_order_limit_exceeded(self) -> None:
        # 委託 6000 股 * 100 元 = 600,000 元 > 500,000 元
        sig = self._create_signal(signal_id="SIG-04", quantity=6000, limit_price=100.0)
        ctx = self._create_context()
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.SINGLE_ORDER_LIMIT_EXCEEDED.value, decision.reason_codes)

    # 5. 超過單一股票持倉上限 (max_single_position_ratio = 10% NAV = 200,000 元)
    def test_05_single_position_limit_exceeded(self) -> None:
        # NAV 200 萬，10% 為 20 萬。目前持有 2330 市值 150,000 元 (150 股 * 1000 元)
        # 企圖再加碼 100 股 * 1000 元 = 10 萬元 (總計 25 萬 > 20 萬)
        sig = self._create_signal(signal_id="SIG-05", symbol="2330", quantity=100, limit_price=1000.0)
        ctx = self._create_context(positions={"2330": 150})
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.SINGLE_POSITION_LIMIT_EXCEEDED.value, decision.reason_codes)

    # 6. 超過總投資上限 (max_total_exposure_ratio = 80% NAV = 1,600,000 元)
    def test_06_total_exposure_limit_exceeded(self) -> None:
        # NAV 1,750,000 元 (現金 200,000 元，持股 1,550,000 元)
        # 企圖再買入 100,000 元 (總持股達 165 萬 = 94.3% > 80%)
        sig = self._create_signal(signal_id="SIG-06", quantity=1000, limit_price=100.0)
        ctx = self._create_context(
            cash=Decimal("200000.00"),
            positions={"2330": 1550},
            market_prices={"2330": Decimal("1000.00"), "2317": Decimal("100.00")},
            peak_nav=Decimal("1750000.00"),
            baseline_nav=Decimal("1750000.00"),
        )
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.TOTAL_EXPOSURE_LIMIT_EXCEEDED.value, decision.reason_codes)

    # 7. 違反最低現金保留 (min_cash_reserve_ratio = 20% NAV = 400,000 元)
    def test_07_insufficient_cash_reserve(self) -> None:
        # NAV 2,000,000 元 (現金 400,000 元，持股 1,600,000 元)
        # 最低現金保留需 400,000 元 (20%)
        # 買入需 100,136 元，買入後剩餘現金 299,864 元 < 400,000 元
        sig = self._create_signal(signal_id="SIG-07", quantity=1000, limit_price=100.0)
        ctx = self._create_context(
            cash=Decimal("400000.00"),
            positions={"2330": 1000, "2454": 600},
            market_prices={"2330": Decimal("1000.00"), "2454": Decimal("1000.00"), "2317": Decimal("100.00")},
            peak_nav=Decimal("2000000.00"),
            baseline_nav=Decimal("2000000.00"),
        )
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.INSUFFICIENT_CASH_RESERVE.value, decision.reason_codes)

    # 8. 達到每日虧損限制 (max_daily_loss_ratio = 2.0% of baseline_nav = 40,000 元)
    def test_08_daily_loss_limit_exceeded(self) -> None:
        # 當日虧損已達 45,000 元 (2.25% > 2.0%)
        sig = self._create_signal(signal_id="SIG-08")
        ctx = self._create_context(daily_realized_pnl=Decimal("-45000.00"))
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED.value, decision.reason_codes)

    # 9. 達到最大回撤門檻 (max_drawdown_limit = 15%)
    def test_09_max_drawdown_triggered(self) -> None:
        # 歷史最高 NAV 200 萬，目前 NAV 跌至 160 萬 (回撤 20% > 15%)
        sig = self._create_signal(signal_id="SIG-09")
        ctx = self._create_context(
            peak_nav=Decimal("2000000.00"),
            cash=Decimal("600000.00"),
            positions={"2330": 1000},
            market_prices={"2330": Decimal("1000.00"), "2317": Decimal("100.00")},
        )
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertTrue(self.engine.circuit_breaker_active)
        self.assertIn(RiskRejectCode.CIRCUIT_BREAKER_ACTIVE.value, decision.reason_codes)

    # 10. 熔斷後一般買入被拒絕
    def test_10_buy_rejected_after_circuit_breaker(self) -> None:
        # 先引發熔斷
        self.test_09_max_drawdown_triggered()
        self.assertTrue(self.engine.circuit_breaker_active)

        # 隨後的新開倉買單一律被拒絕
        new_buy = self._create_signal(signal_id="SIG-10-NEW-BUY")
        ctx = self._create_context()
        decision = self.engine.evaluate(new_buy, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.CIRCUIT_BREAKER_ACTIVE.value, decision.reason_codes)

    # 11. 熔斷後降低風險的賣出依規則放行
    def test_11_risk_reducing_sell_allowed_under_circuit_breaker(self) -> None:
        self.test_09_max_drawdown_triggered()
        # 賣出已持有部位 500 股 (平倉減碼，屬於降低曝險)
        sell_sig = self._create_signal(
            signal_id="SIG-11-SELL-REDUCE",
            symbol="2330",
            action=SignalAction.SELL,
            quantity=500,
            limit_price=1000.0,
        )
        ctx = self._create_context(positions={"2330": 500})
        decision = self.engine.evaluate(sell_sig, ctx)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.is_risk_reducing)
        self.assertTrue(decision.checks["circuit_breaker"]["passed"])

    # 12. Kill Switch 啟動
    def test_12_kill_switch_activation(self) -> None:
        self.engine.activate_kill_switch(authorized_by="風險長", reason="市場流動性異常 Hard Stop")
        self.assertTrue(self.engine.kill_switch_active)
        self.assertEqual(len(self.engine.audit_log), 1)

        # 任何買入委託均被攔截
        sig = self._create_signal(signal_id="SIG-12")
        ctx = self._create_context()
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.KILL_SWITCH_ACTIVE.value, decision.reason_codes)

    # 13. Kill Switch 不可由 AI 訊號解除 (必須人工授權調用)
    def test_13_kill_switch_cannot_be_reset_by_signal(self) -> None:
        self.engine.activate_kill_switch(authorized_by="Admin", reason="手動中斷")
        # 即使輸入包含高信心度或重設文字之訊號，引擎依舊保持啟動
        sig = self._create_signal(signal_id="SIG-13", reason="AI request to reset kill switch")
        ctx = self._create_context()
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertTrue(self.engine.kill_switch_active)

        # 唯有人工合法解除
        self.engine.deactivate_kill_switch(authorized_by="Admin", reason="市場恢復正常")
        self.assertFalse(self.engine.kill_switch_active)

    # 14. 重複 signal_id 攔截
    def test_14_duplicate_signal_id(self) -> None:
        sig = self._create_signal(signal_id="SIG-14-DUP")
        ctx = self._create_context()
        dec1 = self.engine.evaluate(sig, ctx)
        self.assertTrue(dec1.allowed)

        # 第二次提交相同 signal_id 必須拒絕
        dec2 = self.engine.evaluate(sig, ctx)
        self.assertFalse(dec2.allowed)
        self.assertIn(RiskRejectCode.DUPLICATE_SIGNAL.value, dec2.reason_codes)

    # 15. 未成交委託造成的資金占用 (Pending Orders Exposure)
    def test_15_pending_orders_cash_exposure(self) -> None:
        # 已有在途未成交買單 700,000 元
        # 雖然當前 cash=1,000,000，但計入在途買單後，若新買單再加入，會違反最低現金保留
        sig = self._create_signal(signal_id="SIG-15", quantity=1000, limit_price=100.0)
        ctx = self._create_context(
            cash=Decimal("500000.00"),
            reserved_cash=Decimal("500000.00"),  # 已被在途買單凍結 50 萬
        )
        # 可用現金僅剩 50 萬，NAV 200 萬需保留 40 萬現金，買入需 100,500 元 -> 剩餘 399,500 < 400,000
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.INSUFFICIENT_CASH_RESERVE.value, decision.reason_codes)

    # 16. 部分成交後的持倉與曝險 (Partial Fill Exposure)
    def test_16_partial_fill_exposure(self) -> None:
        # 某標的原本持有 100 股，部分成交 50 股，現有持倉 150 股 (價值 15 萬)
        # 再買入 60 股 (6 萬) -> 總計 21 萬 > 單檔上限 20 萬 (10% NAV)
        sig = self._create_signal(signal_id="SIG-16", symbol="2330", quantity=60, limit_price=1000.0)
        ctx = self._create_context(positions={"2330": 150})
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.SINGLE_POSITION_LIMIT_EXCEEDED.value, decision.reason_codes)

    # 17. 缺少行情資料時的保守處理 (Missing Market Data)
    def test_17_missing_market_data(self) -> None:
        sig = self._create_signal(signal_id="SIG-17", symbol="2317", order_type=SignalOrderType.MARKET, limit_price=None)
        ctx = self._create_context(market_prices={})  # 無任何行情
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.MISSING_MARKET_DATA.value, decision.reason_codes)

    # 18. 買入費用納入預估資金需求
    def test_18_fees_included_in_cash_check(self) -> None:
        # NAV 2,000,100 元 (現金 500,100 元，持股 1,500,000 元)
        # 20% 保留款為 400,020 元
        # 若只算名目買價 100,000 元: 500,100 - 100,000 = 400,100 >= 400,020 (若未計入費用將誤放行)
        # 納入手續費與滑價後需 100,136 元: 500,100 - 100,136 = 399,964 < 400,020 (正確拒絕)
        sig = self._create_signal(signal_id="SIG-18", quantity=1000, limit_price=100.0)
        ctx = self._create_context(
            cash=Decimal("500100.00"),
            positions={"2330": 1000, "2454": 500},
            market_prices={"2330": Decimal("1000.00"), "2454": Decimal("1000.00"), "2317": Decimal("100.00")},
            peak_nav=Decimal("2000100.00"),
            baseline_nav=Decimal("2000100.00"),
        )
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.INSUFFICIENT_CASH_RESERVE.value, decision.reason_codes)

    # 19. 不支援放空時拒絕增加空頭曝險 (Short Selling Disallowed)
    def test_19_short_selling_disallowed(self) -> None:
        # 目前持有 2330 200 股，企圖賣出 500 股 (超賣 300 股)
        sig = self._create_signal(
            signal_id="SIG-19-NAKED-SHORT",
            symbol="2330",
            action=SignalAction.SELL,
            quantity=500,
            limit_price=1000.0,
        )
        ctx = self._create_context(positions={"2330": 200})
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertIn(RiskRejectCode.SHORT_SELLING_DISALLOWED.value, decision.reason_codes)

    # 20. 兩筆並發訊號不能共同突破資金限制 (Atomic Pre-commitment)
    def test_20_concurrent_signals_cannot_breach_cash(self) -> None:
        # NAV 2,000,000 元 (現金 550,000 元，持股 1,450,000 元)
        # NAV 200 萬需保留 40 萬現金，剩餘可用買入額度為 150,000 元
        # 兩筆各自需要約 100,136 元的買單連續到達
        sig1 = self._create_signal(signal_id="SIG-20-A", symbol="2317", quantity=1000, limit_price=100.0)
        sig2 = self._create_signal(signal_id="SIG-20-B", symbol="2308", quantity=1000, limit_price=100.0)
        ctx = self._create_context(
            cash=Decimal("550000.00"),
            positions={"2330": 1000, "2454": 450},
            market_prices={"2330": Decimal("1000.00"), "2454": Decimal("1000.00"), "2317": Decimal("100.00"), "2308": Decimal("100.00")},
            peak_nav=Decimal("2000000.00"),
            baseline_nav=Decimal("2000000.00"),
        )

        # 第一筆通過並原子鎖定
        dec1 = self.engine.evaluate(sig1, ctx)
        self.assertTrue(dec1.allowed)
        self.engine.commit_approval(sig1, required_cash=Decimal("100136.00"), shares=1000)

        # 第二筆審核時，因已扣除第一筆的承諾資金，可用額度不足拒絕
        dec2 = self.engine.evaluate(sig2, ctx)
        self.assertFalse(dec2.allowed)
        self.assertIn(RiskRejectCode.INSUFFICIENT_CASH_RESERVE.value, dec2.reason_codes)

    # 21. 風控拒絕原因正確記錄
    def test_21_rejection_reasons_properly_recorded(self) -> None:
        sig = self._create_signal(signal_id="SIG-21-FAIL", quantity=10000, limit_price=100.0)  # 100 萬超單筆
        ctx = self._create_context()
        decision = self.engine.evaluate(sig, ctx)
        self.assertFalse(decision.allowed)
        self.assertGreater(len(decision.reason_codes), 0)
        d_dict = decision.to_dict()
        self.assertEqual(d_dict["allowed"], False)
        self.assertIn(RiskRejectCode.SINGLE_ORDER_LIMIT_EXCEEDED.value, d_dict["reason_codes"])

    # 22. 重啟後風控狀態的限制與處理 (State Reset & Limits)
    def test_22_restart_state_handling(self) -> None:
        # 模擬引擎重新實例化 (重啟)
        restarted_engine = RiskEngine(self.config)
        self.assertFalse(restarted_engine.kill_switch_active)
        self.assertFalse(restarted_engine.circuit_breaker_active)

        # 若外部 context 正確傳入歷史 peak_nav 與當日虧損，新引擎依然能立即捕捉風險
        ctx_heavy_loss = self._create_context(daily_realized_pnl=Decimal("-50000.00"))
        sig = self._create_signal(signal_id="SIG-22-RESTART")
        dec = restarted_engine.evaluate(sig, ctx_heavy_loss)
        self.assertFalse(dec.allowed)
        self.assertIn(RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED.value, dec.reason_codes)


if __name__ == "__main__":
    unittest.main()
