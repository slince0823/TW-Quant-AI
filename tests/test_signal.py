"""統一交易訊號單元測試 (tests/test_signal.py).

完全離線執行，不依賴外部 API、網路或真實券商。
全面驗證 UnifiedTradeSignal 的 16 項核心規範與防禦邏輯。
"""

from __future__ import annotations

import datetime as dt
import math
import sys
import unittest

from src.tw_quant.contracts import (
    DuplicateSignalError,
    QuantityUnit,
    SignalAction,
    SignalDeduplicator,
    SignalMode,
    SignalOrderType,
    UnifiedTradeSignal,
    lots_to_shares,
    shares_to_lots,
)


class TestUnifiedTradeSignal(unittest.TestCase):
    """UnifiedTradeSignal 規格與防禦性校驗測試套件。"""

    def setUp(self) -> None:
        self.taipei_tz = dt.timezone(dt.timedelta(hours=8))
        self.utc_tz = dt.timezone.utc
        self.now = dt.datetime(2026, 10, 21, 9, 0, 0, tzinfo=self.taipei_tz)
        self.expires = dt.datetime(2026, 10, 21, 13, 30, 0, tzinfo=self.taipei_tz)

    def _create_valid_signal(self, **overrides) -> UnifiedTradeSignal:
        """建立正常有效訊號之輔助方法。"""
        params = {
            "signal_id": "SIG-20261021-0001",
            "symbol": "2330",
            "action": SignalAction.BUY,
            "quantity": 1000,
            "quantity_unit": QuantityUnit.SHARES,
            "order_type": SignalOrderType.LIMIT,
            "limit_price": 1000.0,
            "created_at": self.now,
            "expires_at": self.expires,
            "strategy_id": "alpha_158_momentum",
            "model_version": "v1.2.0",
            "reason": "Top 1 score in cross-sectional ranking",
            "mode": SignalMode.PAPER,
            "schema_version": "1.0.0",
        }
        params.update(overrides)
        return UnifiedTradeSignal(**params)

    # 1. 正常 BUY 訊號
    def test_normal_buy_signal(self) -> None:
        sig = self._create_valid_signal(action=SignalAction.BUY, limit_price=995.0)
        self.assertEqual(sig.action, SignalAction.BUY)
        self.assertEqual(sig.symbol, "2330")
        self.assertEqual(sig.quantity, 1000)
        self.assertEqual(sig.limit_price, 995.0)
        self.assertEqual(sig.mode, SignalMode.PAPER)

    # 2. 正常 SELL 訊號
    def test_normal_sell_signal(self) -> None:
        sig = self._create_valid_signal(
            action=SignalAction.SELL,
            quantity=2000,
            order_type=SignalOrderType.MARKET,
            limit_price=None,
        )
        self.assertEqual(sig.action, SignalAction.SELL)
        self.assertEqual(sig.order_type, SignalOrderType.MARKET)
        self.assertIsNone(sig.limit_price)

    # 3. 無效股票代碼
    def test_invalid_symbol(self) -> None:
        invalid_symbols = ["", "   ", "TOOLONG1234", "$$$", "2330@TW", "1"]
        for sym in invalid_symbols:
            with self.subTest(sym=sym):
                with self.assertRaises(ValueError):
                    self._create_valid_signal(symbol=sym)

    # 4. 無效 action
    def test_invalid_action(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            self._create_valid_signal(action="HOLD_INVALID")  # type: ignore

    # 5. 零或負數數量
    def test_zero_or_negative_quantity(self) -> None:
        for q in [0, -1, -500, -0.001]:
            with self.subTest(quantity=q):
                with self.assertRaises(ValueError):
                    self._create_valid_signal(quantity=q)

    # 6. 布林值被誤當成數量 (bool 必須被拒絕)
    def test_bool_rejected_as_quantity(self) -> None:
        with self.assertRaises(TypeError):
            self._create_valid_signal(quantity=True)
        with self.assertRaises(TypeError):
            self._create_valid_signal(quantity=False)

    # 7. 缺少必要欄位
    def test_missing_required_fields_in_from_dict(self) -> None:
        data = {
            "signal_id": "SIG-001",
            "symbol": "2330",
            # 缺少 action, quantity 等
        }
        with self.assertRaises(ValueError) as ctx:
            UnifiedTradeSignal.from_dict(data)
        self.assertIn("缺少必要欄位", str(ctx.exception))

    # 8. 無效價格
    def test_invalid_limit_price(self) -> None:
        # 限價單未給價格
        with self.assertRaises(ValueError):
            self._create_valid_signal(order_type=SignalOrderType.LIMIT, limit_price=None)
        # 限價單價格 <= 0
        with self.assertRaises(ValueError):
            self._create_valid_signal(order_type=SignalOrderType.LIMIT, limit_price=-10.0)
        with self.assertRaises(ValueError):
            self._create_valid_signal(order_type=SignalOrderType.LIMIT, limit_price=0.0)
        # 市價單誤填價格
        with self.assertRaises(ValueError):
            self._create_valid_signal(order_type=SignalOrderType.MARKET, limit_price=100.0)

    # 9. NaN 與無限大價格
    def test_nan_and_inf_price(self) -> None:
        for bad_price in [math.nan, math.inf, -math.inf]:
            with self.subTest(price=bad_price):
                with self.assertRaises(ValueError):
                    self._create_valid_signal(
                        order_type=SignalOrderType.LIMIT, limit_price=bad_price
                    )
        # 布林值價格
        with self.assertRaises(TypeError):
            self._create_valid_signal(order_type=SignalOrderType.LIMIT, limit_price=True)

    # 10. 張與股的轉換
    def test_lots_and_shares_conversion(self) -> None:
        # lots_to_shares
        self.assertEqual(lots_to_shares(1), 1000)
        self.assertEqual(lots_to_shares(5), 5000)
        self.assertEqual(lots_to_shares(1.5), 1500)
        # 無效張數
        with self.assertRaises(TypeError):
            lots_to_shares(True)
        with self.assertRaises(ValueError):
            lots_to_shares(0)
        with self.assertRaises(ValueError):
            lots_to_shares(-2)
        with self.assertRaises(ValueError):
            lots_to_shares(1.0001)  # 換算後 1000.1 股，非整數

        # shares_to_lots
        self.assertEqual(shares_to_lots(1000), 1.0)
        self.assertEqual(shares_to_lots(2500), 2.5)
        with self.assertRaises(TypeError):
            shares_to_lots(True)

        # 訊號實例轉換 to_shares()
        sig_lots = self._create_valid_signal(
            quantity=3, quantity_unit=QuantityUnit.LOTS
        )
        sig_shares = sig_lots.to_shares()
        self.assertEqual(sig_shares.quantity, 3000)
        self.assertEqual(sig_shares.quantity_unit, QuantityUnit.SHARES)

    # 11. 訊號過期判定
    def test_signal_expiration(self) -> None:
        sig = self._create_valid_signal(
            created_at=self.now,
            expires_at=self.now + dt.timedelta(hours=2),
        )
        before_expire = self.now + dt.timedelta(hours=1)
        after_expire = self.now + dt.timedelta(hours=3)

        self.assertFalse(sig.is_expired(as_of=before_expire))
        self.assertTrue(sig.is_expired(as_of=after_expire))
        # 剛好到期時
        self.assertTrue(sig.is_expired(as_of=sig.expires_at))

    # 12. 到期時間早於或等於產生時間
    def test_expires_before_created(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._create_valid_signal(
                created_at=self.now,
                expires_at=self.now - dt.timedelta(minutes=1),
            )
        self.assertIn("必須嚴格晚於", str(ctx.exception))

        with self.assertRaises(ValueError):
            self._create_valid_signal(
                created_at=self.now,
                expires_at=self.now,  # 同一時間
            )

    # 13. 缺少時區的時間 (naive datetime)
    def test_naive_datetime_rejected(self) -> None:
        naive_dt = dt.datetime(2026, 10, 21, 9, 0, 0)  # 無 tzinfo
        aware_dt = dt.datetime(2026, 10, 21, 13, 0, 0, tzinfo=self.taipei_tz)

        # created_at naive
        with self.assertRaises(ValueError) as ctx:
            self._create_valid_signal(created_at=naive_dt, expires_at=aware_dt)
        self.assertIn("timezone-aware", str(ctx.exception))

        # expires_at naive
        with self.assertRaises(ValueError) as ctx:
            self._create_valid_signal(created_at=aware_dt, expires_at=naive_dt)
        self.assertIn("timezone-aware", str(ctx.exception))

    # 14. 重複訊號識別碼防護 (SignalDeduplicator)
    def test_duplicate_signal_id(self) -> None:
        dedup = SignalDeduplicator(max_size=100)
        sig_id = "SIG-UNIQUE-001"

        self.assertFalse(dedup.is_duplicate(sig_id))
        dedup.register(sig_id)
        self.assertTrue(dedup.is_duplicate(sig_id))

        # 再次註冊應拋出 DuplicateSignalError
        with self.assertRaises(DuplicateSignalError):
            dedup.register(sig_id)

    # 15. 未知欄位防護 (from_dict)
    def test_unknown_fields_rejected_in_from_dict(self) -> None:
        valid_dict = self._create_valid_signal().to_dict()
        valid_dict["malicious_payload"] = "hack_attempt"
        valid_dict["unexpected_flag"] = 123

        with self.assertRaises(ValueError) as ctx:
            UnifiedTradeSignal.from_dict(valid_dict)
        self.assertIn("包含未定義之未知欄位", str(ctx.exception))

    # 16. T 日盤後訊號與成交時間的限制 (Lookahead Bias 防禦)
    def test_timing_lookahead_bias_guard(self) -> None:
        # T 日 15:00 盤後產生之訊號
        post_market_signal = self._create_valid_signal(
            created_at=dt.datetime(2026, 10, 21, 15, 0, 0, tzinfo=self.taipei_tz),
            expires_at=dt.datetime(2026, 10, 22, 13, 30, 0, tzinfo=self.taipei_tz),
        )

        # 企圖在 T 日 13:30 收盤價成交 (Lookahead Bias 違規)
        cheating_exec_time = dt.datetime(2026, 10, 21, 13, 30, 0, tzinfo=self.taipei_tz)
        with self.assertRaises(ValueError) as ctx:
            post_market_signal.validate_execution_timing(cheating_exec_time)
        self.assertIn("Lookahead Bias", str(ctx.exception))

        # 企圖在早於訊號產生時間成交 (時序因果違規)
        earlier_exec_time = dt.datetime(2026, 10, 21, 14, 0, 0, tzinfo=self.taipei_tz)
        with self.assertRaises(ValueError) as ctx:
            post_market_signal.validate_execution_timing(earlier_exec_time)
        self.assertIn("時序因果律", str(ctx.exception))

        # 正確合法成交時間：T+1 日 09:05:00 開盤撮合
        valid_exec_time = dt.datetime(2026, 10, 22, 9, 5, 0, tzinfo=self.taipei_tz)
        # 不應拋出異常
        post_market_signal.validate_execution_timing(valid_exec_time)

    # 17. 模式必須固定為 PAPER
    def test_mode_must_be_paper(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._create_valid_signal(mode="LIVE")  # type: ignore
        self.assertIn("PAPER", str(ctx.exception))

    # 18. to_dict 與 from_dict 往返完整性
    def test_roundtrip_serialization(self) -> None:
        sig = self._create_valid_signal()
        d = sig.to_dict()
        restored = UnifiedTradeSignal.from_dict(d)
        self.assertEqual(sig.signal_id, restored.signal_id)
        self.assertEqual(sig.symbol, restored.symbol)
        self.assertEqual(sig.action, restored.action)
        self.assertEqual(sig.quantity, restored.quantity)
        self.assertEqual(sig.limit_price, restored.limit_price)
        self.assertEqual(sig.created_at, restored.created_at)
        self.assertEqual(sig.expires_at, restored.expires_at)


if __name__ == "__main__":
    unittest.main()
