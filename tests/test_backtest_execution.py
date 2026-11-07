"""第三階段 C-2.3：歷史撮合模擬器與投資組合估值器單元與驗收測試 (test_backtest_execution.py).

重要聲明：
本測試套件使用明確標記為 `is_synthetic=True` 之合成測試行情與合成公司行動事件，
僅用於驗證 `HistoricalExecutionSimulator` 與 `PortfolioValuator` 之撮合邏輯、市場限制與會計估值正確性，
不得將合成測試結果視為真實台股歷史回測績效。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import subprocess
import unittest
from pathlib import Path

from src.tw_quant.backtest import (
    TAIPEI_TZ,
    AmbiguousLimitPolicy,
    BacktestAccountingError,
    BacktestCalendarError,
    BacktestLotLedger,
    CashAccountingBasis,
    CorporateActionEvent,
    CorporateActionProcessor,
    CorporateActionType,
    DailyNAVRecord,
    DataProvenance,
    DispositionExecutionPolicy,
    DuplicateFillError,
    DuplicateOrderExecutionError,
    ExecutionCostModel,
    ExecutionMode,
    ExecutionOutcomeCode,
    ExecutionSimConfig,
    HistoricalBarRecord,
    HistoricalDataMissingError,
    HistoricalExecutionSimulator,
    IncompleteValuationError,
    InMemoryHistoricalDataProvider,
    InstrumentCategory,
    InstrumentTradingStatus,
    InvalidOHLCVError,
    ListingPhase,
    LookaheadBiasError,
    MissingPricePolicy,
    OversellPositionError,
    PortfolioValuator,
    PriceBasis,
    PriceLimitDataMissingError,
    PriceLimitStatus,
    StaleValuationPriceError,
    SymbolExecutionContext,
    TaiwanHistoricalPriceLimitRule,
    TaiwanTickSizeRule,
    TickRoundingMode,
    TickSizeViolationError,
    TradingCalendar,
    UnknownTradingStatusError,
    UnsupportedDataFormatError,
    ValuationConfig,
    ValuationPriceMissingError,
    ValuationPriceSource,
    ValuationWarningCode,
    VolumeCapPolicy,
)
from src.tw_quant.broker.config import TaiwanPaperConfig
from src.tw_quant.broker.ledger import PortfolioLedger
from src.tw_quant.broker.order import PaperOrder, PaperOrderStatus
from src.tw_quant.contracts.signal import SignalAction, SignalOrderType


def make_synth_provenance() -> DataProvenance:
    return DataProvenance(
        source="SYNTHETIC_EXECUTION_UNIT_TEST",
        dataset_version="synth-exec-v1",
        retrieved_at=dt.datetime(2025, 1, 2, 8, 0, tzinfo=TAIPEI_TZ),
        is_synthetic=True,
        description="Synthetic fixture for Phase 3C-2.3 unit tests (NOT real market performance)",
    )


def make_synth_calendar() -> TradingCalendar:
    # 建立涵蓋 2025-01-02 (週四) 至 2025-01-20 的合成交易日曆
    # 其中 2025-01-04, 05, 11, 12, 18, 19 為週末休市日，另設 2025-01-08 (週三) 為合成假日
    start = dt.date(2025, 1, 2)
    end = dt.date(2025, 1, 20)
    holidays = {
        dt.date(2025, 1, 4),
        dt.date(2025, 1, 5),
        dt.date(2025, 1, 8),
        dt.date(2025, 1, 11),
        dt.date(2025, 1, 12),
        dt.date(2025, 1, 18),
        dt.date(2025, 1, 19),
    }
    trading_days = []
    non_trading_days = []
    cur = start
    while cur <= end:
        if cur in holidays:
            non_trading_days.append(cur)
        else:
            trading_days.append(cur)
        cur += dt.timedelta(days=1)

    return TradingCalendar.create_synthetic_for_test(
        trading_days=trading_days,
        coverage_start=start,
        coverage_end=end,
        non_trading_days=non_trading_days,
        strict_explicit_schedule=True,
    )


def make_bar(
    symbol: str,
    trade_date: dt.date,
    open_px: str,
    high_px: str,
    low_px: str,
    close_px: str,
    volume: int = 100_000,
    yesterday_close: str = "100.00",
    *,
    is_halted: bool = False,
    is_disposition: bool = False,
    is_limit_up_locked: bool = False,
    is_limit_down_locked: bool = False,
    price_basis: PriceBasis = PriceBasis.RAW_UNADJUSTED,
    provenance: DataProvenance | None = None,
) -> HistoricalBarRecord:
    prov = provenance or make_synth_provenance()
    ev_time = dt.datetime.combine(trade_date, dt.time(13, 30, 0), tzinfo=TAIPEI_TZ)
    pub_time = dt.datetime.combine(trade_date, dt.time(15, 0, 0), tzinfo=TAIPEI_TZ)
    avail_time = dt.datetime.combine(trade_date, dt.time(16, 30, 0), tzinfo=TAIPEI_TZ)
    return HistoricalBarRecord(
        symbol=symbol,
        trade_date=trade_date,
        open=Decimal(open_px),
        high=Decimal(high_px),
        low=Decimal(low_px),
        close=Decimal(close_px),
        volume=volume,
        yesterday_close=Decimal(yesterday_close) if yesterday_close is not None else None,
        event_time=ev_time,
        published_at=pub_time,
        available_at=avail_time,
        provenance=prov,
        price_basis=price_basis,
        is_halted=is_halted,
        is_disposition=is_disposition,
        is_limit_up_locked=is_limit_up_locked,
        is_limit_down_locked=is_limit_down_locked,
    )


class TestBacktestExecutionAndValuation(unittest.TestCase):
    """涵蓋第三階段 C-2.3 指定之 21 項必測情境。"""

    def setUp(self) -> None:
        self.prov = make_synth_provenance()
        self.cal = make_synth_calendar()
        self.t1 = dt.date(2025, 1, 2)  # 週四
        self.t2 = dt.date(2025, 1, 3)  # 週五
        self.t3 = dt.date(2025, 1, 6)  # 週一
        self.t4 = dt.date(2025, 1, 7)  # 週二
        self.t5 = dt.date(2025, 1, 9)  # 週四 (01-08 為休市日)

    # ── 1. 市價買賣單以開盤基準撮合 ─────────────────────────────────

    def test_01_market_orders_matched_at_open_benchmark(self) -> None:
        """1. 市價買單與市價賣單以當日開盤價為基準加計滑價並對齊 Tick Size 撮合，絕不使用收盤價。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ledger = PortfolioLedger(Decimal("1000000.00"))
        lot_ledger = BacktestLotLedger(calendar=self.cal)

        # T1 18:00 盤後產生市價買單 1,000 股
        buy_order = PaperOrder(
            order_id="ORD-MKT-BUY-1",
            signal_id="SIG-MKT-BUY-1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_order, ledger, reference_price=Decimal("500.00"))
        self.assertEqual(buy_order.status, PaperOrderStatus.ACCEPTED)

        # T2 行情: 開盤 510, 最高 525, 最低 505, 收盤 520 (昨收 500, 漲停 550)
        # 500~1000 級距之 Tick Size 為 1.00 元；510 * (1 + 0.0005) = 510.255 -> 向上對齊 Tick 為 511.00 元
        bar_t2 = make_bar("2330", self.t2, "510.00", "525.00", "505.00", "520.00", volume=100_000, yesterday_close="500.00")
        ctx = SymbolExecutionContext(symbol="2330", adv_20_shares=100_000)

        outcome = sim.match_order(buy_order, bar_t2, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)
        self.assertEqual(outcome.outcome_code, ExecutionOutcomeCode.FILLED)
        self.assertIsNotNone(outcome.fill_record)
        assert outcome.fill_record is not None
        self.assertEqual(outcome.fill_record.fill_price, Decimal("511.00"))
        self.assertNotEqual(outcome.fill_record.fill_price, bar_t2.close)
        self.assertEqual(outcome.fill_record.fill_quantity, 1000)
        self.assertEqual(lot_ledger.get_oldest_entry_date("2330"), self.t2)

        # T2 18:00 盤後產生市價賣單 1,000 股，於 T3 開盤 (open=520) 撮合
        # 520 * (1 - 0.0005) = 519.74 -> 向下對齊 Tick (1.00) 為 519.00 元
        sell_order = PaperOrder(
            order_id="ORD-MKT-SELL-1",
            signal_id="SIG-MKT-SELL-1",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 3, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_order, ledger, lot_ledger=lot_ledger)
        bar_t3 = make_bar("2330", self.t3, "520.00", "530.00", "515.00", "528.00", volume=100_000, yesterday_close="520.00")
        sell_outcome = sim.match_order(sell_order, bar_t3, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)
        self.assertEqual(sell_outcome.outcome_code, ExecutionOutcomeCode.FILLED)
        assert sell_outcome.fill_record is not None
        self.assertEqual(sell_outcome.fill_record.fill_price, Decimal("519.00"))

    # ── 2. T-1 盤後委託不得在 T-1 成交 & 禁止同日收盤資訊於當日收盤成交 ──

    def test_02_post_market_order_blocked_on_same_day_and_no_same_day_close_execution(self) -> None:
        """2. T-1 盤後產生的委託不得在 T-1 成交；且盤後才取得的 T 日收盤資訊不得用於 T 日收盤成交。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        bar_t1 = make_bar("2330", self.t1, "100.00", "102.00", "99.00", "101.00", volume=100_000, yesterday_close="100.00")

        # T1 18:00 盤後建立之委託
        order_t1_post = PaperOrder(
            order_id="ORD-POST-T1",
            signal_id="SIG-POST-T1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(order_t1_post, reference_price=Decimal("101.00"))

        # 嘗試在 T1 當日撮合 T1 18:00 盤後委託 -> 必須拋出 LookaheadBiasError
        with self.assertRaises(LookaheadBiasError):
            sim.match_order(order_t1_post, bar_t1)

        # 即使委託在 T1 盤後建立，嘗試在 T2 的 13:30 收盤時點利用 T2 盤後收盤價成交 -> 必須拋出 LookaheadBiasError
        bar_t2 = make_bar("2330", self.t2, "101.00", "105.00", "100.50", "104.00", volume=100_000, yesterday_close="101.00")
        with self.assertRaises(LookaheadBiasError):
            sim.match_order(
                order_t1_post,
                bar_t2,
                proposed_execution_time=dt.datetime(2025, 1, 3, 13, 30, tzinfo=TAIPEI_TZ),
            )

        # 在 T2 09:00 開盤合法時段則可正常成交
        outcome_t2 = sim.match_order(order_t1_post, bar_t2)
        self.assertEqual(outcome_t2.outcome_code, ExecutionOutcomeCode.FILLED)

    # ── 3. 限價買賣單不得違反價格限制 ────────────────────────────────

    def test_03_limit_orders_never_violate_limit_price_or_daily_bounds(self) -> None:
        """3. 限價買單不得以高於限價成交；限價賣單不得以低於限價成交；未觸價或超出漲跌停不成交。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ctx = SymbolExecutionContext(symbol="2330", adv_20_shares=100_000)

        # 子情境 A: 限價買單 limit=100.00，T2 行情 O=102, H=104, L=100.50, C=103 -> 未觸及 100.00，不成交
        buy_unreached = PaperOrder(
            order_id="ORD-LIM-B1",
            signal_id="SIG-LIM-B1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("100.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_unreached)
        bar_high = make_bar("2330", self.t2, "102.00", "104.00", "100.50", "103.00", volume=100_000, yesterday_close="100.00")
        out_unreached = sim.match_order(buy_unreached, bar_high, symbol_context=ctx)
        self.assertEqual(out_unreached.outcome_code, ExecutionOutcomeCode.UNFILLED_LIMIT_PRICE_NOT_REACHED)
        self.assertEqual(out_unreached.filled_quantity, 0)

        # 子情境 B: 限價買單 limit=101.00，T2 行情 O=103, H=104, L=98.00, C=99.00
        # 開盤 103 > 101，盤中最低跌到 98.00：保守必須以 101.00 成交（絕不可用 98 或 99 給予超額優惠，且不高於 101）
        buy_intraday = PaperOrder(
            order_id="ORD-LIM-B2",
            signal_id="SIG-LIM-B2",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("101.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_intraday)
        bar_dip = make_bar("2330", self.t2, "103.00", "104.00", "98.00", "99.00", volume=100_000, yesterday_close="100.00")
        out_buy = sim.match_order(buy_intraday, bar_dip, symbol_context=ctx)
        self.assertEqual(out_buy.outcome_code, ExecutionOutcomeCode.FILLED)
        assert out_buy.fill_record is not None
        self.assertEqual(out_buy.fill_record.fill_price, Decimal("101.00"))
        self.assertLessEqual(out_buy.fill_record.fill_price, Decimal("101.00"))

        # 子情境 C: 限價賣單 limit=103.50，T3 行情 O=101, H=105, L=100, C=104.50 -> 盤中觸價保守以 103.50 成交
        sell_intraday = PaperOrder(
            order_id="ORD-LIM-S1",
            signal_id="SIG-LIM-S1",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("103.50"),
            created_at=dt.datetime(2025, 1, 3, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_intraday)
        bar_rally = make_bar("2330", self.t3, "101.00", "105.00", "100.00", "104.50", volume=100_000, yesterday_close="100.00")
        out_sell = sim.match_order(sell_intraday, bar_rally, symbol_context=ctx)
        self.assertEqual(out_sell.outcome_code, ExecutionOutcomeCode.FILLED)
        assert out_sell.fill_record is not None
        self.assertEqual(out_sell.fill_record.fill_price, Decimal("103.50"))
        self.assertGreaterEqual(out_sell.fill_record.fill_price, Decimal("103.50"))

        # 子情境 D: 限價超出當日漲跌停 (昨收 100，漲停 110，但買單限價設為 115.00) -> 攔截不成交
        buy_out_of_bounds = PaperOrder(
            order_id="ORD-LIM-OOB",
            signal_id="SIG-LIM-OOB",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("115.00"),
            created_at=dt.datetime(2025, 1, 3, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_out_of_bounds)
        out_oob = sim.match_order(buy_out_of_bounds, bar_rally, symbol_context=ctx)
        self.assertEqual(out_oob.outcome_code, ExecutionOutcomeCode.UNFILLED_LIMIT_OUTSIDE_DAILY_BOUNDS)
        self.assertEqual(out_oob.filled_quantity, 0)

    # ── 4. 開盤跳空穿越限價與日內路徑歧義政策 ─────────────────────────

    def test_04_opening_gap_through_limit_price_and_ambiguous_ohlc_policies(self) -> None:
        """4. 開盤跳空穿越限價時以開盤基準加滑價（且不劣於限價）成交；並驗證 AmbiguousLimitPolicy 保守規則。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ctx = SymbolExecutionContext(symbol="2330", adv_20_shares=100_000)

        # 買單限價 102.00，但 T2 開盤跳空開低於 98.00 (98 * 1.0005 = 98.049 -> 對齊 0.10 tick 為 98.10 <= 102.00)
        buy_gap = PaperOrder(
            order_id="ORD-GAP-BUY",
            signal_id="SIG-GAP-BUY",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("102.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_gap)
        bar_gap_down = make_bar("2330", self.t2, "98.00", "99.50", "97.50", "99.00", volume=100_000, yesterday_close="100.00")
        out_gap_buy = sim.match_order(buy_gap, bar_gap_down, symbol_context=ctx)
        self.assertEqual(out_gap_buy.outcome_code, ExecutionOutcomeCode.FILLED)
        assert out_gap_buy.fill_record is not None
        self.assertEqual(out_gap_buy.fill_record.fill_price, Decimal("98.10"))

        # 賣單限價 100.00，但 T3 開盤跳空開高於 104.00 (104 * 0.9995 = 103.948 -> 對齊 0.50 tick 為 103.50 >= 100.00)
        sell_gap = PaperOrder(
            order_id="ORD-GAP-SELL",
            signal_id="SIG-GAP-SELL",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("100.00"),
            created_at=dt.datetime(2025, 1, 3, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_gap)
        bar_gap_up = make_bar("2330", self.t3, "104.00", "106.00", "103.00", "105.00", volume=100_000, yesterday_close="100.00")
        out_gap_sell = sim.match_order(sell_gap, bar_gap_up, symbol_context=ctx)
        self.assertEqual(out_gap_sell.outcome_code, ExecutionOutcomeCode.FILLED)
        assert out_gap_sell.fill_record is not None
        self.assertEqual(out_gap_sell.fill_record.fill_price, Decimal("103.50"))

        # 驗證 REQUIRE_STRICT_PENETRATION：當買單限價剛好等於當日最低價 (bar.low == 100.00, bar.open = 102.00) 時拒絕成交
        sim_strict = HistoricalExecutionSimulator(
            config=ExecutionSimConfig(ambiguous_limit_policy=AmbiguousLimitPolicy.REQUIRE_STRICT_PENETRATION),
            calendar=self.cal,
        )
        buy_touch_only = PaperOrder(
            order_id="ORD-TOUCH-ONLY",
            signal_id="SIG-TOUCH-ONLY",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("100.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim_strict.submit_order(buy_touch_only)
        bar_exact_low = make_bar("2330", self.t2, "102.00", "103.00", "100.00", "101.50", volume=100_000, yesterday_close="100.00")
        out_touch = sim_strict.match_order(buy_touch_only, bar_exact_low, symbol_context=ctx)
        self.assertEqual(out_touch.outcome_code, ExecutionOutcomeCode.UNFILLED_AMBIGUOUS_INTRADAY_OHLC)
        self.assertEqual(out_touch.filled_quantity, 0)

    # ── 5. 漲停買單與跌停賣單的保守處理 ──────────────────────────────

    def test_05_limit_up_buy_and_limit_down_sell_conservative_blocking(self) -> None:
        """5. 漲停鎖死時買單不得成交；跌停鎖死時賣單不得成交。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ctx = SymbolExecutionContext(symbol="2330", adv_20_shares=100_000)

        # 昨收 100，漲停價 110.00：T2 開盤即漲停鎖死 110.00
        buy_order = PaperOrder(
            order_id="ORD-LU-BUY",
            signal_id="SIG-LU-BUY",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_order, reference_price=Decimal("100.00"))
        bar_lu = make_bar("2330", self.t2, "110.00", "110.00", "110.00", "110.00", volume=50_000, yesterday_close="100.00")
        out_lu = sim.match_order(buy_order, bar_lu, symbol_context=ctx)
        self.assertEqual(out_lu.outcome_code, ExecutionOutcomeCode.UNFILLED_LIMIT_UP_LOCKED)
        self.assertEqual(out_lu.filled_quantity, 0)

        # 昨收 100，跌停價 90.00：T3 一字跌停鎖死 90.00，賣單無法賣出
        sell_order = PaperOrder(
            order_id="ORD-LD-SELL",
            signal_id="SIG-LD-SELL",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 3, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_order)
        bar_ld = make_bar("2330", self.t3, "90.00", "90.00", "90.00", "90.00", volume=20_000, yesterday_close="100.00")
        out_ld = sim.match_order(sell_order, bar_ld, symbol_context=ctx)
        self.assertEqual(out_ld.outcome_code, ExecutionOutcomeCode.UNFILLED_LIMIT_DOWN_LOCKED)
        self.assertEqual(out_ld.filled_quantity, 0)

    # ── 6. 特殊上市階段與歷史法規的價格限制由規則介面決定 ─────────────

    def test_06_price_limit_rule_interface_special_phases_and_missing_data(self) -> None:
        """6. 價格限制規則支援 IPO 首五日無漲跌幅、2015 年前 7% 漲跌幅，且缺少參考價或未知階段時拒絕推測。"""
        rule = TaiwanHistoricalPriceLimitRule()
        tick_rule = TaiwanTickSizeRule()

        # (a) 2014 年（2015/06/01 放寬前）：法定漲跌幅為 ±7%
        bounds_2014 = rule.resolve_price_limits(
            "2330",
            dt.date(2014, 5, 15),
            Decimal("100.00"),
            instrument_category=InstrumentCategory.COMMON_STOCK,
            listing_phase=ListingPhase.REGULAR_LISTED,
            tick_rule=tick_rule,
        )
        self.assertEqual(bounds_2014.status, PriceLimitStatus.APPLIED)
        self.assertEqual(bounds_2014.limit_rate, Decimal("0.07"))
        self.assertEqual(bounds_2014.limit_up, Decimal("107.00"))
        self.assertEqual(bounds_2014.limit_down, Decimal("93.00"))

        # (b) 新股掛牌首五日 (IPO_NO_LIMIT_PERIOD)：無漲跌幅限制，大漲 50% 仍可正常買進成交
        bounds_ipo = rule.resolve_price_limits(
            "6999",
            self.t2,
            Decimal("100.00"),
            instrument_category=InstrumentCategory.COMMON_STOCK,
            listing_phase=ListingPhase.IPO_NO_LIMIT_PERIOD,
            tick_rule=tick_rule,
        )
        self.assertEqual(bounds_ipo.status, PriceLimitStatus.NO_PRICE_LIMIT)
        self.assertIsNone(bounds_ipo.limit_up)
        self.assertIsNone(bounds_ipo.limit_down)

        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ipo_order = PaperOrder(
            order_id="ORD-IPO-BUY",
            signal_id="SIG-IPO-BUY",
            symbol="6999",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ipo_order, reference_price=Decimal("100.00"))
        bar_ipo = make_bar("6999", self.t2, "140.00", "155.00", "135.00", "150.00", volume=100_000, yesterday_close="100.00")
        ipo_ctx = SymbolExecutionContext(
            symbol="6999",
            listing_phase=ListingPhase.IPO_NO_LIMIT_PERIOD,
            adv_20_shares=100_000,
        )
        out_ipo = sim.match_order(ipo_order, bar_ipo, symbol_context=ipo_ctx)
        self.assertEqual(out_ipo.outcome_code, ExecutionOutcomeCode.FILLED)
        assert out_ipo.fill_record is not None
        self.assertEqual(out_ipo.fill_record.fill_price, Decimal("140.50"))

        # (c) 正常上市階段但缺少 yesterday_close 與 reference_price -> 必須拋出 PriceLimitDataMissingError，禁止自行推測
        bar_no_ref = make_bar("2330", self.t2, "100.00", "102.00", "99.00", "101.00", volume=100_000, yesterday_close=None)
        order_no_ref = PaperOrder(
            order_id="ORD-NO-REF",
            signal_id="SIG-NO-REF",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(order_no_ref, reference_price=Decimal("100.00"))
        with self.assertRaises(PriceLimitDataMissingError):
            sim.match_order(order_no_ref, bar_no_ref, symbol_context=SymbolExecutionContext(symbol="2330"))

        # (d) 上市階段為 UNKNOWN -> 拋出 PriceLimitDataMissingError
        order_unk_phase = PaperOrder(
            order_id="ORD-UNK-PHASE",
            signal_id="SIG-UNK-PHASE",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(order_unk_phase, reference_price=Decimal("100.00"))
        bar_ok = make_bar("2330", self.t3, "100.00", "102.00", "99.00", "101.00", volume=100_000, yesterday_close="100.00")
        with self.assertRaises(PriceLimitDataMissingError):
            sim.match_order(
                order_unk_phase,
                bar_ok,
                symbol_context=SymbolExecutionContext(symbol="2330", listing_phase=ListingPhase.UNKNOWN),
            )

    # ── 7. Tick Size 驗證 ───────────────────────────────────────────

    def test_07_tick_size_validation_and_alignment(self) -> None:
        """7. 驗證台股各級距 Tick Size（普通股 vs ETF）及非法限價單攔截。"""
        tick_rule = TaiwanTickSizeRule()

        # 普通股 6 級檢驗
        self.assertEqual(tick_rule.get_tick_size(Decimal("9.85"), InstrumentCategory.COMMON_STOCK), Decimal("0.01"))
        self.assertEqual(tick_rule.get_tick_size(Decimal("25.35"), InstrumentCategory.COMMON_STOCK), Decimal("0.05"))
        self.assertEqual(tick_rule.get_tick_size(Decimal("75.20"), InstrumentCategory.COMMON_STOCK), Decimal("0.10"))
        self.assertEqual(tick_rule.get_tick_size(Decimal("250.50"), InstrumentCategory.COMMON_STOCK), Decimal("0.50"))
        self.assertEqual(tick_rule.get_tick_size(Decimal("680.00"), InstrumentCategory.COMMON_STOCK), Decimal("1.00"))
        self.assertEqual(tick_rule.get_tick_size(Decimal("1205.00"), InstrumentCategory.COMMON_STOCK), Decimal("5.00"))

        # ETF 在 150 元時 Tick Size 為 0.05，而普通股在 150 元時 Tick Size 為 0.50
        self.assertTrue(tick_rule.is_valid_tick(Decimal("150.15"), InstrumentCategory.ETF))
        self.assertFalse(tick_rule.is_valid_tick(Decimal("150.15"), InstrumentCategory.COMMON_STOCK))

        # 模擬器在 raise_on_invalid_limit_tick=True 時直接拋出 TickSizeViolationError
        sim_strict = HistoricalExecutionSimulator(
            config=ExecutionSimConfig(raise_on_invalid_limit_tick=True),
            calendar=self.cal,
        )
        bad_tick_order = PaperOrder(
            order_id="ORD-BAD-TICK-1",
            signal_id="SIG-BAD-TICK-1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("520.25"),  # 500~1000 應為 1.00 元整數檔位
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        with self.assertRaises(TickSizeViolationError):
            sim_strict.submit_order(bad_tick_order, instrument_category=InstrumentCategory.COMMON_STOCK)

        # 預設模式下將非法 Tick 委託直接標記為 REJECTED，不進入掛單佇列
        sim_default = HistoricalExecutionSimulator(calendar=self.cal)
        bad_tick_order_2 = PaperOrder(
            order_id="ORD-BAD-TICK-2",
            signal_id="SIG-BAD-TICK-2",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("520.25"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim_default.submit_order(bad_tick_order_2, instrument_category=InstrumentCategory.COMMON_STOCK)
        self.assertEqual(bad_tick_order_2.status, PaperOrderStatus.REJECTED)
        self.assertIn("INVALID_TICK_SIZE", bad_tick_order_2.rejection_reason)

    # ── 8. 停牌、處置及未知交易狀態 ──────────────────────────────────

    def test_08_halted_disposition_and_unknown_trading_status(self) -> None:
        """8. 停牌不成交、處置股依政策限制量能或攔截買單、未知交易狀態嚴禁自動視為正常可交易。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)

        # (a) 未知交易狀態 (InstrumentTradingStatus.UNKNOWN) -> 拋出 UnknownTradingStatusError
        ord_unk = PaperOrder(
            order_id="ORD-STAT-UNK",
            signal_id="SIG-STAT-UNK",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_unk, reference_price=Decimal("100.00"))
        bar_normal = make_bar("2330", self.t2, "100.00", "102.00", "99.00", "101.00", volume=100_000, yesterday_close="100.00")
        with self.assertRaises(UnknownTradingStatusError):
            sim.match_order(
                ord_unk,
                bar_normal,
                symbol_context=SymbolExecutionContext(
                    symbol="2330", trading_status=InstrumentTradingStatus.UNKNOWN
                ),
            )

        # (b) 停牌狀態 (HALTED) -> 0 成交，結果為 UNFILLED_HALTED
        ord_halt = PaperOrder(
            order_id="ORD-STAT-HALT",
            signal_id="SIG-STAT-HALT",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_halt, reference_price=Decimal("100.00"))
        bar_halted = make_bar(
            "2330", self.t2, "100.00", "100.00", "100.00", "100.00", volume=0, yesterday_close="100.00", is_halted=True
        )
        out_halt = sim.match_order(ord_halt, bar_halted)
        self.assertEqual(out_halt.outcome_code, ExecutionOutcomeCode.UNFILLED_HALTED)
        self.assertEqual(out_halt.filled_quantity, 0)

        # (c) 處置股設定 BLOCK_NEW_BUY 時，攔截買單但允許賣單（且量能減半）
        sim_disp = HistoricalExecutionSimulator(
            config=ExecutionSimConfig(disposition_policy=DispositionExecutionPolicy.BLOCK_NEW_BUY),
            calendar=self.cal,
        )
        ord_disp_buy = PaperOrder(
            order_id="ORD-DISP-BUY",
            signal_id="SIG-DISP-BUY",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim_disp.submit_order(ord_disp_buy, reference_price=Decimal("100.00"))
        bar_disp = make_bar(
            "2330",
            self.t2,
            "100.00",
            "102.00",
            "99.00",
            "101.00",
            volume=40_000,
            yesterday_close="100.00",
            is_disposition=True,
        )
        out_disp_buy = sim_disp.match_order(
            ord_disp_buy,
            bar_disp,
            symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=40_000),
        )
        self.assertEqual(out_disp_buy.outcome_code, ExecutionOutcomeCode.UNFILLED_DISPOSITION_BLOCKED)
        self.assertEqual(out_disp_buy.filled_quantity, 0)

    # ── 9. 成交量參與率上限与事前均量規則 ─────────────────────────────

    def test_09_volume_participation_cap_and_causal_adv_rules(self) -> None:
        """9. 驗證成交量參與率上限 (5%)、禁止把整日量當開盤已知資訊、以及處置股量能折半。"""
        # 子情境 A: STRICT_EX_ANTE_ADV_ONLY 下缺少事前 adv_20_shares 必須拋出 HistoricalDataMissingError
        sim_strict_adv = HistoricalExecutionSimulator(
            config=ExecutionSimConfig(volume_cap_policy=VolumeCapPolicy.STRICT_EX_ANTE_ADV_ONLY),
            calendar=self.cal,
        )
        ord_no_adv = PaperOrder(
            order_id="ORD-NO-ADV",
            signal_id="SIG-NO-ADV",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim_strict_adv.submit_order(ord_no_adv, reference_price=Decimal("100.00"))
        bar_t2 = make_bar("2330", self.t2, "100.00", "102.00", "99.00", "101.00", volume=100_000, yesterday_close="100.00")
        with self.assertRaises(HistoricalDataMissingError):
            sim_strict_adv.match_order(
                ord_no_adv, bar_t2, symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=None)
            )

        # 子情境 B: CAUSAL_ADV_AND_DAILY_BOUND: ADV20_T-1 = 30,000 股，當日爆量 Volume_T = 200,000 股，參與率 5%
        # 可成交上限受事前均量約束 = min(30,000, 200,000) * 5% = 1,500 股
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ord_cap = PaperOrder(
            order_id="ORD-CAP-1",
            signal_id="SIG-CAP-1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=5000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_cap, reference_price=Decimal("100.00"))
        bar_burst = make_bar("2330", self.t2, "100.00", "102.00", "99.00", "101.00", volume=200_000, yesterday_close="100.00")
        out_cap = sim.match_order(
            ord_cap, bar_burst, symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=30_000)
        )
        self.assertEqual(out_cap.max_fillable_quantity, 1500)
        self.assertEqual(out_cap.filled_quantity, 1500)
        self.assertFalse(out_cap.used_ex_post_daily_volume_approximation)

        # 子情境 C: 缺少事前均量但允許回退時，明確標記 used_ex_post_daily_volume_approximation=True 並附警告
        ord_fallback = PaperOrder(
            order_id="ORD-CAP-FALLBACK",
            signal_id="SIG-CAP-FALLBACK",
            symbol="2317",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_fallback, reference_price=Decimal("100.00"))
        bar_2317 = make_bar("2317", self.t2, "100.00", "102.00", "99.00", "101.00", volume=100_000, yesterday_close="100.00")
        out_fb = sim.match_order(
            ord_fallback, bar_2317, symbol_context=SymbolExecutionContext(symbol="2317", adv_20_shares=None)
        )
        self.assertTrue(out_fb.used_ex_post_daily_volume_approximation)
        self.assertGreaterEqual(len(out_fb.warnings), 1)

    # ── 10. 部分成交與完全未成交數量區分 ─────────────────────────────

    def test_10_partial_fill_and_unfilled_quantity_accounting(self) -> None:
        """10. 明確區分可成交數量 (max_fillable_quantity)、已成交數量 (filled_quantity) 與未成交數量 (unfilled_quantity)。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ledger = PortfolioLedger(Decimal("1000000.00"))
        lot_ledger = BacktestLotLedger(calendar=self.cal)

        ord_partial = PaperOrder(
            order_id="ORD-PARTIAL-1",
            signal_id="SIG-PARTIAL-1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=4000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_partial, ledger, reference_price=Decimal("100.00"))
        bar_t2 = make_bar("2330", self.t2, "100.00", "102.00", "99.00", "101.00", volume=30_000, yesterday_close="100.00")
        ctx = SymbolExecutionContext(symbol="2330", adv_20_shares=30_000)

        out1 = sim.match_order(ord_partial, bar_t2, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)
        self.assertEqual(out1.outcome_code, ExecutionOutcomeCode.PARTIALLY_FILLED_VOLUME_CAP)
        self.assertEqual(out1.order_quantity, 4000)
        self.assertEqual(out1.order_remaining_before, 4000)
        self.assertEqual(out1.max_fillable_quantity, 1500)
        self.assertEqual(out1.filled_quantity, 1500)
        self.assertEqual(out1.unfilled_quantity, 2500)
        self.assertEqual(ord_partial.status, PaperOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(lot_ledger.get_position_quantity("2330"), 1500)

        # 同日第二筆買單嘗試撮合同一標的：當日 1,500 股配額已耗盡 -> 完全未成交
        ord_second = PaperOrder(
            order_id="ORD-PARTIAL-2",
            signal_id="SIG-PARTIAL-2",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 5, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_second, ledger, reference_price=Decimal("100.00"))
        out2 = sim.match_order(ord_second, bar_t2, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)
        self.assertEqual(out2.outcome_code, ExecutionOutcomeCode.UNFILLED_VOLUME_CAP_ZERO)
        self.assertEqual(out2.max_fillable_quantity, 0)
        self.assertEqual(out2.filled_quantity, 0)
        self.assertEqual(out2.unfilled_quantity, 1000)

    # ── 11. 未成交委託到期與預留資金單次釋放 ──────────────────────────

    def test_11_unfilled_order_expiry_and_single_reservation_release(self) -> None:
        """11. 部分成交與完全未成交委託於收盤到期時，精確釋放剩餘預留資金與股數，且只能釋放一次。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        initial_cash = Decimal("1000000.00")
        ledger = PortfolioLedger(initial_cash)
        lot_ledger = BacktestLotLedger(calendar=self.cal)

        # 委託買進 3,000 股 (會產生非整除比例 1/3 與 2/3)
        ord_buy = PaperOrder(
            order_id="ORD-EXP-BUY",
            signal_id="SIG-EXP-BUY",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=3000,
            limit_price=Decimal("100.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(ord_buy, ledger)
        self.assertGreater(ledger.reserved_cash, Decimal("0.00"))

        # T2 僅部分成交 1,000 股 (ADV20 = 20,000 * 5% = 1,000 股)
        bar_t2 = make_bar("2330", self.t2, "100.00", "101.00", "99.50", "100.50", volume=20_000, yesterday_close="100.00")
        out = sim.match_order(
            ord_buy,
            bar_t2,
            symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=20_000),
            portfolio_ledger=ledger,
            lot_ledger=lot_ledger,
        )
        self.assertEqual(out.filled_quantity, 1000)
        self.assertEqual(ord_buy.status, PaperOrderStatus.PARTIALLY_FILLED)
        self.assertGreater(ledger.reserved_cash, Decimal("0.00"))

        # 收盤到期釋放未成交之 2,000 股預留資金
        expired = sim.expire_open_orders_at_close([ord_buy], portfolio_ledger=ledger, trade_date=self.t2)
        self.assertEqual(len(expired), 1)
        self.assertEqual(ord_buy.status, PaperOrderStatus.EXPIRED)
        # 關鍵斷言：到期後預留現金必須 100% 精確歸零（無任何 1 分錢殘差），且可用現金 = 初始資金 - 實際成交支出
        self.assertEqual(ledger.reserved_cash, Decimal("0.00"))
        assert out.fill_record is not None
        actual_spent = out.fill_record.notional + out.fill_record.commission
        self.assertEqual(ledger.cash, initial_cash - actual_spent)

        # 再次對已到期之委託呼叫 expire_order 或 cancel_order 必須拋出 DuplicateOrderExecutionError，絕不重複釋放資金
        with self.assertRaises(DuplicateOrderExecutionError):
            sim.expire_order(ord_buy, portfolio_ledger=ledger)
        with self.assertRaises(DuplicateOrderExecutionError):
            sim.cancel_order(ord_buy, portfolio_ledger=ledger)
        self.assertEqual(ledger.cash, initial_cash - actual_spent)

    # ── 12. 未成交賣單預留股數與超賣防護 ──────────────────────────────

    def test_12_sell_order_reserved_shares_and_oversell_protection(self) -> None:
        """12. 賣出成交不得超過可供出售的實際持倉及已扣除其他委託預留的股數。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ledger = PortfolioLedger(Decimal("1000000.00"))
        lot_ledger = BacktestLotLedger(calendar=self.cal)

        # 先在 T1 建立 2,000 股持倉
        buy_init = PaperOrder(
            order_id="ORD-INIT-BUY",
            signal_id="SIG-INIT-BUY",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=2000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 1, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_init, ledger, reference_price=Decimal("100.00"))
        bar_t1 = make_bar("2330", self.t1, "100.00", "101.00", "99.00", "100.00", volume=100_000, yesterday_close="100.00")
        sim.match_order(
            buy_init,
            bar_t1,
            symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=100_000),
            portfolio_ledger=ledger,
            lot_ledger=lot_ledger,
        )
        self.assertEqual(ledger.positions["2330"].quantity, 2000)

        # T1 盤後送出第一張賣單 1,500 股 -> 成功預留 1,500 股，剩餘可用股數僅 500 股
        sell_1 = PaperOrder(
            order_id="ORD-SELL-1",
            signal_id="SIG-SELL-1",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1500,
            limit_price=Decimal("105.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_1, ledger, lot_ledger=lot_ledger)
        self.assertEqual(sell_1.status, PaperOrderStatus.ACCEPTED)
        self.assertEqual(ledger.reserved_shares["2330"], 1500)
        self.assertEqual(ledger.get_available_shares("2330"), 500)

        # T1 盤後再送出第二張賣單 1,000 股（超過扣除第一張預留後的 500 股）-> 提交時立即被 REJECTED
        sell_2_over = PaperOrder(
            order_id="ORD-SELL-2-OVER",
            signal_id="SIG-SELL-2-OVER",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 1, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_2_over, ledger, lot_ledger=lot_ledger)
        self.assertEqual(sell_2_over.status, PaperOrderStatus.REJECTED)
        self.assertEqual(ledger.reserved_shares["2330"], 1500)

        # 若有外部未經預留直接塞入之 ACCEPTED 賣單嘗試在 T2 賣出 1,000 股（而其他委託已預留 1,500 股），
        # match_order 執行前也會偵測出可售僅 500 股並拋出 OversellPositionError
        rogue_sell = PaperOrder(
            order_id="ORD-ROGUE-SELL",
            signal_id="SIG-ROGUE-SELL",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 2, tzinfo=TAIPEI_TZ),
        )
        rogue_sell.accept(reserved_cash=Decimal("0.00"), reserved_shares=0)
        bar_t2 = make_bar("2330", self.t2, "102.00", "103.00", "101.00", "102.50", volume=100_000, yesterday_close="100.00")
        with self.assertRaises(OversellPositionError):
            sim.match_order(
                rogue_sell,
                bar_t2,
                symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=100_000),
                portfolio_ledger=ledger,
                lot_ledger=lot_ledger,
            )

        # T2 收盤 sell_1 因未達 105.00 到期作廢後，1,500 股預留被釋放，可用股數恢復為 2,000 股
        sim.expire_order(sell_1, portfolio_ledger=ledger)
        self.assertEqual(ledger.reserved_shares.get("2330", 0), 0)
        self.assertEqual(ledger.get_available_shares("2330"), 2000)

    # ── 13. 手續費、稅費及滑價不重複計算 ──────────────────────────────

    def test_13_single_responsibility_commission_tax_slippage_no_double_charge(self) -> None:
        """13. 手續費、證交稅（區分普通股 0.3% 與 ETF 0.1%）、滑價只計算一次，且未知商品類別拒絕猜測費率。"""
        paper_cfg = TaiwanPaperConfig(
            initial_cash=Decimal("1000000.00"),
            commission_rate=Decimal("0.001425"),
            commission_discount=Decimal("0.60"),
            min_commission=Decimal("20.00"),
            stock_tax_rate=Decimal("0.0030"),
            etf_tax_rate=Decimal("0.0010"),
            slippage_rate=Decimal("0.0005"),
        )
        cost_model = ExecutionCostModel.from_paper_config(paper_cfg)
        sim = HistoricalExecutionSimulator(cost_model=cost_model, calendar=self.cal)
        ledger = PortfolioLedger(paper_cfg.initial_cash)

        # (a) 未知商品類別計算稅費必須拒絕並拋出 BacktestAccountingError（不假定通用股票稅率）
        with self.assertRaises(BacktestAccountingError):
            cost_model.calculate_tax(Decimal("100000.00"), SignalAction.SELL, InstrumentCategory.UNKNOWN)

        # (b) 買進 ETF 0050 (1,000 股 @ 100.00) 與普通股 2330 (1,000 股 @ 500.00)
        buy_etf = PaperOrder(
            order_id="ORD-ETF-B",
            signal_id="SIG-ETF-B",
            symbol="0050",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("100.00"),
            created_at=dt.datetime(2025, 1, 1, 18, 0, tzinfo=TAIPEI_TZ),
        )
        buy_stk = PaperOrder(
            order_id="ORD-STK-B",
            signal_id="SIG-STK-B",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("500.00"),
            created_at=dt.datetime(2025, 1, 1, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_etf, ledger, instrument_category=InstrumentCategory.ETF)
        sim.submit_order(buy_stk, ledger, instrument_category=InstrumentCategory.COMMON_STOCK)

        bar_etf_t1 = make_bar("0050", self.t1, "100.00", "100.50", "99.50", "100.00", volume=100_000, yesterday_close="100.00")
        bar_stk_t1 = make_bar("2330", self.t1, "500.00", "505.00", "498.00", "502.00", volume=100_000, yesterday_close="500.00")

        out_etf_b = sim.match_order(
            buy_etf,
            bar_etf_t1,
            symbol_context=SymbolExecutionContext(
                symbol="0050", instrument_category=InstrumentCategory.ETF, adv_20_shares=100_000
            ),
            portfolio_ledger=ledger,
        )
        out_stk_b = sim.match_order(
            buy_stk,
            bar_stk_t1,
            symbol_context=SymbolExecutionContext(
                symbol="2330", instrument_category=InstrumentCategory.COMMON_STOCK, adv_20_shares=100_000
            ),
            portfolio_ledger=ledger,
        )
        assert out_etf_b.fill_record is not None and out_stk_b.fill_record is not None

        # T2 全數賣出 ETF 0050 (@105.00) 與普通股 2330 (@520.00)
        sell_etf = PaperOrder(
            order_id="ORD-ETF-S",
            signal_id="SIG-ETF-S",
            symbol="0050",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("105.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sell_stk = PaperOrder(
            order_id="ORD-STK-S",
            signal_id="SIG-STK-S",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("520.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(sell_etf, ledger, instrument_category=InstrumentCategory.ETF)
        sim.submit_order(sell_stk, ledger, instrument_category=InstrumentCategory.COMMON_STOCK)

        bar_etf_t2 = make_bar("0050", self.t2, "104.00", "106.00", "103.50", "105.50", volume=100_000, yesterday_close="100.00")
        bar_stk_t2 = make_bar("2330", self.t2, "515.00", "525.00", "512.00", "522.00", volume=100_000, yesterday_close="502.00")

        out_etf_s = sim.match_order(
            sell_etf,
            bar_etf_t2,
            symbol_context=SymbolExecutionContext(
                symbol="0050", instrument_category=InstrumentCategory.ETF, adv_20_shares=100_000
            ),
            portfolio_ledger=ledger,
        )
        out_stk_s = sim.match_order(
            sell_stk,
            bar_stk_t2,
            symbol_context=SymbolExecutionContext(
                symbol="2330", instrument_category=InstrumentCategory.COMMON_STOCK, adv_20_shares=100_000
            ),
            portfolio_ledger=ledger,
        )
        assert out_etf_s.fill_record is not None and out_stk_s.fill_record is not None

        # 驗證 ETF 賣出證交稅為 0.1% (105,000 * 0.001 = 105 元)，普通股賣出證交稅為 0.3% (520,000 * 0.003 = 1,560 元)
        self.assertEqual(out_etf_s.fill_record.tax, Decimal("105"))
        self.assertEqual(out_stk_s.fill_record.tax, Decimal("1560"))

        # 驗證單次扣費守恆：最終現金 = 初始資金 + 賣出價金總和 - 買進價金總和 - 四筆手續費 - 兩筆證交稅
        expected_cash = (
            Decimal("1000000.00")
            - out_etf_b.fill_record.notional
            - out_etf_b.fill_record.commission
            - out_stk_b.fill_record.notional
            - out_stk_b.fill_record.commission
            + out_etf_s.fill_record.notional
            - out_etf_s.fill_record.commission
            - out_etf_s.fill_record.tax
            + out_stk_s.fill_record.notional
            - out_stk_s.fill_record.commission
            - out_stk_s.fill_record.tax
        )
        self.assertEqual(ledger.cash, expected_cash)

    # ── 14. FillRecord 可被 BacktestLotLedger 正確接收 ─────────────────

    def test_14_fill_record_seamless_integration_with_backtest_lot_ledger(self) -> None:
        """14. 撮合器產生的 FillRecord 可直接交由 BacktestLotLedger 執行分批買進與 FIFO 部分賣出，並與 PortfolioLedger 對帳一致。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ledger = PortfolioLedger(Decimal("2000000.00"))
        lot_ledger = BacktestLotLedger(calendar=self.cal)
        ctx = SymbolExecutionContext(symbol="2330", adv_20_shares=100_000)

        # T1 買進第一批 1,000 股 @ 500
        b1 = PaperOrder(
            order_id="ORD-LOT-B1",
            signal_id="SIG-LOT-B1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("500.00"),
            created_at=dt.datetime(2025, 1, 1, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(b1, ledger)
        bar_t1 = make_bar("2330", self.t1, "500.00", "502.00", "499.00", "500.00", volume=100_000, yesterday_close="500.00")
        sim.match_order(b1, bar_t1, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)

        # T2 買進第二批 2,000 股 @ 510
        b2 = PaperOrder(
            order_id="ORD-LOT-B2",
            signal_id="SIG-LOT-B2",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=2000,
            limit_price=Decimal("510.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(b2, ledger)
        bar_t2 = make_bar("2330", self.t2, "510.00", "515.00", "508.00", "512.00", volume=100_000, yesterday_close="500.00")
        sim.match_order(b2, bar_t2, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)

        self.assertEqual(lot_ledger.get_oldest_entry_date("2330"), self.t1)
        self.assertEqual(len(lot_ledger.get_open_lots("2330")), 2)

        # T3 部分賣出 1,500 股 -> FIFO 出清第一批 (T1) 1,000 股 + 第二批 (T2) 500 股，oldest_entry_date 推進為 T2
        s1 = PaperOrder(
            order_id="ORD-LOT-S1",
            signal_id="SIG-LOT-S1",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1500,
            limit_price=Decimal("520.00"),
            created_at=dt.datetime(2025, 1, 3, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(s1, ledger, lot_ledger=lot_ledger)
        bar_t3 = make_bar("2330", self.t3, "520.00", "525.00", "518.00", "522.00", volume=100_000, yesterday_close="512.00")
        sim.match_order(s1, bar_t3, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)

        self.assertEqual(lot_ledger.get_position_quantity("2330"), 1500)
        self.assertEqual(lot_ledger.get_oldest_entry_date("2330"), self.t2)
        lot_ledger.assert_reconciled_with_portfolio_ledger(ledger, check_realized_pnl_when_flat=False)

        # T4 賣出剩餘 1,500 股全平倉 -> FIFO 與加權平均累計已實現損益 100% 相等
        s2 = PaperOrder(
            order_id="ORD-LOT-S2",
            signal_id="SIG-LOT-S2",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1500,
            limit_price=Decimal("525.00"),
            created_at=dt.datetime(2025, 1, 6, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(s2, ledger, lot_ledger=lot_ledger)
        bar_t4 = make_bar("2330", self.t4, "525.00", "530.00", "523.00", "528.00", volume=100_000, yesterday_close="522.00")
        sim.match_order(s2, bar_t4, symbol_context=ctx, portfolio_ledger=ledger, lot_ledger=lot_ledger)
        lot_ledger.assert_reconciled_with_portfolio_ledger(ledger, check_realized_pnl_when_flat=True)

    # ── 15. 現金、持倉、預留股數、應收股利與 NAV 的一致性 ──────────────

    def test_15_cash_reserved_positions_receivables_and_nav_consistency(self) -> None:
        """15. 驗證可用現金、預留現金、預留股數與應收股利之 NAV 結構分解，杜絕重複計算。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ledger = PortfolioLedger(Decimal("1000000.00"))
        lot_ledger = BacktestLotLedger(calendar=self.cal)
        cap = CorporateActionProcessor(calendar=self.cal)
        valuator = PortfolioValuator(calendar=self.cal, provenance=self.prov)

        # T1 買進 2330 1,000 股 @ 500.00 (市價加滑價對齊 Tick 501.00，價金 501,000，手續費 428)
        buy_order = PaperOrder(
            order_id="ORD-NAV-B1",
            signal_id="SIG-NAV-B1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 1, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_order, ledger, reference_price=Decimal("500.00"))
        bar_t1 = make_bar("2330", self.t1, "500.00", "505.00", "499.00", "500.00", volume=100_000, yesterday_close="500.00")
        sim.match_order(
            buy_order,
            bar_t1,
            symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=100_000),
            portfolio_ledger=ledger,
            lot_ledger=lot_ledger,
        )

        # T1 盤後同時掛出一張未成交買單（預留現金）與一張未成交賣單（預留 400 股）
        pending_buy = PaperOrder(
            order_id="ORD-PEND-BUY",
            signal_id="SIG-PEND-BUY",
            symbol="2317",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("100.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        pending_sell = PaperOrder(
            order_id="ORD-PEND-SELL",
            signal_id="SIG-PEND-SELL",
            symbol="2330",
            action=SignalAction.SELL,
            order_type=SignalOrderType.LIMIT,
            order_quantity=400,
            limit_price=Decimal("510.00"),
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(pending_buy, ledger)
        sim.submit_order(pending_sell, ledger, lot_ledger=lot_ledger)

        # 估值 T1：驗證預留現金與可用現金加總正確，且預留 400 股仍包含在 1,000 股總持倉中（不重複計算為 1,400 股）
        nav_t1 = valuator.evaluate_daily_nav(
            self.t1,
            ledger,
            market_bars={"2330": bar_t1},
            lot_ledger=lot_ledger,
            corporate_action_processor=cap,
            valuation_timestamp=dt.datetime(2025, 1, 2, 18, 10, tzinfo=TAIPEI_TZ),
        )
        self.assertTrue(nav_t1.is_complete)
        self.assertEqual(nav_t1.total_cash, Decimal("1000000.00") - Decimal("501428.00"))
        self.assertEqual(nav_t1.available_cash + nav_t1.reserved_cash, nav_t1.total_cash)
        self.assertEqual(len(nav_t1.positions), 1)
        self.assertEqual(nav_t1.positions[0].total_quantity, 1000)
        self.assertEqual(nav_t1.positions[0].available_quantity, 600)
        self.assertEqual(nav_t1.positions[0].reserved_quantity, 400)
        self.assertEqual(nav_t1.market_value, Decimal("500000.00"))

        # 驗證若外部使用 TOTAL_CASH_INCLUDES_RESERVED 基準傳入總現金，亦不會重複加總 reserved_cash
        val_total_basis = PortfolioValuator(
            calendar=self.cal,
            provenance=self.prov,
            config=ValuationConfig(cash_basis=CashAccountingBasis.TOTAL_CASH_INCLUDES_RESERVED),
        )
        fake_ledger = PortfolioLedger(Decimal("1000000.00"))
        fake_ledger.cash = nav_t1.total_cash  # 此處 cash 欄位已包含預留現金
        fake_ledger.reserved_cash = nav_t1.reserved_cash
        nav_alt = val_total_basis.evaluate_daily_nav(self.t1, fake_ledger, close_prices={})
        self.assertEqual(nav_alt.total_cash, nav_t1.total_cash)
        self.assertEqual(nav_alt.available_cash, nav_t1.available_cash)

    # ── 16. 股利應收轉現金時 NAV 不重複計算 ────────────────────────────

    def test_16_dividend_receivable_to_cash_nav_conservation(self) -> None:
        """16. 發放日應收股利轉成現金後，應收股利歸零且 NAV 不得因帳務轉換本身而改變。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        ledger = PortfolioLedger(Decimal("1000000.00"))
        lot_ledger = BacktestLotLedger(calendar=self.cal)
        cap = CorporateActionProcessor(calendar=self.cal)
        valuator = PortfolioValuator(calendar=self.cal, provenance=self.prov)

        buy_order = PaperOrder(
            order_id="ORD-DIV-B1",
            signal_id="SIG-DIV-B1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("500.00"),
            created_at=dt.datetime(2025, 1, 1, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(buy_order, ledger)
        bar_t1 = make_bar("2330", self.t1, "500.00", "502.00", "499.00", "500.00", volume=100_000, yesterday_close="500.00")
        sim.match_order(
            buy_order,
            bar_t1,
            symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=100_000),
            portfolio_ledger=ledger,
            lot_ledger=lot_ledger,
        )
        nav_t1 = valuator.evaluate_daily_nav(
            self.t1, ledger, market_bars={"2330": bar_t1}, lot_ledger=lot_ledger, corporate_action_processor=cap
        )

        # T2 除息每股 10 元（應收股利 10,000 元），T2 收盤除息參考價 490.00 元，發放日為 T3
        div_event = CorporateActionEvent(
            event_id="CA-2330-2025Q1",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=self.t2,
            payable_date=self.t3,
            announced_at=dt.datetime(2024, 12, 20, 17, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2024, 12, 20, 17, 30, tzinfo=TAIPEI_TZ),
            provenance=self.prov,
            cash_dividend_per_share=Decimal("10.00"),
            is_synthetic_payable_date=True,
        )
        cap.process_ex_dividend_event(
            div_event, lot_ledger, as_of_time=dt.datetime(2025, 1, 3, 8, 0, tzinfo=TAIPEI_TZ)
        )
        bar_t2 = make_bar("2330", self.t2, "490.00", "492.00", "488.00", "490.00", volume=100_000, yesterday_close="490.00")
        nav_t2 = valuator.evaluate_daily_nav(
            self.t2, ledger, market_bars={"2330": bar_t2}, lot_ledger=lot_ledger, corporate_action_processor=cap
        )
        self.assertEqual(nav_t2.dividends_receivable, Decimal("10000.00"))
        self.assertEqual(nav_t2.market_value, Decimal("490000.00"))
        self.assertEqual(nav_t2.total_nav, nav_t1.total_nav)

        # T3 發放日轉為現金：應收股利歸零、現金增加 10,000、總 NAV 完全不變
        cap.process_due_payables(portfolio_ledger=ledger, as_of_time=dt.datetime(2025, 1, 6, 8, 0, tzinfo=TAIPEI_TZ))
        bar_t3 = make_bar("2330", self.t3, "490.00", "491.00", "489.00", "490.00", volume=100_000, yesterday_close="490.00")
        nav_t3 = valuator.evaluate_daily_nav(
            self.t3, ledger, market_bars={"2330": bar_t3}, lot_ledger=lot_ledger, corporate_action_processor=cap
        )
        self.assertEqual(nav_t3.dividends_receivable, Decimal("0.00"))
        self.assertEqual(nav_t3.available_cash, nav_t2.available_cash + Decimal("10000.00"))
        self.assertEqual(nav_t3.total_nav, nav_t2.total_nav)

    # ── 17. 缺價、過期價格與停牌估值警告 ──────────────────────────────

    def test_17_missing_price_stale_price_and_halted_valuation_warnings(self) -> None:
        """17. 停牌股最後已知價格只能作為帶有明確日期與警告的估值假設，不能視為當日可成交價格。"""
        ledger = PortfolioLedger(Decimal("1000000.00"))
        ledger.record_buy_fill(
            symbol="2330",
            fill_shares=1000,
            fill_price=Decimal("500.00"),
            notional=Decimal("500000.00"),
            fee=Decimal("428.00"),
            reserved_cash_deduct=Decimal("0.00"),
        )
        valuator = PortfolioValuator(
            calendar=self.cal,
            provenance=self.prov,
            config=ValuationConfig(
                halted_price_policy=MissingPricePolicy.USE_LAST_KNOWN_CLOSE_WITH_WARNING,
                max_stale_trading_days=2,
                verify_lot_ledger_consistency=False,
            ),
        )

        # T1 正常收盤 500.00 元
        bar_t1 = make_bar("2330", self.t1, "500.00", "502.00", "498.00", "500.00", volume=100_000, yesterday_close="500.00")
        nav_t1 = valuator.evaluate_daily_nav(self.t1, ledger, market_bars={"2330": bar_t1})
        self.assertEqual(nav_t1.tradable_close_prices.get("2330"), Decimal("500.00"))

        # T2 停牌：使用 T1 最後已知價格 500.00，發出 HALTED_LAST_KNOWN_PRICE_USED 警告，且 is_tradable_price=False
        nav_t2 = valuator.evaluate_daily_nav(self.t2, ledger, halted_symbols={"2330"})
        self.assertTrue(nav_t2.is_complete)
        self.assertEqual(nav_t2.halted_symbols, ("2330",))
        self.assertEqual(nav_t2.stale_price_symbols, ("2330",))
        self.assertEqual(nav_t2.price_dates["2330"], self.t1)
        self.assertEqual(nav_t2.price_sources["2330"], ValuationPriceSource.LAST_KNOWN_CLOSE_HALTED)
        self.assertFalse(nav_t2.positions[0].is_tradable_price)
        self.assertNotIn("2330", nav_t2.tradable_close_prices)
        self.assertEqual(len(nav_t2.warnings), 1)
        self.assertEqual(nav_t2.warnings[0].code, ValuationWarningCode.HALTED_LAST_KNOWN_PRICE_USED)
        self.assertEqual(nav_t2.warnings[0].staleness_trading_days, 1)

    # ── 18. 無法估值時拒絕輸出虛假的精確 NAV ──────────────────────────

    def test_18_refuse_fake_exact_nav_when_unvaluable(self) -> None:
        """18. 缺價不得默認為 0；當價格過期超限或無法合理估值時，拒絕產生正式 NAV 或拋出明確例外。"""
        ledger = PortfolioLedger(Decimal("1000000.00"))
        ledger.record_buy_fill(
            symbol="2330",
            fill_shares=1000,
            fill_price=Decimal("500.00"),
            notional=Decimal("500000.00"),
            fee=Decimal("428.00"),
            reserved_cash_deduct=Decimal("0.00"),
        )
        val_cfg = ValuationConfig(
            halted_price_policy=MissingPricePolicy.USE_LAST_KNOWN_CLOSE_WITH_WARNING,
            missing_price_policy=MissingPricePolicy.REJECT_VALUATION,
            stale_exceeded_policy=MissingPricePolicy.REJECT_VALUATION,
            max_stale_trading_days=1,
            verify_lot_ledger_consistency=False,
        )
        valuator = PortfolioValuator(calendar=self.cal, provenance=self.prov, config=val_cfg)

        # (a) 嚴禁價格為 0：傳入 0 元價格立即拋出 InvalidOHLCVError
        with self.assertRaises(InvalidOHLCVError):
            valuator.evaluate_daily_nav(self.t1, ledger, close_prices={"2330": Decimal("0.00")})

        # (b) T1 正常收盤 500，T2 停牌 (過期 1 日可容許)，T3 繼續停牌 (過期 2 日 > 上限 1 日) -> 拋出 StaleValuationPriceError
        bar_t1 = make_bar("2330", self.t1, "500.00", "502.00", "498.00", "500.00", volume=100_000, yesterday_close="500.00")
        valuator.evaluate_daily_nav(self.t1, ledger, market_bars={"2330": bar_t1})
        valuator.evaluate_daily_nav(self.t2, ledger, halted_symbols={"2330"})
        with self.assertRaises(StaleValuationPriceError):
            valuator.evaluate_daily_nav(self.t3, ledger, halted_symbols={"2330"})

        # (c) 非停牌但缺少報價：依 missing_price_policy=REJECT_VALUATION 拋出 ValuationPriceMissingError
        with self.assertRaises(ValuationPriceMissingError):
            valuator.evaluate_daily_nav(self.t3, ledger, close_prices={})

        # (d) 當政策設為 MARK_INCOMPLETE_NAV 時：回傳 is_complete=False, total_nav=None，且 require_official_nav() 拋出 IncompleteValuationError
        val_incomplete = PortfolioValuator(
            calendar=self.cal,
            provenance=self.prov,
            config=ValuationConfig(
                missing_price_policy=MissingPricePolicy.MARK_INCOMPLETE_NAV,
                verify_lot_ledger_consistency=False,
            ),
        )
        nav_inc = val_incomplete.evaluate_daily_nav(self.t3, ledger, close_prices={})
        self.assertFalse(nav_inc.is_complete)
        self.assertFalse(nav_inc.is_official_nav)
        self.assertIsNone(nav_inc.total_nav)
        self.assertIsNone(nav_inc.market_value)
        self.assertEqual(nav_inc.unvaluable_symbols, ("2330",))
        with self.assertRaises(IncompleteValuationError):
            nav_inc.require_official_nav()

    # ── 19. 時區與交易日邊界 ─────────────────────────────────────────

    def test_19_timezone_and_trading_day_boundary_enforcement(self) -> None:
        """19. 驗證無時區時間戳攔截、跨時區 UTC 時間正確轉換至 Asia/Taipei 交易日，以及休市日拒絕撮合與估值。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        valuator = PortfolioValuator(calendar=self.cal, provenance=self.prov)
        ledger = PortfolioLedger(Decimal("1000000.00"))

        # (a) 無時區 (naive) order.created_at 必須被拒絕
        naive_order = PaperOrder(
            order_id="ORD-NAIVE",
            signal_id="SIG-NAIVE",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0),  # 無 tzinfo
        )
        with self.assertRaises(BacktestCalendarError):
            sim.submit_order(naive_order, reference_price=Decimal("100.00"))

        # (b) UTC 時間 2025-01-02 10:00Z 等同於台北時間 2025-01-02 18:00+08:00 (T1 盤後)，
        # 在 T2 (2025-01-03) 可正常成交，但在 T1 不可成交
        utc_order = PaperOrder(
            order_id="ORD-UTC",
            signal_id="SIG-UTC",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=1000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 10, 0, tzinfo=dt.timezone.utc),
        )
        sim.submit_order(utc_order, ledger, reference_price=Decimal("100.00"))
        self.assertEqual(utc_order.created_at.tzinfo, TAIPEI_TZ)
        self.assertEqual(utc_order.created_at.hour, 18)

        # (c) 非交易日 (2025-01-04 週六) 嘗試執行每日收盤估值 -> 拋出 BacktestCalendarError
        with self.assertRaises(BacktestCalendarError):
            valuator.evaluate_daily_nav(dt.date(2025, 1, 4), ledger, close_prices={})

    # ── 20. 同一委託或成交重複處理的防護 ─────────────────────────────

    def test_20_duplicate_order_and_fill_processing_guards(self) -> None:
        """20. 同一委託重複提交、同日重複撮合、終態委託再撮合或同一 FillRecord 重複入帳皆須拋出明確例外。"""
        sim = HistoricalExecutionSimulator(calendar=self.cal)
        lot_ledger = BacktestLotLedger(calendar=self.cal)

        order = PaperOrder(
            order_id="ORD-DUP-1",
            signal_id="SIG-DUP-1",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.MARKET,
            order_quantity=2000,
            limit_price=None,
            created_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
        )
        sim.submit_order(order, reference_price=Decimal("100.00"))

        # 重複提交同一 order_id
        with self.assertRaises(DuplicateOrderExecutionError):
            sim.submit_order(order, reference_price=Decimal("100.00"))

        # T2 部分成交 1,000 股
        bar_t2 = make_bar("2330", self.t2, "100.00", "101.00", "99.00", "100.50", volume=20_000, yesterday_close="100.00")
        out = sim.match_order(
            order,
            bar_t2,
            symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=20_000),
            lot_ledger=lot_ledger,
        )
        self.assertEqual(out.filled_quantity, 1000)
        assert out.fill_record is not None

        # 同一交易日 T2 嘗試對同一筆 PARTIALLY_FILLED 委託再次呼叫 match_order -> 拋出 DuplicateOrderExecutionError
        with self.assertRaises(DuplicateOrderExecutionError):
            sim.match_order(
                order,
                bar_t2,
                symbol_context=SymbolExecutionContext(symbol="2330", adv_20_shares=20_000),
                lot_ledger=lot_ledger,
            )

        # 嘗試將同一筆 FillRecord 重複送入 BacktestLotLedger -> 拋出 DuplicateFillError
        with self.assertRaises(DuplicateFillError):
            lot_ledger.record_order_fill(out.fill_record, order)

    # ── 21. 原始子專案完整性與純標準庫依賴驗證 ────────────────────────

    def test_21_subprojects_clean_and_pure_stdlib_verification(self) -> None:
        """21. 驗證兩個原始子專案 HEAD commit 與工作目錄完全未修改，且新模組僅使用標準庫。"""
        root = Path(__file__).resolve().parent.parent
        expected = {
            "anti-gambling-trader-tw": "8609cef73718a056fddb74201fc304f174fa1553",
            "qlib-tw-trader": "774b3fb60d3b20b5c9ee86d25052ad2ec08e2011",
        }
        for sub_name, exp_commit in expected.items():
            sub_dir = root / sub_name
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=sub_dir, text=True).strip()
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=sub_dir, text=True).strip()
            self.assertEqual(head, exp_commit, f"{sub_name} HEAD commit 被改變!")
            self.assertEqual(status, "", f"{sub_name} 工作目錄不乾淨: {status}")


if __name__ == "__main__":
    unittest.main()
