"""台股歷史回測日線撮合模擬器模組 (simulator.py).

核心原則：
1. 預設採用 `DAILY_OPEN_CONSERVATIVE`（日線開盤保守撮合模式）：
   - T-1 盤後（或 T 日 09:00 開盤前）產生之有效委託，僅能在 T 日或之後的合法交易時段撮合。
   - 嚴格攔截「T-1 盤後委託於 T-1 當日成交」以及「利用 T 日盤後收盤資訊於 T 日收盤成交」之前視偏差。
2. 明確區分市價單 (`MARKET`) 與限價單 (`LIMIT`)：
   - 市價單以當日開盤價 (`bar.open`) 為基準，加計滑價並對齊台股 Tick Size，且受當日漲跌停限制約束。
   - 限價買單成交價絕不高於 `limit_price`；限價賣單成交價絕不低於 `limit_price`。
   - 開盤跳空穿越限價時（買單 `bar.open <= limit_price`、賣單 `bar.open >= limit_price`），以開盤基準加滑價保守撮合（且不劣於限價）。
   - 日線 OHLC 不代表完整日內價格路徑與委託簿排隊順序；當日內觸價存在歧義時，依可配置之保守規則 (`AmbiguousLimitPolicy`) 明確處理並記錄原因。
3. 可注入之台股市場微結構規則（不硬編碼單一固定 ±10% 或單一股票費率）：
   - `PriceLimitRule`：依參考價、商品類別 (`InstrumentCategory`)、上市階段 (`ListingPhase`) 與有效日期 (`trade_date`) 計算漲跌停或無漲跌幅限制；缺少必要資料時回報 `UNKNOWN_DATA` 並拋出 `PriceLimitDataMissingError`，絕不自行推測。
   - `TickSizeRule`：支援台股一般股票 6 級升降單位、ETF 升降單位與自訂規則驗證。
   - 明確處理停牌 (`HALTED`)、處置 (`DISPOSITION`)、全額交割／限制 (`RESTRICTED`) 與未知交易狀態 (`UNKNOWN`)；未知狀態嚴禁自動視為正常可交易。
4. 流動性參與率與部分成交、到期釋放：
   - 成交量單位強制為整數「股 (`SHARES`)」，嚴禁把整日成交量 (`bar.volume`) 視為 09:00 開盤已知資訊。
   - 支援部分成交 (`PARTIALLY_FILLED`)、完全未成交與收盤到期 (`EXPIRED`)。
   - 未成交或部分成交剩餘委託到期／取消時，精確釋放剩餘預留現金與預留股數，且強制保證「僅能釋放一次」（冪等單次釋放防護）。
   - 賣出成交嚴格檢查可供出售之實際持倉與已扣除其他委託預留後的股數，杜絕超賣。
5. 單一成交成本責任層：
   - 手續費、證交稅與滑價僅在產生 `FillRecord` 時由 `ExecutionCostModel` 計算一次，並可直接交由 `PortfolioLedger` 與 `BacktestLotLedger` 入帳，絕不重複扣費。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from enum import Enum
import re
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, runtime_checkable

from ..broker.config import TaiwanPaperConfig
from ..broker.ledger import PortfolioLedger
from ..broker.order import FillRecord, PaperOrder, PaperOrderStatus
from ..contracts.signal import SignalAction, SignalOrderType
from ..strategy.config import CostModel as StrategyCostModel
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .data_provider import (
    TWSE_MARKET_CLOSE_TIME,
    TWSE_MARKET_OPEN_TIME,
    HistoricalBarRecord,
    PriceBasis,
)
from .exceptions import (
    BacktestAccountingError,
    BacktestCalendarError,
    BacktestExecutionError,
    DuplicateFillError,
    DuplicateOrderExecutionError,
    HistoricalDataMissingError,
    InvalidFillRecordError,
    InvalidOHLCVError,
    LookaheadBiasError,
    OrderReservationError,
    OversellPositionError,
    PriceLimitDataMissingError,
    PriceLimitError,
    TickSizeViolationError,
    UnknownTradingStatusError,
    UnsupportedDataFormatError,
)
from .lot_ledger import BacktestLotLedger

_PLAIN_SYMBOL_RE = re.compile(r"^[0-9A-Z]{4,6}$")
_ZERO = Decimal("0.00")
_ONE = Decimal("1")
_CENT = Decimal("0.01")


def _normalize_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise HistoricalDataMissingError("symbol 必須為非空字串", missing_fields=("symbol",))
    clean = symbol.strip().split(".")[0].strip()
    if not _PLAIN_SYMBOL_RE.match(clean):
        raise InvalidOHLCVError(
            f"無效的台股代號格式: {symbol!r}，必須為 4~6 碼英數字代號",
            symbol=clean,
            field_name="symbol",
            invalid_value=symbol,
        )
    return clean


# ── 1. 商品類別、上市階段、交易狀態與撮合模式列舉 ──────────────────────


class ExecutionMode(str, Enum):
    """歷史日線撮合模式。"""

    DAILY_OPEN_CONSERVATIVE = "DAILY_OPEN_CONSERVATIVE"


class AmbiguousLimitPolicy(str, Enum):
    """當日線 OHLC 無法唯一確定日內價格路徑與排隊順序時之限價單保守處理政策。"""

    # 開盤未觸及、但盤中極值觸及或穿過限價時，保守以委託限價 (limit_price) 成交（絕不給予更優之 low/high/close）
    FILL_AT_LIMIT_PRICE = "FILL_AT_LIMIT_PRICE"
    # 嚴格要求日內極值必須「實質穿透」限價 (買單 bar.low < limit_price；賣單 bar.high > limit_price)；
    # 若僅剛好觸及邊界 (bar.low == limit_price 或 bar.high == limit_price)，因無法確認排隊成交，保守視為未成交
    REQUIRE_STRICT_PENETRATION = "REQUIRE_STRICT_PENETRATION"
    # 極度保守：僅允許開盤價本身已滿足限價 (買單 bar.open <= limit；賣單 bar.open >= limit) 時成交；
    # 所有盤中才觸及限價之情況一律視為日內路徑未知而不予成交
    OPEN_ONLY_REJECT_INTRADAY = "OPEN_ONLY_REJECT_INTRADAY"


class InstrumentCategory(str, Enum):
    """台股商品類別列舉（決定適用之 Tick Size、證交稅率與漲跌幅基準）。"""

    COMMON_STOCK = "COMMON_STOCK"          # 一般上市櫃普通股
    ETF = "ETF"                            # 一般指數股票型基金 (0050, 0056 等)
    LEVERAGED_ETF = "LEVERAGED_ETF"        # 槓桿／反向型 ETF
    EMERGING_STOCK = "EMERGING_STOCK"      # 興櫃股票
    WARRANT = "WARRANT"                    # 權證
    UNKNOWN = "UNKNOWN"                    # 未知商品類別（嚴禁自動套用預設規則）


class ListingPhase(str, Enum):
    """股票上市櫃階段列舉（決定是否適用無漲跌幅限制或特殊漲跌幅規則）。"""

    REGULAR_LISTED = "REGULAR_LISTED"          # 一般正常上市櫃交易期
    IPO_NO_LIMIT_PERIOD = "IPO_NO_LIMIT_PERIOD"  # 初次上市櫃掛牌首五個交易日（無漲跌幅限制）
    EMERGING_NO_LIMIT = "EMERGING_NO_LIMIT"    # 興櫃一般交易期（無漲跌幅限制）
    RELISTING_NO_LIMIT = "RELISTING_NO_LIMIT"  # 特殊恢復買賣／減資換股首日無漲跌幅限制
    CUSTOM_REGIME = "CUSTOM_REGIME"            # 自訂特殊漲跌幅階段（需由規則明確指定比例）
    UNKNOWN = "UNKNOWN"                        # 上市階段未知（嚴禁自行推測）


class InstrumentTradingStatus(str, Enum):
    """單一交易日標的交易狀態。"""

    NORMAL = "NORMAL"                          # 正常交易
    HALTED = "HALTED"                          # 停牌／暫停交易
    DISPOSITION = "DISPOSITION"                # 處置股（分盤交易／限制量能）
    FULL_CASH_DELIVERY = "FULL_CASH_DELIVERY"  # 變更交易方法（全額交割）
    RESTRICTED = "RESTRICTED"                  # 其他禁止或限制交易狀態
    DELISTED = "DELISTED"                      # 已終止上市／下市
    UNKNOWN = "UNKNOWN"                        # 交易狀態未知（嚴禁自動視為正常可交易）


class DispositionExecutionPolicy(str, Enum):
    """處置股 (`DISPOSITION`) 撮合政策。"""

    ALLOW_WITH_VOLUME_HAIRCUT = "ALLOW_WITH_VOLUME_HAIRCUT"  # 允許撮合，但可成交量上限折半
    BLOCK_NEW_BUY = "BLOCK_NEW_BUY"                          # 禁止買進，僅允許賣出平倉（且量能折半）
    BLOCK_ALL = "BLOCK_ALL"                                  # 處置期間完全禁止買賣撮合


class VolumeCapPolicy(str, Enum):
    """日線流動性與成交量參與率上限政策。

    重要方法論原則：
    - 日線之 `bar.volume` 為當日 09:00~13:30 累計總成交量，並非 09:00 開盤時已知資訊。
    - 嚴禁將整日成交量當作開盤已知資訊來放大委託或繞過事前流動性限制。
    """

    # 預設保守雙重上限：取 min(ADV20_T-1, Volume_T) * participation_rate。
    # 其中 ADV20_T-1 為截至 T-1 收盤已知之事前均量，Volume_T 僅作為當日實際物理總成交量天花板。
    CAUSAL_ADV_AND_DAILY_BOUND = "CAUSAL_ADV_AND_DAILY_BOUND"
    # 嚴格事前均量模式：必須提供 T-1 已知之 ADV20_T-1，缺少時立即拋出 HistoricalDataMissingError；
    # 可成交上限為 min(ADV20_T-1 * participation_rate, Volume_T)。
    STRICT_EX_ANTE_ADV_ONLY = "STRICT_EX_ANTE_ADV_ONLY"
    # 開盤競價量模式：僅使用明確提供之開盤集合競價量 opening_volume_shares * participation_rate。
    OPENING_AUCTION_VOLUME_ONLY = "OPENING_AUCTION_VOLUME_ONLY"
    # 事後日線近似模式：僅在無 ADV20_T-1 且顯式允許時使用 Volume_T * participation_rate，
    # 並強制於審計紀錄標記為「事後日線近似假設 (EX_POST_DAILY_VOLUME_APPROXIMATION)」。
    EX_POST_DAILY_VOLUME_APPROXIMATION = "EX_POST_DAILY_VOLUME_APPROXIMATION"


class TickRoundingMode(str, Enum):
    """Tick Size 取整方向。"""

    FLOOR = "FLOOR"      # 向下對齊合法 Tick（如賣單滑價後、漲停價計算）
    CEILING = "CEILING"  # 向上對齊合法 Tick（如買單滑價後、跌停價計算）
    NEAREST = "NEAREST"  # 四捨五入至最近合法 Tick
    EXACT = "EXACT"      # 必須本身即為合法 Tick，否則拋出 TickSizeViolationError


class ExecutionOutcomeCode(str, Enum):
    """單筆委託撮合結果原因代碼。"""

    FILLED = "FILLED"
    PARTIALLY_FILLED_VOLUME_CAP = "PARTIALLY_FILLED_VOLUME_CAP"
    PARTIALLY_FILLED_ODD_LOT_ROUNDING = "PARTIALLY_FILLED_ODD_LOT_ROUNDING"
    UNFILLED_HALTED = "UNFILLED_HALTED"
    UNFILLED_DISPOSITION_BLOCKED = "UNFILLED_DISPOSITION_BLOCKED"
    UNFILLED_RESTRICTED_STATUS = "UNFILLED_RESTRICTED_STATUS"
    UNFILLED_LIMIT_UP_LOCKED = "UNFILLED_LIMIT_UP_LOCKED"
    UNFILLED_LIMIT_DOWN_LOCKED = "UNFILLED_LIMIT_DOWN_LOCKED"
    UNFILLED_LIMIT_PRICE_NOT_REACHED = "UNFILLED_LIMIT_PRICE_NOT_REACHED"
    UNFILLED_LIMIT_OUTSIDE_DAILY_BOUNDS = "UNFILLED_LIMIT_OUTSIDE_DAILY_BOUNDS"
    UNFILLED_AMBIGUOUS_INTRADAY_OHLC = "UNFILLED_AMBIGUOUS_INTRADAY_OHLC"
    UNFILLED_VOLUME_CAP_ZERO = "UNFILLED_VOLUME_CAP_ZERO"
    REJECTED_INVALID_TICK_SIZE = "REJECTED_INVALID_TICK_SIZE"
    REJECTED_INSUFFICIENT_RESERVATION = "REJECTED_INSUFFICIENT_RESERVATION"


# ── 2. Tick Size 規則介面與台股實作 ─────────────────────────────────────


@runtime_checkable
class TickSizeRule(Protocol):
    """價格升降單位 (Tick Size) 驗證與對齊規則介面。"""

    def get_tick_size(
        self,
        price: Decimal,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
    ) -> Decimal:
        """取得指定價格與商品類別適用之單一升降單位。"""
        ...

    def is_valid_tick(
        self,
        price: Decimal,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
    ) -> bool:
        """檢查價格是否符合適用之升降單位。"""
        ...

    def validate_tick(
        self,
        price: Decimal,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
        *,
        field_name: str = "price",
        symbol: Optional[str] = None,
    ) -> Decimal:
        """驗證價格必須符合 Tick Size，否則拋出 `TickSizeViolationError`。"""
        ...

    def align_to_tick(
        self,
        price: Decimal,
        mode: TickRoundingMode,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
    ) -> Decimal:
        """將價格依指定方向對齊至合法 Tick Size。"""
        ...


# 台股普通股/興櫃標準 6 級升降單位表: (價格上限不含, Tick Size)
DEFAULT_TWSE_STOCK_TICK_TIERS: Tuple[Tuple[Optional[Decimal], Decimal], ...] = (
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.10")),
    (Decimal("500"), Decimal("0.50")),
    (Decimal("1000"), Decimal("1.00")),
    (None, Decimal("5.00")),
)

# 台股 ETF 標準 2 級升降單位表
DEFAULT_TWSE_ETF_TICK_TIERS: Tuple[Tuple[Optional[Decimal], Decimal], ...] = (
    (Decimal("50"), Decimal("0.01")),
    (None, Decimal("0.05")),
)

# 台股權證升降單位表
DEFAULT_TWSE_WARRANT_TICK_TIERS: Tuple[Tuple[Optional[Decimal], Decimal], ...] = (
    (Decimal("5"), Decimal("0.01")),
    (Decimal("10"), Decimal("0.05")),
    (Decimal("50"), Decimal("0.10")),
    (Decimal("100"), Decimal("0.50")),
    (Decimal("500"), Decimal("1.00")),
    (None, Decimal("5.00")),
)


class TaiwanTickSizeRule:
    """台股可配置多商品類別升降單位 (Tick Size) 規則實作。"""

    def __init__(
        self,
        tiers_by_category: Optional[
            Mapping[InstrumentCategory, Sequence[Tuple[Optional[Decimal], Decimal]]]
        ] = None,
    ) -> None:
        default_map: Dict[InstrumentCategory, Tuple[Tuple[Optional[Decimal], Decimal], ...]] = {
            InstrumentCategory.COMMON_STOCK: DEFAULT_TWSE_STOCK_TICK_TIERS,
            InstrumentCategory.EMERGING_STOCK: DEFAULT_TWSE_STOCK_TICK_TIERS,
            InstrumentCategory.ETF: DEFAULT_TWSE_ETF_TICK_TIERS,
            InstrumentCategory.LEVERAGED_ETF: DEFAULT_TWSE_ETF_TICK_TIERS,
            InstrumentCategory.WARRANT: DEFAULT_TWSE_WARRANT_TICK_TIERS,
        }
        if tiers_by_category is not None:
            for cat, tiers in tiers_by_category.items():
                default_map[cat] = tuple(tiers)
        self._tiers_by_category = default_map

    def _get_tiers(
        self,
        instrument_category: InstrumentCategory,
    ) -> Tuple[Tuple[Optional[Decimal], Decimal], ...]:
        if not isinstance(instrument_category, InstrumentCategory) or instrument_category == InstrumentCategory.UNKNOWN:
            raise TickSizeViolationError(
                f"未知的商品類別 ({instrument_category!r})，無法決定適用之 Tick Size 規則",
                context={"instrument_category": getattr(instrument_category, "value", repr(instrument_category))},
            )
        tiers = self._tiers_by_category.get(instrument_category)
        if not tiers:
            raise TickSizeViolationError(
                f"商品類別 {instrument_category.value} 尚未配置 Tick Size 級距表",
                context={"instrument_category": instrument_category.value},
            )
        return tiers

    def get_tick_size(
        self,
        price: Decimal,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
    ) -> Decimal:
        if isinstance(price, bool) or not isinstance(price, Decimal) or not price.is_finite() or price <= _ZERO:
            raise TickSizeViolationError(
                f"計算 Tick Size 之價格必須為 > 0 的有限 Decimal，收到 {price!r}",
                context={"price": repr(price)},
            )
        tiers = self._get_tiers(instrument_category)
        for upper_bound, tick in tiers:
            if upper_bound is None or price < upper_bound:
                return tick
        return tiers[-1][1]

    def is_valid_tick(
        self,
        price: Decimal,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
    ) -> bool:
        if isinstance(price, bool) or not isinstance(price, Decimal) or not price.is_finite() or price <= _ZERO:
            return False
        try:
            tick = self.get_tick_size(price, instrument_category)
        except TickSizeViolationError:
            return False
        remainder = price % tick
        return remainder == Decimal("0")

    def validate_tick(
        self,
        price: Decimal,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
        *,
        field_name: str = "price",
        symbol: Optional[str] = None,
    ) -> Decimal:
        if isinstance(price, bool) or not isinstance(price, Decimal) or not price.is_finite() or price <= _ZERO:
            raise TickSizeViolationError(
                f"{field_name} 必須為 > 0 的有限 Decimal，收到 {price!r}",
                context={"symbol": symbol, "field_name": field_name, "price": repr(price)},
            )
        tick = self.get_tick_size(price, instrument_category)
        if (price % tick) != Decimal("0"):
            raise TickSizeViolationError(
                f"價格 {field_name}={price} 不符合商品類別 {instrument_category.value} 之升降單位 (Tick Size={tick})",
                context={
                    "symbol": symbol,
                    "field_name": field_name,
                    "price": str(price),
                    "tick_size": str(tick),
                    "instrument_category": instrument_category.value,
                },
            )
        return price

    def align_to_tick(
        self,
        price: Decimal,
        mode: TickRoundingMode,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
    ) -> Decimal:
        """對齊價格至合法 Tick Size，正確處理跨價格級距邊界之情況。"""
        if isinstance(price, bool) or not isinstance(price, Decimal) or not price.is_finite() or price <= _ZERO:
            raise TickSizeViolationError(
                f"對齊 Tick Size 之價格必須為 > 0 的有限 Decimal，收到 {price!r}",
                context={"price": repr(price)},
            )
        if mode == TickRoundingMode.EXACT:
            return self.validate_tick(price, instrument_category)

        tiers = self._get_tiers(instrument_category)
        if mode == TickRoundingMode.FLOOR:
            # 若價格剛好落在某級距下界（如 10.00, 50.00, 100.00, 500.00, 1000.00），其本身即為合法檔位
            tick = self.get_tick_size(price, instrument_category)
            units = (price / tick).to_integral_value(rounding=ROUND_FLOOR)
            snapped = (units * tick).quantize(tick)
            if snapped <= _ZERO:
                return tiers[0][1]
            # 若向下取整後落入下一低價級距，重新以低價級距計算
            new_tick = self.get_tick_size(snapped, instrument_category)
            if new_tick != tick:
                units2 = (price / new_tick).to_integral_value(rounding=ROUND_FLOOR)
                snapped = (units2 * new_tick).quantize(new_tick)
            return snapped

        if mode == TickRoundingMode.CEILING:
            tick = self.get_tick_size(price, instrument_category)
            units = (price / tick).to_integral_value(rounding=ROUND_CEILING)
            snapped = (units * tick).quantize(tick)
            # 若向上取整後跨越至高價級距，且非剛好落在邊界上，重新以高價級距對齊
            new_tick = self.get_tick_size(snapped, instrument_category)
            if new_tick != tick and (snapped % new_tick) != Decimal("0"):
                units2 = (snapped / new_tick).to_integral_value(rounding=ROUND_CEILING)
                snapped = (units2 * new_tick).quantize(new_tick)
            return snapped

        if mode == TickRoundingMode.NEAREST:
            tick = self.get_tick_size(price, instrument_category)
            units = (price / tick).to_integral_value(rounding=ROUND_HALF_UP)
            snapped = (units * tick).quantize(tick)
            if snapped <= _ZERO:
                return tiers[0][1]
            new_tick = self.get_tick_size(snapped, instrument_category)
            if new_tick != tick and (snapped % new_tick) != Decimal("0"):
                units2 = (snapped / new_tick).to_integral_value(rounding=ROUND_HALF_UP)
                snapped = (units2 * new_tick).quantize(new_tick)
            return snapped

        raise TickSizeViolationError(f"不支援的 TickRoundingMode: {mode!r}")


# ── 3. 可注入之價格限制規則介面 (PriceLimitRule) 與實作 ───────────────────


class PriceLimitStatus(str, Enum):
    """單日漲跌幅限制解析狀態。"""

    APPLIED = "APPLIED"                # 正常適用漲跌停上下限 (limit_up, limit_down)
    NO_PRICE_LIMIT = "NO_PRICE_LIMIT"  # 依上市階段或商品規則明確無漲跌幅限制 (如新股首五日、興櫃)
    UNKNOWN_DATA = "UNKNOWN_DATA"      # 缺少參考價、未知商品類別、未知上市階段或未知歷史法規日期（嚴禁推測）


@dataclass(frozen=True)
class PriceLimitBounds:
    """單一標的於單一交易日之漲跌幅限制計算結果。"""

    symbol: str
    trade_date: dt.date
    status: PriceLimitStatus
    reference_price: Optional[Decimal]
    limit_up: Optional[Decimal]
    limit_down: Optional[Decimal]
    limit_rate: Optional[Decimal]
    instrument_category: InstrumentCategory
    listing_phase: ListingPhase
    rule_name: str
    reason: str = ""

    @property
    def has_limits(self) -> bool:
        return self.status == PriceLimitStatus.APPLIED and self.limit_up is not None and self.limit_down is not None


@runtime_checkable
class PriceLimitRule(Protocol):
    """可注入之台股價格限制規則介面。

    不得硬編碼固定 ±10% 於所有股票與所有日期；
    必須依據參考價 (`reference_price`)、商品類別 (`instrument_category`)、
    上市階段 (`listing_phase`) 與有效日期 (`trade_date`) 決定。
    """

    def resolve_price_limits(
        self,
        symbol: str,
        trade_date: dt.date,
        reference_price: Optional[Decimal],
        *,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
        listing_phase: ListingPhase = ListingPhase.REGULAR_LISTED,
        tick_rule: Optional[TickSizeRule] = None,
        custom_limit_rate: Optional[Decimal] = None,
    ) -> PriceLimitBounds:
        ...


@dataclass(frozen=True)
class HistoricalPriceLimitBand:
    """歷史法規生效區間之漲跌幅限制設定。"""

    effective_from: dt.date
    effective_until: Optional[dt.date]
    base_limit_rate: Decimal
    regime_name: str
    leveraged_etf_multiplier: Decimal = Decimal("1")


# 台股歷史漲跌幅法規演進預設表（可由外部自訂覆寫）：
# - 1989-10-11 ~ 2015-05-31: ±7%
# - 2015-06-01 起: ±10%
# 早於 1989-10-11 若未特別注入規則，視為 UNKNOWN_DATA（拒絕以 ±10% 猜測早期歷史）。
DEFAULT_TWSE_HISTORICAL_LIMIT_SCHEDULE: Tuple[HistoricalPriceLimitBand, ...] = (
    HistoricalPriceLimitBand(
        effective_from=dt.date(1989, 10, 11),
        effective_until=dt.date(2015, 5, 31),
        base_limit_rate=Decimal("0.07"),
        regime_name="TWSE_1989_2015_7PCT",
    ),
    HistoricalPriceLimitBand(
        effective_from=dt.date(2015, 6, 1),
        effective_until=None,
        base_limit_rate=Decimal("0.10"),
        regime_name="TWSE_2015_PRESENT_10PCT",
    ),
)


class TaiwanHistoricalPriceLimitRule:
    """台股可配置之歷史與多階段漲跌幅限制規則。

    支援：
    1. 正常漲跌幅限制 (`PriceLimitStatus.APPLIED`)：依有效日期區間（如 2015/06/01 前 7%、之後 10%）
       及商品類別、參考價與 Tick Size 計算漲跌停價。
    2. 無漲跌幅限制 (`PriceLimitStatus.NO_PRICE_LIMIT`)：
       - 初次上市櫃首五個交易日 (`ListingPhase.IPO_NO_LIMIT_PERIOD`)
       - 興櫃股票 (`InstrumentCategory.EMERGING_STOCK` 或 `ListingPhase.EMERGING_NO_LIMIT`)
       - 減資／合併恢復買賣首日無漲跌幅限制 (`ListingPhase.RELISTING_NO_LIMIT`)
    3. 資料未知 (`PriceLimitStatus.UNKNOWN_DATA`)：
       - 缺少 `reference_price`（或 `reference_price <= 0`）
       - `instrument_category == InstrumentCategory.UNKNOWN` 或 `listing_phase == ListingPhase.UNKNOWN`
       - `trade_date` 不在任何已知法規生效區間內
       - `ListingPhase.CUSTOM_REGIME` 但未提供 `custom_limit_rate`
    """

    def __init__(
        self,
        schedule: Sequence[HistoricalPriceLimitBand] = DEFAULT_TWSE_HISTORICAL_LIMIT_SCHEDULE,
        *,
        align_limits_to_tick: bool = True,
        require_reference_price_for_no_limit: bool = False,
    ) -> None:
        if not schedule:
            raise PriceLimitError("漲跌幅歷史規則表 schedule 不得為空")
        self._schedule: Tuple[HistoricalPriceLimitBand, ...] = tuple(schedule)
        self.align_limits_to_tick: bool = bool(align_limits_to_tick)
        self.require_reference_price_for_no_limit: bool = bool(require_reference_price_for_no_limit)

    def _find_band_for_date(self, trade_date: dt.date) -> Optional[HistoricalPriceLimitBand]:
        for band in self._schedule:
            if trade_date >= band.effective_from and (
                band.effective_until is None or trade_date <= band.effective_until
            ):
                return band
        return None

    def resolve_price_limits(
        self,
        symbol: str,
        trade_date: dt.date,
        reference_price: Optional[Decimal],
        *,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
        listing_phase: ListingPhase = ListingPhase.REGULAR_LISTED,
        tick_rule: Optional[TickSizeRule] = None,
        custom_limit_rate: Optional[Decimal] = None,
    ) -> PriceLimitBounds:
        clean_sym = _normalize_symbol(symbol)
        t_date = to_taipei_date(trade_date, "trade_date", allow_datetime=False)

        # 1. 檢查未知商品類別或未知上市階段
        if not isinstance(instrument_category, InstrumentCategory) or instrument_category == InstrumentCategory.UNKNOWN:
            return PriceLimitBounds(
                symbol=clean_sym,
                trade_date=t_date,
                status=PriceLimitStatus.UNKNOWN_DATA,
                reference_price=reference_price,
                limit_up=None,
                limit_down=None,
                limit_rate=None,
                instrument_category=InstrumentCategory.UNKNOWN,
                listing_phase=listing_phase if isinstance(listing_phase, ListingPhase) else ListingPhase.UNKNOWN,
                rule_name="TaiwanHistoricalPriceLimitRule",
                reason="商品類別為 UNKNOWN，拒絕自行推測漲跌幅限制",
            )

        if not isinstance(listing_phase, ListingPhase) or listing_phase == ListingPhase.UNKNOWN:
            return PriceLimitBounds(
                symbol=clean_sym,
                trade_date=t_date,
                status=PriceLimitStatus.UNKNOWN_DATA,
                reference_price=reference_price,
                limit_up=None,
                limit_down=None,
                limit_rate=None,
                instrument_category=instrument_category,
                listing_phase=ListingPhase.UNKNOWN,
                rule_name="TaiwanHistoricalPriceLimitRule",
                reason="上市階段為 UNKNOWN，拒絕自行推測漲跌幅限制",
            )

        # 2. 無漲跌幅限制階段 (IPO 首五日、興櫃、恢復買賣無漲跌幅首日)
        if (
            listing_phase
            in {
                ListingPhase.IPO_NO_LIMIT_PERIOD,
                ListingPhase.EMERGING_NO_LIMIT,
                ListingPhase.RELISTING_NO_LIMIT,
            }
            or instrument_category == InstrumentCategory.EMERGING_STOCK
        ):
            if self.require_reference_price_for_no_limit and (
                reference_price is None
                or isinstance(reference_price, bool)
                or not isinstance(reference_price, Decimal)
                or not reference_price.is_finite()
                or reference_price <= _ZERO
            ):
                return PriceLimitBounds(
                    symbol=clean_sym,
                    trade_date=t_date,
                    status=PriceLimitStatus.UNKNOWN_DATA,
                    reference_price=None,
                    limit_up=None,
                    limit_down=None,
                    limit_rate=None,
                    instrument_category=instrument_category,
                    listing_phase=listing_phase,
                    rule_name="TaiwanHistoricalPriceLimitRule",
                    reason="無漲跌幅限制階段但缺少必要之掛牌參考價",
                )
            return PriceLimitBounds(
                symbol=clean_sym,
                trade_date=t_date,
                status=PriceLimitStatus.NO_PRICE_LIMIT,
                reference_price=reference_price,
                limit_up=None,
                limit_down=None,
                limit_rate=None,
                instrument_category=instrument_category,
                listing_phase=listing_phase,
                rule_name="TaiwanHistoricalPriceLimitRule",
                reason=f"適用無漲跌幅限制規則 (category={instrument_category.value}, phase={listing_phase.value})",
            )

        # 3. 檢查參考價是否有效 (嚴禁缺少參考價時以開盤價或收盤價猜測)
        if (
            reference_price is None
            or isinstance(reference_price, bool)
            or not isinstance(reference_price, Decimal)
            or not reference_price.is_finite()
            or reference_price <= _ZERO
        ):
            return PriceLimitBounds(
                symbol=clean_sym,
                trade_date=t_date,
                status=PriceLimitStatus.UNKNOWN_DATA,
                reference_price=None,
                limit_up=None,
                limit_down=None,
                limit_rate=None,
                instrument_category=instrument_category,
                listing_phase=listing_phase,
                rule_name="TaiwanHistoricalPriceLimitRule",
                reason="缺少有效之平盤參考價 (reference_price / yesterday_close)，嚴禁自行推測漲跌停價",
            )

        # 4. 決定適用之漲跌幅比例
        if listing_phase == ListingPhase.CUSTOM_REGIME:
            if (
                custom_limit_rate is None
                or isinstance(custom_limit_rate, bool)
                or not isinstance(custom_limit_rate, Decimal)
                or not custom_limit_rate.is_finite()
                or custom_limit_rate <= _ZERO
            ):
                return PriceLimitBounds(
                    symbol=clean_sym,
                    trade_date=t_date,
                    status=PriceLimitStatus.UNKNOWN_DATA,
                    reference_price=reference_price,
                    limit_up=None,
                    limit_down=None,
                    limit_rate=None,
                    instrument_category=instrument_category,
                    listing_phase=listing_phase,
                    rule_name="TaiwanHistoricalPriceLimitRule",
                    reason="CUSTOM_REGIME 未提供有效之 custom_limit_rate",
                )
            rate = custom_limit_rate
            rule_label = f"CUSTOM_REGIME_{rate}"
        elif custom_limit_rate is not None:
            if (
                isinstance(custom_limit_rate, bool)
                or not isinstance(custom_limit_rate, Decimal)
                or not custom_limit_rate.is_finite()
                or custom_limit_rate <= _ZERO
            ):
                raise PriceLimitError(f"custom_limit_rate 必須為 > 0 之有限 Decimal，收到 {custom_limit_rate!r}")
            rate = custom_limit_rate
            rule_label = f"EXPLICIT_OVERRIDE_{rate}"
        else:
            band = self._find_band_for_date(t_date)
            if band is None:
                return PriceLimitBounds(
                    symbol=clean_sym,
                    trade_date=t_date,
                    status=PriceLimitStatus.UNKNOWN_DATA,
                    reference_price=reference_price,
                    limit_up=None,
                    limit_down=None,
                    limit_rate=None,
                    instrument_category=instrument_category,
                    listing_phase=listing_phase,
                    rule_name="TaiwanHistoricalPriceLimitRule",
                    reason=f"交易日 {t_date} 不在已配置之台股歷史漲跌幅法規期間內，拒絕推測",
                )
            rate = band.base_limit_rate
            if instrument_category == InstrumentCategory.LEVERAGED_ETF:
                rate = rate * band.leveraged_etf_multiplier
            rule_label = band.regime_name

        # 5. 計算漲跌停價並對齊 Tick Size（確保不超過法定漲跌幅上限）
        raw_up = reference_price * (_ONE + rate)
        raw_down = reference_price * (_ONE - rate)
        if raw_down <= _ZERO:
            raw_down = Decimal("0.01")

        active_tick_rule = tick_rule or TaiwanTickSizeRule()
        if self.align_limits_to_tick:
            # 證交所漲跌停計算慣例：漲停向下取至合法檔位、跌停向上取至合法檔位（不超越法定漲跌幅百分比）
            limit_up = active_tick_rule.align_to_tick(raw_up, TickRoundingMode.FLOOR, instrument_category)
            limit_down = active_tick_rule.align_to_tick(raw_down, TickRoundingMode.CEILING, instrument_category)
        else:
            limit_up = raw_up.quantize(_CENT, rounding=ROUND_HALF_UP)
            limit_down = raw_down.quantize(_CENT, rounding=ROUND_HALF_UP)

        return PriceLimitBounds(
            symbol=clean_sym,
            trade_date=t_date,
            status=PriceLimitStatus.APPLIED,
            reference_price=reference_price,
            limit_up=limit_up,
            limit_down=limit_down,
            limit_rate=rate,
            instrument_category=instrument_category,
            listing_phase=listing_phase,
            rule_name=rule_label,
            reason="正常計算漲跌停上下限",
        )


# ── 4. 單一責任成交成本計算模型 (ExecutionCostModel) ───────────────────


@dataclass(frozen=True)
class ExecutionCostModel:
    """歷史撮合模擬器之單一責任成交成本模型。

    設計原則：
    - 手續費、證交稅、滑價只在此責任層計算一次，寫入 `FillRecord`。
    - 買賣方向、費率、最低手續費、稅率及商品類型皆透過明確配置取得，
      不將單一普通股費率硬編碼假定為所有商品與交易類型的通用規則。
    """

    commission_rate: Decimal = Decimal("0.001425")
    commission_discount: Decimal = Decimal("0.60")
    min_commission: Decimal = Decimal("20.00")
    odd_lot_min_commission: Optional[Decimal] = None
    slippage_rate: Decimal = Decimal("0.0005")
    tax_rates_by_category: Mapping[InstrumentCategory, Decimal] = field(
        default_factory=lambda: {
            InstrumentCategory.COMMON_STOCK: Decimal("0.0030"),
            InstrumentCategory.ETF: Decimal("0.0010"),
            InstrumentCategory.LEVERAGED_ETF: Decimal("0.0010"),
            InstrumentCategory.EMERGING_STOCK: Decimal("0.0030"),
            InstrumentCategory.WARRANT: Decimal("0.0010"),
        }
    )
    day_trading_tax_rates_by_category: Mapping[InstrumentCategory, Decimal] = field(
        default_factory=lambda: {
            InstrumentCategory.COMMON_STOCK: Decimal("0.0015"),
        }
    )
    commission_rounding: str = ROUND_HALF_UP
    tax_rounding: str = ROUND_FLOOR

    def __post_init__(self) -> None:
        for name in ("commission_rate", "commission_discount", "min_commission", "slippage_rate"):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, Decimal) or not val.is_finite() or val < _ZERO:
                raise BacktestAccountingError(f"ExecutionCostModel.{name} 必須為非負有限 Decimal，收到 {val!r}")
        if self.odd_lot_min_commission is not None:
            if (
                isinstance(self.odd_lot_min_commission, bool)
                or not isinstance(self.odd_lot_min_commission, Decimal)
                or not self.odd_lot_min_commission.is_finite()
                or self.odd_lot_min_commission < _ZERO
            ):
                raise BacktestAccountingError(
                    f"odd_lot_min_commission 必須為非負有限 Decimal，收到 {self.odd_lot_min_commission!r}"
                )

    @classmethod
    def from_paper_config(cls, config: TaiwanPaperConfig) -> "ExecutionCostModel":
        """由既有 `TaiwanPaperConfig` 建立一致之成本模型。"""
        if not isinstance(config, TaiwanPaperConfig):
            raise BacktestAccountingError(f"config 必須為 TaiwanPaperConfig 實例，收到 {type(config).__name__}")
        return cls(
            commission_rate=config.commission_rate,
            commission_discount=config.commission_discount,
            min_commission=config.min_commission,
            slippage_rate=config.slippage_rate,
            tax_rates_by_category={
                InstrumentCategory.COMMON_STOCK: config.stock_tax_rate,
                InstrumentCategory.ETF: config.etf_tax_rate,
                InstrumentCategory.LEVERAGED_ETF: config.etf_tax_rate,
                InstrumentCategory.EMERGING_STOCK: config.stock_tax_rate,
                InstrumentCategory.WARRANT: config.etf_tax_rate,
            },
            day_trading_tax_rates_by_category={
                InstrumentCategory.COMMON_STOCK: config.day_trading_tax_rate,
            },
            commission_rounding=config.commission_rounding,
            tax_rounding=config.tax_rounding,
        )

    @classmethod
    def from_strategy_cost_model(cls, cost_model: StrategyCostModel) -> "ExecutionCostModel":
        """由策略層 `CostModel` 建立一致之成交成本模型。"""
        if not isinstance(cost_model, StrategyCostModel):
            raise BacktestAccountingError(
                f"cost_model 必須為 strategy.config.CostModel 實例，收到 {type(cost_model).__name__}"
            )
        return cls(
            commission_rate=cost_model.commission_rate,
            commission_discount=cost_model.commission_discount,
            min_commission=cost_model.min_commission,
            slippage_rate=cost_model.slippage_rate,
            tax_rates_by_category={
                InstrumentCategory.COMMON_STOCK: cost_model.stock_tax_rate,
                InstrumentCategory.ETF: cost_model.etf_tax_rate,
                InstrumentCategory.LEVERAGED_ETF: cost_model.etf_tax_rate,
                InstrumentCategory.EMERGING_STOCK: cost_model.stock_tax_rate,
                InstrumentCategory.WARRANT: cost_model.etf_tax_rate,
            },
        )

    def calculate_commission(self, notional: Decimal, *, is_odd_lot: bool = False) -> Decimal:
        """計算單筆成交手續費（僅在產生 FillRecord 時計算一次）。"""
        if notional <= _ZERO:
            return _ZERO
        raw_fee = notional * self.commission_rate * self.commission_discount
        rounded_fee = raw_fee.quantize(Decimal("1"), rounding=self.commission_rounding)
        floor_fee = (
            self.odd_lot_min_commission
            if (is_odd_lot and self.odd_lot_min_commission is not None)
            else self.min_commission
        )
        return max(floor_fee, rounded_fee)

    def calculate_tax(
        self,
        notional: Decimal,
        action: SignalAction,
        instrument_category: InstrumentCategory,
        *,
        is_day_trading: bool = False,
    ) -> Decimal:
        """依買賣方向、商品類別與是否當沖計算證券交易稅（買進為 0，未知商品類別拋出例外）。"""
        if action == SignalAction.BUY:
            return _ZERO
        if action not in {SignalAction.SELL, SignalAction.CLOSE}:
            raise BacktestAccountingError(f"未知的交易動作 action={action!r}")

        if not isinstance(instrument_category, InstrumentCategory) or instrument_category == InstrumentCategory.UNKNOWN:
            raise BacktestAccountingError(
                f"無法為未知商品類別 ({instrument_category!r}) 計算證交稅，嚴禁假定通用稅率",
                context={"instrument_category": getattr(instrument_category, "value", repr(instrument_category))},
            )

        if is_day_trading:
            if instrument_category not in self.day_trading_tax_rates_by_category:
                raise BacktestAccountingError(
                    f"商品類別 {instrument_category.value} 未配置當沖證交稅率 (day_trading_tax_rates_by_category)",
                    context={"instrument_category": instrument_category.value},
                )
            rate = self.day_trading_tax_rates_by_category[instrument_category]
        else:
            if instrument_category not in self.tax_rates_by_category:
                raise BacktestAccountingError(
                    f"商品類別 {instrument_category.value} 未配置證交稅率 (tax_rates_by_category)，嚴禁假定通用稅率",
                    context={"instrument_category": instrument_category.value},
                )
            rate = self.tax_rates_by_category[instrument_category]

        raw_tax = notional * rate
        return raw_tax.quantize(Decimal("1"), rounding=self.tax_rounding)


# ── 5. 標的市場元資料、模擬器設定與撮合審計紀錄 ─────────────────────────


@dataclass(frozen=True)
class SymbolExecutionContext:
    """單一標的於單一交易日之市場與微結構上下文。

    欄位說明：
    - `instrument_category`: 商品類別（決定 Tick Size 與稅率；若未指定，不得盲目猜測未知商品）。
    - `listing_phase`: 上市階段（如 `REGULAR_LISTED`、`IPO_NO_LIMIT_PERIOD` 等）。
    - `trading_status`: 交易狀態（如 `NORMAL`、`HALTED`、`DISPOSITION`、`UNKNOWN`；`UNKNOWN` 禁止交易）。
    - `reference_price`: 當日漲跌停計算基準價（如平盤參考價、除權息參考價；若為 None 則回退讀取 `bar.yesterday_close`）。
    - `adv_20_shares`: 截至 T-1 收盤已知之過去 20 日平均成交股數（單位：整數股 `SHARES`）。
    - `opening_volume_shares`: T 日開盤集合競價成交股數（選填，單位：整數股 `SHARES`）。
    - `custom_limit_rate`: 特殊階段之自訂漲跌幅比例（選填）。
    - `is_day_trading`: 是否適用現股當沖稅率。
    """

    symbol: str
    instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK
    listing_phase: ListingPhase = ListingPhase.REGULAR_LISTED
    trading_status: InstrumentTradingStatus = InstrumentTradingStatus.NORMAL
    reference_price: Optional[Decimal] = None
    adv_20_shares: Optional[int] = None
    opening_volume_shares: Optional[int] = None
    custom_limit_rate: Optional[Decimal] = None
    is_day_trading: bool = False

    def __post_init__(self) -> None:
        clean_sym = _normalize_symbol(self.symbol)
        object.__setattr__(self, "symbol", clean_sym)

        if self.adv_20_shares is not None:
            if isinstance(self.adv_20_shares, bool) or not isinstance(self.adv_20_shares, int) or self.adv_20_shares < 0:
                raise InvalidOHLCVError(
                    f"adv_20_shares 必須為非負整數股數 (SHARES)，收到 {self.adv_20_shares!r}",
                    symbol=clean_sym,
                    field_name="adv_20_shares",
                    invalid_value=self.adv_20_shares,
                )
        if self.opening_volume_shares is not None:
            if (
                isinstance(self.opening_volume_shares, bool)
                or not isinstance(self.opening_volume_shares, int)
                or self.opening_volume_shares < 0
            ):
                raise InvalidOHLCVError(
                    f"opening_volume_shares 必須為非負整數股數 (SHARES)，收到 {self.opening_volume_shares!r}",
                    symbol=clean_sym,
                    field_name="opening_volume_shares",
                    invalid_value=self.opening_volume_shares,
                )


@dataclass(frozen=True)
class ExecutionSimConfig:
    """`HistoricalExecutionSimulator` 配置參數。"""

    execution_mode: ExecutionMode = ExecutionMode.DAILY_OPEN_CONSERVATIVE
    ambiguous_limit_policy: AmbiguousLimitPolicy = AmbiguousLimitPolicy.FILL_AT_LIMIT_PRICE
    disposition_policy: DispositionExecutionPolicy = DispositionExecutionPolicy.ALLOW_WITH_VOLUME_HAIRCUT
    volume_cap_policy: VolumeCapPolicy = VolumeCapPolicy.CAUSAL_ADV_AND_DAILY_BOUND
    max_volume_participation: Decimal = Decimal("0.05")  # 預設保守 5% 成交量參與率上限
    allow_ex_post_daily_volume_fallback: bool = True     # 當未提供 ADV20_T-1 時是否允許退回使用當日量上限並標記警告
    conservative_close_at_limit_block: bool = True       # 收盤鎖死漲停/跌停時是否保守攔截買單/賣單
    enforce_tick_size_on_limit_orders: bool = True       # 嚴格驗證限價單 limit_price 必須符合 Tick Size
    raise_on_invalid_limit_tick: bool = False            # True 時直接拋出 TickSizeViolationError；False 時將委託轉為 REJECTED 並退還預留
    reject_limit_outside_daily_bounds: bool = True       # 限價超出當日漲跌停範圍時不予成交（台股超過漲跌停之限價單無效）
    allow_odd_lots: bool = True                          # 是否允許零股成交
    board_lot_shares: int = 1000                         # 台股整張單位股數 (1000 股)

    def __post_init__(self) -> None:
        if not isinstance(self.execution_mode, ExecutionMode):
            raise BacktestExecutionError(f"不支援的 execution_mode: {self.execution_mode!r}")
        if not isinstance(self.ambiguous_limit_policy, AmbiguousLimitPolicy):
            raise BacktestExecutionError(f"不支援的 ambiguous_limit_policy: {self.ambiguous_limit_policy!r}")
        if not isinstance(self.disposition_policy, DispositionExecutionPolicy):
            raise BacktestExecutionError(f"不支援的 disposition_policy: {self.disposition_policy!r}")
        if not isinstance(self.volume_cap_policy, VolumeCapPolicy):
            raise BacktestExecutionError(f"不支援的 volume_cap_policy: {self.volume_cap_policy!r}")
        if (
            isinstance(self.max_volume_participation, bool)
            or not isinstance(self.max_volume_participation, Decimal)
            or not self.max_volume_participation.is_finite()
            or not (_ZERO < self.max_volume_participation <= _ONE)
        ):
            raise BacktestExecutionError(
                f"max_volume_participation 必須為 (0, 1] 之間的 Decimal，收到 {self.max_volume_participation!r}"
            )
        if isinstance(self.board_lot_shares, bool) or not isinstance(self.board_lot_shares, int) or self.board_lot_shares <= 0:
            raise BacktestExecutionError(f"board_lot_shares 必須為 > 0 之整數，收到 {self.board_lot_shares!r}")


@dataclass(frozen=True)
class OrderExecutionOutcome:
    """單筆委託於單次撮合嘗試之完整結構化審計結果。

    明確區分：
    - `order_quantity`: 原始委託總股數
    - `order_remaining_before`: 本次撮合前委託尚餘未成交股數
    - `max_fillable_quantity`: 受市場流動性上限 (`volume_cap`) 與可售持倉限制後之最大可成交股數
    - `filled_quantity`: 本次實際成交股數 (`0 <= filled_quantity <= min(order_remaining_before, max_fillable_quantity)`)
    - `unfilled_quantity`: 本次撮合後仍未成交之剩餘股數 (`order_remaining_before - filled_quantity`)
    """

    order_id: str
    signal_id: str
    symbol: str
    trade_date: dt.date
    execution_timestamp: dt.datetime
    action: SignalAction
    order_type: SignalOrderType
    order_quantity: int
    order_remaining_before: int
    max_fillable_quantity: int
    filled_quantity: int
    unfilled_quantity: int
    order_status_after: PaperOrderStatus
    outcome_code: ExecutionOutcomeCode
    reason: str
    fill_record: Optional[FillRecord]
    price_limits: PriceLimitBounds
    volume_cap_basis: str
    used_ex_post_daily_volume_approximation: bool
    warnings: Tuple[str, ...] = ()


# ── 6. 歷史日線撮合模擬器主體 (HistoricalExecutionSimulator) ─────────────


class HistoricalExecutionSimulator:
    """台股歷史回測日線撮合模擬器 (`HistoricalExecutionSimulator`)."""

    def __init__(
        self,
        config: Optional[ExecutionSimConfig] = None,
        *,
        cost_model: Optional[ExecutionCostModel] = None,
        price_limit_rule: Optional[PriceLimitRule] = None,
        tick_size_rule: Optional[TickSizeRule] = None,
        calendar: Optional[TradingCalendar] = None,
    ) -> None:
        if calendar is not None and not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須是 TradingCalendar 實例，收到 {type(calendar).__name__}")
        self.config: ExecutionSimConfig = config or ExecutionSimConfig()
        self.cost_model: ExecutionCostModel = cost_model or ExecutionCostModel()
        self.price_limit_rule: PriceLimitRule = price_limit_rule or TaiwanHistoricalPriceLimitRule()
        self.tick_size_rule: TickSizeRule = tick_size_rule or TaiwanTickSizeRule()
        self.calendar: Optional[TradingCalendar] = calendar

        self._fill_seq: int = 0
        # 追蹤已註冊委託與其尚未釋放/消耗之預留現金與預留股數 (確保單次精確釋放，不殘留分攤尾差)
        self._registered_order_ids: Set[str] = set()
        self._remaining_reserved_cash: Dict[str, Decimal] = {}
        self._remaining_reserved_shares: Dict[str, int] = {}
        self._reservation_released_order_ids: Set[str] = set()

        # 防重複處理追蹤：((order_id, trade_date)) 與 fill_id
        self._processed_order_day_pairs: Set[Tuple[str, dt.date]] = set()
        self._emitted_fill_ids: Set[str] = set()
        self._settled_fill_ids: Set[str] = set()

        # 追蹤單一交易日各標的已消耗之市場成交量配額 (防止同日多筆委託合計突破 volume_cap)
        self._daily_consumed_volume: Dict[Tuple[str, dt.date], int] = {}

        # 歷史審計紀錄
        self._execution_outcomes: List[OrderExecutionOutcome] = []
        self._all_fills: List[FillRecord] = []

    def generate_fill_id(self) -> str:
        self._fill_seq += 1
        return f"SIM-FILL-{self._fill_seq:08d}"

    @property
    def execution_outcomes(self) -> Tuple[OrderExecutionOutcome, ...]:
        return tuple(self._execution_outcomes)

    @property
    def all_fills(self) -> Tuple[FillRecord, ...]:
        return tuple(self._all_fills)

    # ── 委託註冊、預留款券與超賣防護 ─────────────────────────────

    def submit_order(
        self,
        order: PaperOrder,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        *,
        reference_price: Optional[Decimal] = None,
        instrument_category: InstrumentCategory = InstrumentCategory.COMMON_STOCK,
        lot_ledger: Optional[BacktestLotLedger] = None,
    ) -> PaperOrder:
        """提交委託單進入模擬器，執行 Tick Size 檢驗與資金／持倉預留。

        保護機制：
        1. 同一 `order_id` 嚴禁重複提交 (`DuplicateOrderExecutionError`)。
        2. `order.created_at` 必須具備明確時區 (`Asia/Taipei`)。
        3. 若為限價單 (`LIMIT`)，驗證 `limit_price` 是否符合該商品類別之 Tick Size。
        4. 若為買單 (`BUY`)，於 `portfolio_ledger` 凍結預留現金（含滑價與手續費）。
        5. 若為賣單 (`SELL` / `CLOSE`)，檢查 `portfolio_ledger`（及 `lot_ledger`）扣除其他委託已預留股數後之可用股數；
           若可用股數不足，立即拒絕委託 (`REJECTED`)，絕不超額預留或允許超賣。
        """
        if not isinstance(order, PaperOrder):
            raise BacktestExecutionError(f"order 必須為 PaperOrder 實例，收到 {type(order).__name__}")

        if order.order_id in self._registered_order_ids:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 已提交過，嚴禁重複提交同一委託",
                context={"order_id": order.order_id, "symbol": order.symbol},
            )

        clean_sym = _normalize_symbol(order.symbol)
        created_taipei = ensure_taipei_datetime(order.created_at, "order.created_at")
        order.created_at = created_taipei

        if order.is_terminal:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 已處於終態 {order.status.value}，不得再次提交",
                context={"order_id": order.order_id, "status": order.status.value},
            )

        # 檢查零股限制
        if not self.config.allow_odd_lots and (order.order_quantity % self.config.board_lot_shares != 0):
            self._registered_order_ids.add(order.order_id)
            if order.status == PaperOrderStatus.NEW:
                order.reject("模擬器設定不允許零股委託")
            else:
                self._remaining_reserved_cash[order.order_id] = order.reserved_cash
                self._remaining_reserved_shares[order.order_id] = order.reserved_shares
                self._release_order_reservation_once(order, portfolio_ledger)
                order.cancel("模擬器設定不允許零股委託")
            return order

        # 檢查限價單 Tick Size
        if order.order_type == SignalOrderType.LIMIT:
            if order.limit_price is None or order.limit_price <= _ZERO:
                self._registered_order_ids.add(order.order_id)
                if order.status == PaperOrderStatus.NEW:
                    order.reject("限價單缺少有效 limit_price")
                else:
                    self._remaining_reserved_cash[order.order_id] = order.reserved_cash
                    self._remaining_reserved_shares[order.order_id] = order.reserved_shares
                    self._release_order_reservation_once(order, portfolio_ledger)
                    order.cancel("限價單缺少有效 limit_price")
                return order
            if self.config.enforce_tick_size_on_limit_orders:
                if not self.tick_size_rule.is_valid_tick(order.limit_price, instrument_category):
                    if self.config.raise_on_invalid_limit_tick:
                        self.tick_size_rule.validate_tick(
                            order.limit_price,
                            instrument_category,
                            field_name="order.limit_price",
                            symbol=clean_sym,
                        )
                    self._registered_order_ids.add(order.order_id)
                    if order.status == PaperOrderStatus.NEW:
                        order.reject(
                            f"INVALID_TICK_SIZE: 限價 {order.limit_price} 不符合 {instrument_category.value} 之 Tick Size"
                        )
                    else:
                        self._remaining_reserved_cash[order.order_id] = order.reserved_cash
                        self._remaining_reserved_shares[order.order_id] = order.reserved_shares
                        self._release_order_reservation_once(order, portfolio_ledger)
                        order.cancel(
                            f"INVALID_TICK_SIZE: 限價 {order.limit_price} 不符合 {instrument_category.value} 之 Tick Size"
                        )
                    return order

        # 若訂單尚未 ACCEPTED，執行資金或股數預留
        if order.status == PaperOrderStatus.NEW:
            if portfolio_ledger is not None:
                if not isinstance(portfolio_ledger, PortfolioLedger):
                    raise BacktestExecutionError("portfolio_ledger 必須是 PortfolioLedger 實例")

                if order.action == SignalAction.BUY:
                    est_px = order.limit_price if order.order_type == SignalOrderType.LIMIT else reference_price
                    if est_px is None or est_px <= _ZERO:
                        self._registered_order_ids.add(order.order_id)
                        order.reject("市價買單必須提供有效 reference_price 以預留買進資金")
                        return order

                    est_notional = (
                        est_px * (_ONE + self.cost_model.slippage_rate) * Decimal(order.order_quantity)
                    ).quantize(_CENT, rounding=ROUND_HALF_UP)
                    is_odd = (order.order_quantity % self.config.board_lot_shares) != 0
                    est_fee = self.cost_model.calculate_commission(est_notional, is_odd_lot=is_odd)
                    required_cash = est_notional + est_fee

                    try:
                        portfolio_ledger.reserve_buy_funds(required_cash)
                        order.accept(reserved_cash=required_cash, reserved_shares=0)
                        self._remaining_reserved_cash[order.order_id] = required_cash
                        self._remaining_reserved_shares[order.order_id] = 0
                    except ValueError as exc:
                        self._registered_order_ids.add(order.order_id)
                        order.reject(f"買進資金預留失敗: {exc}")
                        return order

                elif order.action in {SignalAction.SELL, SignalAction.CLOSE}:
                    # 同時核對 lot_ledger (若有提供) 與 portfolio_ledger 的可用持倉
                    avail_in_ledger = portfolio_ledger.get_available_shares(order.symbol)
                    if lot_ledger is not None:
                        lot_total = lot_ledger.get_position_quantity(clean_sym)
                        already_reserved = portfolio_ledger.reserved_shares.get(order.symbol, 0)
                        avail_in_lots = max(0, lot_total - already_reserved)
                        avail_in_ledger = min(avail_in_ledger, avail_in_lots)

                    if order.order_quantity > avail_in_ledger:
                        self._registered_order_ids.add(order.order_id)
                        order.reject(
                            f"賣出持倉預留失敗: 欲賣出 {order.order_quantity} 股，但扣除已預留後可用僅 {avail_in_ledger} 股"
                        )
                        return order

                    try:
                        portfolio_ledger.reserve_sell_shares(order.symbol, order.order_quantity)
                        order.accept(reserved_cash=_ZERO, reserved_shares=order.order_quantity)
                        self._remaining_reserved_cash[order.order_id] = _ZERO
                        self._remaining_reserved_shares[order.order_id] = order.order_quantity
                    except ValueError as exc:
                        self._registered_order_ids.add(order.order_id)
                        order.reject(f"賣出持倉預留失敗: {exc}")
                        return order
            else:
                # 未傳入 portfolio_ledger 時，僅將 NEW 轉為 ACCEPTED
                order.accept(reserved_cash=order.reserved_cash, reserved_shares=order.reserved_shares)
                self._remaining_reserved_cash[order.order_id] = order.reserved_cash
                self._remaining_reserved_shares[order.order_id] = order.reserved_shares
        else:
            # 外部已執行過 accept() (如透過 TaiwanPaperBroker.submit_signal 建立之 ACCEPTED 委託)
            self._remaining_reserved_cash[order.order_id] = order.reserved_cash
            self._remaining_reserved_shares[order.order_id] = order.reserved_shares

        self._registered_order_ids.add(order.order_id)
        return order

    # ── 內部輔助：驗證因果時序與計算可成交量 ──────────────────────

    def _validate_order_and_bar_causality(
        self,
        order: PaperOrder,
        bar: HistoricalBarRecord,
        proposed_execution_time: Optional[dt.datetime],
    ) -> dt.datetime:
        """強制檢驗委託單與日線行情之時序因果關係，攔截任何前視偏差。

        規則：
        1. `bar.price_basis` 必須為 `PriceBasis.RAW_UNADJUSTED`（禁止以還原價撮合）。
        2. 預設 `DAILY_OPEN_CONSERVATIVE` 模式下，撮合時間為 `bar.trade_date 09:00:00+08:00`（若傳入 `proposed_execution_time` 則驗證其合法性）。
        3. 若有人試圖在 `bar.trade_date` 收盤 (`13:30`) 利用盤後才發布的 `bar` 收盤資訊進行同日收盤成交，立即拋出 `LookaheadBiasError`。
        4. `order.created_at` 必須嚴格早於或等於當日開盤撮合時間 (`exec_time`)，且：
           - 在 T-1 盤後（`> T-1 13:30` 或 `> T-1 09:00`）產生的委託，**絕對不得在 T-1 當日成交**，最早只能在 T 日 (`trade_date >= T`) 撮合！
        """
        if not isinstance(bar, HistoricalBarRecord):
            raise UnsupportedDataFormatError(
                f"bar 必須為 HistoricalBarRecord 實例，收到 {type(bar).__name__}",
                format_type=type(bar).__name__,
            )

        if bar.price_basis != PriceBasis.RAW_UNADJUSTED:
            raise UnsupportedDataFormatError(
                f"撮合器僅允許使用原始未還原價格 (RAW_UNADJUSTED)，收到 {bar.price_basis.value}",
                format_type=bar.price_basis.value,
                context={"symbol": bar.symbol, "trade_date": bar.trade_date.isoformat()},
            )

        if self.calendar is not None:
            if not self.calendar.is_trading_day(bar.trade_date):
                raise BacktestCalendarError(
                    f"行情日期 {bar.trade_date} 在交易日曆中非有效交易日，禁止撮合",
                    context={"symbol": bar.symbol, "trade_date": bar.trade_date.isoformat()},
                )

        order_created = ensure_taipei_datetime(order.created_at, "order.created_at")
        market_open_dt = dt.datetime.combine(bar.trade_date, TWSE_MARKET_OPEN_TIME, tzinfo=TAIPEI_TZ)
        market_close_dt = dt.datetime.combine(bar.trade_date, TWSE_MARKET_CLOSE_TIME, tzinfo=TAIPEI_TZ)

        if proposed_execution_time is not None:
            exec_dt = ensure_taipei_datetime(proposed_execution_time, "proposed_execution_time")
            if exec_dt.date() != bar.trade_date:
                raise LookaheadBiasError(
                    f"預定撮合時間日期 ({exec_dt.date()}) 與行情交易日 ({bar.trade_date}) 不一致",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    execution_time=exec_dt,
                )
            if exec_dt < market_open_dt or exec_dt > market_close_dt:
                raise BacktestExecutionError(
                    f"預定撮合時間 ({exec_dt}) 不在台股合法交易時段 [{market_open_dt}, {market_close_dt}] 內",
                    context={"symbol": bar.symbol, "execution_time": exec_dt.isoformat()},
                )
            # 阻止利用盤後才取得的 T 日收盤資訊於 T 日收盤成交
            if exec_dt >= market_close_dt:
                raise LookaheadBiasError(
                    f"禁止同日收盤成交: 標的 {bar.symbol} 於 {bar.trade_date} 之日線收盤資訊於盤後 ({bar.available_at or bar.event_time}) 始可取得，"
                    f"嚴禁在當日收盤 ({exec_dt}) 使用收盤價撮合成交",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    available_at=bar.available_at,
                    execution_time=exec_dt,
                )
        else:
            exec_dt = market_open_dt

        # 關鍵因果檢查：委託建立時間不得晚於撮合時間 (例如 T-1 18:00 盤後產生的委託，在 T-1 09:00 尚不存在，絕不可在 T-1 成交！)
        if order_created > exec_dt:
            raise LookaheadBiasError(
                f"因果時序違規 (禁止盤後委託回溯於當日或過去成交): 委託 {order.order_id} 建立於 {order_created}，"
                f"晚於交易日 {bar.trade_date} 之撮合時點 {exec_dt}；T-1 盤後委託只能在 T 日或之後撮合",
                symbol=bar.symbol,
                trade_date=bar.trade_date,
                cutoff_time=order_created,
                execution_time=exec_dt,
                context={"order_id": order.order_id, "order_created_at": order_created.isoformat()},
            )

        return exec_dt

    def _compute_volume_cap(
        self,
        bar: HistoricalBarRecord,
        sym_ctx: SymbolExecutionContext,
        is_disposition: bool,
    ) -> Tuple[int, str, bool, Tuple[str, ...]]:
        """計算該標的於當日之最大可成交股數上限，回傳 `(remaining_cap, basis_str, used_ex_post_approx, warnings)`。"""
        warnings: List[str] = []
        policy = self.config.volume_cap_policy
        rate = self.config.max_volume_participation
        used_ex_post = False

        if policy == VolumeCapPolicy.OPENING_AUCTION_VOLUME_ONLY:
            if sym_ctx.opening_volume_shares is None:
                raise HistoricalDataMissingError(
                    f"VolumeCapPolicy.OPENING_AUCTION_VOLUME_ONLY 要求提供 {bar.symbol} ({bar.trade_date}) 之 opening_volume_shares",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    missing_fields=("opening_volume_shares",),
                )
            base_vol = min(sym_ctx.opening_volume_shares, bar.volume)
            basis_str = f"OPENING_AUCTION_VOLUME({sym_ctx.opening_volume_shares})*rate({rate})"

        elif policy == VolumeCapPolicy.STRICT_EX_ANTE_ADV_ONLY:
            if sym_ctx.adv_20_shares is None:
                raise HistoricalDataMissingError(
                    f"VolumeCapPolicy.STRICT_EX_ANTE_ADV_ONLY 要求提供截至 T-1 已知之 adv_20_shares (單位: 股)，"
                    f"嚴禁在缺少事前均量時把 {bar.trade_date} 整日成交量視為開盤已知資訊",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    missing_fields=("adv_20_shares",),
                )
            ex_ante_cap = int((Decimal(sym_ctx.adv_20_shares) * rate).to_integral_value(rounding=ROUND_FLOOR))
            # 受當日實際物理成交量上限約束
            raw_cap = min(ex_ante_cap, bar.volume)
            basis_str = f"STRICT_EX_ANTE_ADV20({sym_ctx.adv_20_shares})*rate({rate})"
            if is_disposition:
                raw_cap = raw_cap // 2
                basis_str += "//2(DISPOSITION)"
            consumed = self._daily_consumed_volume.get((bar.symbol, bar.trade_date), 0)
            return max(0, raw_cap - consumed), basis_str, False, tuple(warnings)

        elif policy == VolumeCapPolicy.CAUSAL_ADV_AND_DAILY_BOUND:
            if sym_ctx.adv_20_shares is not None:
                base_vol = min(sym_ctx.adv_20_shares, bar.volume)
                basis_str = f"MIN(ADV20_T_MINUS_1={sym_ctx.adv_20_shares}, DAILY_VOL={bar.volume})*rate({rate})"
            else:
                if not self.config.allow_ex_post_daily_volume_fallback:
                    raise HistoricalDataMissingError(
                        f"標的 {bar.symbol} 於 {bar.trade_date} 缺少事前 adv_20_shares，且已關閉事後日線成交量回退",
                        symbol=bar.symbol,
                        trade_date=bar.trade_date,
                        missing_fields=("adv_20_shares",),
                    )
                base_vol = bar.volume
                used_ex_post = True
                basis_str = f"EX_POST_DAILY_VOLUME_FALLBACK({bar.volume})*rate({rate})"
                warnings.append(
                    f"缺少 T-1 事前均量 (adv_20_shares)，退回使用當日總成交量 {bar.volume} 股 × {rate} 作為流動性近似上限（注意：整日成交量非 09:00 開盤已知資訊）"
                )
        else:
            # EX_POST_DAILY_VOLUME_APPROXIMATION
            base_vol = bar.volume
            used_ex_post = True
            basis_str = f"EX_POST_DAILY_VOLUME_APPROXIMATION({bar.volume})*rate({rate})"
            warnings.append(
                f"使用事後全日成交量 ({bar.volume} 股) × {rate} 作為日線流動性上限近似值（非開盤已知資訊）"
            )

        raw_cap = int((Decimal(base_vol) * rate).to_integral_value(rounding=ROUND_FLOOR))
        if is_disposition:
            raw_cap = raw_cap // 2
            basis_str += "//2(DISPOSITION)"

        consumed = self._daily_consumed_volume.get((bar.symbol, bar.trade_date), 0)
        remaining_cap = max(0, raw_cap - consumed)
        return remaining_cap, basis_str, used_ex_post, tuple(warnings)

    # ── 單筆委託撮合核心 ─────────────────────────────────────────

    def match_order(
        self,
        order: PaperOrder,
        bar: HistoricalBarRecord,
        *,
        symbol_context: Optional[SymbolExecutionContext] = None,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        lot_ledger: Optional[BacktestLotLedger] = None,
        strategy_id: str = "default",
        proposed_execution_time: Optional[dt.datetime] = None,
        settle_immediately: bool = True,
    ) -> OrderExecutionOutcome:
        """於指定交易日行情 `bar` 上撮合單筆有效委託 `order`，回傳結構化 `OrderExecutionOutcome`。"""
        if not isinstance(order, PaperOrder):
            raise BacktestExecutionError(f"order 必須為 PaperOrder 實例，收到 {type(order).__name__}")
        if not isinstance(bar, HistoricalBarRecord):
            raise UnsupportedDataFormatError(
                f"bar 必須為 HistoricalBarRecord 實例，收到 {type(bar).__name__}",
                format_type=type(bar).__name__,
            )

        clean_order_sym = _normalize_symbol(order.symbol)
        clean_bar_sym = _normalize_symbol(bar.symbol)
        if clean_order_sym != clean_bar_sym:
            raise BacktestExecutionError(
                f"委託標的 ({clean_order_sym}) 與行情標的 ({clean_bar_sym}) 不一致",
                context={"order_symbol": clean_order_sym, "bar_symbol": clean_bar_sym},
            )

        # 1. 檢查終態與同日重複撮合防護
        if order.is_terminal:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 已處於終態 {order.status.value}，嚴禁再次撮合",
                context={"order_id": order.order_id, "status": order.status.value},
            )

        order_day_key = (order.order_id, bar.trade_date)
        if order_day_key in self._processed_order_day_pairs:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 於交易日 {bar.trade_date} 已執行過撮合，嚴禁同日重複處理同一委託",
                context={"order_id": order.order_id, "trade_date": bar.trade_date.isoformat()},
            )

        # 若該委託尚未在模擬器登記預留追蹤，先同步其目前預留金額/股數
        if order.order_id not in self._registered_order_ids:
            self._registered_order_ids.add(order.order_id)
            self._remaining_reserved_cash[order.order_id] = order.reserved_cash
            self._remaining_reserved_shares[order.order_id] = order.reserved_shares

        # 2. 驗證時序因果 (T-1 盤後委託不得於 T-1 成交；不得於 T 日收盤使用盤後資訊成交)
        exec_dt = self._validate_order_and_bar_causality(order, bar, proposed_execution_time)

        sym_ctx = symbol_context or SymbolExecutionContext(symbol=clean_bar_sym)
        if _normalize_symbol(sym_ctx.symbol) != clean_bar_sym:
            raise BacktestExecutionError(
                f"symbol_context.symbol ({sym_ctx.symbol}) 與 bar.symbol ({clean_bar_sym}) 不一致"
            )

        # 3. 檢查交易狀態 (未知狀態嚴禁自動視為正常可交易)
        if not isinstance(sym_ctx.trading_status, InstrumentTradingStatus) or (
            sym_ctx.trading_status == InstrumentTradingStatus.UNKNOWN
        ):
            raise UnknownTradingStatusError(
                f"標的 {clean_bar_sym} 於 {bar.trade_date} 之交易狀態為 UNKNOWN，嚴禁自動視為正常可交易",
                context={"symbol": clean_bar_sym, "trade_date": bar.trade_date.isoformat()},
            )

        ref_px = sym_ctx.reference_price if sym_ctx.reference_price is not None else bar.yesterday_close
        price_bounds = self.price_limit_rule.resolve_price_limits(
            clean_bar_sym,
            bar.trade_date,
            ref_px,
            instrument_category=sym_ctx.instrument_category,
            listing_phase=sym_ctx.listing_phase,
            tick_rule=self.tick_size_rule,
            custom_limit_rate=sym_ctx.custom_limit_rate,
        )

        rem_before = order.remaining_quantity

        # 4. 處理停牌 (HALTED) 或已下市 / 限制狀態
        is_halted = bar.is_halted or (sym_ctx.trading_status == InstrumentTradingStatus.HALTED) or (bar.volume == 0)
        if is_halted:
            self._processed_order_day_pairs.add(order_day_key)
            outcome = OrderExecutionOutcome(
                order_id=order.order_id,
                signal_id=order.signal_id,
                symbol=clean_bar_sym,
                trade_date=bar.trade_date,
                execution_timestamp=exec_dt,
                action=order.action,
                order_type=order.order_type,
                order_quantity=order.order_quantity,
                order_remaining_before=rem_before,
                max_fillable_quantity=0,
                filled_quantity=0,
                unfilled_quantity=rem_before,
                order_status_after=order.status,
                outcome_code=ExecutionOutcomeCode.UNFILLED_HALTED,
                reason=f"標的 {clean_bar_sym} 於 {bar.trade_date} 處於停牌或無成交量狀態，禁止撮合",
                fill_record=None,
                price_limits=price_bounds,
                volume_cap_basis="HALTED_ZERO_CAPACITY",
                used_ex_post_daily_volume_approximation=False,
            )
            self._execution_outcomes.append(outcome)
            return outcome

        if sym_ctx.trading_status in {
            InstrumentTradingStatus.RESTRICTED,
            InstrumentTradingStatus.FULL_CASH_DELIVERY,
            InstrumentTradingStatus.DELISTED,
        }:
            self._processed_order_day_pairs.add(order_day_key)
            outcome = OrderExecutionOutcome(
                order_id=order.order_id,
                signal_id=order.signal_id,
                symbol=clean_bar_sym,
                trade_date=bar.trade_date,
                execution_timestamp=exec_dt,
                action=order.action,
                order_type=order.order_type,
                order_quantity=order.order_quantity,
                order_remaining_before=rem_before,
                max_fillable_quantity=0,
                filled_quantity=0,
                unfilled_quantity=rem_before,
                order_status_after=order.status,
                outcome_code=ExecutionOutcomeCode.UNFILLED_RESTRICTED_STATUS,
                reason=f"標的 {clean_bar_sym} 於 {bar.trade_date} 交易狀態為 {sym_ctx.trading_status.value}，依規則攔截不撮合",
                fill_record=None,
                price_limits=price_bounds,
                volume_cap_basis="RESTRICTED_ZERO_CAPACITY",
                used_ex_post_daily_volume_approximation=False,
            )
            self._execution_outcomes.append(outcome)
            return outcome

        # 5. 處理處置股 (DISPOSITION) 政策
        is_disposition = bar.is_disposition or (sym_ctx.trading_status == InstrumentTradingStatus.DISPOSITION)
        if is_disposition:
            if self.config.disposition_policy == DispositionExecutionPolicy.BLOCK_ALL or (
                self.config.disposition_policy == DispositionExecutionPolicy.BLOCK_NEW_BUY
                and order.action == SignalAction.BUY
            ):
                self._processed_order_day_pairs.add(order_day_key)
                outcome = OrderExecutionOutcome(
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    symbol=clean_bar_sym,
                    trade_date=bar.trade_date,
                    execution_timestamp=exec_dt,
                    action=order.action,
                    order_type=order.order_type,
                    order_quantity=order.order_quantity,
                    order_remaining_before=rem_before,
                    max_fillable_quantity=0,
                    filled_quantity=0,
                    unfilled_quantity=rem_before,
                    order_status_after=order.status,
                    outcome_code=ExecutionOutcomeCode.UNFILLED_DISPOSITION_BLOCKED,
                    reason=(
                        f"標的 {clean_bar_sym} 於 {bar.trade_date} 為處置股，依政策 "
                        f"{self.config.disposition_policy.value} 攔截委託"
                    ),
                    fill_record=None,
                    price_limits=price_bounds,
                    volume_cap_basis="DISPOSITION_BLOCKED",
                    used_ex_post_daily_volume_approximation=False,
                )
                self._execution_outcomes.append(outcome)
                return outcome

        # 6. 確認漲跌幅限制資料完整性（缺少必要資料時拋出 PriceLimitDataMissingError，絕不自行推測）
        if price_bounds.status == PriceLimitStatus.UNKNOWN_DATA:
            raise PriceLimitDataMissingError(
                f"標的 {clean_bar_sym} 於 {bar.trade_date} 無法決定價格限制: {price_bounds.reason}",
                symbol=clean_bar_sym,
                trade_date=bar.trade_date,
                missing_fields=("reference_price_or_limit_rule",),
                context={"reason": price_bounds.reason},
            )

        # 7. 驗證限價單 Tick Size 與當日漲跌停邊界
        if order.order_type == SignalOrderType.LIMIT:
            assert order.limit_price is not None
            if self.config.enforce_tick_size_on_limit_orders:
                if not self.tick_size_rule.is_valid_tick(order.limit_price, sym_ctx.instrument_category):
                    if self.config.raise_on_invalid_limit_tick:
                        self.tick_size_rule.validate_tick(
                            order.limit_price,
                            sym_ctx.instrument_category,
                            field_name="order.limit_price",
                            symbol=clean_bar_sym,
                        )
                    self._processed_order_day_pairs.add(order_day_key)
                    self._release_order_reservation_once(order, portfolio_ledger)
                    order.cancel(f"INVALID_TICK_SIZE: {order.limit_price}")
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=clean_bar_sym,
                        trade_date=bar.trade_date,
                        execution_timestamp=exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.REJECTED_INVALID_TICK_SIZE,
                        reason=f"委託限價 {order.limit_price} 不符合 Tick Size 規則",
                        fill_record=None,
                        price_limits=price_bounds,
                        volume_cap_basis="INVALID_TICK_SIZE",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    self._execution_outcomes.append(outcome)
                    return outcome

            if self.config.reject_limit_outside_daily_bounds and price_bounds.has_limits:
                assert price_bounds.limit_up is not None and price_bounds.limit_down is not None
                if order.limit_price > price_bounds.limit_up or order.limit_price < price_bounds.limit_down:
                    self._processed_order_day_pairs.add(order_day_key)
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=clean_bar_sym,
                        trade_date=bar.trade_date,
                        execution_timestamp=exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.UNFILLED_LIMIT_OUTSIDE_DAILY_BOUNDS,
                        reason=(
                            f"委託限價 ({order.limit_price}) 超出當日法定漲跌停範圍 "
                            f"[{price_bounds.limit_down}, {price_bounds.limit_up}]，不予成交"
                        ),
                        fill_record=None,
                        price_limits=price_bounds,
                        volume_cap_basis="LIMIT_OUT_OF_BOUNDS",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    self._execution_outcomes.append(outcome)
                    return outcome

        # 8. 漲停買單與跌停賣單之保守攔截檢查
        if price_bounds.has_limits:
            assert price_bounds.limit_up is not None and price_bounds.limit_down is not None
            if order.action == SignalAction.BUY:
                is_locked_up = (
                    bar.is_limit_up_locked
                    or (bar.open >= price_bounds.limit_up and bar.low >= price_bounds.limit_up)
                    or (self.config.conservative_close_at_limit_block and bar.close >= price_bounds.limit_up)
                    or (bar.open >= price_bounds.limit_up)
                )
                if is_locked_up:
                    self._processed_order_day_pairs.add(order_day_key)
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=clean_bar_sym,
                        trade_date=bar.trade_date,
                        execution_timestamp=exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.UNFILLED_LIMIT_UP_LOCKED,
                        reason=(
                            f"標的 {clean_bar_sym} 於 {bar.trade_date} 觸及或鎖死漲停價 "
                            f"({price_bounds.limit_up})，買單保守判定無法成交"
                        ),
                        fill_record=None,
                        price_limits=price_bounds,
                        volume_cap_basis="LOCKED_LIMIT_UP",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    self._execution_outcomes.append(outcome)
                    return outcome

            elif order.action in {SignalAction.SELL, SignalAction.CLOSE}:
                is_locked_down = (
                    bar.is_limit_down_locked
                    or (bar.open <= price_bounds.limit_down and bar.high <= price_bounds.limit_down)
                    or (self.config.conservative_close_at_limit_block and bar.close <= price_bounds.limit_down)
                    or (bar.open <= price_bounds.limit_down)
                )
                if is_locked_down:
                    self._processed_order_day_pairs.add(order_day_key)
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=clean_bar_sym,
                        trade_date=bar.trade_date,
                        execution_timestamp=exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.UNFILLED_LIMIT_DOWN_LOCKED,
                        reason=(
                            f"標的 {clean_bar_sym} 於 {bar.trade_date} 觸及或鎖死跌停價 "
                            f"({price_bounds.limit_down})，賣單保守判定無法成交"
                        ),
                        fill_record=None,
                        price_limits=price_bounds,
                        volume_cap_basis="LOCKED_LIMIT_DOWN",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    self._execution_outcomes.append(outcome)
                    return outcome

        # 9. 決定成交價 (DAILY_OPEN_CONSERVATIVE 市價單 vs 限價單)
        base_ref_price = bar.open
        exec_price: Optional[Decimal] = None
        match_reason_detail = ""

        if order.order_type == SignalOrderType.MARKET:
            if order.action == SignalAction.BUY:
                raw_slipped = base_ref_price * (_ONE + self.cost_model.slippage_rate)
                snapped = self.tick_size_rule.align_to_tick(
                    raw_slipped, TickRoundingMode.CEILING, sym_ctx.instrument_category
                )
                if price_bounds.has_limits and price_bounds.limit_up is not None:
                    snapped = min(snapped, price_bounds.limit_up)
                exec_price = snapped
                match_reason_detail = f"MARKET_BUY_OPEN_CONSERVATIVE(open={bar.open}, exec={exec_price})"
            else:
                raw_slipped = base_ref_price * (_ONE - self.cost_model.slippage_rate)
                if raw_slipped <= _ZERO:
                    raw_slipped = Decimal("0.01")
                snapped = self.tick_size_rule.align_to_tick(
                    raw_slipped, TickRoundingMode.FLOOR, sym_ctx.instrument_category
                )
                if price_bounds.has_limits and price_bounds.limit_down is not None:
                    snapped = max(snapped, price_bounds.limit_down)
                exec_price = snapped
                match_reason_detail = f"MARKET_SELL_OPEN_CONSERVATIVE(open={bar.open}, exec={exec_price})"

        else:
            # LIMIT 限價單
            limit_px = order.limit_price
            assert limit_px is not None

            if order.action == SignalAction.BUY:
                if bar.low > limit_px:
                    # 當日最低價始終高於買進限價 -> 完全未觸價
                    self._processed_order_day_pairs.add(order_day_key)
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=clean_bar_sym,
                        trade_date=bar.trade_date,
                        execution_timestamp=exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.UNFILLED_LIMIT_PRICE_NOT_REACHED,
                        reason=f"買單限價 ({limit_px}) 低於當日最低價 ({bar.low})，未觸價不成交",
                        fill_record=None,
                        price_limits=price_bounds,
                        volume_cap_basis="PRICE_NOT_REACHED",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    self._execution_outcomes.append(outcome)
                    return outcome

                if bar.open <= limit_px:
                    # 情況 A：開盤即低於或等於買單限價（包含開盤跳空向下穿越限價）
                    # 於開盤集合競價成交，基準為 bar.open 加滑價，但絕不高於 limit_px 與漲停價
                    raw_slipped = bar.open * (_ONE + self.cost_model.slippage_rate)
                    snapped = self.tick_size_rule.align_to_tick(
                        raw_slipped, TickRoundingMode.CEILING, sym_ctx.instrument_category
                    )
                    exec_price = min(limit_px, snapped)
                    if price_bounds.has_limits and price_bounds.limit_up is not None:
                        exec_price = min(exec_price, price_bounds.limit_up)
                    match_reason_detail = (
                        f"LIMIT_BUY_OPEN_GAP_OR_EQUAL(open={bar.open} <= limit={limit_px}, exec={exec_price})"
                    )
                else:
                    # 情況 B：開盤高於限價 (bar.open > limit_px)，但盤中拉回觸及 (bar.low <= limit_px)
                    # 因日線無日內完整軌跡，依 AmbiguousLimitPolicy 處理
                    if self.config.ambiguous_limit_policy == AmbiguousLimitPolicy.OPEN_ONLY_REJECT_INTRADAY:
                        self._processed_order_day_pairs.add(order_day_key)
                        outcome = OrderExecutionOutcome(
                            order_id=order.order_id,
                            signal_id=order.signal_id,
                            symbol=clean_bar_sym,
                            trade_date=bar.trade_date,
                            execution_timestamp=exec_dt,
                            action=order.action,
                            order_type=order.order_type,
                            order_quantity=order.order_quantity,
                            order_remaining_before=rem_before,
                            max_fillable_quantity=0,
                            filled_quantity=0,
                            unfilled_quantity=rem_before,
                            order_status_after=order.status,
                            outcome_code=ExecutionOutcomeCode.UNFILLED_AMBIGUOUS_INTRADAY_OHLC,
                            reason=(
                                f"開盤價 ({bar.open}) 高於買單限價 ({limit_px})，雖盤中最低 ({bar.low}) 觸及，"
                                f"但依 OPEN_ONLY_REJECT_INTRADAY 保守規則拒絕盤中路徑未知之限價成交"
                            ),
                            fill_record=None,
                            price_limits=price_bounds,
                            volume_cap_basis="AMBIGUOUS_OHLC_REJECTED",
                            used_ex_post_daily_volume_approximation=False,
                        )
                        self._execution_outcomes.append(outcome)
                        return outcome

                    if (
                        self.config.ambiguous_limit_policy == AmbiguousLimitPolicy.REQUIRE_STRICT_PENETRATION
                        and bar.low == limit_px
                    ):
                        self._processed_order_day_pairs.add(order_day_key)
                        outcome = OrderExecutionOutcome(
                            order_id=order.order_id,
                            signal_id=order.signal_id,
                            symbol=clean_bar_sym,
                            trade_date=bar.trade_date,
                            execution_timestamp=exec_dt,
                            action=order.action,
                            order_type=order.order_type,
                            order_quantity=order.order_quantity,
                            order_remaining_before=rem_before,
                            max_fillable_quantity=0,
                            filled_quantity=0,
                            unfilled_quantity=rem_before,
                            order_status_after=order.status,
                            outcome_code=ExecutionOutcomeCode.UNFILLED_AMBIGUOUS_INTRADAY_OHLC,
                            reason=(
                                f"盤中最低價 ({bar.low}) 僅剛好觸及買單限價 ({limit_px}) 未穿透，"
                                f"日線無法確認委託簿排隊順序，依 REQUIRE_STRICT_PENETRATION 保守判定不成交"
                            ),
                            fill_record=None,
                            price_limits=price_bounds,
                            volume_cap_basis="AMBIGUOUS_BOUNDARY_TOUCH_REJECTED",
                            used_ex_post_daily_volume_approximation=False,
                        )
                        self._execution_outcomes.append(outcome)
                        return outcome

                    # 保守以限價 limit_px 成交（絕不用更低的 bar.low 或 bar.close 給予不切實際優惠）
                    exec_price = limit_px
                    if price_bounds.has_limits and price_bounds.limit_up is not None:
                        exec_price = min(exec_price, price_bounds.limit_up)
                    match_reason_detail = (
                        f"LIMIT_BUY_INTRADAY_TOUCH_CONSERVATIVE(open={bar.open} > limit={limit_px} >= low={bar.low}, exec={exec_price})"
                    )

            else:
                # SELL / CLOSE 限價賣單
                if bar.high < limit_px:
                    self._processed_order_day_pairs.add(order_day_key)
                    outcome = OrderExecutionOutcome(
                        order_id=order.order_id,
                        signal_id=order.signal_id,
                        symbol=clean_bar_sym,
                        trade_date=bar.trade_date,
                        execution_timestamp=exec_dt,
                        action=order.action,
                        order_type=order.order_type,
                        order_quantity=order.order_quantity,
                        order_remaining_before=rem_before,
                        max_fillable_quantity=0,
                        filled_quantity=0,
                        unfilled_quantity=rem_before,
                        order_status_after=order.status,
                        outcome_code=ExecutionOutcomeCode.UNFILLED_LIMIT_PRICE_NOT_REACHED,
                        reason=f"賣單限價 ({limit_px}) 高於當日最高價 ({bar.high})，未觸價不成交",
                        fill_record=None,
                        price_limits=price_bounds,
                        volume_cap_basis="PRICE_NOT_REACHED",
                        used_ex_post_daily_volume_approximation=False,
                    )
                    self._execution_outcomes.append(outcome)
                    return outcome

                if bar.open >= limit_px:
                    # 情況 A：開盤即高於或等於賣單限價（包含開盤跳空向上穿越限價）
                    raw_slipped = bar.open * (_ONE - self.cost_model.slippage_rate)
                    if raw_slipped <= _ZERO:
                        raw_slipped = Decimal("0.01")
                    snapped = self.tick_size_rule.align_to_tick(
                        raw_slipped, TickRoundingMode.FLOOR, sym_ctx.instrument_category
                    )
                    exec_price = max(limit_px, snapped)
                    if price_bounds.has_limits and price_bounds.limit_down is not None:
                        exec_price = max(exec_price, price_bounds.limit_down)
                    match_reason_detail = (
                        f"LIMIT_SELL_OPEN_GAP_OR_EQUAL(open={bar.open} >= limit={limit_px}, exec={exec_price})"
                    )
                else:
                    # 情況 B：開盤低於限價 (bar.open < limit_px)，但盤中反彈觸及 (bar.high >= limit_px)
                    if self.config.ambiguous_limit_policy == AmbiguousLimitPolicy.OPEN_ONLY_REJECT_INTRADAY:
                        self._processed_order_day_pairs.add(order_day_key)
                        outcome = OrderExecutionOutcome(
                            order_id=order.order_id,
                            signal_id=order.signal_id,
                            symbol=clean_bar_sym,
                            trade_date=bar.trade_date,
                            execution_timestamp=exec_dt,
                            action=order.action,
                            order_type=order.order_type,
                            order_quantity=order.order_quantity,
                            order_remaining_before=rem_before,
                            max_fillable_quantity=0,
                            filled_quantity=0,
                            unfilled_quantity=rem_before,
                            order_status_after=order.status,
                            outcome_code=ExecutionOutcomeCode.UNFILLED_AMBIGUOUS_INTRADAY_OHLC,
                            reason=(
                                f"開盤價 ({bar.open}) 低於賣單限價 ({limit_px})，雖盤中最高 ({bar.high}) 觸及，"
                                f"但依 OPEN_ONLY_REJECT_INTRADAY 保守規則拒絕盤中路徑未知之限價成交"
                            ),
                            fill_record=None,
                            price_limits=price_bounds,
                            volume_cap_basis="AMBIGUOUS_OHLC_REJECTED",
                            used_ex_post_daily_volume_approximation=False,
                        )
                        self._execution_outcomes.append(outcome)
                        return outcome

                    if (
                        self.config.ambiguous_limit_policy == AmbiguousLimitPolicy.REQUIRE_STRICT_PENETRATION
                        and bar.high == limit_px
                    ):
                        self._processed_order_day_pairs.add(order_day_key)
                        outcome = OrderExecutionOutcome(
                            order_id=order.order_id,
                            signal_id=order.signal_id,
                            symbol=clean_bar_sym,
                            trade_date=bar.trade_date,
                            execution_timestamp=exec_dt,
                            action=order.action,
                            order_type=order.order_type,
                            order_quantity=order.order_quantity,
                            order_remaining_before=rem_before,
                            max_fillable_quantity=0,
                            filled_quantity=0,
                            unfilled_quantity=rem_before,
                            order_status_after=order.status,
                            outcome_code=ExecutionOutcomeCode.UNFILLED_AMBIGUOUS_INTRADAY_OHLC,
                            reason=(
                                f"盤中最高價 ({bar.high}) 僅剛好觸及賣單限價 ({limit_px}) 未穿透，"
                                f"日線無法確認委託簿排隊順序，依 REQUIRE_STRICT_PENETRATION 保守判定不成交"
                            ),
                            fill_record=None,
                            price_limits=price_bounds,
                            volume_cap_basis="AMBIGUOUS_BOUNDARY_TOUCH_REJECTED",
                            used_ex_post_daily_volume_approximation=False,
                        )
                        self._execution_outcomes.append(outcome)
                        return outcome

                    exec_price = limit_px
                    if price_bounds.has_limits and price_bounds.limit_down is not None:
                        exec_price = max(exec_price, price_bounds.limit_down)
                    match_reason_detail = (
                        f"LIMIT_SELL_INTRADAY_TOUCH_CONSERVATIVE(open={bar.open} < limit={limit_px} <= high={bar.high}, exec={exec_price})"
                    )

        assert exec_price is not None and exec_price > _ZERO

        # 再次確認成交價符合漲跌停與限價約束
        if price_bounds.has_limits:
            assert price_bounds.limit_up is not None and price_bounds.limit_down is not None
            if exec_price > price_bounds.limit_up or exec_price < price_bounds.limit_down:
                raise PriceLimitError(
                    f"計算出之成交價 ({exec_price}) 超出漲跌停範圍 [{price_bounds.limit_down}, {price_bounds.limit_up}]"
                )
        if order.order_type == SignalOrderType.LIMIT and order.limit_price is not None:
            if order.action == SignalAction.BUY and exec_price > order.limit_price:
                raise PriceLimitError(f"限價買單成交價 ({exec_price}) 不得高於限價 ({order.limit_price})")
            if order.action in {SignalAction.SELL, SignalAction.CLOSE} and exec_price < order.limit_price:
                raise PriceLimitError(f"限價賣單成交價 ({exec_price}) 不得低於限價 ({order.limit_price})")

        # 10. 賣單超賣與其他委託預留股數檢查
        if order.action in {SignalAction.SELL, SignalAction.CLOSE}:
            self._verify_sellable_shares_before_fill(
                order=order,
                symbol=clean_bar_sym,
                desired_shares=rem_before,
                portfolio_ledger=portfolio_ledger,
                lot_ledger=lot_ledger,
            )

        # 11. 計算成交量參與率上限與可成交股數
        vol_cap, vol_basis_str, used_ex_post, vol_warnings = self._compute_volume_cap(
            bar, sym_ctx, is_disposition=is_disposition
        )

        if not self.config.allow_odd_lots:
            vol_cap = (vol_cap // self.config.board_lot_shares) * self.config.board_lot_shares

        if vol_cap <= 0:
            self._processed_order_day_pairs.add(order_day_key)
            outcome = OrderExecutionOutcome(
                order_id=order.order_id,
                signal_id=order.signal_id,
                symbol=clean_bar_sym,
                trade_date=bar.trade_date,
                execution_timestamp=exec_dt,
                action=order.action,
                order_type=order.order_type,
                order_quantity=order.order_quantity,
                order_remaining_before=rem_before,
                max_fillable_quantity=0,
                filled_quantity=0,
                unfilled_quantity=rem_before,
                order_status_after=order.status,
                outcome_code=ExecutionOutcomeCode.UNFILLED_VOLUME_CAP_ZERO,
                reason=f"當日可參與成交量上限為 0 股 ({vol_basis_str})，無法成交",
                fill_record=None,
                price_limits=price_bounds,
                volume_cap_basis=vol_basis_str,
                used_ex_post_daily_volume_approximation=used_ex_post,
                warnings=vol_warnings,
            )
            self._execution_outcomes.append(outcome)
            return outcome

        fill_qty = min(rem_before, vol_cap)
        if not self.config.allow_odd_lots:
            fill_qty = (fill_qty // self.config.board_lot_shares) * self.config.board_lot_shares
            if fill_qty <= 0:
                self._processed_order_day_pairs.add(order_day_key)
                outcome = OrderExecutionOutcome(
                    order_id=order.order_id,
                    signal_id=order.signal_id,
                    symbol=clean_bar_sym,
                    trade_date=bar.trade_date,
                    execution_timestamp=exec_dt,
                    action=order.action,
                    order_type=order.order_type,
                    order_quantity=order.order_quantity,
                    order_remaining_before=rem_before,
                    max_fillable_quantity=vol_cap,
                    filled_quantity=0,
                    unfilled_quantity=rem_before,
                    order_status_after=order.status,
                    outcome_code=ExecutionOutcomeCode.UNFILLED_VOLUME_CAP_ZERO,
                    reason="可成交股數未滿一整張 (1,000 股) 且設定禁止零股，不予成交",
                    fill_record=None,
                    price_limits=price_bounds,
                    volume_cap_basis=vol_basis_str,
                    used_ex_post_daily_volume_approximation=used_ex_post,
                    warnings=vol_warnings,
                )
                self._execution_outcomes.append(outcome)
                return outcome

        # 12. 單一責任層計算成交價金、手續費、證交稅與滑價成本，建立 FillRecord
        is_odd_fill = (fill_qty % self.config.board_lot_shares) != 0
        notional = (exec_price * Decimal(fill_qty)).quantize(_CENT, rounding=ROUND_HALF_UP)
        commission = self.cost_model.calculate_commission(notional, is_odd_lot=is_odd_fill)
        tax = self.cost_model.calculate_tax(
            notional,
            order.action,
            sym_ctx.instrument_category,
            is_day_trading=sym_ctx.is_day_trading,
        )
        # 滑價金額：相對於開盤基準價或限價之價差乘以股數
        slippage_ref = (
            order.limit_price
            if (
                order.order_type == SignalOrderType.LIMIT
                and order.limit_price is not None
                and (
                    (order.action == SignalAction.BUY and bar.open > order.limit_price)
                    or (order.action in {SignalAction.SELL, SignalAction.CLOSE} and bar.open < order.limit_price)
                )
            )
            else base_ref_price
        )
        slippage_amount = (abs(exec_price - slippage_ref) * Decimal(fill_qty)).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )

        fill_id = self.generate_fill_id()
        if fill_id in self._emitted_fill_ids:
            raise DuplicateFillError(f"重複產生之 fill_id={fill_id!r}")

        fill = FillRecord(
            fill_id=fill_id,
            order_id=order.order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            fill_quantity=fill_qty,
            fill_price=exec_price,
            notional=notional,
            commission=commission,
            tax=tax,
            slippage=slippage_amount,
            timestamp=exec_dt,
        )

        # 13. 更新訂單狀態、PortfolioLedger 與 BacktestLotLedger（單次入帳，不重算費用）
        order.add_fill(fill)
        self._emitted_fill_ids.add(fill_id)
        self._all_fills.append(fill)
        self._processed_order_day_pairs.add(order_day_key)
        self._daily_consumed_volume[(clean_bar_sym, bar.trade_date)] = (
            self._daily_consumed_volume.get((clean_bar_sym, bar.trade_date), 0) + fill_qty
        )

        if settle_immediately:
            self._settle_fill_to_ledgers(
                order=order,
                fill=fill,
                rem_before=rem_before,
                portfolio_ledger=portfolio_ledger,
                lot_ledger=lot_ledger,
                strategy_id=strategy_id,
            )

        unfilled_after = order.remaining_quantity
        outcome_code = (
            ExecutionOutcomeCode.FILLED
            if unfilled_after == 0
            else ExecutionOutcomeCode.PARTIALLY_FILLED_VOLUME_CAP
        )

        outcome = OrderExecutionOutcome(
            order_id=order.order_id,
            signal_id=order.signal_id,
            symbol=clean_bar_sym,
            trade_date=bar.trade_date,
            execution_timestamp=exec_dt,
            action=order.action,
            order_type=order.order_type,
            order_quantity=order.order_quantity,
            order_remaining_before=rem_before,
            max_fillable_quantity=vol_cap,
            filled_quantity=fill_qty,
            unfilled_quantity=unfilled_after,
            order_status_after=order.status,
            outcome_code=outcome_code,
            reason=match_reason_detail,
            fill_record=fill,
            price_limits=price_bounds,
            volume_cap_basis=vol_basis_str,
            used_ex_post_daily_volume_approximation=used_ex_post,
            warnings=vol_warnings,
        )
        self._execution_outcomes.append(outcome)
        return outcome

    def _verify_sellable_shares_before_fill(
        self,
        *,
        order: PaperOrder,
        symbol: str,
        desired_shares: int,
        portfolio_ledger: Optional[PortfolioLedger],
        lot_ledger: Optional[BacktestLotLedger],
    ) -> None:
        """驗證賣單欲成交股數不超過實際持倉扣除「其他委託預留股數」後的可售股數。"""
        this_order_res = self._remaining_reserved_shares.get(order.order_id, order.reserved_shares)

        if portfolio_ledger is not None:
            pos = portfolio_ledger.positions.get(order.symbol) or portfolio_ledger.positions.get(symbol)
            held_qty = pos.quantity if pos is not None else 0
            total_res = portfolio_ledger.reserved_shares.get(
                order.symbol, portfolio_ledger.reserved_shares.get(symbol, 0)
            )
            other_orders_reserved = max(0, total_res - this_order_res)
            sellable_in_portfolio = held_qty - other_orders_reserved
            if desired_shares > sellable_in_portfolio:
                raise OversellPositionError(
                    f"賣單 {order.order_id} ({symbol}) 超賣攔截: 欲賣出 {desired_shares} 股，"
                    f"但實際總持倉 {held_qty} 股扣除其他委託預留 {other_orders_reserved} 股後，可售僅 {sellable_in_portfolio} 股",
                    context={
                        "order_id": order.order_id,
                        "symbol": symbol,
                        "desired_shares": desired_shares,
                        "held_quantity": held_qty,
                        "other_orders_reserved": other_orders_reserved,
                    },
                )

        if lot_ledger is not None:
            lot_held_qty = lot_ledger.get_position_quantity(symbol)
            other_res = 0
            if portfolio_ledger is not None:
                total_res = portfolio_ledger.reserved_shares.get(
                    order.symbol, portfolio_ledger.reserved_shares.get(symbol, 0)
                )
                other_res = max(0, total_res - this_order_res)
            sellable_in_lots = lot_held_qty - other_res
            if desired_shares > sellable_in_lots:
                raise OversellPositionError(
                    f"賣單 {order.order_id} ({symbol}) 超出批次帳本可售股數: 欲賣出 {desired_shares} 股，"
                    f"但 BacktestLotLedger 持倉 {lot_held_qty} 股扣除其他預留 {other_res} 股後僅剩 {sellable_in_lots} 股",
                    context={
                        "order_id": order.order_id,
                        "symbol": symbol,
                        "desired_shares": desired_shares,
                        "lot_held_quantity": lot_held_qty,
                    },
                )

    def settle_outcome_fill(
        self,
        outcome: OrderExecutionOutcome,
        order: PaperOrder,
        *,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        lot_ledger: Optional[BacktestLotLedger] = None,
        strategy_id: str = "default",
    ) -> None:
        """將 `match_order(..., settle_immediately=False)` 產生之 `FillRecord` 單次交割入帳。"""
        if outcome.fill_record is None or outcome.filled_quantity <= 0:
            return
        if order.order_id != outcome.order_id:
            raise BacktestExecutionError(
                f"order.order_id ({order.order_id}) 與 outcome.order_id ({outcome.order_id}) 不一致"
            )
        self._settle_fill_to_ledgers(
            order=order,
            fill=outcome.fill_record,
            rem_before=outcome.order_remaining_before,
            portfolio_ledger=portfolio_ledger,
            lot_ledger=lot_ledger,
            strategy_id=strategy_id,
        )

    def _settle_fill_to_ledgers(
        self,
        *,
        order: PaperOrder,
        fill: FillRecord,
        rem_before: int,
        portfolio_ledger: Optional[PortfolioLedger],
        lot_ledger: Optional[BacktestLotLedger],
        strategy_id: str,
    ) -> None:
        """將單筆 `FillRecord` 交割至 `PortfolioLedger` 與 `BacktestLotLedger`，保證費用不重複計算、預留款不殘留尾差。"""
        if fill.fill_id in self._settled_fill_ids:
            raise DuplicateFillError(
                f"FillRecord {fill.fill_id} 已完成交割入帳，嚴禁重複交割",
                context={"fill_id": fill.fill_id, "order_id": order.order_id},
            )
        self._settled_fill_ids.add(fill.fill_id)

        if order.action == SignalAction.BUY:
            rem_res_cash = self._remaining_reserved_cash.get(order.order_id, Decimal("0.00"))
            if order.remaining_quantity == 0:
                # 完全成交：全數沖銷該筆委託剩餘預留金，消除任何比例分攤之 0.01 尾差
                reserved_deduct = rem_res_cash
            else:
                ratio = Decimal(fill.fill_quantity) / Decimal(rem_before)
                reserved_deduct = (rem_res_cash * ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
            self._remaining_reserved_cash[order.order_id] = max(_ZERO, rem_res_cash - reserved_deduct)
            if order.remaining_quantity == 0:
                self._reservation_released_order_ids.add(order.order_id)

            if portfolio_ledger is not None:
                portfolio_ledger.record_buy_fill(
                    symbol=order.symbol,
                    fill_shares=fill.fill_quantity,
                    fill_price=fill.fill_price,
                    notional=fill.notional,
                    fee=fill.commission,
                    reserved_cash_deduct=reserved_deduct,
                )
        else:
            rem_res_shares = self._remaining_reserved_shares.get(order.order_id, 0)
            reserved_shares_deduct = min(rem_res_shares, fill.fill_quantity)
            self._remaining_reserved_shares[order.order_id] = max(0, rem_res_shares - reserved_shares_deduct)
            if order.remaining_quantity == 0:
                self._reservation_released_order_ids.add(order.order_id)

            if portfolio_ledger is not None:
                portfolio_ledger.record_sell_fill(
                    symbol=order.symbol,
                    fill_shares=fill.fill_quantity,
                    fill_price=fill.fill_price,
                    notional=fill.notional,
                    fee=fill.commission,
                    tax=fill.tax,
                    reserved_shares_deduct=reserved_shares_deduct,
                )

        if lot_ledger is not None:
            lot_ledger.record_order_fill(fill, order, strategy_id=strategy_id)

    # ── 未成交委託到期與取消（保證預留資金與股數僅釋放一次） ──────────

    def _release_order_reservation_once(
        self,
        order: PaperOrder,
        portfolio_ledger: Optional[PortfolioLedger],
    ) -> Tuple[Decimal, int]:
        """釋放委託單尚未消耗之預留現金與預留股數，並保證每張委託最多只釋放一次。"""
        if order.order_id in self._reservation_released_order_ids:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 之預留款券已經釋放過，嚴禁重複釋放",
                context={"order_id": order.order_id},
            )

        refund_cash = self._remaining_reserved_cash.get(order.order_id)
        if refund_cash is None:
            if order.order_quantity > 0 and order.remaining_quantity > 0:
                ratio = Decimal(order.remaining_quantity) / Decimal(order.order_quantity)
                refund_cash = (order.reserved_cash * ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
            else:
                refund_cash = _ZERO

        refund_shares = self._remaining_reserved_shares.get(order.order_id)
        if refund_shares is None:
            refund_shares = min(order.reserved_shares, order.remaining_quantity)

        if portfolio_ledger is not None:
            if order.action == SignalAction.BUY and refund_cash > _ZERO:
                portfolio_ledger.release_buy_funds(refund_cash)
            elif order.action in {SignalAction.SELL, SignalAction.CLOSE} and refund_shares > 0:
                portfolio_ledger.release_sell_shares(order.symbol, refund_shares)

        self._remaining_reserved_cash[order.order_id] = _ZERO
        self._remaining_reserved_shares[order.order_id] = 0
        self._reservation_released_order_ids.add(order.order_id)
        return refund_cash, refund_shares

    def expire_order(
        self,
        order: PaperOrder,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        *,
        reason: str = "當日收盤未成交自動到期作廢",
    ) -> Tuple[Decimal, int]:
        """將單筆未完全成交之活躍委託轉為 `EXPIRED`，並精確釋放剩餘預留資金或股數（僅限一次）。"""
        if not isinstance(order, PaperOrder):
            raise BacktestExecutionError(f"order 必須為 PaperOrder 實例，收到 {type(order).__name__}")
        if order.is_terminal or order.order_id in self._reservation_released_order_ids:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 已處於終態 ({order.status.value}) 或已釋放過預留款券，嚴禁重複到期釋放",
                context={"order_id": order.order_id, "status": order.status.value},
            )
        refund_cash, refund_shares = self._release_order_reservation_once(order, portfolio_ledger)
        order.expire(reason)
        return refund_cash, refund_shares

    def cancel_order(
        self,
        order: PaperOrder,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        *,
        reason: str = "委託取消",
    ) -> Tuple[Decimal, int]:
        """撤銷單筆未完全成交之活躍委託 (`CANCELLED`)，並精確釋放剩餘預留資金或股數（僅限一次）。"""
        if not isinstance(order, PaperOrder):
            raise BacktestExecutionError(f"order 必須為 PaperOrder 實例，收到 {type(order).__name__}")
        if order.is_terminal or order.order_id in self._reservation_released_order_ids:
            raise DuplicateOrderExecutionError(
                f"委託單 {order.order_id} 已處於終態 ({order.status.value}) 或已釋放過預留款券，嚴禁重複取消釋放",
                context={"order_id": order.order_id, "status": order.status.value},
            )
        refund_cash, refund_shares = self._release_order_reservation_once(order, portfolio_ledger)
        order.cancel(reason)
        return refund_cash, refund_shares

    def expire_open_orders_at_close(
        self,
        orders: Iterable[PaperOrder],
        portfolio_ledger: Optional[PortfolioLedger] = None,
        *,
        trade_date: Optional[dt.date] = None,
    ) -> Tuple[PaperOrder, ...]:
        """於當日 13:30 收盤時將所有仍處於 `ACCEPTED` / `PARTIALLY_FILLED` 之委託到期作廢並釋放剩餘預留款券。"""
        expired_list: List[PaperOrder] = []
        date_label = f" ({trade_date.isoformat()})" if trade_date is not None else ""
        for order in orders:
            if order.is_active:
                self.expire_order(
                    order,
                    portfolio_ledger=portfolio_ledger,
                    reason=f"當日收盤未成交自動到期作廢{date_label}",
                )
                expired_list.append(order)
        return tuple(expired_list)
