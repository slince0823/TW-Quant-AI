"""回測績效分析器 (metrics.py) — 第三階段 C-2.5.

核心原則：
1. 嚴格依 `BACKTEST_METHODOLOGY.md` 第四章 17 項指標定義實作；與方法論不同之處一律記錄於
   `METHODOLOGY_DEVIATIONS`，不靜默改變定義。
2. 缺失資料絕不以 0 取代：以 `MetricStatus` 明確標示 `NOT_AVAILABLE` / `INSUFFICIENT_DATA` /
   `UNDEFINED` / `INCOMPLETE` / `BASIS_MISMATCH`，`value` 為 `None`。
3. 金額以 `Decimal` 精確計算；需開根號／冪次之比率以 `float` 計算 (確定性，可重現)。
4. 淨損益 (`ClosedLotTrade.realized_pnl`) 已扣除手續費與證交稅，絕不再次扣除；
   滑價已內含於成交價，只作為歸因資訊揭露，不另行扣減。
5. 股利只依帳本實際紀錄 (`paid_dividends` / `pending_dividends_at_end`) 揭露；
   NAV 已含股利 (現金或應收股利)，絕不與 NAV 報酬重複相加。
6. 純 Python 標準庫。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .data_provider import DataProvenance
from .exceptions import (
    BacktestMetricsError,
    BenchmarkAlignmentError,
    InvalidNavSeriesError,
)
from .lot_ledger import ClosedLotTrade
from .runner import BacktestRunResult, BacktestRunStatus

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FLOAT_EPS = 1e-12

# ─────────────────────────────────────────────────────────────
# 指標狀態與數值容器
# ─────────────────────────────────────────────────────────────


class MetricStatus(str, Enum):
    """指標計算狀態；非 `OK` 時 `MetricValue.value` 一律為 `None`。"""

    OK = "OK"
    NOT_AVAILABLE = "NOT_AVAILABLE"          # 缺少必要輸入 (例如無 Benchmark、無 NAV)
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # 樣本數不足 (例如 N < 2)
    UNDEFINED = "UNDEFINED"                  # 數學上未定義 (分母為零)
    INCOMPLETE = "INCOMPLETE"                # 輸入不完整 (NAV 估值不完整、欄位缺漏)
    BASIS_MISMATCH = "BASIS_MISMATCH"        # 報酬口徑或日期不一致


MetricNumber = Union[Decimal, float, int]


@dataclass(frozen=True)
class MetricValue:
    """單一指標結果。"""

    name: str
    value: Optional[MetricNumber]
    status: MetricStatus
    note: str = ""

    def __post_init__(self) -> None:
        if self.status == MetricStatus.OK and self.value is None:
            raise BacktestMetricsError(f"指標 {self.name} 狀態為 OK 時 value 不得為 None")
        if self.status != MetricStatus.OK and self.value is not None:
            raise BacktestMetricsError(f"指標 {self.name} 狀態為 {self.status.value} 時 value 必須為 None")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise BacktestMetricsError(f"指標 {self.name} 不得為 NaN/Inf")

    @property
    def is_ok(self) -> bool:
        return self.status == MetricStatus.OK

    def to_dict(self) -> Dict[str, Any]:
        if isinstance(self.value, Decimal):
            val: Any = str(self.value)
        else:
            val = self.value
        return {"value": val, "status": self.status.value, "note": self.note}


def _ok(name: str, value: MetricNumber, note: str = "") -> MetricValue:
    return MetricValue(name, value, MetricStatus.OK, note)


def _na(name: str, status: MetricStatus, note: str) -> MetricValue:
    return MetricValue(name, None, status, note)


# ─────────────────────────────────────────────────────────────
# 設定、NAV 序列與 Benchmark
# ─────────────────────────────────────────────────────────────


class IncompleteNavPolicy(str, Enum):
    """遇到估值不完整之 NAV 時的處理方式。"""

    REJECT = "REJECT"                    # 拋出 InvalidNavSeriesError (預設)
    MARK_INCOMPLETE = "MARK_INCOMPLETE"  # 所有 NAV 型指標標示 INCOMPLETE，不填假數值


@dataclass(frozen=True)
class AnalyzerConfig:
    """績效分析設定。

    - `trading_days_per_year`: 年化交易日數 A (方法論預設 252)。
    - `risk_free_rate_annual`: 年化無風險利率 R_f (方法論預設 0.015)；
      每日無風險利率 r_f = (1 + R_f)^(1/A) - 1。
    - `short_window_days`: N 小於此值時標註年化外推風險 (方法論預設 21)。
    - `reconciliation_tolerance`: 損益對帳容許誤差 (元)。
    """

    trading_days_per_year: int = 252
    risk_free_rate_annual: float = 0.015
    short_window_days: int = 21
    reconciliation_tolerance: Decimal = Decimal("0.01")
    incomplete_nav_policy: IncompleteNavPolicy = IncompleteNavPolicy.REJECT
    strict_benchmark: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.trading_days_per_year, bool) or not isinstance(self.trading_days_per_year, int) or self.trading_days_per_year < 1:
            raise BacktestMetricsError("trading_days_per_year 必須為 >= 1 的整數")
        rf = self.risk_free_rate_annual
        if isinstance(rf, bool) or not isinstance(rf, (int, float)) or not math.isfinite(rf) or rf <= -1.0:
            raise BacktestMetricsError("risk_free_rate_annual 必須為有限數值且 > -1")
        if not isinstance(self.reconciliation_tolerance, Decimal) or self.reconciliation_tolerance < _ZERO:
            raise BacktestMetricsError("reconciliation_tolerance 必須為非負 Decimal")
        if not isinstance(self.incomplete_nav_policy, IncompleteNavPolicy):
            raise BacktestMetricsError("incomplete_nav_policy 必須為 IncompleteNavPolicy")

    @property
    def daily_risk_free(self) -> float:
        return (1.0 + float(self.risk_free_rate_annual)) ** (1.0 / self.trading_days_per_year) - 1.0


@dataclass(frozen=True)
class NavPoint:
    """單日收盤 NAV (含現金、預留現金、持倉市值與應收股利)。"""

    date: dt.date
    nav: Decimal


def validate_nav_series(initial_nav: Decimal, points: Sequence[NavPoint]) -> None:
    """驗證日期嚴格遞增 (無重複、已排序) 與所有 NAV > 0。"""
    if not isinstance(initial_nav, Decimal) or not initial_nav.is_finite() or initial_nav <= _ZERO:
        raise InvalidNavSeriesError(f"初始 NAV 必須為 > 0 的有限 Decimal，收到 {initial_nav!r}")
    prev: Optional[dt.date] = None
    for p in points:
        if not isinstance(p, NavPoint) or isinstance(p.date, dt.datetime) or not isinstance(p.date, dt.date):
            raise InvalidNavSeriesError("NAV 序列元素必須為 NavPoint 且 date 為 datetime.date")
        if not isinstance(p.nav, Decimal) or not p.nav.is_finite() or p.nav <= _ZERO:
            raise InvalidNavSeriesError(
                f"{p.date} 之 NAV 必須為 > 0 的有限 Decimal，收到 {p.nav!r}",
                context={"date": p.date.isoformat()},
            )
        if prev is not None:
            if p.date == prev:
                raise InvalidNavSeriesError(f"NAV 日期重複: {p.date}", context={"date": p.date.isoformat()})
            if p.date < prev:
                raise InvalidNavSeriesError(
                    f"NAV 日期未依時間排序: {p.date} 出現在 {prev} 之後", context={"date": p.date.isoformat()}
                )
        prev = p.date


class BenchmarkReturnBasis(str, Enum):
    """Benchmark 報酬口徑。策略 NAV 含現金股利，屬 `TOTAL_RETURN` 口徑。"""

    TOTAL_RETURN = "TOTAL_RETURN"
    PRICE_ONLY = "PRICE_ONLY"


@dataclass(frozen=True)
class BenchmarkSeries:
    """Benchmark 指數／淨值水位序列。

    `levels` 必須包含一個嚴格早於策略第一個 NAV 日期的基準日 (取得第一日報酬 I_1/I_0 - 1 所必需)，
    並對策略每個 NAV 日期逐日有值。
    """

    name: str
    basis: BenchmarkReturnBasis
    provenance: DataProvenance
    levels: Tuple[Tuple[dt.date, Decimal], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise BenchmarkAlignmentError("Benchmark name 不得為空")
        if not isinstance(self.basis, BenchmarkReturnBasis):
            raise BenchmarkAlignmentError("Benchmark basis 必須為 BenchmarkReturnBasis")
        if not isinstance(self.provenance, DataProvenance):
            raise BenchmarkAlignmentError("Benchmark provenance 必須為 DataProvenance")
        lv = tuple(self.levels)
        prev: Optional[dt.date] = None
        for d, v in lv:
            if not isinstance(v, Decimal) or not v.is_finite() or v <= _ZERO:
                raise BenchmarkAlignmentError(f"Benchmark {d} 水位必須為 > 0 的有限 Decimal")
            if prev is not None and d <= prev:
                raise BenchmarkAlignmentError(f"Benchmark 日期重複或未排序: {d}")
            prev = d
        object.__setattr__(self, "levels", lv)

    @property
    def is_synthetic(self) -> bool:
        return self.provenance.is_synthetic

    @property
    def includes_dividends(self) -> bool:
        return self.basis == BenchmarkReturnBasis.TOTAL_RETURN

    def align(self, nav_dates: Sequence[dt.date]) -> Tuple[Decimal, Tuple[Decimal, ...]]:
        """對齊策略 NAV 日期，回傳 (基準日水位 I_0, 各 NAV 日水位)。

        不一致 (缺基準日、缺日、區間內多出策略沒有的日期而造成報酬壓縮) 一律拋出 `BenchmarkAlignmentError`。
        """
        if not nav_dates:
            raise BenchmarkAlignmentError("策略無 NAV 日期，無法對齊 Benchmark")
        first, last = nav_dates[0], nav_dates[-1]
        mapping = dict(self.levels)
        bases = [d for d, _ in self.levels if d < first]
        if not bases:
            raise BenchmarkAlignmentError(
                f"Benchmark 缺少早於策略首日 {first} 之基準日水位，無法計算第一日報酬",
                context={"first_nav_date": first.isoformat()},
            )
        base_date = bases[-1]
        window = [d for d, _ in self.levels if base_date < d <= last]
        missing = [d for d in nav_dates if d not in mapping]
        if missing:
            raise BenchmarkAlignmentError(
                f"Benchmark 缺少策略交易日 {missing[0]}；嚴禁壓縮兩日報酬為一日",
                context={"missing_date": missing[0].isoformat(), "missing_count": len(missing)},
            )
        extra = [d for d in window if d not in set(nav_dates)]
        if extra:
            raise BenchmarkAlignmentError(
                f"Benchmark 於策略區間內含有策略不存在之日期 {extra[0]}；日期區間不一致",
                context={"extra_date": extra[0].isoformat(), "extra_count": len(extra)},
            )
        return mapping[base_date], tuple(mapping[d] for d in nav_dates)


# ─────────────────────────────────────────────────────────────
# 純數學函式 (可由小型手算案例驗證)
# ─────────────────────────────────────────────────────────────


def simple_returns(initial_nav: Decimal, navs: Sequence[Decimal]) -> List[float]:
    """r_t = V_t / V_{t-1} - 1，V_0 = initial_nav。"""
    out: List[float] = []
    prev = initial_nav
    for v in navs:
        out.append(float(v / prev - _ONE))
        prev = v
    return out


def mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def sample_std(xs: Sequence[float]) -> float:
    """樣本標準差 (自由度 n - 1)；需 n >= 2。"""
    m = mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown_and_duration(
    initial_nav: Decimal, navs: Sequence[Decimal]
) -> Tuple[Decimal, int, bool]:
    """回傳 (MDD, 最長回撤持續交易日數, 該最長回撤是否於期末前已恢復)。

    高水位 M_t 包含 V_0；tau_t = 0 若 V_t >= M_t，否則 tau_{t-1} + 1。
    """
    hwm = initial_nav
    mdd = _ZERO
    tau = 0
    best_tau = 0
    best_recovered = True
    for v in navs:
        if v >= hwm:
            hwm = v
            tau = 0
        else:
            tau += 1
            dd = (hwm - v) / hwm
            if dd > mdd:
                mdd = dd
            if tau > best_tau:
                best_tau = tau
        # 以「目前進行中之回撤是否即為最長」判斷是否仍未恢復
    # 重新掃描以判斷最長回撤是否已恢復
    hwm = initial_nav
    tau = 0
    episodes: List[Tuple[int, bool]] = []
    for v in navs:
        if v >= hwm:
            if tau > 0:
                episodes.append((tau, True))
            hwm = v
            tau = 0
        else:
            tau += 1
    if tau > 0:
        episodes.append((tau, False))
    if episodes:
        best_tau = max(t for t, _ in episodes)
        best_recovered = next(rec for t, rec in episodes if t == best_tau)
    return mdd, best_tau, best_recovered


# ─────────────────────────────────────────────────────────────
# 方法論對照表
# ─────────────────────────────────────────────────────────────

# (編號, key, 中文名稱, 公式摘要, 輸入欄位)
METHODOLOGY_METRICS: Tuple[Tuple[int, str, str, str, str], ...] = (
    (1, "total_return", "累積報酬率", "(V_N - V_0) / V_0", "initial_cash, DailyNAVRecord.total_nav"),
    (2, "cagr", "年化報酬率", "(V_N/V_0)^(A/N) - 1", "initial_cash, total_nav, N"),
    (3, "annualized_volatility", "年化波動率", "std_{n-1}(r_t) * sqrt(A)", "total_nav"),
    (4, "sharpe_ratio", "Sharpe Ratio", "mean(e)/std(e) * sqrt(A), e = r - r_f", "total_nav, risk_free_rate_annual"),
    (5, "sortino_ratio", "Sortino Ratio", "mean(e)*A / (DD_daily*sqrt(A))", "total_nav, risk_free_rate_annual"),
    (6, "max_drawdown", "最大回撤", "max_t (M_t - V_t) / M_t", "initial_cash, total_nav"),
    (7, "max_drawdown_duration", "最長回撤期間 (交易日)", "max_t tau_t", "total_nav"),
    (8, "calmar_ratio", "Calmar Ratio", "CAGR / MDD", "cagr, max_drawdown"),
    (9, "win_rate", "勝率 (已平倉批次)", "K_win / K", "ClosedLotTrade.realized_pnl"),
    (10, "payoff_ratio", "平均獲利 / 平均虧損比", "AvgWin / AvgLoss", "ClosedLotTrade.realized_pnl"),
    (11, "profit_factor", "Profit Factor", "sum(win) / sum(|loss|)", "ClosedLotTrade.realized_pnl"),
    (12, "average_holding_days", "平均持有交易日數", "mean(h_c), 加權 sum(q*h)/sum(q)", "ClosedLotTrade.holding_trading_days, quantity"),
    (13, "turnover", "週轉率", "mean((B_t+S_t)/(2 V_t)) * A", "DailyBacktestLog.settled_fills.notional, total_nav"),
    (14, "commission_tax_cost_drag", "稅費拖累", "C_total / V_0; C_total/avg(V)*A/N; C_total/sum(notional)", "FillRecord.commission, FillRecord.tax, notional"),
    (15, "slippage_cost_drag", "滑價成本佔比", "sum(FillRecord.slippage) / V_0", "FillRecord.slippage"),
    (16, "unfilled_order_ratio", "未成交委託比例", "O_unfilled / O_resolved; 1 - sum(filled)/sum(qty)", "PaperOrder, OrderExecutionOutcome"),
    (17, "risk_rejection_ratio", "風控攔截比例", "S_rejected / S_total", "SignalDispatchAuditRecord.risk_decision"),
)

# 使用者任務清單中、但不在方法論 17 項內之補充指標 (不改動 17 項定義，另列為 supplementary)
SUPPLEMENTARY_METRICS: Tuple[Tuple[str, str], ...] = (
    ("excess_return_total", "累積超額報酬 R_s - R_b (需 Benchmark，方法論第五章)"),
    ("excess_return_annualized", "年化超額報酬 CAGR_s - CAGR_b (方法論第五章)"),
    ("tracking_error", "年化追蹤誤差 std(a_t)*sqrt(A) (方法論第五章)"),
    ("information_ratio", "資訊比率 (方法論第五章)"),
    ("beta", "Beta (方法論第五章)"),
    ("closed_lot_trade_count", "已平倉批次交易次數 (方法論第 9 項之 K)"),
    ("fill_count", "成交筆數"),
    ("average_win", "平均獲利交易 (方法論第 10 項分子)"),
    ("average_loss", "平均虧損交易 (方法論第 10 項分母)"),
    ("daily_win_rate", "日報酬勝率 (方法論第 9 項輔助指標)"),
)

METHODOLOGY_DEVIATIONS: Tuple[str, ...] = (
    "第 1 項：方法論 N=0 時回傳 0.0；本實作改為 NOT_AVAILABLE，因 0 會與「真實零報酬」混淆。",
    "第 2 項：方法論 N=0 或 V_N<=0 時回傳 0.0 / -1.0；本實作 N=0 為 NOT_AVAILABLE，V_N<=0 於 NAV 驗證階段直接拒絕 (NAV 必須為正值)。",
    "第 3 項：方法論 N<2 時回傳 0.0；本實作改為 INSUFFICIENT_DATA。所有日報酬完全相同 (真實零波動) 仍回傳 0.0。",
    "第 4/5/8/10/11 項：分母為零時方法論回傳 None；本實作以 UNDEFINED 狀態 + value=None 表示，語意相同但保留原因。浮點分母 <= 1e-12 視為零。",
    "第 9 項：K=0 時 None；本實作以 NOT_AVAILABLE 表示。報酬恰為 0 之交易不計入勝或負，但計入 K。",
    "第 12 項：任一已平倉批次缺少 holding_trading_days 時，本實作回報 INCOMPLETE，而非忽略缺失批次。",
    "第 16 項：未於期末前結案 (非終態) 之委託不納入分母與股數短缺率，另行揭露 pending 筆數，避免把尚未有機會成交之委託視為未成交。",
    "第 17 項：僅 `risk_decision.allowed == False` 之訊號計入風控攔截；通過風控但於券商預留／撮合階段被拒者另列 non_risk_rejections。",
    "使用者任務清單含「超額報酬、追蹤誤差、交易次數、毛／淨損益、股利收入、買賣手續費分列」，其中超額報酬與追蹤誤差源自方法論第五章、其餘為成本歸因補充，皆列為 supplementary，不改動 17 項定義；使用者清單未列出之第 16、17 項仍依方法論實作。",
    "方法論第 15 項以「基準價 vs 成交價」計算滑價；本實作直接採用 `FillRecord.slippage` (=|成交價-參考價|×股數，已為非負金額)，不重新推算。",
)


# ─────────────────────────────────────────────────────────────
# 結果容器
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PnLReconciliation:
    """損益對帳：final_nav - initial = 已實現(淨) + 未平倉未實現 + 已入帳股利 + 應收股利。"""

    status: MetricStatus
    initial_cash: Decimal
    final_nav: Optional[Decimal]
    realized_pnl_net: Decimal
    unrealized_pnl_open: Optional[Decimal]
    dividends_paid_net: Decimal
    dividends_receivable_end: Decimal
    explained_total: Optional[Decimal]
    residual: Optional[Decimal]
    open_position_count: int
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        def s(x: Any) -> Any:
            return None if x is None else str(x)

        return {
            "status": self.status.value,
            "initial_cash": s(self.initial_cash),
            "final_nav": s(self.final_nav),
            "realized_pnl_net": s(self.realized_pnl_net),
            "unrealized_pnl_open": s(self.unrealized_pnl_open),
            "dividends_paid_net": s(self.dividends_paid_net),
            "dividends_receivable_end": s(self.dividends_receivable_end),
            "explained_total": s(self.explained_total),
            "residual": s(self.residual),
            "open_position_count": self.open_position_count,
            "note": self.note,
        }


@dataclass(frozen=True)
class PerformanceMetrics:
    """完整績效分析結果。"""

    metrics: Mapping[str, MetricValue]
    breakdowns: Mapping[str, Any]
    reconciliation: PnLReconciliation
    nav_complete: bool
    nav_status: str
    benchmark_status: str
    warnings: Tuple[str, ...]
    config: AnalyzerConfig
    trading_day_count: int

    def get(self, key: str) -> MetricValue:
        return self.metrics[key]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": {
                "trading_days_per_year": self.config.trading_days_per_year,
                "risk_free_rate_annual": self.config.risk_free_rate_annual,
                "daily_risk_free": self.config.daily_risk_free,
                "short_window_days": self.config.short_window_days,
            },
            "trading_day_count": self.trading_day_count,
            "nav_complete": self.nav_complete,
            "nav_status": self.nav_status,
            "benchmark_status": self.benchmark_status,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "breakdowns": _jsonable(self.breakdowns),
            "pnl_reconciliation": self.reconciliation.to_dict(),
            "warnings": list(self.warnings),
        }


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    return obj


# ─────────────────────────────────────────────────────────────
# 分析器
# ─────────────────────────────────────────────────────────────

_NAV_KEYS = (
    "total_return", "cagr", "annualized_volatility", "sharpe_ratio", "sortino_ratio",
    "max_drawdown", "max_drawdown_duration", "calmar_ratio",
)
_BENCH_KEYS = (
    "excess_return_total", "excess_return_annualized", "tracking_error", "information_ratio", "beta",
)


class PerformanceAnalyzer:
    """回測績效分析器。"""

    def __init__(self, config: Optional[AnalyzerConfig] = None) -> None:
        self.config = config or AnalyzerConfig()

    # ── NAV 型指標 (純序列輸入，便於手算驗證) ──────────────────────

    def compute_nav_metrics(
        self,
        initial_nav: Decimal,
        points: Sequence[NavPoint],
        *,
        benchmark: Optional[BenchmarkSeries] = None,
        nav_complete: bool = True,
    ) -> Tuple[Dict[str, MetricValue], str]:
        """計算報酬／風險／超額報酬指標，回傳 (指標字典, benchmark_status)。"""
        cfg = self.config
        out: Dict[str, MetricValue] = {}

        if not nav_complete:
            for k in _NAV_KEYS:
                out[k] = _na(k, MetricStatus.INCOMPLETE, "NAV 估值不完整 (MARK_INCOMPLETE)，拒絕計算以免填入假精確數值")
            bench_status = self._fill_benchmark_unavailable(out, "NAV 不完整，無法計算超額報酬")
            return out, bench_status

        validate_nav_series(initial_nav, points)
        n = len(points)
        a = cfg.trading_days_per_year

        if n == 0:
            for k in _NAV_KEYS:
                out[k] = _na(k, MetricStatus.NOT_AVAILABLE, "無任何 NAV 日期 (N=0)")
            bench_status = self._fill_benchmark_unavailable(out, "無 NAV 日期")
            return out, bench_status

        navs = [p.nav for p in points]
        v_n = navs[-1]
        rets = simple_returns(initial_nav, navs)
        rf = cfg.daily_risk_free

        # 1. 累積報酬率
        total_ret = (v_n - initial_nav) / initial_nav
        out["total_return"] = _ok("total_return", total_ret)

        # 2. CAGR
        cagr_val: Optional[float] = None
        try:
            cagr_val = float(v_n / initial_nav) ** (a / n) - 1.0
            if not math.isfinite(cagr_val):
                raise OverflowError
            note = "年化外推自短窗口，僅供參考" if n < cfg.short_window_days else ""
            out["cagr"] = _ok("cagr", cagr_val, note)
        except OverflowError:
            out["cagr"] = _na("cagr", MetricStatus.UNDEFINED, "年化外推數值溢位")
            cagr_val = None

        # 3. 年化波動率
        if n < 2:
            out["annualized_volatility"] = _na("annualized_volatility", MetricStatus.INSUFFICIENT_DATA, "N < 2")
            s_r: Optional[float] = None
        else:
            s_r = sample_std(rets)
            if s_r <= _FLOAT_EPS:
                s_r = 0.0
            out["annualized_volatility"] = _ok("annualized_volatility", s_r * math.sqrt(a))

        # 4. Sharpe
        excess = [r - rf for r in rets]
        if n < 2:
            out["sharpe_ratio"] = _na("sharpe_ratio", MetricStatus.INSUFFICIENT_DATA, "N < 2")
        else:
            s_e = sample_std(excess)
            if s_e <= _FLOAT_EPS:
                out["sharpe_ratio"] = _na("sharpe_ratio", MetricStatus.UNDEFINED, "N/A (Zero Volatility)")
            else:
                out["sharpe_ratio"] = _ok("sharpe_ratio", mean(excess) / s_e * math.sqrt(a))

        # 5. Sortino
        dd_daily = math.sqrt(math.fsum(min(0.0, e) ** 2 for e in excess) / n)
        if n < 2:
            out["sortino_ratio"] = _na("sortino_ratio", MetricStatus.INSUFFICIENT_DATA, "N < 2")
        elif dd_daily <= _FLOAT_EPS * 1e-3:
            out["sortino_ratio"] = _na("sortino_ratio", MetricStatus.UNDEFINED, "N/A (Zero Downside Deviation)")
        else:
            out["sortino_ratio"] = _ok("sortino_ratio", mean(excess) * a / (dd_daily * math.sqrt(a)))

        # 6/7. MDD 與最長回撤期間
        mdd, dur, recovered = max_drawdown_and_duration(initial_nav, navs)
        out["max_drawdown"] = _ok("max_drawdown", mdd)
        out["max_drawdown_duration"] = _ok(
            "max_drawdown_duration", dur, f"is_recovered={recovered}" if dur > 0 else "無回撤"
        )

        # 8. Calmar
        if cagr_val is None:
            out["calmar_ratio"] = _na("calmar_ratio", MetricStatus.UNDEFINED, "CAGR 未定義")
        elif mdd == _ZERO:
            out["calmar_ratio"] = _na("calmar_ratio", MetricStatus.UNDEFINED, "N/A (Zero Drawdown)")
        else:
            out["calmar_ratio"] = _ok("calmar_ratio", cagr_val / float(mdd))

        bench_status = self._compute_benchmark_metrics(
            out, initial_nav, points, rets, total_ret, cagr_val, benchmark
        )
        return out, bench_status

    # ── Benchmark 相對績效 ─────────────────────────────────────

    def _fill_benchmark_unavailable(self, out: Dict[str, MetricValue], note: str) -> str:
        for k in _BENCH_KEYS:
            out[k] = _na(k, MetricStatus.NOT_AVAILABLE, note)
        return "NOT_AVAILABLE"

    def _compute_benchmark_metrics(
        self,
        out: Dict[str, MetricValue],
        initial_nav: Decimal,
        points: Sequence[NavPoint],
        rets: Sequence[float],
        total_ret: Decimal,
        cagr_val: Optional[float],
        benchmark: Optional[BenchmarkSeries],
    ) -> str:
        cfg = self.config
        if benchmark is None:
            return self._fill_benchmark_unavailable(out, "未提供 Benchmark：無法計算真實超額報酬")
        if benchmark.basis != BenchmarkReturnBasis.TOTAL_RETURN:
            msg = (
                f"Benchmark {benchmark.name} 為 {benchmark.basis.value} 口徑 (benchmark_includes_dividends=False)，"
                "策略 NAV 含股利之 TOTAL_RETURN 口徑，不可直接比較超額報酬"
            )
            if cfg.strict_benchmark:
                raise BenchmarkAlignmentError(msg)
            for k in _BENCH_KEYS:
                out[k] = _na(k, MetricStatus.BASIS_MISMATCH, msg)
            return "BASIS_MISMATCH"
        try:
            base, levels = benchmark.align([p.date for p in points])
        except BenchmarkAlignmentError as exc:
            if cfg.strict_benchmark:
                raise
            for k in _BENCH_KEYS:
                out[k] = _na(k, MetricStatus.BASIS_MISMATCH, f"Benchmark 日期不一致: {exc.message}")
            return "DATE_MISMATCH"

        n = len(points)
        a = cfg.trading_days_per_year
        synth_note = "Benchmark 為合成資料，結果僅為測試用，非真實超額報酬" if benchmark.is_synthetic else ""
        b_rets = simple_returns(base, list(levels))
        b_total = (levels[-1] - base) / base
        out["excess_return_total"] = _ok("excess_return_total", total_ret - b_total, synth_note)
        try:
            b_cagr = float(levels[-1] / base) ** (a / n) - 1.0
            if cagr_val is None or not math.isfinite(b_cagr):
                raise OverflowError
            out["excess_return_annualized"] = _ok("excess_return_annualized", cagr_val - b_cagr, synth_note)
        except OverflowError:
            out["excess_return_annualized"] = _na("excess_return_annualized", MetricStatus.UNDEFINED, "CAGR 未定義")

        if n < 2:
            for k in ("tracking_error", "information_ratio", "beta"):
                out[k] = _na(k, MetricStatus.INSUFFICIENT_DATA, "N < 2")
        else:
            active = [r - b for r, b in zip(rets, b_rets)]
            s_a = sample_std(active)
            if s_a <= _FLOAT_EPS:
                out["tracking_error"] = _ok("tracking_error", 0.0, synth_note)
                out["information_ratio"] = _na("information_ratio", MetricStatus.UNDEFINED, "N/A (Zero Tracking Error)")
            else:
                out["tracking_error"] = _ok("tracking_error", s_a * math.sqrt(a), synth_note)
                out["information_ratio"] = _ok("information_ratio", mean(active) / s_a * math.sqrt(a), synth_note)
            var_b = sample_std(b_rets) ** 2
            if var_b <= _FLOAT_EPS ** 2:
                out["beta"] = _na("beta", MetricStatus.UNDEFINED, "N/A (Zero Benchmark Variance)")
            else:
                mr, mb = mean(rets), mean(b_rets)
                cov = math.fsum((r - mr) * (b - mb) for r, b in zip(rets, b_rets)) / (n - 1)
                out["beta"] = _ok("beta", cov / var_b, synth_note)
        return "SYNTHETIC_BENCHMARK" if benchmark.is_synthetic else "OK"

    # ── 交易行為指標 ──────────────────────────────────────────

    def compute_trade_metrics(
        self, trades: Sequence[ClosedLotTrade]
    ) -> Dict[str, MetricValue]:
        out: Dict[str, MetricValue] = {}
        k = len(trades)
        out["closed_lot_trade_count"] = _ok("closed_lot_trade_count", k)
        if k == 0:
            for key in ("win_rate", "average_win", "average_loss", "payoff_ratio", "profit_factor", "average_holding_days"):
                out[key] = _na(key, MetricStatus.NOT_AVAILABLE, "N/A (0 Closed Trades)")
            return out

        wins = [t.realized_pnl for t in trades if t.realized_pnl > _ZERO]
        losses = [-t.realized_pnl for t in trades if t.realized_pnl < _ZERO]
        out["win_rate"] = _ok(
            "win_rate", Decimal(len(wins)) / Decimal(k),
            f"wins={len(wins)}, losses={len(losses)}, flat={k - len(wins) - len(losses)}, K={k}",
        )
        avg_win = (sum(wins, _ZERO) / Decimal(len(wins))) if wins else None
        avg_loss = (sum(losses, _ZERO) / Decimal(len(losses))) if losses else None
        out["average_win"] = (
            _ok("average_win", avg_win) if avg_win is not None
            else _na("average_win", MetricStatus.NOT_AVAILABLE, "無獲利交易")
        )
        out["average_loss"] = (
            _ok("average_loss", avg_loss) if avg_loss is not None
            else _na("average_loss", MetricStatus.NOT_AVAILABLE, "無虧損交易")
        )
        if avg_win is None or avg_loss is None:
            out["payoff_ratio"] = _na("payoff_ratio", MetricStatus.UNDEFINED, "缺少獲利或虧損交易")
        else:
            out["payoff_ratio"] = _ok("payoff_ratio", avg_win / avg_loss)
        if not losses:
            out["profit_factor"] = _na("profit_factor", MetricStatus.UNDEFINED, "N/A (無虧損交易，分母為 0)")
        else:
            out["profit_factor"] = _ok("profit_factor", sum(wins, _ZERO) / sum(losses, _ZERO))

        if any(t.holding_trading_days is None for t in trades):
            out["average_holding_days"] = _na(
                "average_holding_days", MetricStatus.INCOMPLETE, "部分已平倉批次缺少 holding_trading_days"
            )
        else:
            simple = Decimal(sum(int(t.holding_trading_days) for t in trades)) / Decimal(k)  # type: ignore[arg-type]
            qty = sum(t.quantity for t in trades)
            weighted = Decimal(sum(t.quantity * int(t.holding_trading_days) for t in trades)) / Decimal(qty)  # type: ignore[arg-type]
            out["average_holding_days"] = _ok(
                "average_holding_days", simple, f"simple={simple}, share_weighted={weighted}"
            )
        return out

    # ── 完整分析 (輸入 BacktestRunResult) ──────────────────────────

    def analyze(
        self,
        result: BacktestRunResult,
        *,
        benchmark: Optional[BenchmarkSeries] = None,
    ) -> PerformanceMetrics:
        cfg = self.config
        warnings: List[str] = []
        if not isinstance(result, BacktestRunResult):
            raise BacktestMetricsError(f"result 必須為 BacktestRunResult，收到 {type(result).__name__}")

        # NAV 序列與完整性
        points: List[NavPoint] = []
        nav_complete = True
        incomplete_dates: List[dt.date] = []
        for rec in result.nav_history:
            if rec.total_nav is None or not rec.is_complete or not rec.is_official_nav:
                nav_complete = False
                incomplete_dates.append(rec.valuation_date)
                continue
            points.append(NavPoint(rec.valuation_date, rec.total_nav))
        if not nav_complete:
            msg = f"NAV 估值不完整日期: {[d.isoformat() for d in incomplete_dates[:5]]}"
            if cfg.incomplete_nav_policy == IncompleteNavPolicy.REJECT:
                raise InvalidNavSeriesError(msg, context={"incomplete_count": len(incomplete_dates)})
            warnings.append(msg + "；所有 NAV 型指標標示 INCOMPLETE")

        nav_dates = [r.valuation_date for r in result.nav_history]
        if tuple(nav_dates) != tuple(result.trading_days_executed):
            if cfg.incomplete_nav_policy == IncompleteNavPolicy.REJECT or nav_complete:
                raise InvalidNavSeriesError(
                    "nav_history 日期與 trading_days_executed 不一致 (缺漏、重複或未排序)",
                    context={"nav_count": len(nav_dates), "executed_count": len(result.trading_days_executed)},
                )

        if nav_complete:
            validate_nav_series(result.initial_cash, points)

        nav_metrics, bench_status = self.compute_nav_metrics(
            result.initial_cash, points, benchmark=benchmark, nav_complete=nav_complete
        )
        metrics: Dict[str, MetricValue] = dict(nav_metrics)

        if result.status != BacktestRunStatus.COMPLETED:
            warnings.append(f"回測狀態為 {result.status.value}：僅涵蓋已執行交易日，並非完整區間")
        if nav_complete and len(points) < cfg.short_window_days and points:
            warnings.append(f"樣本天數 N={len(points)} < {cfg.short_window_days}，年化指標屬短窗口外推")

        # 交易行為
        metrics.update(self.compute_trade_metrics(result.closed_lot_trades))

        # 逐筆成交歸因
        action_by_fill = self._fill_actions(result)
        fills = tuple(result.all_fills)
        metrics["fill_count"] = _ok("fill_count", len(fills))
        cost_break = self._cost_attribution(result, fills, action_by_fill, points, nav_complete, metrics, warnings)

        # 週轉率與日勝率
        self._turnover_and_daily_win(result, metrics, nav_complete)

        # 委託與風控統計
        unfilled_break = self._order_metrics(result, metrics)
        risk_break = self._risk_metrics(result, metrics)

        # 損益對帳
        recon = self._reconcile(result, warnings)

        breakdowns: Dict[str, Any] = {
            "cost_attribution": cost_break,
            "unfilled_orders": unfilled_break,
            "risk_rejections": risk_break,
        }
        return PerformanceMetrics(
            metrics=metrics,
            breakdowns=breakdowns,
            reconciliation=recon,
            nav_complete=nav_complete,
            nav_status="COMPLETE" if nav_complete else "INCOMPLETE",
            benchmark_status=bench_status,
            warnings=tuple(warnings),
            config=cfg,
            trading_day_count=len(result.nav_history),
        )

    # ── 內部：成交動作、成本、週轉、委託、風控、對帳 ───────────────────

    @staticmethod
    def _fill_actions(result: BacktestRunResult) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for log in result.daily_logs:
            for o in log.matched_order_outcomes:
                if o.fill_record is not None:
                    out[o.fill_record.fill_id] = o.action.value
        return out

    def _cost_attribution(
        self,
        result: BacktestRunResult,
        fills: Sequence[Any],
        action_by_fill: Mapping[str, str],
        points: Sequence[NavPoint],
        nav_complete: bool,
        metrics: Dict[str, MetricValue],
        warnings: List[str],
    ) -> Dict[str, Any]:
        cfg = self.config
        a = cfg.trading_days_per_year
        v0 = result.initial_cash
        unknown = [f.fill_id for f in fills if f.fill_id not in action_by_fill]
        buy_comm = sum((f.commission for f in fills if action_by_fill.get(f.fill_id) == "BUY"), _ZERO)
        sell_comm = sum((f.commission for f in fills if action_by_fill.get(f.fill_id) in ("SELL", "CLOSE")), _ZERO)
        total_tax = sum((f.tax for f in fills), _ZERO)
        total_slip = sum((f.slippage for f in fills), _ZERO)
        traded = sum((f.notional for f in fills), _ZERO)
        total_comm_all = sum((f.commission for f in fills), _ZERO)
        c_total = total_comm_all + total_tax

        dividends_paid = sum((d.net_dividend for d in result.paid_dividends), _ZERO)
        dividends_gross = sum((d.gross_dividend for d in result.paid_dividends), _ZERO)
        dividends_pending = sum((d.net_dividend for d in result.pending_dividends_at_end), _ZERO)

        if unknown:
            note = f"{len(unknown)} 筆成交無法對應買賣方向，買／賣手續費分列標示 INCOMPLETE"
            warnings.append(note)
            metrics["buy_commission"] = _na("buy_commission", MetricStatus.INCOMPLETE, note)
            metrics["sell_commission"] = _na("sell_commission", MetricStatus.INCOMPLETE, note)
        else:
            metrics["buy_commission"] = _ok("buy_commission", buy_comm)
            metrics["sell_commission"] = _ok("sell_commission", sell_comm)
        metrics["securities_transaction_tax"] = _ok("securities_transaction_tax", total_tax)
        metrics["slippage_cost_total"] = _ok(
            "slippage_cost_total", total_slip, "已內含於成交價，僅為歸因揭露，未再次扣除"
        )
        metrics["total_traded_notional"] = _ok("total_traded_notional", traded)
        metrics["dividend_income_paid"] = _ok(
            "dividend_income_paid", dividends_paid, "帳本實際已入帳淨股利；NAV 已含此項，不得與 NAV 報酬相加"
        )
        metrics["dividend_receivable_end"] = _ok("dividend_receivable_end", dividends_pending)

        # 方法論第 14 項
        if not fills:
            metrics["commission_tax_cost_drag"] = _ok("commission_tax_cost_drag", _ZERO, "無成交，稅費為 0 (非缺失)")
            metrics["cost_drag_annualized"] = _na("cost_drag_annualized", MetricStatus.NOT_AVAILABLE, "無成交")
            metrics["effective_fee_rate"] = _na("effective_fee_rate", MetricStatus.UNDEFINED, "成交金額為 0")
        else:
            metrics["commission_tax_cost_drag"] = _ok("commission_tax_cost_drag", c_total / v0, "佔初始資金比例")
            if nav_complete and points:
                avg_nav = sum((p.nav for p in points), _ZERO) / Decimal(len(points))
                metrics["cost_drag_annualized"] = _ok(
                    "cost_drag_annualized", float(c_total / avg_nav) * a / len(points)
                )
            else:
                metrics["cost_drag_annualized"] = _na(
                    "cost_drag_annualized", MetricStatus.INCOMPLETE, "NAV 不完整或無資料"
                )
            metrics["effective_fee_rate"] = (
                _ok("effective_fee_rate", c_total / traded) if traded > _ZERO
                else _na("effective_fee_rate", MetricStatus.UNDEFINED, "成交金額為 0")
            )
        # 方法論第 15 項
        metrics["slippage_cost_drag"] = _ok("slippage_cost_drag", total_slip / v0, "佔初始資金比例")

        # 已平倉批次層級：毛／淨損益 (淨損益已含費用，不重複扣除)
        trades = result.closed_lot_trades
        net_realized = sum((t.realized_pnl for t in trades), _ZERO)
        closed_fees = sum(
            (t.allocated_entry_commission + t.allocated_exit_commission + t.allocated_exit_tax for t in trades), _ZERO
        )
        metrics["realized_pnl_net"] = _ok("realized_pnl_net", net_realized, "已扣除手續費與證交稅")
        metrics["realized_pnl_gross_before_fees"] = _ok(
            "realized_pnl_gross_before_fees", net_realized + closed_fees, "= 淨損益 + 已分攤手續費與證交稅 (還原，非再扣一次)"
        )

        # 成本交叉核對：全部成交手續費 = 已平倉分攤 + 未平倉批次剩餘買進手續費 + 未平倉前已出清部分
        open_entry_comm = _ZERO
        for lots in result.open_lots_at_end.values():
            open_entry_comm += sum((lot.remaining_entry_commission for lot in lots), _ZERO)
        closed_comm = sum((t.allocated_entry_commission + t.allocated_exit_commission for t in trades), _ZERO)
        cost_residual = total_comm_all - closed_comm - open_entry_comm
        tax_closed = sum((t.allocated_exit_tax for t in trades), _ZERO)
        tax_residual = total_tax - tax_closed
        if abs(cost_residual) > cfg.reconciliation_tolerance or abs(tax_residual) > cfg.reconciliation_tolerance:
            warnings.append(
                f"成本交叉核對不符: 手續費殘差={cost_residual}, 證交稅殘差={tax_residual}"
            )
        return {
            "fill_total_commission": total_comm_all,
            "closed_lot_commission": closed_comm,
            "open_lot_entry_commission": open_entry_comm,
            "commission_residual": cost_residual,
            "fill_total_tax": total_tax,
            "closed_lot_tax": tax_closed,
            "tax_residual": tax_residual,
            "dividends_gross_paid": dividends_gross,
            "dividends_net_paid": dividends_paid,
            "unmapped_fill_count": len(unknown),
        }

    def _turnover_and_daily_win(
        self, result: BacktestRunResult, metrics: Dict[str, MetricValue], nav_complete: bool
    ) -> None:
        a = self.config.trading_days_per_year
        logs = result.daily_logs
        if not nav_complete:
            metrics["turnover"] = _na("turnover", MetricStatus.INCOMPLETE, "NAV 不完整")
            metrics["daily_win_rate"] = _na("daily_win_rate", MetricStatus.INCOMPLETE, "NAV 不完整")
            return
        if not logs:
            metrics["turnover"] = _na("turnover", MetricStatus.NOT_AVAILABLE, "無每日日誌")
            metrics["daily_win_rate"] = _na("daily_win_rate", MetricStatus.NOT_AVAILABLE, "無每日日誌")
            return
        daily = []
        for log in logs:
            traded = sum((f.notional for f in log.settled_fills), _ZERO)
            nav = log.nav_record.total_nav
            daily.append(float(traded / (Decimal(2) * nav)))  # type: ignore[operator]
        metrics["turnover"] = _ok("turnover", mean(daily) * a, f"mean_daily_single_sided={mean(daily)}")

        # 日勝率：有持倉或有成交之交易日中 r_t > 0 之比例
        prev_nav = result.initial_cash
        prev_has_pos = False
        active_days = 0
        win_days = 0
        for log in logs:
            nav = log.nav_record.total_nav
            r = nav / prev_nav - _ONE  # type: ignore[operator]
            has_pos = bool(log.nav_record.positions)
            if has_pos or prev_has_pos or log.settled_fills:
                active_days += 1
                if r > _ZERO:
                    win_days += 1
            prev_nav, prev_has_pos = nav, has_pos  # type: ignore[assignment]
        metrics["daily_win_rate"] = (
            _ok("daily_win_rate", Decimal(win_days) / Decimal(active_days), f"active_days={active_days}")
            if active_days
            else _na("daily_win_rate", MetricStatus.NOT_AVAILABLE, "無持倉或成交之交易日")
        )

    def _order_metrics(self, result: BacktestRunResult, metrics: Dict[str, MetricValue]) -> Dict[str, Any]:
        orders: Dict[str, Any] = {}
        for log in result.daily_logs:
            if log.dispatch_summary is not None:
                for o in log.dispatch_summary.approved_orders_queued:
                    orders[o.order_id] = o
        submitted = len(orders)
        resolved = [o for o in orders.values() if o.is_terminal]
        pending = submitted - len(resolved)
        reasons: Dict[str, int] = {}
        for log in result.daily_logs:
            for out in log.matched_order_outcomes:
                if out.filled_quantity == 0:
                    reasons[out.outcome_code.value] = reasons.get(out.outcome_code.value, 0) + 1
        brk: Dict[str, Any] = {
            "submitted": submitted, "resolved": len(resolved), "pending_at_end": pending,
            "unfilled_reason_counts": reasons,
        }
        if not resolved:
            msg = "無已結案委託" if submitted == 0 else "所有委託於期末前尚未結案"
            metrics["unfilled_order_ratio"] = _na("unfilled_order_ratio", MetricStatus.NOT_AVAILABLE, msg)
            metrics["volume_shortfall_ratio"] = _na("volume_shortfall_ratio", MetricStatus.NOT_AVAILABLE, msg)
            return brk
        from ..broker.order import PaperOrderStatus

        unfilled = [o for o in resolved if o.status == PaperOrderStatus.EXPIRED and o.filled_quantity == 0]
        partial = [o for o in resolved if 0 < o.filled_quantity < o.order_quantity]
        filled = [o for o in resolved if o.filled_quantity == o.order_quantity]
        brk.update({"unfilled": len(unfilled), "partial": len(partial), "filled": len(filled)})
        metrics["unfilled_order_ratio"] = _ok(
            "unfilled_order_ratio", Decimal(len(unfilled)) / Decimal(len(resolved)),
            f"unfilled={len(unfilled)}, partial={len(partial)}, filled={len(filled)}, pending={pending}",
        )
        tot_q = sum(o.order_quantity for o in resolved)
        tot_f = sum(o.filled_quantity for o in resolved)
        metrics["volume_shortfall_ratio"] = _ok("volume_shortfall_ratio", _ONE - Decimal(tot_f) / Decimal(tot_q))
        return brk

    def _risk_metrics(self, result: BacktestRunResult, metrics: Dict[str, MetricValue]) -> Dict[str, Any]:
        total = 0
        risk_rej = 0
        non_risk = 0
        by_code: Dict[str, int] = {}
        for log in result.daily_logs:
            ds = log.dispatch_summary
            if ds is None:
                continue
            for rec in ds.audit_records:
                total += 1
                if not rec.risk_decision.allowed:
                    risk_rej += 1
                    for code in rec.risk_decision.reason_codes:
                        by_code[code] = by_code.get(code, 0) + 1
                elif not rec.queued_for_next_day:
                    non_risk += 1
        brk = {"total_signals": total, "risk_rejected": risk_rej, "non_risk_rejections": non_risk, "by_code": by_code}
        if total == 0:
            metrics["risk_rejection_ratio"] = _na("risk_rejection_ratio", MetricStatus.NOT_AVAILABLE, "無任何送審訊號")
        else:
            metrics["risk_rejection_ratio"] = _ok(
                "risk_rejection_ratio", Decimal(risk_rej) / Decimal(total),
                f"risk_rejected={risk_rej}, non_risk_rejections={non_risk}, total={total}",
            )
        return brk

    def _reconcile(self, result: BacktestRunResult, warnings: List[str]) -> PnLReconciliation:
        cfg = self.config
        realized = sum((t.realized_pnl for t in result.closed_lot_trades), _ZERO)
        paid = sum((d.net_dividend for d in result.paid_dividends), _ZERO)
        recv_end = sum((d.net_dividend for d in result.pending_dividends_at_end), _ZERO)
        open_count = sum(1 for lots in result.open_lots_at_end.values() if lots)

        if not result.nav_history or result.nav_history[-1].total_nav is None or not result.nav_history[-1].is_complete:
            return PnLReconciliation(
                MetricStatus.INCOMPLETE, result.initial_cash, None, realized, None, paid, recv_end, None, None,
                open_count, "期末 NAV 不完整，無法對帳；未平倉未實現損益不可忽略",
            )
        last = result.nav_history[-1]
        # 未平倉部位與每日 NAV 一致性檢查
        for sym, lots in result.open_lots_at_end.items():
            lot_qty = sum(l.remaining_quantity for l in lots)
            nav_qty = sum(p.total_quantity for p in last.positions if p.symbol == sym)
            if lot_qty != nav_qty:
                raise InvalidNavSeriesError(
                    f"未平倉股數與期末 NAV 持倉不一致: {sym} lots={lot_qty}, nav={nav_qty}",
                    context={"symbol": sym},
                )
        nav_symbols = {p.symbol for p in last.positions if p.total_quantity > 0}
        lot_symbols = {s for s, lots in result.open_lots_at_end.items() if lots}
        if nav_symbols != lot_symbols:
            raise InvalidNavSeriesError(
                f"期末 NAV 持倉標的與未平倉批次不一致: nav={sorted(nav_symbols)}, lots={sorted(lot_symbols)}"
            )
        if last.market_value is None:
            return PnLReconciliation(
                MetricStatus.INCOMPLETE, result.initial_cash, last.total_nav, realized, None, paid, recv_end,
                None, None, open_count, "期末持倉市值缺失",
            )
        open_cost = sum(
            (l.remaining_cost_basis for lots in result.open_lots_at_end.values() for l in lots), _ZERO
        )
        unrealized = last.market_value - open_cost
        explained = realized + unrealized + paid + last.dividends_receivable
        residual = (last.total_nav - result.initial_cash) - explained
        if abs(residual) <= cfg.reconciliation_tolerance:
            status, note = MetricStatus.OK, "final_nav - initial = realized + unrealized + dividends_paid + dividends_receivable"
        else:
            status = MetricStatus.BASIS_MISMATCH
            note = f"損益對帳不符，殘差 {residual}"
            warnings.append(note)
        return PnLReconciliation(
            status, result.initial_cash, last.total_nav, realized, unrealized, paid,
            last.dividends_receivable, explained, residual, open_count, note,
        )
