"""台股歷史回測 Point-in-Time (PIT) 資料供應器模組 (data_provider.py).

核心原則：
1. 明確區分「資料發生時間 (event_time / trade_date)」、「資料發布時間 (published_at)」與「策略實際可用時間 (available_at)」。
2. 若資料格式缺少發布時間或可用時間，嚴禁自行推定為當時已可用；一律標記為 `UNVERIFIED_MISSING_TIMESTAMPS`，並在正式 PIT 查詢時拋出 `PITViolationError`。
3. 嚴格攔截盘後資料回填盤中：日線收盤行情 (DAILY) 之事件完成時間與發布/可用時間不得早於當日台股 13:30 收盤。
4. 嚴格阻止同日收盤成交：對於盤後才取得的 T 日收盤資訊，強制阻止策略在 T 日盤中或 T 日收盤成交（下一交易日之執行排程交由後續撮合層處理）。
5. 嚴格驗證 OHLCV 數值範圍、高低開收邏輯關係、NaN/Infinity、成交量合法性及時間序列嚴格遞增排序。
6. 完整保留資料來源 (source)、擷取時間 (retrieved_at)、資料版本 (dataset_version) 與合成／真實資料標記 (is_synthetic)，嚴禁混淆。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import math
import re
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ..broker.engine import MarketBar
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .exceptions import (
    BacktestCalendarError,
    DataProvenanceError,
    HistoricalDataMissingError,
    InvalidOHLCVError,
    LookaheadBiasError,
    PITViolationError,
    UnsupportedDataFormatError,
)

# 台股普通交易日收盤時間 (13:30 Asia/Taipei)
TWSE_MARKET_OPEN_TIME: dt.time = dt.time(9, 0, 0)
TWSE_MARKET_CLOSE_TIME: dt.time = dt.time(13, 30, 0)

# 台股標準 4~6 碼代號 (不含後綴，或允許標準化前檢驗)
_TW_PLAIN_SYMBOL_RE = re.compile(r"^[0-9A-Z]{4,6}$")

# 禁止偽裝為真實歷史資料 (is_synthetic=False) 之關鍵字
_SYNTHETIC_SPOOF_TOKENS: Tuple[str, ...] = (
    "SYNTHETIC",
    "MOCK",
    "FAKE",
    "DUMMY",
    "FIXTURE",
    "TEST",
)

# 禁止偽裝為合成資料 (is_synthetic=True) 之真實官方標記關鍵字
_REAL_SPOOF_TOKENS: Tuple[str, ...] = (
    "OFFICIAL_REAL_EXCHANGE",
    "VERIFIED_REAL_MARKET",
)


class DataFrequency(str, Enum):
    """回測資料頻率列舉（目前基礎層僅支援日線 DAILY）。"""

    DAILY = "DAILY"


class PriceBasis(str, Enum):
    """價格基準列舉。"""

    RAW_UNADJUSTED = "RAW_UNADJUSTED"                      # 原始未還原成交價（回測撮合唯一合法基準）
    CAUSAL_BACKWARD_ADJUSTED = "CAUSAL_BACKWARD_ADJUSTED"  # 時點因果向後還原價（僅供相對特徵計算）
    FORWARD_ADJUSTED = "FORWARD_ADJUSTED"                  # 前復權價（含未來除權息因子，PIT 回測嚴格禁用）


class PITVerificationStatus(str, Enum):
    """Point-in-Time (PIT) 時間戳驗證狀態。"""

    VERIFIED_STRICT_PIT = "VERIFIED_STRICT_PIT"
    UNVERIFIED_MISSING_TIMESTAMPS = "UNVERIFIED_MISSING_TIMESTAMPS"


def _normalize_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise HistoricalDataMissingError(
            "symbol 必須為非空字串",
            symbol=str(symbol) if symbol is not None else None,
            missing_fields=("symbol",),
        )
    clean = symbol.strip()
    if not _TW_PLAIN_SYMBOL_RE.match(clean):
        raise InvalidOHLCVError(
            f"無效的台股代號格式: {symbol!r} (必須為 4~6 碼不含後綴之英數字代號，如 '2330')",
            symbol=clean,
            field_name="symbol",
            invalid_value=symbol,
        )
    return clean


def _validate_positive_decimal_price(
    value: object,
    field_name: str,
    symbol: str,
    trade_date: dt.date,
) -> Decimal:
    """嚴格檢驗價格欄位：拒絕 None、bool、NaN、Infinity、非正數或非數值型別。"""
    if value is None:
        raise HistoricalDataMissingError(
            f"標的 {symbol} 於 {trade_date} 缺少必要價格欄位 {field_name}",
            symbol=symbol,
            trade_date=trade_date,
            missing_fields=(field_name,),
        )
    if isinstance(value, bool):
        raise InvalidOHLCVError(
            f"價格欄位 {field_name} 不得為布林值 (bool)",
            symbol=symbol,
            trade_date=trade_date,
            field_name=field_name,
            invalid_value=value,
        )
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidOHLCVError(
                f"價格欄位 {field_name} 包含非法浮點數 (NaN 或 Infinity): {value!r}",
                symbol=symbol,
                trade_date=trade_date,
                field_name=field_name,
                invalid_value=value,
            )
        raise UnsupportedDataFormatError(
            f"價格欄位 {field_name} 必須使用精確 Decimal，禁止傳入 float ({value!r})",
            format_type=type(value).__name__,
            context={"symbol": symbol, "trade_date": trade_date.isoformat(), "field_name": field_name},
        )
    if isinstance(value, int):
        dec_val = Decimal(value)
    elif isinstance(value, Decimal):
        dec_val = value
    else:
        raise InvalidOHLCVError(
            f"價格欄位 {field_name} 型別不合法，預期 Decimal，收到 {type(value).__name__}",
            symbol=symbol,
            trade_date=trade_date,
            field_name=field_name,
            invalid_value=value,
        )

    if dec_val.is_nan() or dec_val.is_infinite() or not dec_val.is_finite():
        raise InvalidOHLCVError(
            f"價格欄位 {field_name} 不得為 NaN 或 Infinity: {dec_val!r}",
            symbol=symbol,
            trade_date=trade_date,
            field_name=field_name,
            invalid_value=dec_val,
        )
    if dec_val <= Decimal("0"):
        raise InvalidOHLCVError(
            f"價格欄位 {field_name} 必須為大於 0 之有限正數，收到 {dec_val!r}",
            symbol=symbol,
            trade_date=trade_date,
            field_name=field_name,
            invalid_value=dec_val,
        )
    return dec_val


def _validate_volume(
    value: object,
    symbol: str,
    trade_date: dt.date,
    *,
    is_halted: bool,
) -> int:
    """嚴格檢驗成交量 volume：拒絕 None、bool、float/Decimal (含 NaN/Inf)、負數及非停牌零量。"""
    if value is None:
        raise HistoricalDataMissingError(
            f"標的 {symbol} 於 {trade_date} 缺少必要欄位 volume",
            symbol=symbol,
            trade_date=trade_date,
            missing_fields=("volume",),
        )
    if isinstance(value, bool):
        raise InvalidOHLCVError(
            "成交量 volume 不得為布林值 (bool)",
            symbol=symbol,
            trade_date=trade_date,
            field_name="volume",
            invalid_value=value,
        )
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidOHLCVError(
                f"成交量 volume 不得為 NaN 或 Infinity: {value!r}",
                symbol=symbol,
                trade_date=trade_date,
                field_name="volume",
                invalid_value=value,
            )
        raise InvalidOHLCVError(
            f"成交量 volume 必須為整數股數 (int)，不得為 float: {value!r}",
            symbol=symbol,
            trade_date=trade_date,
            field_name="volume",
            invalid_value=value,
        )
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite() or not value.is_finite():
            raise InvalidOHLCVError(
                f"成交量 volume 不得為 NaN 或 Infinity: {value!r}",
                symbol=symbol,
                trade_date=trade_date,
                field_name="volume",
                invalid_value=value,
            )
        raise InvalidOHLCVError(
            f"成交量 volume 必須為整數股數 (int)，不得為 Decimal: {value!r}",
            symbol=symbol,
            trade_date=trade_date,
            field_name="volume",
            invalid_value=value,
        )
    if not isinstance(value, int):
        raise InvalidOHLCVError(
            f"成交量 volume 必須為整數股數 (int)，收到 {type(value).__name__}",
            symbol=symbol,
            trade_date=trade_date,
            field_name="volume",
            invalid_value=value,
        )
    if value < 0:
        raise InvalidOHLCVError(
            f"成交量 volume 不得為負數，收到 {value}",
            symbol=symbol,
            trade_date=trade_date,
            field_name="volume",
            invalid_value=value,
        )
    if not is_halted and value == 0:
        raise InvalidOHLCVError(
            f"標的 {symbol} 於 {trade_date} 未標記停牌 (is_halted=False)，但成交量 volume 為 0",
            symbol=symbol,
            trade_date=trade_date,
            field_name="volume",
            invalid_value=value,
        )
    return value


@dataclass(frozen=True)
class DataProvenance:
    """歷史資料來源、擷取時間、資料版本與真實／合成標記元資料。"""

    source: str
    dataset_version: str
    retrieved_at: dt.datetime
    is_synthetic: bool
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise DataProvenanceError(
                "資料來源 source 不得為空字串或 None",
                source=self.source,
                dataset_version=self.dataset_version,
                is_synthetic=self.is_synthetic if isinstance(self.is_synthetic, bool) else None,
            )
        if not isinstance(self.dataset_version, str) or not self.dataset_version.strip():
            raise DataProvenanceError(
                "資料版本 dataset_version 不得為空字串或 None",
                source=self.source,
                dataset_version=self.dataset_version,
                is_synthetic=self.is_synthetic if isinstance(self.is_synthetic, bool) else None,
            )
        if not isinstance(self.is_synthetic, bool):
            raise DataProvenanceError(
                f"is_synthetic 必須是明確的布林值 (bool)，收到 {self.is_synthetic!r}",
                source=self.source,
                dataset_version=self.dataset_version,
            )
        if self.retrieved_at is None or not isinstance(self.retrieved_at, dt.datetime):
            raise DataProvenanceError(
                "retrieved_at (資料擷取時間) 必須提供且為 timezone-aware datetime",
                source=self.source,
                dataset_version=self.dataset_version,
                is_synthetic=self.is_synthetic,
            )
        if self.retrieved_at.tzinfo is None or self.retrieved_at.tzinfo.utcoffset(self.retrieved_at) is None:
            raise DataProvenanceError(
                "retrieved_at 必須具備明確時區 (timezone-aware)",
                source=self.source,
                dataset_version=self.dataset_version,
                is_synthetic=self.is_synthetic,
            )
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(TAIPEI_TZ))

        upper_text = f"{self.source.upper()}::{self.dataset_version.upper()}::{self.description.upper()}"
        if not self.is_synthetic:
            for token in _SYNTHETIC_SPOOF_TOKENS:
                if token in upper_text:
                    raise DataProvenanceError(
                        f"合成或測試資料嚴禁偽裝標記為真實歷史資料 (is_synthetic=False，但來源/版本包含 {token!r})",
                        source=self.source,
                        dataset_version=self.dataset_version,
                        is_synthetic=self.is_synthetic,
                        context={"spoof_token": token},
                    )
        else:
            for token in _REAL_SPOOF_TOKENS:
                if token in upper_text:
                    raise DataProvenanceError(
                        f"真實資料標記與 is_synthetic=True 衝突 (偵測到 {token!r})，合成資料與真實資料標記不可混淆",
                        source=self.source,
                        dataset_version=self.dataset_version,
                        is_synthetic=self.is_synthetic,
                        context={"spoof_token": token},
                    )


@dataclass(frozen=True)
class HistoricalBarRecord:
    """具備完整 Point-in-Time (PIT) 時間戳與資料血統之單日歷史行情紀錄。

    時間欄位語意區分：
    - `trade_date`: 行情所屬之台股交易日 (T)。
    - `event_time`: 市場上該筆 K 線實際形成之事件時間（日線必須 >= T 日 13:30+08:00 收盤）。
    - `published_at`: 交易所或資料供應商實際發布該筆完整資料之時間（若未知則為 None）。
    - `available_at`: 策略在回測中實際被允許讀取該筆資料之時間（必須 >= published_at >= event_time；若未知則為 None）。
    """

    symbol: str
    trade_date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    event_time: dt.datetime
    published_at: Optional[dt.datetime]
    available_at: Optional[dt.datetime]
    provenance: DataProvenance
    yesterday_close: Optional[Decimal] = None
    frequency: DataFrequency = DataFrequency.DAILY
    price_basis: PriceBasis = PriceBasis.RAW_UNADJUSTED
    is_halted: bool = False
    is_disposition: bool = False
    is_limit_up_locked: bool = False
    is_limit_down_locked: bool = False

    def __post_init__(self) -> None:
        # 1. 股票代號與交易日驗證
        clean_sym = _normalize_symbol(self.symbol)
        object.__setattr__(self, "symbol", clean_sym)

        if self.trade_date is None:
            raise HistoricalDataMissingError(
                f"標的 {clean_sym} 缺少 trade_date",
                symbol=clean_sym,
                missing_fields=("trade_date",),
            )
        t_date = to_taipei_date(self.trade_date, "trade_date", allow_datetime=False)
        object.__setattr__(self, "trade_date", t_date)

        # 2. 資料血統驗證
        if self.provenance is None or not isinstance(self.provenance, DataProvenance):
            raise DataProvenanceError(
                f"標的 {clean_sym} 於 {t_date} 缺少有效之 DataProvenance 元資料"
            )

        # 3. 資料頻率與價格基準驗證
        if not isinstance(self.frequency, DataFrequency):
            raise UnsupportedDataFormatError(
                f"不支援的資料頻率: {self.frequency!r}，目前基礎層僅支援 DataFrequency.DAILY",
                frequency=self.frequency,
            )
        if not isinstance(self.price_basis, PriceBasis):
            raise UnsupportedDataFormatError(
                f"不支援的價格基準格式: {self.price_basis!r}",
                format_type=self.price_basis,
            )
        if self.price_basis == PriceBasis.FORWARD_ADJUSTED:
            raise UnsupportedDataFormatError(
                f"標的 {clean_sym} 於 {t_date} 使用前復權價格 (FORWARD_ADJUSTED)，含未來除權息因子，PIT 回測嚴格禁用",
                format_type=self.price_basis.value,
                context={"symbol": clean_sym, "trade_date": t_date.isoformat()},
            )

        # 4. 布林旗標驗證
        for flag_name in ("is_halted", "is_disposition", "is_limit_up_locked", "is_limit_down_locked"):
            flag_val = getattr(self, flag_name)
            if not isinstance(flag_val, bool):
                raise InvalidOHLCVError(
                    f"{flag_name} 必須為 bool，收到 {flag_val!r}",
                    symbol=clean_sym,
                    trade_date=t_date,
                    field_name=flag_name,
                    invalid_value=flag_val,
                )

        # 5. OHLCV 數值與高低開收關係驗證
        o_px = _validate_positive_decimal_price(self.open, "open", clean_sym, t_date)
        h_px = _validate_positive_decimal_price(self.high, "high", clean_sym, t_date)
        l_px = _validate_positive_decimal_price(self.low, "low", clean_sym, t_date)
        c_px = _validate_positive_decimal_price(self.close, "close", clean_sym, t_date)
        vol = _validate_volume(self.volume, clean_sym, t_date, is_halted=self.is_halted)

        if h_px < l_px:
            raise InvalidOHLCVError(
                f"標的 {clean_sym} 於 {t_date} 非法 OHLC 關係: high ({h_px}) < low ({l_px})",
                symbol=clean_sym,
                trade_date=t_date,
                field_name="high/low",
                context={"open": str(o_px), "high": str(h_px), "low": str(l_px), "close": str(c_px)},
            )
        if o_px > h_px or o_px < l_px:
            raise InvalidOHLCVError(
                f"標的 {clean_sym} 於 {t_date} 非法 OHLC 關係: open ({o_px}) 超出 [low ({l_px}), high ({h_px})] 範圍",
                symbol=clean_sym,
                trade_date=t_date,
                field_name="open",
                invalid_value=o_px,
            )
        if c_px > h_px or c_px < l_px:
            raise InvalidOHLCVError(
                f"標的 {clean_sym} 於 {t_date} 非法 OHLC 關係: close ({c_px}) 超出 [low ({l_px}), high ({h_px})] 範圍",
                symbol=clean_sym,
                trade_date=t_date,
                field_name="close",
                invalid_value=c_px,
            )

        object.__setattr__(self, "open", o_px)
        object.__setattr__(self, "high", h_px)
        object.__setattr__(self, "low", l_px)
        object.__setattr__(self, "close", c_px)
        object.__setattr__(self, "volume", vol)

        if self.yesterday_close is not None:
            y_px = _validate_positive_decimal_price(self.yesterday_close, "yesterday_close", clean_sym, t_date)
            object.__setattr__(self, "yesterday_close", y_px)

        # 6. 事件時間、發布時間與可用時間 (PIT 規則驗證)
        if self.event_time is None:
            raise HistoricalDataMissingError(
                f"標的 {clean_sym} 於 {t_date} 缺少 event_time",
                symbol=clean_sym,
                trade_date=t_date,
                missing_fields=("event_time",),
            )
        try:
            ev_time = ensure_taipei_datetime(self.event_time, "event_time")
        except BacktestCalendarError as exc:
            raise PITViolationError(
                f"event_time 時區或型別不合法: {exc.message}",
                symbol=clean_sym,
                trade_date=t_date,
            ) from exc
        object.__setattr__(self, "event_time", ev_time)

        market_close_dt = dt.datetime.combine(t_date, TWSE_MARKET_CLOSE_TIME, tzinfo=TAIPEI_TZ)
        if self.frequency == DataFrequency.DAILY:
            if ev_time.date() != t_date:
                raise PITViolationError(
                    f"標的 {clean_sym} 日線 event_time 日期 ({ev_time.date()}) 與 trade_date ({t_date}) 不一致",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                )
            if ev_time < market_close_dt:
                raise PITViolationError(
                    f"標的 {clean_sym} 於 {t_date} 之日線收盤行情 event_time ({ev_time}) 早於當日 13:30 收盤時間，違反盤後收盤語意",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                )

        pub_time: Optional[dt.datetime] = None
        if self.published_at is not None:
            try:
                pub_time = ensure_taipei_datetime(self.published_at, "published_at")
            except BacktestCalendarError as exc:
                raise PITViolationError(
                    f"published_at 時區或型別不合法: {exc.message}",
                    symbol=clean_sym,
                    trade_date=t_date,
                ) from exc
            if pub_time < market_close_dt:
                raise PITViolationError(
                    f"盤後發布之日線資料 published_at ({pub_time}) 不得回填至當日盤中 (< {market_close_dt})",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                    published_at=pub_time,
                )
            if pub_time < ev_time:
                raise PITViolationError(
                    f"資料發布時間 published_at ({pub_time}) 不得早於事件發生時間 event_time ({ev_time})",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                    published_at=pub_time,
                )
            object.__setattr__(self, "published_at", pub_time)

        avail_time: Optional[dt.datetime] = None
        if self.available_at is not None:
            try:
                avail_time = ensure_taipei_datetime(self.available_at, "available_at")
            except BacktestCalendarError as exc:
                raise PITViolationError(
                    f"available_at 時區或型別不合法: {exc.message}",
                    symbol=clean_sym,
                    trade_date=t_date,
                ) from exc
            if avail_time < market_close_dt:
                raise PITViolationError(
                    f"盤後發布之日線資料 available_at ({avail_time}) 不得回填至當日盤中 (< {market_close_dt})",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                    available_at=avail_time,
                )
            if avail_time < ev_time:
                raise PITViolationError(
                    f"策略可用時間 available_at ({avail_time}) 不得早於事件發生時間 event_time ({ev_time})",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                    available_at=avail_time,
                )
            if pub_time is not None and avail_time < pub_time:
                raise PITViolationError(
                    f"策略可用時間 available_at ({avail_time}) 不得早於資料發布時間 published_at ({pub_time})",
                    symbol=clean_sym,
                    trade_date=t_date,
                    event_time=ev_time,
                    published_at=pub_time,
                    available_at=avail_time,
                )
            object.__setattr__(self, "available_at", avail_time)

    @property
    def pit_status(self) -> PITVerificationStatus:
        """回傳該筆資料之 PIT 驗證狀態。

        若缺少 `published_at` 或 `available_at`，絕不自動推定為當時已可用，
        而是明確標記為 `UNVERIFIED_MISSING_TIMESTAMPS`。
        """
        if self.published_at is None or self.available_at is None:
            return PITVerificationStatus.UNVERIFIED_MISSING_TIMESTAMPS
        return PITVerificationStatus.VERIFIED_STRICT_PIT

    @property
    def is_pit_verified(self) -> bool:
        """是否已具備完整且通過檢驗之 PIT 發布與可用時間戳。"""
        return self.pit_status == PITVerificationStatus.VERIFIED_STRICT_PIT

    @property
    def allows_same_day_close_execution(self) -> bool:
        """日線收盤資訊屬於盤後可得，永遠禁止在當日 (trade_date) 收盤成交。"""
        return False

    def assert_pit_verified(self) -> None:
        """要求此紀錄必須具備完整之 `published_at` 與 `available_at`，否則拋出 `PITViolationError`。"""
        if not self.is_pit_verified:
            raise PITViolationError(
                f"標的 {self.symbol} 於 {self.trade_date} 缺少 published_at 或 available_at，"
                f"狀態為 {self.pit_status.value}，不得無條件通過正式 PIT 驗證",
                symbol=self.symbol,
                trade_date=self.trade_date,
                event_time=self.event_time,
                published_at=self.published_at,
                available_at=self.available_at,
            )

    def assert_available_as_of(self, cutoff_time: dt.datetime, *, require_pit_verified: bool = True) -> None:
        """驗證此筆資料在策略截止時間 `cutoff_time` 前是否合法可用。"""
        cutoff_taipei = ensure_taipei_datetime(cutoff_time, "cutoff_time")

        if require_pit_verified:
            self.assert_pit_verified()

        if self.trade_date > cutoff_taipei.date():
            raise LookaheadBiasError(
                f"未來資料洩漏: 請求標的 {self.symbol} 於 {self.trade_date} 之行情，晚於截止日期 ({cutoff_taipei.date()})",
                symbol=self.symbol,
                trade_date=self.trade_date,
                available_at=self.available_at,
                cutoff_time=cutoff_taipei,
            )

        # 防止盤後收盤資料回填至當日盤中 (即使 allow_unverified_pit=True 亦不得在當日 13:30 收盤前讀取當日日線)
        if cutoff_taipei < self.event_time:
            raise LookaheadBiasError(
                f"盤後資料不可回填至盤中: 標的 {self.symbol} 於 {self.trade_date} 之事件完成時間為 {self.event_time}，"
                f"晚於查詢截止時間 {cutoff_taipei}",
                symbol=self.symbol,
                trade_date=self.trade_date,
                available_at=self.available_at or self.event_time,
                cutoff_time=cutoff_taipei,
            )

        if self.available_at is not None and cutoff_taipei < self.available_at:
            raise LookaheadBiasError(
                f"資料可用時間晚於策略截止時間: 標的 {self.symbol} 於 {self.trade_date} 實際可用時間為 {self.available_at}，"
                f"晚於策略截止時間 {cutoff_taipei}，拒絕使用",
                symbol=self.symbol,
                trade_date=self.trade_date,
                available_at=self.available_at,
                cutoff_time=cutoff_taipei,
            )

    def assert_not_same_day_execution(self, proposed_execution_time: dt.datetime) -> None:
        """阻止策略利用盤後才取得的 T 日收盤資訊在 T 日盤中或 T 日收盤成交。

        下一交易日的執行排程應交由後續撮合層處理。
        """
        exec_taipei = ensure_taipei_datetime(proposed_execution_time, "proposed_execution_time")
        self.assert_pit_verified()
        assert self.available_at is not None

        if exec_taipei.date() <= self.trade_date:
            raise LookaheadBiasError(
                f"禁止同日收盤成交: 標的 {self.symbol} 於 {self.trade_date} 之收盤資訊於盤後 ({self.available_at}) 始可取得，"
                f"嚴禁在 {exec_taipei} (<= 交易日 {self.trade_date}) 執行成交；必須排程至下一交易日執行",
                symbol=self.symbol,
                trade_date=self.trade_date,
                available_at=self.available_at,
                execution_time=exec_taipei,
            )
        if exec_taipei <= self.available_at:
            raise LookaheadBiasError(
                f"因果時序違規: 預定成交時間 ({exec_taipei}) 不得早於或等於資料可用時間 ({self.available_at})",
                symbol=self.symbol,
                trade_date=self.trade_date,
                available_at=self.available_at,
                execution_time=exec_taipei,
            )

    def to_market_bar(self, fallback_yesterday_close: Optional[Decimal] = None) -> MarketBar:
        """轉換為既有模擬券商層之 `MarketBar` 物件。"""
        y_close = self.yesterday_close if self.yesterday_close is not None else fallback_yesterday_close
        if y_close is None:
            raise HistoricalDataMissingError(
                f"轉換 {self.symbol} ({self.trade_date}) 為 MarketBar 時缺少 yesterday_close",
                symbol=self.symbol,
                trade_date=self.trade_date,
                missing_fields=("yesterday_close",),
            )
        return MarketBar(
            symbol=self.symbol,
            timestamp=self.event_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            yesterday_close=y_close,
            is_halted=self.is_halted,
            is_disposition=self.is_disposition,
            is_limit_up_locked=self.is_limit_up_locked,
            is_limit_down_locked=self.is_limit_down_locked,
        )


@runtime_checkable
class HistoricalDataProvider(Protocol):
    """台股歷史回測資料供應器介面協定。"""

    @property
    def calendar(self) -> TradingCalendar:
        ...

    @property
    def provenance(self) -> DataProvenance:
        ...

    @property
    def is_synthetic(self) -> bool:
        ...

    def get_bar(
        self,
        symbol: str,
        trade_date: dt.date,
        *,
        cutoff_time: dt.datetime,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> HistoricalBarRecord:
        """取得指定股票、指定交易日截至 `cutoff_time` 可用之歷史行情。"""
        ...

    def get_available_bars(
        self,
        symbol: str,
        *,
        cutoff_time: dt.datetime,
        start_date: Optional[dt.date] = None,
        end_date: Optional[dt.date] = None,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> Tuple[HistoricalBarRecord, ...]:
        """取得指定截止時間 `cutoff_time` 前可使用之歷史行情序列（按交易日升冪排列）。"""
        ...

    def get_latest_available_bar(
        self,
        symbol: str,
        *,
        cutoff_time: dt.datetime,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> HistoricalBarRecord:
        """取得指定股票於 `cutoff_time` 前最新一筆可用之歷史行情。"""
        ...


class InMemoryHistoricalDataProvider:
    """基於純標準庫記憶體儲存之 `HistoricalDataProvider` 最小可測與防前視實作。

    內建防護機制：
    1. 強制綁定 `TradingCalendar` 與 `DataProvenance`。
    2. 寫入時強制檢查每筆行情的 `trade_date` 必須為日曆確認之有效交易日。
    3. 寫入時強制檢查同一股票之時間序列必須嚴格按 `trade_date` 與 `event_time` 遞增排序（拒絕亂序與重複日期）。
    4. 嚴格隔離合成測試資料 (`is_synthetic=True`) 與真實歷史資料 (`is_synthetic=False`)，禁止混裝或冒充。
    5. 查詢時強制執行 `cutoff_time` 與 `available_at` 比對，攔截任何未來資料與未驗證 PIT 資料。
    """

    def __init__(
        self,
        calendar: TradingCalendar,
        provenance: DataProvenance,
        bars: Optional[Iterable[HistoricalBarRecord]] = None,
    ) -> None:
        if not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須是 TradingCalendar 實例，收到 {type(calendar).__name__}")
        calendar.assert_ready(allow_synthetic=True)

        if not isinstance(provenance, DataProvenance):
            raise DataProvenanceError(f"provenance 必須是 DataProvenance 實例，收到 {type(provenance).__name__}")

        if calendar.is_synthetic != provenance.is_synthetic:
            raise DataProvenanceError(
                f"交易日曆 (is_synthetic={calendar.is_synthetic}) 與資料供應器 "
                f"(is_synthetic={provenance.is_synthetic}) 之合成／真實標記不一致，嚴禁混用",
                source=provenance.source,
                dataset_version=provenance.dataset_version,
                is_synthetic=provenance.is_synthetic,
            )

        self._calendar = calendar
        self._provenance = provenance
        self._bars_by_symbol: Dict[str, List[HistoricalBarRecord]] = {}
        self._bar_index: Dict[Tuple[str, dt.date], HistoricalBarRecord] = {}

        if bars is not None:
            self.add_bars(bars)

    @property
    def calendar(self) -> TradingCalendar:
        return self._calendar

    @property
    def provenance(self) -> DataProvenance:
        return self._provenance

    @property
    def is_synthetic(self) -> bool:
        return self._provenance.is_synthetic

    def add_bar(self, bar: HistoricalBarRecord) -> None:
        """新增單筆歷史行情紀錄，並驗證日曆有效性、來源一致性與時間嚴格遞增順序。"""
        if not isinstance(bar, HistoricalBarRecord):
            raise UnsupportedDataFormatError(
                f"bar 必須是 HistoricalBarRecord 實例，收到 {type(bar).__name__}",
                format_type=type(bar).__name__,
            )

        # 1. 合成／真實標記一致性檢查
        if bar.provenance.is_synthetic != self._provenance.is_synthetic:
            raise DataProvenanceError(
                f"標的 {bar.symbol} ({bar.trade_date}) 之 is_synthetic={bar.provenance.is_synthetic} "
                f"與 Provider 之 is_synthetic={self._provenance.is_synthetic} 衝突，嚴禁混裝合成與真實資料",
                source=bar.provenance.source,
                dataset_version=bar.provenance.dataset_version,
                is_synthetic=bar.provenance.is_synthetic,
            )

        # 2. 交易日曆有效性檢查 (非交易日或未知日期均拒絕寫入交易 K 棒)
        if not self._calendar.is_trading_day(bar.trade_date):
            raise BacktestCalendarError(
                f"標的 {bar.symbol} 之行情日期 {bar.trade_date} 在交易日曆中屬於非交易日 (NON_TRADING_DAY)，拒絕載入",
                context={"symbol": bar.symbol, "trade_date": bar.trade_date.isoformat()},
            )

        # 3. 檢查時間排序與重複日期 (嚴格要求按時間升冪寫入，禁止亂序或覆蓋)
        sym_list = self._bars_by_symbol.setdefault(bar.symbol, [])
        if sym_list:
            last_bar = sym_list[-1]
            if bar.trade_date == last_bar.trade_date:
                raise InvalidOHLCVError(
                    f"標的 {bar.symbol} 出現重複交易日行情: {bar.trade_date}",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    field_name="trade_date",
                )
            if bar.trade_date < last_bar.trade_date or bar.event_time <= last_bar.event_time:
                raise InvalidOHLCVError(
                    f"標的 {bar.symbol} 行情時間序列排序錯誤: 新紀錄 ({bar.trade_date}, {bar.event_time}) "
                    f"未嚴格晚於前一筆紀錄 ({last_bar.trade_date}, {last_bar.event_time})",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    field_name="trade_date/event_time",
                )

        sym_list.append(bar)
        self._bar_index[(bar.symbol, bar.trade_date)] = bar

    def add_bars(self, bars: Iterable[HistoricalBarRecord]) -> None:
        """批次新增歷史行情紀錄。"""
        for bar in bars:
            self.add_bar(bar)

    def _check_real_data_requirement(self, *, require_real_data: bool) -> None:
        if require_real_data and self.is_synthetic:
            raise DataProvenanceError(
                "正式歷史回測要求真實歷史資料 (require_real_data=True)，嚴禁將合成測試資料當作真實市場資料使用",
                source=self._provenance.source,
                dataset_version=self._provenance.dataset_version,
                is_synthetic=True,
            )

    def get_bar(
        self,
        symbol: str,
        trade_date: dt.date,
        *,
        cutoff_time: dt.datetime,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> HistoricalBarRecord:
        """取得指定股票、指定交易日截至 `cutoff_time` 可用之歷史行情。"""
        clean_sym = _normalize_symbol(symbol)
        t_date = to_taipei_date(trade_date, "trade_date", allow_datetime=False)
        cutoff_taipei = ensure_taipei_datetime(cutoff_time, "cutoff_time")

        self._check_real_data_requirement(require_real_data=require_real_data)

        # 先檢查目標日期是否晚於截止時間所在日期 (未來日期攔截)
        if t_date > cutoff_taipei.date():
            raise LookaheadBiasError(
                f"未來資料洩漏: 在截止時間 {cutoff_taipei} 嘗試查詢未來日期 {t_date} 之行情 ({clean_sym})",
                symbol=clean_sym,
                trade_date=t_date,
                cutoff_time=cutoff_taipei,
            )

        # 驗證該日期在交易日曆中的有效性
        if not self._calendar.is_trading_day(t_date):
            raise BacktestCalendarError(
                f"查詢日期 {t_date} 為非交易日 (NON_TRADING_DAY)，無交易行情",
                context={"symbol": clean_sym, "trade_date": t_date.isoformat()},
            )

        bar = self._bar_index.get((clean_sym, t_date))
        if bar is None:
            raise HistoricalDataMissingError(
                f"找不到標的 {clean_sym} 於交易日 {t_date} 之歷史行情資料",
                symbol=clean_sym,
                trade_date=t_date,
            )

        bar.assert_available_as_of(cutoff_taipei, require_pit_verified=require_pit_verified)
        return bar

    def get_available_bars(
        self,
        symbol: str,
        *,
        cutoff_time: dt.datetime,
        start_date: Optional[dt.date] = None,
        end_date: Optional[dt.date] = None,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> Tuple[HistoricalBarRecord, ...]:
        """取得指定股票於 `cutoff_time` 前已可用之歷史行情列表。

        - 若指定 `end_date` 且 `end_date > cutoff_time.date()`，立即拋出 `LookaheadBiasError`。
        - 若區間內存在資料缺少 `published_at` / `available_at` 且 `require_pit_verified=True`，
          立即拋出 `PITViolationError`（禁止無條件放行或靜默略過未驗證資料）。
        """
        clean_sym = _normalize_symbol(symbol)
        cutoff_taipei = ensure_taipei_datetime(cutoff_time, "cutoff_time")
        self._check_real_data_requirement(require_real_data=require_real_data)

        s_date = to_taipei_date(start_date, "start_date") if start_date is not None else None
        e_date = to_taipei_date(end_date, "end_date") if end_date is not None else None

        if s_date is not None and e_date is not None and s_date > e_date:
            raise InvalidOHLCVError(
                f"查詢區間 start_date ({s_date}) 不得晚於 end_date ({e_date})",
                symbol=clean_sym,
                field_name="start_date/end_date",
            )

        if e_date is not None and e_date > cutoff_taipei.date():
            raise LookaheadBiasError(
                f"未來資料洩漏: 請求區間終點 end_date ({e_date}) 晚於策略截止日期 ({cutoff_taipei.date()})",
                symbol=clean_sym,
                trade_date=e_date,
                cutoff_time=cutoff_taipei,
            )

        records = self._bars_by_symbol.get(clean_sym, [])
        if not records:
            raise HistoricalDataMissingError(
                f"資料供應器中不存在標的 {clean_sym} 之任何歷史行情資料",
                symbol=clean_sym,
            )

        available_list: List[HistoricalBarRecord] = []
        for bar in records:
            if s_date is not None and bar.trade_date < s_date:
                continue
            if e_date is not None and bar.trade_date > e_date:
                continue

            # 若未顯式指定 end_date，則自動過濾發生在 cutoff_time 之後的未來紀錄
            if e_date is None and bar.trade_date > cutoff_taipei.date():
                continue

            if require_pit_verified:
                bar.assert_pit_verified()

            # 當呼叫端明確指定 end_date == bar.trade_date，但該日行情在 cutoff_time 尚未可用時，拋出 LookaheadBiasError
            if e_date is not None and bar.trade_date == e_date:
                bar.assert_available_as_of(cutoff_taipei, require_pit_verified=require_pit_verified)
                available_list.append(bar)
                continue

            # 一般截至 cutoff_time 篩選：僅納入 event_time <= cutoff_time 且 available_at <= cutoff_time 之紀錄
            if bar.event_time <= cutoff_taipei and (
                bar.available_at is not None and bar.available_at <= cutoff_taipei
                or (not require_pit_verified and bar.available_at is None)
            ):
                available_list.append(bar)

        return tuple(available_list)

    def get_latest_available_bar(
        self,
        symbol: str,
        *,
        cutoff_time: dt.datetime,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> HistoricalBarRecord:
        """取得截至 `cutoff_time` 為止最新一筆可用之歷史行情。"""
        bars = self.get_available_bars(
            symbol,
            cutoff_time=cutoff_time,
            require_pit_verified=require_pit_verified,
            require_real_data=require_real_data,
        )
        if not bars:
            clean_sym = _normalize_symbol(symbol)
            cutoff_taipei = ensure_taipei_datetime(cutoff_time, "cutoff_time")
            raise HistoricalDataMissingError(
                f"截至 {cutoff_taipei} 為止，標的 {clean_sym} 無任何已達可用時間之歷史行情",
                symbol=clean_sym,
                context={"cutoff_time": cutoff_taipei.isoformat()},
            )
        return bars[-1]

    def get_cross_section_bars(
        self,
        symbols: Sequence[str],
        trade_date: dt.date,
        *,
        cutoff_time: dt.datetime,
        require_pit_verified: bool = True,
        require_real_data: bool = False,
    ) -> Dict[str, HistoricalBarRecord]:
        """取得多檔股票於指定交易日 `trade_date` 截至 `cutoff_time` 可用之橫截面行情字典。"""
        if not symbols:
            raise HistoricalDataMissingError("symbols 清單不得為空", missing_fields=("symbols",))
        result: Dict[str, HistoricalBarRecord] = {}
        for sym in symbols:
            bar = self.get_bar(
                sym,
                trade_date,
                cutoff_time=cutoff_time,
                require_pit_verified=require_pit_verified,
                require_real_data=require_real_data,
            )
            result[bar.symbol] = bar
        return result

    def validate_execution_timing_for_bar(
        self,
        bar: HistoricalBarRecord,
        proposed_execution_time: dt.datetime,
    ) -> None:
        """驗證基於 `bar` 產生的交易決策是否試圖在 `bar.trade_date` 當日收盤或盤中成交。

        對於盤後才取得的 T 日收盤資訊，強制阻止策略在 T 日收盤成交。
        """
        if not isinstance(bar, HistoricalBarRecord):
            raise UnsupportedDataFormatError(f"bar 必須是 HistoricalBarRecord，收到 {type(bar).__name__}")
        bar.assert_not_same_day_execution(proposed_execution_time)
