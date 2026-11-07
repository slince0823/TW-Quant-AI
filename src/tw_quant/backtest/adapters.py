"""台股歷史回測策略與風控橋接器模組 (adapters.py).

核心原則：
1. `StrategyEngineAdapter`：
   - 將 `BacktestLotLedger`、`PortfolioLedger`、`DailyNAVRecord` 與當前活躍委託同步轉換為
     `StrategyEngine.evaluate(batch, snapshot, decision_time)` 所需之 `PortfolioSnapshot`。
   - 嚴格核對 `PortfolioLedger` 與 `BacktestLotLedger` 之持倉股數一致性；若發生偏差立即拋出
     `BacktestAccountingError`。
   - 每筆持倉之 `entry_date` 取自 `BacktestLotLedger.get_oldest_entry_date(symbol)`（FIFO 最早未平倉批次進場日），
     結合 `TradingCalendar` 確保固定持有天數 (`holding_trading_days`) 到期出場精確無誤。
   - `snapshot.cash` 嚴格取自 `portfolio_ledger.cash`（可用現金餘額），
     **絕不將尚未發放之 `dividends_receivable`（應收股利）計入可支配買進現金**。
   - 完整保留 `StrategyDecision` 之五種動作 (`BUY`, `SELL`, `CLOSE`, `HOLD`, `SKIP`)，
     不混淆部分減碼 (`SELL`) 與全數平倉 (`CLOSE`)，亦不丟失 `HOLD` 與 `SKIP` 審計紀錄。
2. `RiskEngineAdapter`：
   - 將 `StrategyResult.signals` 依序送入既有 `TradingGateway` -> `RiskEngine` -> `TaiwanPaperBroker` 鏈路，
     並將核准通過且完成預留之 `PaperOrder` 註冊至 `HistoricalExecutionSimulator` 排入下一交易日 (T+1) 開盤撮合佇列。
   - 建立 `BacktestRiskContext(RiskContext)`，將 `dividends_receivable` 納入正式當日總資產 (`calculate_current_nav()`)
     與 `daily_unrealized_pnl` 計算，確保除息日股價自然下調不會被誤判為單日虧損超限或最大回撤熔斷，
     同時維持 `cash` 不包含應收股利。
   - 嚴格保證：
     * 被 `RiskEngine` 拒絕之訊號絕不產生有效 `PaperOrder`、絕不預留資金或股數、絕不進入撮合佇列。
     * 核准之委託僅在 `PortfolioLedger` 預留一次資金或股數，轉交 `HistoricalExecutionSimulator` 時不重複預留。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from ..broker.broker import TaiwanPaperBroker
from ..broker.ledger import PortfolioLedger
from ..broker.order import PaperOrder
from ..contracts.signal import SignalAction, UnifiedTradeSignal
from ..gateway import TradingGateway
from ..prediction.contract import PredictionBatch
from ..risk.context import RiskContext
from ..risk.decision import RiskDecision, RiskRejectCode
from ..risk.engine import RiskEngine
from ..strategy.decision import (
    BatchRejectCode,
    StrategyAction,
    StrategyDecision,
    StrategyResult,
)
from ..strategy.engine import StrategyEngine
from ..strategy.snapshot import PortfolioSnapshot, PositionSnapshot
from .calendar import TradingCalendar, ensure_taipei_datetime
from .data_provider import HistoricalBarRecord
from .exceptions import (
    BacktestAccountingError,
    BacktestRunnerError,
    LookaheadBiasError,
)
from .lot_ledger import BacktestLotLedger
from .simulator import (
    HistoricalExecutionSimulator,
    InstrumentCategory,
    InstrumentTradingStatus,
    SymbolExecutionContext,
)
from .valuator import DailyNAVRecord

_ZERO = Decimal("0.00")


@dataclass
class BacktestRiskContext(RiskContext):
    """回測專用之風控上下文 (`BacktestRiskContext`)，擴充支援除息應收股利 (`dividends_receivable`)。

    設計目的：
    - 繼承自既有 `RiskContext`，100% 相容於既有 `RiskEngine.evaluate(signal, context)`。
    - `calculate_current_nav()` 回傳 `cash + reserved_cash + market_value + dividends_receivable`，
      與 `PortfolioValuator` 之官方 `total_nav` 完全一致，杜絕除息日因現金股利尚在應收帳款而誤觸
      `DAILY_LOSS_LIMIT_EXCEEDED` 或 `CIRCUIT_BREAKER_ACTIVE`。
    - `cash` 維持僅含已實現可用現金，確保應收股利在 `payable_date` 實收前絕不可用於買進新股。
    """

    dividends_receivable: Decimal = Decimal("0.00")

    def calculate_current_nav(self) -> Optional[Decimal]:
        base_nav = super().calculate_current_nav()
        if base_nav is None:
            return None
        return base_nav + self.dividends_receivable


@dataclass(frozen=True)
class StrategyEvaluationSummary:
    """單日盤後策略評估結構化摘要（完整保留 BUY / SELL / CLOSE / HOLD / SKIP 決策）。"""

    decision_date: dt.date
    target_trade_date: dt.date
    decision_time: dt.datetime
    batch_id: str
    accepted: bool
    reject_code: Optional[BatchRejectCode]
    reject_reason: str
    portfolio_snapshot: PortfolioSnapshot
    strategy_result: StrategyResult
    decisions_by_action: Mapping[StrategyAction, Tuple[StrategyDecision, ...]]
    emitted_signals: Tuple[UnifiedTradeSignal, ...]

    @property
    def buy_decisions(self) -> Tuple[StrategyDecision, ...]:
        return self.decisions_by_action.get(StrategyAction.BUY, ())

    @property
    def sell_decisions(self) -> Tuple[StrategyDecision, ...]:
        return self.decisions_by_action.get(StrategyAction.SELL, ())

    @property
    def close_decisions(self) -> Tuple[StrategyDecision, ...]:
        return self.decisions_by_action.get(StrategyAction.CLOSE, ())

    @property
    def hold_decisions(self) -> Tuple[StrategyDecision, ...]:
        return self.decisions_by_action.get(StrategyAction.HOLD, ())

    @property
    def skip_decisions(self) -> Tuple[StrategyDecision, ...]:
        return self.decisions_by_action.get(StrategyAction.SKIP, ())


@dataclass(frozen=True)
class SignalDispatchAuditRecord:
    """單筆交易訊號經由 `TradingGateway` / `RiskEngine` / `TaiwanPaperBroker` 與模擬器佇列之審計紀錄。"""

    signal: UnifiedTradeSignal
    strategy_decision: Optional[StrategyDecision]
    risk_decision: RiskDecision
    gateway_accepted: bool
    order: Optional[PaperOrder]
    queued_for_next_day: bool
    reference_price: Optional[Decimal]
    reserved_cash_after: Decimal
    reserved_shares_after: int


@dataclass(frozen=True)
class DailySignalDispatchSummary:
    """單日所有交易訊號派發與風控審核彙總結果。"""

    decision_date: dt.date
    target_trade_date: dt.date
    decision_time: dt.datetime
    audit_records: Tuple[SignalDispatchAuditRecord, ...]
    approved_orders_queued: Tuple[PaperOrder, ...]
    rejected_signals: Tuple[UnifiedTradeSignal, ...]
    risk_rejections_by_code: Mapping[str, int]
    kill_switch_active_after: bool
    circuit_breaker_active_after: bool


class StrategyEngineAdapter:
    """策略引擎回測橋接器 (`StrategyEngineAdapter`)."""

    def __init__(
        self,
        strategy_engine: StrategyEngine,
        calendar: TradingCalendar,
    ) -> None:
        if not isinstance(strategy_engine, StrategyEngine):
            raise BacktestRunnerError(
                f"strategy_engine 必須為 StrategyEngine 實例，收到 {type(strategy_engine).__name__}"
            )
        if not isinstance(calendar, TradingCalendar):
            raise BacktestRunnerError(f"calendar 必須為 TradingCalendar 實例，收到 {type(calendar).__name__}")
        self.strategy_engine: StrategyEngine = strategy_engine
        self.calendar: TradingCalendar = calendar

    def build_portfolio_snapshot(
        self,
        *,
        decision_time: dt.datetime,
        portfolio_ledger: PortfolioLedger,
        lot_ledger: BacktestLotLedger,
        nav_record: DailyNAVRecord,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        open_orders: Sequence[PaperOrder] = (),
        extra_reference_prices: Optional[Mapping[str, Decimal]] = None,
    ) -> PortfolioSnapshot:
        """從 `PortfolioLedger`、`BacktestLotLedger` 與當日 `DailyNAVRecord` 建立 `PortfolioSnapshot`。

        嚴格不變量：
        1. `PortfolioLedger` 與 `BacktestLotLedger` 之各標的持倉股數必須完全一致。
        2. 每筆持倉之 `entry_date` 取自 `lot_ledger.get_oldest_entry_date(symbol)`（FIFO 最早批次進場日）。
        3. `cash` 僅為 `portfolio_ledger.cash`，絕不包含 `nav_record.dividends_receivable`。
        4. `prices` 包含當日 PIT 行情收盤價與持倉估值價格（停牌持倉使用 `DailyNAVRecord` 已驗證之最後已知收盤價）。
        """
        dec_dt = ensure_taipei_datetime(decision_time, "decision_time")
        trade_date = dec_dt.date()

        if nav_record.valuation_date != trade_date:
            raise LookaheadBiasError(
                f"DailyNAVRecord 估值日期 ({nav_record.valuation_date}) 與策略決策日期 ({trade_date}) 不一致",
                trade_date=trade_date,
                cutoff_time=dec_dt,
            )

        # 1. 核對 PortfolioLedger 與 BacktestLotLedger 持倉一致性並建立 PositionSnapshot
        lot_ledger.assert_reconciled_with_portfolio_ledger(
            portfolio_ledger, check_realized_pnl_when_flat=False
        )
        lot_quantities = lot_ledger.get_all_position_quantities()

        pos_snapshots: Dict[str, PositionSnapshot] = {}
        for sym, qty in sorted(lot_quantities.items()):
            if qty <= 0:
                continue
            oldest_entry_date = lot_ledger.get_oldest_entry_date(sym)
            if oldest_entry_date is None:
                raise BacktestAccountingError(
                    f"標的 {sym} 於 BacktestLotLedger 中有 {qty} 股持倉，但查無 oldest_entry_date",
                    context={"symbol": sym, "quantity": qty},
                )
            if oldest_entry_date > trade_date:
                raise LookaheadBiasError(
                    f"標的 {sym} 之持倉最早進場日 ({oldest_entry_date}) 晚於當前決策日 ({trade_date})",
                    symbol=sym,
                    trade_date=trade_date,
                    cutoff_time=dec_dt,
                )
            res_qty = portfolio_ledger.reserved_shares.get(sym, 0)
            pos_snapshots[sym] = PositionSnapshot(
                symbol=sym,
                quantity=qty,
                reserved_quantity=res_qty,
                entry_date=oldest_entry_date,
            )

        # 2. 彙整當日有效參考價格 (當日 PIT 收盤價 + 停牌持倉之 DailyNAVRecord 估值價)
        prices: Dict[str, Decimal] = {}
        if extra_reference_prices:
            for sym, px in extra_reference_prices.items():
                if isinstance(px, Decimal) and px.is_finite() and px > _ZERO:
                    prices[sym] = px

        for sym, px in nav_record.valuation_prices.items():
            if isinstance(px, Decimal) and px.is_finite() and px > _ZERO:
                prices[sym] = px

        for sym, bar in bars_by_symbol.items():
            if bar.trade_date > trade_date:
                raise LookaheadBiasError(
                    f"建立 PortfolioSnapshot 時混入未來日期行情: {sym} bar.trade_date={bar.trade_date} > {trade_date}",
                    symbol=sym,
                    trade_date=bar.trade_date,
                    cutoff_time=dec_dt,
                )
            if bar.available_at is not None and bar.available_at > dec_dt:
                raise LookaheadBiasError(
                    f"建立 PortfolioSnapshot 時混入尚未可用之行情: {sym} available_at={bar.available_at} > {dec_dt}",
                    symbol=sym,
                    trade_date=bar.trade_date,
                    available_at=bar.available_at,
                    cutoff_time=dec_dt,
                )
            if not bar.is_halted and bar.close > _ZERO:
                prices[sym] = bar.close

        # 3. 彙整未完成之待買進標的 (pending_buy_symbols)
        pending_buys: Set[str] = set()
        for order in open_orders:
            if order.is_active and order.action == SignalAction.BUY:
                pending_buys.add(order.symbol)

        # 4. 建立 PortfolioSnapshot（嚴格使用 portfolio_ledger.cash，絕不混入 dividends_receivable）
        return PortfolioSnapshot(
            as_of=dec_dt,
            cash=portfolio_ledger.cash,
            reserved_cash=portfolio_ledger.reserved_cash,
            positions=pos_snapshots,
            prices=prices,
            prices_as_of=nav_record.valuation_timestamp,
            pending_buy_symbols=frozenset(pending_buys),
        )

    def evaluate_post_market(
        self,
        *,
        batch: PredictionBatch,
        decision_time: dt.datetime,
        portfolio_ledger: PortfolioLedger,
        lot_ledger: BacktestLotLedger,
        nav_record: DailyNAVRecord,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        open_orders: Sequence[PaperOrder] = (),
        extra_reference_prices: Optional[Mapping[str, Decimal]] = None,
    ) -> StrategyEvaluationSummary:
        """於 T 日盤後執行策略評估，產生面向 T+1 交易日之 `StrategyEvaluationSummary`。"""
        dec_dt = ensure_taipei_datetime(decision_time, "decision_time")
        snapshot = self.build_portfolio_snapshot(
            decision_time=dec_dt,
            portfolio_ledger=portfolio_ledger,
            lot_ledger=lot_ledger,
            nav_record=nav_record,
            bars_by_symbol=bars_by_symbol,
            open_orders=open_orders,
            extra_reference_prices=extra_reference_prices,
        )

        result = self.strategy_engine.evaluate(
            batch=batch,
            snapshot=snapshot,
            decision_time=dec_dt,
        )

        grouped: Dict[StrategyAction, List[StrategyDecision]] = {
            StrategyAction.BUY: [],
            StrategyAction.SELL: [],
            StrategyAction.CLOSE: [],
            StrategyAction.HOLD: [],
            StrategyAction.SKIP: [],
        }
        for d in result.decisions:
            grouped[d.action].append(d)

        decisions_by_action: Dict[StrategyAction, Tuple[StrategyDecision, ...]] = {
            act: tuple(items) for act, items in grouped.items()
        }

        return StrategyEvaluationSummary(
            decision_date=dec_dt.date(),
            target_trade_date=batch.as_of_date,
            decision_time=dec_dt,
            batch_id=batch.batch_id,
            accepted=(result.batch_rejection is None),
            reject_code=result.batch_rejection,
            reject_reason=result.batch_rejection_detail,
            portfolio_snapshot=snapshot,
            strategy_result=result,
            decisions_by_action=decisions_by_action,
            emitted_signals=result.signals,
        )


class RiskEngineAdapter:
    """風控引擎與交易閘道回測橋接器 (`RiskEngineAdapter`)."""

    def __init__(
        self,
        gateway: TradingGateway,
        simulator: HistoricalExecutionSimulator,
    ) -> None:
        if not isinstance(gateway, TradingGateway):
            raise BacktestRunnerError(f"gateway 必須為 TradingGateway 實例，收到 {type(gateway).__name__}")
        if not isinstance(simulator, HistoricalExecutionSimulator):
            raise BacktestRunnerError(
                f"simulator 必須為 HistoricalExecutionSimulator 實例，收到 {type(simulator).__name__}"
            )
        self.gateway: TradingGateway = gateway
        self.broker: TaiwanPaperBroker = gateway.broker
        self.risk_engine: RiskEngine = gateway.risk_engine
        self.simulator: HistoricalExecutionSimulator = simulator

    def build_backtest_risk_context(
        self,
        *,
        decision_time: dt.datetime,
        nav_record: DailyNAVRecord,
        baseline_nav: Decimal,
        peak_nav: Decimal,
        daily_realized_pnl: Decimal,
        market_prices: Mapping[str, Decimal],
        halted_symbols: FrozenSet[str] = frozenset(),
        disposition_symbols: FrozenSet[str] = frozenset(),
    ) -> BacktestRiskContext:
        """建構反映當日真實總資產 (`total_nav` 含 `dividends_receivable`) 與預留狀態之 `BacktestRiskContext`。"""
        dec_dt = ensure_taipei_datetime(decision_time, "decision_time")
        ledger = self.broker.ledger

        holdings_dict = {sym: pos.quantity for sym, pos in ledger.positions.items() if pos.quantity > 0}
        reserved_shares_dict = {sym: qty for sym, qty in ledger.reserved_shares.items() if qty > 0}

        pending_buy_notionals: Dict[str, Decimal] = {}
        for o in self.broker.order_manager.get_open_orders():
            if o.action == SignalAction.BUY and o.reserved_cash > _ZERO:
                pending_buy_notionals[o.symbol] = (
                    pending_buy_notionals.get(o.symbol, _ZERO) + o.reserved_cash
                )

        merged_prices: Dict[str, Decimal] = {}
        for sym, pos in ledger.positions.items():
            if pos.quantity > 0 and pos.avg_price > _ZERO:
                merged_prices[sym] = pos.avg_price
        for sym, px in nav_record.valuation_prices.items():
            if isinstance(px, Decimal) and px.is_finite() and px > _ZERO:
                merged_prices[sym] = px
        for sym, px in market_prices.items():
            if isinstance(px, Decimal) and px.is_finite() and px > _ZERO:
                merged_prices[sym] = px

        dividends_rec = nav_record.dividends_receivable
        holdings_val = _ZERO
        for sym, qty in holdings_dict.items():
            px = merged_prices.get(sym, _ZERO)
            holdings_val += px * Decimal(qty)

        current_total_nav = ledger.cash + ledger.reserved_cash + holdings_val + dividends_rec
        effective_peak_nav = max(peak_nav, current_total_nav, baseline_nav)

        # 當日總損益 = 當日官方總資產變動 (current_total_nav - baseline_nav)
        # 其中 daily_unrealized_pnl = (current_total_nav - baseline_nav) - daily_realized_pnl
        # 因為 current_total_nav 包含 dividends_receivable，除息日股價下跌與應收股利增加互相抵銷，不會產生虛假虧損！
        daily_total_change = current_total_nav - baseline_nav
        daily_unrealized_pnl = daily_total_change - daily_realized_pnl

        return BacktestRiskContext(
            current_time=dec_dt,
            cash=ledger.cash,
            reserved_cash=ledger.reserved_cash,
            positions=holdings_dict,
            reserved_shares=reserved_shares_dict,
            pending_buy_notionals=pending_buy_notionals,
            market_prices=merged_prices,
            halted_symbols=set(halted_symbols),
            disposition_symbols=set(disposition_symbols),
            baseline_nav=baseline_nav,
            daily_realized_pnl=daily_realized_pnl,
            daily_unrealized_pnl=daily_unrealized_pnl,
            peak_nav=effective_peak_nav,
            dividends_receivable=dividends_rec,
        )

    def dispatch_signals_for_next_day(
        self,
        *,
        strategy_summary: StrategyEvaluationSummary,
        nav_record: DailyNAVRecord,
        baseline_nav: Decimal,
        peak_nav: Decimal,
        daily_realized_pnl: Decimal,
        bars_by_symbol: Mapping[str, HistoricalBarRecord],
        symbol_contexts: Optional[Mapping[str, SymbolExecutionContext]] = None,
        lot_ledger: Optional[BacktestLotLedger] = None,
    ) -> DailySignalDispatchSummary:
        """將策略產生之訊號送入 `TradingGateway` 與 `RiskEngine` 審核，並將核准之委託排入 T+1 開盤撮合佇列。"""
        dec_dt = strategy_summary.decision_time
        market_prices = dict(strategy_summary.portfolio_snapshot.prices)

        halted_set: Set[str] = set(nav_record.halted_symbols)
        disposition_set: Set[str] = set()
        for sym, bar in bars_by_symbol.items():
            if bar.is_halted:
                halted_set.add(sym)
            if bar.is_disposition:
                disposition_set.add(sym)
        if symbol_contexts:
            for sym, s_ctx in symbol_contexts.items():
                if s_ctx.trading_status == InstrumentTradingStatus.HALTED:
                    halted_set.add(sym)
                elif s_ctx.trading_status == InstrumentTradingStatus.DISPOSITION:
                    disposition_set.add(sym)

        halted_frozen = frozenset(halted_set)
        disposition_frozen = frozenset(disposition_set)

        decision_by_sig_id: Dict[str, StrategyDecision] = {
            d.signal_id: d for d in strategy_summary.strategy_result.decisions if d.signal_id is not None
        }

        audit_records: List[SignalDispatchAuditRecord] = []
        queued_orders: List[PaperOrder] = []
        rejected_signals: List[UnifiedTradeSignal] = []
        reject_counts: Dict[str, int] = {}

        orig_build_ctx = self.broker.build_risk_context

        def _custom_build_risk_context(
            current_time: dt.datetime,
            market_prices: Optional[Dict[str, Decimal]] = None,
            halted_symbols: Optional[Set[str]] = None,
            disposition_symbols: Optional[Set[str]] = None,
            baseline_nav: Optional[Decimal] = None,
        ) -> BacktestRiskContext:
            merged_px = dict(strategy_summary.portfolio_snapshot.prices)
            if market_prices:
                merged_px.update(market_prices)
            h_syms = frozenset(halted_symbols) if halted_symbols is not None else halted_frozen
            d_syms = frozenset(disposition_symbols) if disposition_symbols is not None else disposition_frozen
            return self.build_backtest_risk_context(
                decision_time=current_time,
                nav_record=nav_record,
                baseline_nav=baseline_nav if baseline_nav is not None else _outer_baseline_nav,
                peak_nav=peak_nav,
                daily_realized_pnl=daily_realized_pnl,
                market_prices=merged_px,
                halted_symbols=h_syms,
                disposition_symbols=d_syms,
            )

        _outer_baseline_nav = baseline_nav
        try:
            self.broker.build_risk_context = _custom_build_risk_context  # type: ignore[method-assign]

            for sig in strategy_summary.emitted_signals:
                ref_px = (
                    Decimal(str(sig.limit_price))
                    if sig.limit_price is not None
                    else market_prices.get(sig.symbol)
                )
                accepted, risk_dec, order = self.gateway.submit_signal(
                    sig,
                    ref_price=ref_px,
                    market_prices=market_prices,
                    halted_symbols=set(halted_frozen),
                    disposition_symbols=set(disposition_frozen),
                )

                queued = False
                if accepted and order is not None and order.is_active:
                    sym_ctx = symbol_contexts.get(sig.symbol) if symbol_contexts else None
                    inst_cat = (
                        sym_ctx.instrument_category
                        if sym_ctx is not None
                        else InstrumentCategory.COMMON_STOCK
                    )
                    self.simulator.submit_order(
                        order,
                        portfolio_ledger=self.broker.ledger,
                        reference_price=ref_px,
                        instrument_category=inst_cat,
                        lot_ledger=lot_ledger,
                    )
                    if order.is_active:
                        queued_orders.append(order)
                        queued = True
                    else:
                        accepted = False
                        rejected_signals.append(sig)
                else:
                    rejected_signals.append(sig)
                    if not risk_dec.allowed:
                        for code_str in risk_dec.reason_codes:
                            reject_counts[code_str] = reject_counts.get(code_str, 0) + 1

                audit_records.append(
                    SignalDispatchAuditRecord(
                        signal=sig,
                        strategy_decision=decision_by_sig_id.get(sig.signal_id),
                        risk_decision=risk_dec,
                        gateway_accepted=accepted,
                        order=order,
                        queued_for_next_day=queued,
                        reference_price=ref_px,
                        reserved_cash_after=self.broker.ledger.reserved_cash,
                        reserved_shares_after=self.broker.ledger.reserved_shares.get(sig.symbol, 0),
                    )
                )
        finally:
            self.broker.build_risk_context = orig_build_ctx  # type: ignore[method-assign]

        return DailySignalDispatchSummary(
            decision_date=strategy_summary.decision_date,
            target_trade_date=strategy_summary.target_trade_date,
            decision_time=dec_dt,
            audit_records=tuple(audit_records),
            approved_orders_queued=tuple(queued_orders),
            rejected_signals=tuple(rejected_signals),
            risk_rejections_by_code=reject_counts,
            kill_switch_active_after=self.risk_engine.kill_switch_active,
            circuit_breaker_active_after=self.risk_engine.circuit_breaker_active,
        )
