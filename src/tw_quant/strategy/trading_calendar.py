"""可注入的交易日曆介面 (trading_calendar.py).

核心原則：
1. 策略不內建任何市場日曆；日曆必須由呼叫端注入。
2. 嚴禁以「日曆日 + 1」推算進場日，也不假設中國 (REG_CN) 日曆等於台股日曆。
3. 日曆必須宣告涵蓋範圍；查詢範圍以外的日期一律拋出 CalendarError，不得猜測。

注意：本專案尚未提供經驗證的 TWSE 官方日曆。StaticTradingCalendar 只是「由呼叫端明確列出交易日」
的容器，測試所用日期皆為合成資料，不代表真實台股休市日。
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable, Protocol, runtime_checkable


class CalendarError(Exception):
    """日曆不可用、查詢超出涵蓋範圍或輸入不合法。"""


@runtime_checkable
class TradingCalendar(Protocol):
    """交易日曆協定。"""

    def is_trading_day(self, day: dt.date) -> bool:
        """day 是否為交易日。超出涵蓋範圍必須拋出 CalendarError。"""
        ...

    def next_trading_day(self, day: dt.date) -> dt.date:
        """嚴格晚於 day 的第一個交易日。找不到必須拋出 CalendarError。"""
        ...

    def trading_days_between(self, start: dt.date, end: dt.date) -> int:
        """區間 (start, end] 內的交易日數 (不含 start，含 end)。"""
        ...


def _require_date(value: object, name: str) -> dt.date:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise CalendarError(f"{name} 必須是 datetime.date (不得為 datetime)，收到 {type(value).__name__}")
    return value


class StaticTradingCalendar:
    """由呼叫端明確提供交易日集合與涵蓋範圍的日曆。"""

    def __init__(
        self,
        trading_days: Iterable[dt.date],
        coverage_start: dt.date,
        coverage_end: dt.date,
    ) -> None:
        _require_date(coverage_start, "coverage_start")
        _require_date(coverage_end, "coverage_end")
        if coverage_start > coverage_end:
            raise CalendarError("coverage_start 不得晚於 coverage_end")
        days = frozenset(_require_date(d, "trading_days 元素") for d in trading_days)
        if not days:
            raise CalendarError("trading_days 不得為空")
        for d in days:
            if not (coverage_start <= d <= coverage_end):
                raise CalendarError(f"交易日 {d} 超出宣告的涵蓋範圍 [{coverage_start}, {coverage_end}]")
        self._days = days
        self.coverage_start = coverage_start
        self.coverage_end = coverage_end

    def _require_covered(self, day: dt.date, name: str = "day") -> None:
        _require_date(day, name)
        if not (self.coverage_start <= day <= self.coverage_end):
            raise CalendarError(
                f"{name}={day} 超出日曆涵蓋範圍 [{self.coverage_start}, {self.coverage_end}]，拒絕猜測"
            )

    def is_trading_day(self, day: dt.date) -> bool:
        self._require_covered(day)
        return day in self._days

    def next_trading_day(self, day: dt.date) -> dt.date:
        self._require_covered(day)
        cursor = day + dt.timedelta(days=1)
        while cursor <= self.coverage_end:
            if cursor in self._days:
                return cursor
            cursor += dt.timedelta(days=1)
        raise CalendarError(f"{day} 之後在涵蓋範圍內找不到下一個交易日 (涵蓋至 {self.coverage_end})")

    def trading_days_between(self, start: dt.date, end: dt.date) -> int:
        self._require_covered(start, "start")
        self._require_covered(end, "end")
        if start > end:
            raise CalendarError(f"start ({start}) 不得晚於 end ({end})")
        return sum(1 for d in self._days if start < d <= end)
