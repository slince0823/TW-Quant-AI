"""台股歷史回測交易日曆模組 (calendar.py).

核心原則：
1. 明確使用台灣時區 `Asia/Taipei` (UTC+08:00)，嚴格拒絕無時區 (naive) 之時間戳記。
2. 明確區分「交易日 (TRADING_DAY)」、「非交易日 (NON_TRADING_DAY)」與「未知日期 (UNKNOWN)」。
3. 嚴禁將未知日期默認為有效交易日或休市日；查詢或跨越未知日期一律拋出 UnknownTradingDateError。
4. 嚴禁以 0050.TW K 棒或單一標的歷史價格日期宣稱已取得完整台股官方交易日曆。
5. 嚴禁自行捏造歷史休市日或外推未來交易日；無真實交易日曆資料時明確回報 CalendarNotReadyError。
6. 測試用合成日曆必須顯式標記 `is_synthetic=True`，不得與真實交易日曆混淆。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Iterable, Optional, Tuple
import zoneinfo

from .exceptions import (
    BacktestCalendarError,
    CalendarNotReadyError,
    DataProvenanceError,
    UnknownTradingDateError,
)

TAIPEI_TZ_NAME: str = "Asia/Taipei"
_TAIPEI_OFFSET = dt.timedelta(hours=8)

# 禁止宣稱為正式官方交易日曆之代理資料關鍵字 (如 0050.TW K 棒反推、Yahoo Finance 價格代理或合成測試標記)
_FORBIDDEN_OFFICIAL_CALENDAR_TOKENS: Tuple[str, ...] = (
    "0050",
    "YFINANCE",
    "KBAR",
    "K_BAR",
    "PRICE_PROXY",
    "ETF_PROXY",
    "SYNTHETIC",
    "MOCK",
    "FAKE",
    "DUMMY",
    "TEST",
    "REG_CN",
)


def _resolve_taipei_tz() -> dt.tzinfo:
    """取得台灣標準時間 `Asia/Taipei` (UTC+08:00) 時區物件。

    優先載入標準庫 `zoneinfo.ZoneInfo("Asia/Taipei")`；
    若執行環境（如無 tzdata 套件之純標準庫 Windows 環境）無法載入 IANA 資料庫，
    則回退使用具名固定偏移時區 `dt.timezone(dt.timedelta(hours=8), name="Asia/Taipei")`。
    （台灣自 1980 年起無日光節約時間，全年固定為 UTC+08:00）。
    """
    try:
        return zoneinfo.ZoneInfo(TAIPEI_TZ_NAME)
    except Exception:
        return dt.timezone(_TAIPEI_OFFSET, name=TAIPEI_TZ_NAME)


TAIPEI_TZ: dt.tzinfo = _resolve_taipei_tz()


class TradingDayStatus(str, Enum):
    """日期於交易日曆中之狀態分類。"""

    TRADING_DAY = "TRADING_DAY"          # 已確認之有效開市交易日
    NON_TRADING_DAY = "NON_TRADING_DAY"  # 已確認之休市日（週末或國定假日/颱風休市）
    UNKNOWN = "UNKNOWN"                  # 超出涵蓋範圍、缺漏未驗證或日曆尚未就緒


def ensure_taipei_datetime(value: object, field_name: str = "timestamp") -> dt.datetime:
    """驗證輸入為 timezone-aware datetime 並轉換至台灣時區 `Asia/Taipei` (UTC+08:00)。"""
    if not isinstance(value, dt.datetime):
        raise BacktestCalendarError(
            f"{field_name} 必須是 datetime.datetime 物件，收到 {type(value).__name__}",
            context={"field_name": field_name, "received_type": type(value).__name__},
        )
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise BacktestCalendarError(
            f"{field_name} 必須具備明確時區 (timezone-aware)，禁止使用無時區 (naive) datetime",
            context={"field_name": field_name, "value": value.isoformat()},
        )
    return value.astimezone(TAIPEI_TZ)


def to_taipei_date(
    value: object,
    field_name: str = "day",
    *,
    allow_datetime: bool = False,
) -> dt.date:
    """驗證並取得台灣時區 (`Asia/Taipei`) 下之日期 `datetime.date`。

    預設僅接受純 `datetime.date`（拒絕 `datetime.datetime` 與 `bool`），以相容策略層協定；
    當 `allow_datetime=True` 時，可將 timezone-aware `datetime` 先轉換至 `Asia/Taipei` 再擷取其當地日期。
    """
    if isinstance(value, bool):
        raise BacktestCalendarError(
            f"{field_name} 不得為布林值 (bool)",
            context={"field_name": field_name, "value": value},
        )
    if isinstance(value, dt.datetime):
        if not allow_datetime:
            raise BacktestCalendarError(
                f"{field_name} 必須是純 datetime.date (不得為 datetime)，收到 {type(value).__name__}",
                context={"field_name": field_name, "received_type": type(value).__name__},
            )
        taipei_dt = ensure_taipei_datetime(value, field_name=field_name)
        return taipei_dt.date()
    if not isinstance(value, dt.date):
        raise BacktestCalendarError(
            f"{field_name} 必須是 datetime.date，收到 {type(value).__name__}",
            context={"field_name": field_name, "received_type": type(value).__name__},
        )
    return value


@dataclass(frozen=True)
class CalendarMetadata:
    """交易日曆資料來源與版本元資料。"""

    source: str
    version: str
    is_synthetic: bool
    retrieved_at: Optional[dt.datetime] = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise DataProvenanceError(
                "交易日曆 source (資料來源) 不得為空",
                source=self.source,
                dataset_version=self.version,
                is_synthetic=self.is_synthetic,
            )
        if not isinstance(self.version, str) or not self.version.strip():
            raise DataProvenanceError(
                "交易日曆 version (資料版本) 不得為空",
                source=self.source,
                dataset_version=self.version,
                is_synthetic=self.is_synthetic,
            )
        if not isinstance(self.is_synthetic, bool):
            raise DataProvenanceError(
                "is_synthetic 必須為明確的布林值 (bool)",
                source=self.source,
                dataset_version=self.version,
            )
        if self.retrieved_at is not None:
            converted = ensure_taipei_datetime(self.retrieved_at, field_name="retrieved_at")
            object.__setattr__(self, "retrieved_at", converted)

        upper_combined = f"{self.source.upper()}::{self.version.upper()}::{self.description.upper()}"
        if not self.is_synthetic:
            for token in _FORBIDDEN_OFFICIAL_CALENDAR_TOKENS:
                if token in upper_combined:
                    raise DataProvenanceError(
                        f"正式台股交易日曆 (is_synthetic=False) 嚴禁使用代理或測試資料來源 "
                        f"(偵測到禁用標記 {token!r})；不得以 0050.TW K 棒或合成日曆宣稱為完整官方日曆",
                        source=self.source,
                        dataset_version=self.version,
                        is_synthetic=self.is_synthetic,
                        context={"forbidden_token": token},
                    )


class TradingCalendar:
    """台股歷史回測交易日曆。

    相容於 `src.tw_quant.strategy.trading_calendar.TradingCalendar` Protocol，
    並強化以下安全保證：
    - 區分 `TRADING_DAY`、`NON_TRADING_DAY` 與 `UNKNOWN`。
    - 嚴格檢查涵蓋範圍 `[coverage_start, coverage_end]` 與內部缺漏日期 (`unknown_dates`)。
    - 支援嚴格排程模式 (`strict_explicit_schedule=True`)：在涵蓋範圍內，若某日期既不在
      `trading_days` 亦不在 `non_trading_days`，則視為 `UNKNOWN`（未知缺漏日），查詢時立即報錯。
    - 明確標記 `is_synthetic` 與 `is_ready`，防止無資料或合成資料被誤當作真實市場日曆。
    """

    def __init__(
        self,
        trading_days: Optional[Iterable[dt.date]] = None,
        *,
        coverage_start: Optional[dt.date] = None,
        coverage_end: Optional[dt.date] = None,
        non_trading_days: Optional[Iterable[dt.date]] = None,
        unknown_dates: Optional[Iterable[dt.date]] = None,
        metadata: Optional[CalendarMetadata] = None,
        is_ready: bool = True,
        strict_explicit_schedule: bool = False,
    ) -> None:
        self.timezone: dt.tzinfo = TAIPEI_TZ
        self.timezone_name: str = TAIPEI_TZ_NAME
        self._is_ready: bool = bool(is_ready)

        if not self._is_ready:
            self._trading_days: FrozenSet[dt.date] = frozenset()
            self._non_trading_days: FrozenSet[dt.date] = frozenset()
            self._unknown_dates: FrozenSet[dt.date] = frozenset()
            self._sorted_trading_days: Tuple[dt.date, ...] = ()
            self.coverage_start: Optional[dt.date] = None
            self.coverage_end: Optional[dt.date] = None
            self.metadata: Optional[CalendarMetadata] = metadata
            self.strict_explicit_schedule: bool = strict_explicit_schedule
            return

        if metadata is None:
            raise DataProvenanceError(
                "建立已就緒之 TradingCalendar 必須提供 CalendarMetadata (含 source、version 與 is_synthetic 標記)"
            )
        if not isinstance(metadata, CalendarMetadata):
            raise DataProvenanceError(f"metadata 必須是 CalendarMetadata 實例，收到 {type(metadata).__name__}")
        self.metadata = metadata

        if coverage_start is None or coverage_end is None:
            raise BacktestCalendarError("必須明確提供 coverage_start 與 coverage_end，不得省略日曆涵蓋範圍")

        cov_start = to_taipei_date(coverage_start, "coverage_start")
        cov_end = to_taipei_date(coverage_end, "coverage_end")
        if cov_start > cov_end:
            raise BacktestCalendarError(
                f"coverage_start ({cov_start}) 不得晚於 coverage_end ({cov_end})",
                context={"coverage_start": cov_start.isoformat(), "coverage_end": cov_end.isoformat()},
            )

        if trading_days is None:
            raise CalendarNotReadyError("trading_days 不得為 None；若尚無真實交易日曆資料，請使用 TradingCalendar.uninitialized()")

        parsed_trading = frozenset(to_taipei_date(d, "trading_days 元素") for d in trading_days)
        if not parsed_trading:
            raise CalendarNotReadyError("trading_days 不得為空集合")

        for d in parsed_trading:
            if not (cov_start <= d <= cov_end):
                raise BacktestCalendarError(
                    f"交易日 {d} 超出宣告的涵蓋範圍 [{cov_start}, {cov_end}]",
                    context={"invalid_date": d.isoformat(), "coverage_start": cov_start.isoformat(), "coverage_end": cov_end.isoformat()},
                )

        parsed_non_trading: FrozenSet[dt.date] = (
            frozenset(to_taipei_date(d, "non_trading_days 元素") for d in non_trading_days)
            if non_trading_days is not None
            else frozenset()
        )
        for d in parsed_non_trading:
            if not (cov_start <= d <= cov_end):
                raise BacktestCalendarError(
                    f"非交易日 {d} 超出宣告的涵蓋範圍 [{cov_start}, {cov_end}]",
                    context={"invalid_date": d.isoformat()},
                )

        parsed_unknown: FrozenSet[dt.date] = (
            frozenset(to_taipei_date(d, "unknown_dates 元素") for d in unknown_dates)
            if unknown_dates is not None
            else frozenset()
        )
        for d in parsed_unknown:
            if not (cov_start <= d <= cov_end):
                raise BacktestCalendarError(
                    f"unknown_dates 日期 {d} 超出宣告的涵蓋範圍 [{cov_start}, {cov_end}]",
                    context={"invalid_date": d.isoformat()},
                )

        overlap_tn = parsed_trading & parsed_non_trading
        if overlap_tn:
            sample = sorted(overlap_tn)[0]
            raise BacktestCalendarError(
                f"日期 {sample} 同時出現在 trading_days 與 non_trading_days 中，日曆定義矛盾",
                context={"conflicting_date": sample.isoformat()},
            )

        overlap_u = (parsed_trading | parsed_non_trading) & parsed_unknown
        if overlap_u:
            sample = sorted(overlap_u)[0]
            raise BacktestCalendarError(
                f"日期 {sample} 同時標記為已知日期與 unknown_dates，日曆定義矛盾",
                context={"conflicting_date": sample.isoformat()},
            )

        self.coverage_start = cov_start
        self.coverage_end = cov_end
        self._trading_days = parsed_trading
        self._non_trading_days = parsed_non_trading
        self._unknown_dates = parsed_unknown
        self._sorted_trading_days = tuple(sorted(parsed_trading))
        self.strict_explicit_schedule = bool(strict_explicit_schedule)

    # ── 工廠方法 ───────────────────────────────────────────────

    @classmethod
    def uninitialized(cls, reason: str = "尚未載入經驗證之 TWSE 官方交易日曆資料") -> "TradingCalendar":
        """建立一個明確標示「尚未就緒」之交易日曆實例。"""
        cal = cls(is_ready=False)
        cal._not_ready_reason = reason
        return cal

    @classmethod
    def create_synthetic_for_test(
        cls,
        trading_days: Iterable[dt.date],
        coverage_start: dt.date,
        coverage_end: dt.date,
        *,
        non_trading_days: Optional[Iterable[dt.date]] = None,
        unknown_dates: Optional[Iterable[dt.date]] = None,
        strict_explicit_schedule: bool = False,
        version: str = "synth-cal-v1",
        description: str = "Synthetic calendar for unit testing only (NOT real TWSE calendar)",
    ) -> "TradingCalendar":
        """建立專供單元與整合測試使用之合成交易日曆（強制標記 `is_synthetic=True`）。"""
        meta = CalendarMetadata(
            source="SYNTHETIC_TEST_FIXTURE",
            version=version,
            is_synthetic=True,
            retrieved_at=dt.datetime.now(TAIPEI_TZ),
            description=description,
        )
        return cls(
            trading_days=trading_days,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            non_trading_days=non_trading_days,
            unknown_dates=unknown_dates,
            metadata=meta,
            is_ready=True,
            strict_explicit_schedule=strict_explicit_schedule,
        )

    # ── 狀態與屬性查詢 ─────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """日曆是否已載入資料。"""
        return self._is_ready

    @property
    def is_synthetic(self) -> bool:
        """是否為測試用合成日曆。若日曆未就緒則回傳 False。"""
        if self.metadata is None:
            return False
        return self.metadata.is_synthetic

    def assert_ready(self, *, allow_synthetic: bool = True) -> None:
        """確認日曆已就緒；若 `allow_synthetic=False`（正式歷史回測模式），則同時拒絕合成日曆。"""
        if not self._is_ready or self.coverage_start is None or self.coverage_end is None or self.metadata is None:
            reason = getattr(self, "_not_ready_reason", "交易日曆資料未就緒")
            raise CalendarNotReadyError(reason)
        if not allow_synthetic and self.metadata.is_synthetic:
            raise DataProvenanceError(
                "目前交易日曆為合成測試資料 (is_synthetic=True)，不得用於要求真實日曆之正式回測",
                source=self.metadata.source,
                dataset_version=self.metadata.version,
                is_synthetic=True,
            )

    def get_day_status(self, day: dt.date | dt.datetime, *, allow_datetime: bool = False) -> TradingDayStatus:
        """查詢指定日期之狀態（`TRADING_DAY`、`NON_TRADING_DAY` 或 `UNKNOWN`）。

        注意：即使日期超出涵蓋範圍或日曆未就緒，此方法亦回傳 `TradingDayStatus.UNKNOWN` 供呼叫端檢查；
        而 `is_trading_day()` 遇到 `UNKNOWN` 時則會強制拋出例外，絕不默認為有效或無效交易日。
        """
        target = to_taipei_date(day, "day", allow_datetime=allow_datetime)
        if not self._is_ready or self.coverage_start is None or self.coverage_end is None:
            return TradingDayStatus.UNKNOWN
        if not (self.coverage_start <= target <= self.coverage_end):
            return TradingDayStatus.UNKNOWN
        if target in self._unknown_dates:
            return TradingDayStatus.UNKNOWN
        if target in self._trading_days:
            return TradingDayStatus.TRADING_DAY
        if self.strict_explicit_schedule:
            if target in self._non_trading_days:
                return TradingDayStatus.NON_TRADING_DAY
            return TradingDayStatus.UNKNOWN
        return TradingDayStatus.NON_TRADING_DAY

    def _require_known_status(
        self,
        day: dt.date | dt.datetime,
        field_name: str = "day",
        *,
        allow_datetime: bool = False,
    ) -> Tuple[dt.date, TradingDayStatus]:
        """驗證日期位於已知範圍且非 UNKNOWN，否則拋出對應之 CalendarNotReadyError 或 UnknownTradingDateError。"""
        target = to_taipei_date(day, field_name, allow_datetime=allow_datetime)
        self.assert_ready(allow_synthetic=True)
        assert self.coverage_start is not None and self.coverage_end is not None

        if not (self.coverage_start <= target <= self.coverage_end):
            raise UnknownTradingDateError(
                f"{field_name}={target} 超出交易日曆涵蓋範圍 [{self.coverage_start}, {self.coverage_end}]，拒絕猜測是否為交易日",
                target_date=target,
                coverage_start=self.coverage_start,
                coverage_end=self.coverage_end,
            )

        status = self.get_day_status(target)
        if status == TradingDayStatus.UNKNOWN:
            raise UnknownTradingDateError(
                f"{field_name}={target} 為未驗證或缺漏之未知日期 (UNKNOWN)，嚴禁默認為有效交易日或休市日",
                target_date=target,
                coverage_start=self.coverage_start,
                coverage_end=self.coverage_end,
            )
        return target, status

    # ── 核心交易日運算介面 ─────────────────────────────────────

    def is_trading_day(self, day: dt.date | dt.datetime, *, allow_datetime: bool = False) -> bool:
        """判斷 `day` 是否為有效交易日。

        若日曆未就緒、日期超出涵蓋範圍或該日屬於未知缺漏日 (`UNKNOWN`)，
        一律拋出 `CalendarNotReadyError` 或 `UnknownTradingDateError`，絕不默認回傳 True/False。
        """
        _, status = self._require_known_status(day, "day", allow_datetime=allow_datetime)
        return status == TradingDayStatus.TRADING_DAY

    def previous_trading_day(self, day: dt.date | dt.datetime, *, allow_datetime: bool = False) -> dt.date:
        """回傳嚴格早於 `day` 的最近一個交易日。

        若向後搜尋過程中遇到 `UNKNOWN` 缺漏日期，或直到 `coverage_start` 仍找不到前一交易日，
        一律拋出 `UnknownTradingDateError`。
        """
        target, _ = self._require_known_status(day, "day", allow_datetime=allow_datetime)
        assert self.coverage_start is not None and self.coverage_end is not None

        cursor = target - dt.timedelta(days=1)
        while cursor >= self.coverage_start:
            status = self.get_day_status(cursor)
            if status == TradingDayStatus.UNKNOWN:
                raise UnknownTradingDateError(
                    f"由 {target} 向前搜尋前一交易日時，於 {cursor} 遇到未驗證之缺漏日期 (UNKNOWN)，拒絕跨越未知區間",
                    target_date=cursor,
                    coverage_start=self.coverage_start,
                    coverage_end=self.coverage_end,
                )
            if status == TradingDayStatus.TRADING_DAY:
                return cursor
            cursor -= dt.timedelta(days=1)

        raise UnknownTradingDateError(
            f"{target} 之前在日曆涵蓋範圍內找不到前一個交易日 (涵蓋起點為 {self.coverage_start})",
            target_date=target,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
        )

    # 別名，方便呼叫端使用
    prev_trading_day = previous_trading_day

    def next_trading_day(self, day: dt.date | dt.datetime, *, allow_datetime: bool = False) -> dt.date:
        """回傳嚴格晚於 `day` 的第一個交易日。

        若向前搜尋過程中遇到 `UNKNOWN` 缺漏日期，或直到 `coverage_end` 仍找不到下一交易日，
        一律拋出 `UnknownTradingDateError`（絕不自行外推未來交易日）。
        """
        target, _ = self._require_known_status(day, "day", allow_datetime=allow_datetime)
        assert self.coverage_start is not None and self.coverage_end is not None

        cursor = target + dt.timedelta(days=1)
        while cursor <= self.coverage_end:
            status = self.get_day_status(cursor)
            if status == TradingDayStatus.UNKNOWN:
                raise UnknownTradingDateError(
                    f"由 {target} 向後搜尋下一交易日時，於 {cursor} 遇到未驗證之缺漏日期 (UNKNOWN)，拒絕跨越未知區間",
                    target_date=cursor,
                    coverage_start=self.coverage_start,
                    coverage_end=self.coverage_end,
                )
            if status == TradingDayStatus.TRADING_DAY:
                return cursor
            cursor += dt.timedelta(days=1)

        raise UnknownTradingDateError(
            f"{target} 之後在日曆涵蓋範圍內找不到下一個交易日 (涵蓋終點為 {self.coverage_end})，嚴禁外推未來交易日",
            target_date=target,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
        )

    def trading_days_between(self, start: dt.date, end: dt.date) -> int:
        """計算區間 `(start, end]` 內的交易日數（不含 `start`，含 `end`）。

        與 `src.tw_quant.strategy.trading_calendar.TradingCalendar` 協定完全相容。
        若區間內包含任何 `UNKNOWN` 缺漏日期，將拋出 `UnknownTradingDateError`。
        """
        s_date, _ = self._require_known_status(start, "start")
        e_date, _ = self._require_known_status(end, "end")
        if s_date > e_date:
            raise BacktestCalendarError(
                f"start ({s_date}) 不得晚於 end ({e_date})",
                context={"start": s_date.isoformat(), "end": e_date.isoformat()},
            )
        if s_date == e_date:
            return 0

        count = 0
        cursor = s_date + dt.timedelta(days=1)
        while cursor <= e_date:
            status = self.get_day_status(cursor)
            if status == TradingDayStatus.UNKNOWN:
                raise UnknownTradingDateError(
                    f"計算 ({s_date}, {e_date}] 交易日數時，於 {cursor} 發現未驗證之缺漏日期 (UNKNOWN)",
                    target_date=cursor,
                    coverage_start=self.coverage_start,
                    coverage_end=self.coverage_end,
                )
            if status == TradingDayStatus.TRADING_DAY:
                count += 1
            cursor += dt.timedelta(days=1)
        return count

    def get_trading_days_in_range(
        self,
        start: dt.date,
        end: dt.date,
    ) -> Tuple[dt.date, ...]:
        """回傳閉區間 `[start, end]` 內按時間升冪排列之所有有效交易日。

        若區間內存在任何 `UNKNOWN` 缺漏日期，將拋出 `UnknownTradingDateError`。
        """
        s_date, _ = self._require_known_status(start, "start")
        e_date, _ = self._require_known_status(end, "end")
        if s_date > e_date:
            raise BacktestCalendarError(
                f"start ({s_date}) 不得晚於 end ({e_date})",
                context={"start": s_date.isoformat(), "end": e_date.isoformat()},
            )

        result = []
        cursor = s_date
        while cursor <= e_date:
            status = self.get_day_status(cursor)
            if status == TradingDayStatus.UNKNOWN:
                raise UnknownTradingDateError(
                    f"列舉 [{s_date}, {e_date}] 交易日時，於 {cursor} 發現未驗證之缺漏日期 (UNKNOWN)",
                    target_date=cursor,
                    coverage_start=self.coverage_start,
                    coverage_end=self.coverage_end,
                )
            if status == TradingDayStatus.TRADING_DAY:
                result.append(cursor)
            cursor += dt.timedelta(days=1)
        return tuple(result)

    def shift_trading_days(self, day: dt.date, offset: int) -> dt.date:
        """從 `day` 位移 `offset` 個交易日。

        - 當 `offset == 0` 時：`day` 本身必須是有效交易日，否則拋出 `BacktestCalendarError`。
        - 當 `offset > 0` 時：向後推進 `offset` 個交易日。
        - 當 `offset < 0` 時：向前倒退 `abs(offset)` 個交易日。
        """
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise BacktestCalendarError(f"offset 必須為整數 (int)，收到 {offset!r}")

        current, status = self._require_known_status(day, "day")
        if offset == 0:
            if status != TradingDayStatus.TRADING_DAY:
                raise BacktestCalendarError(
                    f"offset=0 時，起算日 {current} 必須本身為有效交易日",
                    context={"day": current.isoformat(), "status": status.value},
                )
            return current

        steps = abs(offset)
        for _ in range(steps):
            if offset > 0:
                current = self.next_trading_day(current)
            else:
                current = self.previous_trading_day(current)
        return current
