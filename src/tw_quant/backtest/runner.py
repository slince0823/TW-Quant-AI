"""台股歷史回測事件驅動 Walk-Forward 主迴圈模組 (runner.py).

核心原則：
1. 嚴格遵守單一交易日 T 之 12 步因果時序 (Daily Causal Timeline)：
   - Step 01 (`08:00`): 執行現金股利發放日 (`payable_date == T`) 入帳
   - Step 02 (`08:30`): 執行除權息日 (`ex_date == T`) 應收股利 (`dividends_receivable`) 登記
   - Step 03 (`09:00`): 撮合前一交易日 (T-1) 盤後排入佇列之有效委託 (`DAILY_OPEN_CONSERVATIVE`)
   - Step 04 (`09:00`): 交割成交紀錄 (`FillRecord`) 至 `PortfolioLedger` 與 `BacktestLotLedger`（單次入帳）
   - Step 05 (`13:30`): 收盤將未成交或部分成交剩餘委託轉為 `EXPIRED` 並釋放預留款券（單次釋放）
   - Step 06 (`15:00`): 盤後以 PIT 收盤價與應收股利執行每日投資組合估值 (`PortfolioValuator`)
   - Step 07 (`15:00`): 更新盤後風控基準與熔斷狀態 (`BacktestRiskContext`)
   - Step 08 (`18:00`): 自 `PredictionFeed` 消費 T 日盤後可用之 `PredictionBatch`（驗證 PIT 與單次消費）
   - Step 09 (`18:00`): 執行 `StrategyEngineAdapter` 評估，產生面向 T+1 日之策略決策與交易訊號
   - Step 10 (`18:00`): 經由 `RiskEngineAdapter` (`TradingGateway` + `RiskEngine` + `TaiwanPaperBroker`) 審核訊號並預留款券
   - Step 11 (`18:00`): 將核准之 `PaperOrder` 排入下一交易日 (T+1) 開盤待執行佇列（嚴禁於 T 日提前成交）
   - Step 12 (`18:00`): 寫入當日完整不可變審計日誌 (`DailyBacktestLog`)
2. 零前視偏差與完整防呆保護：
   - 嚴格攔截同一執行器重複執行、同一交易日重複執行、同一委託重複撮合或重複釋放預留。
   - 強制保留完整資料與模型血統 (`DataProvenance`)；若輸入含任何合成或 Mock 資料，結果自動標記 `is_synthetic=True`。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ..broker.broker import TaiwanPaperBroker
from ..broker.order import FillRecord, PaperOrder
from ..gateway import TradingGateway
from ..prediction.contract import PredictionBatch
from ..risk.engine import RiskEngine
from ..strategy.engine import StrategyEngine
from .adapters import (
    BacktestRiskContext,
    DailySignalDispatchSummary,
    RiskEngineAdapter,
    StrategyEngineAdapter,
    StrategyEvaluationSummary,
)
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .corporate_actions import (
    CorporateActionProcessor,
    DividendReceivable,
)
from .data_provider import (
    TWSE_MARKET_CLOSE_TIME,
    TWSE_MARKET_OPEN_TIME,
    DataProvenance,
    HistoricalBarRecord,
    HistoricalDataProvider,
)
from .exceptions import (
    BacktestCalendarError,
    BacktestError,
    BacktestRunnerError,
    DuplicateRunnerExecutionError,
    HistoricalDataMissingError,
    LookaheadBiasError,
    PredictionProvenanceError,
)
from .lot_ledger import BacktestLotLedger, ClosedLotTrade, PositionLot
from .prediction_feed import (
    PredictionConsumptionRecord,
    PredictionFeed,
    QuarantinedPredictionRecord,
)
from .simulator import (
    ExecutionOutcomeCode,
    HistoricalExecutionSimulator,
    InstrumentTradingStatus,
    OrderExecutionOutcome,
    PriceLimitBounds,
    PriceLimitStatus,
    SymbolExecutionContext,
)
from .valuator import DailyNAVRecord, PortfolioValuator

_ZERO = Decimal("0.00")

# 單日各因果步驟預設台北時間點
STEP_01_PAYOUT_TIME = dt.time(8, 0, 0)
STEP_02_EX_DIV_TIME = dt.time(8, 30, 0)
STEP_03_OPEN_MATCH_TIME = TWSE_MARKET_OPEN_TIME          # 09:00:00
STEP_05_ORDER_EXPIRE_TIME = TWSE_MARKET_CLOSE_TIME       # 13:30:00
STEP_06_CLOSE_VALUATION_TIME = dt.time(15, 0, 0)         # 15:00:00
STEP_08_POST_MARKET_DECISION_TIME = dt.time(18, 0, 0)    # 18:00:00


class DailyTimelineStep(str, Enum):
    """單一交易日 12 步因果時序步驟列舉（供單元與整合測試精確驗證執行順序）。"""

    STEP_01_DIVIDEND_PAYOUT_0800 = "STEP_01_DIVIDEND_PAYOUT_0800"
    STEP_02_EX_DIVIDEND_0830 = "STEP_02_EX_DIVIDEND_0830"
    STEP_03_OPEN_MATCHING_0900 = "STEP_03_OPEN_MATCHING_0900"
    STEP_04_FILL_SETTLEMENT_0900 = "STEP_04_FILL_SETTLEMENT_0900"
    STEP_05_EXPIRE_UNFILLED_1330 = "STEP_05_EXPIRE_UNFILLED_1330"
    STEP_06_CLOSE_VALUATION_1500 = "STEP_06_CLOSE_VALUATION_1500"
    STEP_07_RISK_STATE_UPDATE_1500 = "STEP_07_RISK_STATE_UPDATE_1500"
    STEP_08_CONSUME_PREDICTION_1800 = "STEP_08_CONSUME_PREDICTION_1800"
    STEP_09_STRATEGY_EVALUATION_1800 = "STEP_09_STRATEGY_EVALUATION_1800"
    STEP_10_GATEWAY_RISK_DISPATCH_1800 = "STEP_10_GATEWAY_RISK_DISPATCH_1800"
    STEP_11_QUEUE_NEXT_DAY_ORDERS_1800 = "STEP_11_QUEUE_NEXT_DAY_ORDERS_1800"
    STEP_12_DAILY_AUDIT_LOG_1800 = "STEP_12_DAILY_AUDIT_LOG_1800"


EXPECTED_DAILY_TIMELINE_SEQUENCE: Tuple[DailyTimelineStep, ...] = (
    DailyTimelineStep.STEP_01_DIVIDEND_PAYOUT_0800,
    DailyTimelineStep.STEP_02_EX_DIVIDEND_0830,
    DailyTimelineStep.STEP_03_OPEN_MATCHING_0900,
    DailyTimelineStep.STEP_04_FILL_SETTLEMENT_0900,
    DailyTimelineStep.STEP_05_EXPIRE_UNFILLED_1330,
    DailyTimelineStep.STEP_06_CLOSE_VALUATION_1500,
    DailyTimelineStep.STEP_07_RISK_STATE_UPDATE_1500,
    DailyTimelineStep.STEP_08_CONSUME_PREDICTION_1800,
    DailyTimelineStep.STEP_09_STRATEGY_EVALUATION_1800,
    DailyTimelineStep.STEP_10_GATEWAY_RISK_DISPATCH_1800,
    DailyTimelineStep.STEP_11_QUEUE_NEXT_DAY_ORDERS_1800,
    DailyTimelineStep.STEP_12_DAILY_AUDIT_LOG_1800,
)


class MissingPredictionPolicy(str, Enum):
    """當某交易日 T 盤後缺少可用 `PredictionBatch` 時之處理政策。"""

    # 嚴格失敗模式：立即拋出 PredictionBatchUnavailableError 中止回測
    FAIL_FAST = "FAIL_FAST"
    # 安全跳過策略模式：記錄明確警告、當日不產生新訊號（既有持倉續抱並正常估值與除息）
    SKIP_STRATEGY_HOLD = "SKIP_STRATEGY_HOLD"


class ErrorHandlingMode(str, Enum):
    """回測主迴圈例外處理模式。"""

    # 預設嚴格拋出：遇到任何資料缺漏、PIT 違規或定價異常立即向外拋出結構化 BacktestError
    FAIL_FAST = "FAIL_FAST"
    # 結構化中止紀錄：攔截 BacktestError，將執行狀態標記為 ABORTED_ON_ERROR 並保留已執行日誌與錯誤詳情（絕不繼續或偽造 NAV）
    ABORT_AND_RECORD = "ABORT_AND_RECORD"


class BacktestRunStatus(str, Enum):
    """回測執行結果狀態。"""

    COMPLETED = "COMPLETED"
    ABORTED_ON_ERROR = "ABORTED_ON_ERROR"


@dataclass(frozen=True)
class WalkForwardRunConfig:
    """`WalkForwardRunner` 執行配置。"""

    start_date: dt.date
    end_date: dt.date
    universe_symbols: Tuple[str, ...] = ()
    strategy_id: str = "walk_forward_default"
    decision_clock_time: dt.time = STEP_08_POST_MARKET_DECISION_TIME
    valuation_clock_time: dt.time = STEP_06_CLOSE_VALUATION_TIME
    missing_prediction_policy: MissingPredictionPolicy = MissingPredictionPolicy.FAIL_FAST
    error_handling_mode: ErrorHandlingMode = ErrorHandlingMode.FAIL_FAST
    skip_prediction_on_final_day: bool = False
    allow_synthetic_data: bool = True

    def __post_init__(self) -> None:
        s_date = to_taipei_date(self.start_date, "start_date", allow_datetime=False)
        e_date = to_taipei_date(self.end_date, "end_date", allow_datetime=False)
        object.__setattr__(self, "start_date", s_date)
        object.__setattr__(self, "end_date", e_date)
        if s_date > e_date:
            raise BacktestRunnerError(
                f"WalkForwardRunConfig.start_date ({s_date}) 不得晚於 end_date ({e_date})",
                context={"start_date": s_date.isoformat(), "end_date": e_date.isoformat()},
            )
        if not isinstance(self.missing_prediction_policy, MissingPredictionPolicy):
            raise BacktestRunnerError(f"不支援的 missing_prediction_policy: {self.missing_prediction_policy!r}")
        if not isinstance(self.error_handling_mode, ErrorHandlingMode):
            raise BacktestRunnerError(f"不支援的 error_handling_mode: {self.error_handling_mode!r}")
        if self.valuation_clock_time < TWSE_MARKET_CLOSE_TIME:
            raise LookaheadBiasError(
                f"valuation_clock_time ({self.valuation_clock_time}) 不得早於台股收盤時間 ({TWSE_MARKET_CLOSE_TIME})",
                trade_date=s_date,
            )
        if self.decision_clock_time < self.valuation_clock_time:
            raise LookaheadBiasError(
                f"decision_clock_time ({self.decision_clock_time}) 不得早於收盤估值時間 ({self.valuation_clock_time})",
                trade_date=s_date,
            )


@dataclass(frozen=True)
class DailyBacktestLog:
    """單一交易日完整 12 步執行軌跡與帳務／策略／風控審計日誌。"""

    trade_date: dt.date
    executed_steps: Tuple[DailyTimelineStep, ...]
    dividend_payouts: Tuple[DividendReceivable, ...]
    ex_dividend_entitlements: Tuple[DividendReceivable, ...]
    matched_order_outcomes: Tuple[OrderExecutionOutcome, ...]
    settled_fills: Tuple[FillRecord, ...]
    expired_orders_at_close: Tuple[PaperOrder, ...]
    nav_record: DailyNAVRecord
    post_market_risk_context: BacktestRiskContext
    prediction_consumption: Optional[PredictionConsumptionRecord]
    strategy_summary: Optional[StrategyEvaluationSummary]
    dispatch_summary: Optional[DailySignalDispatchSummary]
    queued_orders_for_next_day: Tuple[PaperOrder, ...]
    baseline_nav_at_start: Decimal
    peak_nav_after_valuation: Decimal
    daily_realized_pnl: Decimal
    daily_unrealized_pnl: Decimal
    kill_switch_active: bool
    circuit_breaker_active: bool
    warnings: Tuple[str, ...] = ()

    @property
    def valuation_snapshot(self) -> DailyNAVRecord:
        return self.nav_record


@dataclass(frozen=True)
class BacktestRunResult:
    """Walk-Forward 歷史回測主迴圈完整輸出結果（供後續 C-2.5 績效分析器與報告直接消費）。"""

    run_id: str
    strategy_id: str
    status: BacktestRunStatus
    start_date: dt.date
    end_date: dt.date
    trading_days_executed: Tuple[dt.date, ...]
    initial_cash: Decimal
    final_cash: Decimal
    final_reserved_cash: Decimal
    final_dividends_receivable: Decimal
    final_nav: Decimal
    peak_nav: Decimal
    daily_logs: Tuple[DailyBacktestLog, ...]
    nav_history: Tuple[DailyNAVRecord, ...]
    all_fills: Tuple[FillRecord, ...]
    closed_lot_trades: Tuple[ClosedLotTrade, ...]
    open_lots_at_end: Mapping[str, Tuple[PositionLot, ...]]
    pending_dividends_at_end: Tuple[DividendReceivable, ...]
    paid_dividends: Tuple[DividendReceivable, ...]
    consumed_predictions: Tuple[PredictionConsumptionRecord, ...]
    quarantined_predictions: Tuple[QuarantinedPredictionRecord, ...]
    pending_orders_at_end: Tuple[PaperOrder, ...]
    market_data_provenance: DataProvenance
    prediction_provenance: DataProvenance
    calendar_metadata_summary: Mapping[str, object]
    is_synthetic: bool
    warnings: Tuple[str, ...] = ()
    abort_error: Optional[BacktestError] = None


class WalkForwardRunner:
    """台股事件驅動 Walk-Forward 歷史回測主迴圈執行器 (`WalkForwardRunner`)."""

    def __init__(
        self,
        *,
        config: WalkForwardRunConfig,
        calendar: TradingCalendar,
        data_provider: HistoricalDataProvider,
        prediction_feed: PredictionFeed,
        strategy_engine: StrategyEngine,
        gateway: TradingGateway,
        simulator: HistoricalExecutionSimulator,
        valuator: PortfolioValuator,
        lot_ledger: BacktestLotLedger,
        corporate_action_processor: Optional[CorporateActionProcessor] = None,
        symbol_contexts_by_date: Optional[Mapping[dt.date, Mapping[str, SymbolExecutionContext]]] = None,
        default_symbol_contexts: Optional[Mapping[str, SymbolExecutionContext]] = None,
        run_id: str = "WF-RUN-0001",
    ) -> None:
        if not isinstance(config, WalkForwardRunConfig):
            raise BacktestRunnerError(f"config 必須為 WalkForwardRunConfig 實例，收到 {type(config).__name__}")
        if not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須為 TradingCalendar 實例，收到 {type(calendar).__name__}")

        self.config: WalkForwardRunConfig = config
        self.calendar: TradingCalendar = calendar
        self.data_provider: HistoricalDataProvider = data_provider
        self.prediction_feed: PredictionFeed = prediction_feed
        self.strategy_engine: StrategyEngine = strategy_engine
        self.gateway: TradingGateway = gateway
        self.broker: TaiwanPaperBroker = gateway.broker
        self.risk_engine: RiskEngine = gateway.risk_engine
        self.simulator: HistoricalExecutionSimulator = simulator
        self.valuator: PortfolioValuator = valuator
        self.lot_ledger: BacktestLotLedger = lot_ledger
        self.corporate_action_processor: CorporateActionProcessor = (
            corporate_action_processor or CorporateActionProcessor(calendar=calendar)
        )
        self.run_id: str = run_id

        # 驗證資料血統與合成資料防偽規則
        mkt_prov = self.data_provider.provenance
        pred_prov = self.prediction_feed.provenance
        self._is_synthetic: bool = bool(
            mkt_prov.is_synthetic or pred_prov.is_synthetic or self.calendar.is_synthetic
        )
        if not self.config.allow_synthetic_data and self._is_synthetic:
            raise PredictionProvenanceError(
                "WalkForwardRunConfig 設定禁止合成資料 (allow_synthetic_data=False)，"
                f"但輸入組件含合成資料 (market={mkt_prov.is_synthetic}, "
                f"prediction={pred_prov.is_synthetic}, calendar={self.calendar.is_synthetic})",
                is_synthetic=True,
            )

        self._symbol_contexts_by_date: Dict[dt.date, Dict[str, SymbolExecutionContext]] = {
            d: dict(ctx_map) for d, ctx_map in (symbol_contexts_by_date or {}).items()
        }
        self._default_symbol_contexts: Dict[str, SymbolExecutionContext] = dict(default_symbol_contexts or {})

        self.strategy_adapter = StrategyEngineAdapter(
            strategy_engine=self.strategy_engine,
            calendar=self.calendar,
        )
        self.risk_adapter = RiskEngineAdapter(
            gateway=self.gateway,
            simulator=self.simulator,
        )

        # 執行期狀態追蹤
        self._has_run: bool = False
        self._executed_dates: Set[dt.date] = set()
        self._last_executed_date: Optional[dt.date] = None
        self._queued_orders_for_next_day: List[PaperOrder] = []
        self._queued_orders_target_date: Optional[dt.date] = None
        self._daily_logs: List[DailyBacktestLog] = []

        self._initial_cash: Decimal = self.broker.ledger.initial_cash
        self._current_baseline_nav: Decimal = self.broker.ledger.initial_cash
        self._peak_nav: Decimal = self.broker.ledger.initial_cash
        self._prev_cumulative_realized_pnl: Decimal = self.broker.ledger.cumulative_realized_pnl

    @property
    def is_synthetic(self) -> bool:
        return self._is_synthetic

    @property
    def daily_logs(self) -> Tuple[DailyBacktestLog, ...]:
        return tuple(self._daily_logs)

    @property
    def queued_orders_for_next_day(self) -> Tuple[PaperOrder, ...]:
        return tuple(self._queued_orders_for_next_day)

    def _get_symbol_contexts_for_date(self, trade_date: dt.date) -> Dict[str, SymbolExecutionContext]:
        merged = dict(self._default_symbol_contexts)
        if trade_date in self._symbol_contexts_by_date:
            merged.update(self._symbol_contexts_by_date[trade_date])
        return merged

    def _collect_relevant_symbols_for_day(self, trade_date: dt.date) -> Set[str]:
        """收集當日需要查詢行情之標的集合（包含標的池、持倉標的、待撮合委託標的、當日預測候選標的）。"""
        syms: Set[str] = set(self.config.universe_symbols)
        for sym, pos in self.broker.ledger.positions.items():
            if pos.quantity > 0:
                syms.add(sym)
        for sym in self.lot_ledger.get_all_position_quantities().keys():
            syms.add(sym)
        for order in self._queued_orders_for_next_day:
            if order.is_active:
                syms.add(order.symbol)
        decision_dt = dt.datetime.combine(trade_date, self.config.decision_clock_time, tzinfo=TAIPEI_TZ)
        if self.prediction_feed.has_batch_for_decision_date(trade_date):
            entry = self.prediction_feed.peek_entry(trade_date, decision_dt)
            if entry is not None:
                for pred in entry.batch.predictions:
                    syms.add(pred.symbol)
        return syms

    def _fetch_pit_bars_for_date(
        self,
        trade_date: dt.date,
        cutoff_time: dt.datetime,
        symbols: Iterable[str],
    ) -> Dict[str, HistoricalBarRecord]:
        """在指定 `cutoff_time` 下以 PIT 安全方式取得指定日期 `trade_date` 之各標的日線行情。"""
        bars: Dict[str, HistoricalBarRecord] = {}
        for sym in sorted(set(symbols)):
            try:
                bar = self.data_provider.get_bar(
                    sym,
                    trade_date,
                    cutoff_time=cutoff_time,
                    require_pit_verified=True,
                    require_real_data=not self._is_synthetic,
                )
                bars[sym] = bar
            except HistoricalDataMissingError:
                continue
        return bars

    # ── 單日 12 步因果時序子方法（每一步驟皆可獨立審計與測試） ─────────────

    def _step_01_process_dividend_payouts(
        self,
        trade_date: dt.date,
        step_trace: List[DailyTimelineStep],
    ) -> Tuple[DividendReceivable, ...]:
        """Step 01 (`08:00`): 執行現金股利發放日 (`payable_date <= T`) 入帳。"""
        step_trace.append(DailyTimelineStep.STEP_01_DIVIDEND_PAYOUT_0800)
        payout_dt = dt.datetime.combine(trade_date, STEP_01_PAYOUT_TIME, tzinfo=TAIPEI_TZ)
        payouts = self.corporate_action_processor.process_due_payables(
            portfolio_ledger=self.broker.ledger,
            as_of_time=payout_dt,
        )
        return payouts

    def _step_02_process_ex_dividend_events(
        self,
        trade_date: dt.date,
        step_trace: List[DailyTimelineStep],
    ) -> Tuple[DividendReceivable, ...]:
        """Step 02 (`08:30`): 執行除息日 (`ex_date == T`) 應收股利登記（不提前加計至現金）。"""
        step_trace.append(DailyTimelineStep.STEP_02_EX_DIVIDEND_0830)
        ex_div_dt = dt.datetime.combine(trade_date, STEP_02_EX_DIV_TIME, tzinfo=TAIPEI_TZ)
        entitlements = self.corporate_action_processor.process_due_ex_dividends(
            self.lot_ledger,
            as_of_time=ex_div_dt,
        )
        return entitlements

    def _step_03_match_open_orders(
        self,
        trade_date: dt.date,
        orders_to_match: Sequence[PaperOrder],
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        symbol_contexts: Mapping[str, SymbolExecutionContext],
        step_trace: List[DailyTimelineStep],
    ) -> Tuple[Tuple[OrderExecutionOutcome, ...], Tuple[PaperOrder, ...]]:
        """Step 03 (`09:00`): 於 T 日開盤撮合 T-1 盤後排入佇列之有效委託（此時僅計算撮合結果與產生 FillRecord，Step 04 入帳）。"""
        step_trace.append(DailyTimelineStep.STEP_03_OPEN_MATCHING_0900)
        if not orders_to_match:
            return (), ()

        if self._queued_orders_target_date is not None and self._queued_orders_target_date != trade_date:
            raise LookaheadBiasError(
                f"待執行委託佇列之目標交易日 ({self._queued_orders_target_date}) 與當前交易日 ({trade_date}) 不一致",
                trade_date=trade_date,
            )

        open_exec_dt = dt.datetime.combine(trade_date, STEP_03_OPEN_MATCH_TIME, tzinfo=TAIPEI_TZ)
        outcomes: List[OrderExecutionOutcome] = []
        matched_orders: List[PaperOrder] = []

        for order in orders_to_match:
            if not order.is_active:
                continue
            sym_ctx = symbol_contexts.get(order.symbol)
            bar = bars_by_symbol.get(order.symbol)
            if bar is None:
                if sym_ctx is not None and sym_ctx.trading_status == InstrumentTradingStatus.HALTED:
                    rem_before = order.remaining_quantity
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=order.symbol,
                        trade_date=trade_date,
                        execution_timestamp=open_exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.UNFILLED_HALTED,
                        reason=f"標的 {order.symbol} 於 {trade_date} 標記為停牌 (HALTED) 且無當日行情，不予撮合",
                        fill_record=None,
                        price_limits=PriceLimitBounds(
                            symbol=order.symbol,
                            trade_date=trade_date,
                            status=PriceLimitStatus.NO_PRICE_LIMIT,
                            reference_price=sym_ctx.reference_price,
                            limit_up=None,
                            limit_down=None,
                            limit_rate=None,
                            instrument_category=sym_ctx.instrument_category,
                            listing_phase=sym_ctx.listing_phase,
                            rule_name="HALTED_NO_BAR",
                            reason="停牌無行情",
                        ),
                        volume_cap_basis="HALTED_ZERO_CAPACITY",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    outcomes.append(outcome)
                    matched_orders.append(order)
                    continue
                raise HistoricalDataMissingError(
                    f"交易日 {trade_date} 開盤撮合委託 {order.order_id} 時缺少標的 {order.symbol} 之日線行情資料",
                    symbol=order.symbol,
                    trade_date=trade_date,
                    missing_fields=("daily_bar",),
                )

            outcome = self.simulator.match_order(
                order,
                bar,
                symbol_context=sym_ctx,
                portfolio_ledger=self.broker.ledger,
                lot_ledger=self.lot_ledger,
                strategy_id=self.config.strategy_id,
                proposed_execution_time=open_exec_dt,
                settle_immediately=False,
            )
            outcomes.append(outcome)
            matched_orders.append(order)

        return tuple(outcomes), tuple(matched_orders)

    def _step_04_settle_fills(
        self,
        outcomes: Sequence[OrderExecutionOutcome],
        matched_orders: Sequence[PaperOrder],
        step_trace: List[DailyTimelineStep],
    ) -> Tuple[FillRecord, ...]:
        """Step 04 (`09:00`): 將 Step 03 產生之 `FillRecord` 單次交割寫入 `PortfolioLedger` 與 `BacktestLotLedger`。"""
        step_trace.append(DailyTimelineStep.STEP_04_FILL_SETTLEMENT_0900)
        settled: List[FillRecord] = []
        for outcome, order in zip(outcomes, matched_orders):
            if outcome.fill_record is not None and outcome.filled_quantity > 0:
                self.simulator.settle_outcome_fill(
                    outcome,
                    order,
                    portfolio_ledger=self.broker.ledger,
                    lot_ledger=self.lot_ledger,
                    strategy_id=self.config.strategy_id,
                )
                settled.append(outcome.fill_record)
        return tuple(settled)

    def _step_05_expire_unfilled_orders(
        self,
        trade_date: dt.date,
        orders_from_today: Sequence[PaperOrder],
        step_trace: List[DailyTimelineStep],
    ) -> Tuple[PaperOrder, ...]:
        """Step 05 (`13:30`): 收盤將當日未完全成交之剩餘有效委託轉為 `EXPIRED` 並釋放預留款券（單次釋放）。"""
        step_trace.append(DailyTimelineStep.STEP_05_EXPIRE_UNFILLED_1330)
        expired = self.simulator.expire_open_orders_at_close(
            orders_from_today,
            portfolio_ledger=self.broker.ledger,
            trade_date=trade_date,
        )
        self._queued_orders_for_next_day.clear()
        self._queued_orders_target_date = None
        return expired

    def _step_06_value_portfolio_at_close(
        self,
        trade_date: dt.date,
        valuation_dt: dt.datetime,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        symbol_contexts: Mapping[str, SymbolExecutionContext],
        step_trace: List[DailyTimelineStep],
    ) -> DailyNAVRecord:
        """Step 06 (`15:00`): 執行盤後正式投資組合估值，同步寫入 `PortfolioLedger.nav_history` 並更新歷史最高淨值 `peak_nav`。"""
        step_trace.append(DailyTimelineStep.STEP_06_CLOSE_VALUATION_1500)
        halted_syms = {
            sym for sym, bar in bars_by_symbol.items() if bar.is_halted
        } | {
            sym for sym, ctx in symbol_contexts.items() if ctx.trading_status == InstrumentTradingStatus.HALTED
        }

        nav_record = self.valuator.evaluate_daily_nav(
            valuation_date=trade_date,
            portfolio_ledger=self.broker.ledger,
            market_bars=bars_by_symbol,
            halted_symbols=halted_syms,
            lot_ledger=self.lot_ledger,
            corporate_action_processor=self.corporate_action_processor,
            valuation_timestamp=valuation_dt,
        )

        official_nav = nav_record.require_official_nav()

        if nav_record.valuation_prices:
            self.broker.ledger.mark_to_market(
                close_prices=dict(nav_record.valuation_prices),
                as_of_date=trade_date,
            )
        else:
            self.broker.ledger.mark_to_market(
                close_prices={},
                as_of_date=trade_date,
            )

        if official_nav > self._peak_nav:
            self._peak_nav = official_nav

        return nav_record

    def _step_07_update_post_market_risk_state(
        self,
        decision_dt: dt.datetime,
        nav_record: DailyNAVRecord,
        daily_realized_pnl: Decimal,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        symbol_contexts: Mapping[str, SymbolExecutionContext],
        step_trace: List[DailyTimelineStep],
    ) -> BacktestRiskContext:
        """Step 07 (`15:00`): 建立盤後 `BacktestRiskContext` 並檢查是否觸發最大回撤熔斷。"""
        step_trace.append(DailyTimelineStep.STEP_07_RISK_STATE_UPDATE_1500)
        market_prices: Dict[str, Decimal] = dict(nav_record.valuation_prices)
        for sym, bar in bars_by_symbol.items():
            if not bar.is_halted and bar.close > _ZERO:
                market_prices[sym] = bar.close

        halted_set = (
            set(nav_record.halted_symbols)
            | {sym for sym, bar in bars_by_symbol.items() if bar.is_halted}
            | {sym for sym, ctx in symbol_contexts.items() if ctx.trading_status == InstrumentTradingStatus.HALTED}
        )
        disp_set = {
            sym for sym, bar in bars_by_symbol.items() if bar.is_disposition
        } | {
            sym for sym, ctx in symbol_contexts.items() if ctx.trading_status == InstrumentTradingStatus.DISPOSITION
        }

        risk_ctx = self.risk_adapter.build_backtest_risk_context(
            decision_time=decision_dt,
            nav_record=nav_record,
            baseline_nav=self._current_baseline_nav,
            peak_nav=self._peak_nav,
            daily_realized_pnl=daily_realized_pnl,
            market_prices=market_prices,
            halted_symbols=frozenset(halted_set),
            disposition_symbols=frozenset(disp_set),
        )

        # 若當日收盤真實總資產 (含應收股利) 已跌破最大回撤門檻，立即更新熔斷旗標
        current_nav = risk_ctx.calculate_current_nav()
        if current_nav is not None and risk_ctx.peak_nav > _ZERO:
            drawdown = ((risk_ctx.peak_nav - current_nav) / risk_ctx.peak_nav).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if drawdown >= self.risk_engine.config.max_drawdown_limit:
                self.risk_engine._circuit_breaker_active = True

        return risk_ctx

    def _step_08_consume_prediction_batch(
        self,
        trade_date: dt.date,
        decision_dt: dt.datetime,
        step_trace: List[DailyTimelineStep],
        warnings_out: List[str],
    ) -> Tuple[Optional[PredictionBatch], Optional[PredictionConsumptionRecord]]:
        """Step 08 (`18:00`): 消費 T 日盤後可用之 `PredictionBatch`（執行 PIT 驗證與單次消費保護）。"""
        step_trace.append(DailyTimelineStep.STEP_08_CONSUME_PREDICTION_1800)
        is_final_day = trade_date == self.config.end_date
        if is_final_day and self.config.skip_prediction_on_final_day:
            if not self.prediction_feed.has_batch_for_decision_date(trade_date):
                warnings_out.append(
                    f"交易日 {trade_date} 為回測結束日且設定 skip_prediction_on_final_day=True，略過盤後預測消費"
                )
                return None, None

        fail_on_missing = self.config.missing_prediction_policy == MissingPredictionPolicy.FAIL_FAST
        batch, record = self.prediction_feed.consume_batch(
            trade_date,
            decision_dt,
            fail_on_missing=fail_on_missing,
        )
        if batch is None:
            warnings_out.append(
                f"MISSING_PREDICTION_BATCH: 決策日 {trade_date} 無可用預測批次，依 "
                f"{self.config.missing_prediction_policy.value} 政策不產生新交易訊號（既有部位續抱）"
            )
        elif record is not None and record.warnings:
            warnings_out.extend(record.warnings)
        return batch, record

    def _step_09_evaluate_strategy(
        self,
        decision_dt: dt.datetime,
        batch: Optional[PredictionBatch],
        nav_record: DailyNAVRecord,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        step_trace: List[DailyTimelineStep],
        warnings_out: List[str],
    ) -> Optional[StrategyEvaluationSummary]:
        """Step 09 (`18:00`): 將 `PredictionBatch` 與最新持倉快照送入 `StrategyEngineAdapter` 評估。"""
        step_trace.append(DailyTimelineStep.STEP_09_STRATEGY_EVALUATION_1800)
        if batch is None:
            return None

        summary = self.strategy_adapter.evaluate_post_market(
            batch=batch,
            decision_time=decision_dt,
            portfolio_ledger=self.broker.ledger,
            lot_ledger=self.lot_ledger,
            nav_record=nav_record,
            bars_by_symbol=bars_by_symbol,
            open_orders=self._queued_orders_for_next_day,
        )
        if not summary.accepted:
            warnings_out.append(
                f"STRATEGY_BATCH_REJECTED: 批次 {batch.batch_id} 被策略閘門拒絕 "
                f"({summary.reject_code.value}: {summary.reject_reason})"
            )
        return summary

    def _step_10_dispatch_signals_through_risk(
        self,
        strategy_summary: Optional[StrategyEvaluationSummary],
        nav_record: DailyNAVRecord,
        daily_realized_pnl: Decimal,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        symbol_contexts: Mapping[str, SymbolExecutionContext],
        step_trace: List[DailyTimelineStep],
    ) -> Optional[DailySignalDispatchSummary]:
        """Step 10 (`18:00`): 將策略產生之交易訊號送入 `RiskEngineAdapter` 審核並於 `PortfolioLedger` 預留款券。"""
        step_trace.append(DailyTimelineStep.STEP_10_GATEWAY_RISK_DISPATCH_1800)
        if strategy_summary is None or not strategy_summary.accepted:
            return None

        dispatch_summary = self.risk_adapter.dispatch_signals_for_next_day(
            strategy_summary=strategy_summary,
            nav_record=nav_record,
            baseline_nav=self._current_baseline_nav,
            peak_nav=self._peak_nav,
            daily_realized_pnl=daily_realized_pnl,
            bars_by_symbol=bars_by_symbol,
            symbol_contexts=symbol_contexts,
            lot_ledger=self.lot_ledger,
        )
        return dispatch_summary

    def _step_11_queue_orders_for_next_trading_day(
        self,
        trade_date: dt.date,
        dispatch_summary: Optional[DailySignalDispatchSummary],
        step_trace: List[DailyTimelineStep],
    ) -> Tuple[PaperOrder, ...]:
        """Step 11 (`18:00`): 將風控核准之 `PaperOrder` 排入下一交易日 (T+1) 09:00 待執行佇列。"""
        step_trace.append(DailyTimelineStep.STEP_11_QUEUE_NEXT_DAY_ORDERS_1800)
        if dispatch_summary is None or not dispatch_summary.approved_orders_queued:
            self._queued_orders_for_next_day = []
            self._queued_orders_target_date = None
            return ()

        for order in dispatch_summary.approved_orders_queued:
            order_created_date = ensure_taipei_datetime(order.created_at, "order.created_at").date()
            if order_created_date != trade_date:
                raise LookaheadBiasError(
                    f"排入下一交易日佇列之委託 {order.order_id} 建立日期 ({order_created_date}) 不等於決策日 ({trade_date})",
                    symbol=order.symbol,
                    trade_date=trade_date,
                )

        self._queued_orders_for_next_day = list(dispatch_summary.approved_orders_queued)
        self._queued_orders_target_date = dispatch_summary.target_trade_date
        return tuple(self._queued_orders_for_next_day)

    def _step_12_record_daily_log(
        self,
        *,
        trade_date: dt.date,
        step_trace: List[DailyTimelineStep],
        dividend_payouts: Tuple[DividendReceivable, ...],
        ex_dividend_entitlements: Tuple[DividendReceivable, ...],
        matched_order_outcomes: Tuple[OrderExecutionOutcome, ...],
        settled_fills: Tuple[FillRecord, ...],
        expired_orders_at_close: Tuple[PaperOrder, ...],
        nav_record: DailyNAVRecord,
        post_market_risk_context: BacktestRiskContext,
        prediction_consumption: Optional[PredictionConsumptionRecord],
        strategy_summary: Optional[StrategyEvaluationSummary],
        dispatch_summary: Optional[DailySignalDispatchSummary],
        queued_orders_for_next_day: Tuple[PaperOrder, ...],
        baseline_nav_at_start: Decimal,
        daily_realized_pnl: Decimal,
        warnings: Sequence[str],
    ) -> DailyBacktestLog:
        """Step 12 (`18:00`): 驗證當日 12 步因果時序完整性並建立不可變之 `DailyBacktestLog`。"""
        step_trace.append(DailyTimelineStep.STEP_12_DAILY_AUDIT_LOG_1800)
        executed_tuple = tuple(step_trace)
        if executed_tuple != EXPECTED_DAILY_TIMELINE_SEQUENCE:
            raise BacktestRunnerError(
                f"交易日 {trade_date} 執行步驟順序異常: 預期 {EXPECTED_DAILY_TIMELINE_SEQUENCE}，實際 {executed_tuple}",
                context={"trade_date": trade_date.isoformat()},
            )

        merged_warnings = [w.message for w in nav_record.warnings] + list(warnings)
        for out in matched_order_outcomes:
            merged_warnings.extend(out.warnings)

        log = DailyBacktestLog(
            trade_date=trade_date,
            executed_steps=executed_tuple,
            dividend_payouts=dividend_payouts,
            ex_dividend_entitlements=ex_dividend_entitlements,
            matched_order_outcomes=matched_order_outcomes,
            settled_fills=settled_fills,
            expired_orders_at_close=expired_orders_at_close,
            nav_record=nav_record,
            post_market_risk_context=post_market_risk_context,
            prediction_consumption=prediction_consumption,
            strategy_summary=strategy_summary,
            dispatch_summary=dispatch_summary,
            queued_orders_for_next_day=queued_orders_for_next_day,
            baseline_nav_at_start=baseline_nav_at_start,
            peak_nav_after_valuation=self._peak_nav,
            daily_realized_pnl=daily_realized_pnl,
            daily_unrealized_pnl=post_market_risk_context.daily_unrealized_pnl,
            kill_switch_active=self.risk_engine.kill_switch_active,
            circuit_breaker_active=self.risk_engine.circuit_breaker_active,
            warnings=tuple(merged_warnings),
        )
        self._daily_logs.append(log)
        return log

    # ── 單日執行入口與完整區間 Walk-Forward 執行入口 ────────────────────

    def run_single_day(self, trade_date: dt.date) -> DailyBacktestLog:
        """依嚴格 12 步因果時序執行單一有效交易日 `trade_date`。"""
        t_date = to_taipei_date(trade_date, "trade_date", allow_datetime=False)
        if not self.calendar.is_trading_day(t_date):
            raise BacktestCalendarError(
                f"日期 {t_date} 非有效台股交易日，WalkForwardRunner 嚴禁於非交易日執行",
                context={"trade_date": t_date.isoformat()},
            )
        if t_date in self._executed_dates:
            raise DuplicateRunnerExecutionError(
                f"交易日 {t_date} 已執行過，嚴禁重複執行同一交易日",
                context={"trade_date": t_date.isoformat()},
            )
        if self._last_executed_date is not None and t_date <= self._last_executed_date:
            raise LookaheadBiasError(
                f"回測主迴圈時序倒流: 試圖執行 {t_date}，但已執行至 {self._last_executed_date}",
                trade_date=t_date,
            )

        baseline_for_today = self._current_baseline_nav
        cum_pnl_before_today = self._prev_cumulative_realized_pnl

        step_trace: List[DailyTimelineStep] = []
        day_warnings: List[str] = []
        sym_contexts = self._get_symbol_contexts_for_date(t_date)

        # Step 01: 08:00 現金股利實收發放
        payouts = self._step_01_process_dividend_payouts(t_date, step_trace)

        # Step 02: 08:30 除權息應收股利登記
        entitlements = self._step_02_process_ex_dividend_events(t_date, step_trace)

        # 取得當日行情供 Step 03 實體開盤撮合與 Step 06 收盤估值使用
        valuation_dt = dt.datetime.combine(t_date, self.config.valuation_clock_time, tzinfo=TAIPEI_TZ)
        decision_dt = dt.datetime.combine(t_date, self.config.decision_clock_time, tzinfo=TAIPEI_TZ)
        relevant_syms = self._collect_relevant_symbols_for_day(t_date)
        bars_by_symbol = self._fetch_pit_bars_for_date(t_date, valuation_dt, relevant_syms)

        orders_for_today = list(self._queued_orders_for_next_day)

        # Step 03: 09:00 開盤撮合 T-1 盤後排入之委託
        match_outcomes, matched_orders = self._step_03_match_open_orders(
            t_date,
            orders_for_today,
            bars_by_symbol,
            sym_contexts,
            step_trace,
        )

        # Step 04: 09:00 交割成交紀錄至帳本與批次帳本
        settled_fills = self._step_04_settle_fills(match_outcomes, matched_orders, step_trace)

        # 計算當日已實現損益 (Step 04 賣單交割後之增量)
        daily_realized_pnl = self.broker.ledger.cumulative_realized_pnl - cum_pnl_before_today

        # Step 05: 13:30 收盤未成交委託到期與單次釋放預留
        expired_orders = self._step_05_expire_unfilled_orders(t_date, orders_for_today, step_trace)

        # Step 06: 15:00 盤後正式投資組合估值 (含應收股利與停牌最後已知收盤價)
        nav_record = self._step_06_value_portfolio_at_close(
            t_date,
            valuation_dt,
            bars_by_symbol,
            sym_contexts,
            step_trace,
        )

        # Step 07: 15:00 更新盤後風控狀態與最大回撤檢查
        post_risk_ctx = self._step_07_update_post_market_risk_state(
            decision_dt,
            nav_record,
            daily_realized_pnl,
            bars_by_symbol,
            sym_contexts,
            step_trace,
        )

        # Step 08: 18:00 消費當日盤後 AI 預測批次
        batch, pred_record = self._step_08_consume_prediction_batch(
            t_date,
            decision_dt,
            step_trace,
            day_warnings,
        )

        # 若該預測批次包含新候選標的尚未載入當日行情，以 decision_dt 補齊 PIT 行情
        if batch is not None:
            missing_pred_syms = [
                p.symbol for p in batch.predictions if p.symbol not in bars_by_symbol
            ]
            if missing_pred_syms:
                extra_bars = self._fetch_pit_bars_for_date(t_date, decision_dt, missing_pred_syms)
                bars_by_symbol.update(extra_bars)

        # Step 09: 18:00 策略引擎評估 (產生 BUY / SELL / CLOSE / HOLD / SKIP 決策與訊號)
        strategy_summary = self._step_09_evaluate_strategy(
            decision_dt,
            batch,
            nav_record,
            bars_by_symbol,
            step_trace,
            day_warnings,
        )

        # Step 10: 18:00 經由 TradingGateway 與 RiskEngine 審核訊號並預留資金／股數
        dispatch_summary = self._step_10_dispatch_signals_through_risk(
            strategy_summary,
            nav_record,
            daily_realized_pnl,
            bars_by_symbol,
            sym_contexts,
            step_trace,
        )

        # Step 11: 18:00 將核准之有效委託排入下一交易日 (T+1) 開盤佇列
        queued_next = self._step_11_queue_orders_for_next_trading_day(
            t_date,
            dispatch_summary,
            step_trace,
        )

        # Step 12: 18:00 記錄當日完整審計日誌
        daily_log = self._step_12_record_daily_log(
            trade_date=t_date,
            step_trace=step_trace,
            dividend_payouts=payouts,
            ex_dividend_entitlements=entitlements,
            matched_order_outcomes=match_outcomes,
            settled_fills=settled_fills,
            expired_orders_at_close=expired_orders,
            nav_record=nav_record,
            post_market_risk_context=post_risk_ctx,
            prediction_consumption=pred_record,
            strategy_summary=strategy_summary,
            dispatch_summary=dispatch_summary,
            queued_orders_for_next_day=queued_next,
            baseline_nav_at_start=baseline_for_today,
            daily_realized_pnl=daily_realized_pnl,
            warnings=day_warnings,
        )

        # 更新下一交易日之風控基準 NAV 為今日官方收盤總資產 (含應收股利)
        self._current_baseline_nav = nav_record.require_official_nav()
        self._prev_cumulative_realized_pnl = self.broker.ledger.cumulative_realized_pnl
        self._executed_dates.add(t_date)
        self._last_executed_date = t_date
        return daily_log

    def run(self) -> BacktestRunResult:
        """依交易日曆推進 `[start_date, end_date]` 所有有效交易日，回傳完整 `BacktestRunResult`。"""
        if self._has_run:
            raise DuplicateRunnerExecutionError(
                f"WalkForwardRunner ({self.run_id}) 已執行過 run()，嚴禁重複執行同一回測實例",
                context={"run_id": self.run_id},
            )
        self._has_run = True

        trading_days = self.calendar.get_trading_days_in_range(
            self.config.start_date,
            self.config.end_date,
        )
        if not trading_days:
            raise BacktestCalendarError(
                f"回測區間 [{self.config.start_date}, {self.config.end_date}] 內無任何有效台股交易日",
                context={
                    "start_date": self.config.start_date.isoformat(),
                    "end_date": self.config.end_date.isoformat(),
                },
            )

        status = BacktestRunStatus.COMPLETED
        abort_err: Optional[BacktestError] = None

        for t_date in trading_days:
            try:
                self.run_single_day(t_date)
            except BacktestError as exc:
                if self.config.error_handling_mode == ErrorHandlingMode.FAIL_FAST:
                    raise
                status = BacktestRunStatus.ABORTED_ON_ERROR
                abort_err = exc
                break

        nav_history = self.valuator.nav_history
        final_nav = (
            nav_history[-1].require_official_nav()
            if (nav_history and nav_history[-1].total_nav is not None)
            else self._initial_cash
        )
        final_div_rec = (
            nav_history[-1].dividends_receivable
            if nav_history
            else self.corporate_action_processor.get_total_dividends_receivable()
        )

        open_lots_map = {
            sym: self.lot_ledger.get_open_lots(sym)
            for sym in sorted(self.lot_ledger.get_all_position_quantities().keys())
        }

        all_warnings: List[str] = []
        for d_log in self._daily_logs:
            for w in d_log.warnings:
                all_warnings.append(f"[{d_log.trade_date.isoformat()}] {w}")

        cal_meta = self.calendar.metadata
        cal_summary: Dict[str, object] = {
            "source": cal_meta.source if cal_meta else "UNKNOWN",
            "version": cal_meta.version if cal_meta else "UNKNOWN",
            "coverage_start": self.calendar.coverage_start.isoformat() if self.calendar.coverage_start else None,
            "coverage_end": self.calendar.coverage_end.isoformat() if self.calendar.coverage_end else None,
            "is_ready": self.calendar.is_ready,
            "is_synthetic": self.calendar.is_synthetic,
        }

        return BacktestRunResult(
            run_id=self.run_id,
            strategy_id=self.config.strategy_id,
            status=status,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            trading_days_executed=tuple(d.trade_date for d in self._daily_logs),
            initial_cash=self._initial_cash,
            final_cash=self.broker.ledger.cash,
            final_reserved_cash=self.broker.ledger.reserved_cash,
            final_dividends_receivable=final_div_rec,
            final_nav=final_nav,
            peak_nav=self._peak_nav,
            daily_logs=tuple(self._daily_logs),
            nav_history=nav_history,
            all_fills=self.simulator.all_fills,
            closed_lot_trades=self.lot_ledger.get_closed_trades(),
            open_lots_at_end=open_lots_map,
            pending_dividends_at_end=self.corporate_action_processor.get_pending_receivables(),
            paid_dividends=self.corporate_action_processor.get_paid_receivables(),
            consumed_predictions=self.prediction_feed.consumed_records,
            quarantined_predictions=self.prediction_feed.quarantined_records,
            pending_orders_at_end=tuple(self._queued_orders_for_next_day),
            market_data_provenance=self.data_provider.provenance,
            prediction_provenance=self.prediction_feed.provenance,
            calendar_metadata_summary=cal_summary,
            is_synthetic=self._is_synthetic,
            warnings=tuple(all_warnings),
            abort_error=abort_err,
        )
