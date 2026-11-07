"""第三階段 C-2.4：策略／風控橋接與事件驅動 Walk-Forward 主迴圈整合測試 (test_backtest_runner.py).

涵蓋全部 16 項核心驗證情境：
1. 單日 12 步因果執行順序測試 (`EXPECTED_DAILY_TIMELINE_SEQUENCE`)
2. T-1 盤後產生訊號 -> T 開盤撮合 -> T 收盤估值
3. 禁止 T 盤後訊號在 T 當日成交
4. 禁止未來 `PredictionBatch` 洩漏與攔截缺少 `available_at` 之未驗證預測
5. 模型 `trained_cutoff_date` 晚於決策日或侵入 Walk-Forward Embargo 時拒絕
6. 固定持有天數到期自動產生出場訊號 (`HOLDING_PERIOD_EXPIRED`) 並在下一交易日成交
7. 除息日股價下跌但含 `dividends_receivable` 時，不誤觸 `RiskEngine` 日虧損或最大回撤熔斷
8. `dividends_receivable` 在 `payable_date` 前不得用於買股；`payable_date` 入帳後才可動用
9. `RiskEngine` 拒絕訊號後不得產生有效委託、預留或成交
10. 漲停買不進、跌停賣不出時，收盤到期並正確釋放預留現金／預留股數（單次釋放）
11. 部分成交後，剩餘股數收盤到期，帳務與預留正確更新
12. 停牌股期間：不可成交、以最後已知收盤價估值並附結構化警告
13. 週末與休市日自動跳過，不執行撮合、估值或策略評估
14. 缺少當日 `PredictionBatch` 時依設定拒絕 (`FAIL_FAST`) 或安全跳過 (`SKIP_STRATEGY_HOLD`)
15. 多日連續回測後，最終現金、持倉市值、已實現損益、未實現損益、股利、交易成本與 `total_nav` 完全守恆
16. 防重複處理測試：重複消費同一 `PredictionBatch`、重複執行同一交易日、重複交割同一 `FillRecord` 拋出明確例外
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import unittest

from tw_quant.backtest import (
    EXPECTED_DAILY_TIMELINE_SEQUENCE,
    TAIPEI_TZ,
    BacktestLotLedger,
    BacktestRiskContext,
    BacktestRunStatus,
    CalendarMetadata,
    CorporateActionEvent,
    CorporateActionProcessor,
    CorporateActionType,
    DailyTimelineStep,
    DataProvenance,
    DuplicateFillError,
    DuplicatePredictionConsumptionError,
    DuplicateRunnerExecutionError,
    EmbargoViolationError,
    ErrorHandlingMode,
    ExecutionOutcomeCode,
    ExecutionSimConfig,
    HistoricalBarRecord,
    HistoricalExecutionSimulator,
    InMemoryHistoricalDataProvider,
    InMemoryPredictionFeed,
    InstrumentTradingStatus,
    LookaheadBiasError,
    MissingPredictionPolicy,
    PITViolationError,
    PortfolioValuator,
    PredictionBatchUnavailableError,
    PredictionProvenanceError,
    SymbolExecutionContext,
    TradingCalendar,
    UnverifiedPredictionPolicy,
    ValuationPriceSource,
    ValuationWarningCode,
    WalkForwardRunConfig,
    WalkForwardRunner,
    WalkForwardWindowSpec,
)
from tw_quant.broker.broker import TaiwanPaperBroker
from tw_quant.broker.config import TaiwanPaperConfig
from tw_quant.broker.order import PaperOrderStatus
from tw_quant.contracts.signal import QuantityUnit, SignalAction, SignalOrderType, UnifiedTradeSignal
from tw_quant.gateway import TradingGateway
from tw_quant.prediction.contract import PredictionBatch, PredictionMetadata, StockPrediction
from tw_quant.risk.config import RiskConfig
from tw_quant.risk.decision import RiskRejectCode
from tw_quant.risk.engine import RiskEngine
from tw_quant.strategy.config import CostModel, StrategyConfig
from tw_quant.strategy.decision import StrategyAction, StrategyReason
from tw_quant.strategy.engine import StrategyEngine


def _make_provenance(source: str = "SYNTHETIC_C24_TEST_SUITE") -> DataProvenance:
    return DataProvenance(
        source=source,
        dataset_version="v1.0-c24-synthetic",
        retrieved_at=dt.datetime(2025, 1, 1, 9, 0, tzinfo=TAIPEI_TZ),
        is_synthetic=True,
        description="Phase 3C-2.4 synthetic unit test fixture",
    )


def _make_calendar(
    trading_days: Sequence[dt.date],
    holidays: Sequence[dt.date] = (),
    coverage_start: Optional[dt.date] = None,
    coverage_end: Optional[dt.date] = None,
) -> TradingCalendar:
    c_start = coverage_start or min(trading_days)
    c_end = coverage_end or max(trading_days)
    return TradingCalendar.create_synthetic_for_test(
        trading_days=trading_days,
        coverage_start=c_start,
        coverage_end=c_end,
        non_trading_days=holidays if holidays else None,
        version="2025.c24",
    )


def _make_bar(
    symbol: str,
    trade_date: dt.date,
    open_px: str,
    high_px: str,
    low_px: str,
    close_px: str,
    yesterday_close: str,
    volume: int = 500_000,
    *,
    is_halted: bool = False,
    is_disposition: bool = False,
    is_limit_up_locked: bool = False,
    is_limit_down_locked: bool = False,
    provenance: Optional[DataProvenance] = None,
) -> HistoricalBarRecord:
    prov = provenance or _make_provenance()
    event_dt = dt.datetime.combine(trade_date, dt.time(13, 30, 0), tzinfo=TAIPEI_TZ)
    pub_dt = dt.datetime.combine(trade_date, dt.time(14, 30, 0), tzinfo=TAIPEI_TZ)
    avail_dt = dt.datetime.combine(trade_date, dt.time(15, 0, 0), tzinfo=TAIPEI_TZ)
    return HistoricalBarRecord(
        symbol=symbol,
        trade_date=trade_date,
        open=Decimal(open_px),
        high=Decimal(high_px),
        low=Decimal(low_px),
        close=Decimal(close_px),
        volume=volume,
        yesterday_close=Decimal(yesterday_close),
        event_time=event_dt,
        published_at=pub_dt,
        available_at=avail_dt,
        provenance=prov,
        is_halted=is_halted,
        is_disposition=is_disposition,
        is_limit_up_locked=is_limit_up_locked,
        is_limit_down_locked=is_limit_down_locked,
    )


def _make_prediction_batch(
    *,
    batch_id: str,
    decision_date: dt.date,
    target_trade_date: dt.date,
    scores_and_ranks: Sequence[Tuple[str, float, int]],
    cutoff_time: Optional[dt.datetime] = None,
    generated_at: Optional[dt.datetime] = None,
    trained_cutoff_date: Optional[dt.date] = None,
    is_mock: bool = False,
    model_type: str = "SYNTHETIC_PRODUCTION_FIXTURE",
) -> PredictionBatch:
    c_dt = cutoff_time or dt.datetime.combine(decision_date, dt.time(16, 0, 0), tzinfo=TAIPEI_TZ)
    g_dt = generated_at or dt.datetime.combine(decision_date, dt.time(17, 0, 0), tzinfo=TAIPEI_TZ)
    meta = PredictionMetadata(
        model_name="qlib_lgbm_c24_fixture",
        model_version="v1.0.0",
        feature_version="alpha158_v1",
        model_type=model_type,
        is_mock=is_mock,
        trained_cutoff_date=trained_cutoff_date,
    )
    preds = tuple(
        StockPrediction(
            symbol=sym,
            as_of_date=target_trade_date,
            data_cutoff_time=c_dt,
            prediction_time=g_dt,
            score=score,
            rank=rank,
            model_version="v1.0.0",
            feature_version="alpha158_v1",
        )
        for sym, score, rank in scores_and_ranks
    )
    return PredictionBatch(
        batch_id=batch_id,
        as_of_date=target_trade_date,
        data_cutoff_time=c_dt,
        generated_at=g_dt,
        metadata=meta,
        predictions=preds,
    )


def _make_strategy_config(
    *,
    top_n: int = 2,
    min_score: float = 0.50,
    max_position_weight: str = "0.45",
    min_cash_reserve_pct: str = "0.10",
    holding_trading_days: int = 5,
    allow_odd_lots: bool = False,
    order_type: SignalOrderType = SignalOrderType.MARKET,
    rebalance_drift_threshold_pct: str = "0.02",
) -> StrategyConfig:
    target_ratio = min(
        Decimal(max_position_weight),
        (Decimal("1") - Decimal(min_cash_reserve_pct)) / Decimal(max(1, top_n)),
    )
    return StrategyConfig(
        strategy_name="c24_test_strategy",
        strategy_version="v1.0",
        max_positions=top_n,
        min_score=min_score,
        target_position_ratio=target_ratio,
        max_single_position_ratio=Decimal(max_position_weight),
        min_cash_ratio=Decimal(min_cash_reserve_pct),
        holding_trading_days=holding_trading_days,
        cost_model=CostModel.from_paper_config(TaiwanPaperConfig()),
        allow_mock_predictions=False,
        order_type=order_type,
        share_unit=1 if allow_odd_lots else 1000,
        rebalance_tolerance_ratio=Decimal(rebalance_drift_threshold_pct),
    )


class TestBacktestRunnerPhase3C24(unittest.TestCase):
    """第三階段 C-2.4：PredictionFeed、Adapters 與 WalkForwardRunner 整合驗收測試。"""

    def setUp(self) -> None:
        # 建立標準合成交易日曆：
        # 2025-01-06 (一), 01-07 (二), 01-08 (三, 假設休市), 01-09 (四), 01-10 (五),
        # 01-11 (六), 01-12 (日), 01-13 (一), 01-14 (二), 01-15 (三)
        self.d1 = dt.date(2025, 1, 6)
        self.d2 = dt.date(2025, 1, 7)
        self.holiday_wed = dt.date(2025, 1, 8)
        self.d3 = dt.date(2025, 1, 9)
        self.d4 = dt.date(2025, 1, 10)
        self.d5 = dt.date(2025, 1, 13)
        self.d6 = dt.date(2025, 1, 14)
        self.d7 = dt.date(2025, 1, 15)

        self.trading_days = (self.d1, self.d2, self.d3, self.d4, self.d5, self.d6, self.d7)
        self.calendar = _make_calendar(
            self.trading_days,
            holidays=(self.holiday_wed,),
            coverage_start=dt.date(2024, 12, 1),
            coverage_end=dt.date(2025, 1, 31),
        )
        self.provenance = _make_provenance()

    def _build_stack(
        self,
        *,
        bars: Iterable[HistoricalBarRecord],
        initial_cash: str = "1000000.00",
        strategy_config: Optional[StrategyConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        sim_config: Optional[ExecutionSimConfig] = None,
        run_config: Optional[WalkForwardRunConfig] = None,
        prediction_feed: Optional[InMemoryPredictionFeed] = None,
        corporate_processor: Optional[CorporateActionProcessor] = None,
        symbol_contexts_by_date: Optional[Mapping[dt.date, Mapping[str, SymbolExecutionContext]]] = None,
        default_symbol_contexts: Optional[Mapping[str, SymbolExecutionContext]] = None,
    ) -> WalkForwardRunner:
        data_provider = InMemoryHistoricalDataProvider(
            calendar=self.calendar,
            provenance=self.provenance,
            bars=bars,
        )
        p_feed = prediction_feed or InMemoryPredictionFeed(
            calendar=self.calendar,
            provenance=self.provenance,
        )
        r_cfg = risk_config or RiskConfig(
            max_order_amount=Decimal("600000.00"),
            max_single_position_ratio=Decimal("0.60"),
            max_total_exposure_ratio=Decimal("0.95"),
            min_cash_reserve_ratio=Decimal("0.05"),
            max_daily_loss_ratio=Decimal("0.05"),
            max_drawdown_limit=Decimal("0.15"),
        )
        s_cfg = strategy_config or _make_strategy_config()
        strategy_engine = StrategyEngine(config=s_cfg, calendar=self.calendar, risk_config=r_cfg)

        allow_odd = (s_cfg.share_unit == 1)
        paper_cfg = TaiwanPaperConfig(
            initial_cash=Decimal(initial_cash),
            allow_odd_lots=allow_odd,
        )
        risk_engine = RiskEngine(config=r_cfg)
        broker = TaiwanPaperBroker(config=paper_cfg, risk_engine=risk_engine)
        gateway = TradingGateway(broker=broker, risk_engine=risk_engine)

        simulator = HistoricalExecutionSimulator(
            config=sim_config or ExecutionSimConfig(allow_odd_lots=allow_odd),
            calendar=self.calendar,
        )
        valuator = PortfolioValuator(
            calendar=self.calendar,
            provenance=self.provenance,
            data_provider=data_provider,
        )
        lot_ledger = BacktestLotLedger(calendar=self.calendar)
        corp_proc = corporate_processor or CorporateActionProcessor(calendar=self.calendar)
        w_cfg = run_config or WalkForwardRunConfig(
            start_date=self.d1,
            end_date=self.d2,
            universe_symbols=("2330", "2317"),
            skip_prediction_on_final_day=True,
        )

        return WalkForwardRunner(
            config=w_cfg,
            calendar=self.calendar,
            data_provider=data_provider,
            prediction_feed=p_feed,
            strategy_engine=strategy_engine,
            gateway=gateway,
            simulator=simulator,
            valuator=valuator,
            lot_ledger=lot_ledger,
            corporate_action_processor=corp_proc,
            symbol_contexts_by_date=symbol_contexts_by_date,
            default_symbol_contexts=default_symbol_contexts,
        )

    # ── 1. 單日 12 步因果執行順序測試 ─────────────────────────────────

    def test_01_daily_12_step_causal_sequence(self) -> None:
        bars = [
            _make_bar("2330", self.d1, "100", "102", "99", "100", "100"),
            _make_bar("2317", self.d1, "50", "51", "49", "50", "50"),
            _make_bar("2330", self.d2, "101", "103", "100", "102", "100"),
            _make_bar("2317", self.d2, "50", "52", "50", "51", "50"),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        batch_d1 = _make_prediction_batch(
            batch_id="B-D1",
            decision_date=self.d1,
            target_trade_date=self.d2,
            scores_and_ranks=[("2330", 0.90, 1), ("2317", 0.80, 2)],
        )
        feed.register_batch(
            batch_d1,
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )

        runner = self._build_stack(bars=bars, prediction_feed=feed)
        result = runner.run()

        self.assertEqual(result.status, BacktestRunStatus.COMPLETED)
        self.assertEqual(len(result.daily_logs), 2)
        for day_log in result.daily_logs:
            self.assertEqual(day_log.executed_steps, EXPECTED_DAILY_TIMELINE_SEQUENCE)
            self.assertEqual(len(day_log.executed_steps), 12)
            self.assertEqual(day_log.executed_steps[0], DailyTimelineStep.STEP_01_DIVIDEND_PAYOUT_0800)
            self.assertEqual(day_log.executed_steps[2], DailyTimelineStep.STEP_03_OPEN_MATCHING_0900)
            self.assertEqual(day_log.executed_steps[5], DailyTimelineStep.STEP_06_CLOSE_VALUATION_1500)
            self.assertEqual(day_log.executed_steps[7], DailyTimelineStep.STEP_08_CONSUME_PREDICTION_1800)
            self.assertEqual(day_log.executed_steps[11], DailyTimelineStep.STEP_12_DAILY_AUDIT_LOG_1800)

    # ── 2. T-1 盤後產生訊號 -> T 開盤撮合 -> T 收盤估值 ───────────────

    def test_02_t_minus_1_signal_matches_at_t_open_and_valued_at_t_close(self) -> None:
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "102", "106", "101", "105", "100"),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        batch_d1 = _make_prediction_batch(
            batch_id="B-D1",
            decision_date=self.d1,
            target_trade_date=self.d2,
            scores_and_ranks=[("2330", 0.92, 1)],
        )
        feed.register_batch(
            batch_d1,
            available_at=dt.datetime.combine(self.d1, dt.time(17, 15, 0), tzinfo=TAIPEI_TZ),
        )

        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            strategy_config=_make_strategy_config(top_n=1, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d2,
                universe_symbols=("2330",),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()

        log_d1, log_d2 = result.daily_logs
        # Day 1 (T-1): 無開盤成交，盤後產生 1 筆買單並排入 T 待執行佇列
        self.assertEqual(len(log_d1.settled_fills), 0)
        self.assertEqual(len(log_d1.queued_orders_for_next_day), 1)
        queued_order = log_d1.queued_orders_for_next_day[0]
        self.assertEqual(queued_order.symbol, "2330")
        self.assertEqual(queued_order.order_quantity, 4000)
        # Day 1 15:00 收盤估值時尚未產生盤後委託，總 NAV 為 1,000,000.00；
        # Day 1 18:00 Step 10 盤後訊號核准後，立即預留買進現金
        self.assertEqual(log_d1.nav_record.total_nav, Decimal("1000000.00"))
        self.assertEqual(log_d1.nav_record.reserved_cash, Decimal("0.00"))
        assert log_d1.dispatch_summary is not None
        self.assertGreater(log_d1.dispatch_summary.audit_records[0].reserved_cash_after, Decimal("0.00"))

        # Day 2 (T): 09:00 開盤撮合 (open=102, 滑價 5bps -> 102.051 -> 向上對齊 Tick 0.50 -> 102.50)
        self.assertEqual(len(log_d2.settled_fills), 1)
        fill = log_d2.settled_fills[0]
        self.assertEqual(fill.symbol, "2330")
        self.assertEqual(fill.fill_quantity, 4000)
        self.assertEqual(fill.fill_price, Decimal("102.50"))
        self.assertEqual(
            fill.timestamp,
            dt.datetime.combine(self.d2, dt.time(9, 0, 0), tzinfo=TAIPEI_TZ),
        )

        # Day 2 15:00 收盤以 105.00 估值：持倉市值 = 4000 * 105 = 420,000.00，預留現金歸零
        self.assertEqual(log_d2.nav_record.reserved_cash, Decimal("0.00"))
        self.assertEqual(log_d2.nav_record.market_value, Decimal("420000.00"))
        expected_cash = Decimal("1000000.00") - fill.notional - fill.commission
        self.assertEqual(log_d2.nav_record.available_cash, expected_cash)
        self.assertEqual(log_d2.nav_record.total_nav, expected_cash + Decimal("420000.00"))

    # ── 3. 禁止 T 盤後訊號在 T 當日成交 ───────────────────────────────

    def test_03_prevent_same_day_execution_of_post_market_signal(self) -> None:
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "101", "102", "100", "101", "100"),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        batch_d1 = _make_prediction_batch(
            batch_id="B-D1",
            decision_date=self.d1,
            target_trade_date=self.d2,
            scores_and_ranks=[("2330", 0.90, 1)],
        )
        feed.register_batch(
            batch_d1,
            available_at=dt.datetime.combine(self.d1, dt.time(17, 15, 0), tzinfo=TAIPEI_TZ),
        )
        runner = self._build_stack(bars=bars, prediction_feed=feed)
        log_d1 = runner.run_single_day(self.d1)

        # Day 1 盤後產生的委託必須留在 queued_orders_for_next_day，Day 1 持倉必須為 0
        self.assertEqual(len(log_d1.settled_fills), 0)
        self.assertEqual(runner.lot_ledger.get_position_quantity("2330"), 0)
        self.assertEqual(len(log_d1.queued_orders_for_next_day), 1)
        order_d1 = log_d1.queued_orders_for_next_day[0]
        self.assertEqual(order_d1.status, PaperOrderStatus.ACCEPTED)

        # 若外部強制嘗試將 Day 1 18:00 盤後產生的委託放回 Day 1 行情撮合，必須拋出 LookaheadBiasError
        bar_d1 = bars[0]
        with self.assertRaises(LookaheadBiasError):
            runner.simulator.match_order(order_d1, bar_d1)

    # ── 4. 禁止未來 PredictionBatch 洩漏與未驗證預測攔截 ──────────────

    def test_04_prevent_future_prediction_leakage_and_missing_available_at(self) -> None:
        feed_reject = InMemoryPredictionFeed(
            calendar=self.calendar,
            provenance=self.provenance,
            unverified_policy=UnverifiedPredictionPolicy.REJECT,
        )
        batch_d1 = _make_prediction_batch(
            batch_id="B-UNVERIFIED",
            decision_date=self.d1,
            target_trade_date=self.d2,
            scores_and_ranks=[("2330", 0.85, 1)],
        )
        # 4a. 缺少 available_at 在預設 REJECT 政策下立即拋出 PITViolationError
        with self.assertRaises(PITViolationError):
            feed_reject.register_batch(batch_d1, available_at=None)

        # 4b. 在 QUARANTINE 政策下，缺少 available_at 的批次被隔離且不可被消費
        feed_quarantine = InMemoryPredictionFeed(
            calendar=self.calendar,
            provenance=self.provenance,
            unverified_policy=UnverifiedPredictionPolicy.QUARANTINE,
        )
        entry_q = feed_quarantine.register_batch(batch_d1, available_at=None)
        self.assertIsNone(entry_q)
        self.assertEqual(len(feed_quarantine.quarantined_records), 1)
        self.assertFalse(feed_quarantine.has_batch_for_decision_date(self.d1))

        # 4c. available_at 晚於決策時點 (例如 20:00 才可用，但決策在 18:00) 必須拋出 LookaheadBiasError
        feed_lookahead = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        batch_late = _make_prediction_batch(
            batch_id="B-LATE-AVAIL",
            decision_date=self.d1,
            target_trade_date=self.d2,
            scores_and_ranks=[("2330", 0.85, 1)],
        )
        feed_lookahead.register_batch(
            batch_late,
            available_at=dt.datetime.combine(self.d1, dt.time(20, 0, 0), tzinfo=TAIPEI_TZ),
        )
        with self.assertRaises(LookaheadBiasError):
            feed_lookahead.consume_batch(
                self.d1,
                dt.datetime.combine(self.d1, dt.time(18, 0, 0), tzinfo=TAIPEI_TZ),
            )

        # 4d. 試圖將未來日期 (d2) 的預測標記於 d1 決策，或 batch.as_of_date == d1 (同日回溯) 必須拋出例外
        feed_same_day = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        with self.assertRaises(LookaheadBiasError):
            bad_same_day_batch = _make_prediction_batch(
                batch_id="B-SAME-DAY",
                decision_date=self.d1,
                target_trade_date=self.d1,
                scores_and_ranks=[("2330", 0.85, 1)],
            )
            feed_same_day.register_batch(
                bad_same_day_batch,
                available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

    # ── 5. 模型 trained_cutoff_date 晚於測試區間或侵入 Embargo 時拒絕 ────

    def test_05_reject_trained_cutoff_date_lookahead_and_embargo_violation(self) -> None:
        feed = InMemoryPredictionFeed(
            calendar=self.calendar,
            provenance=self.provenance,
            require_trained_cutoff_date=True,
            default_embargo_days=2,
        )

        # 5a. trained_cutoff_date 晚於 decision_date (未來訓練資料洩漏) -> LookaheadBiasError
        batch_future_train = _make_prediction_batch(
            batch_id="B-FUTURE-TRAIN",
            decision_date=self.d2,
            target_trade_date=self.d3,
            scores_and_ranks=[("2330", 0.90, 1)],
            trained_cutoff_date=self.d3,
        )
        with self.assertRaises(LookaheadBiasError):
            feed.register_batch(
                batch_future_train,
                available_at=dt.datetime.combine(self.d2, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

        # 5b. trained_cutoff_date 侵入 Embargo 隔離天數 (trained_cutoff=d1, decision=d2, 僅相隔 1 交易日 < embargo=2)
        batch_embargo_breach = _make_prediction_batch(
            batch_id="B-EMBARGO-BREACH",
            decision_date=self.d2,
            target_trade_date=self.d3,
            scores_and_ranks=[("2330", 0.90, 1)],
            trained_cutoff_date=self.d1,
        )
        with self.assertRaises(EmbargoViolationError):
            feed.register_batch(
                batch_embargo_breach,
                available_at=dt.datetime.combine(self.d2, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

        # 5c. WalkForwardWindowSpec 訓練結束日與測試開始日間隔不足 embargo_days -> EmbargoViolationError
        bad_win = WalkForwardWindowSpec(
            window_id="WIN-BAD-GAP",
            train_start_date=self.d1,
            train_end_date=self.d2,
            embargo_days=2,
            test_start_date=self.d3,  # d2 與 d3 僅相隔 0 個中間交易日 < 2
            test_end_date=self.d5,
        )
        with self.assertRaises(EmbargoViolationError):
            bad_win.validate_with_calendar(self.calendar)

        # 5d. 合法的 WalkForwardWindowSpec 與滿足 Embargo 的批次可成功註冊
        good_win = WalkForwardWindowSpec(
            window_id="WIN-VALID",
            train_start_date=self.d1,
            train_end_date=self.d1,
            embargo_days=2,
            test_start_date=self.d4,  # d1 -> d2 (1), d3 (2) -> d4 測試開始
            test_end_date=self.d6,
        )
        good_win.validate_with_calendar(self.calendar)
        valid_batch = _make_prediction_batch(
            batch_id="B-VALID-WF",
            decision_date=self.d3,
            target_trade_date=self.d4,
            scores_and_ranks=[("2330", 0.90, 1)],
            trained_cutoff_date=self.d1,
        )
        entry = feed.register_batch(
            valid_batch,
            available_at=dt.datetime.combine(self.d3, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            window_spec=good_win,
        )
        self.assertIsNotNone(entry)

    # ── 6. 固定持有天數到期自動產生出場訊號並在下一交易日成交 ──────────

    def test_06_fixed_holding_trading_days_expiry_exit_on_next_day(self) -> None:
        # 設定 holding_trading_days = 2：
        # - d1 (01-06) 盤後產生 BUY 2330 訊號 -> d2 (01-07) 開盤買進成交，entry_date = d2 (01-07)
        # - d2 (01-07) 盤後評估目標日 d3 (01-09, 因 01-08 休市)：trading_days_between(d2, d3) = 1 < 2 -> HOLD
        # - d3 (01-09) 盤後評估目標日 d4 (01-10)：trading_days_between(d2, d4) = 2 >= 2 -> 觸發 HOLDING_PERIOD_EXPIRED (CLOSE)
        # - d4 (01-10) 09:00 開盤執行 CLOSE 賣出成交，持倉歸零，ClosedLotTrade 紀錄 holding_trading_days == 2
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "100", "103", "99", "102", "100"),
            _make_bar("2330", self.d3, "102", "105", "101", "104", "102"),
            _make_bar("2330", self.d4, "105", "107", "104", "106", "104"),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        for idx, (dec_d, tgt_d) in enumerate(
            [(self.d1, self.d2), (self.d2, self.d3), (self.d3, self.d4)], start=1
        ):
            # 即使 AI 分數每天都很高 (0.95)，到期時仍必須強制觸發 HOLDING_PERIOD_EXPIRED 平倉且不再同日買回
            b = _make_prediction_batch(
                batch_id=f"B-EXP-{idx}",
                decision_date=dec_d,
                target_trade_date=tgt_d,
                scores_and_ranks=[("2330", 0.95, 1)],
            )
            feed.register_batch(
                b,
                available_at=dt.datetime.combine(dec_d, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            strategy_config=_make_strategy_config(
                top_n=1,
                max_position_weight="0.41",
                holding_trading_days=2,
            ),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d4,
                universe_symbols=("2330",),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()

        log_d1, log_d2, log_d3, log_d4 = result.daily_logs

        # d2 開盤買進，d2 盤後策略決策為 HOLD
        self.assertEqual(len(log_d2.settled_fills), 1)
        assert log_d2.strategy_summary is not None
        self.assertEqual(len(log_d2.strategy_summary.hold_decisions), 1)
        self.assertEqual(len(log_d2.strategy_summary.close_decisions), 0)

        # d3 盤後策略決策為 CLOSE (HOLDING_PERIOD_EXPIRED)
        assert log_d3.strategy_summary is not None
        self.assertEqual(len(log_d3.strategy_summary.close_decisions), 1)
        close_dec = log_d3.strategy_summary.close_decisions[0]
        self.assertEqual(close_dec.action, StrategyAction.CLOSE)
        self.assertEqual(close_dec.reason, StrategyReason.HOLDING_PERIOD_EXPIRED)
        self.assertEqual(len(log_d3.queued_orders_for_next_day), 1)
        self.assertEqual(log_d3.queued_orders_for_next_day[0].action, SignalAction.CLOSE)

        # d4 開盤平倉成交，收盤持倉為 0，ClosedLotTrade 記錄進場 d2、出場 d4、持有 2 個交易日
        self.assertEqual(len(log_d4.settled_fills), 1)
        self.assertEqual(runner.lot_ledger.get_position_quantity("2330"), 0)
        self.assertEqual(len(result.closed_lot_trades), 1)
        closed_trade = result.closed_lot_trades[0]
        self.assertEqual(closed_trade.entry_date, self.d2)
        self.assertEqual(closed_trade.exit_date, self.d4)
        self.assertEqual(closed_trade.holding_trading_days, 2)

    # ── 7. 除息日股價下跌但含 dividends_receivable 時不誤觸風控熔斷 ────

    def test_07_ex_dividend_drop_with_receivable_does_not_trigger_risk_circuit_breaker(self) -> None:
        # 情境：
        # - d1 盤後發出買單買進 2330 -> d2 開盤以 100.00 買進 5,000 股 (市值約 50 萬，佔總資產 50%)
        # - d3 為 2330 除息日：每股配發現金股利 12.00 元 (12% 殖利率)，股價由 100.00 除息開盤/收盤跌至 88.00 (-12%)
        # - 若不計入應收股利 (5,000 * 12 = 60,000 元)，帳面將出現 -60,000 元 (-6% 總資產虧損)，
        #   超過 max_daily_loss_ratio (3%) 與 max_drawdown_limit (5%)！
        # - 因 BacktestRiskContext 與 PortfolioValuator 正確計入 dividends_receivable = 60,000 元，
        #   當日真實總資產變動為 0，絕不誤觸日虧損限制或最大回撤熔斷，且 d3 盤後可正常核准買進 2317！
        bars = [
            _make_bar("2330", self.d1, "100", "100", "100", "100", "100"),
            _make_bar("2317", self.d1, "50", "50", "50", "50", "50"),
            _make_bar("2330", self.d2, "100", "100", "100", "100", "100"),
            _make_bar("2317", self.d2, "50", "50", "50", "50", "50"),
            _make_bar("2330", self.d3, "88", "88", "88", "88", "88"),  # 除息參考價 88
            _make_bar("2317", self.d3, "50", "50", "50", "50", "50"),
            _make_bar("2330", self.d4, "88", "89", "88", "89", "88"),
            _make_bar("2317", self.d4, "50", "51", "50", "51", "50"),
        ]
        corp_proc = CorporateActionProcessor(calendar=self.calendar)
        div_event = CorporateActionEvent(
            event_id="CA-2330-2025Q1",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=self.d3,
            payable_date=self.d6,
            cash_dividend_per_share=Decimal("12.00"),
            announced_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 1, 2, 18, 30, tzinfo=TAIPEI_TZ),
            provenance=self.provenance,
            is_synthetic_payable_date=True,
        )
        corp_proc.register_event(div_event)

        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        # d1 推薦 2330 -> d2 買進
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-DIV-1",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.92, 1), ("2317", 0.30, 2)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        # d2 續抱 2330
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-DIV-2",
                decision_date=self.d2,
                target_trade_date=self.d3,
                scores_and_ranks=[("2330", 0.92, 1), ("2317", 0.30, 2)],
            ),
            available_at=dt.datetime.combine(self.d2, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        # d3 (除息日) 同時推薦買進 2317 -> 驗證風控不會因除息股價下調而拒絕新買單
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-DIV-3",
                decision_date=self.d3,
                target_trade_date=self.d4,
                scores_and_ranks=[("2330", 0.92, 1), ("2317", 0.90, 2)],
            ),
            available_at=dt.datetime.combine(self.d3, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )

        strict_risk_cfg = RiskConfig(
            max_order_amount=Decimal("600000.00"),
            max_single_position_ratio=Decimal("0.60"),
            max_total_exposure_ratio=Decimal("0.95"),
            min_cash_reserve_ratio=Decimal("0.05"),
            max_daily_loss_ratio=Decimal("0.03"),   # 嚴格 3% 單日虧損上限
            max_drawdown_limit=Decimal("0.04"),     # 嚴格 4% 最大回撤熔斷上限
        )
        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            corporate_processor=corp_proc,
            risk_config=strict_risk_cfg,
            strategy_config=_make_strategy_config(top_n=2, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d4,
                universe_symbols=("2330", "2317"),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()

        log_d2 = result.daily_logs[1]
        log_d3 = result.daily_logs[2]
        log_d4 = result.daily_logs[3]

        # d3 除息日產生應收股利 4,000 股 * 12 = 48,000 元，收盤總 NAV 與 d2 收盤完全相同
        self.assertEqual(len(log_d3.ex_dividend_entitlements), 1)
        self.assertEqual(log_d3.nav_record.dividends_receivable, Decimal("48000.00"))
        self.assertEqual(log_d3.nav_record.total_nav, log_d2.nav_record.total_nav)
        self.assertEqual(log_d3.daily_unrealized_pnl, Decimal("0.00"))
        self.assertFalse(log_d3.circuit_breaker_active)
        self.assertFalse(log_d3.kill_switch_active)

        # d3 盤後產生之 2317 買單順利通過風控並於 d4 開盤成交
        assert log_d3.dispatch_summary is not None
        self.assertEqual(len(log_d3.dispatch_summary.rejected_signals), 0)
        self.assertTrue(any(o.symbol == "2317" for o in log_d3.queued_orders_for_next_day))
        self.assertTrue(any(f.symbol == "2317" for f in log_d4.settled_fills))

    # ── 8. dividends_receivable 在 payable_date 前不可買股，入帳後才可動用 ──

    def test_08_dividends_receivable_cannot_fund_buys_until_payable_date(self) -> None:
        # 情境：
        # - 初始資金 100,000 元；d1 推薦 2330 (收盤 44.00) -> d2 開盤以 48.05 買進 1,000 股 (耗資 48,091 元，剩餘現金 51,909 元)
        # - d3 為 2330 除息日，每股配息 4.00 元 (應收股利 = 4,000 元，payable_date = d4)，2330 除息後填息收 48.00
        # - d3 盤後：可用現金 51,909 元，扣掉 10% 最低現金保留 (9,990.90 元) 後，可支配現金僅 41,918.10 元，
        #   不足以買進 1 張 (1,000 股) 股價 43.00 元的 2317 (需 43,058.50 元，雖低於目標部位上限 44,959.05 元)！
        #   驗證策略絕不會拿 4,000 元的應收股利去買 2317（2317 決策為 SKIP: INSUFFICIENT_CASH）。
        # - d4 (發放日) 08:00 Step 01：4,000 元股利正式入帳至 cash (可用現金增至 55,909 元，扣除保留金後可支配 ~45,518 元)；
        #   d4 盤後策略即可使用該筆已入帳現金買進 2317 並於 d5 開盤成交！
        bars = [
            _make_bar("2330", self.d1, "44", "44.5", "43.5", "44", "44"),
            _make_bar("2317", self.d1, "43", "43.5", "42.5", "43", "43"),
            _make_bar("2330", self.d2, "48", "48.3", "47.8", "48.2", "44"),
            _make_bar("2317", self.d2, "43", "43.5", "42.5", "43", "43"),
            _make_bar("2330", self.d3, "45", "48.2", "44.5", "48.0", "44.2"),
            _make_bar("2317", self.d3, "43", "43.5", "42.5", "43", "43"),
            _make_bar("2330", self.d4, "48", "48.5", "47.5", "48.0", "48.0"),
            _make_bar("2317", self.d4, "43", "43.5", "42.5", "43", "43"),
            _make_bar("2330", self.d5, "48", "48.5", "47.5", "48.0", "48.0"),
            _make_bar("2317", self.d5, "43", "43.5", "42.5", "43", "43"),
        ]
        corp_proc = CorporateActionProcessor(calendar=self.calendar)
        corp_proc.register_event(
            CorporateActionEvent(
                event_id="CA-2330-BIGDIV",
                symbol="2330",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=self.d3,
                payable_date=self.d4,
                cash_dividend_per_share=Decimal("4.00"),
                announced_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 1, 2, 18, 30, tzinfo=TAIPEI_TZ),
                provenance=self.provenance,
                is_synthetic_payable_date=True,
            )
        )

        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-PAY-1",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.95, 1), ("2317", 0.20, 2)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-PAY-2",
                decision_date=self.d2,
                target_trade_date=self.d3,
                scores_and_ranks=[("2330", 0.95, 1), ("2317", 0.20, 2)],
            ),
            available_at=dt.datetime.combine(self.d2, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        # d3 (除息日，股利尚未發放) 推薦買進 2317
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-PAY-3",
                decision_date=self.d3,
                target_trade_date=self.d4,
                scores_and_ranks=[("2330", 0.95, 1), ("2317", 0.90, 2)],
            ),
            available_at=dt.datetime.combine(self.d3, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        # d4 (發放日，股利已於 08:00 入帳) 推薦買進 2317
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-PAY-4",
                decision_date=self.d4,
                target_trade_date=self.d5,
                scores_and_ranks=[("2330", 0.95, 1), ("2317", 0.90, 2)],
            ),
            available_at=dt.datetime.combine(self.d4, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )

        runner = self._build_stack(
            bars=bars,
            initial_cash="100000.00",
            prediction_feed=feed,
            corporate_processor=corp_proc,
            strategy_config=_make_strategy_config(
                top_n=2,
                max_position_weight="0.45",
                min_cash_reserve_pct="0.10",
                holding_trading_days=10,
            ),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d5,
                universe_symbols=("2330", "2317"),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()

        log_d3 = result.daily_logs[2]
        log_d4 = result.daily_logs[3]
        log_d5 = result.daily_logs[4]

        # d3 除息日：應收股利 4,000 元，但可用現金扣除最低現金保留後僅 ~41,918 元，無法買進 2317 (1,000 股需 43,058.50 元)
        self.assertEqual(log_d3.nav_record.dividends_receivable, Decimal("4000.00"))
        assert log_d3.strategy_summary is not None
        self.assertEqual(
            log_d3.strategy_summary.portfolio_snapshot.cash,
            log_d3.nav_record.available_cash,
        )
        self.assertFalse(any(o.symbol == "2317" for o in log_d3.queued_orders_for_next_day))
        self.assertTrue(
            any(
                d.symbol == "2317" and d.reason == StrategyReason.INSUFFICIENT_CASH
                for d in log_d3.strategy_summary.skip_decisions
            )
        )

        # d4 發放日：Step 01 入帳 4,000 元，應收股利歸零，d4 盤後成功買進 2317 並於 d5 開盤成交
        self.assertEqual(len(log_d4.dividend_payouts), 1)
        self.assertEqual(log_d4.dividend_payouts[0].net_dividend, Decimal("4000.00"))
        self.assertEqual(log_d4.nav_record.dividends_receivable, Decimal("0.00"))
        self.assertTrue(any(o.symbol == "2317" for o in log_d4.queued_orders_for_next_day))
        self.assertTrue(any(f.symbol == "2317" for f in log_d5.settled_fills))

    # ── 9. RiskEngine 拒絕訊號後不得產生有效委託或成交 ────────────────

    def test_09_risk_rejected_signal_creates_no_order_or_reservation_or_fill(self) -> None:
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "100", "101", "99", "100", "100"),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-RISK-REJ",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.95, 1)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            strategy_config=_make_strategy_config(top_n=1, max_position_weight="0.45"),
        )
        # 啟動獨立風控引擎之 Kill Switch 以驗證 Step 10 風控拒絕訊號時不產生任何委託或預留
        runner.risk_engine.activate_kill_switch("risk_officer", "模擬緊急停止風控攔截測試")

        result = runner.run()
        log_d1, log_d2 = result.daily_logs

        assert log_d1.dispatch_summary is not None
        self.assertEqual(len(log_d1.dispatch_summary.rejected_signals), 1)
        self.assertEqual(len(log_d1.dispatch_summary.approved_orders_queued), 0)
        self.assertIn(
            RiskRejectCode.KILL_SWITCH_ACTIVE.value,
            log_d1.dispatch_summary.risk_rejections_by_code,
        )
        # 驗證無任何預留現金、無排隊委託、無成交
        self.assertEqual(runner.broker.ledger.reserved_cash, Decimal("0.00"))
        self.assertEqual(len(log_d1.queued_orders_for_next_day), 0)
        self.assertEqual(len(log_d2.settled_fills), 0)
        self.assertEqual(result.final_cash, Decimal("1000000.00"))

    # ── 10. 漲停買不進、跌停賣不出時，收盤到期並正確釋放預留款券 ──────

    def test_10_limit_up_buy_and_limit_down_sell_unfilled_expire_and_release_reservations(self) -> None:
        # 階段 A (d1 -> d2)：d1 推薦買進 2330 與 2317；
        # d2 開盤 2330 一字漲停鎖死 (昨收 100, 漲停 110)，買單無法成交，收盤 EXPIRED 並釋放預留現金；
        # 同日 2317 正常開盤 50 成交買進。
        # 階段 B (d2 -> d3)：d2 盤後因 2317 分數跌破門檻產生 CLOSE 賣單；
        # d3 開盤 2317 一字跌停鎖死 (昨收 50, 跌停 45)，賣單無法成交，收盤 EXPIRED 並釋放預留股數！
        bars = [
            _make_bar("2330", self.d1, "100", "100", "100", "100", "100"),
            _make_bar("2317", self.d1, "50", "50", "50", "50", "50"),
            # d2: 2330 漲停鎖死在 110；2317 正常在 50 交易
            _make_bar("2330", self.d2, "110", "110", "110", "110", "100", is_limit_up_locked=True),
            _make_bar("2317", self.d2, "50", "50", "50", "50", "50"),
            # d3: 2330 正常 110；2317 跌停鎖死在 45
            _make_bar("2330", self.d3, "110", "110", "110", "110", "110"),
            _make_bar("2317", self.d3, "45", "45", "45", "45", "50", is_limit_down_locked=True),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-LIM-1",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.95, 1), ("2317", 0.90, 2)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        # d2 盤後：2317 分數跌至 -0.5 (< exit_on_score_drop_below=0.0)，觸發平倉賣單
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-LIM-2",
                decision_date=self.d2,
                target_trade_date=self.d3,
                scores_and_ranks=[("2330", 0.20, 1), ("2317", -0.50, 2)],
            ),
            available_at=dt.datetime.combine(self.d2, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )

        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            strategy_config=_make_strategy_config(top_n=2, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d3,
                universe_symbols=("2330", "2317"),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()
        log_d1, log_d2, log_d3 = result.daily_logs

        # d2 檢查：2330 買單因 UNFILLED_LIMIT_UP_LOCKED 未成交，並於 13:30 收盤轉為 EXPIRED，預留現金完整釋放
        outcomes_d2 = {o.symbol: o for o in log_d2.matched_order_outcomes}
        self.assertEqual(outcomes_d2["2330"].outcome_code, ExecutionOutcomeCode.UNFILLED_LIMIT_UP_LOCKED)
        self.assertEqual(outcomes_d2["2317"].outcome_code, ExecutionOutcomeCode.FILLED)
        self.assertEqual(len(log_d2.expired_orders_at_close), 1)
        self.assertEqual(log_d2.expired_orders_at_close[0].symbol, "2330")
        self.assertEqual(log_d2.expired_orders_at_close[0].status, PaperOrderStatus.EXPIRED)
        self.assertEqual(log_d2.nav_record.reserved_cash, Decimal("0.00"))

        # d3 檢查：2317 賣單因 UNFILLED_LIMIT_DOWN_LOCKED 未成交，於 13:30 收盤轉為 EXPIRED，預留股數完整釋放回可用股數
        outcomes_d3 = {o.symbol: o for o in log_d3.matched_order_outcomes}
        self.assertEqual(outcomes_d3["2317"].outcome_code, ExecutionOutcomeCode.UNFILLED_LIMIT_DOWN_LOCKED)
        self.assertEqual(len(log_d3.expired_orders_at_close), 1)
        self.assertEqual(log_d3.expired_orders_at_close[0].symbol, "2317")
        self.assertEqual(log_d3.expired_orders_at_close[0].status, PaperOrderStatus.EXPIRED)
        self.assertEqual(runner.broker.ledger.reserved_shares.get("2317", 0), 0)
        self.assertEqual(
            runner.broker.ledger.get_available_shares("2317"),
            runner.lot_ledger.get_position_quantity("2317"),
        )

    # ── 11. 部分成交後剩餘股數收盤到期，帳務與預留正確更新 ────────────

    def test_11_partial_fill_at_open_then_remainder_expires_at_close_with_exact_accounting(self) -> None:
        # d1 盤後排入買單 4,000 股 2330；
        # d2 當日成交量僅 40,000 股，參與率上限 5% -> 最多僅能成交 2,000 股 (PARTIALLY_FILLED_VOLUME_CAP)；
        # 收盤時剩餘 2,000 股轉為 EXPIRED，且剩餘預留資金全數釋放，reserved_cash == 0.00！
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100", volume=500_000),
            _make_bar("2330", self.d2, "100", "101", "99", "100", "100", volume=40_000),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-PARTIAL",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.92, 1)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            strategy_config=_make_strategy_config(top_n=1, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d2,
                universe_symbols=("2330",),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()
        log_d1, log_d2 = result.daily_logs

        self.assertEqual(log_d1.queued_orders_for_next_day[0].order_quantity, 4000)
        self.assertEqual(len(log_d2.matched_order_outcomes), 1)
        out = log_d2.matched_order_outcomes[0]
        self.assertEqual(out.outcome_code, ExecutionOutcomeCode.PARTIALLY_FILLED_VOLUME_CAP)
        self.assertEqual(out.filled_quantity, 2000)
        self.assertEqual(out.unfilled_quantity, 2000)

        # Step 05 收盤將該部分成交委託轉為 EXPIRED 並釋放剩餘預留金
        self.assertEqual(len(log_d2.expired_orders_at_close), 1)
        expired_order = log_d2.expired_orders_at_close[0]
        self.assertEqual(expired_order.status, PaperOrderStatus.EXPIRED)
        self.assertEqual(expired_order.filled_quantity, 2000)
        self.assertEqual(runner.broker.ledger.reserved_cash, Decimal("0.00"))
        self.assertEqual(runner.lot_ledger.get_position_quantity("2330"), 2000)

        fill = log_d2.settled_fills[0]
        expected_cash = Decimal("1000000.00") - fill.notional - fill.commission
        self.assertEqual(result.final_cash, expected_cash)

    # ── 12. 停牌股期間：不可成交、以最後已知收盤價估值並附警告 ────────

    def test_12_halted_stock_blocks_execution_and_values_at_last_known_close_with_warning(self) -> None:
        # d1 推薦 2330 -> d2 開盤買進 4,000 股 (d2 收盤價 102.00)
        # d2 盤後因分數轉負產生 2330 平倉賣單；
        # d3 2330 全日停牌 (is_halted=True, volume=0)：
        #   - Step 03 賣單因 UNFILLED_HALTED 無法成交
        #   - Step 05 賣單到期釋放預留股數
        #   - Step 06 估值器使用 d2 最後已知收盤價 102.00 估值，標記 LAST_KNOWN_CLOSE_HALTED 並發出警告
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "100", "103", "100", "102", "100"),
            _make_bar("2330", self.d3, "102", "102", "102", "102", "102", volume=0, is_halted=True),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-HALT-1",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.90, 1)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        feed.register_batch(
            _make_prediction_batch(
                batch_id="B-HALT-2",
                decision_date=self.d2,
                target_trade_date=self.d3,
                scores_and_ranks=[("2330", -0.20, 1)],
            ),
            available_at=dt.datetime.combine(self.d2, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )

        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            strategy_config=_make_strategy_config(top_n=1, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d3,
                universe_symbols=("2330",),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()
        log_d3 = result.daily_logs[2]

        self.assertEqual(len(log_d3.matched_order_outcomes), 1)
        self.assertEqual(log_d3.matched_order_outcomes[0].outcome_code, ExecutionOutcomeCode.UNFILLED_HALTED)
        self.assertEqual(len(log_d3.settled_fills), 0)
        self.assertEqual(len(log_d3.expired_orders_at_close), 1)
        self.assertEqual(runner.broker.ledger.reserved_shares.get("2330", 0), 0)

        # 檢查 d3 停牌估值來源與警告
        self.assertEqual(
            log_d3.nav_record.price_sources["2330"],
            ValuationPriceSource.LAST_KNOWN_CLOSE_HALTED,
        )
        self.assertEqual(log_d3.nav_record.price_dates["2330"], self.d2)
        self.assertEqual(log_d3.nav_record.valuation_prices["2330"], Decimal("102.00"))
        self.assertTrue(
            any(w.code == ValuationWarningCode.HALTED_LAST_KNOWN_PRICE_USED for w in log_d3.nav_record.warnings)
        )

    # ── 13. 週末與休市日自動跳過，不執行撮合、估值或策略評估 ──────────

    def test_13_weekends_and_holidays_automatically_skipped(self) -> None:
        # 區間 d2 (01-07 週二) 至 d5 (01-13 週一)：
        # 中間包含 01-08 (週三國定假日休市) 及 01-11、01-12 (週末)。
        # 實際有效交易日僅為：(d2=01-07, d3=01-09, d4=01-10, d5=01-13) 共 4 天！
        bars = [
            _make_bar("2330", d, "100", "101", "99", "100", "100")
            for d in (self.d2, self.d3, self.d4, self.d5)
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        for dec_d, tgt_d in [(self.d2, self.d3), (self.d3, self.d4), (self.d4, self.d5)]:
            feed.register_batch(
                _make_prediction_batch(
                    batch_id=f"B-CAL-{dec_d.isoformat()}",
                    decision_date=dec_d,
                    target_trade_date=tgt_d,
                    scores_and_ranks=[("2330", 0.85, 1)],
                ),
                available_at=dt.datetime.combine(dec_d, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            run_config=WalkForwardRunConfig(
                start_date=self.d2,
                end_date=self.d5,
                universe_symbols=("2330",),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()

        self.assertEqual(result.trading_days_executed, (self.d2, self.d3, self.d4, self.d5))
        self.assertNotIn(self.holiday_wed, result.trading_days_executed)
        self.assertNotIn(dt.date(2025, 1, 11), result.trading_days_executed)
        self.assertNotIn(dt.date(2025, 1, 12), result.trading_days_executed)

        # 直接以非交易日呼叫 run_single_day 必須拋出例外
        with self.assertRaises(Exception):
            runner.run_single_day(self.holiday_wed)

    # ── 14. 缺少當日 PredictionBatch 時依設定拒絕或安全跳過 ──────────

    def test_14_missing_prediction_batch_fail_fast_vs_skip_strategy_hold(self) -> None:
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "100", "102", "99", "101", "100"),
            _make_bar("2330", self.d3, "101", "103", "100", "102", "101"),
        ]

        # 14a. FAIL_FAST 模式：d2 缺少 PredictionBatch 時立即拋出 PredictionBatchUnavailableError
        feed_fail = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed_fail.register_batch(
            _make_prediction_batch(
                batch_id="B-MISS-1",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.90, 1)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        runner_fail = self._build_stack(
            bars=bars,
            prediction_feed=feed_fail,
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d3,
                universe_symbols=("2330",),
                missing_prediction_policy=MissingPredictionPolicy.FAIL_FAST,
                skip_prediction_on_final_day=True,
            ),
        )
        with self.assertRaises(PredictionBatchUnavailableError):
            runner_fail.run()

        # 14b. SKIP_STRATEGY_HOLD 模式：d2 缺少 PredictionBatch 時記錄警告、續抱既有持倉並完成回測
        feed_skip = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        feed_skip.register_batch(
            _make_prediction_batch(
                batch_id="B-SKIP-1",
                decision_date=self.d1,
                target_trade_date=self.d2,
                scores_and_ranks=[("2330", 0.90, 1)],
            ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )
        runner_skip = self._build_stack(
            bars=bars,
            prediction_feed=feed_skip,
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d3,
                universe_symbols=("2330",),
                missing_prediction_policy=MissingPredictionPolicy.SKIP_STRATEGY_HOLD,
                skip_prediction_on_final_day=True,
            ),
        )
        res_skip = runner_skip.run()
        self.assertEqual(res_skip.status, BacktestRunStatus.COMPLETED)
        self.assertEqual(len(res_skip.daily_logs), 3)
        self.assertIsNone(res_skip.daily_logs[1].strategy_summary)
        self.assertTrue(any("MISSING_PREDICTION_BATCH" in w for w in res_skip.daily_logs[1].warnings))
        self.assertGreater(runner_skip.lot_ledger.get_position_quantity("2330"), 0)

    # ── 15. 多日連續回測後，帳務、損益、股利與 total_nav 完全守恆 ──────

    def test_15_multi_day_end_to_end_accounting_and_nav_conservation(self) -> None:
        # 完整 6 個交易日 (d1~d6) 連續 Walk-Forward 回測：
        # 涵蓋：新買進 (BUY)、部分減碼 (SELL OVER_TARGET_TRIM)、輪動平倉 (CLOSE)、除息與股利實收
        bars = [
            # d1: 2330 收盤 19.00 (排入買單 20,000 股)，2317 收盤 38.00 (排入買單 10,000 股)
            _make_bar("2330", self.d1, "19", "19.5", "18.8", "19", "19"),
            _make_bar("2317", self.d1, "38", "38.5", "37.5", "38", "38"),
            # d2: 開盤買進 2330 (20,000 股) 與 2317 (10,000 股)；2330 收盤漲至 20.80 (< 漲停 20.90)
            _make_bar("2330", self.d2, "19", "20.8", "19", "20.8", "19"),
            _make_bar("2317", self.d2, "38", "38.5", "37.8", "38.0", "38"),
            # d3: 2330 續漲至 22.00 (< 漲停 22.80)，市值達 440,000 元，超過目標權重 (38.5%) 逾 1 張 (1,000 股)，
            #     於 d3 盤後觸發 OVER_TARGET_TRIM 部分減碼 SELL 1,000 股！
            _make_bar("2330", self.d3, "20.8", "22.0", "20.8", "22.0", "20.8"),
            _make_bar("2317", self.d3, "38.0", "39.2", "38.0", "39.0", "38.0"),
            # d4: 2330 開盤執行 1,000 股部分減碼 SELL (剩 19,000 股)；同日 2317 除息 2.00 元 (payable_date = d5)
            _make_bar("2330", self.d4, "22.0", "22.2", "21.8", "22.0", "22.0"),
            _make_bar("2317", self.d4, "37.0", "37.5", "36.8", "37.0", "37.0"),
            # d5: 2317 股利 (20,000 元) 於 08:00 發放入帳；盤後訊號讓 2330 與 2317 全數出場 (score < 0)
            _make_bar("2330", self.d5, "22.0", "22.2", "21.8", "22.0", "22.0"),
            _make_bar("2317", self.d5, "37.0", "37.5", "36.8", "37.0", "37.0"),
            # d6: 開盤全數平倉出場，期末空手
            _make_bar("2330", self.d6, "22.0", "22.2", "21.8", "22.0", "22.0"),
            _make_bar("2317", self.d6, "37.0", "37.5", "36.8", "37.0", "37.0"),
        ]

        corp_proc = CorporateActionProcessor(calendar=self.calendar)
        corp_proc.register_event(
            CorporateActionEvent(
                event_id="CA-2317-DIV",
                symbol="2317",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=self.d4,
                payable_date=self.d5,
                cash_dividend_per_share=Decimal("2.00"),
                announced_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 1, 2, 18, 30, tzinfo=TAIPEI_TZ),
                provenance=self.provenance,
                is_synthetic_payable_date=True,
            )
        )

        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        batches_spec = [
            (self.d1, self.d2, [("2330", 0.95, 1), ("2317", 0.88, 2)]),
            (self.d2, self.d3, [("2330", 0.92, 1), ("2317", 0.85, 2)]),
            (self.d3, self.d4, [("2330", 0.90, 1), ("2317", 0.84, 2)]),
            (self.d4, self.d5, [("2330", 0.89, 1), ("2317", 0.83, 2)]),
            (self.d5, self.d6, [("2330", -0.50, 1), ("2317", -0.60, 2)]),
        ]
        for idx, (dec_d, tgt_d, ranks) in enumerate(batches_spec, start=1):
            feed.register_batch(
                _make_prediction_batch(
                    batch_id=f"B-E2E-{idx}",
                    decision_date=dec_d,
                    target_trade_date=tgt_d,
                    scores_and_ranks=ranks,
                ),
                available_at=dt.datetime.combine(dec_d, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

        runner = self._build_stack(
            bars=bars,
            prediction_feed=feed,
            corporate_processor=corp_proc,
            strategy_config=_make_strategy_config(
                top_n=2,
                max_position_weight="0.385",
                holding_trading_days=10,
                rebalance_drift_threshold_pct="0.02",
            ),
            run_config=WalkForwardRunConfig(
                start_date=self.d1,
                end_date=self.d6,
                universe_symbols=("2330", "2317"),
                skip_prediction_on_final_day=True,
            ),
        )
        result = runner.run()

        # 驗證 d3 盤後確實產生了 OVER_TARGET_TRIM 之部分減碼 SELL 決策（與完全平倉 CLOSE 明確區分）
        log_d3 = result.daily_logs[2]
        assert log_d3.strategy_summary is not None
        self.assertGreaterEqual(len(log_d3.strategy_summary.sell_decisions), 1)
        self.assertEqual(
            log_d3.strategy_summary.sell_decisions[0].reason,
            StrategyReason.OVER_TARGET_TRIM,
        )

        # 驗證每一天的資產恆等式：total_nav == available_cash + reserved_cash + market_value + dividends_receivable
        for d_log in result.daily_logs:
            nav_rec = d_log.nav_record
            assert nav_rec.market_value is not None and nav_rec.total_nav is not None
            self.assertEqual(
                nav_rec.total_nav,
                nav_rec.available_cash + nav_rec.reserved_cash + nav_rec.market_value + nav_rec.dividends_receivable,
            )

        # 期末 (d6) 全數平倉後：
        # 1. BacktestLotLedger 與 PortfolioLedger 完全對帳守恆 (包含累計已實現損益 100% 相等)
        runner.lot_ledger.assert_reconciled_with_portfolio_ledger(
            runner.broker.ledger,
            check_realized_pnl_when_flat=True,
        )
        # 2. 期末總資產 - 初始資金 == 累計已實現交易損益 + 累計實收現金股利
        total_net_profit = result.final_nav - result.initial_cash
        expected_net_profit = (
            runner.lot_ledger.get_realized_pnl() + runner.corporate_action_processor.total_paid_dividends
        )
        self.assertEqual(total_net_profit, expected_net_profit)
        self.assertTrue(result.is_synthetic)

    # ── 16. 防重複處理測試 ────────────────────────────────────────────

    def test_16_duplicate_guards_prediction_runner_and_settlement(self) -> None:
        bars = [
            _make_bar("2330", self.d1, "100", "101", "99", "100", "100"),
            _make_bar("2330", self.d2, "101", "103", "100", "102", "100"),
        ]
        feed = InMemoryPredictionFeed(calendar=self.calendar, provenance=self.provenance)
        b1 = _make_prediction_batch(
            batch_id="B-DUP-1",
            decision_date=self.d1,
            target_trade_date=self.d2,
            scores_and_ranks=[("2330", 0.90, 1)],
        )
        feed.register_batch(
            b1,
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
        )

        # 16a. 重複註冊同一 batch_id 或同一決策日 -> DuplicatePredictionConsumptionError
        with self.assertRaises(DuplicatePredictionConsumptionError):
            feed.register_batch(
                b1,
                available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )

        runner = self._build_stack(bars=bars, prediction_feed=feed)
        result = runner.run()

        # 16b. 重複消費已消費過之決策日預測 -> DuplicatePredictionConsumptionError
        with self.assertRaises(DuplicatePredictionConsumptionError):
            feed.consume_batch(
                self.d1,
                dt.datetime.combine(self.d1, dt.time(18, 0, 0), tzinfo=TAIPEI_TZ),
            )

        # 16c. 重複執行 runner.run() 或重複執行已執行過之交易日 -> DuplicateRunnerExecutionError
        with self.assertRaises(DuplicateRunnerExecutionError):
            runner.run()
        with self.assertRaises(DuplicateRunnerExecutionError):
            runner.run_single_day(self.d1)

        # 16d. 重複交割同一筆成交結果 -> DuplicateFillError
        log_d2 = result.daily_logs[1]
        outcome_d2 = log_d2.matched_order_outcomes[0]
        order_d1 = result.daily_logs[0].queued_orders_for_next_day[0]
        with self.assertRaises(DuplicateFillError):
            runner.simulator.settle_outcome_fill(
                outcome_d2,
                order_d1,
                portfolio_ledger=runner.broker.ledger,
                lot_ledger=runner.lot_ledger,
            )


if __name__ == "__main__":
    unittest.main()
