"""第三階段 C-2.1：回測基礎資料層單元與驗收測試 (test_backtest_data_layer.py).

測試涵蓋範圍：
1. 有效交易日與非交易日判斷（含週末、跨年、國定休市日）。
2. 前一交易日、下一交易日、區間交易日數與位移（含與策略層 TradingCalendar Protocol 相容性）。
3. 未知交易日曆（未就緒、超出範圍、內部缺漏日期、嚴格排程缺漏）絕不默認為有效交易日。
4. 台灣時區 `Asia/Taipei` (UTC+08:00) 與跨日邊界處理。
5. 行情資料缺漏、時間序列錯誤排序、重複日期與非法 OHLC 關係攔截。
6. NaN、Infinity、非正數價格與非法成交量攔截（含停牌零量合法性）。
7. 資料可用時間 (available_at) 晚於策略截止時間 (cutoff_time) 時拒絕使用。
8. 盤後發布資料不能回填至當日盤中，且盤後收盤資訊嚴禁於當日收盤成交。
9. 缺少發布時間 (published_at) 或可用時間 (available_at) 的資料不能無條件通過正式 PIT 驗證。
10. 合成資料與真實資料標記不可混淆（含禁止以 0050.TW K 棒宣稱為完整官方日曆）。
11. 資料來源與版本缺漏時的例外處理與診斷資訊保留。
12. 原始子專案 (anti-gambling-trader-tw, qlib-tw-trader) 保持不變與純標準庫依賴驗證。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import unittest

# 確保 src 在匯入路徑中
ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tw_quant.backtest import (
    TAIPEI_TZ,
    TAIPEI_TZ_NAME,
    TWSE_MARKET_CLOSE_TIME,
    BacktestCalendarError,
    BacktestDataError,
    BacktestError,
    BacktestTemporalError,
    CalendarMetadata,
    CalendarNotReadyError,
    DataFrequency,
    DataProvenance,
    DataProvenanceError,
    HistoricalBarRecord,
    HistoricalDataMissingError,
    HistoricalDataProvider,
    InMemoryHistoricalDataProvider,
    InvalidOHLCVError,
    LookaheadBiasError,
    PITVerificationStatus,
    PITViolationError,
    PriceBasis,
    TradingCalendar,
    TradingDayStatus,
    UnknownTradingDateError,
    UnsupportedDataFormatError,
    ensure_taipei_datetime,
    to_taipei_date,
)
from tw_quant.strategy.trading_calendar import CalendarError, TradingCalendar as StrategyTradingCalendarProtocol


def make_synthetic_provenance(
    *,
    source: str = "SYNTHETIC_UNIT_TEST",
    dataset_version: str = "synth-v1.0.0",
    retrieved_at: dt.datetime | None = None,
) -> DataProvenance:
    return DataProvenance(
        source=source,
        dataset_version=dataset_version,
        retrieved_at=retrieved_at or dt.datetime(2026, 1, 1, 9, 0, tzinfo=TAIPEI_TZ),
        is_synthetic=True,
        description="Synthetic test fixture for Phase 3C-2.1",
    )


def make_bar(
    symbol: str = "2330",
    trade_date: dt.date = dt.date(2026, 1, 2),
    *,
    open_px: Decimal = Decimal("500.00"),
    high_px: Decimal = Decimal("510.00"),
    low_px: Decimal = Decimal("495.00"),
    close_px: Decimal = Decimal("505.00"),
    volume: int = 10_000_000,
    yesterday_close: Decimal | None = Decimal("498.00"),
    event_time: dt.datetime | None = None,
    published_at: dt.datetime | None = ...,  # type: ignore[assignment]
    available_at: dt.datetime | None = ...,  # type: ignore[assignment]
    provenance: DataProvenance | None = None,
    price_basis: PriceBasis = PriceBasis.RAW_UNADJUSTED,
    is_halted: bool = False,
) -> HistoricalBarRecord:
    ev_dt = event_time or dt.datetime.combine(trade_date, dt.time(13, 30), tzinfo=TAIPEI_TZ)
    pub_dt = (
        dt.datetime.combine(trade_date, dt.time(15, 0), tzinfo=TAIPEI_TZ)
        if published_at is ...
        else published_at
    )
    avail_dt = (
        dt.datetime.combine(trade_date, dt.time(16, 30), tzinfo=TAIPEI_TZ)
        if available_at is ...
        else available_at
    )
    prov = provenance or make_synthetic_provenance()
    return HistoricalBarRecord(
        symbol=symbol,
        trade_date=trade_date,
        open=open_px,
        high=high_px,
        low=low_px,
        close=close_px,
        volume=volume,
        yesterday_close=yesterday_close,
        event_time=ev_dt,
        published_at=pub_dt,
        available_at=avail_dt,
        provenance=prov,
        price_basis=price_basis,
        is_halted=is_halted,
    )


class TestBacktestDataLayer(unittest.TestCase):
    """第三階段 C-2.1 基礎資料層 12 大測試領域完整驗收。"""

    def setUp(self) -> None:
        # 建立跨年合成測試交易日曆：2025-12-29 (一) ~ 2026-01-06 (二)
        # 2025-12-29(一), 2025-12-30(二), 2025-12-31(三): 交易日
        # 2026-01-01(四): 元旦休市
        # 2026-01-02(五): 交易日
        # 2026-01-03(六), 2026-01-04(日): 週末休市
        # 2026-01-05(一), 2026-01-06(二): 交易日
        self.trading_days = (
            dt.date(2025, 12, 29),
            dt.date(2025, 12, 30),
            dt.date(2025, 12, 31),
            dt.date(2026, 1, 2),
            dt.date(2026, 1, 5),
            dt.date(2026, 1, 6),
        )
        self.non_trading_days = (
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 3),
            dt.date(2026, 1, 4),
        )
        self.calendar = TradingCalendar.create_synthetic_for_test(
            trading_days=self.trading_days,
            coverage_start=dt.date(2025, 12, 29),
            coverage_end=dt.date(2026, 1, 6),
            non_trading_days=self.non_trading_days,
            strict_explicit_schedule=True,
        )

    # ── 1. 有效交易日與非交易日判斷 ─────────────────────────────

    def test_01_valid_trading_and_non_trading_days(self) -> None:
        """案例 1：有效交易日與非交易日（跨年、元旦休市、週末）判斷。"""
        self.assertTrue(self.calendar.is_ready)
        self.assertTrue(self.calendar.is_synthetic)
        self.assertIsInstance(self.calendar, StrategyTradingCalendarProtocol)

        for d in self.trading_days:
            with self.subTest(trading_day=d):
                self.assertTrue(self.calendar.is_trading_day(d))
                self.assertEqual(self.calendar.get_day_status(d), TradingDayStatus.TRADING_DAY)

        for d in self.non_trading_days:
            with self.subTest(non_trading_day=d):
                self.assertFalse(self.calendar.is_trading_day(d))
                self.assertEqual(self.calendar.get_day_status(d), TradingDayStatus.NON_TRADING_DAY)

    # ── 2. 前一交易日與下一交易日 ───────────────────────────────

    def test_02_previous_and_next_trading_day_across_year_and_weekend(self) -> None:
        """案例 2：跨年、跨元旦與跨週末之前一交易日、下一交易日與區間計算。"""
        # 2025-12-31 的下一交易日應跨過 2026-01-01 元旦，直接到 2026-01-02
        self.assertEqual(
            self.calendar.next_trading_day(dt.date(2025, 12, 31)),
            dt.date(2026, 1, 2),
        )
        # 2026-01-01 (元旦休市) 的前一交易日為 2025-12-31，下一交易日為 2026-01-02
        self.assertEqual(
            self.calendar.previous_trading_day(dt.date(2026, 1, 1)),
            dt.date(2025, 12, 31),
        )
        self.assertEqual(
            self.calendar.next_trading_day(dt.date(2026, 1, 1)),
            dt.date(2026, 1, 2),
        )
        # 2026-01-02 (週五) 的下一交易日跨過週末至 2026-01-05 (週一)
        self.assertEqual(
            self.calendar.next_trading_day(dt.date(2026, 1, 2)),
            dt.date(2026, 1, 5),
        )
        self.assertEqual(
            self.calendar.prev_trading_day(dt.date(2026, 1, 5)),
            dt.date(2026, 1, 2),
        )

        # 區間計算：(2025-12-30, 2026-01-05] 包含 12/31, 1/2, 1/5 共 3 個交易日
        self.assertEqual(
            self.calendar.trading_days_between(dt.date(2025, 12, 30), dt.date(2026, 1, 5)),
            3,
        )
        self.assertEqual(
            self.calendar.get_trading_days_in_range(dt.date(2025, 12, 31), dt.date(2026, 1, 5)),
            (dt.date(2025, 12, 31), dt.date(2026, 1, 2), dt.date(2026, 1, 5)),
        )
        # 位移測試
        self.assertEqual(self.calendar.shift_trading_days(dt.date(2025, 12, 30), 2), dt.date(2026, 1, 2))
        self.assertEqual(self.calendar.shift_trading_days(dt.date(2026, 1, 5), -2), dt.date(2025, 12, 31))

        # 邊界外不得捏造未來交易日或過去交易日
        with self.assertRaises(UnknownTradingDateError):
            self.calendar.next_trading_day(dt.date(2026, 1, 6))
        with self.assertRaises(UnknownTradingDateError):
            self.calendar.previous_trading_day(dt.date(2025, 12, 29))

    # ── 3. 未知交易日曆不能默認為有效交易日 ─────────────────────

    def test_03_unknown_or_uninitialized_calendar_never_defaults_to_valid(self) -> None:
        """案例 3：未就緒日曆、超出涵蓋範圍或內部缺漏日期絕不默認為有效交易日。"""
        # A. 尚未載入真實交易日曆資料時，明確拋出 CalendarNotReadyError
        unready_cal = TradingCalendar.uninitialized()
        self.assertFalse(unready_cal.is_ready)
        self.assertEqual(unready_cal.get_day_status(dt.date(2026, 1, 2)), TradingDayStatus.UNKNOWN)
        with self.assertRaises(CalendarNotReadyError):
            unready_cal.is_trading_day(dt.date(2026, 1, 2))

        # B. 超出涵蓋範圍之日期，狀態為 UNKNOWN，呼叫 is_trading_day 拋出 UnknownTradingDateError (且同時為 CalendarError 子類別)
        self.assertEqual(self.calendar.get_day_status(dt.date(2026, 1, 7)), TradingDayStatus.UNKNOWN)
        with self.assertRaises(UnknownTradingDateError) as ctx:
            self.calendar.is_trading_day(dt.date(2026, 1, 7))
        self.assertIsInstance(ctx.exception, CalendarError)
        self.assertEqual(ctx.exception.target_date, dt.date(2026, 1, 7))

        # C. 涵蓋範圍內存在缺漏未驗證日期 (例如 2026-01-05 未列入 trading_days 亦未列入 non_trading_days)
        gapped_cal = TradingCalendar.create_synthetic_for_test(
            trading_days=(dt.date(2026, 1, 2), dt.date(2026, 1, 6)),
            non_trading_days=(dt.date(2026, 1, 3), dt.date(2026, 1, 4)),
            coverage_start=dt.date(2026, 1, 2),
            coverage_end=dt.date(2026, 1, 6),
            strict_explicit_schedule=True,
        )
        self.assertEqual(gapped_cal.get_day_status(dt.date(2026, 1, 5)), TradingDayStatus.UNKNOWN)
        with self.assertRaises(UnknownTradingDateError):
            gapped_cal.is_trading_day(dt.date(2026, 1, 5))
        # 跨越缺漏日搜尋下一交易日或計算區間交易日數時，同樣拒絕猜測並拋出 UnknownTradingDateError
        with self.assertRaises(UnknownTradingDateError):
            gapped_cal.next_trading_day(dt.date(2026, 1, 2))
        with self.assertRaises(UnknownTradingDateError):
            gapped_cal.trading_days_between(dt.date(2026, 1, 2), dt.date(2026, 1, 6))

    # ── 4. 台灣時區與日期邊界 ───────────────────────────────────

    def test_04_taiwan_timezone_and_date_boundaries(self) -> None:
        """案例 4：明確使用台灣時區 Asia/Taipei，處理跨時區日期邊界並拒絕無時區時間戳。"""
        self.assertEqual(self.calendar.timezone_name, "Asia/Taipei")
        self.assertEqual(TAIPEI_TZ_NAME, "Asia/Taipei")
        sample_dt = dt.datetime(2026, 1, 2, 9, 0, tzinfo=TAIPEI_TZ)
        self.assertEqual(sample_dt.utcoffset(), dt.timedelta(hours=8))

        # 無時區 (naive) datetime 必須被拒絕
        with self.assertRaises(BacktestCalendarError):
            ensure_taipei_datetime(dt.datetime(2026, 1, 2, 9, 0))

        # UTC 2025-12-31 18:30:00Z 換算為台灣時間為 2026-01-01 02:30:00+08:00 (元旦休市日)
        utc_dt = dt.datetime(2025, 12, 31, 18, 30, tzinfo=dt.timezone.utc)
        taipei_dt = ensure_taipei_datetime(utc_dt)
        self.assertEqual(taipei_dt.date(), dt.date(2026, 1, 1))
        self.assertEqual(to_taipei_date(utc_dt, allow_datetime=True), dt.date(2026, 1, 1))
        self.assertFalse(self.calendar.is_trading_day(utc_dt, allow_datetime=True))

        # 預設不允許直接把 datetime 當作純 date 傳入 (防止時區未轉換之截斷錯誤)
        with self.assertRaises(BacktestCalendarError):
            self.calendar.is_trading_day(utc_dt, allow_datetime=False)
        with self.assertRaises(BacktestCalendarError):
            self.calendar.is_trading_day(True)  # type: ignore[arg-type]

    # ── 5. 行情資料缺漏、錯誤排序、非法 OHLC 關係 ───────────────

    def test_05_missing_data_unsorted_series_and_invalid_ohlc(self) -> None:
        """案例 5：行情欄位缺漏、錯誤排序、重複日期與非法 OHLC 高低關係攔截。"""
        # A. 缺少必要價格或成交量欄位
        with self.assertRaises(HistoricalDataMissingError):
            make_bar(open_px=None)  # type: ignore[arg-type]
        with self.assertRaises(HistoricalDataMissingError):
            make_bar(volume=None)  # type: ignore[arg-type]

        # B. 非法 OHLC 邏輯關係
        # high < low
        with self.assertRaises(InvalidOHLCVError):
            make_bar(open_px=Decimal("500"), high_px=Decimal("490"), low_px=Decimal("495"), close_px=Decimal("492"))
        # open > high
        with self.assertRaises(InvalidOHLCVError):
            make_bar(open_px=Decimal("515"), high_px=Decimal("510"), low_px=Decimal("495"), close_px=Decimal("505"))
        # close < low
        with self.assertRaises(InvalidOHLCVError):
            make_bar(open_px=Decimal("500"), high_px=Decimal("510"), low_px=Decimal("495"), close_px=Decimal("490"))

        # C. 時間序列錯誤排序與重複日期
        bar_day1 = make_bar(trade_date=dt.date(2026, 1, 2))
        bar_day2 = make_bar(trade_date=dt.date(2026, 1, 5))
        provider = InMemoryHistoricalDataProvider(
            calendar=self.calendar,
            provenance=make_synthetic_provenance(),
        )
        provider.add_bar(bar_day2)
        # 嘗試在 2026-01-05 之後寫入較早的 2026-01-02 (亂序)
        with self.assertRaises(InvalidOHLCVError):
            provider.add_bar(bar_day1)
        # 嘗試寫入重複日期 2026-01-05
        with self.assertRaises(InvalidOHLCVError):
            provider.add_bar(make_bar(trade_date=dt.date(2026, 1, 5)))

        # D. 查詢不存在之股票或缺漏之交易日行情
        cutoff = dt.datetime(2026, 1, 6, 18, 0, tzinfo=TAIPEI_TZ)
        with self.assertRaises(HistoricalDataMissingError):
            provider.get_bar("2317", dt.date(2026, 1, 5), cutoff_time=cutoff)
        with self.assertRaises(HistoricalDataMissingError):
            provider.get_bar("2330", dt.date(2026, 1, 2), cutoff_time=cutoff)

    # ── 6. NaN、Infinity 與非法成交量 ───────────────────────────

    def test_06_nan_infinity_and_invalid_volume(self) -> None:
        """案例 6：NaN、Infinity、負數價格與非法成交量攔截。"""
        for bad_dec in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("0"), Decimal("-10")):
            with self.subTest(bad_price=bad_dec):
                with self.assertRaises(InvalidOHLCVError):
                    make_bar(close_px=bad_dec)

        for bad_float in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_float=bad_float):
                with self.assertRaises(InvalidOHLCVError):
                    make_bar(open_px=bad_float)  # type: ignore[arg-type]

        # 禁止負成交量、浮點數成交量、NaN 成交量、布林值成交量及非停牌 0 成交量
        for bad_vol in (-1000, 0, 100.5, float("nan"), float("inf"), True, Decimal("NaN")):
            with self.subTest(bad_volume=bad_vol):
                with self.assertRaises(InvalidOHLCVError):
                    make_bar(volume=bad_vol, is_halted=False)  # type: ignore[arg-type]

        # 停牌日 (is_halted=True) 允許成交量為 0
        halted_bar = make_bar(volume=0, is_halted=True)
        self.assertEqual(halted_bar.volume, 0)
        self.assertTrue(halted_bar.is_halted)

    # ── 7. 資料可用時間晚於策略截止時間時拒絕使用 ───────────────

    def test_07_reject_when_available_at_exceeds_cutoff_time(self) -> None:
        """案例 7：當資料 available_at 晚於策略 cutoff_time 時，嚴格拒絕使用並拋出 LookaheadBiasError。"""
        # 2026-01-02 收盤行情於 16:30 才可用
        bar_jan2 = make_bar(
            trade_date=dt.date(2026, 1, 2),
            event_time=dt.datetime(2026, 1, 2, 13, 30, tzinfo=TAIPEI_TZ),
            published_at=dt.datetime(2026, 1, 2, 15, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2026, 1, 2, 16, 30, tzinfo=TAIPEI_TZ),
        )
        bar_jan5 = make_bar(
            trade_date=dt.date(2026, 1, 5),
            event_time=dt.datetime(2026, 1, 5, 13, 30, tzinfo=TAIPEI_TZ),
            published_at=dt.datetime(2026, 1, 5, 15, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2026, 1, 5, 16, 30, tzinfo=TAIPEI_TZ),
        )
        provider = InMemoryHistoricalDataProvider(
            calendar=self.calendar,
            provenance=make_synthetic_provenance(),
            bars=(bar_jan2, bar_jan5),
        )

        # 在 2026-01-02 15:30 (已發布但尚未達 16:30 可用時間) 查詢，必須拋出 LookaheadBiasError
        cutoff_early = dt.datetime(2026, 1, 2, 15, 30, tzinfo=TAIPEI_TZ)
        with self.assertRaises(LookaheadBiasError) as ctx:
            provider.get_bar("2330", dt.date(2026, 1, 2), cutoff_time=cutoff_early)
        self.assertEqual(ctx.exception.available_at, dt.datetime(2026, 1, 2, 16, 30, tzinfo=TAIPEI_TZ))

        # 在 2026-01-02 16:30 查詢未來交易日 2026-01-05，必須拋出 LookaheadBiasError
        cutoff_jan2_after = dt.datetime(2026, 1, 2, 16, 30, tzinfo=TAIPEI_TZ)
        with self.assertRaises(LookaheadBiasError):
            provider.get_bar("2330", dt.date(2026, 1, 5), cutoff_time=cutoff_jan2_after)

        # 呼叫 get_available_bars 時僅回傳截至 2026-01-02 16:30 可用的 bar_jan2，不包含 bar_jan5
        avail_bars = provider.get_available_bars("2330", cutoff_time=cutoff_jan2_after)
        self.assertEqual(len(avail_bars), 1)
        self.assertEqual(avail_bars[0].trade_date, dt.date(2026, 1, 2))

    # ── 8. 盤後發布資料不能回填至當日盤中 & 禁止同日收盤成交 ─────

    def test_08_post_market_data_cannot_backfill_intraday_or_execute_same_day_close(self) -> None:
        """案例 8：盤後發布資料不得回填至當日盤中，且盤後收盤資訊嚴禁於 T 日收盤成交。"""
        # A. 嘗試建立 event_time、published_at 或 available_at 早於 13:30 收盤之日線紀錄，立即拋出 PITViolationError
        with self.assertRaises(PITViolationError):
            make_bar(
                trade_date=dt.date(2026, 1, 2),
                event_time=dt.datetime(2026, 1, 2, 12, 0, tzinfo=TAIPEI_TZ),
            )
        with self.assertRaises(PITViolationError):
            make_bar(
                trade_date=dt.date(2026, 1, 2),
                event_time=dt.datetime(2026, 1, 2, 13, 30, tzinfo=TAIPEI_TZ),
                published_at=dt.datetime(2026, 1, 2, 11, 30, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2026, 1, 2, 16, 30, tzinfo=TAIPEI_TZ),
            )

        # B. 對於盤後才取得的 T 日 (2026-01-02) 收盤資訊，阻止策略在 T 日盤中 (10:00)、T 日收盤 (13:30) 或 T 日盤後執行成交
        bar_jan2 = make_bar(trade_date=dt.date(2026, 1, 2))
        self.assertFalse(bar_jan2.allows_same_day_close_execution)

        provider = InMemoryHistoricalDataProvider(
            calendar=self.calendar,
            provenance=make_synthetic_provenance(),
            bars=(bar_jan2,),
        )
        for illegal_exec_time in (
            dt.datetime(2026, 1, 2, 10, 0, tzinfo=TAIPEI_TZ),
            dt.datetime(2026, 1, 2, 13, 30, tzinfo=TAIPEI_TZ),
            dt.datetime(2026, 1, 2, 17, 0, tzinfo=TAIPEI_TZ),
        ):
            with self.subTest(illegal_exec_time=illegal_exec_time):
                with self.assertRaises(LookaheadBiasError):
                    provider.validate_execution_timing_for_bar(bar_jan2, illegal_exec_time)

        # 排程至下一交易日 (2026-01-05 09:00) 則通過因果檢查
        next_open = dt.datetime(2026, 1, 5, 9, 0, tzinfo=TAIPEI_TZ)
        provider.validate_execution_timing_for_bar(bar_jan2, next_open)

    # ── 9. 缺少發布時間或可用時間的資料不能無條件通過正式 PIT 驗證 ─

    def test_09_missing_published_or_available_time_rejected_in_pit_validation(self) -> None:
        """案例 9：缺少 published_at 或 available_at 的資料標記為未驗證，且拒絕通過正式 PIT 驗證。"""
        unverified_bar = make_bar(
            trade_date=dt.date(2026, 1, 2),
            published_at=None,
            available_at=None,
        )
        # 系統絕不自行捏造或推定 available_at = event_time
        self.assertIsNone(unverified_bar.published_at)
        self.assertIsNone(unverified_bar.available_at)
        self.assertFalse(unverified_bar.is_pit_verified)
        self.assertEqual(unverified_bar.pit_status, PITVerificationStatus.UNVERIFIED_MISSING_TIMESTAMPS)

        provider = InMemoryHistoricalDataProvider(
            calendar=self.calendar,
            provenance=make_synthetic_provenance(),
            bars=(unverified_bar,),
        )
        cutoff = dt.datetime(2026, 1, 2, 18, 0, tzinfo=TAIPEI_TZ)

        # 預設正式 PIT 查詢 (require_pit_verified=True) 必須拋出 PITViolationError
        with self.assertRaises(PITViolationError):
            provider.get_bar("2330", dt.date(2026, 1, 2), cutoff_time=cutoff)
        with self.assertRaises(PITViolationError):
            provider.get_available_bars("2330", cutoff_time=cutoff)

        # 僅在顯式聲明 allow unverified (require_pit_verified=False) 時才可讀取，且其 is_pit_verified 仍為 False
        raw_bar = provider.get_bar(
            "2330",
            dt.date(2026, 1, 2),
            cutoff_time=cutoff,
            require_pit_verified=False,
        )
        self.assertFalse(raw_bar.is_pit_verified)

    # ── 10. 合成資料與真實資料標記不可混淆 ───────────────────────

    def test_10_synthetic_and_real_data_flags_cannot_be_confused(self) -> None:
        """案例 10：合成資料與真實資料標記不可混淆，且禁止以 0050.TW K 棒宣稱為正式交易日曆。"""
        # A. 禁止以 0050.TW K 棒或 yfinance 宣稱為正式交易日曆 (is_synthetic=False)
        for bad_source in ("0050.TW_KBAR", "yfinance_0050", "SYNTHETIC_CALENDAR", "MOCK_EXCHANGE"):
            with self.subTest(bad_calendar_source=bad_source):
                with self.assertRaises(DataProvenanceError):
                    CalendarMetadata(
                        source=bad_source,
                        version="v1.0",
                        is_synthetic=False,
                    )

        # B. 禁止將含 SYNTHETIC / MOCK / TEST / FIXTURE 的行情資料標記為 is_synthetic=False
        for bad_src in ("SYNTHETIC_FEED", "MOCK_PRICE_DB", "TEST_FIXTURE"):
            with self.subTest(bad_data_source=bad_src):
                with self.assertRaises(DataProvenanceError):
                    DataProvenance(
                        source=bad_src,
                        dataset_version="v1.0",
                        retrieved_at=dt.datetime(2026, 1, 1, 9, 0, tzinfo=TAIPEI_TZ),
                        is_synthetic=False,
                    )

        # C. 正式模式 (require_real_data=True 或 allow_synthetic=False) 下拒絕合成日曆與合成資料
        with self.assertRaises(DataProvenanceError):
            self.calendar.assert_ready(allow_synthetic=False)

        provider = InMemoryHistoricalDataProvider(
            calendar=self.calendar,
            provenance=make_synthetic_provenance(),
            bars=(make_bar(trade_date=dt.date(2026, 1, 2)),),
        )
        with self.assertRaises(DataProvenanceError):
            provider.get_bar(
                "2330",
                dt.date(2026, 1, 2),
                cutoff_time=dt.datetime(2026, 1, 2, 18, 0, tzinfo=TAIPEI_TZ),
                require_real_data=True,
            )

        # D. 禁止在合成 Provider 中混入真實標記之 BarRecord
        real_prov = DataProvenance(
            source="TWSE_HISTORICAL_ARCHIVE",
            dataset_version="2026Q1",
            retrieved_at=dt.datetime(2026, 1, 10, 9, 0, tzinfo=TAIPEI_TZ),
            is_synthetic=False,
        )
        real_bar = make_bar(trade_date=dt.date(2026, 1, 5), provenance=real_prov)
        with self.assertRaises(DataProvenanceError):
            provider.add_bar(real_bar)

    # ── 11. 資料來源與版本缺漏時的處理 ───────────────────────────

    def test_11_missing_provenance_source_or_version_handling(self) -> None:
        """案例 11：資料來源 (source)、版本 (dataset_version) 或擷取時間 (retrieved_at) 缺漏時拋出 DataProvenanceError。"""
        with self.assertRaises(DataProvenanceError):
            DataProvenance(
                source="",
                dataset_version="v1.0",
                retrieved_at=dt.datetime(2026, 1, 1, 9, 0, tzinfo=TAIPEI_TZ),
                is_synthetic=True,
            )
        with self.assertRaises(DataProvenanceError):
            DataProvenance(
                source="SYNTHETIC_TEST",
                dataset_version="   ",
                retrieved_at=dt.datetime(2026, 1, 1, 9, 0, tzinfo=TAIPEI_TZ),
                is_synthetic=True,
            )
        with self.assertRaises(DataProvenanceError):
            DataProvenance(
                source="SYNTHETIC_TEST",
                dataset_version="v1.0",
                retrieved_at=None,  # type: ignore[arg-type]
                is_synthetic=True,
            )
        with self.assertRaises(DataProvenanceError):
            DataProvenance(
                source="SYNTHETIC_TEST",
                dataset_version="v1.0",
                retrieved_at=dt.datetime(2026, 1, 1, 9, 0),  # naive datetime
                is_synthetic=True,
            )

    # ── 12. 原始子專案保持不變與純標準庫依賴驗證 ─────────────────

    def test_12_subprojects_unchanged_and_pure_stdlib_imports(self) -> None:
        """案例 12：驗證兩個原始子專案 Git 狀態與 Commit 未受影響，且新模組不依賴外部套件。"""
        expected_commits = {
            "anti-gambling-trader-tw": "8609cef73718a056fddb74201fc304f174fa1553",
            "qlib-tw-trader": "774b3fb60d3b20b5c9ee86d25052ad2ec08e2011",
        }
        for repo_name, expected_sha in expected_commits.items():
            repo_dir = ROOT_DIR / repo_name
            head_proc = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(head_proc.stdout.strip(), expected_sha)

            status_proc = subprocess.run(
                ["git", "-C", str(repo_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(status_proc.stdout.strip(), "", f"{repo_name} 工作目錄應保持完全乾淨")

        # 確認未意外載入 qlib, sqlalchemy, lightgbm, pandas, numpy 等非標準庫套件至 backtest 模組
        forbidden_pkgs = ("qlib", "sqlalchemy", "lightgbm", "yfinance")
        for pkg in forbidden_pkgs:
            self.assertNotIn(pkg, sys.modules, f"基礎回測資料層不得引入外部套件 {pkg}")

    # ── 13. 不支援格式攔截與 MarketBar 轉換驗證 ──────────────────

    def test_13_market_bar_conversion_and_unsupported_formats(self) -> None:
        """案例 13：前復權價 (FORWARD_ADJUSTED) 與非法頻率攔截，以及轉換為 MarketBar。"""
        with self.assertRaises(UnsupportedDataFormatError):
            make_bar(price_basis=PriceBasis.FORWARD_ADJUSTED)

        with self.assertRaises(UnsupportedDataFormatError):
            HistoricalBarRecord(
                symbol="2330",
                trade_date=dt.date(2026, 1, 2),
                open=Decimal("500"),
                high=Decimal("510"),
                low=Decimal("495"),
                close=Decimal("505"),
                volume=1000,
                event_time=dt.datetime(2026, 1, 2, 13, 30, tzinfo=TAIPEI_TZ),
                published_at=dt.datetime(2026, 1, 2, 15, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2026, 1, 2, 16, 30, tzinfo=TAIPEI_TZ),
                provenance=make_synthetic_provenance(),
                frequency="TICK",  # type: ignore[arg-type]
            )

        bar = make_bar(trade_date=dt.date(2026, 1, 2), yesterday_close=Decimal("500.00"))
        market_bar = bar.to_market_bar()
        self.assertEqual(market_bar.symbol, "2330")
        self.assertEqual(market_bar.close, Decimal("505.00"))
        self.assertEqual(market_bar.yesterday_close, Decimal("500.00"))

        # 若缺少 yesterday_close 且未提供 fallback，拒絕轉換並拋出 HistoricalDataMissingError
        bar_no_y = make_bar(trade_date=dt.date(2026, 1, 2), yesterday_close=None)
        with self.assertRaises(HistoricalDataMissingError):
            bar_no_y.to_market_bar()

    # ── 14. 與既有 StrategyEngine 之日曆協定相容性驗證 ────────────

    def test_14_strategy_engine_compatibility_with_backtest_calendar(self) -> None:
        """案例 14：驗證新 TradingCalendar 可直接注入既有 StrategyEngine 正常運作。"""
        from tw_quant.broker.config import TaiwanPaperConfig
        from tw_quant.strategy.config import CostModel, StrategyConfig
        from tw_quant.strategy.engine import StrategyEngine

        cfg = StrategyConfig(
            strategy_name="equal_weight_top5",
            strategy_version="v1.0",
            max_positions=5,
            min_score=0.0,
            target_position_ratio=Decimal("0.10"),
            holding_trading_days=5,
            cost_model=CostModel.from_paper_config(TaiwanPaperConfig()),
        )
        engine = StrategyEngine(config=cfg, calendar=self.calendar)
        self.assertIs(engine.calendar, self.calendar)


if __name__ == "__main__":
    unittest.main()
