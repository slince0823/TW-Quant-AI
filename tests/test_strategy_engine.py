"""StrategyEngine 單元與整合測試 (tests/test_strategy_engine.py).

全部使用可控合成資料，完全離線；不連接任何市場 API、真實券商或真實憑證。
測試日曆為合成日曆 (2026-10-23 被刻意設為休市日)，不代表真實 TWSE 休市日。
測試用預測資料標示為 SYNTHETIC / MOCK，不代表任何真實 AI 模型的輸出。
"""

from __future__ import annotations

import datetime as dt
import math
import unittest
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from src.tw_quant.broker.broker import TaiwanPaperBroker
from src.tw_quant.broker.config import TaiwanPaperConfig
from src.tw_quant.broker.engine import FillEngine, MarketBar
from src.tw_quant.broker.order import PaperOrderStatus
from src.tw_quant.contracts.signal import QuantityUnit, SignalAction, SignalMode, SignalOrderType
from src.tw_quant.gateway import TradingGateway
from src.tw_quant.prediction.contract import PredictionBatch, PredictionMetadata, StockPrediction
from src.tw_quant.risk.config import RiskConfig
from src.tw_quant.risk.engine import RiskEngine
from src.tw_quant.strategy import (
    BatchRejectCode,
    CalendarError,
    CostModel,
    PortfolioSnapshot,
    PositionSnapshot,
    StaticTradingCalendar,
    StrategyAction,
    StrategyConfig,
    StrategyConfigError,
    StrategyEngine,
    StrategyInputError,
    StrategyReason,
    dispatch_signals,
)

TZ = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc
D = Decimal

CUTOFF = dt.datetime(2026, 10, 20, 13, 30, tzinfo=TZ)     # T 日收盤
GENERATED = dt.datetime(2026, 10, 20, 18, 0, tzinfo=TZ)   # prediction_time
SNAP_TIME = dt.datetime(2026, 10, 20, 18, 30, tzinfo=TZ)
PRICES_TIME = dt.datetime(2026, 10, 20, 13, 30, tzinfo=TZ)
DECISION = dt.datetime(2026, 10, 20, 19, 0, tzinfo=TZ)
AS_OF = dt.date(2026, 10, 21)                              # 預定進場日 (T+1 交易日)
SYMS = ["2330", "2317", "2454", "2308", "2412"]


def make_calendar() -> StaticTradingCalendar:
    holidays = {dt.date(2026, 10, 23)}  # 合成休市日
    days: List[dt.date] = []
    cur = dt.date(2026, 9, 1)
    while cur <= dt.date(2026, 12, 31):
        if cur.weekday() < 5 and cur not in holidays:
            days.append(cur)
        cur += dt.timedelta(days=1)
    return StaticTradingCalendar(days, dt.date(2026, 9, 1), dt.date(2026, 12, 31))


def make_config(**kw) -> StrategyConfig:
    base = dict(
        strategy_name="ai_rank_ew",
        strategy_version="1.0.0",
        max_positions=3,
        min_score=0.0,
        target_position_ratio=D("0.10"),
        holding_trading_days=3,
        cost_model=CostModel.from_paper_config(TaiwanPaperConfig()),
        share_unit=1,
    )
    base.update(kw)
    return StrategyConfig(**base)


def make_batch(
    entries: Iterable[Tuple[str, float]],
    *,
    failed: Optional[Dict[str, str]] = None,
    missing: Optional[Dict[str, List[str]]] = None,
    mock: bool = False,
    cutoff: dt.datetime = CUTOFF,
    as_of: dt.date = AS_OF,
    generated: dt.datetime = GENERATED,
    batch_id: str = "B-1",
    reverse: bool = False,
) -> PredictionBatch:
    ordered = sorted(entries, key=lambda e: (-e[1], e[0]))
    if mock:
        meta = PredictionMetadata("mock_model", "v-mock-1", "feat-1", "MOCK_FOR_TESTING", True)
    else:
        meta = PredictionMetadata("synthetic_fixture", "v-syn-1", "feat-1", "SYNTHETIC_PRODUCTION_FIXTURE", False)
    preds = [
        StockPrediction(
            symbol=s, as_of_date=as_of, data_cutoff_time=cutoff, prediction_time=generated,
            score=sc, rank=i, model_version=meta.model_version, feature_version=meta.feature_version,
        )
        for i, (s, sc) in enumerate(ordered, start=1)
    ]
    if reverse:
        preds.reverse()
    fl = dict(failed or {})
    ms = {k: list(v) for k, v in (missing or {}).items()}
    if reverse:
        fl = dict(reversed(list(fl.items())))
        ms = dict(reversed(list(ms.items())))
    return PredictionBatch(
        batch_id=batch_id, as_of_date=as_of, data_cutoff_time=cutoff, generated_at=generated,
        metadata=meta, predictions=preds, failed_symbols=fl, missing_features=ms,
    )


STANDARD = [("2330", 0.9), ("2317", 0.8), ("2454", 0.7), ("2308", 0.6), ("2412", 0.5)]


def make_snapshot(
    cash: str = "2000000.00",
    positions: Optional[Dict[str, tuple]] = None,
    prices: Optional[Dict[str, Decimal]] = None,
    pending: Iterable[str] = (),
    as_of: dt.datetime = SNAP_TIME,
    prices_as_of: dt.datetime = PRICES_TIME,
    reserved: str = "0.00",
) -> PortfolioSnapshot:
    pos = {}
    for sym, spec in (positions or {}).items():
        qty, entry = spec[0], spec[1]
        res = spec[2] if len(spec) > 2 else 0
        pos[sym] = PositionSnapshot(sym, qty, res, entry)
    px = {s: D("100.00") for s in SYMS}
    px.update(prices or {})
    return PortfolioSnapshot(
        as_of=as_of, cash=D(cash), reserved_cash=D(reserved), positions=pos, prices=px,
        prices_as_of=prices_as_of, pending_buy_symbols=frozenset(pending),
    )


def decisions_of(result):
    return result.decisions_by_symbol()


class StrategyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = make_calendar()
        self.config = make_config()
        self.engine = StrategyEngine(self.config, self.calendar)

    def evaluate(self, batch=None, snapshot=None, engine=None, decision_time=DECISION):
        return (engine or self.engine).evaluate(batch or make_batch(STANDARD), snapshot or make_snapshot(), decision_time)


class TestSelectionAndSizing(StrategyTestBase):
    # 1. 正常排序與選股
    def test_01_normal_ranking_and_selection(self) -> None:
        res = self.evaluate()
        self.assertIsNone(res.batch_rejection)
        self.assertEqual([s.symbol for s in res.signals], ["2330", "2317", "2454"])
        dec = decisions_of(res)
        for sym, rank in (("2330", 1), ("2317", 2), ("2454", 3)):
            self.assertEqual(dec[sym].action, StrategyAction.BUY)
            self.assertEqual(dec[sym].rank, rank)
            self.assertEqual(dec[sym].reason, StrategyReason.SELECTED_BY_RANK)
        for sym in ("2308", "2412"):
            self.assertEqual(dec[sym].action, StrategyAction.SKIP)

    # 2. 分數門檻篩選
    def test_02_score_threshold(self) -> None:
        engine = StrategyEngine(make_config(min_score=0.75), self.calendar)
        res = self.evaluate(engine=engine)
        self.assertEqual([s.symbol for s in res.signals], ["2330", "2317"])
        self.assertEqual(decisions_of(res)["2454"].reason, StrategyReason.SCORE_BELOW_MIN)

    # 3. 最大持股數限制
    def test_03_max_positions(self) -> None:
        engine = StrategyEngine(make_config(max_positions=2), self.calendar)
        res = self.evaluate(engine=engine)
        self.assertEqual(len(res.signals), 2)
        self.assertEqual(decisions_of(res)["2454"].reason, StrategyReason.RANK_BEYOND_MAX_POSITIONS)

    # 4. 等權重配置：與 AI score 大小無關
    def test_04_equal_weight_independent_of_score(self) -> None:
        res = self.evaluate(make_batch([("2330", 0.99), ("2317", 0.01)]))
        dec = decisions_of(res)
        self.assertEqual(dec["2330"].target.target_value, dec["2317"].target.target_value)
        self.assertEqual(dec["2330"].target.adjust_quantity, dec["2317"].target.adjust_quantity)
        self.assertEqual(dec["2330"].target.target_ratio, D("0.10"))
        self.assertEqual(dec["2330"].target.target_value, D("200000.00"))
        self.assertEqual(dec["2330"].target.adjust_quantity, 1999)

    # 5. 可用資金不足：降低配置，之後的標的因資金耗盡被略過，現金不為負
    def test_05_insufficient_cash_reduces_then_skips(self) -> None:
        snap = make_snapshot(cash="450000.00", positions={"2002": (15500, dt.date(2026, 10, 20))},
                             prices={"2002": D("100.00")})
        res = self.evaluate(snapshot=snap)
        dec = decisions_of(res)
        self.assertEqual(dec["2330"].action, StrategyAction.BUY)
        self.assertEqual(dec["2330"].target.adjust_quantity, 499)
        self.assertLess(dec["2330"].target.adjust_quantity, 1999)
        self.assertEqual(dec["2317"].action, StrategyAction.SKIP)
        self.assertEqual(dec["2317"].reason, StrategyReason.INSUFFICIENT_CASH)
        spend = sum(-d.target.estimated_cash_flow for d in res.decisions if d.action == StrategyAction.BUY)
        reserve = (snap.nav * D("0.20"))
        self.assertGreaterEqual(snap.cash - spend, reserve)
        self.assertGreaterEqual(snap.cash - spend, D("0"))

    # 6. 最低現金比例限制
    def test_06_min_cash_ratio(self) -> None:
        engine = StrategyEngine(make_config(min_cash_ratio=D("0.50")), self.calendar)
        snap = make_snapshot(cash="1100000.00", positions={"2002": (9000, dt.date(2026, 10, 20))},
                             prices={"2002": D("100.00")})
        res = self.evaluate(snapshot=snap, engine=engine)
        spend = sum(-d.target.estimated_cash_flow for d in res.decisions if d.action == StrategyAction.BUY)
        self.assertGreater(spend, D("0"))
        self.assertGreaterEqual(snap.cash - spend, snap.nav * D("0.50"))
        # 現金剛好等於保留額：不得買進
        snap2 = make_snapshot(cash="400000.00", positions={"2002": (16000, dt.date(2026, 10, 20))},
                              prices={"2002": D("100.00")})
        res2 = self.evaluate(snapshot=snap2, engine=StrategyEngine(self.config, self.calendar))
        self.assertEqual(len(res2.signals), 0)
        self.assertEqual(decisions_of(res2)["2330"].reason, StrategyReason.INSUFFICIENT_CASH)

    # 7. 單檔持倉上限 (策略自身上限與 RiskConfig 取較嚴者)
    def test_07_single_position_cap(self) -> None:
        engine = StrategyEngine(make_config(max_single_position_ratio=D("0.05"), target_position_ratio=D("0.05")), self.calendar)
        res = self.evaluate(engine=engine)
        notional = decisions_of(res)["2330"].target.trade_notional
        self.assertLessEqual(notional, D("100000.00"))
        # RiskConfig 更嚴格時以風控為準 (策略仍不是最終裁決者)
        engine2 = StrategyEngine(self.config, self.calendar, risk_config=RiskConfig(max_single_position_ratio=D("0.05")))
        res2 = self.evaluate(engine=engine2)
        self.assertLessEqual(decisions_of(res2)["2330"].target.trade_notional, D("100000.00"))
        with self.assertRaises(StrategyConfigError):
            make_config(target_position_ratio=D("0.20"), max_single_position_ratio=D("0.10"))

    # 8. 已有持倉：目標調整 (加碼 / 減碼)
    def test_08_rebalance_existing_positions(self) -> None:
        batch = make_batch([("2330", 0.9), ("2317", 0.8)])
        snap = make_snapshot(cash="1600000.00", positions={
            "2330": (1000, dt.date(2026, 10, 20)),   # 10 萬 < 目標 20 萬 -> 加碼
            "2317": (3000, dt.date(2026, 10, 20)),   # 30 萬 > 目標 20 萬 -> 減碼
        })
        res = self.evaluate(batch=batch, snapshot=snap)
        dec = decisions_of(res)
        self.assertEqual(dec["2330"].action, StrategyAction.BUY)
        self.assertEqual(dec["2330"].reason, StrategyReason.BELOW_TARGET_REBALANCE)
        self.assertEqual(dec["2330"].target.current_quantity, 1000)
        self.assertEqual(dec["2330"].target.adjust_quantity, 999)
        self.assertEqual(dec["2317"].action, StrategyAction.SELL)
        self.assertEqual(dec["2317"].reason, StrategyReason.OVER_TARGET_TRIM)
        self.assertEqual(dec["2317"].target.adjust_quantity, -1000)

    # 9. 已達目標或在容忍範圍內：不重複買進
    def test_09_no_duplicate_buy_at_target(self) -> None:
        batch = make_batch([("2330", 0.9), ("2317", 0.8)])
        snap = make_snapshot(cash="1600000.00", positions={
            "2330": (2000, dt.date(2026, 10, 20)),
            "2317": (1900, dt.date(2026, 10, 20)),
        })
        res = self.evaluate(batch=batch, snapshot=snap)
        dec = decisions_of(res)
        self.assertEqual(dec["2330"].action, StrategyAction.HOLD)
        self.assertEqual(dec["2330"].reason, StrategyReason.AT_TARGET)
        self.assertEqual(dec["2317"].action, StrategyAction.HOLD)
        self.assertEqual(dec["2317"].reason, StrategyReason.WITHIN_TOLERANCE)
        self.assertEqual(len(res.signals), 0)

    def test_09b_pending_buy_order_blocks_reentry(self) -> None:
        res = self.evaluate(snapshot=make_snapshot(pending=["2330"]))
        self.assertEqual(decisions_of(res)["2330"].reason, StrategyReason.PENDING_BUY_ORDER)
        self.assertNotIn("2330", [s.symbol for s in res.signals])

    def test_09c_policy_switches(self) -> None:
        res = self.evaluate(engine=StrategyEngine(make_config(allow_new_positions=False), self.calendar))
        self.assertEqual(len(res.signals), 0)
        self.assertEqual(decisions_of(res)["2330"].reason, StrategyReason.NEW_POSITIONS_DISABLED)
        snap = make_snapshot(cash="1800000.00", positions={"2308": (2000, dt.date(2026, 10, 15))})
        res2 = self.evaluate(snapshot=snap, engine=StrategyEngine(make_config(allow_reduce_positions=False), self.calendar))
        self.assertEqual(decisions_of(res2)["2308"].action, StrategyAction.HOLD)
        self.assertEqual(decisions_of(res2)["2308"].reason, StrategyReason.REDUCE_DISABLED)
        self.assertNotIn(SignalAction.CLOSE, [s.action for s in res2.signals])

    # 26. 取整與資金邊界
    def test_26_rounding_and_cash_boundaries(self) -> None:
        # 整張取整：1999 股 -> 1000 股；單價 3000 時不足一張 -> ROUNDS_TO_ZERO
        lot = StrategyEngine(make_config(share_unit=1000), self.calendar)
        res = self.evaluate(engine=lot)
        self.assertEqual(decisions_of(res)["2330"].target.adjust_quantity, 1000)
        res2 = self.evaluate(snapshot=make_snapshot(prices={"2330": D("3000.00")}), engine=StrategyEngine(make_config(share_unit=1000), self.calendar))
        self.assertEqual(decisions_of(res2)["2330"].reason, StrategyReason.ROUNDS_TO_ZERO)
        # 資金邊界：預算恰好等於 1000 股所需 (100,136) 時買 1000；少 0.80 元時只能買 999
        cfg = make_config(max_positions=1, target_position_ratio=D("0.80"), max_single_position_ratio=D("0.80"))
        batch = make_batch([("2330", 0.9)])
        exact = self.evaluate(batch=batch, snapshot=make_snapshot(cash="125170.00"), engine=StrategyEngine(cfg, self.calendar))
        self.assertEqual(decisions_of(exact)["2330"].target.adjust_quantity, 1000)
        self.assertEqual(-decisions_of(exact)["2330"].target.estimated_cash_flow, D("100136.00"))
        short = self.evaluate(batch=batch, snapshot=make_snapshot(cash="125169.00"), engine=StrategyEngine(cfg, self.calendar))
        self.assertEqual(decisions_of(short)["2330"].target.adjust_quantity, 999)
        for r, cash in ((exact, D("125170.00")), (short, D("125169.00"))):
            total = -decisions_of(r)["2330"].target.estimated_cash_flow
            self.assertGreaterEqual(cash - total, cash * D("0.20"))

    # 27. 交易成本估算與模擬券商一致
    def test_27_cost_model_matches_paper_broker(self) -> None:
        cfg = TaiwanPaperConfig()
        cm = CostModel.from_paper_config(cfg)
        self.assertTrue(cm.matches_paper_config(cfg))
        notional, fee, total = cm.estimate_buy(D("100.00"), 1000)
        self.assertEqual((notional, fee, total), (D("100050.00"), D("86"), D("100136.00")))
        fe = FillEngine(cfg)
        self.assertEqual(fee, fe.calculate_commission(notional))
        n, f, tax, net = cm.estimate_sell("2330", D("100.00"), 1000, SignalOrderType.LIMIT)
        self.assertEqual((n, f, tax, net), (D("100000.00"), D("86"), D("300"), D("99614.00")))
        self.assertEqual(tax, fe.calculate_tax("2330", n))
        self.assertEqual(cm.sell_tax("0050", D("100000.00")), fe.calculate_tax("0050", D("100000.00")))
        self.assertFalse(cm.matches_paper_config(TaiwanPaperConfig(commission_discount=D("0.28"))))

    def test_27b_estimate_equals_broker_reservation(self) -> None:
        broker = TaiwanPaperBroker(TaiwanPaperConfig(initial_cash=D("2000000.00")))
        risk = RiskEngine(RiskConfig())
        gateway = TradingGateway(broker, risk)
        engine = StrategyEngine(self.config, self.calendar, risk_config=risk.config)
        snap = PortfolioSnapshot.from_broker(broker, SNAP_TIME, {s: D("100.00") for s in SYMS}, PRICES_TIME, {})
        res = engine.evaluate(make_batch(STANDARD), snap, DECISION)
        records = dispatch_signals(res, gateway, snap, cost_model=self.config.cost_model)
        self.assertEqual(len(records), 3)
        for rec in records:
            self.assertTrue(rec.risk_allowed, rec)
            self.assertEqual(rec.order_status, "ACCEPTED")   # 核准 != 成交
            order = broker.order_manager.get_order(rec.order_id)
            est = decisions_of(res)[rec.symbol].target
            self.assertEqual(order.reserved_cash, -est.estimated_cash_flow)
            self.assertEqual(order.filled_quantity, 0)
        self.assertEqual(broker.get_cash() + broker.get_reserved_cash(), D("2000000.00"))

    def test_27c_cost_mismatch_is_disclosed(self) -> None:
        broker = TaiwanPaperBroker(TaiwanPaperConfig(initial_cash=D("2000000.00"), commission_discount=D("0.28")))
        gateway = TradingGateway(broker, RiskEngine(RiskConfig()))
        snap = PortfolioSnapshot.from_broker(broker, SNAP_TIME, {s: D("100.00") for s in SYMS}, PRICES_TIME, {})
        res = StrategyEngine(self.config, self.calendar).evaluate(make_batch(STANDARD), snap, DECISION)
        records = dispatch_signals(res, gateway, snap, cost_model=self.config.cost_model)
        self.assertTrue(records)
        self.assertTrue(all("COST_MODEL_DIFFERS" in r.note for r in records))


class TestHoldingAndExits(StrategyTestBase):
    # 10. 固定持有期限到期 (邊界：預定出場日恰滿 N 個交易日即到期)
    def test_10_holding_period_expiry_boundaries(self) -> None:
        batch = make_batch([("2317", 0.9)])
        cases = [
            (dt.date(2026, 10, 15), True),    # 16,19,20,21 -> 4
            (dt.date(2026, 10, 16), True),    # 19,20,21 -> 3 (恰好)
            (dt.date(2026, 10, 19), False),   # 20,21 -> 2
        ]
        for entry, expired in cases:
            with self.subTest(entry=entry):
                engine = StrategyEngine(self.config, self.calendar)
                res = self.evaluate(batch=batch, snapshot=make_snapshot(cash="1800000.00", positions={"2330": (2000, entry)}), engine=engine)
                d = decisions_of(res)["2330"]
                if expired:
                    self.assertEqual(d.action, StrategyAction.CLOSE)
                    self.assertEqual(d.reason, StrategyReason.HOLDING_PERIOD_EXPIRED)
                else:
                    self.assertNotEqual(d.action, StrategyAction.CLOSE)

    def test_10b_not_selected_exit_and_hold_when_disabled(self) -> None:
        batch = make_batch([("2317", 0.9), ("2454", 0.8), ("2308", 0.7), ("2330", 0.6)])  # 2330 排第 4，不在前 3
        snap = make_snapshot(cash="1800000.00", positions={"2330": (2000, dt.date(2026, 10, 19))})
        res = self.evaluate(batch=batch, snapshot=snap)
        self.assertEqual(decisions_of(res)["2330"].reason, StrategyReason.NOT_IN_TARGET_PORTFOLIO)
        self.assertEqual(decisions_of(res)["2330"].action, StrategyAction.CLOSE)
        keep = StrategyEngine(make_config(exit_when_not_selected=False), self.calendar)
        res2 = self.evaluate(batch=batch, snapshot=snap, engine=keep)
        self.assertEqual(decisions_of(res2)["2330"].action, StrategyAction.HOLD)
        self.assertEqual(decisions_of(res2)["2330"].reason, StrategyReason.HOLDING_PERIOD_NOT_REACHED)

    def test_10c_unknown_entry_date_is_not_assumed(self) -> None:
        snap = make_snapshot(cash="1800000.00", positions={"2330": (2000, None)})
        res = self.evaluate(batch=make_batch([("2330", 0.9)]), snapshot=snap)
        self.assertNotIn(SignalAction.CLOSE, [s.action for s in res.signals])
        self.assertTrue(any("進場日未知" in w for w in res.warnings))

    def test_10d_failed_prediction_on_held_symbol_does_not_force_exit(self) -> None:
        batch = make_batch([("2317", 0.9)], failed={"2330": "推論失敗"})
        snap = make_snapshot(cash="1800000.00", positions={"2330": (2000, dt.date(2026, 10, 19))})
        d = decisions_of(self.evaluate(batch=batch, snapshot=snap))["2330"]
        self.assertEqual(d.action, StrategyAction.HOLD)
        self.assertEqual(d.reason, StrategyReason.NO_VALID_PREDICTION)

    def test_10e_pending_sell_blocks_new_exit(self) -> None:
        snap = make_snapshot(cash="1800000.00", positions={"2330": (2000, dt.date(2026, 10, 15), 2000)})
        d = decisions_of(self.evaluate(batch=make_batch([("2317", 0.9)]), snapshot=snap))["2330"]
        self.assertEqual(d.reason, StrategyReason.PENDING_SELL_ORDER)

    # 25. SELL 與 CLOSE 的持倉語意；HOLD / SKIP 不會變成訊號
    def test_25_sell_vs_close_semantics(self) -> None:
        batch = make_batch([("2317", 0.9)])
        snap = make_snapshot(cash="1450000.00", positions={
            "2330": (2500, dt.date(2026, 10, 15)),   # 到期 -> CLOSE 全部
            "2317": (3000, dt.date(2026, 10, 20)),   # 超額 -> SELL 部分
        })
        res = self.evaluate(batch=batch, snapshot=snap)
        sigs = {s.symbol: s for s in res.signals}
        self.assertEqual(sigs["2330"].action, SignalAction.CLOSE)
        self.assertEqual(sigs["2330"].quantity, 2500)             # CLOSE = 全部持倉
        self.assertEqual(sigs["2317"].action, SignalAction.SELL)
        self.assertEqual(sigs["2317"].quantity, 1000)
        self.assertLess(sigs["2317"].quantity, 3000)              # SELL = 部分減碼
        self.assertEqual({s.quantity_unit for s in res.signals}, {QuantityUnit.SHARES})
        self.assertEqual({s.mode for s in res.signals}, {SignalMode.PAPER})
        self.assertNotIn(StrategyAction.HOLD.value, {a.value for a in SignalAction})
        self.assertNotIn(StrategyAction.SKIP.value, {a.value for a in SignalAction})
        # 每個訊號都對應 BUY/SELL/CLOSE 決策，HOLD/SKIP 決策沒有 signal_id
        for d in res.decisions:
            if d.action in (StrategyAction.HOLD, StrategyAction.SKIP):
                self.assertIsNone(d.signal_id)
            else:
                self.assertIsNotNone(d.signal_id)
        # 出場先於進場
        actions = [s.action for s in res.signals]
        self.assertLess(actions.index(SignalAction.CLOSE), actions.index(SignalAction.SELL))


class TestInputValidation(StrategyTestBase):
    # 11. 無持倉資訊時拒絕錯誤假設
    def test_11_missing_positions_rejected(self) -> None:
        with self.assertRaises(StrategyInputError):
            PortfolioSnapshot(as_of=SNAP_TIME, cash=D("2000000"), reserved_cash=D("0"), positions=None,  # type: ignore[arg-type]
                              prices={}, prices_as_of=PRICES_TIME, pending_buy_symbols=frozenset())
        with self.assertRaises(StrategyInputError):
            PortfolioSnapshot(as_of=SNAP_TIME, cash=D("2000000"), reserved_cash=D("0"), positions={},
                              prices={}, prices_as_of=PRICES_TIME, pending_buy_symbols=None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            PortfolioSnapshot(as_of=SNAP_TIME, cash=D("2000000"))  # type: ignore[call-arg]  # 無預設持倉
        with self.assertRaises(StrategyInputError):
            self.engine.evaluate(make_batch(STANDARD), {"cash": 1}, DECISION)  # type: ignore[arg-type]
        # 明確傳入空 dict 才代表「確認無持倉」
        self.assertEqual(make_snapshot().nav, D("2000000.00"))

    # 12. 缺少必要價格時拒絕交易
    def test_12_missing_price_rejects_trade(self) -> None:
        snap = make_snapshot(prices={})
        object.__setattr__(snap, "prices", {k: v for k, v in snap.prices.items() if k != "2330"})
        res = self.evaluate(snapshot=snap)
        self.assertEqual(decisions_of(res)["2330"].reason, StrategyReason.MISSING_PRICE)
        self.assertNotIn("2330", [s.symbol for s in res.signals])

    def test_12b_invalid_prices_rejected_at_snapshot(self) -> None:
        for bad in (100.0, D("0"), D("-1"), D("NaN"), D("Infinity"), None):
            with self.subTest(bad=bad):
                with self.assertRaises(StrategyInputError):
                    make_snapshot(prices={"2330": bad})  # type: ignore[dict-item]
        with self.assertRaises(StrategyInputError):  # 持倉缺價 -> 無法算 NAV，不以 0 / 均價代替
            PortfolioSnapshot(as_of=SNAP_TIME, cash=D("1"), reserved_cash=D("0"),
                              positions={"2330": PositionSnapshot("2330", 100, 0, None)}, prices={},
                              prices_as_of=PRICES_TIME, pending_buy_symbols=frozenset())

    # 13. failed_symbols
    def test_13_failed_symbols(self) -> None:
        res = self.evaluate(make_batch(STANDARD[:3], failed={"2412": "模型推論例外"}))
        d = decisions_of(res)["2412"]
        self.assertEqual((d.action, d.reason), (StrategyAction.SKIP, StrategyReason.PREDICTION_FAILED))
        self.assertIn("模型推論例外", d.detail)
        self.assertNotIn("2412", [s.symbol for s in res.signals])

    # 14. missing_features
    def test_14_missing_features(self) -> None:
        res = self.evaluate(make_batch(STANDARD[:3], failed={"2412": "特徵不完整"}, missing={"2412": ["pe_ratio_inv"]}))
        d = decisions_of(res)["2412"]
        self.assertEqual(d.reason, StrategyReason.MISSING_FEATURES)
        self.assertIn("pe_ratio_inv", d.detail)
        self.assertNotIn("2412", [s.symbol for s in res.signals])

    # 15. 重複股票
    def test_15_duplicate_symbols_rejected(self) -> None:
        batch = make_batch(STANDARD[:3])
        dup = StockPrediction("2330", AS_OF, CUTOFF, GENERATED, 0.1, 4, batch.metadata.model_version, batch.metadata.feature_version)
        batch.predictions.append(dup)
        res = self.evaluate(batch)
        self.assertEqual(res.batch_rejection, BatchRejectCode.BATCH_INVALID)
        self.assertEqual(len(res.signals), 0)

    def test_15b_overlap_between_predictions_and_failures_rejected(self) -> None:
        res = self.evaluate(make_batch(STANDARD[:3], failed={"2330": "矛盾"}))
        self.assertEqual(res.batch_rejection, BatchRejectCode.INCONSISTENT_BATCH)

    def test_15c_suffix_symbol_rejected(self) -> None:
        batch = make_batch(STANDARD[:3])
        object.__setattr__(batch.predictions[0], "symbol", "2330.TW")
        self.assertEqual(self.evaluate(batch).batch_rejection, BatchRejectCode.INVALID_SYMBOL)

    # 16. NaN / Inf / 非法分數與排名
    def test_16_illegal_scores_and_ranks(self) -> None:
        for field, bad in (("score", float("nan")), ("score", float("inf")), ("score", -math.inf), ("rank", 0)):
            with self.subTest(field=field, bad=bad):
                batch = make_batch(STANDARD[:3])
                object.__setattr__(batch.predictions[0], field, bad)
                res = self.evaluate(batch)
                self.assertEqual(res.batch_rejection, BatchRejectCode.BATCH_INVALID)
                self.assertEqual(len(res.signals), 0)
        with self.assertRaises(StrategyConfigError):
            make_config(min_score=float("nan"))

    def test_16b_rank_score_inconsistency_rejected(self) -> None:
        batch = make_batch(STANDARD[:3])
        object.__setattr__(batch.predictions[2], "score", 5.0)   # rank 3 分數高於 rank 1
        self.assertEqual(self.evaluate(batch).batch_rejection, BatchRejectCode.RANK_SCORE_INCONSISTENT)

    def test_16c_missing_versions_rejected(self) -> None:
        for field, code in (("model_version", BatchRejectCode.MODEL_VERSION_MISSING),
                            ("feature_version", BatchRejectCode.FEATURE_VERSION_MISSING)):
            with self.subTest(field=field):
                batch = make_batch(STANDARD[:3])
                object.__setattr__(batch.metadata, field, "")
                self.assertEqual(self.evaluate(batch).batch_rejection, code)

    def test_16d_not_a_batch(self) -> None:
        res = self.engine.evaluate([("2330", 0.9)], make_snapshot(), DECISION)  # type: ignore[arg-type]
        self.assertEqual(res.batch_rejection, BatchRejectCode.NOT_A_PREDICTION_BATCH)


class TestMockIsolation(StrategyTestBase):
    # 17. 正式模式拒絕 Mock
    def test_17_mock_rejected_in_production_mode(self) -> None:
        res = self.evaluate(make_batch(STANDARD, mock=True))
        self.assertEqual(res.batch_rejection, BatchRejectCode.MOCK_MODEL_NOT_ALLOWED)
        self.assertTrue(res.is_mock_source)
        self.assertFalse(res.test_mode)
        self.assertEqual(len(res.signals), 0)

    # 18. 明確測試模式可用，且全程標記
    def test_18_mock_allowed_only_in_explicit_test_mode(self) -> None:
        engine = StrategyEngine(make_config(allow_mock_predictions=True), self.calendar)
        res = self.evaluate(make_batch(STANDARD, mock=True), engine=engine)
        self.assertIsNone(res.batch_rejection)
        self.assertTrue(res.test_mode)
        self.assertTrue(res.is_mock_source)
        self.assertTrue(res.signals)
        for s in res.signals:
            self.assertTrue(s.reason.startswith("[MOCK_TEST_MODE]"))
        self.assertTrue(res.to_dict()["test_mode"])
        # 非 Mock 批次即使開啟測試模式也不會被標成 test_mode
        res2 = self.evaluate(make_batch(STANDARD), engine=StrategyEngine(make_config(allow_mock_predictions=True), self.calendar))
        self.assertFalse(res2.test_mode)
        self.assertFalse(res2.signals[0].reason.startswith("[MOCK_TEST_MODE]"))

    def test_18b_test_mode_still_enforces_time_and_validation(self) -> None:
        engine = StrategyEngine(make_config(allow_mock_predictions=True), self.calendar)
        bad_date = make_batch(STANDARD, mock=True, as_of=dt.date(2026, 10, 20))
        self.assertEqual(self.evaluate(bad_date, engine=engine).batch_rejection, BatchRejectCode.AS_OF_DATE_MISMATCH)
        broken = make_batch(STANDARD, mock=True)
        object.__setattr__(broken.predictions[0], "score", float("nan"))
        self.assertEqual(self.evaluate(broken, engine=engine).batch_rejection, BatchRejectCode.BATCH_INVALID)


class TestTimeAndCalendar(StrategyTestBase):
    # 19. 時區
    def test_19_timezone_handling(self) -> None:
        with self.assertRaises(StrategyInputError):
            self.engine.evaluate(make_batch(STANDARD), make_snapshot(), dt.datetime(2026, 10, 20, 19, 0))
        with self.assertRaises(StrategyInputError):
            make_snapshot(as_of=dt.datetime(2026, 10, 20, 18, 30))
        with self.assertRaises(StrategyInputError):
            make_snapshot(prices_as_of=dt.datetime(2026, 10, 20, 13, 30))
        # UTC 表示的同一瞬間 (05:30Z = 13:30 台北) 正常處理
        utc_cutoff = dt.datetime(2026, 10, 20, 5, 30, tzinfo=UTC)
        self.assertIsNone(self.evaluate(make_batch(STANDARD, cutoff=utc_cutoff)).batch_rejection)
        # 17:00Z = 隔日 01:00 台北：資料截止「台北日期」是 10/21，故預定進場日為 10/22，as_of=10/21 必須被拒絕
        late_utc = dt.datetime(2026, 10, 20, 17, 0, tzinfo=UTC)
        res = self.evaluate(make_batch(STANDARD, cutoff=late_utc, generated=dt.datetime(2026, 10, 20, 17, 30, tzinfo=UTC)))
        self.assertEqual(res.batch_rejection, BatchRejectCode.AS_OF_DATE_MISMATCH)

    # 20. 預測資料時序錯誤
    def test_20_temporal_errors(self) -> None:
        # 同日成交：as_of_date 等於資料截止日。PredictionBatch 合約允許，但策略拒絕 (不得以同日收盤價成交)
        same_day = make_batch(STANDARD, as_of=dt.date(2026, 10, 20))
        same_day.validate()
        self.assertEqual(self.evaluate(same_day).batch_rejection, BatchRejectCode.AS_OF_DATE_MISMATCH)
        # 跳日
        self.assertEqual(self.evaluate(make_batch(STANDARD, as_of=dt.date(2026, 10, 22))).batch_rejection,
                         BatchRejectCode.AS_OF_DATE_MISMATCH)
        # 決策早於預測
        early = dt.datetime(2026, 10, 20, 17, 0, tzinfo=TZ)
        self.assertEqual(self.evaluate(decision_time=early).batch_rejection, BatchRejectCode.DECISION_BEFORE_PREDICTION)
        # 快照 / 價格來自決策之後
        late = dt.datetime(2026, 10, 20, 19, 30, tzinfo=TZ)
        self.assertEqual(self.evaluate(snapshot=make_snapshot(as_of=late)).batch_rejection, BatchRejectCode.SNAPSHOT_FROM_FUTURE)
        self.assertEqual(self.evaluate(snapshot=make_snapshot(prices_as_of=late)).batch_rejection, BatchRejectCode.PRICES_FROM_FUTURE)
        # 訊號有效期限已過 (預定進場日 13:30 起不再產生)
        too_late = dt.datetime(2026, 10, 21, 13, 30, tzinfo=TZ)
        res = self.evaluate(snapshot=make_snapshot(as_of=SNAP_TIME), decision_time=too_late)
        self.assertEqual(res.batch_rejection, BatchRejectCode.DECISION_TOO_LATE)
        for r in (same_day,):
            self.assertEqual(len(self.evaluate(r).signals), 0)

    def test_20b_signal_times(self) -> None:
        res = self.evaluate()
        s = res.signals[0]
        self.assertEqual(s.created_at, DECISION)
        self.assertEqual(s.expires_at, dt.datetime(2026, 10, 21, 13, 30, tzinfo=TZ))
        self.assertEqual(res.planned_entry_date, AS_OF)
        self.assertGreater(s.expires_at, s.created_at)
        s.validate_execution_timing(dt.datetime(2026, 10, 21, 10, 0, tzinfo=TZ))   # 隔日盤中可成交
        with self.assertRaises(ValueError):                                         # 同日收盤價成交 = 前視偏差
            s.validate_execution_timing(dt.datetime(2026, 10, 20, 13, 30, tzinfo=TZ))
        self.assertEqual(s.limit_price, 100.0)
        self.assertEqual(s.model_version, "v-syn-1")
        self.assertEqual(s.strategy_id, "ai_rank_ew:1.0.0")

    # 21. 非交易日 / 日曆缺失
    def test_21_calendar_missing_and_non_trading_day(self) -> None:
        res = self.evaluate(engine=StrategyEngine(self.config, None))
        self.assertEqual(res.batch_rejection, BatchRejectCode.CALENDAR_UNAVAILABLE)
        self.assertEqual(len(res.signals), 0)
        # 10/23 為合成休市日：T=10/22 的資料，下一交易日是 10/26，不是 10/23
        thu = dt.datetime(2026, 10, 22, 13, 30, tzinfo=TZ)
        gen = dt.datetime(2026, 10, 22, 18, 0, tzinfo=TZ)
        decision = dt.datetime(2026, 10, 22, 19, 0, tzinfo=TZ)
        snap = make_snapshot(as_of=dt.datetime(2026, 10, 22, 18, 30, tzinfo=TZ), prices_as_of=thu)
        on_holiday = make_batch(STANDARD, cutoff=thu, generated=gen, as_of=dt.date(2026, 10, 23))
        self.assertEqual(self.evaluate(on_holiday, snap, decision_time=decision).batch_rejection, BatchRejectCode.AS_OF_DATE_MISMATCH)
        ok = make_batch(STANDARD, cutoff=thu, generated=gen, as_of=dt.date(2026, 10, 26))
        res2 = self.evaluate(ok, snap, decision_time=decision)
        self.assertIsNone(res2.batch_rejection)
        self.assertEqual(res2.planned_entry_date, dt.date(2026, 10, 26))
        # 日曆涵蓋範圍之外：拒絕猜測
        far = dt.datetime(2027, 1, 5, 13, 30, tzinfo=TZ)
        far_batch = make_batch(STANDARD, cutoff=far, generated=far + dt.timedelta(hours=4), as_of=dt.date(2027, 1, 6))
        far_snap = make_snapshot(as_of=far + dt.timedelta(hours=5), prices_as_of=far)
        res3 = self.evaluate(far_batch, far_snap, decision_time=far + dt.timedelta(hours=5))
        self.assertEqual(res3.batch_rejection, BatchRejectCode.CALENDAR_UNAVAILABLE)

    def test_21b_calendar_unit(self) -> None:
        cal = self.calendar
        self.assertTrue(cal.is_trading_day(dt.date(2026, 10, 22)))
        self.assertFalse(cal.is_trading_day(dt.date(2026, 10, 23)))
        self.assertFalse(cal.is_trading_day(dt.date(2026, 10, 24)))
        self.assertEqual(cal.next_trading_day(dt.date(2026, 10, 22)), dt.date(2026, 10, 26))
        self.assertEqual(cal.trading_days_between(dt.date(2026, 10, 21), dt.date(2026, 10, 27)), 3)
        for bad in (lambda: cal.is_trading_day(dt.date(2027, 6, 1)),
                    lambda: cal.next_trading_day(dt.date(2026, 12, 31)),
                    lambda: cal.is_trading_day(dt.datetime(2026, 10, 22, 9, 0)),
                    lambda: cal.trading_days_between(dt.date(2026, 10, 27), dt.date(2026, 10, 21))):
            with self.assertRaises(CalendarError):
                bad()
        with self.assertRaises(CalendarError):
            StaticTradingCalendar([], dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        with self.assertRaises(CalendarError):
            StaticTradingCalendar([dt.date(2027, 1, 4)], dt.date(2026, 1, 1), dt.date(2026, 12, 31))


class TestDedupAndDeterminism(StrategyTestBase):
    # 22. 同一訊號重複產生
    def test_22_duplicate_signal_suppressed(self) -> None:
        first = self.evaluate()
        self.assertEqual(len(first.signals), 3)
        second = self.evaluate()
        self.assertEqual(len(second.signals), 0)
        self.assertTrue(all(decisions_of(second)[s].reason == StrategyReason.DUPLICATE_SIGNAL_SUPPRESSED for s in ("2330", "2317", "2454")))
        # 另一批預測 (不同 batch_id / 分數)，同策略同標的同預定進場日 -> 仍抑制
        other = self.evaluate(make_batch([("2330", 0.95), ("2317", 0.85)], batch_id="B-2"))
        self.assertEqual(len(other.signals), 0)
        # signal_id 為確定性：全新引擎得到相同 id，下游去重可作為第二道防線
        fresh = StrategyEngine(self.config, self.calendar).evaluate(make_batch(STANDARD), make_snapshot(), DECISION)
        self.assertEqual([s.signal_id for s in fresh.signals], [s.signal_id for s in first.signals])
        self.assertEqual(len({s.signal_id for s in first.signals}), 3)
        self.assertTrue(all(len(s.signal_id) == 44 for s in first.signals))  # "STR-" + 40 hex

    def test_22b_signal_id_depends_on_all_key_fields(self) -> None:
        base = self.evaluate().signals[0].signal_id
        other_version = StrategyEngine(make_config(strategy_version="1.0.1"), self.calendar).evaluate(make_batch(STANDARD), make_snapshot(), DECISION)
        self.assertNotEqual(base, other_version.signals[0].signal_id)
        other_name = StrategyEngine(make_config(strategy_name="other"), self.calendar).evaluate(make_batch(STANDARD), make_snapshot(), DECISION)
        self.assertNotEqual(base, other_name.signals[0].signal_id)
        with self.assertRaises(StrategyConfigError):
            make_config(strategy_name="bad\x1fname")

    # 28. 輸入順序不同但內容相同 -> 決策確定性
    def test_28_deterministic_regardless_of_input_order(self) -> None:
        failed = {"2412": "x", "2308": "y"}
        px2002 = {"2002": D("100.00")}
        a = StrategyEngine(self.config, self.calendar).evaluate(
            make_batch(STANDARD[:3], failed=failed),
            make_snapshot(positions={"2330": (1000, dt.date(2026, 10, 20)), "2002": (500, dt.date(2026, 10, 15))},
                          cash="1800000.00", prices=px2002), DECISION)
        b = StrategyEngine(self.config, self.calendar).evaluate(
            make_batch(list(reversed(STANDARD[:3])), failed=failed, reverse=True),
            make_snapshot(positions={"2002": (500, dt.date(2026, 10, 15)), "2330": (1000, dt.date(2026, 10, 20))},
                          cash="1800000.00", prices=px2002), DECISION)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual([s.signal_id for s in a.signals], [s.signal_id for s in b.signals])


class TestRiskAndGatewayIntegration(StrategyTestBase):
    def _stack(self, cash: str = "2000000.00", risk_cfg: Optional[RiskConfig] = None):
        broker = TaiwanPaperBroker(TaiwanPaperConfig(initial_cash=D(cash)))
        risk = RiskEngine(risk_cfg or RiskConfig())
        return broker, risk, TradingGateway(broker, risk)

    def _snapshot(self, broker, entry_dates=None, as_of=SNAP_TIME, prices_as_of=PRICES_TIME):
        return PortfolioSnapshot.from_broker(broker, as_of, {s: D("100.00") for s in SYMS}, prices_as_of, entry_dates or {})

    # 23. 風控拒絕：不重新包裝繞過
    def test_23_risk_rejection_is_final(self) -> None:
        broker, risk, gateway = self._stack()
        # 策略設定比風控寬鬆 (15% > 風控 10%)，且未提供 risk_config -> 訊號會被風控拒絕
        cfg = make_config(target_position_ratio=D("0.15"), max_single_position_ratio=D("0.15"))
        engine = StrategyEngine(cfg, self.calendar)
        snap = self._snapshot(broker)
        res = engine.evaluate(make_batch(STANDARD), snap, DECISION)
        records = dispatch_signals(res, gateway, snap)
        self.assertEqual(len(records), 3)
        for rec in records:
            self.assertFalse(rec.risk_allowed)
            self.assertIn("SINGLE_POSITION_LIMIT_EXCEEDED", rec.reason_codes)
            self.assertIsNone(rec.order_id)                      # 未建立任何委託
        self.assertEqual(len(broker.order_manager.get_open_orders()), 0)
        self.assertEqual(broker.get_cash(), D("2000000.00"))
        self.assertEqual(broker.get_reserved_cash(), D("0.00"))
        # 重新評估：同策略同標的同日不會再產生新訊號 (不能改包裝後再試)
        again = engine.evaluate(make_batch(STANDARD, batch_id="B-retry"), snap, DECISION)
        self.assertEqual(len(again.signals), 0)
        # 即使把舊訊號原封不動重送，風控也以 DUPLICATE_SIGNAL 擋下
        resend = dispatch_signals(res, gateway, snap)
        self.assertTrue(all(not r.risk_allowed and "DUPLICATE_SIGNAL" in r.reason_codes for r in resend))
        self.assertEqual(len(broker.order_manager.get_open_orders()), 0)

    # 24. Kill Switch：進場被擋，降低風險的平倉仍依風控規則放行
    def test_24_kill_switch_blocks_buys_allows_risk_reducing_close(self) -> None:
        broker, risk, gateway = self._stack()
        engine = StrategyEngine(self.config, self.calendar, risk_config=risk.config)
        snap = self._snapshot(broker)
        res = engine.evaluate(make_batch([("2330", 0.9)]), snap, DECISION)
        recs = dispatch_signals(res, gateway, snap)
        self.assertTrue(recs[0].risk_allowed)
        broker.process_bar(MarketBar("2330", dt.datetime(2026, 10, 21, 10, 0, tzinfo=TZ), D("100"), D("100.5"), D("99.5"),
                                     D("100.00"), 100_000_000, D("100.00")))
        held = broker.get_positions()["2330"].quantity
        self.assertEqual(held, 1999)

        risk.activate_kill_switch(authorized_by="tester", reason="unit test")
        # 10/26 的資料 -> 預定進場日 10/27；2330 於 10/21 成交，(21,27] 共 3 個交易日 -> 持有期限屆滿
        cutoff = dt.datetime(2026, 10, 26, 13, 30, tzinfo=TZ)
        gen = dt.datetime(2026, 10, 26, 18, 0, tzinfo=TZ)
        decision = dt.datetime(2026, 10, 26, 19, 0, tzinfo=TZ)
        snap2 = self._snapshot(broker, {"2330": dt.date(2026, 10, 21)}, as_of=dt.datetime(2026, 10, 26, 18, 30, tzinfo=TZ), prices_as_of=cutoff)
        batch2 = make_batch([("2317", 0.9)], cutoff=cutoff, generated=gen, as_of=dt.date(2026, 10, 27), batch_id="B-day2")
        res2 = engine.evaluate(batch2, snap2, decision)
        self.assertEqual([s.action for s in res2.signals], [SignalAction.CLOSE, SignalAction.BUY])
        recs2 = {r.symbol: r for r in dispatch_signals(res2, gateway, snap2)}
        self.assertFalse(recs2["2317"].risk_allowed)
        self.assertIn("KILL_SWITCH_ACTIVE", recs2["2317"].reason_codes)
        self.assertIsNone(recs2["2317"].order_id)
        self.assertTrue(recs2["2330"].risk_allowed)                  # 降低風險的平倉放行
        self.assertEqual(recs2["2330"].order_status, "ACCEPTED")     # 已預留持倉，尚未成交
        self.assertEqual(broker.ledger.reserved_shares["2330"], 1999)
        self.assertEqual(broker.get_positions()["2330"].quantity, 1999)
        self.assertTrue(risk.kill_switch_active)

    def test_24b_strategy_never_touches_broker_directly(self) -> None:
        broker, _risk, _gw = self._stack()
        snap = self._snapshot(broker)
        before = (broker.get_cash(), broker.get_reserved_cash(), len(broker.order_manager.get_open_orders()))
        self.engine.evaluate(make_batch(STANDARD), snap, DECISION)
        after = (broker.get_cash(), broker.get_reserved_cash(), len(broker.order_manager.get_open_orders()))
        self.assertEqual(before, after)   # evaluate 本身不下單，也不改帳本

    def test_24c_from_broker_snapshot(self) -> None:
        broker, _risk, gateway = self._stack()
        engine = StrategyEngine(self.config, self.calendar, risk_config=RiskConfig())
        snap = self._snapshot(broker)
        dispatch_signals(engine.evaluate(make_batch([("2330", 0.9)]), snap, DECISION), gateway, snap)
        snap2 = self._snapshot(broker, as_of=dt.datetime(2026, 10, 20, 19, 30, tzinfo=TZ))
        self.assertIn("2330", snap2.pending_buy_symbols)
        self.assertEqual(snap2.nav, D("2000000.00"))
        self.assertEqual(snap2.reserved_cash, broker.get_reserved_cash())


class TestConfigValidation(unittest.TestCase):
    def test_required_fields_have_no_defaults(self) -> None:
        with self.assertRaises(TypeError):
            StrategyConfig()  # type: ignore[call-arg]

    def test_invalid_values_rejected(self) -> None:
        bad_cases = [
            dict(max_positions=0), dict(max_positions=True), dict(max_positions=2.5),
            dict(holding_trading_days=0), dict(share_unit=0),
            dict(min_score=True), dict(min_score=float("inf")),
            dict(target_position_ratio=0.1),                      # float 不接受
            dict(target_position_ratio=D("0")), dict(target_position_ratio=D("NaN")),
            dict(min_cash_ratio=D("1")), dict(min_cash_ratio=D("-0.1")),
            dict(max_positions=9, target_position_ratio=D("0.10"), max_single_position_ratio=D("0.10")),  # 9*10% > 80%
            dict(rebalance_tolerance_ratio=D("0.10")),
            dict(strategy_name=""), dict(strategy_name="a b"), dict(strategy_version="v:1"),
            dict(order_type="LIMIT"), dict(cost_model=None),
            dict(signal_validity=dt.timedelta(0)), dict(signal_validity=dt.timedelta(days=2)),
            dict(allow_new_positions=1),
        ]
        for kw in bad_cases:
            with self.subTest(kw=kw):
                with self.assertRaises(StrategyConfigError):
                    make_config(**kw)

    def test_market_order_config_produces_market_signal(self) -> None:
        engine = StrategyEngine(make_config(order_type=SignalOrderType.MARKET), make_calendar())
        res = engine.evaluate(make_batch(STANDARD), make_snapshot(), DECISION)
        self.assertTrue(res.signals)
        self.assertTrue(all(s.order_type == SignalOrderType.MARKET and s.limit_price is None for s in res.signals))

    def test_snapshot_validation(self) -> None:
        with self.assertRaises(StrategyInputError):
            PositionSnapshot("2330", 0, 0, None)
        with self.assertRaises(StrategyInputError):
            PositionSnapshot("2330", 100, 200, None)
        with self.assertRaises(StrategyInputError):
            PositionSnapshot("2330.TW", 100, 0, None)
        with self.assertRaises(StrategyInputError):
            PositionSnapshot("2330", 100.0, 0, None)  # type: ignore[arg-type]
        with self.assertRaises(StrategyInputError):
            make_snapshot(cash="-1")
        with self.assertRaises(StrategyInputError):   # 進場日晚於快照日期
            make_snapshot(positions={"2330": (100, dt.date(2026, 10, 25))})


if __name__ == "__main__":
    unittest.main()
