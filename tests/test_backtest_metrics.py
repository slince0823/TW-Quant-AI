"""第三階段 C-2.5：績效分析、回測報告與防偽驗收測試 (test_backtest_metrics.py).

所有公式測試皆以小型手算案例驗證 (測試內以註解列出手算過程)。端到端測試使用顯式標記
`is_synthetic=True` 之合成資料，僅驗證分析器與報告邏輯，不代表任何真實台股績效。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from decimal import Decimal
from typing import Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_backtest_runner as _rt  # noqa: E402  (僅重用其合成情境建構器；以模組方式匯入避免重複收集測試)

from tw_quant.backtest import (  # noqa: E402
    METHODOLOGY_METRICS,
    TAIPEI_TZ,
    AnalyzerConfig,
    BacktestMetricsError,
    BacktestReport,
    BenchmarkAlignmentError,
    BenchmarkReturnBasis,
    BenchmarkSeries,
    DataProvenance,
    DatasetChecksumVerifier,
    Ed25519KeyPair,
    EvidenceVerificationResult,
    FileDigestEvidenceVerifier,
    IncompleteNavPolicy,
    InMemoryPredictionFeed,
    IndependentEvidenceVerifier,
    InvalidNavSeriesError,
    InvalidRootAnchorError,
    KeyAuthorityLevel,
    LineageAttestation,
    LineageStatus,
    MetricStatus,
    NavPoint,
    OfficialTrustAuthority,
    OfficialTrustStatus,
    PerformanceAnalyzer,
    ReplayStore,
    ReportSignature,
    SignatureContext,
    TrustedPublicKeyStore,
    ValidationVerdict,
    WalkForwardRunConfig,
    generate_ed25519_keypair,
    get_default_replay_store,
    validate_nav_series,
    validate_signature_timestamps,
    verify_prediction_consumption_pit,
)
from tw_quant.backtest.crypto import (  # noqa: E402
    _P,
    _decodepoint,
    ed25519_publickey,
    ed25519_sign,
    ed25519_verify,
)
from tw_quant.backtest.lot_ledger import ClosedLotTrade  # noqa: E402
from tw_quant.backtest.metrics import max_drawdown_and_duration  # noqa: E402
from tw_quant.backtest.prediction_feed import PredictionConsumptionRecord  # noqa: E402
from tw_quant.risk.config import RiskConfig  # noqa: E402


def _create_test_official_store(*keypairs: Ed25519KeyPair) -> Tuple[TrustedPublicKeyStore, Ed25519KeyPair]:
    """測試專用：建立具備官方信任根錨點之 TrustedPublicKeyStore 並為指定金鑰完成官方根背書登記."""
    root_kp = generate_ed25519_keypair("TEST_OFFICIAL_ROOT_AUTHORITY", KeyAuthorityLevel.OFFICIAL)
    store = TrustedPublicKeyStore(official_root_public_key=root_kp.public_key_bytes)
    for kp in keypairs:
        endorsement = root_kp.sign(f"OFFICIAL_GRANT:{kp.signer_id}:{kp.public_key_hex}".encode("utf-8"))
        store.enroll_key(
            kp.signer_id,
            kp.public_key_bytes,
            KeyAuthorityLevel.OFFICIAL,
            root_endorsement_signature=endorsement,
        )
    OfficialTrustAuthority.activate_isolated_test_context(store=store, test_root=root_kp.public_key_bytes)
    return store, root_kp


@dataclasses.dataclass(frozen=True)
class MockIndependentEvidenceVerifier:
    """僅供測試環境模擬獨立驗證通過或失敗之驗證器 (已自生產程式碼完全隔離至測試層)."""

    is_verified: bool = True
    verifier_name: str = "MockIndependentEvidenceVerifier"
    failure_reasons: Tuple[str, ...] = ()
    details: dict = dataclasses.field(default_factory=dict)

    def verify_evidence(
        self,
        result: Any,
        *,
        benchmark: Optional[BenchmarkSeries] = None,
        attestation: Optional[LineageAttestation] = None,
    ) -> EvidenceVerificationResult:
        now = dt.datetime.now(TAIPEI_TZ)
        if result.is_synthetic and self.is_verified:
            return EvidenceVerificationResult(
                is_verified=False,
                verifier_name=self.verifier_name,
                verified_at=now,
                failure_reasons=("合成資料拒絕授予真實歷史驗證",),
            )
        return EvidenceVerificationResult(
            is_verified=self.is_verified,
            verifier_name=self.verifier_name,
            verified_at=now,
            details=self.details,
            failure_reasons=self.failure_reasons,
        )

D = Decimal
D0 = dt.date(2025, 1, 6)


def _dates(n: int, start: dt.date = dt.date(2025, 1, 7)) -> list:
    return [start + dt.timedelta(days=i) for i in range(n)]


def _pts(navs: Sequence[str], start: dt.date = dt.date(2025, 1, 7)) -> list:
    return [NavPoint(d, D(v)) for d, v in zip(_dates(len(navs), start), navs)]


def _cfg(a: int = 4, rf_daily: float = 0.0, **kw) -> AnalyzerConfig:
    """A=4 使手算簡潔；rf_annual = (1+rf_daily)^A - 1 使每日無風險利率恰為 rf_daily。"""
    return AnalyzerConfig(
        trading_days_per_year=a,
        risk_free_rate_annual=(1.0 + rf_daily) ** a - 1.0,
        **kw,
    )


def _trade(pnl: str, hold: Optional[int], qty: int = 1000, tag: str = "T") -> ClosedLotTrade:
    """建構已平倉批次：買進名目 10,000 + 手續費 20；賣出手續費 20、證交稅 30；淨損益 = pnl。"""
    entry_basis = D("10020")
    exit_net = entry_basis + D(pnl)
    exit_gross = exit_net + D("50")
    return ClosedLotTrade(
        trade_id=f"{tag}-{pnl}-{hold}", lot_id="L", symbol="2330", strategy_id="s",
        entry_fill_id="F1", exit_fill_id="F2", entry_order_id="O1", exit_order_id="O2",
        entry_date=D0, exit_date=D0 + dt.timedelta(days=2),
        entry_timestamp=dt.datetime(2025, 1, 6, 9, 0, tzinfo=TAIPEI_TZ),
        exit_timestamp=dt.datetime(2025, 1, 8, 9, 0, tzinfo=TAIPEI_TZ),
        holding_calendar_days=2, holding_trading_days=hold, quantity=qty,
        entry_price=D("10"), exit_price=exit_gross / D(qty),
        entry_cost_basis=entry_basis, allocated_entry_commission=D("20"),
        exit_gross_proceeds=exit_gross, allocated_exit_commission=D("20"), allocated_exit_tax=D("30"),
        exit_net_proceeds=exit_net, realized_pnl=D(pnl),
    )


def _prov(synthetic: bool, source: str = "SRC") -> DataProvenance:
    return DataProvenance(
        source=source, dataset_version="v1", retrieved_at=dt.datetime(2025, 1, 1, 9, 0, tzinfo=TAIPEI_TZ),
        is_synthetic=synthetic,
    )


def _bench(dates, levels, base_date, *, basis=BenchmarkReturnBasis.TOTAL_RETURN, synthetic=True, base_level="100"):
    pts = ((base_date, D(base_level)),) + tuple((d, D(l)) for d, l in zip(dates, levels))
    return BenchmarkSeries("BENCH", basis, _prov(synthetic, "BENCH_SRC"), pts)


class _Scenarios:
    """以 C-2.4 測試建構器產生合成回測結果。"""

    @staticmethod
    def harness():
        h = _rt.TestBacktestRunnerPhase3C24("test_01_daily_12_step_causal_sequence")
        h.setUp()
        return h

    @classmethod
    def _feed(cls, h, batches):
        feed = InMemoryPredictionFeed(calendar=h.calendar, provenance=h.provenance)
        for i, (dec, tgt, ranks) in enumerate(batches, start=1):
            feed.register_batch(
                _rt._make_prediction_batch(batch_id=f"M-{i}-{dec}", decision_date=dec, target_trade_date=tgt, scores_and_ranks=ranks),
                available_at=dt.datetime.combine(dec, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            )
        return feed

    @classmethod
    def full_cycle(cls):
        """買進 -> 部分減碼 -> 除息入帳 -> 全數平倉 (含已實現損益與股利)。"""
        h = cls.harness()
        mb = _rt._make_bar
        bars = [
            mb("2330", h.d1, "19", "19.5", "18.8", "19", "19"), mb("2317", h.d1, "38", "38.5", "37.5", "38", "38"),
            mb("2330", h.d2, "19", "20.8", "19", "20.8", "19"), mb("2317", h.d2, "38", "38.5", "37.8", "38.0", "38"),
            mb("2330", h.d3, "20.8", "22.0", "20.8", "22.0", "20.8"), mb("2317", h.d3, "38.0", "39.2", "38.0", "39.0", "38.0"),
            mb("2330", h.d4, "22.0", "22.2", "21.8", "22.0", "22.0"), mb("2317", h.d4, "37.0", "37.5", "36.8", "37.0", "37.0"),
            mb("2330", h.d5, "22.0", "22.2", "21.8", "22.0", "22.0"), mb("2317", h.d5, "37.0", "37.5", "36.8", "37.0", "37.0"),
            mb("2330", h.d6, "22.0", "22.2", "21.8", "22.0", "22.0"), mb("2317", h.d6, "37.0", "37.5", "36.8", "37.0", "37.0"),
        ]
        corp = _rt.CorporateActionProcessor(calendar=h.calendar)
        corp.register_event(_rt.CorporateActionEvent(
            event_id="CA-M", symbol="2317", action_type=_rt.CorporateActionType.CASH_DIVIDEND,
            ex_date=h.d4, payable_date=h.d5, cash_dividend_per_share=D("2.00"),
            announced_at=dt.datetime(2025, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 1, 2, 18, 30, tzinfo=TAIPEI_TZ),
            provenance=h.provenance, is_synthetic_payable_date=True,
        ))
        feed = cls._feed(h, [
            (h.d1, h.d2, [("2330", 0.95, 1), ("2317", 0.88, 2)]),
            (h.d2, h.d3, [("2330", 0.92, 1), ("2317", 0.85, 2)]),
            (h.d3, h.d4, [("2330", 0.90, 1), ("2317", 0.84, 2)]),
            (h.d4, h.d5, [("2330", 0.89, 1), ("2317", 0.83, 2)]),
            (h.d5, h.d6, [("2330", -0.50, 1), ("2317", -0.60, 2)]),
        ])
        runner = h._build_stack(
            bars=bars, prediction_feed=feed, corporate_processor=corp,
            strategy_config=_rt._make_strategy_config(top_n=2, max_position_weight="0.385", holding_trading_days=10, rebalance_drift_threshold_pct="0.02"),
            run_config=WalkForwardRunConfig(start_date=h.d1, end_date=h.d6, universe_symbols=("2330", "2317"), skip_prediction_on_final_day=True),
        )
        return runner.run()

    @classmethod
    def open_position(cls):
        """買進後期末仍持有 (無已平倉交易)。"""
        h = cls.harness()
        mb = _rt._make_bar
        bars = [mb("2330", h.d1, "100", "101", "99", "100", "100"), mb("2330", h.d2, "100", "105", "100", "105", "100")]
        feed = cls._feed(h, [(h.d1, h.d2, [("2330", 0.90, 1)])])
        runner = h._build_stack(
            bars=bars, prediction_feed=feed,
            strategy_config=_rt._make_strategy_config(top_n=1, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(start_date=h.d1, end_date=h.d2, universe_symbols=("2330",), skip_prediction_on_final_day=True),
        )
        return runner.run()

    @classmethod
    def partial_fill(cls):
        h = cls.harness()
        mb = _rt._make_bar
        bars = [mb("2330", h.d1, "100", "101", "99", "100", "100", volume=500_000),
                mb("2330", h.d2, "100", "101", "99", "100", "100", volume=40_000)]
        feed = cls._feed(h, [(h.d1, h.d2, [("2330", 0.92, 1)])])
        runner = h._build_stack(
            bars=bars, prediction_feed=feed,
            strategy_config=_rt._make_strategy_config(top_n=1, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(start_date=h.d1, end_date=h.d2, universe_symbols=("2330",), skip_prediction_on_final_day=True),
        )
        return runner.run()

    @classmethod
    def risk_rejected(cls):
        h = cls.harness()
        mb = _rt._make_bar
        bars = [mb("2330", h.d1, "100", "101", "99", "100", "100"), mb("2330", h.d2, "100", "101", "99", "100", "100")]
        feed = cls._feed(h, [(h.d1, h.d2, [("2330", 0.95, 1)])])
        runner = h._build_stack(
            bars=bars, prediction_feed=feed,
            strategy_config=_rt._make_strategy_config(top_n=1, max_position_weight="0.45"),
            run_config=WalkForwardRunConfig(start_date=h.d1, end_date=h.d2, universe_symbols=("2330",), skip_prediction_on_final_day=True),
        )
        runner.risk_engine.activate_kill_switch("risk_officer", "metrics test")
        return runner.run()


def _as_real(result, *, mock: bool = False):
    """將合成情境結果改為「宣稱非合成」之假想結果，僅用於測試防偽閘門邏輯 (非真實資料)。"""
    recs = tuple(dataclasses.replace(r, is_synthetic=False, is_mock=mock) for r in result.consumed_predictions)
    cal = dict(result.calendar_metadata_summary)
    cal["is_synthetic"] = False
    return dataclasses.replace(
        result, is_synthetic=False, consumed_predictions=recs, calendar_metadata_summary=cal,
        market_data_provenance=_prov(False, "CANDIDATE_MARKET_FEED"), prediction_provenance=_prov(False, "CANDIDATE_PRED_FEED"),
    )


class TestMetricsFormulas(unittest.TestCase):
    """手算公式測試。"""

    def an(self, **kw) -> PerformanceAnalyzer:
        return PerformanceAnalyzer(_cfg(**kw))

    def nav(self, v0: str, navs, **kw):
        m, _ = self.an(**kw).compute_nav_metrics(D(v0), _pts(navs))
        return m

    def test_01_positive_negative_zero_return(self) -> None:
        # (110-100)/100 = 0.1； (90-100)/100 = -0.1； 持平 = 0 (且為真實 OK 值，不是缺失)
        self.assertEqual(self.nav("100", ["110"])["total_return"].value, D("0.1"))
        self.assertEqual(self.nav("100", ["90"])["total_return"].value, D("-0.1"))
        zero = self.nav("100", ["100"])["total_return"]
        self.assertEqual(zero.value, D("0"))
        self.assertEqual(zero.status, MetricStatus.OK)

    def test_02_cagr_and_volatility_hand_calc(self) -> None:
        # A=4, V0=100, 日報酬 r=[+0.1, -0.1] (NAV 110, 99), N=2
        # CAGR = (99/100)^(4/2) - 1 = 0.9801 - 1 = -0.0199
        # mean=0, s=sqrt((0.01+0.01)/1)=0.1*sqrt(2), vol=s*sqrt(4)=0.2*sqrt(2)=0.282842712
        m = self.nav("100", ["110", "99"])
        self.assertAlmostEqual(m["cagr"].value, -0.0199, places=12)
        self.assertAlmostEqual(m["annualized_volatility"].value, 0.2 * math.sqrt(2), places=12)
        self.assertIn("外推", m["cagr"].note)  # N=2 < 21：標註短窗口外推

    def test_03_sharpe_annualization_and_risk_free(self) -> None:
        # A=4, rf 日=0.05, r=[0.2, 0.0] (NAV 120,120)  => e=[0.15,-0.05], mean=0.05, s=sqrt((0.01+0.01))=0.141421
        # Sharpe = 0.05/0.141421*sqrt(4) = 0.707107
        m = self.nav("100", ["120", "120"], rf_daily=0.05)
        self.assertAlmostEqual(m["sharpe_ratio"].value, 0.05 / math.sqrt(0.02) * 2, places=10)
        # rf=0 時：e=r=[0.2,0]，mean=0.1，Sharpe = 0.1/0.141421*2 = 1.414214
        m0 = self.nav("100", ["120", "120"], rf_daily=0.0)
        self.assertAlmostEqual(m0["sharpe_ratio"].value, math.sqrt(2), places=10)
        # 零波動 (固定 +10%)：分母為零 → UNDEFINED，value=None
        flat = self.nav("100", ["110", "121"], rf_daily=0.0)["sharpe_ratio"]
        self.assertEqual(flat.status, MetricStatus.UNDEFINED)
        self.assertIsNone(flat.value)
        # N<2
        self.assertEqual(self.nav("100", ["110"])["sharpe_ratio"].status, MetricStatus.INSUFFICIENT_DATA)

    def test_04_sortino_and_zero_downside(self) -> None:
        # rf 日=0.05, e=[0.15,-0.05]; DD_daily = sqrt(((-0.05)^2)/2)=0.0353553; DD_ann=DD*sqrt(4)=0.0707107
        # Sortino = mean(e)*A/DD_ann = 0.05*4/0.0707107 = 2.828427
        m = self.nav("100", ["120", "120"], rf_daily=0.05)
        self.assertAlmostEqual(m["sortino_ratio"].value, 0.05 * 4 / (math.sqrt(0.0025 / 2) * 2), places=10)
        # 無任何低於目標報酬之日 → 下行偏差為 0 → UNDEFINED
        z = self.nav("100", ["110", "130"], rf_daily=0.0)["sortino_ratio"]
        self.assertEqual(z.status, MetricStatus.UNDEFINED)
        self.assertIsNone(z.value)

    def test_05_calmar_and_zero_drawdown(self) -> None:
        # NAV 90,99: V0=100 → MDD = 0.1; CAGR=(0.99)^(4/2)-1=-0.0199 → Calmar=-0.199
        m = self.nav("100", ["90", "99"])
        self.assertEqual(m["max_drawdown"].value, D("0.1"))
        self.assertAlmostEqual(m["calmar_ratio"].value, -0.0199 / 0.1, places=10)
        # 單調上升：MDD=0 → Calmar UNDEFINED
        up = self.nav("100", ["101", "102", "103"])
        self.assertEqual(up["max_drawdown"].value, D("0"))
        self.assertEqual(up["calmar_ratio"].status, MetricStatus.UNDEFINED)
        self.assertIsNone(up["calmar_ratio"].value)

    def test_06_max_drawdown_and_duration(self) -> None:
        # V0=100: 120(新高) 90(dd .25,tau1) 100(tau2) 130(新高,tau0) 117(tau1) 117(tau2) 125(tau3,期末未恢復)
        # MDD = (120-90)/120 = 0.25；最長回撤 = 3 日且未恢復
        mdd, dur, rec = max_drawdown_and_duration(D("100"), [D(x) for x in ("120", "90", "100", "130", "117", "117", "125")])
        self.assertEqual(mdd, D("0.25"))
        self.assertEqual(dur, 3)
        self.assertFalse(rec)
        # 第一段回撤較長且已恢復：V0=100: 90 95 100(恢復,tau0) 99 → episodes (3? 90 tau1,95 tau2,100>=hwm 恢復) =(2,True),(1,False)
        mdd2, dur2, rec2 = max_drawdown_and_duration(D("100"), [D(x) for x in ("90", "95", "100", "99")])
        self.assertEqual((dur2, rec2), (2, True))
        self.assertEqual(mdd2, D("0.1"))
        m = self.nav("100", ["120", "90", "100", "130", "117", "117", "125"])
        self.assertEqual(m["max_drawdown_duration"].value, 3)
        self.assertIn("is_recovered=False", m["max_drawdown_duration"].note)

    def test_07_trade_statistics(self) -> None:
        # pnl = +100,+50,-30,-20,0 ; K=5, wins=2 → win_rate=0.4; avg_win=75; avg_loss=25; payoff=3; PF=150/50=3
        trades = [_trade("100", 2), _trade("50", 4), _trade("-30", 6), _trade("-20", 2), _trade("0", 1)]
        m = PerformanceAnalyzer(_cfg()).compute_trade_metrics(trades)
        self.assertEqual(m["closed_lot_trade_count"].value, 5)
        self.assertEqual(m["win_rate"].value, D("0.4"))
        self.assertEqual(m["average_win"].value, D("75"))
        self.assertEqual(m["average_loss"].value, D("25"))
        self.assertEqual(m["payoff_ratio"].value, D("3"))
        self.assertEqual(m["profit_factor"].value, D("3"))
        # 平均持有 = (2+4+6+2+1)/5 = 3
        self.assertEqual(m["average_holding_days"].value, D("3"))
        # 無虧損交易：Profit Factor 與盈虧比 UNDEFINED
        w = PerformanceAnalyzer(_cfg()).compute_trade_metrics([_trade("10", 1)])
        self.assertEqual(w["profit_factor"].status, MetricStatus.UNDEFINED)
        self.assertEqual(w["payoff_ratio"].status, MetricStatus.UNDEFINED)
        # 缺少持有天數 → INCOMPLETE，不忽略
        inc = PerformanceAnalyzer(_cfg()).compute_trade_metrics([_trade("10", None), _trade("-5", 2)])
        self.assertEqual(inc["average_holding_days"].status, MetricStatus.INCOMPLETE)

    def test_08_no_closed_trades_portfolio(self) -> None:
        m = PerformanceAnalyzer(_cfg()).compute_trade_metrics([])
        self.assertEqual(m["closed_lot_trade_count"].value, 0)
        for k in ("win_rate", "payoff_ratio", "profit_factor", "average_holding_days"):
            self.assertEqual(m[k].status, MetricStatus.NOT_AVAILABLE, k)
            self.assertIsNone(m[k].value)

    def test_10_invalid_nav_series_rejected(self) -> None:
        d = _dates(3)
        with self.assertRaises(InvalidNavSeriesError):  # 重複日期
            validate_nav_series(D("100"), [NavPoint(d[0], D("1")), NavPoint(d[0], D("1"))])
        with self.assertRaises(InvalidNavSeriesError):  # 未排序
            validate_nav_series(D("100"), [NavPoint(d[1], D("1")), NavPoint(d[0], D("1"))])
        with self.assertRaises(InvalidNavSeriesError):  # NAV 零
            validate_nav_series(D("100"), [NavPoint(d[0], D("0"))])
        with self.assertRaises(InvalidNavSeriesError):  # NAV 負
            validate_nav_series(D("100"), [NavPoint(d[0], D("-5"))])
        with self.assertRaises(InvalidNavSeriesError):  # 初始 NAV 非正
            validate_nav_series(D("0"), [])
        with self.assertRaises(InvalidNavSeriesError):  # 非有限
            validate_nav_series(D("100"), [NavPoint(d[0], D("NaN"))])
        with self.assertRaises(InvalidNavSeriesError):
            self.an().compute_nav_metrics(D("100"), [NavPoint(d[1], D("1")), NavPoint(d[0], D("1"))])
        # 空序列：NOT_AVAILABLE，絕不當成 0 報酬
        m, _ = self.an().compute_nav_metrics(D("100"), [])
        self.assertEqual(m["total_return"].status, MetricStatus.NOT_AVAILABLE)
        self.assertIsNone(m["total_return"].value)

    def test_14_benchmark_alignment_and_basis(self) -> None:
        pts = _pts(["110", "99"])
        dates = [p.date for p in pts]
        base = dates[0] - dt.timedelta(days=1)
        an = self.an()
        # 手算：策略 r=[0.1,-0.1]；基準 105,110.25 → b=[0.05,0.05]；a=[0.05,-0.15]
        # 累積超額 = (-0.01) - 0.1025 = -0.1125；TE = std(a)*sqrt(4)；std(a)=sqrt((0.1^2+0.1^2)/1)=0.141421 → 0.282843
        b = _bench(dates, ["105", "110.25"], base)
        m, st = an.compute_nav_metrics(D("100"), pts, benchmark=b)
        self.assertEqual(st, "SYNTHETIC_BENCHMARK")
        self.assertAlmostEqual(float(m["excess_return_total"].value), -0.1125, places=12)
        self.assertAlmostEqual(m["tracking_error"].value, 0.2 * math.sqrt(2), places=10)
        self.assertIn("合成", m["excess_return_total"].note)
        # 無 Benchmark → NOT_AVAILABLE
        m2, st2 = an.compute_nav_metrics(D("100"), pts)
        self.assertEqual(st2, "NOT_AVAILABLE")
        self.assertEqual(m2["excess_return_total"].status, MetricStatus.NOT_AVAILABLE)
        self.assertIsNone(m2["excess_return_total"].value)
        # 缺日 (壓縮報酬) → 日期不一致，不計算
        gap = BenchmarkSeries("B", BenchmarkReturnBasis.TOTAL_RETURN, _prov(True), ((base, D("100")), (dates[1], D("110"))))
        m3, st3 = an.compute_nav_metrics(D("100"), pts, benchmark=gap)
        self.assertEqual(st3, "DATE_MISMATCH")
        self.assertEqual(m3["tracking_error"].status, MetricStatus.BASIS_MISMATCH)
        # 基準日缺失 (沒有早於策略首日之水位)
        nobase = BenchmarkSeries("B", BenchmarkReturnBasis.TOTAL_RETURN, _prov(True), ((dates[0], D("100")), (dates[1], D("101"))))
        with self.assertRaises(BenchmarkAlignmentError):
            nobase.align(dates)
        # 報酬口徑不一致 (價格指數 vs 含股利 NAV)
        price = _bench(dates, ["105", "110.25"], base, basis=BenchmarkReturnBasis.PRICE_ONLY)
        m4, st4 = an.compute_nav_metrics(D("100"), pts, benchmark=price)
        self.assertEqual(st4, "BASIS_MISMATCH")
        self.assertEqual(m4["excess_return_annualized"].status, MetricStatus.BASIS_MISMATCH)
        # strict 模式直接拒絕
        strict = PerformanceAnalyzer(_cfg(strict_benchmark=True))
        with self.assertRaises(BenchmarkAlignmentError):
            strict.compute_nav_metrics(D("100"), pts, benchmark=price)
        with self.assertRaises(BenchmarkAlignmentError):
            strict.compute_nav_metrics(D("100"), pts, benchmark=gap)

    def test_14b_benchmark_extra_date_in_window(self) -> None:
        d = _dates(3)
        pts = [NavPoint(d[0], D("101")), NavPoint(d[2], D("102"))]  # 策略缺 d[1]
        b = BenchmarkSeries("B", BenchmarkReturnBasis.TOTAL_RETURN, _prov(True),
                            ((d[0] - dt.timedelta(days=1), D("100")), (d[0], D("101")), (d[1], D("102")), (d[2], D("103"))))
        with self.assertRaises(BenchmarkAlignmentError):
            b.align([p.date for p in pts])


class TestEndToEndAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.full = _Scenarios.full_cycle()
        cls.open = _Scenarios.open_position()
        cls.analyzer = PerformanceAnalyzer(AnalyzerConfig())
        cls.pm_full = cls.analyzer.analyze(cls.full)
        cls.pm_open = cls.analyzer.analyze(cls.open)

    def test_00_seventeen_metrics_registry_complete(self) -> None:
        self.assertEqual([r[0] for r in METHODOLOGY_METRICS], list(range(1, 18)))
        for no, key, *_ in METHODOLOGY_METRICS:
            self.assertIn(key, self.pm_full.metrics, f"指標 {no}:{key} 缺漏")
        # 第 1 項與 NAV 逐一一致 (手算：(final-initial)/initial)
        self.assertEqual(
            self.pm_full.get("total_return").value,
            (self.full.final_nav - self.full.initial_cash) / self.full.initial_cash,
        )

    def test_09_open_positions_consistent_with_daily_nav(self) -> None:
        r = self.pm_open.reconciliation
        self.assertEqual(self.open.open_lots_at_end.keys(), {"2330"})
        self.assertGreater(r.open_position_count, 0)
        self.assertIsNotNone(r.unrealized_pnl_open)
        self.assertEqual(r.status, MetricStatus.OK)
        self.assertLessEqual(abs(r.residual), D("0.01"))
        # 未實現損益不得被忽略：無已平倉交易時損益全部來自未平倉部位
        self.assertEqual(r.realized_pnl_net, D("0"))
        self.assertNotEqual(r.unrealized_pnl_open, D("0"))
        self.assertEqual(self.pm_open.get("win_rate").status, MetricStatus.NOT_AVAILABLE)
        # 與期末持倉市值對應
        last = self.open.nav_history[-1]
        self.assertEqual(r.final_nav, last.total_nav)
        # 竄改未平倉股數 → 與每日 NAV 不一致必須被攔截
        lots = self.open.open_lots_at_end["2330"]
        bad_lots = (dataclasses.replace(lots[0], remaining_quantity=lots[0].remaining_quantity - 1000),)
        bad = dataclasses.replace(self.open, open_lots_at_end={"2330": bad_lots})
        with self.assertRaises(InvalidNavSeriesError):
            self.analyzer.analyze(bad)

    def test_11_incomplete_nav_rejected_or_marked(self) -> None:
        recs = list(self.full.nav_history)
        recs[-1] = dataclasses.replace(recs[-1], total_nav=None, market_value=None, is_complete=False, is_official_nav=False)
        broken = dataclasses.replace(self.full, nav_history=tuple(recs))
        with self.assertRaises(InvalidNavSeriesError):
            PerformanceAnalyzer(AnalyzerConfig()).analyze(broken)
        marked = PerformanceAnalyzer(AnalyzerConfig(incomplete_nav_policy=IncompleteNavPolicy.MARK_INCOMPLETE)).analyze(broken)
        self.assertFalse(marked.nav_complete)
        for k in ("total_return", "cagr", "annualized_volatility", "sharpe_ratio", "max_drawdown", "calmar_ratio"):
            self.assertEqual(marked.get(k).status, MetricStatus.INCOMPLETE, k)
            self.assertIsNone(marked.get(k).value)
        self.assertEqual(marked.reconciliation.status, MetricStatus.INCOMPLETE)
        # 缺漏日期 (nav_history 少一天) 也必須拒絕
        missing = dataclasses.replace(self.full, nav_history=self.full.nav_history[:-1])
        with self.assertRaises(InvalidNavSeriesError):
            self.analyzer.analyze(missing)
        # 重複日期
        dup = dataclasses.replace(self.full, nav_history=self.full.nav_history + (self.full.nav_history[-1],))
        with self.assertRaises(InvalidNavSeriesError):
            self.analyzer.analyze(dup)

    def test_12_fees_taxes_slippage_not_double_counted(self) -> None:
        pm = self.pm_full
        trades = self.full.closed_lot_trades
        self.assertGreater(len(trades), 0)
        net = sum((t.realized_pnl for t in trades), D("0"))
        fees = sum((t.allocated_entry_commission + t.allocated_exit_commission + t.allocated_exit_tax for t in trades), D("0"))
        # 淨損益直接取帳本值 (已扣費)；毛損益 = 淨 + 費 (還原)，不是淨 - 費
        self.assertEqual(pm.get("realized_pnl_net").value, net)
        self.assertEqual(pm.get("realized_pnl_gross_before_fees").value, net + fees)
        # 全部資產負債守恆 → 若費用被重複扣除，殘差必不為 0
        self.assertEqual(pm.reconciliation.status, MetricStatus.OK)
        self.assertLessEqual(abs(pm.reconciliation.residual), D("0.01"))
        # 成交層級手續費 = 已平倉分攤 + 未平倉剩餘買進手續費
        cost = pm.breakdowns["cost_attribution"]
        self.assertLessEqual(abs(cost["commission_residual"]), D("0.01"))
        self.assertLessEqual(abs(cost["tax_residual"]), D("0.01"))
        # 逐項直接由成交紀錄加總
        fills = self.full.all_fills
        self.assertEqual(pm.get("slippage_cost_total").value, sum((f.slippage for f in fills), D("0")))
        self.assertEqual(pm.get("securities_transaction_tax").value, sum((f.tax for f in fills), D("0")))
        self.assertEqual(
            pm.get("buy_commission").value + pm.get("sell_commission").value,
            sum((f.commission for f in fills), D("0")),
        )
        # 手算 (建構交易): 淨 100 + 費 70 = 毛 170；淨損益本身不變
        t = _trade("100", 2)
        m = PerformanceAnalyzer(_cfg()).compute_trade_metrics([t])
        self.assertEqual(m["average_win"].value, D("100"))
        self.assertEqual(t.realized_pnl, D("100"))
        # 滑價為歸因揭露：淨損益 + 稅費 = 毛，與滑價無關
        self.assertIn("未再次扣除", pm.get("slippage_cost_total").note)
        # 方法論第 14 項：稅費佔成交金額
        tot = pm.get("total_traded_notional").value
        c_total = sum((f.commission + f.tax for f in fills), D("0"))
        self.assertEqual(pm.get("effective_fee_rate").value, c_total / tot)
        self.assertEqual(pm.get("commission_tax_cost_drag").value, c_total / self.full.initial_cash)

    def test_13_dividends_not_double_counted(self) -> None:
        pm = self.pm_full
        paid = sum((d.net_dividend for d in self.full.paid_dividends), D("0"))
        self.assertGreater(paid, D("0"))
        self.assertEqual(pm.get("dividend_income_paid").value, paid)
        # 報酬僅由 NAV 計算，不再加總股利
        self.assertEqual(
            pm.get("total_return").value, (self.full.final_nav - self.full.initial_cash) / self.full.initial_cash
        )
        self.assertNotEqual(
            pm.get("total_return").value,
            (self.full.final_nav - self.full.initial_cash + paid) / self.full.initial_cash,
        )
        # 對帳：股利只出現一次
        r = pm.reconciliation
        self.assertEqual(r.dividends_paid_net, paid)
        self.assertEqual(r.status, MetricStatus.OK)
        self.assertIn("不得與 NAV 報酬相加", pm.get("dividend_income_paid").note)

    def test_turnover_matches_manual_calculation(self) -> None:
        a = 252
        vals = []
        for log in self.full.daily_logs:
            traded = sum((f.notional for f in log.settled_fills), D("0"))
            vals.append(float(traded / (D(2) * log.nav_record.total_nav)))
        expected = sum(vals) / len(vals) * a
        self.assertAlmostEqual(self.pm_full.get("turnover").value, expected, places=12)

    def test_partial_fill_and_risk_rejection_metrics(self) -> None:
        pm = self.analyzer.analyze(_Scenarios.partial_fill())
        # 4000 股委託成交 2000：非「完全未成交」→ unfilled_ratio=0；股數短缺 = 1 - 2000/4000 = 0.5
        self.assertEqual(pm.get("unfilled_order_ratio").value, D("0"))
        self.assertEqual(pm.get("volume_shortfall_ratio").value, D("0.5"))
        rj = self.analyzer.analyze(_Scenarios.risk_rejected())
        self.assertEqual(rj.get("risk_rejection_ratio").value, D("1"))
        self.assertGreaterEqual(sum(rj.breakdowns["risk_rejections"]["by_code"].values()), 1)
        self.assertEqual(rj.get("fill_count").value, 0)
        self.assertEqual(rj.get("commission_tax_cost_drag").value, D("0"))  # 無成交 → 明確為 0 且說明

    def test_17_analysis_is_reproducible(self) -> None:
        a = PerformanceAnalyzer(AnalyzerConfig()).analyze(self.full)
        b = PerformanceAnalyzer(AnalyzerConfig()).analyze(self.full)
        self.assertEqual(json.dumps(a.to_dict(), sort_keys=True), json.dumps(b.to_dict(), sort_keys=True))


class TestReportAndLineage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.full = _Scenarios.full_cycle()
        cls.open = _Scenarios.open_position()
        cls.analyzer = PerformanceAnalyzer(AnalyzerConfig())

    def report(self, result, **kw) -> BacktestReport:
        pm = self.analyzer.analyze(result, benchmark=kw.get("benchmark"))
        return BacktestReport.build(result, pm, **kw)

    def test_15_synthetic_flag_preserved_in_report(self) -> None:
        rep = self.report(self.full)
        c = rep.content
        self.assertTrue(rep.is_synthetic_result)
        self.assertEqual(rep.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)
        self.assertFalse(rep.claims_real_historical_verified)
        lin = c["data_lineage"]
        self.assertTrue(lin["is_synthetic_flag"])
        self.assertTrue(lin["market_data"]["is_synthetic"])
        self.assertTrue(lin["predictions"]["is_synthetic"] or lin["source_statuses"]["predictions"] == "SYNTHETIC")
        self.assertEqual(lin["source_statuses"]["market_data"], "SYNTHETIC")
        self.assertEqual(lin["source_statuses"]["corporate_actions"], "SYNTHETIC")
        self.assertEqual(lin["source_statuses"]["benchmark"], "NOT_PROVIDED")
        self.assertIn("合成", c["conclusion_scope"])
        self.assertIn("不代表任何真實台股歷史績效", c["conclusion_scope"])
        # 模型與版本、回測起訖皆保留
        self.assertEqual(c["models_and_versions"]["models"][0]["model_name"], "qlib_lgbm_c24_fixture")
        self.assertEqual(c["models_and_versions"]["models"][0]["model_version"], "v1.0.0")
        self.assertEqual(c["models_and_versions"]["models"][0]["feature_version"], "alpha158_v1")
        self.assertEqual(c["models_and_versions"]["model_type"], "NOT_AVAILABLE")  # 消費紀錄未含 model_type，不虛構
        self.assertEqual(c["run"]["start_date"], self.full.start_date.isoformat())
        self.assertEqual(c["run"]["end_date"], self.full.end_date.isoformat())
        self.assertEqual(c["data_lineage"]["nav_integrity_status"], "COMPLETE")
        self.assertTrue(c["methodology"]["all_17_implemented"])
        self.assertTrue(any("benchmark" in u for u in c["unverified_items"]))
        # 顯式提供 model_type 時被保留
        meta = _rt._make_prediction_batch(
            batch_id="X", decision_date=_rt.dt.date(2025, 1, 6), target_trade_date=_rt.dt.date(2025, 1, 7),
            scores_and_ranks=[("2330", 0.9, 1)],
        ).metadata
        rep2 = self.report(self.full, model_metadata=[meta])
        self.assertEqual(rep2.content["models_and_versions"]["model_type"], [meta.model_type])
        # 即使提供「完整驗證聲明」，合成資料仍不得被標記為已驗證
        att = LineageAttestation(True, True, True, True, ("fake evidence",))
        rep3 = self.report(self.full, attestation=att)
        self.assertTrue(rep3.is_synthetic_result)
        self.assertFalse(rep3.claims_real_historical_verified)

    def test_16_no_pit_evidence_never_claims_verified(self) -> None:
        real = _as_real(self.open)
        # (a) 非合成但無任何驗證證據 → NOT_VERIFIED
        rep = self.report(real)
        lin = rep.content["data_lineage"]
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertEqual(lin["source_statuses"]["market_data"], LineageStatus.REAL_UNVERIFIED.value)
        self.assertEqual(lin["source_statuses"]["corporate_actions"], LineageStatus.NO_EVENTS_RECORDED.value)
        self.assertEqual(lin["pit_verification_status"], "NOT_VERIFIED")
        self.assertTrue(any("market_data" in u for u in rep.content["unverified_items"]))
        self.assertTrue(any("lineage_attestation_evidence" in u for u in rep.content["unverified_items"]))
        self.assertIn("不得標記為「真實歷史回測已驗證」", rep.content["conclusion_scope"])
        # (b) 部分證據 (缺公司行動覆蓋度、缺 Benchmark) → 仍 NOT_VERIFIED
        part = LineageAttestation(market_data_pit_verified=True, calendar_verified=True, evidence_notes=("partial",))
        self.assertFalse(self.report(real, attestation=part).claims_real_historical_verified)
        # (c) 即使證據齊全，Benchmark 缺失仍不得宣稱已驗證
        full_att = LineageAttestation(True, True, True, True, ("TWSE 官方日曆核對", "FinMind 發布時間核對"))
        self.assertFalse(self.report(real, attestation=full_att).claims_real_historical_verified)
        # (d) Mock 模型：即使一切聲明齊全，仍為合成測試結果
        mock_real = _as_real(self.open, mock=True)
        rep_mock = self.report(mock_real, attestation=full_att)
        self.assertEqual(rep_mock.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)
        # (e) 預測缺 trained_cutoff_date (Walk-Forward 無證據) → 預測來源 REAL_UNVERIFIED
        self.assertEqual(self.report(real).content["data_lineage"]["source_statuses"]["predictions"], "REAL_UNVERIFIED")
        # (f) 全部齊備 (含真實 Benchmark 與預測訓練截止日)，但無獨立驗證器時，依保守原則仍為 REAL_HISTORICAL_NOT_VERIFIED (FINDING-02 修補)
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in real.consumed_predictions)
        real_ok = dataclasses.replace(real, consumed_predictions=recs)
        nav_dates = [r.valuation_date for r in real_ok.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        rep_unverified = self.report(real_ok, attestation=full_att, benchmark=bench)
        self.assertEqual(rep_unverified.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep_unverified.claims_real_historical_verified)
        self.assertTrue(any("independent_evidence_verifier" in u for u in rep_unverified.content["unverified_items"]))

        # (f-2) 自製／Mock 獨立證據驗證器缺少官方公鑰授權時，絕不可授予 REAL_HISTORICAL_PIT_VERIFIED (C-2.5.2.1 閘門)
        mock_v = MockIndependentEvidenceVerifier(is_verified=True, verifier_name="MockTWSEVerifier")
        rep_mock_v = self.report(real_ok, attestation=full_att, benchmark=bench, evidence_verifier=mock_v)
        self.assertEqual(rep_mock_v.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep_mock_v.claims_real_historical_verified)
        self.assertTrue(any("TEST_ONLY_EVIDENCE" in u or "LACKS_OFFICIAL_AUTHORITY" in u for u in rep_mock_v.content["unverified_items"]))

        # (f-3) 配置具備官方 Ed25519 簽章公證之驗證器且驗證通過時，始授予 REAL_HISTORICAL_PIT_VERIFIED
        keypair = generate_ed25519_keypair("OFFICIAL_TWSE_SIGNER", KeyAuthorityLevel.OFFICIAL)
        store, _ = _create_test_official_store(keypair)
        manifest_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:mkt_hash",
            "CANDIDATE_PRED_FEED": "sha256:pred_hash",
            "BENCH_SRC": "sha256:bench_hash",
        }
        h_body = json.dumps(manifest_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest_sig = keypair.sign(h_body).hex()
        official_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=manifest_hashes,
            trusted_key_store=store,
            manifest_signer_id=keypair.signer_id,
            manifest_signature_hex=manifest_sig,
            verifier_name="OfficialTWSEVerifier",
        )
        rep_ok = self.report(real_ok, attestation=full_att, benchmark=bench, evidence_verifier=official_verifier)
        self.assertEqual(rep_ok.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)
        self.assertTrue(rep_ok.claims_real_historical_verified)

        # (g) 一旦 NAV 不完整，立即降級 (即使配置了官方公證之獨立驗證器)
        recs2 = list(real_ok.nav_history)
        recs2[-1] = dataclasses.replace(recs2[-1], total_nav=None, market_value=None, is_complete=False, is_official_nav=False)
        broken = dataclasses.replace(real_ok, nav_history=tuple(recs2))
        pm = PerformanceAnalyzer(AnalyzerConfig(incomplete_nav_policy=IncompleteNavPolicy.MARK_INCOMPLETE)).analyze(broken, benchmark=bench)
        rep_b = BacktestReport.build(broken, pm, attestation=full_att, benchmark=bench, evidence_verifier=official_verifier)
        self.assertFalse(rep_b.claims_real_historical_verified)
        self.assertEqual(rep_b.content["data_lineage"]["nav_integrity_status"], "INCOMPLETE")
        self.assertEqual(rep_b.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)

    def test_17_report_serialization_and_reproducibility(self) -> None:
        rep = self.report(self.full)
        text = rep.to_json()
        data = json.loads(text)  # 合法 JSON (無 NaN/Inf)
        self.assertEqual(data["content_hash"], rep.content_hash)
        self.assertEqual(rep.to_json(), text)  # 確定性
        # 還原後相等並驗證雜湊
        back = BacktestReport.from_json(text)
        self.assertEqual(back, rep)
        self.assertEqual(back.to_json(), text)
        # 由同一結果重新分析與建構 → 相同雜湊 (可重現)
        self.assertEqual(self.report(self.full).content_hash, rep.content_hash)
        # 竄改任何數值 → 雜湊不符
        tampered = json.loads(text)
        tampered["metrics"]["return_and_risk"]["total_return"]["value"] = "0.99"
        from tw_quant.backtest import BacktestMetricsError

        with self.assertRaises(BacktestMetricsError):
            BacktestReport.from_json(json.dumps(tampered))
        # 必要區塊皆在
        for key in (
            "run", "models_and_versions", "data_lineage", "metrics", "pnl_reconciliation", "breakdowns",
            "benchmark_comparison_status", "methodology", "data_quality_warnings", "unverified_items",
            "conclusion_scope", "limitations",
        ):
            self.assertIn(key, data)
        # 缺資料以狀態表示，而非假數值
        self.assertEqual(data["metrics"]["benchmark_relative"]["tracking_error"]["status"], "NOT_AVAILABLE")
        self.assertIsNone(data["metrics"]["benchmark_relative"]["tracking_error"]["value"])
        self.assertEqual(data["benchmark_comparison_status"], "NOT_AVAILABLE")

    def test_report_with_synthetic_benchmark_keeps_flag(self) -> None:
        nav_dates = [r.valuation_date for r in self.open.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=True)
        rep = self.report(self.open, benchmark=bench)
        b = rep.content["data_lineage"]["benchmark"]
        self.assertTrue(b["is_synthetic"])
        self.assertTrue(b["includes_dividends"])
        self.assertEqual(rep.content["benchmark_comparison_status"], "SYNTHETIC_BENCHMARK")
        self.assertTrue(rep.is_synthetic_result)



class TestPitCausalVerificationAndLineageRemediation(unittest.TestCase):
    """C-2.5.1 PIT 防前視與資料血統驗證修補測試 (FINDING-01 & FINDING-02)."""

    def setUp(self) -> None:
        self.d1 = dt.date(2025, 1, 6)
        self.d2 = dt.date(2025, 1, 7)
        self.base_rec = PredictionConsumptionRecord(
            batch_id="BATCH-001",
            decision_date=self.d1,
            target_trade_date=self.d2,
            decision_cutoff_time=dt.datetime.combine(self.d1, dt.time(18, 0, 0), tzinfo=TAIPEI_TZ),
            data_cutoff_time=dt.datetime.combine(self.d1, dt.time(16, 0, 0), tzinfo=TAIPEI_TZ),
            generated_at=dt.datetime.combine(self.d1, dt.time(17, 0, 0), tzinfo=TAIPEI_TZ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            model_name="test_model",
            model_version="v1.0",
            feature_version="f1.0",
            trained_cutoff_date=dt.date(2025, 1, 3),  # 早於 decision_date 2025-01-06
            is_mock=False,
            is_synthetic=False,
            window_id="W1",
            prediction_count=10,
        )

    # ─────────────────────────────────────────────────────────────
    # FINDING-01: PIT 因果驗證測試 (A ~ F)
    # ─────────────────────────────────────────────────────────────

    def test_finding01_A_trained_cutoff_before_decision_date_conforms(self) -> None:
        """A. 訓練截止時間早於決策時間：符合因果條件。"""
        rec = dataclasses.replace(self.base_rec, trained_cutoff_date=dt.date(2025, 1, 5))
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_finding01_B_trained_cutoff_after_decision_date_rejected(self) -> None:
        """B. 訓練截止時間晚於決策時間：必須拒絕。"""
        rec = dataclasses.replace(self.base_rec, trained_cutoff_date=dt.date(2025, 1, 7))
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("晚於決策日", reason)
        self.assertIn("未來資料洩漏", reason)

    def test_finding01_C_trained_cutoff_equals_decision_date_conservatively_rejected(self) -> None:
        """C. 訓練截止時間等於決策時間：依正式資料契約判定；若無法證明可用，保守拒絕。"""
        rec = dataclasses.replace(self.base_rec, trained_cutoff_date=self.d1)
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("等於決策日", reason)
        self.assertIn("保守原則拒絕", reason)

    def test_finding01_D_available_at_before_generated_at_rejected(self) -> None:
        """D. available_at 早於 generated_at：必須拒絕。"""
        rec = dataclasses.replace(
            self.base_rec,
            generated_at=dt.datetime.combine(self.d1, dt.time(17, 30, 0), tzinfo=TAIPEI_TZ),
            available_at=dt.datetime.combine(self.d1, dt.time(17, 0, 0), tzinfo=TAIPEI_TZ),
        )
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("早於 generated_at", reason)

    def test_finding01_E_missing_required_timestamps_not_attested(self) -> None:
        """E. 缺少必要時間戳：不得標記為 REAL_PIT_ATTESTED。"""
        for field_name in ("trained_cutoff_date", "available_at", "generated_at", "decision_cutoff_time", "decision_date"):
            with self.subTest(missing_field=field_name):
                rec = dataclasses.replace(self.base_rec, **{field_name: None})
                ok, reason = verify_prediction_consumption_pit(rec)
                self.assertFalse(ok)
                self.assertIn(f"缺少 {field_name}", reason)

    def test_finding01_F_timezone_naive_and_type_mismatch_handled(self) -> None:
        """F. 時區不一致或日期／時間型別不一致：必須有明確、可測試的處理方式。"""
        # (1) Naive datetime (缺少時區)
        naive_dt = dt.datetime(2025, 1, 6, 17, 0, 0)
        rec_naive = dataclasses.replace(self.base_rec, generated_at=naive_dt)
        ok1, reason1 = verify_prediction_consumption_pit(rec_naive)
        self.assertFalse(ok1)
        self.assertIn("缺少時區資訊", reason1)

        # (2) decision_date 與 decision_cutoff_time 台北日期不一致
        rec_mismatch = dataclasses.replace(self.base_rec, decision_date=dt.date(2025, 1, 8))
        ok2, reason2 = verify_prediction_consumption_pit(rec_mismatch)
        self.assertFalse(ok2)
        self.assertIn("不一致", reason2)

        # (3) available_at 晚於決策截止時間
        rec_late = dataclasses.replace(
            self.base_rec,
            available_at=dt.datetime.combine(self.d1, dt.time(18, 30, 0), tzinfo=TAIPEI_TZ),
        )
        ok3, reason3 = verify_prediction_consumption_pit(rec_late)
        self.assertFalse(ok3)
        self.assertIn("晚於決策截止時間", reason3)

    # ─────────────────────────────────────────────────────────────
    # FINDING-02: 資料血統驗證測試 (A ~ F)
    # ─────────────────────────────────────────────────────────────

    def test_finding02_A_arbitrary_evidence_notes_insufficient(self) -> None:
        """A. 任意非空 evidence_notes 不足以證明真實資料。"""
        real = _as_real(_Scenarios.open_position())
        att = LineageAttestation(
            market_data_pit_verified=True,
            calendar_verified=True,
            corporate_actions_coverage_verified=True,
            benchmark_verified=True,
            evidence_notes=("arbitrary note string from untrusted source", "developer claim"),
        )
        nav_dates = [r.valuation_date for r in real.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        rep = BacktestReport.build(real, PerformanceAnalyzer().analyze(real, benchmark=bench), attestation=att, benchmark=bench)
        # 無獨立驗證器時，任意文字備註不足以獲得真實歷史驗證結論
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertTrue(any("independent_evidence_verifier" in u for u in rep.content["unverified_items"]))

    def test_finding02_B_caller_boolean_attestation_insufficient(self) -> None:
        """B. 呼叫端自行將布林值設為 True，不足以證明真實資料。"""
        real = _as_real(_Scenarios.open_position())
        att = LineageAttestation(
            market_data_pit_verified=True,
            calendar_verified=True,
            corporate_actions_coverage_verified=True,
            benchmark_verified=True,
            evidence_notes=(),
        )
        nav_dates = [r.valuation_date for r in real.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        rep = BacktestReport.build(real, PerformanceAnalyzer().analyze(real, benchmark=bench), attestation=att, benchmark=bench)
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        # 報告明文化四維度框架
        vf = rep.content["data_lineage"]["verification_framework"]
        self.assertIn("呼叫端主觀聲明", vf["caller_attestation"]["disclaimer"])

    def test_finding02_C_synthetic_or_mock_never_promoted(self) -> None:
        """C. SYNTHETIC 或 Mock 資料永遠不能被提升為真實歷史驗證。"""
        # (1) 原始合成結果
        synth = _Scenarios.open_position()
        att = LineageAttestation(True, True, True, True, ("claim",))
        mock_verifier = MockIndependentEvidenceVerifier(is_verified=True)
        rep_synth = BacktestReport.build(
            synth, PerformanceAnalyzer().analyze(synth), attestation=att, evidence_verifier=mock_verifier
        )
        self.assertEqual(rep_synth.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)
        self.assertTrue(rep_synth.is_synthetic_result)
        self.assertFalse(rep_synth.claims_real_historical_verified)

        # (2) 非合成包裝但含 Mock 模型
        real_mock = _as_real(synth, mock=True)
        rep_mock = BacktestReport.build(
            real_mock, PerformanceAnalyzer().analyze(real_mock), attestation=att, evidence_verifier=mock_verifier
        )
        self.assertEqual(rep_mock.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)
        self.assertFalse(rep_mock.claims_real_historical_verified)

    def test_finding02_D_sha256_protects_content_not_data_truth(self) -> None:
        """D. SHA-256 一致只表示內容一致，不表示來源真實。"""
        real = _as_real(_Scenarios.open_position())
        rep = BacktestReport.build(real, PerformanceAnalyzer().analyze(real))
        # 雜湊存在且能驗證反序列化未被竄改
        self.assertTrue(rep.content_hash)
        json_str = rep.to_json()
        restored = BacktestReport.from_json(json_str)
        self.assertEqual(restored.content_hash, rep.content_hash)

        # 明確聲明雜湊範圍僅限報告序列化完整性
        di = rep.content["data_lineage"]["verification_framework"]["data_integrity"]
        self.assertEqual(di["hash_scope"], "REPORT_CONTENT_INTEGRITY_ONLY")
        self.assertIn("不單獨證明外部資料來源之真實性", di["disclaimer"])

    def test_finding02_E_missing_independent_verifier_is_conservative(self) -> None:
        """E. 缺少獨立證據驗證器時，最終結論必須保守。"""
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(_Scenarios.open_position()).consumed_predictions)
        real_ok = dataclasses.replace(_as_real(_Scenarios.open_position()), consumed_predictions=recs)
        nav_dates = [r.valuation_date for r in real_ok.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        full_att = LineageAttestation(True, True, True, True, ("note1", "note2"))

        # 無 evidence_verifier 傳入
        rep = BacktestReport.build(real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench), attestation=full_att, benchmark=bench, evidence_verifier=None)
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertEqual(
            rep.content["data_lineage"]["verification_framework"]["independent_verification"]["status"],
            "NOT_CONFIGURED",
        )

    def test_finding02_F_full_evidence_verification_pipeline(self) -> None:
        """F. 完整的證據驗證流程只能在實際存在且可測試的情況下標記已驗證。"""
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(_Scenarios.open_position()).consumed_predictions)
        real_ok = dataclasses.replace(_as_real(_Scenarios.open_position()), consumed_predictions=recs)
        nav_dates = [r.valuation_date for r in real_ok.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        full_att = LineageAttestation(True, True, True, True, ("TWSE_CALENDAR_AUDIT_20250101", "FINMIND_MARKET_AUDIT_20250101"))

        # (1) 具備已註冊指紋清單與受信任官方 Ed25519 簽章之真實獨立驗證器：成功通過驗證 (C-2.5.2.1 修補)
        keypair = generate_ed25519_keypair("TWSEOfficialSigner", KeyAuthorityLevel.OFFICIAL)
        store, _ = _create_test_official_store(keypair)

        manifest_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:market_data_manifest_approved_hash",
            "CANDIDATE_PRED_FEED": "sha256:prediction_manifest_approved_hash",
            "BENCH_SRC": "sha256:benchmark_manifest_approved_hash",
        }
        h_body = json.dumps(manifest_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest_sig = keypair.sign(h_body).hex()

        checksum_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=manifest_hashes,
            trusted_key_store=store,
            manifest_signer_id=keypair.signer_id,
            manifest_signature_hex=manifest_sig,
            verifier_name="TWSEOfficialDatasetManifestVerifier",
        )
        rep_verified = BacktestReport.build(
            real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
            attestation=full_att, benchmark=bench, evidence_verifier=checksum_verifier
        )
        self.assertEqual(rep_verified.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)
        self.assertTrue(rep_verified.claims_real_historical_verified)
        iv = rep_verified.content["data_lineage"]["verification_framework"]["independent_verification"]
        self.assertTrue(iv["is_verified"])
        self.assertEqual(iv["verifier_name"], "TWSEOfficialDatasetManifestVerifier")
        self.assertEqual(iv["status"], "OFFICIAL_VERIFIED")

        # (2) 驗證器指紋缺少某項來源：驗證失敗，保守降級為 NOT_VERIFIED
        incomplete_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:some_hash",
            # 缺少 CANDIDATE_PRED_FEED 與 BENCH_SRC
        }
        sig_inc = keypair.sign(json.dumps(incomplete_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")).hex()
        incomplete_checksum_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=incomplete_hashes,
            trusted_key_store=store,
            manifest_signer_id=keypair.signer_id,
            manifest_signature_hex=sig_inc,
            verifier_name="IncompleteManifestVerifier",
        )
        rep_failed = BacktestReport.build(
            real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
            attestation=full_att, benchmark=bench, evidence_verifier=incomplete_checksum_verifier
        )
        self.assertEqual(rep_failed.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep_failed.claims_real_historical_verified)
        iv_failed = rep_failed.content["data_lineage"]["verification_framework"]["independent_verification"]
        self.assertFalse(iv_failed["is_verified"])
        self.assertEqual(iv_failed["status"], "FAILED")
        self.assertTrue(any("缺少已登記之公證指紋雜湊" in r for r in iv_failed["failure_reasons"]))

    # ─────────────────────────────────────────────────────────────
    # C-2.5.2 修補回歸測試: R01, R05, R04, R02, R06
    # ─────────────────────────────────────────────────────────────

    # ── R01: 跨午夜時區轉換與 PIT 因果 ──

    def test_r01_cross_midnight_utc_bypass_rejected(self) -> None:
        """R01: UTC 跨午夜時間戳在台北時間屬於決策日清晨，必須依保守原則拒絕 (重現並修補案例 1)."""
        d1 = dt.date(2025, 1, 6)
        # 2025-01-05 17:00:00 UTC 等同於 2025-01-06 01:00:00+08:00 (台北時間即決策日清晨)
        trained_dt_utc = dt.datetime(2025, 1, 5, 17, 0, 0, tzinfo=dt.timezone.utc)

        rec = dataclasses.replace(
            self.base_rec,
            decision_date=d1,
            trained_cutoff_date=trained_dt_utc,
        )
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok, "UTC 跨午夜時間戳在台北時間為 2025-01-06，等於決策日，必須被拒絕")
        self.assertIn("trained_cutoff_date (2025-01-06) 等於決策日 (2025-01-06)", reason)
        self.assertIn("保守原則拒絕", reason)

    def test_r01_cross_midnight_utc_valid_prior_day_accepted(self) -> None:
        """R01: UTC 時間戳在轉換為台北時間後若確實早於決策日，正常通過."""
        d1 = dt.date(2025, 1, 6)
        # 2025-01-05 15:59:00 UTC 等同於 2025-01-05 23:59:00+08:00 (台北時間仍為 1 月 5 日)
        trained_dt_utc = dt.datetime(2025, 1, 5, 15, 59, 0, tzinfo=dt.timezone.utc)

        rec = dataclasses.replace(
            self.base_rec,
            decision_date=d1,
            trained_cutoff_date=trained_dt_utc,
        )
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_r01_naive_trained_cutoff_date_rejected(self) -> None:
        """R01: trained_cutoff_date 為 naive datetime 時必須被拒絕."""
        naive_dt = dt.datetime(2025, 1, 5, 12, 0, 0)
        rec = dataclasses.replace(self.base_rec, trained_cutoff_date=naive_dt)
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("缺少時區資訊 (naive datetime)", reason)

    def test_r01_naive_decision_date_rejected(self) -> None:
        """R01: decision_date 為 naive datetime 時必須被拒絕."""
        naive_dt = dt.datetime(2025, 1, 6, 18, 0, 0)
        rec = dataclasses.replace(self.base_rec, decision_date=naive_dt)
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("缺少時區資訊 (naive datetime)", reason)

    # ── R05: data_cutoff_time 必要性與時序因果 ──

    def test_r05_missing_data_cutoff_time_rejected(self) -> None:
        """R05: data_cutoff_time 為 None 時必須被強制拒絕."""
        rec = dataclasses.replace(self.base_rec, data_cutoff_time=None)
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("缺少 data_cutoff_time 時間戳", reason)

    def test_r05_naive_data_cutoff_time_rejected(self) -> None:
        """R05: data_cutoff_time 為 naive datetime 時必須被拒絕."""
        naive_dt = dt.datetime(2025, 1, 6, 16, 0, 0)
        rec = dataclasses.replace(self.base_rec, data_cutoff_time=naive_dt)
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("data_cutoff_time", reason)
        self.assertIn("缺少時區資訊 (naive datetime", reason)

    def test_r05_data_cutoff_time_after_generated_at_rejected(self) -> None:
        """R05: data_cutoff_time 晚於 generated_at 時必須拒絕."""
        rec = dataclasses.replace(
            self.base_rec,
            generated_at=dt.datetime.combine(self.d1, dt.time(16, 0, 0), tzinfo=TAIPEI_TZ),
            data_cutoff_time=dt.datetime.combine(self.d1, dt.time(16, 30, 0), tzinfo=TAIPEI_TZ),
        )
        ok, reason = verify_prediction_consumption_pit(rec)
        self.assertFalse(ok)
        self.assertIn("晚於 generated_at", reason)

    def test_r05_missing_data_cutoff_gates_backtest_report_verdict(self) -> None:
        """R05: 當預測紀錄缺少 data_cutoff_time 時，BacktestReport 最終結論強制降級."""
        base = _as_real(_Scenarios.open_position())
        bad_recs = tuple(dataclasses.replace(r, data_cutoff_time=None) for r in base.consumed_predictions)
        real_bad = dataclasses.replace(base, consumed_predictions=bad_recs)

        rep = BacktestReport.build(real_bad, PerformanceAnalyzer().analyze(real_bad))
        self.assertFalse(rep.content["models_and_versions"]["all_predictions_pit_passed"])
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertTrue(any("缺少 data_cutoff_time" in w for w in rep.content["data_quality_warnings"]))

    # ── R04: Mock 驗證器隔離與正式環境防誤用 ──

    def test_r04_production_does_not_export_mock_verifier(self) -> None:
        """R04: tw_quant.backtest 生產模組不得匯出 MockIndependentEvidenceVerifier."""
        import tw_quant.backtest as tb
        self.assertFalse(hasattr(tb, "MockIndependentEvidenceVerifier"))
        self.assertNotIn("MockIndependentEvidenceVerifier", tb.__all__)

    def test_r04_missing_verifier_cannot_produce_real_verified(self) -> None:
        """R04: 未配置獨立證據驗證器時，絕不能輸出 REAL_HISTORICAL_PIT_VERIFIED."""
        real = _as_real(_Scenarios.open_position())
        att = LineageAttestation(True, True, True, True, ("full note",))
        rep = BacktestReport.build(
            real, PerformanceAnalyzer().analyze(real), attestation=att, evidence_verifier=None
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertEqual(
            rep.content["data_lineage"]["verification_framework"]["independent_verification"]["status"],
            "NOT_CONFIGURED",
        )

    # ── R02: 指紋清單信任根與實體檔案 SHA-256 校驗 ──

    def test_r02_untrusted_caller_manifest_hashes_rejected(self) -> None:
        """R02: 呼叫端自行於記憶體傳入未授權指紋清單，必須拒絕 (重現並修補案例 2)."""
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(_Scenarios.open_position()).consumed_predictions)
        real_ok = dataclasses.replace(_as_real(_Scenarios.open_position()), consumed_predictions=recs)
        nav_dates = [r.valuation_date for r in real_ok.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        full_att = LineageAttestation(True, True, True, True, ("note1", "note2"))

        # 呼叫端傳入任意偽造 hash 且未取得官方授權 (is_trusted_authority=False 預設)
        fake_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes={
                "CANDIDATE_MARKET_FEED": "sha256:arbitrary_fake_hash",
                "CANDIDATE_PRED_FEED": "sha256:arbitrary_fake_hash",
                "BENCH_SRC": "sha256:arbitrary_fake_hash",
            },
            is_trusted_authority=False,
        )
        rep = BacktestReport.build(
            real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
            attestation=full_att, benchmark=bench, evidence_verifier=fake_verifier
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        iv = rep.content["data_lineage"]["verification_framework"]["independent_verification"]
        self.assertFalse(iv["is_verified"])
        self.assertTrue(any("UNTRUSTED_CALLER_MANIFEST" in r for r in iv["failure_reasons"]))

    def test_r02_file_digest_verifier_physical_files(self) -> None:
        """R02: FileDigestEvidenceVerifier 針對磁碟實體檔案讀取計算真實 SHA-256 摘要."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mkt_file = os.path.join(tmpdir, "market_data.parquet")
            pred_file = os.path.join(tmpdir, "predictions.parquet")
            bench_file = os.path.join(tmpdir, "benchmark.csv")

            with open(mkt_file, "wb") as f:
                f.write(b"REAL_MARKET_CONTENT_TWSE_2025")
            with open(pred_file, "wb") as f:
                f.write(b"REAL_PREDICTION_CONTENT_DOUBLE_ENSEMBLE")
            with open(bench_file, "wb") as f:
                f.write(b"REAL_BENCHMARK_CONTENT_TAIEX")

            mkt_hash = f"sha256:{hashlib.sha256(b'REAL_MARKET_CONTENT_TWSE_2025').hexdigest()}"
            pred_hash = f"sha256:{hashlib.sha256(b'REAL_PREDICTION_CONTENT_DOUBLE_ENSEMBLE').hexdigest()}"
            bench_hash = f"sha256:{hashlib.sha256(b'REAL_BENCHMARK_CONTENT_TAIEX').hexdigest()}"

            recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(_Scenarios.open_position()).consumed_predictions)
            real_ok = dataclasses.replace(_as_real(_Scenarios.open_position()), consumed_predictions=recs)
            nav_dates = [r.valuation_date for r in real_ok.nav_history]
            bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
            full_att = LineageAttestation(True, True, True, True, ("TWSE_AUDIT_OK",))

            kp = generate_ed25519_keypair("TWSE_ROOT_SIGNER", KeyAuthorityLevel.OFFICIAL)
            store, _ = _create_test_official_store(kp)
            h_dict = {
                "CANDIDATE_MARKET_FEED": mkt_hash,
                "CANDIDATE_PRED_FEED": pred_hash,
                "BENCH_SRC": bench_hash,
            }
            h_body = json.dumps(h_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
            sig_hex = kp.sign(h_body).hex()

            # Case A: 正確檔案、符合雜湊、且具備官方公鑰簽章 -> VERIFIED
            valid_file_verifier = FileDigestEvidenceVerifier(
                source_file_paths={
                    "CANDIDATE_MARKET_FEED": mkt_file,
                    "CANDIDATE_PRED_FEED": pred_file,
                    "BENCH_SRC": bench_file,
                },
                expected_file_hashes=h_dict,
                trusted_key_store=store,
                manifest_signer_id=kp.signer_id,
                manifest_signature_hex=sig_hex,
                verifier_name="TWSERootFileVerifier",
            )
            rep_a = BacktestReport.build(
                real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
                attestation=full_att, benchmark=bench, evidence_verifier=valid_file_verifier
            )
            self.assertEqual(rep_a.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)
            self.assertTrue(rep_a.claims_real_historical_verified)

            # Case B: 實體檔案內容被篡改 1 byte -> 雜湊不符拒絕
            with open(mkt_file, "wb") as f:
                f.write(b"REAL_MARKET_CONTENT_TWSE_2025_TAMPERED")
            rep_b = BacktestReport.build(
                real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
                attestation=full_att, benchmark=bench, evidence_verifier=valid_file_verifier
            )
            self.assertEqual(rep_b.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
            iv_b = rep_b.content["data_lineage"]["verification_framework"]["independent_verification"]
            self.assertFalse(iv_b["is_verified"])
            self.assertTrue(any("SHA-256 不符" in r for r in iv_b["failure_reasons"]))

            # Case C: 檔案不存在 -> 拒絕
            missing_verifier = FileDigestEvidenceVerifier(
                source_file_paths={
                    "CANDIDATE_MARKET_FEED": os.path.join(tmpdir, "non_existent.parquet"),
                    "CANDIDATE_PRED_FEED": pred_file,
                    "BENCH_SRC": bench_file,
                },
                expected_file_hashes=h_dict,
                trusted_key_store=store,
                manifest_signer_id=kp.signer_id,
                manifest_signature_hex=sig_hex,
                verifier_name="TWSERootFileVerifier",
            )
            rep_c = BacktestReport.build(
                real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
                attestation=full_att, benchmark=bench, evidence_verifier=missing_verifier
            )
            self.assertEqual(rep_c.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
            iv_c = rep_c.content["data_lineage"]["verification_framework"]["independent_verification"]
            self.assertTrue(any("實體檔案不存在" in r for r in iv_c["failure_reasons"]))

            # Case D: 未授權信任根 (is_trusted_root=False) -> 拒絕
            untrusted_file_verifier = FileDigestEvidenceVerifier(
                source_file_paths={
                    "CANDIDATE_MARKET_FEED": mkt_file,
                    "CANDIDATE_PRED_FEED": pred_file,
                    "BENCH_SRC": bench_file,
                },
                expected_file_hashes={
                    "CANDIDATE_MARKET_FEED": mkt_hash,
                    "CANDIDATE_PRED_FEED": pred_hash,
                    "BENCH_SRC": bench_hash,
                },
                is_trusted_root=False,
            )
            rep_d = BacktestReport.build(
                real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
                attestation=full_att, benchmark=bench, evidence_verifier=untrusted_file_verifier
            )
            self.assertEqual(rep_d.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
            iv_d = rep_d.content["data_lineage"]["verification_framework"]["independent_verification"]
            self.assertTrue(any("UNTRUSTED_MANIFEST_ROOT" in r for r in iv_d["failure_reasons"]))

    # ── R06: 反序列化信任邊界與防篡改 ──

    def test_r06_tampered_json_unkeyed_sha256_downgraded(self) -> None:
        """R06: 攻擊者篡改 JSON 數值並重新計算無密鑰 SHA-256，反序列化強制降級 (重現並修補案例 3)."""
        synth = _Scenarios.open_position()
        rep = BacktestReport.build(synth, PerformanceAnalyzer().analyze(synth))
        self.assertEqual(rep.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)

        # 模擬攻擊者惡意修改 JSON 並自行重新計算 SHA-256
        data = json.loads(rep.to_json())
        data["data_lineage"]["validation_verdict"] = "REAL_HISTORICAL_PIT_VERIFIED"
        data["data_lineage"]["claims_real_historical_verified"] = True
        del data["content_hash"]

        body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        data["content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        tampered_json = json.dumps(data)

        # 透過 from_json 載入：由於缺少受信任 HMAC 數位簽章，強制保守降級為 NOT_VERIFIED
        restored = BacktestReport.from_json(tampered_json)
        self.assertEqual(
            restored.validation_verdict,
            ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED,
            "無密鑰 SHA-256 重算篡改者絕不可維持或提升為 REAL_HISTORICAL_PIT_VERIFIED",
        )
        self.assertFalse(restored.claims_real_historical_verified)
        self.assertTrue(
            any("DESERIALIZATION_TRUST" in w for w in restored.content["data_quality_warnings"]),
            "必須包含反序列化信任降級審計警告",
        )
        self.assertTrue(
            any("deserialization_unauthenticated" in u for u in restored.content["unverified_items"])
        )

    def test_r06_tampered_json_strict_mode_raises(self) -> None:
        """R06: 嚴格模式下，缺少簽章之宣稱已驗證報告反序列化直接拋出例外."""
        synth = _Scenarios.open_position()
        rep = BacktestReport.build(synth, PerformanceAnalyzer().analyze(synth))

        data = json.loads(rep.to_json())
        data["data_lineage"]["validation_verdict"] = "REAL_HISTORICAL_PIT_VERIFIED"
        data["data_lineage"]["claims_real_historical_verified"] = True
        del data["content_hash"]

        body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        data["content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        tampered_json = json.dumps(data)

        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(tampered_json, strict_reject_unverified_load=True)
        self.assertIn("缺少受信任之數位簽章", str(ctx.exception))

    def test_r06_signed_report_roundtrip(self) -> None:
        """R06: 具備合法 Ed25519 官方數位簽章且於受信任公鑰庫中登記時，反序列化驗證保留 (C-2.5.2.1 修補)."""
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(_Scenarios.open_position()).consumed_predictions)
        real_ok = dataclasses.replace(_as_real(_Scenarios.open_position()), consumed_predictions=recs)
        nav_dates = [r.valuation_date for r in real_ok.nav_history]
        bench = _bench(nav_dates, ["101", "102"], nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        full_att = LineageAttestation(True, True, True, True, ("TWSE_AUDIT_2025",))

        m_keypair = generate_ed25519_keypair("TWSE_MANIFEST_SIGNER", KeyAuthorityLevel.OFFICIAL)
        rep_signer = generate_ed25519_keypair("PROD_OFFICIAL_SIGNER", KeyAuthorityLevel.OFFICIAL)
        store, _ = _create_test_official_store(m_keypair, rep_signer)
        manifest_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:market_hash",
            "CANDIDATE_PRED_FEED": "sha256:pred_hash",
            "BENCH_SRC": "sha256:bench_hash",
        }
        h_body = json.dumps(manifest_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest_sig = m_keypair.sign(h_body).hex()

        checksum_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=manifest_hashes,
            trusted_key_store=store,
            manifest_signer_id=m_keypair.signer_id,
            manifest_signature_hex=manifest_sig,
            verifier_name="TWSEVerifier",
        )
        rep = BacktestReport.build(
            real_ok, PerformanceAnalyzer().analyze(real_ok, benchmark=bench),
            attestation=full_att, benchmark=bench, evidence_verifier=checksum_verifier
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)

        # 簽署端使用官方 Ed25519 金鑰對簽名
        rep.sign(rep_signer)
        signed_json = rep.to_json()

        # (1) 以受信任公鑰庫還原 -> 保留 REAL_HISTORICAL_PIT_VERIFIED
        restored_valid = BacktestReport.from_json(
            signed_json,
            trusted_key_store=store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
        )
        self.assertEqual(restored_valid.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)
        self.assertTrue(restored_valid.claims_real_historical_verified)

        # (2) 以未登記該簽署者之空公鑰庫還原 -> 降級為 REAL_HISTORICAL_NOT_VERIFIED
        empty_store = TrustedPublicKeyStore()
        restored_invalid = BacktestReport.from_json(
            signed_json,
            trusted_key_store=empty_store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
        )
        self.assertEqual(restored_invalid.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored_invalid.claims_real_historical_verified)


# ═════════════════════════════════════════════════════════════════
# C-2.5.2.1 信任根與非對稱簽章防偽獨立驗收測試集
# ═════════════════════════════════════════════════════════════════


class TestC2521TrustRootAndAsymmetricSignatureSecurity(unittest.TestCase):
    """驗證 C-2.5.2.1 移除布林信任根、Ed25519 非對稱簽章與任務上下文綁定之安全性."""

    def setUp(self) -> None:
        self.synth = _Scenarios.open_position()
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(self.synth).consumed_predictions)
        self.real_ok = dataclasses.replace(_as_real(self.synth), consumed_predictions=recs)
        self.nav_dates = [r.valuation_date for r in self.real_ok.nav_history]
        self.bench = _bench(self.nav_dates, ["101", "102"], self.nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        self.full_att = LineageAttestation(True, True, True, True, ("TWSE_CALENDAR_AUDIT", "FINMIND_AUDIT"))

        # 官方金鑰與公鑰庫
        self.official_keypair = generate_ed25519_keypair("OFFICIAL_AUTHORITY_2026", KeyAuthorityLevel.OFFICIAL)
        self.store, self.root_keypair = _create_test_official_store(self.official_keypair)
        OfficialTrustAuthority.activate_isolated_test_context(store=self.store, test_root=self.root_keypair.public_key_bytes)

        # 合法官方 DatasetChecksumVerifier
        self.m_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:official_market_hash",
            "CANDIDATE_PRED_FEED": "sha256:official_pred_hash",
            "BENCH_SRC": "sha256:official_bench_hash",
        }
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.manifest_sig = self.official_keypair.sign(h_body).hex()
        self.official_checksum_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=self.store,
            manifest_signer_id=self.official_keypair.signer_id,
            manifest_signature_hex=self.manifest_sig,
            verifier_name="OfficialTWSEVerifier",
        )

    def _build_real_verified_report(self) -> BacktestReport:
        return BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=self.official_checksum_verifier,
            report_id="REP-TEST-001",
            project_id="TW_QUANT_PROD",
            environment_id="PROD_TRADING",
            source_git_commit="abcdef1234567890",
        )

    def tearDown(self) -> None:
        OfficialTrustAuthority.deactivate_isolated_test_context()

    # 1. PoC A1: 呼叫端傳入 is_trusted_authority=True，但未附受信任公鑰簽章，必須被拒絕
    def test_poc_a1_caller_boolean_is_trusted_authority_bypassed_fails(self) -> None:
        """PoC A1: 呼叫端傳入 is_trusted_authority=True，但未附官方公鑰簽章，必須判定未通過."""
        v = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_authority="HackerAuthority",
            is_trusted_authority=True,  # 嘗試透過布林 flag 自封授權
            trusted_key_store=None,     # 無官方公鑰庫
            manifest_signer_id=None,
            manifest_signature_hex=None,
        )
        res = v.verify_evidence(self.real_ok, benchmark=self.bench, attestation=self.full_att)
        self.assertFalse(res.is_verified)
        self.assertFalse(res.is_official_attested)
        self.assertTrue(any("TRUST_ROOT_NOT_CONFIGURED" in r for r in res.failure_reasons))

        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)

    # 2. PoC A2: 呼叫端傳入 is_trusted_root=True，但缺少官方公鑰庫或簽章，必須判定未通過
    def test_poc_a2_caller_boolean_is_trusted_root_bypassed_fails(self) -> None:
        """PoC A2: 呼叫端傳入 is_trusted_root=True，但缺少官方公鑰庫或簽章，必須判定未通過."""
        v = FileDigestEvidenceVerifier(
            source_file_paths={"CANDIDATE_MARKET_FEED": "dummy.parquet"},
            expected_file_hashes={"CANDIDATE_MARKET_FEED": "sha256:dummy"},
            trusted_authority="AttackerRoot",
            is_trusted_root=True,  # 嘗試透過布林 flag 自封信任根
            trusted_key_store=None,
        )
        res = v.verify_evidence(self.real_ok, benchmark=self.bench, attestation=self.full_att)
        self.assertFalse(res.is_verified)
        self.assertFalse(res.is_official_attested)
        self.assertTrue(any("TRUST_ROOT_NOT_CONFIGURED" in r for r in res.failure_reasons))

        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)

    # 3. PoC B4b: 呼叫端使用自行生成的 Ed25519 私鑰自簽報告，反序列化時必須被拒絕或降級
    def test_poc_b4b_caller_self_signed_ed25519_unauthorized_key_fails(self) -> None:
        """PoC B4b: 呼叫端自製金鑰簽署報告，但在官方 TrustedPublicKeyStore 未登記，反序列化必須降級或報錯."""
        rep = self._build_real_verified_report()
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)

        # 攻擊者自行生成金鑰並自簽
        attacker_keypair = generate_ed25519_keypair("ATTACKER_KEY_2026", KeyAuthorityLevel.OFFICIAL)
        rep.sign(attacker_keypair)
        signed_json = rep.to_json()

        # (a) 非嚴格模式：強制降級為 REAL_HISTORICAL_NOT_VERIFIED
        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=self.store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)
        self.assertTrue(any("未在受信任公鑰庫中登記" in w for w in restored.content["data_quality_warnings"]))

        # (b) 嚴格模式：直接拋出 BacktestMetricsError
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
        self.assertIn("未在受信任公鑰庫中登記", str(ctx.exception))

    # 4. PoC B5: 重放防護 (Replay Protection) — 跨任務、跨策略、跨環境挪移
    def test_poc_b5_context_mismatch_run_id_replay_fails(self) -> None:
        """PoC B5: 任務 A 之合法簽章報告挪移至任務 B (expected_run_id 不符)，反序列化必須拒絕."""
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()

        # 預期任務為 RUN-DIFFERENT-002
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id="RUN-DIFFERENT-002",
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
        self.assertIn("任務上下文 run_id 不符", str(ctx.exception))

        # 非嚴格模式降級
        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=self.store,
            expected_run_id="RUN-DIFFERENT-002",
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)

    def test_poc_b5_context_mismatch_strategy_id_replay_fails(self) -> None:
        """PoC B5: 預期 strategy_id 不符時反序列化必須拒絕或降級."""
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()

        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=self.store,
            expected_run_id=rep.run_id,
            expected_strategy_id="OTHER_STRATEGY_XYZ",
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)

    def test_poc_b5_context_mismatch_environment_id_replay_fails(self) -> None:
        """PoC B5: 預期 environment_id 不符 (例如將 TEST 環境簽章挪至 PROD) 時必須降級."""
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()

        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=self.store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id="STAGING_ENV",
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)

    # 5. 簽章過期測試
    def test_poc_expired_signature_fails(self) -> None:
        """簽章有效期限 expires_at 早於目前時間，反序列化必須判定過期失效."""
        rep = self._build_real_verified_report()
        base_time = dt.datetime(2025, 1, 1, 10, 0, 0, tzinfo=TAIPEI_TZ)
        rep.sign(self.official_keypair, valid_days=10, current_time=base_time)
        signed_json = rep.to_json()

        # 檢驗時間為 15 天後 (已過期)
        future_time = dt.datetime(2025, 1, 16, 10, 0, 0, tzinfo=TAIPEI_TZ)
        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=self.store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            current_time=future_time,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)
        self.assertTrue(any("數位簽章已過期" in w for w in restored.content["data_quality_warnings"]))

        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                current_time=future_time,
                strict_reject_unverified_load=True,
            )
        self.assertIn("數位簽章已過期", str(ctx.exception))

    # 6. 竄改報告內容測試 (修改 final_nav 並重算 content_hash)
    def test_poc_tampered_json_content_hash_recomputed_fails(self) -> None:
        """修改 JSON 欄位並重算 content_hash，反序列化時必須因 Ed25519 簽章不符而強制降級."""
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()

        data = json.loads(signed_json)
        # 竄改最終淨值
        data["run"]["final_nav"] = "9999999999.00"
        del data["content_hash"]
        full_body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        data["content_hash"] = hashlib.sha256(full_body.encode("utf-8")).hexdigest()
        tampered_json = json.dumps(data)

        # 反序列化：雖然 content_hash 一致，但 Ed25519 簽章與內容雜湊比對失敗
        restored = BacktestReport.from_json(
            tampered_json,
            trusted_key_store=self.store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)

        with self.assertRaises(BacktestMetricsError):
            BacktestReport.from_json(
                tampered_json,
                trusted_key_store=self.store,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )

    # 7. 自製 Verifier 阻斷測試
    def test_poc_custom_mock_verifier_cannot_elevate_verdict(self) -> None:
        """呼叫端自製 mock/custom verifier 並回傳 is_verified=True，缺少受信任官方公鑰授權時必須為 NOT_VERIFIED."""
        custom_v = MockIndependentEvidenceVerifier(is_verified=True, verifier_name="AttackerCustomVerifier")
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=custom_v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertTrue(any("TEST_ONLY_EVIDENCE" in u or "LACKS_OFFICIAL_AUTHORITY" in u for u in rep.content["unverified_items"]))

    # 8. LineageAttestation 全 True 阻斷測試
    def test_poc_lineage_attestation_all_true_cannot_elevate_verdict(self) -> None:
        """僅提供 LineageAttestation(True, True, True, True)，未配置獨立驗證器時，必須為 NOT_VERIFIED."""
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=LineageAttestation(True, True, True, True, ("All subjective claims",)),
            benchmark=self.bench,
            evidence_verifier=None,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)

    # 9. 測試金鑰隔離測試: TEST_ONLY 金鑰簽署絕不可輸出 REAL_HISTORICAL_PIT_VERIFIED
    def test_poc_test_only_key_authority_cannot_grant_verified(self) -> None:
        """使用標記為 TEST_ONLY 的金鑰簽署 manifest 或報告，verdict 絕不可為 REAL_HISTORICAL_PIT_VERIFIED."""
        test_keypair = generate_ed25519_keypair("SANDBOX_TEST_SIGNER", KeyAuthorityLevel.TEST_ONLY)
        test_store = TrustedPublicKeyStore()
        test_store.enroll_key(test_keypair.signer_id, test_keypair.public_key_bytes, KeyAuthorityLevel.TEST_ONLY)

        # (a) Manifest 驗證器使用 TEST_ONLY 金鑰 -> 報告 build 產出 REAL_HISTORICAL_NOT_VERIFIED
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        test_manifest_sig = test_keypair.sign(h_body).hex()
        test_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=test_store,
            manifest_signer_id=test_keypair.signer_id,
            manifest_signature_hex=test_manifest_sig,
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=test_verifier,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)

        # (b) 若報告以官方驗證通過，但由 TEST_ONLY 金鑰簽署報告 -> from_json 拒絕載入已驗證結論
        rep_official = self._build_real_verified_report()
        rep_official.sign(test_keypair)
        signed_json = rep_official.to_json()

        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=test_store,
            expected_run_id=rep_official.run_id,
            expected_strategy_id=rep_official.strategy_id,
            expected_project_id=rep_official.project_id,
            expected_environment_id=rep_official.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)

    # 10. 合成資料永遠隔離測試
    def test_poc_synthetic_data_with_official_signature_remains_synthetic(self) -> None:
        """任何包含合成行情、預測或 Mock 標記之回測結果，即使附上官方簽章，verdict 永遠為 SYNTHETIC_TEST_RESULT_ONLY."""
        synth = self.synth
        rep = BacktestReport.build(
            synth,
            PerformanceAnalyzer().analyze(synth),
            evidence_verifier=self.official_checksum_verifier,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)
        self.assertTrue(rep.is_synthetic_result)

        rep.sign(self.official_keypair)
        signed_json = rep.to_json()
        restored = BacktestReport.from_json(signed_json, trusted_key_store=self.store)
        self.assertEqual(restored.validation_verdict, ValidationVerdict.SYNTHETIC_TEST_RESULT_ONLY)
        self.assertTrue(restored.is_synthetic_result)

    # 11. 正確官方簽章通路驗證: 完整合法 roundtrip
    def test_poc_official_pipeline_full_roundtrip_succeeds(self) -> None:
        """配置具 OFFICIAL 授權等級之公鑰庫、真實資料、完整 NAV、有效 Ed25519 簽章與正確任務上下文，完整 roundtrip 通過."""
        rep = self._build_real_verified_report()
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)
        self.assertTrue(rep.claims_real_historical_verified)

        sig = rep.sign(self.official_keypair, valid_days=30)
        self.assertEqual(sig.algorithm, "Ed25519-RFC8032")
        self.assertEqual(sig.signer_id, self.official_keypair.signer_id)

        signed_json = rep.to_json()

        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=self.store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id="TW_QUANT_PROD",
            expected_environment_id="PROD_TRADING",
            strict_reject_unverified_load=True,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)
        self.assertTrue(restored.claims_real_historical_verified)
        self.assertEqual(restored.report_id, rep.report_id)
        self.assertEqual(restored.content_hash, rep.content_hash)


# ═════════════════════════════════════════════════════════════════
# RFC 8032 官方測試向量驗證集 (INFO-01 / FINDING-07)
# ═════════════════════════════════════════════════════════════════


class TestRFC8032OfficialVectors(unittest.TestCase):
    """RFC 8032 Section 7.1 Test Vector 1 官方標準測試向量驗證."""

    SK_SEED_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
    EXPECTED_PK_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    EXPECTED_SIG_HEX = (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )

    def test_rfc8032_vector_1_exact_match(self) -> None:
        """RFC 8032 7.1 Vector 1: 空訊息簽章與公鑰產生完全符合官方標準規範."""
        sk = bytes.fromhex(self.SK_SEED_HEX)
        pk = ed25519_publickey(sk)
        self.assertEqual(pk.hex(), self.EXPECTED_PK_HEX)

        sig = ed25519_sign(b"", sk, pk)
        self.assertEqual(sig.hex(), self.EXPECTED_SIG_HEX)

        ok = ed25519_verify(sig, b"", pk)
        self.assertTrue(ok)

    def test_rfc8032_vector_1_tampered_message_fails(self) -> None:
        """RFC 8032 7.1 Vector 1: 訊息遭修改 1 位元組時驗證必須失敗."""
        pk = bytes.fromhex(self.EXPECTED_PK_HEX)
        sig = bytes.fromhex(self.EXPECTED_SIG_HEX)
        self.assertFalse(ed25519_verify(sig, b"a", pk))

    def test_rfc8032_vector_1_tampered_signature_fails(self) -> None:
        """RFC 8032 7.1 Vector 1: 簽章遭修改 1 位元組時驗證必須失敗."""
        pk = bytes.fromhex(self.EXPECTED_PK_HEX)
        sig = bytearray(bytes.fromhex(self.EXPECTED_SIG_HEX))
        sig[0] ^= 0x01
        self.assertFalse(ed25519_verify(bytes(sig), b"", pk))


# ═════════════════════════════════════════════════════════════════
# C-2.5.2.2 信任根與 Verdict 決策閘門安全修補 PoC 測試集 (28 項具體驗證)
# ═════════════════════════════════════════════════════════════════


class TestC2522TrustRootAndVerdictSecurityPoCs(unittest.TestCase):
    """驗證 C-2.5.2.2 信任根加固、決策閘門、防重放 Store 與 RFC 8032 規範性之 28 項安全 PoC."""

    def setUp(self) -> None:
        self.synth = _Scenarios.open_position()
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(self.synth).consumed_predictions)
        self.real_ok = dataclasses.replace(_as_real(self.synth), consumed_predictions=recs)
        self.nav_dates = [r.valuation_date for r in self.real_ok.nav_history]
        self.bench = _bench(self.nav_dates, ["101", "102"], self.nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        self.full_att = LineageAttestation(True, True, True, True, ("TWSE_AUDIT_2026", "TAIEX_BENCH_AUDIT"))

        # 合法官方信任根與背書公鑰庫
        self.official_keypair = generate_ed25519_keypair("OFFICIAL_AUTHORITY_2026", KeyAuthorityLevel.OFFICIAL)
        self.store, self.root_keypair = _create_test_official_store(self.official_keypair)
        OfficialTrustAuthority.activate_isolated_test_context(store=self.store, test_root=self.root_keypair.public_key_bytes)

        self.m_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:official_market_hash",
            "CANDIDATE_PRED_FEED": "sha256:official_pred_hash",
            "BENCH_SRC": "sha256:official_bench_hash",
        }
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.manifest_sig = self.official_keypair.sign(h_body).hex()
        self.official_checksum_verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=self.store,
            manifest_signer_id=self.official_keypair.signer_id,
            manifest_signature_hex=self.manifest_sig,
            verifier_name="OfficialTWSEVerifier",
        )

    def _build_real_verified_report(self) -> BacktestReport:
        return BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=self.official_checksum_verifier,
            report_id="REP-TEST-C2522",
            project_id="TW_QUANT_PROD",
            environment_id="PROD_TRADING",
            source_git_commit="gitcommit123456",
        )

    def tearDown(self) -> None:
        OfficialTrustAuthority.deactivate_isolated_test_context()

    # PoC 01: 呼叫端自行建構公鑰庫並登記 OFFICIAL (無信任根) 必須被拒絕 (FINDING-01)
    def test_poc_c2522_01_caller_instantiate_keystore_self_enroll_official_without_root_fails(self) -> None:
        store = TrustedPublicKeyStore()
        self.assertFalse(store.has_official_root)
        kp = generate_ed25519_keypair("ATTACKER_SIGNER")
        with self.assertRaises(ValueError) as ctx:
            store.enroll_key(kp.signer_id, kp.public_key_bytes, KeyAuthorityLevel.OFFICIAL)
        self.assertIn("OFFICIAL_ROOT_NOT_CONFIGURED", str(ctx.exception))

    # PoC 02: 呼叫端登記 OFFICIAL 但未提供根私鑰背書簽章必須被拒絕 (FINDING-01)
    def test_poc_c2522_02_caller_self_enroll_official_without_root_endorsement_fails(self) -> None:
        root_kp = generate_ed25519_keypair("ROOT_KEY", KeyAuthorityLevel.OFFICIAL)
        store = TrustedPublicKeyStore(official_root_public_key=root_kp.public_key_bytes)
        self.assertTrue(store.has_official_root)
        kp = generate_ed25519_keypair("NEW_SIGNER")
        with self.assertRaises(ValueError) as ctx:
            store.enroll_key(kp.signer_id, kp.public_key_bytes, KeyAuthorityLevel.OFFICIAL)
        self.assertIn("root_endorsement_signature", str(ctx.exception))

    # PoC 03: 呼叫端登記 OFFICIAL 提供偽造／無效根背書簽章必須被拒絕 (FINDING-01)
    def test_poc_c2522_03_caller_self_enroll_official_with_invalid_root_endorsement_fails(self) -> None:
        root_kp = generate_ed25519_keypair("ROOT_KEY", KeyAuthorityLevel.OFFICIAL)
        store = TrustedPublicKeyStore(official_root_public_key=root_kp.public_key_bytes)
        kp = generate_ed25519_keypair("NEW_SIGNER")
        fake_sig = b"\x00" * 64
        with self.assertRaises(ValueError) as ctx:
            store.enroll_key(
                kp.signer_id, kp.public_key_bytes, KeyAuthorityLevel.OFFICIAL,
                root_endorsement_signature=fake_sig,
            )
        self.assertIn("官方根背書簽章驗證失敗", str(ctx.exception))

    # PoC 04: 正確提供官方根私鑰背書簽章時登記 OFFICIAL 成功
    def test_poc_c2522_04_official_enrollment_with_valid_root_endorsement_succeeds(self) -> None:
        root_kp = generate_ed25519_keypair("ROOT_KEY", KeyAuthorityLevel.OFFICIAL)
        store = TrustedPublicKeyStore(official_root_public_key=root_kp.public_key_bytes)
        kp = generate_ed25519_keypair("VALID_SIGNER")
        endorsement = root_kp.sign(f"OFFICIAL_GRANT:{kp.signer_id}:{kp.public_key_hex}".encode("utf-8"))
        store.enroll_key(
            kp.signer_id, kp.public_key_bytes, KeyAuthorityLevel.OFFICIAL,
            root_endorsement_signature=endorsement,
        )
        self.assertTrue(store.is_enrolled(kp.signer_id))
        info = store.get_key(kp.signer_id)
        self.assertIsNotNone(info)
        self.assertEqual(info[1], KeyAuthorityLevel.OFFICIAL)

    # PoC 05: generate_ed25519_keypair 預設授權等級必須為 TEST_ONLY
    def test_poc_c2522_05_default_keypair_authority_level_is_test_only(self) -> None:
        kp = generate_ed25519_keypair("DEFAULT_KEY")
        self.assertEqual(kp.authority_level, KeyAuthorityLevel.TEST_ONLY)

    # PoC 06: 未配置根錨點之 Store 其 has_official_root 必須為 False
    def test_poc_c2522_06_unconfigured_root_store_has_official_root_is_false(self) -> None:
        store = TrustedPublicKeyStore()
        self.assertFalse(store.has_official_root)
        self.assertIsNone(store.root_public_key_hex)

    # PoC 07: 透過環境變數 TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY 試圖配置官方信任根必須被忽略 (FINDING-01 & C-2.5.2.2.1 修復)
    def test_poc_c2522_07_env_var_root_anchor_enrollment(self) -> None:
        root_kp = generate_ed25519_keypair("ENV_ROOT", KeyAuthorityLevel.OFFICIAL)
        old_env = os.environ.get("TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY")
        try:
            os.environ["TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY"] = root_kp.public_key_hex
            store = TrustedPublicKeyStore()
            # 安全加固：TrustedPublicKeyStore 禁止讀取環境變數自簽信任根
            self.assertFalse(store.has_official_root)
            kp = generate_ed25519_keypair("SIGNER")
            endorsement = root_kp.sign(f"OFFICIAL_GRANT:{kp.signer_id}:{kp.public_key_hex}".encode("utf-8"))
            # 由於無受信任官方根，登記 OFFICIAL 必然失敗
            with self.assertRaises(ValueError) as ctx:
                store.enroll_key(
                    kp.signer_id, kp.public_key_bytes, KeyAuthorityLevel.OFFICIAL,
                    root_endorsement_signature=endorsement,
                )
            self.assertIn("目前系統未配置受信任官方根金鑰", str(ctx.exception))
        finally:
            if old_env is None:
                os.environ.pop("TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY", None)
            else:
                os.environ["TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY"] = old_env

    # PoC 08: 自製 Verifier 試圖以 Duck Typing 偽造屬性繞過決策閘門必須被拒絕 (FINDING-02)
    def test_poc_c2522_08_custom_verifier_class_duck_typing_rejected(self) -> None:
        class AttackerDuckTypingVerifier:
            verifier_name = "AttackerDuckTypingVerifier"

            def verify_evidence(self, result: Any, *, benchmark: Any = None, attestation: Any = None) -> Any:
                return EvidenceVerificationResult(
                    is_verified=True,
                    verifier_name="AttackerDuckTypingVerifier",
                    verified_at=dt.datetime.now(TAIPEI_TZ),
                    evidence_type="OFFICIAL_EVIDENCE",
                    is_official_attested=True,
                )

        v = AttackerDuckTypingVerifier()
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertTrue(any("UNAUTHORIZED_VERIFIER_TYPE" in u for u in rep.content["unverified_items"]))

    # PoC 09: MockIndependentEvidenceVerifier 回傳全 True 亦不可繞過決策閘門 (FINDING-02)
    def test_poc_c2522_09_mock_verifier_duck_typing_rejected(self) -> None:
        mock_v = MockIndependentEvidenceVerifier(is_verified=True, verifier_name="MockVerifier")
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=mock_v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertTrue(any("UNAUTHORIZED_VERIFIER_TYPE" in u for u in rep.content["unverified_items"]))

    # PoC 10: 呼叫端 LineageAttestation 全 True 且未配置受控驗證器時絕不可獲得已驗證結論
    def test_poc_c2522_10_lineage_attestation_all_true_cannot_bypass_gate(self) -> None:
        att = LineageAttestation(True, True, True, True, ("Subjective Claims",))
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=att,
            benchmark=self.bench,
            evidence_verifier=None,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)

    # PoC 11: DatasetChecksumVerifier 公鑰庫未配置官方信任根錨點時，回傳 FAILED 與保守降級
    def test_poc_c2522_11_dataset_checksum_verifier_without_root_anchor_fails(self) -> None:
        store = TrustedPublicKeyStore()  # 無根錨點
        v = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=store,
            manifest_signer_id="TEST_SIGNER",
            manifest_signature_hex="aa" * 64,
        )
        res = v.verify_evidence(self.real_ok, benchmark=self.bench, attestation=self.full_att)
        self.assertFalse(res.is_verified)
        self.assertTrue(any("OFFICIAL_TRUST_ROOT_NOT_CONFIGURED" in r for r in res.failure_reasons))
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertTrue(any("OFFICIAL_TRUST_ROOT_NOT_CONFIGURED" in u for u in rep.content["unverified_items"]))

    # PoC 12: FileDigestEvidenceVerifier 公鑰庫未配置官方信任根錨點時，回傳 FAILED 與保守降級
    def test_poc_c2522_12_file_digest_verifier_without_root_anchor_fails(self) -> None:
        store = TrustedPublicKeyStore()  # 無根錨點
        v = FileDigestEvidenceVerifier(
            source_file_paths={"CANDIDATE_MARKET_FEED": "dummy.parquet"},
            expected_file_hashes={"CANDIDATE_MARKET_FEED": "sha256:dummy"},
            trusted_key_store=store,
            manifest_signer_id="TEST_SIGNER",
            manifest_signature_hex="aa" * 64,
        )
        res = v.verify_evidence(self.real_ok, benchmark=self.bench, attestation=self.full_att)
        self.assertFalse(res.is_verified)
        self.assertTrue(any("OFFICIAL_TRUST_ROOT_NOT_CONFIGURED" in r for r in res.failure_reasons))
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertTrue(any("OFFICIAL_TRUST_ROOT_NOT_CONFIGURED" in u for u in rep.content["unverified_items"]))

    # PoC 13: DatasetChecksumVerifier 使用 TEST_ONLY 簽署者時不可獲得 REAL_HISTORICAL_PIT_VERIFIED
    def test_poc_c2522_13_dataset_checksum_verifier_with_test_only_key_fails(self) -> None:
        test_kp = generate_ed25519_keypair("SANDBOX_SIGNER", KeyAuthorityLevel.TEST_ONLY)
        root_kp = generate_ed25519_keypair("ROOT", KeyAuthorityLevel.OFFICIAL)
        store = TrustedPublicKeyStore(official_root_public_key=root_kp.public_key_bytes)
        store.enroll_key(test_kp.signer_id, test_kp.public_key_bytes, KeyAuthorityLevel.TEST_ONLY)
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = test_kp.sign(h_body).hex()
        v = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=store,
            manifest_signer_id=test_kp.signer_id,
            manifest_signature_hex=sig,
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertTrue(any("TEST_ONLY_EVIDENCE" in u for u in rep.content["unverified_items"]))

    # PoC 14: FileDigestEvidenceVerifier 使用 TEST_ONLY 簽署者時不可獲得 REAL_HISTORICAL_PIT_VERIFIED
    def test_poc_c2522_14_file_digest_verifier_with_test_only_key_fails(self) -> None:
        test_kp = generate_ed25519_keypair("SANDBOX_SIGNER", KeyAuthorityLevel.TEST_ONLY)
        root_kp = generate_ed25519_keypair("ROOT", KeyAuthorityLevel.OFFICIAL)
        store = TrustedPublicKeyStore(official_root_public_key=root_kp.public_key_bytes)
        store.enroll_key(test_kp.signer_id, test_kp.public_key_bytes, KeyAuthorityLevel.TEST_ONLY)
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = test_kp.sign(h_body).hex()
        v = FileDigestEvidenceVerifier(
            source_file_paths={"CANDIDATE_MARKET_FEED": "dummy.parquet"},
            expected_file_hashes=self.m_hashes,
            trusted_key_store=store,
            manifest_signer_id=test_kp.signer_id,
            manifest_signature_hex=sig,
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)

    # PoC 15: from_json 還原已驗證報告缺少 expected_run_id 必須拒絕 (FINDING-03)
    def test_poc_c2522_15_from_json_missing_expected_run_id_fails(self) -> None:
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id=None,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
        self.assertIn("必須明確提供完整預期任務上下文", str(ctx.exception))

    # PoC 16: from_json 還原已驗證報告缺少 expected_strategy_id 必須拒絕 (FINDING-03)
    def test_poc_c2522_16_from_json_missing_expected_strategy_id_fails(self) -> None:
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id=rep.run_id,
                expected_strategy_id=None,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
        self.assertIn("必須明確提供完整預期任務上下文", str(ctx.exception))

    # PoC 17: from_json 還原已驗證報告缺少 expected_project_id 必須拒絕 (FINDING-03)
    def test_poc_c2522_17_from_json_missing_expected_project_id_fails(self) -> None:
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=None,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
        self.assertIn("必須明確提供完整預期任務上下文", str(ctx.exception))

    # PoC 18: from_json 還原已驗證報告缺少 expected_environment_id 必須拒絕 (FINDING-03)
    def test_poc_c2522_18_from_json_missing_expected_environment_id_fails(self) -> None:
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=None,
                strict_reject_unverified_load=True,
            )
        self.assertIn("必須明確提供完整預期任務上下文", str(ctx.exception))

    # PoC 19: from_json ReplayStore 原子性 Nonce 重複消費檢查防重放 (FINDING-03)
    def test_poc_c2522_19_from_json_replay_store_atomic_deduplication(self) -> None:
        rep = self._build_real_verified_report()
        rep.sign(self.official_keypair)
        signed_json = rep.to_json()
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            rstore = ReplayStore(db_path=db_path)
            # 第一次反序列化成功
            r1 = BacktestReport.from_json(
                signed_json,
                trusted_key_store=self.store,
                replay_store=rstore,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
            self.assertEqual(r1.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)

            # 第二次使用相同 Nonce 反序列化 -> 觸發重放攻擊防禦
            with self.assertRaises(BacktestMetricsError) as ctx:
                BacktestReport.from_json(
                    signed_json,
                    trusted_key_store=self.store,
                    replay_store=rstore,
                    expected_run_id=rep.run_id,
                    expected_strategy_id=rep.strategy_id,
                    expected_project_id=rep.project_id,
                    expected_environment_id=rep.environment_id,
                    strict_reject_unverified_load=True,
                )
            self.assertIn("Replay Attack Detected", str(ctx.exception))
        finally:
            if os.path.exists(db_path):
                try:
                    os.remove(db_path)
                except Exception:
                    pass

    # PoC 20: ReplayStore scope_key 隔離測試: 跨任務上下文之 Nonce 獨立管理
    def test_poc_c2522_20_from_json_replay_store_scoped_by_context(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            rstore = ReplayStore(db_path=db_path)
            # 任務 1 登記 Nonce
            now_iso = dt.datetime.now(TAIPEI_TZ).isoformat()
            exp_iso = (dt.datetime.now(TAIPEI_TZ) + dt.timedelta(days=1)).isoformat()
            ok1 = rstore.consume_nonce(
                signer_id="SIGNER_1",
                project_id="PROJ_A",
                environment_id="ENV_PROD",
                run_id="RUN_1",
                report_id="REP_1",
                nonce="NONCE_ABC",
                signed_at=now_iso,
                expires_at=exp_iso,
            )
            self.assertTrue(ok1)

            # 任務 2 (不同 run_id) 使用相同 Nonce 字符，應依 scope_key 獨立允許
            ok2 = rstore.consume_nonce(
                signer_id="SIGNER_1",
                project_id="PROJ_A",
                environment_id="ENV_PROD",
                run_id="RUN_2",
                report_id="REP_2",
                nonce="NONCE_ABC",
                signed_at=now_iso,
                expires_at=exp_iso,
            )
            self.assertTrue(ok2)
        finally:
            if os.path.exists(db_path):
                try:
                    os.remove(db_path)
                except Exception:
                    pass

    # PoC 21: validate_signature_timestamps 拒絕 naive datetime (FINDING-04)
    def test_poc_c2522_21_validate_timestamps_rejects_naive_datetime(self) -> None:
        naive_dt = dt.datetime(2025, 1, 1, 10, 0, 0)
        ok, reason = validate_signature_timestamps(naive_dt, naive_dt + dt.timedelta(days=1))
        self.assertFalse(ok)
        self.assertIn("缺少時區資訊", reason)

    # PoC 22: validate_signature_timestamps 拒絕 expires_at 早於等於 signed_at (FINDING-04)
    def test_poc_c2522_22_validate_timestamps_rejects_expires_before_signed(self) -> None:
        t1 = dt.datetime(2025, 1, 5, 10, 0, 0, tzinfo=TAIPEI_TZ)
        t2 = dt.datetime(2025, 1, 1, 10, 0, 0, tzinfo=TAIPEI_TZ)
        ok, reason = validate_signature_timestamps(t1, t2)
        self.assertFalse(ok)
        self.assertIn("expires_at 必須晚於 signed_at", reason)

    # PoC 23: validate_signature_timestamps 拒絕 signed_at 晚於目前時間 (未來簽章) (FINDING-04)
    def test_poc_c2522_23_validate_timestamps_rejects_future_signed_at(self) -> None:
        t_now = dt.datetime(2025, 1, 1, 10, 0, 0, tzinfo=TAIPEI_TZ)
        t_future = dt.datetime(2025, 1, 1, 11, 0, 0, tzinfo=TAIPEI_TZ)
        t_exp = dt.datetime(2025, 1, 10, 10, 0, 0, tzinfo=TAIPEI_TZ)
        ok, reason = validate_signature_timestamps(t_future, t_exp, current_time=t_now)
        self.assertFalse(ok)
        self.assertIn("signed_at 晚於目前時間", reason)

    # PoC 24: validate_signature_timestamps 拒絕已過期簽章 (FINDING-04)
    def test_poc_c2522_24_validate_timestamps_rejects_expired_signature(self) -> None:
        t_signed = dt.datetime(2025, 1, 1, 10, 0, 0, tzinfo=TAIPEI_TZ)
        t_exp = dt.datetime(2025, 1, 5, 10, 0, 0, tzinfo=TAIPEI_TZ)
        t_now = dt.datetime(2025, 1, 10, 10, 0, 0, tzinfo=TAIPEI_TZ)
        ok, reason = validate_signature_timestamps(t_signed, t_exp, current_time=t_now)
        self.assertFalse(ok)
        self.assertIn("數位簽章已過期", reason)

    # PoC 25: RFC 8032 Section 5.1.3 y >= p 拒絕非規範點編碼 (FINDING-05)
    def test_poc_c2522_25_rfc8032_non_canonical_y_greater_than_p_rejected(self) -> None:
        y_non_canonical = _P + 5
        s = y_non_canonical.to_bytes(32, "little")
        with self.assertRaises(ValueError) as ctx:
            _decodepoint(s)
        self.assertIn("RFC 8032 5.1.3", str(ctx.exception))

    # PoC 26: RFC 8032 7.1 Test Vector 1 官方金鑰簽章完全匹配 (FINDING-07)
    def test_poc_c2522_26_rfc8032_vector_1_exact_match(self) -> None:
        sk = bytes.fromhex(TestRFC8032OfficialVectors.SK_SEED_HEX)
        pk = ed25519_publickey(sk)
        self.assertEqual(pk.hex(), TestRFC8032OfficialVectors.EXPECTED_PK_HEX)
        sig = ed25519_sign(b"", sk, pk)
        self.assertEqual(sig.hex(), TestRFC8032OfficialVectors.EXPECTED_SIG_HEX)
        self.assertTrue(ed25519_verify(sig, b"", pk))

    # PoC 27: RFC 8032 7.1 Test Vector 1 訊息竄改 1 位元驗證失敗 (FINDING-07)
    def test_poc_c2522_27_rfc8032_vector_1_tampered_message_fails(self) -> None:
        pk = bytes.fromhex(TestRFC8032OfficialVectors.EXPECTED_PK_HEX)
        sig = bytes.fromhex(TestRFC8032OfficialVectors.EXPECTED_SIG_HEX)
        self.assertFalse(ed25519_verify(sig, b"tampered", pk))

    # PoC 28: RFC 8032 7.1 Test Vector 1 簽章竄改 1 位元驗證失敗 (FINDING-07)
    def test_poc_c2522_28_rfc8032_vector_1_tampered_signature_fails(self) -> None:
        pk = bytes.fromhex(TestRFC8032OfficialVectors.EXPECTED_PK_HEX)
        sig = bytearray(bytes.fromhex(TestRFC8032OfficialVectors.EXPECTED_SIG_HEX))
        sig[10] ^= 0x42
        self.assertFalse(ed25519_verify(bytes(sig), b"", pk))


# ═════════════════════════════════════════════════════════════════
# C-2.5.2.2.1 信任根與 ReplayStore 安全修補專用對抗性驗證套件 (A ~ O)
# ═════════════════════════════════════════════════════════════════


class TestC25221TrustRootAndReplaySecurityRemediation(unittest.TestCase):
    """驗證 C-2.5.2.2.1 根信任錨點、ReplayStore 假物件注入防禦與 SQLite Fail-Closed 之 15 項對抗測試."""

    def setUp(self) -> None:
        # 預設每次測試前清理測試隔離環境，確保回到生產未配置狀態
        OfficialTrustAuthority.deactivate_isolated_test_context()

        self.synth = _Scenarios.open_position()
        recs = tuple(dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2)) for r in _as_real(self.synth).consumed_predictions)
        self.real_ok = dataclasses.replace(_as_real(self.synth), consumed_predictions=recs)
        self.nav_dates = [r.valuation_date for r in self.real_ok.nav_history]
        self.bench = _bench(self.nav_dates, ["101", "102"], self.nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        self.full_att = LineageAttestation(True, True, True, True, ("TWSE_2026_AUDIT", "TAIEX_BENCH_AUDIT"))

        self.m_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:official_market_hash",
            "CANDIDATE_PRED_FEED": "sha256:official_pred_hash",
            "BENCH_SRC": "sha256:official_bench_hash",
        }

    def tearDown(self) -> None:
        OfficialTrustAuthority.deactivate_isolated_test_context()

    # A. 假 Root 與自簽 OFFICIAL_GRANT
    def test_a_fake_root_and_self_signed_official_grant_unauthorized(self) -> None:
        attacker_root = generate_ed25519_keypair("ATTACKER_ROOT", KeyAuthorityLevel.OFFICIAL)
        attacker_signer = generate_ed25519_keypair("ATTACKER_SIGNER", KeyAuthorityLevel.OFFICIAL)
        attacker_store = TrustedPublicKeyStore(root_trust_anchor=attacker_root.public_key_bytes)
        self.assertFalse(attacker_store.is_system_official)

        endorsement = attacker_root.sign(f"OFFICIAL_GRANT:{attacker_signer.signer_id}:{attacker_signer.public_key_hex}".encode("utf-8"))
        attacker_store.enroll_key(
            attacker_signer.signer_id, attacker_signer.public_key_bytes, KeyAuthorityLevel.OFFICIAL,
            root_endorsement_signature=endorsement,
        )
        self.assertTrue(attacker_store.is_enrolled(attacker_signer.signer_id))
        # 驗證系統官方 Trust Authority 並不認可此自簽金鑰
        official_store, _ = OfficialTrustAuthority.get_active_trust_store()
        self.assertFalse(official_store.is_enrolled(attacker_signer.signer_id))

    # B. 自製 Trust Store 注入 build()
    def test_b_custom_trust_store_injected_into_build_fails(self) -> None:
        attacker_root = generate_ed25519_keypair("ATTACKER_ROOT", KeyAuthorityLevel.OFFICIAL)
        attacker_signer = generate_ed25519_keypair("ATTACKER_SIGNER", KeyAuthorityLevel.OFFICIAL)
        attacker_store = TrustedPublicKeyStore(root_trust_anchor=attacker_root.public_key_bytes)
        endorsement = attacker_root.sign(f"OFFICIAL_GRANT:{attacker_signer.signer_id}:{attacker_signer.public_key_hex}".encode("utf-8"))
        attacker_store.enroll_key(
            attacker_signer.signer_id, attacker_signer.public_key_bytes, KeyAuthorityLevel.OFFICIAL,
            root_endorsement_signature=endorsement,
        )
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = attacker_signer.sign(h_body).hex()
        verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=attacker_store,
            manifest_signer_id=attacker_signer.signer_id,
            manifest_signature_hex=sig,
            verifier_name="AttackerMaliciousVerifier",
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=verifier,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)
        self.assertTrue(any("OFFICIAL_TRUST_ROOT_NOT_CONFIGURED" in u or "UNENROLLED_OFFICIAL_SIGNER" in u for u in rep.content["unverified_items"]))

    # C. 自製 Trust Store 注入 from_json() (PoC-1 漏洞修補)
    def test_c_custom_trust_store_injected_into_from_json_fails_poc1(self) -> None:
        attacker_root = generate_ed25519_keypair("ATTACKER_ROOT", KeyAuthorityLevel.OFFICIAL)
        attacker_signer = generate_ed25519_keypair("ATTACKER_SIGNER", KeyAuthorityLevel.OFFICIAL)
        attacker_store = TrustedPublicKeyStore(root_trust_anchor=attacker_root.public_key_bytes)
        endorsement = attacker_root.sign(f"OFFICIAL_GRANT:{attacker_signer.signer_id}:{attacker_signer.public_key_hex}".encode("utf-8"))
        attacker_store.enroll_key(
            attacker_signer.signer_id, attacker_signer.public_key_bytes, KeyAuthorityLevel.OFFICIAL,
            root_endorsement_signature=endorsement,
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=None,
            report_id="MALICIOUS_REP_001",
            project_id="PROD_PROJ",
            environment_id="PROD_ENV",
        )
        # 偽造結論並自簽
        rep._content["data_lineage"]["validation_verdict"] = ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED.value
        rep._content["data_lineage"]["claims_real_historical_verified"] = True
        rep.sign(attacker_signer)
        signed_json = rep.to_json()

        # (a) 非嚴格模式：自製 Trust Store 注入被拒絕，強制降級
        restored = BacktestReport.from_json(
            signed_json,
            trusted_key_store=attacker_store,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)
        self.assertTrue(any("UNAUTHORIZED_TRUST_STORE_INJECTION" in w for w in restored.content["data_quality_warnings"]))

        # (b) 嚴格模式：直接拋出受控例外拒絕載入
        with self.assertRaises(BacktestMetricsError) as ctx:
            BacktestReport.from_json(
                signed_json,
                trusted_key_store=attacker_store,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )
        self.assertIn("UNAUTHORIZED_TRUST_STORE_INJECTION", str(ctx.exception))

    # D. 環境變數替換根公鑰
    def test_d_env_var_replacement_of_root_public_key_strictly_ignored(self) -> None:
        attacker_root = generate_ed25519_keypair("ATTACKER_ROOT", KeyAuthorityLevel.OFFICIAL)
        old_env = os.environ.get("TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY")
        try:
            os.environ["TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY"] = attacker_root.public_key_hex
            # 驗證 TrustedPublicKeyStore() 不會讀取環境變數自封信任根
            store = TrustedPublicKeyStore()
            self.assertFalse(store.has_official_root)
            self.assertFalse(OfficialTrustAuthority.has_official_root())

            # 驗證 build() 依舊維持 Fail-Closed
            rep = BacktestReport.build(
                self.real_ok,
                PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
                attestation=self.full_att,
                benchmark=self.bench,
                evidence_verifier=None,
            )
            self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        finally:
            if old_env is None:
                os.environ.pop("TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY", None)
            else:
                os.environ["TW_QUANT_OFFICIAL_ROOT_PUBLIC_KEY"] = old_env

    # E. 正式根公鑰尚未配置
    def test_e_official_root_not_configured_fail_closed(self) -> None:
        self.assertEqual(OfficialTrustAuthority.status(), OfficialTrustStatus.NOT_CONFIGURED.value)
        self.assertFalse(OfficialTrustAuthority.has_official_root())
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=None,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)

    # F. 假 Verifier、Verifier 子類及內部 Trust Store 替換
    def test_f_fake_verifier_subclass_and_internal_store_swap_fails(self) -> None:
        attacker_kp = generate_ed25519_keypair("ATTACKER_SIGNER", KeyAuthorityLevel.OFFICIAL)
        attacker_store = TrustedPublicKeyStore(root_trust_anchor=attacker_kp.public_key_bytes)

        class AttackerSubclassVerifier(DatasetChecksumVerifier):
            def verify_evidence(self, result: Any, *, benchmark: Any = None, attestation: Any = None) -> Any:
                return EvidenceVerificationResult(
                    is_verified=True,
                    verifier_name="AttackerSubclassVerifier",
                    verified_at=dt.datetime.now(TAIPEI_TZ),
                    evidence_type="OFFICIAL_EVIDENCE",
                    is_official_attested=True,
                )

        fake_v = AttackerSubclassVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=attacker_store,
            manifest_signer_id=attacker_kp.signer_id,
            manifest_signature_hex="aa" * 64,
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=fake_v,
        )
        self.assertEqual(rep.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(rep.claims_real_historical_verified)

    # G. 假 ReplayStore 注入 (PoC-2 漏洞修補)
    def test_g_fake_replay_store_injection_fails_poc2(self) -> None:
        class BypassedReplayStore:
            def consume_nonce(self, **kwargs):
                return True

        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=None,
            report_id="REP_REPLAY_TEST",
            project_id="PROD_PROJ",
            environment_id="PROD_ENV",
        )
        # 標記為 REAL_HISTORICAL_PIT_VERIFIED 並自簽
        rep._content["data_lineage"]["validation_verdict"] = ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED.value
        rep._content["data_lineage"]["claims_real_historical_verified"] = True
        kp = generate_ed25519_keypair("SIGNER_TEST")
        rep.sign(kp)
        signed_json = rep.to_json()

        # (a) 傳入假物件：正式驗證端直接判定 UNAUTHORIZED_REPLAY_STORE_INJECTION
        restored = BacktestReport.from_json(
            signed_json,
            replay_store=BypassedReplayStore(),
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)

        # (b) 傳入 ReplayStore 假子類：同樣被拒絕
        class SubclassedFakeReplayStore(ReplayStore):
            def consume_nonce(self, **kwargs):
                return True

        with self.assertRaises(BacktestMetricsError):
            BacktestReport.from_json(
                signed_json,
                replay_store=SubclassedFakeReplayStore(),
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )

        # (c) 傳入錯誤型別物件
        with self.assertRaises(BacktestMetricsError):
            BacktestReport.from_json(
                signed_json,
                replay_store=12345,
                expected_run_id=rep.run_id,
                expected_strategy_id=rep.strategy_id,
                expected_project_id=rep.project_id,
                expected_environment_id=rep.environment_id,
                strict_reject_unverified_load=True,
            )

    # H. 同一 Nonce 重放
    def test_h_same_nonce_replay_detection(self) -> None:
        official_kp = generate_ed25519_keypair("OFFICIAL_2026", KeyAuthorityLevel.OFFICIAL)
        store, root_kp = _create_test_official_store(official_kp)
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            rstore = ReplayStore(db_path=db_path)
            h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
            sig = official_kp.sign(h_body).hex()
            verifier = DatasetChecksumVerifier(
                expected_manifest_hashes=self.m_hashes,
                trusted_key_store=store,
                manifest_signer_id=official_kp.signer_id,
                manifest_signature_hex=sig,
            )
            rep = BacktestReport.build(
                self.real_ok,
                PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
                attestation=self.full_att,
                benchmark=self.bench,
                evidence_verifier=verifier,
                report_id="NONCE_REP_001",
                project_id="PROD_PROJ",
                environment_id="PROD_ENV",
            )
            rep.sign(official_kp)
            signed_json = rep.to_json()

            # 第一次驗收：成功
            with OfficialTrustAuthority.isolated_test_context(store=store, replay_store=rstore):
                r1 = BacktestReport.from_json(
                    signed_json,
                    trusted_key_store=store,
                    replay_store=rstore,
                    expected_run_id=rep.run_id,
                    expected_strategy_id=rep.strategy_id,
                    expected_project_id=rep.project_id,
                    expected_environment_id=rep.environment_id,
                    strict_reject_unverified_load=True,
                )
                self.assertEqual(r1.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)

                # 第二次驗收同一份報告與 Nonce：嚴格拒絕重放
                with self.assertRaises(BacktestMetricsError) as ctx:
                    BacktestReport.from_json(
                        signed_json,
                        trusted_key_store=store,
                        replay_store=rstore,
                        expected_run_id=rep.run_id,
                        expected_strategy_id=rep.strategy_id,
                        expected_project_id=rep.project_id,
                        expected_environment_id=rep.environment_id,
                        strict_reject_unverified_load=True,
                    )
                self.assertIn("Replay Attack Detected", str(ctx.exception))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # I. 程序重啟後重放
    def test_i_replay_detection_persists_across_process_restarts(self) -> None:
        official_kp = generate_ed25519_keypair("OFFICIAL_2026", KeyAuthorityLevel.OFFICIAL)
        store, root_kp = _create_test_official_store(official_kp)
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            # 建立第一代 ReplayStore
            rstore1 = ReplayStore(db_path=db_path)
            h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
            sig = official_kp.sign(h_body).hex()
            verifier = DatasetChecksumVerifier(
                expected_manifest_hashes=self.m_hashes,
                trusted_key_store=store,
                manifest_signer_id=official_kp.signer_id,
                manifest_signature_hex=sig,
            )
            rep = BacktestReport.build(
                self.real_ok,
                PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
                attestation=self.full_att,
                benchmark=self.bench,
                evidence_verifier=verifier,
                report_id="PERSIST_REP_001",
                project_id="PROD_PROJ",
                environment_id="PROD_ENV",
            )
            rep.sign(official_kp)
            signed_json = rep.to_json()

            with OfficialTrustAuthority.isolated_test_context(store=store, replay_store=rstore1):
                r1 = BacktestReport.from_json(
                    signed_json,
                    trusted_key_store=store,
                    replay_store=rstore1,
                    expected_run_id=rep.run_id,
                    expected_strategy_id=rep.strategy_id,
                    expected_project_id=rep.project_id,
                    expected_environment_id=rep.environment_id,
                    strict_reject_unverified_load=True,
                )
                self.assertEqual(r1.validation_verdict, ValidationVerdict.REAL_HISTORICAL_PIT_VERIFIED)

            # 模擬程序重啟：銷毀 rstore1，建立全新實例 rstore2 指向同一個 SQLite 檔案
            del rstore1
            rstore2 = ReplayStore(db_path=db_path)

            with OfficialTrustAuthority.isolated_test_context(store=store, replay_store=rstore2):
                with self.assertRaises(BacktestMetricsError) as ctx:
                    BacktestReport.from_json(
                        signed_json,
                        trusted_key_store=store,
                        replay_store=rstore2,
                        expected_run_id=rep.run_id,
                        expected_strategy_id=rep.strategy_id,
                        expected_project_id=rep.project_id,
                        expected_environment_id=rep.environment_id,
                        strict_reject_unverified_load=True,
                    )
                self.assertIn("Replay Attack Detected", str(ctx.exception))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # J. SQLite locked (故障注入)
    def test_j_sqlite_database_locked_fail_closed(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            rstore = ReplayStore(db_path=db_path)
            # 使用另一個連線獲取 EXCLUSIVE 排他鎖
            lock_conn = sqlite3.connect(db_path, timeout=0.1, isolation_level="EXCLUSIVE")
            lock_conn.execute("BEGIN EXCLUSIVE")
            try:
                # 在排他鎖佔用下，consume_nonce 必然無法獲取寫入鎖，必須 Fail-Closed 拋出受控例外
                with self.assertRaises(BacktestMetricsError) as ctx:
                    rstore.consume_nonce(
                        signer_id="TEST_SIGNER",
                        project_id="PROJ",
                        environment_id="ENV",
                        run_id="RUN",
                        report_id="REP",
                        nonce="NONCE_LOCKED",
                        signed_at=dt.datetime.now(TAIPEI_TZ).isoformat(),
                        expires_at=(dt.datetime.now(TAIPEI_TZ) + dt.timedelta(days=1)).isoformat(),
                    )
                self.assertIn("Fail-Closed", str(ctx.exception))
            finally:
                lock_conn.rollback()
                lock_conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # K. SQLite readonly (故障注入)
    def test_k_sqlite_database_readonly_fail_closed(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            rstore = ReplayStore(db_path=db_path)
            # 將檔案權限改為唯讀
            os.chmod(db_path, stat.S_IREAD)
            try:
                with self.assertRaises(BacktestMetricsError) as ctx:
                    rstore.consume_nonce(
                        signer_id="TEST_SIGNER",
                        project_id="PROJ",
                        environment_id="ENV",
                        run_id="RUN",
                        report_id="REP",
                        nonce="NONCE_READONLY",
                        signed_at=dt.datetime.now(TAIPEI_TZ).isoformat(),
                        expires_at=(dt.datetime.now(TAIPEI_TZ) + dt.timedelta(days=1)).isoformat(),
                    )
                self.assertIn("Fail-Closed", str(ctx.exception))
            finally:
                # 恢復寫入權限以便清理
                os.chmod(db_path, stat.S_IWRITE | stat.S_IREAD)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # L. SQLite 寫入或 commit 失敗 (故障注入)
    def test_l_sqlite_write_or_commit_failure_fail_closed(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            rstore = ReplayStore(db_path=db_path)
            orig_connect = sqlite3.connect

            def faulty_connect(*args, **kwargs):
                conn = orig_connect(*args, **kwargs)

                class FaultyConnWrapper:
                    def __init__(self, real_conn):
                        self._real = real_conn
                    def __enter__(self):
                        return self
                    def __exit__(self, exc_type, exc_val, exc_tb):
                        raise sqlite3.OperationalError("disk I/O error on commit")
                    def execute(self, *a, **kw):
                        return self._real.execute(*a, **kw)
                    def close(self):
                        return self._real.close()

                return FaultyConnWrapper(conn)

            with patch("sqlite3.connect", side_effect=faulty_connect):
                with self.assertRaises(BacktestMetricsError) as ctx:
                    rstore.consume_nonce(
                        signer_id="TEST_SIGNER",
                        project_id="PROJ",
                        environment_id="ENV",
                        run_id="RUN",
                        report_id="REP",
                        nonce="NONCE_FAIL_COMMIT",
                        signed_at=dt.datetime.now(TAIPEI_TZ).isoformat(),
                        expires_at=(dt.datetime.now(TAIPEI_TZ) + dt.timedelta(days=1)).isoformat(),
                    )
                self.assertIn("Fail-Closed", str(ctx.exception))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # M. ReplayStore 資料庫損毀 (故障注入)
    def test_m_sqlite_database_corrupted_fail_closed(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_path = tf.name
        try:
            # 寫入無效垃圾資料破壞 SQLite 檔案結構
            with open(db_path, "wb") as f:
                f.write(b"CORRUPTED_DATABASE_GARBAGE_BYTES_1234567890")

            rstore = ReplayStore.__new__(ReplayStore)
            rstore.db_path = db_path
            with self.assertRaises(BacktestMetricsError) as ctx:
                rstore.consume_nonce(
                    signer_id="TEST_SIGNER",
                    project_id="PROJ",
                    environment_id="ENV",
                    run_id="RUN",
                    report_id="REP",
                    nonce="NONCE_CORRUPTED",
                    signed_at=dt.datetime.now(TAIPEI_TZ).isoformat(),
                    expires_at=(dt.datetime.now(TAIPEI_TZ) + dt.timedelta(days=1)).isoformat(),
                )
            self.assertIn("Fail-Closed", str(ctx.exception))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # N. JSON 竄改後不能藉由替換 Store 恢復 PIT_VERIFIED
    def test_n_tampered_json_cannot_recover_pit_verified_via_store_swap(self) -> None:
        official_kp = generate_ed25519_keypair("OFFICIAL_2026", KeyAuthorityLevel.OFFICIAL)
        store, root_kp = _create_test_official_store(official_kp)
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = official_kp.sign(h_body).hex()
        verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=store,
            manifest_signer_id=official_kp.signer_id,
            manifest_signature_hex=sig,
        )
        rep = BacktestReport.build(
            self.real_ok,
            PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
            attestation=self.full_att,
            benchmark=self.bench,
            evidence_verifier=verifier,
            report_id="TAMPER_REP_001",
            project_id="PROD_PROJ",
            environment_id="PROD_ENV",
        )
        rep.sign(official_kp)
        json_data = json.loads(rep.to_json())

        # 惡意修改報告內容（例如大幅調高總報酬率）
        json_data["metrics"]["return_and_risk"]["total_return"]["value"] = "99.99"
        tampered_text = json.dumps(json_data)

        # 無論傳入 official store、attacker store 或 None，content_hash 校驗必失敗拋出受控例外
        with self.assertRaises(BacktestMetricsError):
            BacktestReport.from_json(tampered_text, trusted_key_store=store)

    # O. 測試用根與正式根的隔離
    def test_o_test_root_isolation_from_official_root(self) -> None:
        test_kp = generate_ed25519_keypair("TEST_OFFICIAL", KeyAuthorityLevel.OFFICIAL)
        test_store, test_root = _create_test_official_store(test_kp)

        # 在隔離上下文內：test_store 有效
        with OfficialTrustAuthority.isolated_test_context(store=test_store, test_root=test_root.public_key_bytes):
            self.assertTrue(OfficialTrustAuthority.has_official_root())
            act_store, is_test = OfficialTrustAuthority.get_active_trust_store()
            self.assertTrue(is_test)
            self.assertTrue(act_store.has_official_root)

        # 脫離隔離上下文後：系統強制回歸未配置狀態，隔離徹底
        OfficialTrustAuthority.deactivate_isolated_test_context()
        self.assertFalse(OfficialTrustAuthority.has_official_root())
        self.assertEqual(OfficialTrustAuthority.status(), OfficialTrustStatus.NOT_CONFIGURED.value)
        act_store, is_test = OfficialTrustAuthority.get_active_trust_store()
        self.assertFalse(is_test)
        self.assertFalse(act_store.has_official_root)


class TestC25222SecurityHardeningRemediation(unittest.TestCase):
    """第三階段 C-2.5.2.2.2：針對 P0、P1、P2、P3 四項安全審查問題之驗收測試."""

    def setUp(self) -> None:
        OfficialTrustAuthority.deactivate_isolated_test_context()
        self.synth = _Scenarios.open_position()
        recs = tuple(
            dataclasses.replace(r, trained_cutoff_date=dt.date(2024, 12, 2))
            for r in _as_real(self.synth).consumed_predictions
        )
        self.real_ok = dataclasses.replace(_as_real(self.synth), consumed_predictions=recs)
        self.nav_dates = [r.valuation_date for r in self.real_ok.nav_history]
        self.bench = _bench(self.nav_dates, ["101", "102"], self.nav_dates[0] - dt.timedelta(days=1), synthetic=False)
        self.full_att = LineageAttestation(True, True, True, True, ("TWSE_CALENDAR_AUDIT", "FINMIND_AUDIT"))
        self.m_hashes = {
            "CANDIDATE_MARKET_FEED": "sha256:official_market_hash",
            "CANDIDATE_PRED_FEED": "sha256:official_pred_hash",
            "BENCH_SRC": "sha256:official_bench_hash",
        }

    def tearDown(self) -> None:
        OfficialTrustAuthority.deactivate_isolated_test_context()

    # P0-1: 不受信任策略或非測試腳本呼叫 activate_isolated_test_context / isolated_test_context 必須被拒絕
    def test_p0_1_untrusted_strategy_or_non_test_caller_blocked_from_test_context(self) -> None:
        attacker_kp = generate_ed25519_keypair("ATTACKER", KeyAuthorityLevel.OFFICIAL)
        attacker_store = TrustedPublicKeyStore(root_trust_anchor=attacker_kp.public_key_bytes)

        # (a) 模擬從非測試檔案（如外部腳本 <string>）動態 exec 呼叫
        ns = {
            "OfficialTrustAuthority": OfficialTrustAuthority,
            "attacker_store": attacker_store,
        }
        code_obj = compile(
            "OfficialTrustAuthority.activate_isolated_test_context(store=attacker_store)",
            "<untrusted_script>",
            "exec",
        )
        with self.assertRaises(BacktestMetricsError) as ctx1:
            exec(code_obj, ns)
        self.assertIn("SECURITY_VIOLATION", str(ctx1.exception))
        self.assertFalse(OfficialTrustAuthority.is_test_isolated())

        # (b) 模擬策略層檔案在堆疊中呼叫
        strat_code = compile(
            "OfficialTrustAuthority.activate_isolated_test_context(store=attacker_store)",
            "src/tw_quant/strategy/malicious_strategy.py",
            "exec",
        )
        with self.assertRaises(BacktestMetricsError) as ctx2:
            exec(strat_code, ns)
        self.assertIn("SECURITY_VIOLATION", str(ctx2.exception))
        self.assertFalse(OfficialTrustAuthority.is_test_isolated())

    # P0-2: 測試隔離環境產出之報告明確標記為 TEST_ISOLATED_ONLY，不具備正式生產身分，且在正式環境載入必降級
    def test_p0_2_test_isolated_report_lacks_production_official_identity_and_rejected_in_prod(self) -> None:
        official_kp = generate_ed25519_keypair("OFFICIAL_2026", KeyAuthorityLevel.OFFICIAL)
        store, root_kp = _create_test_official_store(official_kp)
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = official_kp.sign(h_body).hex()
        verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=store,
            manifest_signer_id=official_kp.signer_id,
            manifest_signature_hex=sig,
        )

        with OfficialTrustAuthority.isolated_test_context(store=store, test_root=root_kp.public_key_bytes):
            rep = BacktestReport.build(
                self.real_ok,
                PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
                attestation=self.full_att,
                benchmark=self.bench,
                evidence_verifier=verifier,
                report_id="P0_TEST_REP",
                project_id="PROD_PROJ",
                environment_id="PROD_ENV",
            )
            rep.sign(official_kp)
            # 在測試隔離內，雖然狀態機測試為 REAL_HISTORICAL_PIT_VERIFIED，但明確標記為測試隔離且非正式生產驗證
            self.assertTrue(rep.is_test_isolated_verification)
            self.assertFalse(rep.is_official_production_verified)
            self.assertEqual(rep.content["data_lineage"]["trust_environment"], "TEST_ISOLATED_ONLY")
            signed_json = rep.to_json()

        # 退出測試隔離（回到正式驗證模式）後載入該報告：必須因 TEST_ISOLATED_REPORT_REJECTED_IN_PRODUCTION 降級或拒絕
        OfficialTrustAuthority.deactivate_isolated_test_context()
        restored = BacktestReport.from_json(
            signed_json,
            expected_run_id=rep.run_id,
            expected_strategy_id=rep.strategy_id,
            expected_project_id=rep.project_id,
            expected_environment_id=rep.environment_id,
            strict_reject_unverified_load=False,
        )
        self.assertEqual(restored.validation_verdict, ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)
        self.assertFalse(restored.claims_real_historical_verified)
        self.assertFalse(restored.is_official_production_verified)
        self.assertTrue(
            any("TEST_ISOLATED_REPORT_REJECTED_IN_PRODUCTION" in w for w in restored.content["data_quality_warnings"])
        )

    # P1: 測試隔離狀態採用執行緒本地儲存 (threading.local)，杜絕跨執行緒污染
    def test_p1_thread_local_test_isolation_prevents_cross_thread_pollution(self) -> None:
        official_kp = generate_ed25519_keypair("OFFICIAL_2026", KeyAuthorityLevel.OFFICIAL)
        store, root_kp = _create_test_official_store(official_kp)
        h_body = json.dumps(self.m_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = official_kp.sign(h_body).hex()
        verifier = DatasetChecksumVerifier(
            expected_manifest_hashes=self.m_hashes,
            trusted_key_store=store,
            manifest_signer_id=official_kp.signer_id,
            manifest_signature_hex=sig,
        )

        thread_b_results: dict = {}
        barrier = threading.Event()
        proceed = threading.Event()

        def worker_thread_b() -> None:
            barrier.wait(timeout=5.0)
            # 在主執行緒處於 isolated_test_context 期間，執行緒 B 檢查狀態並建構報告
            thread_b_results["is_isolated"] = OfficialTrustAuthority.is_test_isolated()
            thread_b_results["has_root"] = OfficialTrustAuthority.has_official_root()
            thread_b_results["status"] = OfficialTrustAuthority.status()
            rep_b = BacktestReport.build(
                self.real_ok,
                PerformanceAnalyzer().analyze(self.real_ok, benchmark=self.bench),
                attestation=self.full_att,
                benchmark=self.bench,
                evidence_verifier=verifier,
            )
            thread_b_results["verdict"] = rep_b.validation_verdict
            proceed.set()

        t_b = threading.Thread(target=worker_thread_b)
        t_b.start()

        with OfficialTrustAuthority.isolated_test_context(store=store, test_root=root_kp.public_key_bytes):
            self.assertTrue(OfficialTrustAuthority.is_test_isolated())
            barrier.set()
            proceed.wait(timeout=5.0)

        t_b.join(timeout=5.0)
        self.assertFalse(thread_b_results["is_isolated"])
        self.assertFalse(thread_b_results["has_root"])
        self.assertEqual(thread_b_results["status"], OfficialTrustStatus.NOT_CONFIGURED.value)
        self.assertEqual(thread_b_results["verdict"], ValidationVerdict.REAL_HISTORICAL_NOT_VERIFIED)

    # P2-1: ReplayStore 預設安全目錄與符號連結拒絕
    def test_p2_1_replay_store_secure_default_directory_and_symlink_rejection(self) -> None:
        default_rstore = ReplayStore()
        self.assertIn(".tw_quant_security", default_rstore.db_path)
        self.assertTrue(os.path.exists(default_rstore.db_path))

        # 傳入目錄路徑而非檔案路徑必須拒絕
        temp_dir = tempfile.mkdtemp()
        try:
            with self.assertRaises(BacktestMetricsError):
                ReplayStore(db_path=temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        # 模擬 islink=True 必須拒絕
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            sym_path = tf.name
        try:
            with patch("os.path.islink", return_value=True):
                with self.assertRaises(BacktestMetricsError) as ctx:
                    ReplayStore(db_path=sym_path)
                self.assertIn("禁止使用符號連結", str(ctx.exception))
        finally:
            if os.path.exists(sym_path):
                os.remove(sym_path)

    # P2-2: ReplayStore 偵測資料庫遭刪除、截斷、外部替換重建與舊備份回滾 (Fail-Closed)
    def test_p2_2_replay_store_detects_deletion_truncation_replacement_and_rollback(self) -> None:
        now_iso = dt.datetime.now(TAIPEI_TZ).isoformat()
        exp_iso = (dt.datetime.now(TAIPEI_TZ) + dt.timedelta(days=5)).isoformat()

        # (a) 運行中資料庫檔案遭刪除 -> Fail-Closed
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_del = tf.name
        try:
            rs_del = ReplayStore(db_path=db_del)
            os.remove(db_del)
            with self.assertRaises(BacktestMetricsError) as ctx_del:
                rs_del.consume_nonce(
                    signer_id="S1", project_id="P1", environment_id="E1",
                    run_id="R1", report_id="REP1", nonce="N_DEL",
                    signed_at=now_iso, expires_at=exp_iso,
                )
            self.assertIn("已遭刪除或不存在", str(ctx_del.exception))
        finally:
            if os.path.exists(db_del):
                os.remove(db_del)

        # (b) 運行中資料庫檔案遭截斷為 0 bytes -> Fail-Closed
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            db_trunc = tf.name
        try:
            rs_trunc = ReplayStore(db_path=db_trunc)
            with open(db_trunc, "wb") as f:
                f.truncate(0)
            with self.assertRaises(BacktestMetricsError) as ctx_trunc:
                rs_trunc.consume_nonce(
                    signer_id="S1", project_id="P1", environment_id="E1",
                    run_id="R1", report_id="REP1", nonce="N_TRUNC",
                    signed_at=now_iso, expires_at=exp_iso,
                )
            self.assertIn("截斷為空檔案", str(ctx_trunc.exception))
        finally:
            if os.path.exists(db_trunc):
                os.remove(db_trunc)

        # (c) 資料庫遭還原為舊備份 (Sequence Regression) 或替換為新重建之資料庫 (Token Mismatch) -> Fail-Closed
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf1, \
             tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf_bak:
            db_main = tf1.name
            db_backup = tf_bak.name
        try:
            rs_main = ReplayStore(db_path=db_main)
            # 備份初始狀態 (seq = 0)
            shutil.copyfile(db_main, db_backup)

            # 消費一筆 Nonce (seq 變為 1)
            rs_main.consume_nonce(
                signer_id="S1", project_id="P1", environment_id="E1",
                run_id="R1", report_id="REP1", nonce="N_FIRST",
                signed_at=now_iso, expires_at=exp_iso,
            )

            # 攻擊 1：將資料庫還原為舊備份 (seq 退回 0)
            shutil.copyfile(db_backup, db_main)
            with self.assertRaises(BacktestMetricsError) as ctx_rb:
                rs_main.consume_nonce(
                    signer_id="S1", project_id="P1", environment_id="E1",
                    run_id="R1", report_id="REP1", nonce="N_FIRST",
                    signed_at=now_iso, expires_at=exp_iso,
                )
            self.assertIn("Sequence Regression", str(ctx_rb.exception))

            # 攻擊 2：刪除並以全新 ReplayStore 重建同名資料庫 (Token Mismatch)
            os.remove(db_main)
            ReplayStore(db_path=db_main)
            with self.assertRaises(BacktestMetricsError) as ctx_tok:
                rs_main.consume_nonce(
                    signer_id="S1", project_id="P1", environment_id="E1",
                    run_id="R1", report_id="REP1", nonce="N_SECOND",
                    signed_at=now_iso, expires_at=exp_iso,
                )
            self.assertIn("Token Mismatch", str(ctx_tok.exception))
        finally:
            for p in (db_main, db_backup):
                for ext in ("", "-wal", "-shm"):
                    if os.path.exists(p + ext):
                        try:
                            os.remove(p + ext)
                        except OSError:
                            pass

    # P3: TrustedPublicKeyStore 建構時立即驗證 root_trust_anchor 型別、長度與曲線點
    def test_p3_trusted_public_key_store_validates_root_anchor_type_length_and_curve_point(self) -> None:
        # 1. 非法型別 (int, list, bool, dict)
        for bad_type in (12345, ["a", "b"], True, {"key": "val"}):
            with self.assertRaises(InvalidRootAnchorError):
                TrustedPublicKeyStore(root_trust_anchor=bad_type)  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                TrustedPublicKeyStore(root_trust_anchor=bad_type)  # type: ignore[arg-type]

        # 2. 非法 bytes 長度 (< 32 或 > 32)
        for bad_bytes in (b"", b"short", b"\x01" * 31, b"\x01" * 33):
            with self.assertRaises(InvalidRootAnchorError):
                TrustedPublicKeyStore(root_trust_anchor=bad_bytes)
            with self.assertRaises(ValueError):
                TrustedPublicKeyStore(root_trust_anchor=bad_bytes)

        # 3. 非法 hex 字串（長度非 64 或含非 hex 字元）
        for bad_hex in ("abcd", "zz" * 32, "00" * 31, "00" * 33):
            with self.assertRaises(InvalidRootAnchorError):
                TrustedPublicKeyStore(root_trust_anchor=bad_hex)

        # 4. 長度為 32 bytes 但非有效之 RFC 8032 規範曲線點 (例如 y >= p 之 0xFF * 32)
        with self.assertRaises(InvalidRootAnchorError):
            TrustedPublicKeyStore(root_trust_anchor=b"\xff" * 32)

        # 5. 合法生成的 Ed25519 公鑰 (bytes 與 hex) 建構成功
        valid_kp = generate_ed25519_keypair("VALID_ROOT", KeyAuthorityLevel.OFFICIAL)
        s1 = TrustedPublicKeyStore(root_trust_anchor=valid_kp.public_key_bytes)
        self.assertTrue(s1.has_official_root)
        s2 = TrustedPublicKeyStore(root_trust_anchor=valid_kp.public_key_hex)
        self.assertTrue(s2.has_official_root)



if __name__ == "__main__":
    unittest.main()

