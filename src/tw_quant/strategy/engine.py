"""StrategyEngine：AI 排名選股 + 等權重配置 + 固定持有期限 (engine.py).

職責邊界：
- 只負責把 PredictionBatch + 持倉快照轉成「策略決策」與 UnifiedTradeSignal。
- 不呼叫任何券商或下單方法，不修改帳本；訊號是否允許由 RiskEngine 決定，是否成交由 TaiwanPaperBroker 決定。
- 核准 != 成交；本模組所有金額皆為「預估」。

時序規則 (v1，僅支援日頻、盤後特徵、次一交易日執行)：
- 預定進場日 = calendar.next_trading_day(資料截止時間於台北時區的日期)，且必須等於 batch.as_of_date。
  因此絕不允許以資料截止當日的價格成交；盤中截止的特徵同樣只在下一個交易日執行 (保守)。
- 訊號 created_at = 呼叫端提供的 decision_time；expires_at = 預定進場日 09:00(台北) + signal_validity。
- decision_time 必須不早於 prediction_time (batch.generated_at)；快照時間與參考價時間不得晚於 decision_time。

整批拒絕 (batch-level rejection) 時不產生任何訊號，包含出場；這是已知限制，見 STRATEGY_ENGINE_GAP_ANALYSIS.md。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Dict, List, Optional, Set, Tuple

from ..contracts.signal import (
    QuantityUnit,
    SignalAction,
    SignalOrderType,
    UnifiedTradeSignal,
)
from ..prediction.contract import PredictionBatch, StockPrediction
from .config import StrategyConfig
from .decision import (
    BatchRejectCode,
    PositionTarget,
    StrategyAction,
    StrategyDecision,
    StrategyReason,
    StrategyResult,
)
from .errors import StrategyInputError
from .snapshot import PortfolioSnapshot, PositionSnapshot, is_plain_symbol
from .trading_calendar import CalendarError, TradingCalendar

_TAIPEI = dt.timezone(dt.timedelta(hours=8))
_SESSION_OPEN = dt.time(9, 0)
_ZERO = Decimal("0")
_ONE = Decimal("1")
_CENT = Decimal("0.01")
_ACTION_ORDER = {
    StrategyAction.CLOSE: 0,
    StrategyAction.SELL: 1,
    StrategyAction.BUY: 2,
    StrategyAction.HOLD: 3,
    StrategyAction.SKIP: 4,
}


@dataclass(frozen=True)
class _Limits:
    """策略有效限制：策略自身設定與 (若提供) RiskConfig 取較嚴者。RiskEngine 仍是最終裁決者。"""

    single_cap: Decimal
    total_cap: Optional[Decimal]
    min_cash_ratio: Decimal
    order_cap: Optional[Decimal]


@dataclass
class _BuyCandidate:
    symbol: str
    pred: StockPrediction
    held: Optional[PositionSnapshot]
    reason: StrategyReason


def _q2_floor(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_FLOOR)


class StrategyEngine:
    """AI 排名選股 + 等權重 + 固定持有期限策略引擎。"""

    def __init__(
        self,
        config: StrategyConfig,
        calendar: Optional[TradingCalendar],
        risk_config: Optional[object] = None,
    ) -> None:
        if not isinstance(config, StrategyConfig):
            raise TypeError("config 必須是 StrategyConfig")
        self.config = config
        self.calendar = calendar
        self.risk_config = risk_config
        self._emitted_keys: Set[str] = set()

    # ── 公開 API ───────────────────────────────────────────────

    def evaluate(
        self,
        batch: PredictionBatch,
        snapshot: PortfolioSnapshot,
        decision_time: dt.datetime,
    ) -> StrategyResult:
        """評估一批預測並產生策略決策與交易訊號。

        相同輸入 / 設定 / 狀態 (含去重集合) 會得到相同結果。
        """
        if not isinstance(decision_time, dt.datetime) or decision_time.tzinfo is None or decision_time.tzinfo.utcoffset(decision_time) is None:
            raise StrategyInputError("decision_time 必須是具備明確時區的 datetime (timezone-aware)")
        if not isinstance(snapshot, PortfolioSnapshot):
            raise StrategyInputError("snapshot 必須是 PortfolioSnapshot")

        code, detail, planned = self._gate(batch, snapshot, decision_time)
        meta = getattr(batch, "metadata", None)
        is_mock = bool(getattr(meta, "is_mock", False))
        base = dict(
            strategy_id=self.config.strategy_id,
            batch_id=getattr(batch, "batch_id", None),
            decision_time=decision_time,
            planned_entry_date=planned,
            model_version=getattr(meta, "model_version", None),
            feature_version=getattr(meta, "feature_version", None),
            is_mock_source=is_mock,
            test_mode=is_mock and self.config.allow_mock_predictions,
        )
        if code is not None:
            warnings = []
            if snapshot.positions:
                warnings.append(
                    f"整批拒絕 ({code.value})：未對現有 {len(snapshot.positions)} 檔持倉產生任何出場訊號，需人工確認"
                )
            return StrategyResult(
                batch_rejection=code, batch_rejection_detail=detail, warnings=tuple(warnings), **base
            )

        assert planned is not None
        decisions, signals, warnings = self._plan(batch, snapshot, decision_time, planned)
        return StrategyResult(
            batch_rejection=None,
            batch_rejection_detail="",
            decisions=tuple(decisions),
            signals=tuple(signals),
            warnings=tuple(warnings),
            **base,
        )

    # ── 批次與輸入閘門 ─────────────────────────────────────────

    def _gate(
        self,
        batch: object,
        snapshot: PortfolioSnapshot,
        decision_time: dt.datetime,
    ) -> Tuple[Optional[BatchRejectCode], str, Optional[dt.date]]:
        if self.calendar is None:
            return BatchRejectCode.CALENDAR_UNAVAILABLE, "未注入交易日曆；拒絕以日曆日推算進場日，也不假設任何市場日曆", None
        if not isinstance(batch, PredictionBatch):
            return BatchRejectCode.NOT_A_PREDICTION_BATCH, f"收到 {type(batch).__name__}，需要 PredictionBatch", None

        meta = batch.metadata
        if getattr(meta, "is_mock", False) and not self.config.allow_mock_predictions:
            return (
                BatchRejectCode.MOCK_MODEL_NOT_ALLOWED,
                f"預測來自 Mock 模型 ({meta.model_type})，正式模式拒絕產生可執行訊號；僅 allow_mock_predictions=True 的測試模式可用",
                None,
            )
        if not isinstance(getattr(meta, "model_version", None), str) or not meta.model_version.strip():
            return BatchRejectCode.MODEL_VERSION_MISSING, "缺少 model_version，無法追溯", None
        if not isinstance(getattr(meta, "feature_version", None), str) or not meta.feature_version.strip():
            return BatchRejectCode.FEATURE_VERSION_MISSING, "缺少 feature_version，無法追溯", None

        try:
            batch.validate()
        except (ValueError, TypeError) as exc:
            return BatchRejectCode.BATCH_INVALID, f"PredictionBatch 驗證失敗: {exc}", None

        for pred in batch.predictions:
            if not is_plain_symbol(pred.symbol):
                return (
                    BatchRejectCode.INVALID_SYMBOL,
                    f"預測代號 {pred.symbol!r} 不是 4~6 碼不含後綴的台股代號 (策略內部不混用 .TW 後綴)",
                    None,
                )
        pred_symbols = {p.symbol for p in batch.predictions}
        overlap = pred_symbols & (set(batch.failed_symbols) | set(batch.missing_features))
        if overlap:
            return (
                BatchRejectCode.INCONSISTENT_BATCH,
                f"同一標的同時出現在 predictions 與 failed_symbols/missing_features: {sorted(overlap)}",
                None,
            )

        ranked = sorted(batch.predictions, key=lambda p: p.rank)
        for hi, lo in zip(ranked, ranked[1:]):
            if lo.score > hi.score:
                return (
                    BatchRejectCode.RANK_SCORE_INCONSISTENT,
                    f"排名與分數矛盾: rank {hi.rank} ({hi.symbol}, {hi.score}) 低於 rank {lo.rank} ({lo.symbol}, {lo.score})",
                    None,
                )

        try:
            cutoff_date = batch.data_cutoff_time.astimezone(_TAIPEI).date()
            planned = self.calendar.next_trading_day(cutoff_date)
        except CalendarError as exc:
            return BatchRejectCode.CALENDAR_UNAVAILABLE, f"交易日曆無法決定預定進場日: {exc}", None
        if batch.as_of_date != planned:
            return (
                BatchRejectCode.AS_OF_DATE_MISMATCH,
                f"as_of_date={batch.as_of_date} 與資料截止日 {cutoff_date} 之下一個交易日 {planned} 不一致；"
                "拒絕同日成交或跳日使用資料",
                planned,
            )

        if decision_time < batch.generated_at:
            return (
                BatchRejectCode.DECISION_BEFORE_PREDICTION,
                f"decision_time ({decision_time}) 早於預測產生時間 ({batch.generated_at})",
                planned,
            )
        if snapshot.as_of > decision_time:
            return BatchRejectCode.SNAPSHOT_FROM_FUTURE, "持倉快照時間晚於 decision_time", planned
        if snapshot.prices_as_of > decision_time:
            return BatchRejectCode.PRICES_FROM_FUTURE, "參考價時間晚於 decision_time", planned

        if decision_time >= self._expires_at(planned):
            return (
                BatchRejectCode.DECISION_TOO_LATE,
                f"decision_time ({decision_time}) 已超過預定進場日訊號有效期限 ({self._expires_at(planned)})",
                planned,
            )
        return None, "", planned

    def _expires_at(self, planned: dt.date) -> dt.datetime:
        return dt.datetime.combine(planned, _SESSION_OPEN, tzinfo=_TAIPEI) + self.config.signal_validity

    # ── 限制與成本 ─────────────────────────────────────────────

    def _limits(self) -> _Limits:
        cfg = self.config
        single = cfg.max_single_position_ratio
        total: Optional[Decimal] = None
        cash_ratio = cfg.min_cash_ratio
        order_cap: Optional[Decimal] = None
        rc = self.risk_config
        if rc is not None:
            single = min(single, rc.max_single_position_ratio)
            total = rc.max_total_exposure_ratio
            cash_ratio = max(cash_ratio, rc.min_cash_reserve_ratio)
            order_cap = rc.max_order_amount
        return _Limits(single, total, cash_ratio, order_cap)

    # ── 策略主體 ───────────────────────────────────────────────

    def _plan(
        self,
        batch: PredictionBatch,
        snapshot: PortfolioSnapshot,
        decision_time: dt.datetime,
        planned: dt.date,
    ) -> Tuple[List[StrategyDecision], List[UnifiedTradeSignal], List[str]]:
        cfg = self.config
        limits = self._limits()
        nav = snapshot.nav
        warnings: List[str] = []

        ranked = sorted(batch.predictions, key=lambda p: p.rank)
        preds: Dict[str, StockPrediction] = {p.symbol: p for p in ranked}
        selected = [p for p in ranked if p.score >= cfg.min_score][: cfg.max_positions]
        selected_syms = {p.symbol for p in selected}

        eff_ratio = min(cfg.target_position_ratio, limits.single_cap)
        target_value = _q2_floor(nav * eff_ratio)
        tolerance_value = nav * cfg.rebalance_tolerance_ratio

        decisions: List[StrategyDecision] = []
        buy_candidates: List[_BuyCandidate] = []
        # (decision, shares, price) —— 待簽出的訊號意圖
        intents: List[Tuple[StrategyDecision, int, Decimal]] = []

        # ① 既有持倉
        for sym in sorted(snapshot.positions):
            pos = snapshot.positions[sym]
            price = snapshot.prices[sym]
            pred = preds.get(sym)
            current_value = (price * Decimal(pos.quantity)).quantize(_CENT, rounding=ROUND_HALF_UP)

            def hold(reason: StrategyReason, detail: str) -> StrategyDecision:
                return StrategyDecision(
                    symbol=sym, action=StrategyAction.HOLD, reason=reason, detail=detail,
                    rank=None if pred is None else pred.rank, score=None if pred is None else pred.score,
                    target=self._target(sym, price, pos.quantity, current_value, eff_ratio, target_value, 0),
                )

            if pos.reserved_quantity > 0:
                decisions.append(hold(StrategyReason.PENDING_SELL_ORDER, f"已有 {pos.reserved_quantity} 股賣單預留中，不重複送出賣出訊號"))
                continue

            expiry, expiry_detail = self._expiry_state(pos, planned)
            if expiry == "UNKNOWN":
                warnings.append(f"{sym}: {expiry_detail}")

            if expiry == "EXPIRED":
                if not cfg.allow_reduce_positions:
                    decisions.append(hold(StrategyReason.REDUCE_DISABLED, "持有期限屆滿但 allow_reduce_positions=False"))
                else:
                    intents.append(self._close_intent(sym, pos, price, pred, StrategyReason.HOLDING_PERIOD_EXPIRED, expiry_detail, eff_ratio, current_value))
                continue

            if pred is None:
                why = batch.failed_symbols.get(sym) or batch.missing_features.get(sym)
                decisions.append(hold(StrategyReason.NO_VALID_PREDICTION, f"本批無有效預測 ({why or '未涵蓋'})；不因資料缺失而強制出場"))
                continue

            if sym not in selected_syms:
                if cfg.exit_when_not_selected and cfg.allow_reduce_positions:
                    intents.append(self._close_intent(sym, pos, price, pred, StrategyReason.NOT_IN_TARGET_PORTFOLIO,
                                                      f"rank={pred.rank}, score={pred.score} 不在目標組合", eff_ratio, current_value))
                elif cfg.exit_when_not_selected:
                    decisions.append(hold(StrategyReason.REDUCE_DISABLED, "不在目標組合但 allow_reduce_positions=False"))
                else:
                    decisions.append(hold(StrategyReason.HOLDING_PERIOD_NOT_REACHED if expiry == "NOT_YET" else StrategyReason.UNKNOWN_ENTRY_DATE, expiry_detail))
                continue

            diff = target_value - current_value
            if diff > tolerance_value:
                buy_candidates.append(_BuyCandidate(sym, pred, pos, StrategyReason.BELOW_TARGET_REBALANCE))
            elif diff < -tolerance_value:
                if not cfg.allow_reduce_positions:
                    decisions.append(hold(StrategyReason.REDUCE_DISABLED, "高於目標但 allow_reduce_positions=False"))
                else:
                    intent = self._trim_intent(sym, pos, price, pred, current_value, target_value, eff_ratio)
                    if isinstance(intent, StrategyDecision):
                        decisions.append(intent)
                    else:
                        intents.append(intent)
            else:
                reason = StrategyReason.AT_TARGET if diff <= _ZERO else StrategyReason.WITHIN_TOLERANCE
                decisions.append(hold(reason, f"目標 {target_value}、現值 {current_value}，差距在容忍範圍內或已達目標"))

        # ② 非持倉：預測失敗 / 缺特徵 / 分數與名次篩選
        for sym in sorted(set(batch.failed_symbols) | set(batch.missing_features)):
            if sym in snapshot.positions:
                continue
            if sym in batch.missing_features:
                decisions.append(StrategyDecision(
                    symbol=sym, action=StrategyAction.SKIP, reason=StrategyReason.MISSING_FEATURES,
                    detail=f"缺少特徵 {list(batch.missing_features[sym])}；{batch.failed_symbols.get(sym, '')}".strip("； "),
                ))
            else:
                decisions.append(StrategyDecision(
                    symbol=sym, action=StrategyAction.SKIP, reason=StrategyReason.PREDICTION_FAILED,
                    detail=batch.failed_symbols[sym],
                ))

        for p in ranked:
            if p.symbol in snapshot.positions:
                continue
            if p.score < cfg.min_score:
                decisions.append(StrategyDecision(p.symbol, StrategyAction.SKIP, StrategyReason.SCORE_BELOW_MIN,
                                                  f"score={p.score} < min_score={cfg.min_score}", p.rank, p.score))
            elif p.symbol not in selected_syms:
                decisions.append(StrategyDecision(p.symbol, StrategyAction.SKIP, StrategyReason.RANK_BEYOND_MAX_POSITIONS,
                                                  f"rank={p.rank} 超過 max_positions={cfg.max_positions}", p.rank, p.score))
            else:
                buy_candidates.append(_BuyCandidate(p.symbol, p, None, StrategyReason.SELECTED_BY_RANK))

        # ③ 買進：依名次逐檔分配資金 (不計入同批賣出所得，保守)
        required_cash_reserve = (nav * limits.min_cash_ratio).quantize(_CENT, rounding=ROUND_CEILING)
        cash_left = snapshot.cash - required_cash_reserve
        exposure_left: Optional[Decimal] = None
        if limits.total_cap is not None:
            exposure_left = nav * limits.total_cap - snapshot.market_value - snapshot.reserved_cash

        for cand in sorted(buy_candidates, key=lambda c: c.pred.rank):
            decisions_or_intent = self._buy_intent(
                cand, snapshot, planned, nav, eff_ratio, target_value, limits, cash_left, exposure_left,
            )
            if isinstance(decisions_or_intent, StrategyDecision):
                decisions.append(decisions_or_intent)
                continue
            decision, shares, price = decisions_or_intent
            tgt = decision.target
            assert tgt is not None
            cash_left -= -tgt.estimated_cash_flow
            if exposure_left is not None:
                exposure_left -= tgt.trade_notional
            intents.append((decision, shares, price))

        # ④ 簽出訊號 (含去重)
        signals: List[UnifiedTradeSignal] = []
        final: List[StrategyDecision] = list(decisions)
        mock_prefix = "[MOCK_TEST_MODE] " if batch.metadata.is_mock else ""
        for decision, shares, price in sorted(
            intents, key=lambda it: (_ACTION_ORDER[it[0].action], it[0].rank if it[0].rank is not None else 10**9, it[0].symbol)
        ):
            action = SignalAction(decision.action.value)
            key = self._dedup_key(action, decision.symbol, planned)
            if key in self._emitted_keys:
                final.append(self._suppressed(decision))
                continue
            try:
                signal = self._build_signal(decision, action, shares, price, key, decision_time, planned, batch, mock_prefix)
            except (ValueError, TypeError) as exc:
                final.append(StrategyDecision(
                    symbol=decision.symbol, action=StrategyAction.SKIP, reason=StrategyReason.SIGNAL_CONTRACT_ERROR,
                    detail=f"UnifiedTradeSignal 驗證失敗: {exc}", rank=decision.rank, score=decision.score, target=decision.target,
                ))
                continue
            self._emitted_keys.add(key)
            signals.append(signal)
            final.append(StrategyDecision(
                symbol=decision.symbol, action=decision.action, reason=decision.reason, detail=decision.detail,
                rank=decision.rank, score=decision.score, target=decision.target, signal_id=signal.signal_id,
            ))

        final.sort(key=lambda d: (_ACTION_ORDER[d.action], d.rank if d.rank is not None else 10**9, d.symbol))
        return final, signals, warnings

    # ── 持有期限 ───────────────────────────────────────────────

    def _expiry_state(self, pos: PositionSnapshot, planned: dt.date) -> Tuple[str, str]:
        """回傳 (EXPIRED | NOT_YET | UNKNOWN, 說明)。進場日未知時絕不假設已屆滿或未屆滿。"""
        if pos.entry_date is None:
            return "UNKNOWN", "進場日未知，無法判斷持有期限 (PortfolioLedger 不記錄進場日，須由呼叫端提供)"
        try:
            elapsed = self.calendar.trading_days_between(pos.entry_date, planned)  # type: ignore[union-attr]
        except CalendarError as exc:
            return "UNKNOWN", f"日曆無法計算持有天數: {exc}"
        if elapsed >= self.config.holding_trading_days:
            return "EXPIRED", f"進場日 {pos.entry_date} 至預定出場日 {planned} 已滿 {elapsed} 個交易日 (>= {self.config.holding_trading_days})"
        return "NOT_YET", f"進場日 {pos.entry_date} 至預定出場日 {planned} 僅 {elapsed} 個交易日 (< {self.config.holding_trading_days})"

    # ── 意圖建構 ───────────────────────────────────────────────

    def _target(
        self,
        symbol: str,
        price: Optional[Decimal],
        current_qty: int,
        current_value: Optional[Decimal],
        ratio: Decimal,
        target_value: Optional[Decimal],
        adjust_qty: int,
        notional: Decimal = _ZERO,
        fee: Decimal = _ZERO,
        tax: Decimal = _ZERO,
        cash_flow: Decimal = _ZERO,
    ) -> PositionTarget:
        return PositionTarget(
            symbol=symbol, reference_price=price, current_quantity=current_qty, current_value=current_value,
            target_ratio=ratio, target_value=target_value, adjust_quantity=adjust_qty,
            trade_notional=notional, estimated_fee=fee, estimated_tax=tax, estimated_cash_flow=cash_flow,
        )

    def _limit_price(self, price: Decimal) -> Decimal:
        return price.quantize(_CENT, rounding=ROUND_HALF_UP)

    def _close_intent(
        self,
        sym: str,
        pos: PositionSnapshot,
        price: Decimal,
        pred: Optional[StockPrediction],
        reason: StrategyReason,
        detail: str,
        eff_ratio: Decimal,
        current_value: Decimal,
    ) -> Tuple[StrategyDecision, int, Decimal]:
        px = self._limit_price(price)
        notional, fee, tax, net = self.config.cost_model.estimate_sell(sym, px, pos.quantity, self.config.order_type)
        target = self._target(sym, px, pos.quantity, current_value, eff_ratio, _ZERO, -pos.quantity, notional, fee, tax, net)
        decision = StrategyDecision(
            symbol=sym, action=StrategyAction.CLOSE, reason=reason, detail=detail,
            rank=None if pred is None else pred.rank, score=None if pred is None else pred.score, target=target,
        )
        return decision, pos.quantity, px

    def _trim_intent(
        self,
        sym: str,
        pos: PositionSnapshot,
        price: Decimal,
        pred: StockPrediction,
        current_value: Decimal,
        target_value: Decimal,
        eff_ratio: Decimal,
    ):
        px = self._limit_price(price)
        unit = self.config.share_unit
        raw = int(((current_value - target_value) / px).to_integral_value(rounding=ROUND_FLOOR))
        shares = (min(raw, pos.quantity - 1) // unit) * unit
        if shares <= 0:
            return StrategyDecision(
                symbol=sym, action=StrategyAction.HOLD, reason=StrategyReason.ROUNDS_TO_ZERO,
                detail=f"超額部位不足一個交易單位 ({unit} 股)，不減碼", rank=pred.rank, score=pred.score,
                target=self._target(sym, px, pos.quantity, current_value, eff_ratio, target_value, 0),
            )
        notional, fee, tax, net = self.config.cost_model.estimate_sell(sym, px, shares, self.config.order_type)
        target = self._target(sym, px, pos.quantity, current_value, eff_ratio, target_value, -shares, notional, fee, tax, net)
        decision = StrategyDecision(
            symbol=sym, action=StrategyAction.SELL, reason=StrategyReason.OVER_TARGET_TRIM,
            detail=f"現值 {current_value} 高於目標 {target_value}，減碼 {shares} 股 (部分賣出，非全部平倉)",
            rank=pred.rank, score=pred.score, target=target,
        )
        return decision, shares, px

    def _buy_intent(
        self,
        cand: _BuyCandidate,
        snapshot: PortfolioSnapshot,
        planned: dt.date,
        nav: Decimal,
        eff_ratio: Decimal,
        target_value: Decimal,
        limits: _Limits,
        cash_left: Decimal,
        exposure_left: Optional[Decimal],
    ):
        cfg = self.config
        sym, pred = cand.symbol, cand.pred
        current_qty = 0 if cand.held is None else cand.held.quantity

        def skip(reason: StrategyReason, detail: str, target: Optional[PositionTarget] = None) -> StrategyDecision:
            return StrategyDecision(sym, StrategyAction.SKIP, reason, detail, pred.rank, pred.score, target)

        if not cfg.allow_new_positions:
            return skip(StrategyReason.NEW_POSITIONS_DISABLED, "allow_new_positions=False")
        if sym in snapshot.pending_buy_symbols:
            return skip(StrategyReason.PENDING_BUY_ORDER, "已有未結買單，不重複建立進場訊號")
        price = snapshot.prices.get(sym)
        if price is None:
            return skip(StrategyReason.MISSING_PRICE, "快照缺少此標的參考價；不以 0 或任何預設值代替")
        if self._dedup_key(SignalAction.BUY, sym, planned) in self._emitted_keys:
            return skip(StrategyReason.DUPLICATE_SIGNAL_SUPPRESSED, "同策略、同標的、同預定進場日已簽出 BUY 訊號")

        px = self._limit_price(price)
        current_value = (px * Decimal(current_qty)).quantize(_CENT, rounding=ROUND_HALF_UP)
        desired_value = target_value - current_value
        if desired_value <= _ZERO:
            return skip(StrategyReason.ROUNDS_TO_ZERO, "目標市值未高於現值")

        shares, zero_reason, limited_by = self._size_buy(px, desired_value, max(cash_left, _ZERO), exposure_left, limits.order_cap)
        if shares <= 0:
            return skip(zero_reason, f"無法在限制內買進至少一個交易單位 ({cfg.share_unit} 股)；限制來源={limited_by}",
                        self._target(sym, px, current_qty, current_value, eff_ratio, target_value, 0))

        notional, fee, total = cfg.cost_model.estimate_buy(px, shares)
        target = self._target(sym, px, current_qty, current_value, eff_ratio, target_value, shares, notional, fee, _ZERO, -total)
        reduced = "" if limited_by == "value" else f"；配置受 {limited_by} 限制而低於目標"
        decision = StrategyDecision(
            symbol=sym, action=StrategyAction.BUY, reason=cand.reason,
            detail=f"目標市值 {target_value}、現值 {current_value}，買進 {shares} 股{reduced}",
            rank=pred.rank, score=pred.score, target=target,
        )
        return decision, shares, px

    def _size_buy(
        self,
        price: Decimal,
        desired_value: Decimal,
        cash_budget: Decimal,
        exposure_budget: Optional[Decimal],
        order_cap: Optional[Decimal],
    ) -> Tuple[int, StrategyReason, str]:
        """依明確規則計算買進股數：一律向下取整至 share_unit，並以含滑價與手續費的預估逐步驗證。"""
        cm, unit = self.config.cost_model, self.config.share_unit
        unit_cost = price * (_ONE + cm.slippage_rate)

        def units(value: Decimal) -> int:
            if value <= _ZERO:
                return 0
            raw = int((value / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
            return (raw // unit) * unit

        bounds: Dict[str, int] = {"value": units(desired_value), "cash": units(cash_budget)}
        if exposure_budget is not None:
            bounds["exposure"] = units(exposure_budget)
        if order_cap is not None:
            bounds["order_cap"] = units(order_cap)

        limited_by = min(sorted(bounds), key=lambda k: bounds[k])  # 同值時以名稱排序，確保確定性
        shares = bounds[limited_by]
        while shares > 0:
            notional, _fee, total = cm.estimate_buy(price, shares)
            if (
                total <= cash_budget
                and notional <= desired_value
                and (order_cap is None or total <= order_cap)
                and (exposure_budget is None or notional <= exposure_budget)
            ):
                break
            shares -= unit
            limited_by = "fee_and_rounding"
        shares = max(shares, 0)

        if shares == 0:
            if bounds["value"] == 0:
                return 0, StrategyReason.ROUNDS_TO_ZERO, "value"
            if bounds["cash"] == 0 or limited_by == "fee_and_rounding":
                return 0, StrategyReason.INSUFFICIENT_CASH, "cash"
            if bounds.get("exposure") == 0:
                return 0, StrategyReason.EXPOSURE_LIMIT, "exposure"
            return 0, StrategyReason.ROUNDS_TO_ZERO, limited_by
        return shares, StrategyReason.ROUNDS_TO_ZERO, limited_by

    # ── 訊號建構與去重 ─────────────────────────────────────────

    def _dedup_key(self, action: SignalAction, symbol: str, planned: dt.date) -> str:
        """以 SHA-256 串接固定欄位 (策略名稱與版本限定為不含分隔字元的字元集，避免欄位拼接碰撞)。"""
        parts = (self.config.strategy_name, self.config.strategy_version, action.value, symbol, planned.isoformat())
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def _suppressed(self, decision: StrategyDecision) -> StrategyDecision:
        return StrategyDecision(
            symbol=decision.symbol, action=StrategyAction.SKIP, reason=StrategyReason.DUPLICATE_SIGNAL_SUPPRESSED,
            detail=f"同策略、同標的、同預定執行日已簽出 {decision.action.value} 訊號；原決策原因 {decision.reason.value}",
            rank=decision.rank, score=decision.score, target=decision.target,
        )

    def _build_signal(
        self,
        decision: StrategyDecision,
        action: SignalAction,
        shares: int,
        price: Decimal,
        key: str,
        decision_time: dt.datetime,
        planned: dt.date,
        batch: PredictionBatch,
        mock_prefix: str,
    ) -> UnifiedTradeSignal:
        limit_price = float(price) if self.config.order_type == SignalOrderType.LIMIT else None
        rank_txt = "" if decision.rank is None else f" rank={decision.rank}"
        score_txt = "" if decision.score is None else f" score={decision.score}"
        return UnifiedTradeSignal(
            signal_id=f"STR-{key[:40]}",
            symbol=decision.symbol,
            action=action,
            quantity=int(shares),
            quantity_unit=QuantityUnit.SHARES,
            order_type=self.config.order_type,
            limit_price=limit_price,
            created_at=decision_time,
            expires_at=self._expires_at(planned),
            strategy_id=self.config.strategy_id,
            model_version=batch.metadata.model_version,
            reason=f"{mock_prefix}{decision.reason.value}:{rank_txt}{score_txt} batch={batch.batch_id}",
        )
