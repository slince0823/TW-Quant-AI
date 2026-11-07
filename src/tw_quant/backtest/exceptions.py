"""台股歷史回測基礎例外階層 (exceptions.py).

核心原則：
1. 明確區分資料缺漏、交易日曆異常、PIT 時序違規、未來資訊洩漏、格式/數值錯誤及資料血統（來源與版本）缺失。
2. 所有例外均保留結構化診斷資訊（error_code 與 context 字典），便於自動化測試斷言與稽核報告定位根因。
3. BacktestCalendarError 同時繼承自策略層之 CalendarError，確保與既有 StrategyEngine 100% 相容。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from ..strategy.trading_calendar import CalendarError


class BacktestError(Exception):
    """台股歷史回測模組之基底例外。"""

    default_code: str = "BACKTEST_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message
        self.error_code = error_code or self.default_code
        self.context: Dict[str, Any] = dict(context) if context else {}
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if not self.context:
            return f"[{self.error_code}] {self.message}"
        ctx_parts = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"[{self.error_code}] {self.message} ({ctx_parts})"


# ── 1. 交易日曆相關例外 ──────────────────────────────────────────────


class BacktestCalendarError(BacktestError, CalendarError):
    """交易日曆相關錯誤基底類別（同時相容於策略層 CalendarError）。"""

    default_code = "CALENDAR_ERROR"


class CalendarNotReadyError(BacktestCalendarError):
    """交易日曆尚未載入有效資料，或試圖在正式模式使用未驗證/代理日曆。"""

    default_code = "CALENDAR_NOT_READY"


class UnknownTradingDateError(BacktestCalendarError):
    """查詢日期超出日曆已知涵蓋範圍，或落入未驗證之未知日期區間（嚴禁默認為有效交易日）。"""

    default_code = "UNKNOWN_TRADING_DATE"

    def __init__(
        self,
        message: str,
        *,
        target_date: Optional[dt.date] = None,
        coverage_start: Optional[dt.date] = None,
        coverage_end: Optional[dt.date] = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if target_date is not None:
            ctx["target_date"] = target_date.isoformat() if isinstance(target_date, dt.date) else target_date
        if coverage_start is not None:
            ctx["coverage_start"] = coverage_start.isoformat()
        if coverage_end is not None:
            ctx["coverage_end"] = coverage_end.isoformat()
        self.target_date = target_date
        self.coverage_start = coverage_start
        self.coverage_end = coverage_end
        super().__init__(message, error_code=error_code, context=ctx)


# 相容設計書別名
CalendarBoundaryError = UnknownTradingDateError


# ── 2. 時序因果、PIT 規則與前視偏差例外 ───────────────────────────────


class BacktestTemporalError(BacktestError):
    """時序因果與 Point-in-Time (PIT) 相關錯誤基底類別。"""

    default_code = "TEMPORAL_ERROR"


class PITViolationError(BacktestTemporalError):
    """資料時間戳違反 Point-in-Time (PIT) 規則。

    常見觸發情境：
    - 缺少發布時間 (published_at) 或可用時間 (available_at) 卻試圖通過正式 PIT 驗證
    - 發布時間早於事件發生時間 (published_at < event_time)
    - 可用時間早於發布時間 (available_at < published_at)
    - 盤後才產生之日線收盤資料被回填標記為當日盤中可用
    - 時間戳缺少明確時區 (naive datetime)
    """

    default_code = "PIT_VIOLATION"

    def __init__(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[dt.date] = None,
        event_time: Optional[dt.datetime] = None,
        published_at: Optional[dt.datetime] = None,
        available_at: Optional[dt.datetime] = None,
        cutoff_time: Optional[dt.datetime] = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if symbol is not None:
            ctx["symbol"] = symbol
        if trade_date is not None:
            ctx["trade_date"] = trade_date.isoformat() if isinstance(trade_date, dt.date) else trade_date
        if event_time is not None:
            ctx["event_time"] = event_time.isoformat() if isinstance(event_time, dt.datetime) else event_time
        if published_at is not None:
            ctx["published_at"] = published_at.isoformat() if isinstance(published_at, dt.datetime) else published_at
        if available_at is not None:
            ctx["available_at"] = available_at.isoformat() if isinstance(available_at, dt.datetime) else available_at
        if cutoff_time is not None:
            ctx["cutoff_time"] = cutoff_time.isoformat() if isinstance(cutoff_time, dt.datetime) else cutoff_time
        self.symbol = symbol
        self.trade_date = trade_date
        self.event_time = event_time
        self.published_at = published_at
        self.available_at = available_at
        self.cutoff_time = cutoff_time
        super().__init__(message, error_code=error_code, context=ctx)


class LookaheadBiasError(BacktestTemporalError):
    """未來資料洩漏（前視偏差 Lookahead Bias）錯誤。

    常見觸發情境：
    - 請求取得 available_at > cutoff_time 之未來資料
    - 查詢交易日晚於當前策略截止日
    - 試圖使用 T 日盤後才取得的收盤資訊在 T 日盤中或 T 日收盤成交
    """

    default_code = "LOOKAHEAD_BIAS_DETECTED"

    def __init__(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[dt.date] = None,
        available_at: Optional[dt.datetime] = None,
        cutoff_time: Optional[dt.datetime] = None,
        execution_time: Optional[dt.datetime] = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if symbol is not None:
            ctx["symbol"] = symbol
        if trade_date is not None:
            ctx["trade_date"] = trade_date.isoformat() if isinstance(trade_date, dt.date) else trade_date
        if available_at is not None:
            ctx["available_at"] = available_at.isoformat() if isinstance(available_at, dt.datetime) else available_at
        if cutoff_time is not None:
            ctx["cutoff_time"] = cutoff_time.isoformat() if isinstance(cutoff_time, dt.datetime) else cutoff_time
        if execution_time is not None:
            ctx["execution_time"] = (
                execution_time.isoformat() if isinstance(execution_time, dt.datetime) else execution_time
            )
        self.symbol = symbol
        self.trade_date = trade_date
        self.available_at = available_at
        self.cutoff_time = cutoff_time
        self.execution_time = execution_time
        super().__init__(message, error_code=error_code, context=ctx)


# 相容設計書別名
LookaheadViolationError = LookaheadBiasError
FutureDataLeakageError = LookaheadBiasError


# ── 3. 歷史資料內容、格式與血統例外 ──────────────────────────────────


class BacktestDataError(BacktestError):
    """歷史資料相關錯誤基底類別。"""

    default_code = "BACKTEST_DATA_ERROR"


class HistoricalDataMissingError(BacktestDataError):
    """歷史資料不存在、指定標的/日期缺漏或必要欄位不完整。"""

    default_code = "HISTORICAL_DATA_MISSING"

    def __init__(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[dt.date] = None,
        missing_fields: Optional[tuple[str, ...]] = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if symbol is not None:
            ctx["symbol"] = symbol
        if trade_date is not None:
            ctx["trade_date"] = trade_date.isoformat() if isinstance(trade_date, dt.date) else trade_date
        if missing_fields:
            ctx["missing_fields"] = missing_fields
        self.symbol = symbol
        self.trade_date = trade_date
        self.missing_fields = missing_fields
        super().__init__(message, error_code=error_code, context=ctx)


IncompleteHistoricalDataError = HistoricalDataMissingError


class InvalidOHLCVError(BacktestDataError):
    """OHLCV 數值範圍非法（如 NaN/Inf/負數）、高低價邏輯矛盾或時間序列排序錯誤。"""

    default_code = "INVALID_OHLCV_DATA"

    def __init__(
        self,
        message: str,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[dt.date] = None,
        field_name: Optional[str] = None,
        invalid_value: Any = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if symbol is not None:
            ctx["symbol"] = symbol
        if trade_date is not None:
            ctx["trade_date"] = trade_date.isoformat() if isinstance(trade_date, dt.date) else trade_date
        if field_name is not None:
            ctx["field_name"] = field_name
        if invalid_value is not None:
            ctx["invalid_value"] = repr(invalid_value)
        self.symbol = symbol
        self.trade_date = trade_date
        self.field_name = field_name
        self.invalid_value = invalid_value
        super().__init__(message, error_code=error_code, context=ctx)


DataIntegrityError = InvalidOHLCVError


class UnsupportedDataFormatError(BacktestDataError):
    """不支援的資料頻率、價格基準或資料結構格式。"""

    default_code = "UNSUPPORTED_DATA_FORMAT"

    def __init__(
        self,
        message: str,
        *,
        frequency: Any = None,
        format_type: Any = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if frequency is not None:
            ctx["frequency"] = frequency
        if format_type is not None:
            ctx["format_type"] = format_type
        self.frequency = frequency
        self.format_type = format_type
        super().__init__(message, error_code=error_code, context=ctx)


class DataProvenanceError(BacktestDataError):
    """資料來源、擷取時間、版本資訊缺漏，或合成資料與真實資料標記混淆。"""

    default_code = "DATA_PROVENANCE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        source: Optional[str] = None,
        dataset_version: Optional[str] = None,
        is_synthetic: Optional[bool] = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if source is not None:
            ctx["source"] = source
        if dataset_version is not None:
            ctx["dataset_version"] = dataset_version
        if is_synthetic is not None:
            ctx["is_synthetic"] = is_synthetic
        self.source = source
        self.dataset_version = dataset_version
        self.is_synthetic = is_synthetic
        super().__init__(message, error_code=error_code, context=ctx)


# ── 4. 持倉批次帳務與公司行動例外 (第三階段 C-2.2) ───────────────────


class BacktestAccountingError(BacktestDataError):
    """回測持倉批次 (Lot) 與會計帳務錯誤基底類別。"""

    default_code = "BACKTEST_ACCOUNTING_ERROR"


class DuplicateFillError(BacktestAccountingError):
    """重複之成交紀錄識別碼 (fill_id) 入帳錯誤。"""

    default_code = "DUPLICATE_FILL_ID"


class OversellPositionError(BacktestAccountingError):
    """賣出成交股數超過目前未平倉批次總股數（嚴禁自動建立空頭持倉）。"""

    default_code = "OVERSELL_POSITION"


class InvalidFillRecordError(BacktestAccountingError):
    """成交紀錄欄位不合法、時序倒流、非有效交易日或金額/股數不一致。"""

    default_code = "INVALID_FILL_RECORD"


class CorporateActionError(BacktestDataError):
    """除權息與公司行動處理錯誤基底類別。"""

    default_code = "CORPORATE_ACTION_ERROR"


class DuplicateCorporateActionError(CorporateActionError):
    """同一公司行動事件重複註冊、重複除息入帳或重複發放現金。"""

    default_code = "DUPLICATE_CORPORATE_ACTION"


class CorporateActionOrderError(CorporateActionError):
    """公司行動事件順序錯誤（如發放日早於除息日、回溯處理過期事件或未到期提前發放）。"""

    default_code = "CORPORATE_ACTION_ORDER_ERROR"


class UnsupportedCorporateActionError(CorporateActionError, UnsupportedDataFormatError):
    """尚未支援之公司行動類型（如股票分割、股票股利、減資），嚴禁靜默略過或僅改股數不調成本。"""

    default_code = "UNSUPPORTED_CORPORATE_ACTION"


# ── 5. 歷史撮合模擬與投資組合估值例外 (第三階段 C-2.3) ───────────────


class BacktestExecutionError(BacktestError):
    """歷史日線撮合模擬器錯誤基底類別。"""

    default_code = "BACKTEST_EXECUTION_ERROR"


class PriceLimitError(BacktestExecutionError):
    """台股漲跌幅限制計算或驗證錯誤。"""

    default_code = "PRICE_LIMIT_ERROR"


class PriceLimitDataMissingError(PriceLimitError, HistoricalDataMissingError):
    """缺少計算漲跌幅限制所需之參考價、商品類別、上市階段或有效日期規則（嚴禁自行推測）。"""

    default_code = "PRICE_LIMIT_DATA_MISSING"


class TickSizeViolationError(BacktestExecutionError):
    """委託限價或撮合價格不符合適用之台股升降單位 (Tick Size)。"""

    default_code = "TICK_SIZE_VIOLATION"


class UnknownTradingStatusError(BacktestExecutionError):
    """標的交易狀態為未知 (UNKNOWN)，嚴禁自動視為正常可交易。"""

    default_code = "UNKNOWN_TRADING_STATUS"


class DuplicateOrderExecutionError(BacktestExecutionError):
    """同一委託重複提交、同一交易時段重複撮合、或終態委託重複釋放預留款券。"""

    default_code = "DUPLICATE_ORDER_EXECUTION"


class OrderReservationError(BacktestExecutionError, BacktestAccountingError):
    """委託預留資金或預留股數不足、超額釋放或帳務不一致錯誤。"""

    default_code = "ORDER_RESERVATION_ERROR"


class BacktestValuationError(BacktestError):
    """投資組合每日估值器錯誤基底類別。"""

    default_code = "BACKTEST_VALUATION_ERROR"


class ValuationPriceMissingError(BacktestValuationError, HistoricalDataMissingError):
    """持倉標的缺少有效收盤報價且無合法可用之最後已知價格（嚴禁默認價格為 0）。"""

    default_code = "VALUATION_PRICE_MISSING"


class StaleValuationPriceError(BacktestValuationError):
    """持倉標的最後已知價格之過期天數超過允許上限，無法合理估值。"""

    default_code = "STALE_VALUATION_PRICE"


class IncompleteValuationError(BacktestValuationError):
    """估值不完整或含無法估值之部位，拒絕輸出虛假精確的正式 NAV。"""

    default_code = "INCOMPLETE_VALUATION"


# ── 6. 預測供給、策略／風控橋接與事件驅動主迴圈例外 (第三階段 C-2.4) ──────


class PredictionFeedError(BacktestError):
    """回測預測供給器 (`PredictionFeed`) 錯誤基底類別。"""

    default_code = "PREDICTION_FEED_ERROR"


class PredictionBatchUnavailableError(PredictionFeedError, HistoricalDataMissingError):
    """指定決策日缺少可用之 `PredictionBatch`（在 FAIL_FAST 模式下觸發）。"""

    default_code = "PREDICTION_BATCH_UNAVAILABLE"


class DuplicatePredictionConsumptionError(PredictionFeedError):
    """同一 `PredictionBatch` 或同一決策日之預測被重複消費。"""

    default_code = "DUPLICATE_PREDICTION_CONSUMPTION"


class PredictionProvenanceError(PredictionFeedError, DataProvenanceError):
    """預測批次之模型來源、訓練截止日或合成／Mock 標記不一致或試圖冒充真實歷史模型。"""

    default_code = "PREDICTION_PROVENANCE_ERROR"


class EmbargoViolationError(BacktestTemporalError):
    """Walk-Forward 訓練期、冷卻隔離期 (Embargo) 與測試期重疊或模型訓練截止日越界錯誤。"""

    default_code = "EMBARGO_VIOLATION"


class BacktestRunnerError(BacktestError):
    """事件驅動 Walk-Forward 回測執行器錯誤基底類別。"""

    default_code = "BACKTEST_RUNNER_ERROR"


class DuplicateRunnerExecutionError(BacktestRunnerError):
    """同一回測執行器實例重複執行、或同一交易日／同一時間點步驟被重複處理。"""

    default_code = "DUPLICATE_RUNNER_EXECUTION"





# ── 7. 績效分析與報告例外 (第三階段 C-2.5) ───────────────────────────


class BacktestMetricsError(BacktestError):
    """績效分析與報告例外基底類別。"""

    default_code = "BACKTEST_METRICS_ERROR"


class InvalidNavSeriesError(BacktestMetricsError):
    """NAV 序列不合法（日期未排序／重複、NAV 非正值、估值不完整、與持倉不一致）。"""

    default_code = "INVALID_NAV_SERIES"


class BenchmarkAlignmentError(BacktestMetricsError):
    """Benchmark 與策略日期區間／交易日不一致，或報酬口徑不一致。"""

    default_code = "BENCHMARK_ALIGNMENT_ERROR"
