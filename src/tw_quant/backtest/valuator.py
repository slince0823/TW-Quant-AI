"""台股歷史回測每日投資組合估值器模組 (valuator.py).

核心原則：
1. 輸出結構化不可變之 `DailyNAVRecord`，完整記錄估值日期、可用現金、預留現金、持倉市值、應收股利、總 NAV、持倉估值明細、價格來源與價格日期、缺價／停牌／過期警告，以及資料來源與合成／真實標記。
2. 嚴格防範資產重複計算或漏計：
   - 根據 `PortfolioLedger` 實際設計：`ledger.cash` 為扣除買單預留後之「可用現金」，`ledger.reserved_cash` 為「買單凍結預留現金」（兩者互斥不重疊）；同時支援透過 `CashAccountingBasis` 處理外部傳入已含預留之現金總額，杜絕重複加總。
   - 賣單預留股數 (`reserved_shares`) 屬於尚未成交賣出之凍結持倉，計入該標的之總持有股數 (`total_quantity = available_quantity + reserved_quantity`) 進行單次市值估算，絕不同時當作已賣出之現金與仍持有之股票。
   - 應收股利 (`dividends_receivable`) 僅計入狀態為 `PENDING` 之除息應收款；發放日 (`payable_date`) 轉入現金後自動歸零，確保帳務轉換前後總 NAV 守恆不變。
3. 嚴格價格有效性與過期控制：
   - 缺價時嚴禁默認價格為 0 或以買入均價靜默頂替。
   - 停牌股使用最後已知收盤價時，強制標記為「估值假設 (`is_tradable_price=False`)」、記錄確切 `price_date`、過期交易日數與結構化 `ValuationWarning`，絕不混充為當日可成交價格。
   - 價格過期天數上限 (`max_stale_trading_days`) 與缺價處理政策 (`MissingPricePolicy`) 完全可設定。
   - 當無法合理估值時，依設定直接拋出 `ValuationPriceMissingError` / `StaleValuationPriceError` 或將 `DailyNAVRecord` 標記為 `is_complete=False, is_official_nav=False, total_nav=None`，絕不靜默輸出看似精確的虛假 NAV。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from ..broker.ledger import PortfolioLedger
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .corporate_actions import CorporateActionProcessor
from .data_provider import (
    TWSE_MARKET_CLOSE_TIME,
    DataProvenance,
    HistoricalBarRecord,
    HistoricalDataProvider,
)
from .exceptions import (
    BacktestAccountingError,
    BacktestCalendarError,
    BacktestValuationError,
    DataProvenanceError,
    HistoricalDataMissingError,
    IncompleteValuationError,
    InvalidOHLCVError,
    LookaheadBiasError,
    StaleValuationPriceError,
    UnsupportedDataFormatError,
    ValuationPriceMissingError,
)
from .lot_ledger import BacktestLotLedger

_PLAIN_SYMBOL_RE = re.compile(r"^[0-9A-Z]{4,6}$")
_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


def _normalize_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise BacktestValuationError("symbol 必須為非空字串", context={"symbol": symbol})
    clean = symbol.strip().split(".")[0].strip()
    if not _PLAIN_SYMBOL_RE.match(clean):
        raise InvalidOHLCVError(
            f"無效的台股代號格式: {symbol!r}",
            symbol=clean,
            field_name="symbol",
            invalid_value=symbol,
        )
    return clean


def _validate_positive_price(value: object, symbol: str, valuation_date: dt.date, field_name: str = "price") -> Decimal:
    """驗證估值價格必須為 > 0 之有限 Decimal；嚴禁為 0、負數、float 或 NaN/Inf。"""
    if value is None:
        raise ValuationPriceMissingError(
            f"標的 {symbol} 於估值日 {valuation_date} 缺少有效價格 ({field_name}=None)，嚴禁默認為 0",
            symbol=symbol,
            trade_date=valuation_date,
            missing_fields=(field_name,),
        )
    if isinstance(value, bool):
        raise InvalidOHLCVError(
            f"估值價格 {field_name} 不得為 bool",
            symbol=symbol,
            trade_date=valuation_date,
            field_name=field_name,
            invalid_value=value,
        )
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidOHLCVError(
                f"估值價格 {field_name} 包含 NaN 或 Infinity: {value!r}",
                symbol=symbol,
                trade_date=valuation_date,
                field_name=field_name,
                invalid_value=value,
            )
        raise UnsupportedDataFormatError(
            f"估值價格 {field_name} 必須為 Decimal，禁止使用 float ({value!r})",
            format_type="float",
            context={"symbol": symbol, "valuation_date": valuation_date.isoformat()},
        )
    if isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, Decimal):
        dec = value
    else:
        raise InvalidOHLCVError(
            f"估值價格 {field_name} 型別錯誤: 收到 {type(value).__name__}",
            symbol=symbol,
            trade_date=valuation_date,
            field_name=field_name,
            invalid_value=value,
        )

    if dec.is_nan() or dec.is_infinite() or not dec.is_finite():
        raise InvalidOHLCVError(
            f"估值價格 {field_name} 不得為 NaN 或 Infinity: {dec!r}",
            symbol=symbol,
            trade_date=valuation_date,
            field_name=field_name,
            invalid_value=dec,
        )
    if dec <= _ZERO:
        raise InvalidOHLCVError(
            f"標的 {symbol} 於 {valuation_date} 之估值價格 {field_name} 必須嚴格大於 0 (收到 {dec})，嚴禁默認價格為 0",
            symbol=symbol,
            trade_date=valuation_date,
            field_name=field_name,
            invalid_value=dec,
        )
    return dec


# ── 1. 估值列舉與政策設定 ──────────────────────────────────────────────


class CashAccountingBasis(str, Enum):
    """現金與預留現金之會計輸入基準。"""

    # 與 PortfolioLedger 一致：傳入之 cash 為扣除買單預留後之「可用現金」，
    # 總現金 = available_cash + reserved_cash
    SEPARATED_AVAILABLE_AND_RESERVED = "SEPARATED_AVAILABLE_AND_RESERVED"
    # 外部傳入之 cash 總額已包含買單預留現金：
    # available_cash = cash_input - reserved_cash，總現金 = cash_input（不重複加計 reserved_cash）
    TOTAL_CASH_INCLUDES_RESERVED = "TOTAL_CASH_INCLUDES_RESERVED"


class ValuationPriceSource(str, Enum):
    """持倉估值價格來源分類。"""

    CURRENT_CLOSE = "CURRENT_CLOSE"                                # 當日正常收盤價 (可交易收盤行情)
    HALTED_CURRENT_BAR_CLOSE = "HALTED_CURRENT_BAR_CLOSE"          # 當日標記停牌之行情紀錄所載參考收盤價 (僅供估值假設，不可交易)
    LAST_KNOWN_CLOSE_HALTED = "LAST_KNOWN_CLOSE_HALTED"            # 停牌股回退使用之最後已知歷史收盤價 (僅供估值假設，不可交易)
    LAST_KNOWN_CLOSE_MISSING_BAR = "LAST_KNOWN_CLOSE_MISSING_BAR"  # 當日缺價但依政策回退使用之最後已知歷史收盤價 (僅供估值假設)
    UNAVAILABLE = "UNAVAILABLE"                                    # 無有效價格且無法回退（無法估值）


class MissingPricePolicy(str, Enum):
    """缺價或過期價格之處理政策。"""

    # 直接拋出 ValuationPriceMissingError 或 StaleValuationPriceError，拒絕產生 NAV
    REJECT_VALUATION = "REJECT_VALUATION"
    # 不拋出例外，但回傳 is_complete=False, is_official_nav=False, total_nav=None 之不完整紀錄，拒絕輸出虛假精確數字
    MARK_INCOMPLETE_NAV = "MARK_INCOMPLETE_NAV"
    # 允許在 max_stale_trading_days 期限內使用最後已知收盤價作為估值假設，並強制附帶警告與實際價格日期；
    # 若超過過期上限或無歷史價格，則依 fallback_on_stale_exceeded (REJECT 或 MARK_INCOMPLETE) 處理
    USE_LAST_KNOWN_CLOSE_WITH_WARNING = "USE_LAST_KNOWN_CLOSE_WITH_WARNING"


class ValuationWarningCode(str, Enum):
    """結構化估值警告代碼。"""

    HALTED_LAST_KNOWN_PRICE_USED = "HALTED_LAST_KNOWN_PRICE_USED"
    HALTED_BAR_PRICE_USED = "HALTED_BAR_PRICE_USED"
    MISSING_BAR_LAST_KNOWN_PRICE_USED = "MISSING_BAR_LAST_KNOWN_PRICE_USED"
    STALE_PRICE_LIMIT_EXCEEDED = "STALE_PRICE_LIMIT_EXCEEDED"
    MISSING_VALUATION_PRICE = "MISSING_VALUATION_PRICE"


@dataclass(frozen=True)
class ValuationWarning:
    """單筆估值警告紀錄（不可變）。"""

    code: ValuationWarningCode
    symbol: str
    valuation_date: dt.date
    price_date: Optional[dt.date]
    staleness_trading_days: Optional[int]
    staleness_calendar_days: Optional[int]
    message: str


@dataclass(frozen=True)
class ValuationConfig:
    """`PortfolioValuator` 估值規則配置。"""

    cash_basis: CashAccountingBasis = CashAccountingBasis.SEPARATED_AVAILABLE_AND_RESERVED
    halted_price_policy: MissingPricePolicy = MissingPricePolicy.USE_LAST_KNOWN_CLOSE_WITH_WARNING
    missing_price_policy: MissingPricePolicy = MissingPricePolicy.REJECT_VALUATION
    stale_exceeded_policy: MissingPricePolicy = MissingPricePolicy.REJECT_VALUATION
    max_stale_trading_days: int = 20
    max_stale_calendar_days: Optional[int] = None
    verify_lot_ledger_consistency: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.cash_basis, CashAccountingBasis):
            raise BacktestValuationError(f"不支援的 cash_basis: {self.cash_basis!r}")
        for policy_name in ("halted_price_policy", "missing_price_policy", "stale_exceeded_policy"):
            pol = getattr(self, policy_name)
            if not isinstance(pol, MissingPricePolicy):
                raise BacktestValuationError(f"{policy_name} 必須為 MissingPricePolicy，收到 {pol!r}")
        if self.stale_exceeded_policy == MissingPricePolicy.USE_LAST_KNOWN_CLOSE_WITH_WARNING:
            raise BacktestValuationError(
                "stale_exceeded_policy 僅能設定為 REJECT_VALUATION 或 MARK_INCOMPLETE_NAV"
            )
        if (
            isinstance(self.max_stale_trading_days, bool)
            or not isinstance(self.max_stale_trading_days, int)
            or self.max_stale_trading_days < 0
        ):
            raise BacktestValuationError(
                f"max_stale_trading_days 必須為 >= 0 之整數，收到 {self.max_stale_trading_days!r}"
            )
        if self.max_stale_calendar_days is not None:
            if (
                isinstance(self.max_stale_calendar_days, bool)
                or not isinstance(self.max_stale_calendar_days, int)
                or self.max_stale_calendar_days < 0
            ):
                raise BacktestValuationError(
                    f"max_stale_calendar_days 必須為 >= 0 之整數或 None，收到 {self.max_stale_calendar_days!r}"
                )


# ── 2. 持倉估值明細與每日 NAV 結構化紀錄 ──────────────────────────────


@dataclass(frozen=True)
class PositionValuationItem:
    """單一持倉標的之收盤估值明細（不可變）。

    重要不變量：
    - `total_quantity == available_quantity + reserved_quantity`
    - `reserved_quantity` 為未成交賣單已預留之股數，仍屬於帳上持有資產，不得重複加計亦不得提前視為已變現現金。
    - 當 `is_halted=True` 或 `is_stale_price=True` 時，`is_tradable_price` 強制為 `False`，
      明確標示該價格僅作為估值假設，不可視為當日可成交價格。
    """

    symbol: str
    total_quantity: int
    available_quantity: int
    reserved_quantity: int
    valuation_price: Optional[Decimal]
    price_date: Optional[dt.date]
    price_source: ValuationPriceSource
    staleness_trading_days: Optional[int]
    staleness_calendar_days: Optional[int]
    is_halted: bool
    is_stale_price: bool
    is_tradable_price: bool
    market_value: Optional[Decimal]
    cost_basis: Decimal
    unrealized_pnl: Optional[Decimal]


@dataclass(frozen=True)
class DailyNAVRecord:
    """每日投資組合估值結構化紀錄 (`DailyNAVRecord`).

    核心資產恆等式（當 `is_complete=True` 時）：
    `total_nav = available_cash + reserved_cash + market_value + dividends_receivable`
    """

    valuation_date: dt.date
    valuation_timestamp: dt.datetime
    available_cash: Decimal
    reserved_cash: Decimal
    total_cash: Decimal
    market_value: Optional[Decimal]
    dividends_receivable: Decimal
    total_nav: Optional[Decimal]
    is_complete: bool
    is_official_nav: bool
    positions: Tuple[PositionValuationItem, ...]
    price_sources: Mapping[str, ValuationPriceSource]
    price_dates: Mapping[str, Optional[dt.date]]
    tradable_close_prices: Mapping[str, Decimal]
    valuation_prices: Mapping[str, Decimal]
    halted_symbols: Tuple[str, ...]
    missing_price_symbols: Tuple[str, ...]
    stale_price_symbols: Tuple[str, ...]
    unvaluable_symbols: Tuple[str, ...]
    warnings: Tuple[ValuationWarning, ...]
    provenance: DataProvenance
    is_synthetic: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_sources", MappingProxyType(dict(self.price_sources)))
        object.__setattr__(self, "price_dates", MappingProxyType(dict(self.price_dates)))
        object.__setattr__(self, "tradable_close_prices", MappingProxyType(dict(self.tradable_close_prices)))
        object.__setattr__(self, "valuation_prices", MappingProxyType(dict(self.valuation_prices)))

    def require_official_nav(self) -> Decimal:
        """取得可作為正式回測結算與風控基準之 `total_nav`；若估值不完整則立即拋出 `IncompleteValuationError`。"""
        if not self.is_complete or not self.is_official_nav or self.total_nav is None:
            raise IncompleteValuationError(
                f"估值日 {self.valuation_date} 之投資組合估值不完整 (unvaluable_symbols={self.unvaluable_symbols})，"
                f"拒絕輸出虛假精確之正式 NAV",
                context={
                    "valuation_date": self.valuation_date.isoformat(),
                    "is_complete": self.is_complete,
                    "unvaluable_symbols": self.unvaluable_symbols,
                },
            )
        return self.total_nav


# ── 3. 每日投資組合估值器主體 (PortfolioValuator) ──────────────────────


class PortfolioValuator:
    """台股歷史回測每日投資組合估值器 (`PortfolioValuator`)."""

    def __init__(
        self,
        calendar: TradingCalendar,
        provenance: DataProvenance,
        *,
        config: Optional[ValuationConfig] = None,
        data_provider: Optional[HistoricalDataProvider] = None,
    ) -> None:
        if not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須是 TradingCalendar 實例，收到 {type(calendar).__name__}")
        calendar.assert_ready(allow_synthetic=True)

        if not isinstance(provenance, DataProvenance):
            raise DataProvenanceError(f"provenance 必須是 DataProvenance 實例，收到 {type(provenance).__name__}")

        if calendar.is_synthetic != provenance.is_synthetic:
            raise DataProvenanceError(
                f"交易日曆 (is_synthetic={calendar.is_synthetic}) 與估值器資料血統 "
                f"(is_synthetic={provenance.is_synthetic}) 之合成／真實標記不一致，嚴禁混用",
                source=provenance.source,
                dataset_version=provenance.dataset_version,
                is_synthetic=provenance.is_synthetic,
            )

        self.calendar: TradingCalendar = calendar
        self.provenance: DataProvenance = provenance
        self.config: ValuationConfig = config or ValuationConfig()
        self.data_provider: Optional[HistoricalDataProvider] = data_provider

        # 記錄各標的最後已知之有效收盤價 (symbol -> (price_date, close_price))
        self._last_known_closes: Dict[str, Tuple[dt.date, Decimal]] = {}
        # 估值歷史紀錄 (valuation_date -> DailyNAVRecord)
        self._nav_history_by_date: Dict[dt.date, DailyNAVRecord] = {}
        self._last_valuation_date: Optional[dt.date] = None

    @property
    def is_synthetic(self) -> bool:
        return self.provenance.is_synthetic

    @property
    def nav_history(self) -> Tuple[DailyNAVRecord, ...]:
        return tuple(self._nav_history_by_date[d] for d in sorted(self._nav_history_by_date.keys()))

    def record_observed_close(self, symbol: str, trade_date: dt.date, close_price: Decimal) -> None:
        """手動登記已確認之歷史交易日收盤價（供停牌股回退查詢最後已知價格）。"""
        clean_sym = _normalize_symbol(symbol)
        t_date = to_taipei_date(trade_date, "trade_date", allow_datetime=False)
        if not self.calendar.is_trading_day(t_date):
            raise BacktestCalendarError(
                f"登記最後已知收盤價之日期 {t_date} 非有效交易日",
                context={"symbol": clean_sym, "trade_date": t_date.isoformat()},
            )
        px = _validate_positive_price(close_price, clean_sym, t_date, "close_price")
        existing = self._last_known_closes.get(clean_sym)
        if existing is None or t_date >= existing[0]:
            self._last_known_closes[clean_sym] = (t_date, px)

    def _resolve_cash_components(
        self,
        cash_input: Decimal,
        reserved_cash_input: Decimal,
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """依據 `CashAccountingBasis` 解析 `(available_cash, reserved_cash, total_cash)`，防止重複加總預留現金。"""
        if isinstance(cash_input, bool) or not isinstance(cash_input, Decimal) or not cash_input.is_finite():
            raise BacktestValuationError(f"cash 必須為有限 Decimal，收到 {cash_input!r}")
        if (
            isinstance(reserved_cash_input, bool)
            or not isinstance(reserved_cash_input, Decimal)
            or not reserved_cash_input.is_finite()
        ):
            raise BacktestValuationError(f"reserved_cash 必須為有限 Decimal，收到 {reserved_cash_input!r}")
        if cash_input < _ZERO:
            raise BacktestValuationError(f"cash 不得為負數，收到 {cash_input}")
        if reserved_cash_input < _ZERO:
            raise BacktestValuationError(f"reserved_cash 不得為負數，收到 {reserved_cash_input}")

        if self.config.cash_basis == CashAccountingBasis.SEPARATED_AVAILABLE_AND_RESERVED:
            avail = cash_input.quantize(_CENT, rounding=ROUND_HALF_UP)
            res = reserved_cash_input.quantize(_CENT, rounding=ROUND_HALF_UP)
            return avail, res, avail + res

        # TOTAL_CASH_INCLUDES_RESERVED: 傳入之 cash_input 已包含 reserved_cash_input
        if cash_input < reserved_cash_input:
            raise BacktestValuationError(
                f"TOTAL_CASH_INCLUDES_RESERVED 模式下，總現金 ({cash_input}) 不得小於預留現金 ({reserved_cash_input})"
            )
        total = cash_input.quantize(_CENT, rounding=ROUND_HALF_UP)
        res = reserved_cash_input.quantize(_CENT, rounding=ROUND_HALF_UP)
        avail = total - res
        return avail, res, total

    def _lookup_last_known_close(
        self,
        symbol: str,
        valuation_date: dt.date,
        cutoff_time: dt.datetime,
    ) -> Optional[Tuple[dt.date, Decimal]]:
        """查詢嚴格早於 `valuation_date` 之最後已知有效收盤價 `(price_date, close_price)`。"""
        candidates: List[Tuple[dt.date, Decimal]] = []

        cached = self._last_known_closes.get(symbol)
        if cached is not None and cached[0] < valuation_date:
            candidates.append(cached)

        if self.data_provider is not None:
            prev_bars = self.data_provider.get_available_bars(
                symbol,
                cutoff_time=cutoff_time,
                end_date=valuation_date - dt.timedelta(days=1),
                require_pit_verified=True,
                require_real_data=not self.is_synthetic,
            )
            for b in reversed(prev_bars):
                if b.trade_date < valuation_date and not b.is_halted and b.close > _ZERO:
                    candidates.append((b.trade_date, b.close))
                    break

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[-1]

    def evaluate_daily_nav(
        self,
        valuation_date: dt.date,
        portfolio_ledger: PortfolioLedger,
        *,
        market_bars: Optional[Mapping[str, HistoricalBarRecord]] = None,
        close_prices: Optional[Mapping[str, Decimal]] = None,
        halted_symbols: Optional[Iterable[str]] = None,
        lot_ledger: Optional[BacktestLotLedger] = None,
        corporate_action_processor: Optional[CorporateActionProcessor] = None,
        valuation_timestamp: Optional[dt.datetime] = None,
    ) -> DailyNAVRecord:
        """計算指定交易日 `valuation_date` 之完整結構化 `DailyNAVRecord`。"""
        if not isinstance(portfolio_ledger, PortfolioLedger):
            raise BacktestValuationError(
                f"portfolio_ledger 必須為 PortfolioLedger 實例，收到 {type(portfolio_ledger).__name__}"
            )

        val_date = to_taipei_date(valuation_date, "valuation_date", allow_datetime=False)
        if not self.calendar.is_trading_day(val_date):
            raise BacktestCalendarError(
                f"估值日期 {val_date} 在交易日曆中非有效交易日，禁止執行正式收盤估值",
                context={"valuation_date": val_date.isoformat()},
            )

        if valuation_timestamp is not None:
            val_ts = ensure_taipei_datetime(valuation_timestamp, "valuation_timestamp")
            if val_ts.date() != val_date:
                raise BacktestValuationError(
                    f"valuation_timestamp 日期 ({val_ts.date()}) 與 valuation_date ({val_date}) 不一致",
                    context={"valuation_date": val_date.isoformat(), "valuation_timestamp": val_ts.isoformat()},
                )
            close_dt = dt.datetime.combine(val_date, TWSE_MARKET_CLOSE_TIME, tzinfo=TAIPEI_TZ)
            if val_ts < close_dt:
                raise LookaheadBiasError(
                    f"收盤估值時間 ({val_ts}) 不得早於當日 13:30 收盤 ({close_dt})",
                    trade_date=val_date,
                    cutoff_time=val_ts,
                )
        else:
            # 預設於當日 13:35+08:00 收盤後執行估值
            val_ts = dt.datetime.combine(val_date, dt.time(13, 35, 0), tzinfo=TAIPEI_TZ)

        # 1. 若有提供 BacktestLotLedger，交叉驗證持倉總股數完全一致
        if lot_ledger is not None and self.config.verify_lot_ledger_consistency:
            lot_ledger.assert_reconciled_with_portfolio_ledger(
                portfolio_ledger, check_realized_pnl_when_flat=False
            )

        # 2. 解析可用現金與預留現金（防止重複加總）
        avail_cash, res_cash, total_cash = self._resolve_cash_components(
            portfolio_ledger.cash, portfolio_ledger.reserved_cash
        )

        # 3. 解析應收股利（僅計入 PENDING 狀態，絕不重複計入已發放之 PAID 股利）
        div_receivable = _ZERO
        if corporate_action_processor is not None:
            if not isinstance(corporate_action_processor, CorporateActionProcessor):
                raise BacktestValuationError("corporate_action_processor 必須是 CorporateActionProcessor 實例")
            for rec in corporate_action_processor.get_pending_receivables():
                if rec.is_synthetic != self.is_synthetic:
                    raise DataProvenanceError(
                        f"應收股利 {rec.receivable_id} (is_synthetic={rec.is_synthetic}) 與估值器 "
                        f"(is_synthetic={self.is_synthetic}) 之真偽標記不一致",
                        source=self.provenance.source,
                        dataset_version=self.provenance.dataset_version,
                        is_synthetic=self.is_synthetic,
                    )
                div_receivable += rec.net_dividend

        # 4. 彙整當日行情輸入與停牌集合
        explicit_halted: Set[str] = {_normalize_symbol(s) for s in (halted_symbols or ())}
        bars_by_sym: Dict[str, HistoricalBarRecord] = {}
        if market_bars is not None:
            for k, bar in market_bars.items():
                if not isinstance(bar, HistoricalBarRecord):
                    raise UnsupportedDataFormatError(
                        f"market_bars[{k!r}] 必須是 HistoricalBarRecord 實例",
                        format_type=type(bar).__name__,
                    )
                if bar.provenance.is_synthetic != self.is_synthetic:
                    raise DataProvenanceError(
                        f"標的 {bar.symbol} 行情 (is_synthetic={bar.provenance.is_synthetic}) 與估值器 "
                        f"(is_synthetic={self.is_synthetic}) 之合成／真實標記衝突",
                        source=bar.provenance.source,
                        dataset_version=bar.provenance.dataset_version,
                        is_synthetic=bar.provenance.is_synthetic,
                    )
                if bar.trade_date > val_date:
                    raise LookaheadBiasError(
                        f"估值日 {val_date} 收盤估值混入未來日期行情: {bar.symbol} ({bar.trade_date})",
                        symbol=bar.symbol,
                        trade_date=bar.trade_date,
                        cutoff_time=val_ts,
                    )
                if bar.trade_date == val_date:
                    bars_by_sym[bar.symbol] = bar
                    if bar.is_halted:
                        explicit_halted.add(bar.symbol)
                    else:
                        self.record_observed_close(bar.symbol, val_date, bar.close)
                else:
                    # 過去日期的歷史行情可更新最後已知收盤價
                    if not bar.is_halted:
                        self.record_observed_close(bar.symbol, bar.trade_date, bar.close)

        prices_by_sym: Dict[str, Decimal] = {}
        if close_prices is not None:
            for k, raw_px in close_prices.items():
                clean_k = _normalize_symbol(k)
                valid_px = _validate_positive_price(raw_px, clean_k, val_date, f"close_prices[{clean_k!r}]")
                prices_by_sym[clean_k] = valid_px
                if clean_k not in explicit_halted:
                    self.record_observed_close(clean_k, val_date, valid_px)

        # 5. 逐一估值所有持倉部位
        position_items: List[PositionValuationItem] = []
        price_sources: Dict[str, ValuationPriceSource] = {}
        price_dates: Dict[str, Optional[dt.date]] = {}
        tradable_prices: Dict[str, Decimal] = {}
        valuation_prices: Dict[str, Decimal] = {}
        halted_held_list: List[str] = []
        missing_price_list: List[str] = []
        stale_price_list: List[str] = []
        unvaluable_list: List[str] = []
        warnings: List[ValuationWarning] = []

        total_market_value = _ZERO
        is_complete = True

        # 檢查是否有預留賣出股數卻無對應持倉或超過持倉（Rule 2 防呆）
        for res_sym, res_qty in portfolio_ledger.reserved_shares.items():
            if res_qty < 0:
                raise BacktestValuationError(f"標的 {res_sym} 之 reserved_shares 不得為負數 ({res_qty})")
            if res_qty > 0:
                pos_obj = portfolio_ledger.positions.get(res_sym)
                held_q = pos_obj.quantity if pos_obj is not None else 0
                if res_qty > held_q:
                    raise BacktestValuationError(
                        f"標的 {res_sym} 之預留賣出股數 ({res_qty}) 超過實際總持倉股數 ({held_q})，違反持倉守恆",
                        context={"symbol": res_sym, "reserved_shares": res_qty, "held_quantity": held_q},
                    )

        for raw_sym in sorted(portfolio_ledger.positions.keys()):
            pos = portfolio_ledger.positions[raw_sym]
            if pos.quantity <= 0:
                continue

            clean_sym = _normalize_symbol(raw_sym)
            total_qty = pos.quantity
            res_qty = portfolio_ledger.reserved_shares.get(
                raw_sym, portfolio_ledger.reserved_shares.get(clean_sym, 0)
            )
            if res_qty < 0 or res_qty > total_qty:
                raise BacktestValuationError(
                    f"標的 {clean_sym} 預留股數 ({res_qty}) 不合法 (總持倉={total_qty})"
                )
            avail_qty = total_qty - res_qty

            cost_basis = (
                lot_ledger.get_position_cost_basis(clean_sym)
                if lot_ledger is not None
                else pos.total_cost
            )

            # 若未在 market_bars / close_prices 提供，且綁定了 data_provider，嘗試從 data_provider 讀取當日行情
            bar_today = bars_by_sym.get(clean_sym)
            if bar_today is None and clean_sym not in prices_by_sym and self.data_provider is not None:
                try:
                    fetched_bar = self.data_provider.get_bar(
                        clean_sym,
                        val_date,
                        cutoff_time=val_ts,
                        require_pit_verified=True,
                        require_real_data=not self.is_synthetic,
                    )
                    bar_today = fetched_bar
                    bars_by_sym[clean_sym] = fetched_bar
                    if fetched_bar.is_halted:
                        explicit_halted.add(clean_sym)
                    else:
                        self.record_observed_close(clean_sym, val_date, fetched_bar.close)
                except HistoricalDataMissingError:
                    bar_today = None

            sym_is_halted = (clean_sym in explicit_halted) or (bar_today is not None and bar_today.is_halted)

            # 情況 A：當日正常交易且有當日有效收盤價
            if not sym_is_halted and (bar_today is not None or clean_sym in prices_by_sym):
                px = bar_today.close if bar_today is not None else prices_by_sym[clean_sym]
                mkt_val = (px * Decimal(total_qty)).quantize(_CENT, rounding=ROUND_HALF_UP)
                unreal_pnl = mkt_val - cost_basis
                total_market_value += mkt_val

                price_sources[clean_sym] = ValuationPriceSource.CURRENT_CLOSE
                price_dates[clean_sym] = val_date
                tradable_prices[clean_sym] = px
                valuation_prices[clean_sym] = px

                position_items.append(
                    PositionValuationItem(
                        symbol=clean_sym,
                        total_quantity=total_qty,
                        available_quantity=avail_qty,
                        reserved_quantity=res_qty,
                        valuation_price=px,
                        price_date=val_date,
                        price_source=ValuationPriceSource.CURRENT_CLOSE,
                        staleness_trading_days=0,
                        staleness_calendar_days=0,
                        is_halted=False,
                        is_stale_price=False,
                        is_tradable_price=True,
                        market_value=mkt_val,
                        cost_basis=cost_basis,
                        unrealized_pnl=unreal_pnl,
                    )
                )
                continue

            # 情況 B：當日為停牌股 (sym_is_halted=True) 或當日缺價
            if sym_is_halted:
                halted_held_list.append(clean_sym)
                active_policy = self.config.halted_price_policy
            else:
                missing_price_list.append(clean_sym)
                active_policy = self.config.missing_price_policy

            if active_policy == MissingPricePolicy.REJECT_VALUATION:
                raise ValuationPriceMissingError(
                    f"標的 {clean_sym} 於估值日 {val_date} {'處於停牌狀態' if sym_is_halted else '缺少當日收盤報價'}，"
                    f"依政策 {active_policy.value} 拒絕產生估值",
                    symbol=clean_sym,
                    trade_date=val_date,
                    missing_fields=("close_price",),
                )

            if active_policy == MissingPricePolicy.MARK_INCOMPLETE_NAV:
                is_complete = False
                unvaluable_list.append(clean_sym)
                price_sources[clean_sym] = ValuationPriceSource.UNAVAILABLE
                price_dates[clean_sym] = None
                warnings.append(
                    ValuationWarning(
                        code=(
                            ValuationWarningCode.HALTED_LAST_KNOWN_PRICE_USED
                            if sym_is_halted
                            else ValuationWarningCode.MISSING_VALUATION_PRICE
                        ),
                        symbol=clean_sym,
                        valuation_date=val_date,
                        price_date=None,
                        staleness_trading_days=None,
                        staleness_calendar_days=None,
                        message=(
                            f"標的 {clean_sym} 於 {val_date} {'停牌' if sym_is_halted else '缺少當日報價'}，"
                            f"依 MARK_INCOMPLETE_NAV 政策標記為無法估值"
                        ),
                    )
                )
                position_items.append(
                    PositionValuationItem(
                        symbol=clean_sym,
                        total_quantity=total_qty,
                        available_quantity=avail_qty,
                        reserved_quantity=res_qty,
                        valuation_price=None,
                        price_date=None,
                        price_source=ValuationPriceSource.UNAVAILABLE,
                        staleness_trading_days=None,
                        staleness_calendar_days=None,
                        is_halted=sym_is_halted,
                        is_stale_price=True,
                        is_tradable_price=False,
                        market_value=None,
                        cost_basis=cost_basis,
                        unrealized_pnl=None,
                    )
                )
                continue

            # active_policy == USE_LAST_KNOWN_CLOSE_WITH_WARNING
            last_known = self._lookup_last_known_close(clean_sym, val_date, val_ts)
            fallback_source = (
                ValuationPriceSource.LAST_KNOWN_CLOSE_HALTED
                if sym_is_halted
                else ValuationPriceSource.LAST_KNOWN_CLOSE_MISSING_BAR
            )

            # 若無歷史前收，但當日有停牌 Bar 且載有正數平盤/前收價，可作為已知前收（其日期為前一交易日）
            if last_known is None and sym_is_halted and bar_today is not None:
                ref_c = bar_today.yesterday_close or bar_today.close
                if ref_c is not None and ref_c > _ZERO:
                    try:
                        prev_td = self.calendar.previous_trading_day(val_date)
                    except BacktestCalendarError:
                        prev_td = val_date
                    last_known = (prev_td, ref_c)
                    fallback_source = ValuationPriceSource.HALTED_CURRENT_BAR_CLOSE

            if last_known is None:
                if self.config.stale_exceeded_policy == MissingPricePolicy.REJECT_VALUATION:
                    raise ValuationPriceMissingError(
                        f"標的 {clean_sym} 於 {val_date} {'停牌' if sym_is_halted else '缺價'}，"
                        f"且查無任何歷史最後已知收盤價，拒絕默認價格為 0",
                        symbol=clean_sym,
                        trade_date=val_date,
                        missing_fields=("last_known_close",),
                    )
                is_complete = False
                unvaluable_list.append(clean_sym)
                price_sources[clean_sym] = ValuationPriceSource.UNAVAILABLE
                price_dates[clean_sym] = None
                warnings.append(
                    ValuationWarning(
                        code=ValuationWarningCode.MISSING_VALUATION_PRICE,
                        symbol=clean_sym,
                        valuation_date=val_date,
                        price_date=None,
                        staleness_trading_days=None,
                        staleness_calendar_days=None,
                        message=f"標的 {clean_sym} 於 {val_date} 查無最後已知收盤價，標記估值不完整",
                    )
                )
                position_items.append(
                    PositionValuationItem(
                        symbol=clean_sym,
                        total_quantity=total_qty,
                        available_quantity=avail_qty,
                        reserved_quantity=res_qty,
                        valuation_price=None,
                        price_date=None,
                        price_source=ValuationPriceSource.UNAVAILABLE,
                        staleness_trading_days=None,
                        staleness_calendar_days=None,
                        is_halted=sym_is_halted,
                        is_stale_price=True,
                        is_tradable_price=False,
                        market_value=None,
                        cost_basis=cost_basis,
                        unrealized_pnl=None,
                    )
                )
                continue

            px_date, px_val = last_known
            stale_trad_days = self.calendar.trading_days_between(px_date, val_date)
            stale_cal_days = (val_date - px_date).days
            stale_price_list.append(clean_sym)

            exceeded_trading = stale_trad_days > self.config.max_stale_trading_days
            exceeded_calendar = (
                self.config.max_stale_calendar_days is not None
                and stale_cal_days > self.config.max_stale_calendar_days
            )

            if exceeded_trading or exceeded_calendar:
                msg = (
                    f"標的 {clean_sym} 於 {val_date} 之最後已知價格 ({px_val} @ {px_date}) "
                    f"已過期 {stale_trad_days} 個交易日 ({stale_cal_days} 日曆日)，"
                    f"超過允許上限 (max_stale_trading_days={self.config.max_stale_trading_days})"
                )
                if self.config.stale_exceeded_policy == MissingPricePolicy.REJECT_VALUATION:
                    raise StaleValuationPriceError(
                        msg,
                        context={
                            "symbol": clean_sym,
                            "valuation_date": val_date.isoformat(),
                            "price_date": px_date.isoformat(),
                            "staleness_trading_days": stale_trad_days,
                            "max_stale_trading_days": self.config.max_stale_trading_days,
                        },
                    )
                # MARK_INCOMPLETE_NAV
                is_complete = False
                unvaluable_list.append(clean_sym)
                price_sources[clean_sym] = ValuationPriceSource.UNAVAILABLE
                price_dates[clean_sym] = px_date
                warnings.append(
                    ValuationWarning(
                        code=ValuationWarningCode.STALE_PRICE_LIMIT_EXCEEDED,
                        symbol=clean_sym,
                        valuation_date=val_date,
                        price_date=px_date,
                        staleness_trading_days=stale_trad_days,
                        staleness_calendar_days=stale_cal_days,
                        message=msg,
                    )
                )
                position_items.append(
                    PositionValuationItem(
                        symbol=clean_sym,
                        total_quantity=total_qty,
                        available_quantity=avail_qty,
                        reserved_quantity=res_qty,
                        valuation_price=None,
                        price_date=px_date,
                        price_source=ValuationPriceSource.UNAVAILABLE,
                        staleness_trading_days=stale_trad_days,
                        staleness_calendar_days=stale_cal_days,
                        is_halted=sym_is_halted,
                        is_stale_price=True,
                        is_tradable_price=False,
                        market_value=None,
                        cost_basis=cost_basis,
                        unrealized_pnl=None,
                    )
                )
                continue

            # 在過期容許範圍內：使用最後已知收盤價作為帶有明確日期與警告之估值假設（絕不放入 tradable_prices）
            mkt_val = (px_val * Decimal(total_qty)).quantize(_CENT, rounding=ROUND_HALF_UP)
            unreal_pnl = mkt_val - cost_basis
            total_market_value += mkt_val

            price_sources[clean_sym] = fallback_source
            price_dates[clean_sym] = px_date
            valuation_prices[clean_sym] = px_val

            warn_code = (
                ValuationWarningCode.HALTED_LAST_KNOWN_PRICE_USED
                if sym_is_halted
                else ValuationWarningCode.MISSING_BAR_LAST_KNOWN_PRICE_USED
            )
            warnings.append(
                ValuationWarning(
                    code=warn_code,
                    symbol=clean_sym,
                    valuation_date=val_date,
                    price_date=px_date,
                    staleness_trading_days=stale_trad_days,
                    staleness_calendar_days=stale_cal_days,
                    message=(
                        f"標的 {clean_sym} 於 {val_date} {'停牌' if sym_is_halted else '缺少當日報價'}，"
                        f"採用 {px_date} 之最後已知收盤價 {px_val} 作為估值假設 "
                        f"(過期 {stale_trad_days} 交易日；不可視為當日可成交價格)"
                    ),
                )
            )

            position_items.append(
                PositionValuationItem(
                    symbol=clean_sym,
                    total_quantity=total_qty,
                    available_quantity=avail_qty,
                    reserved_quantity=res_qty,
                    valuation_price=px_val,
                    price_date=px_date,
                    price_source=fallback_source,
                    staleness_trading_days=stale_trad_days,
                    staleness_calendar_days=stale_cal_days,
                    is_halted=sym_is_halted,
                    is_stale_price=True,
                    is_tradable_price=False,
                    market_value=mkt_val,
                    cost_basis=cost_basis,
                    unrealized_pnl=unreal_pnl,
                )
            )

        final_mkt_val: Optional[Decimal] = total_market_value if is_complete else None
        final_nav: Optional[Decimal] = (
            (total_cash + total_market_value + div_receivable) if is_complete else None
        )

        record = DailyNAVRecord(
            valuation_date=val_date,
            valuation_timestamp=val_ts,
            available_cash=avail_cash,
            reserved_cash=res_cash,
            total_cash=total_cash,
            market_value=final_mkt_val,
            dividends_receivable=div_receivable,
            total_nav=final_nav,
            is_complete=is_complete,
            is_official_nav=is_complete,
            positions=tuple(position_items),
            price_sources=price_sources,
            price_dates=price_dates,
            tradable_close_prices=tradable_prices,
            valuation_prices=valuation_prices,
            halted_symbols=tuple(sorted(set(halted_held_list))),
            missing_price_symbols=tuple(sorted(set(missing_price_list))),
            stale_price_symbols=tuple(sorted(set(stale_price_list))),
            unvaluable_symbols=tuple(sorted(set(unvaluable_list))),
            warnings=tuple(warnings),
            provenance=self.provenance,
            is_synthetic=self.is_synthetic,
        )

        self._nav_history_by_date[val_date] = record
        self._last_valuation_date = val_date
        return record
