"""台股歷史回測 AI 預測批次供給器模組 (prediction_feed.py).

核心原則：
1. 嚴格時序因果與 Point-in-Time (PIT) 驗證：
   - 每個預測批次必須同時明確紀錄：
     * `decision_date` (T 日，盤後特徵與預測產生日)
     * `target_trade_date` (T+1 日，目標執行交易日，即 `batch.as_of_date == calendar.next_trading_day(T)`)
     * `data_cutoff_time` (特徵資料截止時間，必須具備 Asia/Taipei 時區且日期為 T 日)
     * `generated_at` (模型推論完成時間，`>= data_cutoff_time`)
     * `available_at` (系統實際可取得該預測批次的時間，`>= generated_at`)
     * `trained_cutoff_date` (模型訓練集截止日，不得侵入冷卻期 Embargo 或測試期)
2. 攔截前視偏差 (Lookahead Bias) 與未驗證時間戳：
   - 當回測推進至 T 日盤後決策時點 `decision_cutoff_time` 時，僅能讀取
     `data_cutoff_time <= decision_cutoff_time`、`generated_at <= decision_cutoff_time`、
     且 `available_at <= decision_cutoff_time` 之預測批次。
   - 缺少 `available_at` 之預測批次絕不允許靜默通過正式 PIT 驗證，依 `UnverifiedPredictionPolicy`
     明確拒絕 (`REJECT`) 或隔離 (`QUARANTINE`)。
3. Walk-Forward 視窗與 Embargo 隔離防護：
   - 強制驗證 `train_end_date < embargo < test_start_date` 以及 `trained_cutoff_date <= decision_date`，
     杜絕 In-Sample 訓練資料洩漏至 Out-of-Sample 回測期間。
4. 單次消費保護與防偽血統標籤：
   - 同一 `decision_date` 或同一 `batch_id` 在單次回測中最多僅能被消費一次，重複消費立即拋出
     `DuplicatePredictionConsumptionError`。
   - 嚴格綁定 `DataProvenance`，禁止將 Mock / 合成預測標記為非合成真實歷史預測。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, runtime_checkable

from ..prediction.contract import PredictionBatch, PredictionMetadata, StockPrediction
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .data_provider import DataProvenance
from .exceptions import (
    BacktestCalendarError,
    DuplicatePredictionConsumptionError,
    EmbargoViolationError,
    LookaheadBiasError,
    PITViolationError,
    PredictionBatchUnavailableError,
    PredictionFeedError,
    PredictionProvenanceError,
)


class UnverifiedPredictionPolicy(str, Enum):
    """缺少 `available_at` 或未驗證時間戳之預測批次處理政策。"""

    # 預設嚴格拒絕：直接拋出 PITViolationError
    REJECT = "REJECT"
    # 隔離模式：將未驗證批次移入隔離區 (quarantined_batches) 並排除於可用預測之外
    QUARANTINE = "QUARANTINE"
    # 合成沙盒回退：僅允許在明確標記 is_synthetic=True 之合成測試環境中以 generated_at 作為 available_at 並附加警告
    SYNTHETIC_SANDBOX_FALLBACK = "SYNTHETIC_SANDBOX_FALLBACK"


@dataclass(frozen=True)
class WalkForwardWindowSpec:
    """Walk-Forward 滾動訓練／冷卻隔離 (Embargo)／測試視窗規格。

    時序不變量：
    - `train_start_date <= train_end_date < test_start_date <= test_end_date`
    - 在交易日曆上，`train_end_date` 與 `test_start_date` 之間必須至少間隔 `embargo_days` 個交易日：
      亦即 `calendar.trading_days_between(train_end_date, test_start_date) >= embargo_days + 1`。
    """

    window_id: str
    train_start_date: dt.date
    train_end_date: dt.date
    embargo_days: int
    test_start_date: dt.date
    test_end_date: dt.date

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or not self.window_id.strip():
            raise PredictionFeedError("WalkForwardWindowSpec.window_id 必須為非空字串")
        object.__setattr__(self, "window_id", self.window_id.strip())

        t_start = to_taipei_date(self.train_start_date, "train_start_date", allow_datetime=False)
        t_end = to_taipei_date(self.train_end_date, "train_end_date", allow_datetime=False)
        test_start = to_taipei_date(self.test_start_date, "test_start_date", allow_datetime=False)
        test_end = to_taipei_date(self.test_end_date, "test_end_date", allow_datetime=False)

        object.__setattr__(self, "train_start_date", t_start)
        object.__setattr__(self, "train_end_date", t_end)
        object.__setattr__(self, "test_start_date", test_start)
        object.__setattr__(self, "test_end_date", test_end)

        if isinstance(self.embargo_days, bool) or not isinstance(self.embargo_days, int) or self.embargo_days < 0:
            raise EmbargoViolationError(
                f"embargo_days 必須為非負整數，收到 {self.embargo_days!r}",
                context={"window_id": self.window_id, "embargo_days": self.embargo_days},
            )

        if t_start > t_end:
            raise EmbargoViolationError(
                f"Walk-Forward 視窗 {self.window_id} 之 train_start_date ({t_start}) 不得晚於 train_end_date ({t_end})",
                context={
                    "window_id": self.window_id,
                    "train_start_date": t_start.isoformat(),
                    "train_end_date": t_end.isoformat(),
                },
            )
        if test_start > test_end:
            raise EmbargoViolationError(
                f"Walk-Forward 視窗 {self.window_id} 之 test_start_date ({test_start}) 不得晚於 test_end_date ({test_end})",
                context={
                    "window_id": self.window_id,
                    "test_start_date": test_start.isoformat(),
                    "test_end_date": test_end.isoformat(),
                },
            )
        if t_end >= test_start:
            raise EmbargoViolationError(
                f"Walk-Forward 視窗 {self.window_id} 違反樣本外隔離原則: "
                f"train_end_date ({t_end}) 必須嚴格早於 test_start_date ({test_start})",
                context={
                    "window_id": self.window_id,
                    "train_end_date": t_end.isoformat(),
                    "test_start_date": test_start.isoformat(),
                },
            )

    def validate_with_calendar(self, calendar: TradingCalendar) -> None:
        """結合 `TradingCalendar` 驗證訓練結束日與測試開始日間隔滿足 `embargo_days` 交易日。"""
        if not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError("calendar 必須為 TradingCalendar 實例")
        gap_trading_days = calendar.trading_days_between(self.train_end_date, self.test_start_date)
        required_step = self.embargo_days + 1
        if gap_trading_days < required_step:
            raise EmbargoViolationError(
                f"Walk-Forward 視窗 {self.window_id} 違反 Embargo 隔離天數要求: "
                f"train_end_date ({self.train_end_date}) 至 test_start_date ({self.test_start_date}) "
                f"僅相隔 {gap_trading_days - 1} 個中間交易日，少於設定之 embargo_days={self.embargo_days}",
                context={
                    "window_id": self.window_id,
                    "train_end_date": self.train_end_date.isoformat(),
                    "test_start_date": self.test_start_date.isoformat(),
                    "embargo_days": self.embargo_days,
                    "actual_intermediate_trading_days": max(0, gap_trading_days - 1),
                },
            )


@dataclass(frozen=True)
class PredictionFeedEntry:
    """回測預測供給器中之單一預測項目與完整時序／血統紀錄。"""

    batch: PredictionBatch
    decision_date: dt.date
    target_trade_date: dt.date
    data_cutoff_time: dt.datetime
    generated_at: dt.datetime
    available_at: Optional[dt.datetime]
    provenance: DataProvenance
    window_spec: Optional[WalkForwardWindowSpec] = None
    warnings: Tuple[str, ...] = ()

    @property
    def batch_id(self) -> str:
        return self.batch.batch_id

    @property
    def metadata(self) -> PredictionMetadata:
        return self.batch.metadata

    @property
    def model_version(self) -> str:
        return self.batch.metadata.model_version

    @property
    def feature_version(self) -> str:
        return self.batch.metadata.feature_version

    @property
    def trained_cutoff_date(self) -> Optional[dt.date]:
        return self.batch.metadata.trained_cutoff_date


@dataclass(frozen=True)
class QuarantinedPredictionRecord:
    """因時間戳未驗證或違反隔離政策而被隔離之預測批次紀錄。"""

    batch_id: str
    decision_date: dt.date
    target_trade_date: dt.date
    reason: str
    quarantined_at: dt.datetime
    error_code: str = "UNVERIFIED_PREDICTION_QUARANTINED"


@dataclass(frozen=True)
class PredictionConsumptionRecord:
    """單次預測批次消費審計紀錄。"""

    batch_id: str
    decision_date: dt.date
    target_trade_date: dt.date
    decision_cutoff_time: dt.datetime
    data_cutoff_time: dt.datetime
    generated_at: dt.datetime
    available_at: dt.datetime
    model_name: str
    model_version: str
    feature_version: str
    trained_cutoff_date: Optional[dt.date]
    is_mock: bool
    is_synthetic: bool
    window_id: Optional[str]
    prediction_count: int
    warnings: Tuple[str, ...] = ()


@runtime_checkable
class PredictionFeed(Protocol):
    """回測 AI 預測批次供給器協定介面 (`PredictionFeed`)."""

    @property
    def provenance(self) -> DataProvenance:
        """回傳預測資料集之來源與合成標記血統資訊。"""
        ...

    @property
    def is_synthetic(self) -> bool:
        """是否為合成／測試預測資料集。"""
        ...

    @property
    def consumed_records(self) -> Tuple[PredictionConsumptionRecord, ...]:
        """回傳所有已消費之預測批次審計紀錄。"""
        ...

    @property
    def quarantined_records(self) -> Tuple[QuarantinedPredictionRecord, ...]:
        """回傳所有被隔離之未驗證預測批次紀錄。"""
        ...

    def has_batch_for_decision_date(self, decision_date: dt.date) -> bool:
        """檢查指定決策日 (T 日) 是否存在可讀取之預測批次。"""
        ...

    def peek_entry(
        self,
        decision_date: dt.date,
        decision_cutoff_time: dt.datetime,
    ) -> Optional[PredictionFeedEntry]:
        """唯讀檢視指定決策日於截止時點可用之 `PredictionFeedEntry`（不標記為已消費）。"""
        ...

    def consume_batch(
        self,
        decision_date: dt.date,
        decision_cutoff_time: dt.datetime,
        *,
        fail_on_missing: bool = False,
    ) -> Tuple[Optional[PredictionBatch], Optional[PredictionConsumptionRecord]]:
        """於指定決策日 (T 日) 與盤後決策截止時點消費對應之 `PredictionBatch`。

        保證：
        1. 嚴格驗證 `data_cutoff_time <= generated_at <= available_at <= decision_cutoff_time`。
        2. 驗證 `batch.as_of_date == calendar.next_trading_day(decision_date)`（即 T+1 目標交易日）。
        3. 驗證模型 `trained_cutoff_date` 與 `WalkForwardWindowSpec` 未發生前視或 Embargo 違規。
        4. 同一 `decision_date` 或 `batch_id` 僅能消費一次，重複呼叫立即拋出 `DuplicatePredictionConsumptionError`。
        """
        ...


class InMemoryPredictionFeed:
    """記憶體內建之 PIT 安全 AI 預測供給器實作 (`InMemoryPredictionFeed`)."""

    def __init__(
        self,
        calendar: TradingCalendar,
        provenance: DataProvenance,
        *,
        unverified_policy: UnverifiedPredictionPolicy = UnverifiedPredictionPolicy.REJECT,
        require_trained_cutoff_date: bool = False,
        default_embargo_days: int = 0,
        allow_synthetic: bool = True,
    ) -> None:
        if not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須為 TradingCalendar 實例，收到 {type(calendar).__name__}")
        if not isinstance(provenance, DataProvenance):
            raise PredictionProvenanceError(
                f"provenance 必須為 DataProvenance 實例，收到 {type(provenance).__name__}"
            )
        if not isinstance(unverified_policy, UnverifiedPredictionPolicy):
            raise PredictionFeedError(f"不支援的 unverified_policy: {unverified_policy!r}")
        if (
            isinstance(default_embargo_days, bool)
            or not isinstance(default_embargo_days, int)
            or default_embargo_days < 0
        ):
            raise EmbargoViolationError(f"default_embargo_days 必須為非負整數，收到 {default_embargo_days!r}")

        if not allow_synthetic and provenance.is_synthetic:
            raise PredictionProvenanceError(
                "此 PredictionFeed 設定為禁止合成預測 (allow_synthetic=False)，但傳入之 provenance.is_synthetic=True",
                source=provenance.source,
                dataset_version=provenance.dataset_version,
                is_synthetic=provenance.is_synthetic,
            )

        self.calendar: TradingCalendar = calendar
        self._provenance: DataProvenance = provenance
        self.unverified_policy: UnverifiedPredictionPolicy = unverified_policy
        self.require_trained_cutoff_date: bool = bool(require_trained_cutoff_date)
        self.default_embargo_days: int = default_embargo_days
        self.allow_synthetic: bool = bool(allow_synthetic)

        self._entries_by_decision_date: Dict[dt.date, PredictionFeedEntry] = {}
        self._registered_batch_ids: Set[str] = set()
        self._consumed_decision_dates: Set[dt.date] = set()
        self._consumed_batch_ids: Set[str] = set()
        self._consumed_records: List[PredictionConsumptionRecord] = []
        self._quarantined_records: List[QuarantinedPredictionRecord] = []

    @property
    def provenance(self) -> DataProvenance:
        return self._provenance

    @property
    def is_synthetic(self) -> bool:
        return self._provenance.is_synthetic

    @property
    def consumed_records(self) -> Tuple[PredictionConsumptionRecord, ...]:
        return tuple(self._consumed_records)

    @property
    def quarantined_records(self) -> Tuple[QuarantinedPredictionRecord, ...]:
        return tuple(self._quarantined_records)

    @property
    def registered_decision_dates(self) -> Tuple[dt.date, ...]:
        return tuple(sorted(self._entries_by_decision_date.keys()))

    def has_batch_for_decision_date(self, decision_date: dt.date) -> bool:
        d_date = to_taipei_date(decision_date, "decision_date", allow_datetime=False)
        return d_date in self._entries_by_decision_date

    def register_batch(
        self,
        batch: PredictionBatch,
        *,
        available_at: Optional[dt.datetime] = None,
        decision_date: Optional[dt.date] = None,
        window_spec: Optional[WalkForwardWindowSpec] = None,
        provenance: Optional[DataProvenance] = None,
    ) -> Optional[PredictionFeedEntry]:
        """註冊單一 `PredictionBatch` 進入供給器，並於註冊階段執行結構、日曆、防偽與時序驗證。"""
        if not isinstance(batch, PredictionBatch):
            raise PredictionFeedError(f"batch 必須為 PredictionBatch 實例，收到 {type(batch).__name__}")

        if batch.batch_id in self._registered_batch_ids:
            raise DuplicatePredictionConsumptionError(
                f"PredictionBatch {batch.batch_id!r} 已註冊過，嚴禁重複註冊同一 batch_id",
                context={"batch_id": batch.batch_id},
            )

        entry_prov = provenance or self._provenance
        if not isinstance(entry_prov, DataProvenance):
            raise PredictionProvenanceError("provenance 必須為 DataProvenance 實例")

        # 1. 防偽與合成標記一致性檢查
        model_type_upper = batch.metadata.model_type.upper()
        batch_looks_mock_or_syn = (
            batch.metadata.is_mock
            or ("MOCK" in model_type_upper)
            or ("SYNTHETIC" in model_type_upper)
        )
        if batch_looks_mock_or_syn and not entry_prov.is_synthetic:
            raise PredictionProvenanceError(
                f"預測批次 {batch.batch_id} 之模型 ({batch.metadata.model_name}, type={batch.metadata.model_type}, "
                f"is_mock={batch.metadata.is_mock}) 為 Mock 或合成模型，嚴禁將 DataProvenance.is_synthetic 標記為 False",
                source=entry_prov.source,
                dataset_version=entry_prov.dataset_version,
                is_synthetic=entry_prov.is_synthetic,
                context={"batch_id": batch.batch_id, "model_type": batch.metadata.model_type},
            )
        if not self.allow_synthetic and (entry_prov.is_synthetic or batch.metadata.is_mock):
            raise PredictionProvenanceError(
                f"此 PredictionFeed 禁止使用合成或 Mock 預測，但批次 {batch.batch_id} 為合成/Mock 預測",
                source=entry_prov.source,
                dataset_version=entry_prov.dataset_version,
                is_synthetic=True,
                context={"batch_id": batch.batch_id},
            )

        # 2. 時間戳時區標準化與基本因果檢查
        cutoff_dt = ensure_taipei_datetime(batch.data_cutoff_time, "batch.data_cutoff_time")
        gen_dt = ensure_taipei_datetime(batch.generated_at, "batch.generated_at")
        if gen_dt < cutoff_dt:
            raise PITViolationError(
                f"預測批次 {batch.batch_id} 之 generated_at ({gen_dt}) 早於 data_cutoff_time ({cutoff_dt})",
                event_time=cutoff_dt,
                published_at=gen_dt,
                context={"batch_id": batch.batch_id},
            )

        inferred_decision_date = cutoff_dt.date()
        if decision_date is not None:
            explicit_d_date = to_taipei_date(decision_date, "decision_date", allow_datetime=False)
            if explicit_d_date != inferred_decision_date:
                raise PITViolationError(
                    f"預測批次 {batch.batch_id} 指定之 decision_date ({explicit_d_date}) "
                    f"與 data_cutoff_time 所屬台北日期 ({inferred_decision_date}) 不一致",
                    trade_date=explicit_d_date,
                    event_time=cutoff_dt,
                    context={"batch_id": batch.batch_id},
                )
            d_date = explicit_d_date
        else:
            d_date = inferred_decision_date

        if d_date in self._entries_by_decision_date:
            existing = self._entries_by_decision_date[d_date]
            raise DuplicatePredictionConsumptionError(
                f"決策日 {d_date} 已註冊過預測批次 ({existing.batch_id})，嚴禁同一決策日註冊多個批次",
                context={"decision_date": d_date.isoformat(), "existing_batch_id": existing.batch_id},
            )

        # 3. 驗證決策日 T 與目標執行日 T+1 (batch.as_of_date) 之交易日曆關係
        if not self.calendar.is_trading_day(d_date):
            raise BacktestCalendarError(
                f"預測批次 {batch.batch_id} 之決策日 {d_date} 非有效台股交易日",
                context={"batch_id": batch.batch_id, "decision_date": d_date.isoformat()},
            )

        target_trade_date = to_taipei_date(batch.as_of_date, "batch.as_of_date", allow_datetime=False)
        if not self.calendar.is_trading_day(target_trade_date):
            raise BacktestCalendarError(
                f"預測批次 {batch.batch_id} 之目標交易日 batch.as_of_date ({target_trade_date}) 非有效台股交易日",
                context={"batch_id": batch.batch_id, "target_trade_date": target_trade_date.isoformat()},
            )

        expected_next_day = self.calendar.next_trading_day(d_date)
        if target_trade_date != expected_next_day:
            if target_trade_date <= d_date:
                raise LookaheadBiasError(
                    f"預測批次 {batch.batch_id} 時序顛倒或同日回溯: 決策日為 {d_date}，"
                    f"但目標交易日 batch.as_of_date 為 {target_trade_date} (必須為下一交易日 {expected_next_day})",
                    trade_date=target_trade_date,
                    cutoff_time=cutoff_dt,
                    context={
                        "batch_id": batch.batch_id,
                        "decision_date": d_date.isoformat(),
                        "target_trade_date": target_trade_date.isoformat(),
                        "expected_next_trading_day": expected_next_day.isoformat(),
                    },
                )
            raise PITViolationError(
                f"預測批次 {batch.batch_id} 之目標交易日 ({target_trade_date}) 不等於決策日 ({d_date}) "
                f"之下一交易日 ({expected_next_day})",
                trade_date=target_trade_date,
                event_time=cutoff_dt,
                context={
                    "batch_id": batch.batch_id,
                    "decision_date": d_date.isoformat(),
                    "target_trade_date": target_trade_date.isoformat(),
                    "expected_next_trading_day": expected_next_day.isoformat(),
                },
            )

        # 4. 驗證 available_at 與未驗證預測處理政策
        warnings: List[str] = []
        effective_avail_dt: Optional[dt.datetime] = None
        if available_at is not None:
            effective_avail_dt = ensure_taipei_datetime(available_at, "available_at")
            if effective_avail_dt < gen_dt:
                raise PITViolationError(
                    f"預測批次 {batch.batch_id} 之 available_at ({effective_avail_dt}) "
                    f"不得早於 generated_at ({gen_dt})",
                    trade_date=d_date,
                    event_time=cutoff_dt,
                    published_at=gen_dt,
                    available_at=effective_avail_dt,
                    context={"batch_id": batch.batch_id},
                )
        else:
            if self.unverified_policy == UnverifiedPredictionPolicy.REJECT:
                raise PITViolationError(
                    f"預測批次 {batch.batch_id} (decision_date={d_date}) 缺少 available_at 時間戳，"
                    f"依 UnverifiedPredictionPolicy.REJECT 拒絕通過正式 PIT 驗證",
                    trade_date=d_date,
                    event_time=cutoff_dt,
                    published_at=gen_dt,
                    available_at=None,
                    context={"batch_id": batch.batch_id, "policy": self.unverified_policy.value},
                )
            if self.unverified_policy == UnverifiedPredictionPolicy.QUARANTINE:
                q_rec = QuarantinedPredictionRecord(
                    batch_id=batch.batch_id,
                    decision_date=d_date,
                    target_trade_date=target_trade_date,
                    reason="缺少 available_at 時間戳，依 QUARANTINE 政策隔離且不納入回測消費",
                    quarantined_at=gen_dt,
                )
                self._quarantined_records.append(q_rec)
                self._registered_batch_ids.add(batch.batch_id)
                return None

            # SYNTHETIC_SANDBOX_FALLBACK
            if not entry_prov.is_synthetic:
                raise PITViolationError(
                    f"僅允許合成測試資料 (is_synthetic=True) 使用 SYNTHETIC_SANDBOX_FALLBACK，"
                    f"批次 {batch.batch_id} 缺少 available_at",
                    trade_date=d_date,
                    event_time=cutoff_dt,
                    published_at=gen_dt,
                    context={"batch_id": batch.batch_id},
                )
            effective_avail_dt = gen_dt
            warnings.append(
                f"SYNTHETIC_SANDBOX_FALLBACK: 批次 {batch.batch_id} 未提供 available_at，於合成沙盒回退使用 generated_at ({gen_dt.isoformat()})"
            )

        # 5. 驗證個別 StockPrediction 時間戳與版本一致性
        for pred in batch.predictions:
            pred_cutoff = ensure_taipei_datetime(pred.data_cutoff_time, f"prediction[{pred.symbol}].data_cutoff_time")
            pred_time = ensure_taipei_datetime(pred.prediction_time, f"prediction[{pred.symbol}].prediction_time")
            if pred_cutoff > cutoff_dt:
                raise LookaheadBiasError(
                    f"標的 {pred.symbol} 之預測 data_cutoff_time ({pred_cutoff}) 晚於批次截止時間 ({cutoff_dt})",
                    symbol=pred.symbol,
                    trade_date=d_date,
                    available_at=pred_cutoff,
                    cutoff_time=cutoff_dt,
                    context={"batch_id": batch.batch_id},
                )
            if pred_time > gen_dt:
                raise LookaheadBiasError(
                    f"標的 {pred.symbol} 之 prediction_time ({pred_time}) 晚於批次 generated_at ({gen_dt})",
                    symbol=pred.symbol,
                    trade_date=d_date,
                    available_at=pred_time,
                    cutoff_time=gen_dt,
                    context={"batch_id": batch.batch_id},
                )

        # 6. 驗證 Walk-Forward 視窗、trained_cutoff_date 與 Embargo 隔離規則
        trained_cutoff = batch.metadata.trained_cutoff_date
        if self.require_trained_cutoff_date and trained_cutoff is None:
            raise EmbargoViolationError(
                f"預測批次 {batch.batch_id} 缺少 metadata.trained_cutoff_date，無法驗證訓練集截止日是否洩漏未來資訊",
                context={"batch_id": batch.batch_id, "decision_date": d_date.isoformat()},
            )

        if window_spec is not None:
            if not isinstance(window_spec, WalkForwardWindowSpec):
                raise PredictionFeedError("window_spec 必須為 WalkForwardWindowSpec 實例")
            window_spec.validate_with_calendar(self.calendar)

            if not (window_spec.test_start_date <= target_trade_date <= window_spec.test_end_date):
                raise EmbargoViolationError(
                    f"預測批次 {batch.batch_id} 之目標交易日 ({target_trade_date}) 不在 Walk-Forward 視窗 "
                    f"{window_spec.window_id} 之測試期間 [{window_spec.test_start_date}, {window_spec.test_end_date}] 內",
                    context={
                        "batch_id": batch.batch_id,
                        "window_id": window_spec.window_id,
                        "target_trade_date": target_trade_date.isoformat(),
                        "test_start_date": window_spec.test_start_date.isoformat(),
                        "test_end_date": window_spec.test_end_date.isoformat(),
                    },
                )
            if trained_cutoff is not None and trained_cutoff > window_spec.train_end_date:
                raise EmbargoViolationError(
                    f"模型訓練集越界洩漏: 批次 {batch.batch_id} 之 trained_cutoff_date ({trained_cutoff}) "
                    f"晚於視窗 {window_spec.window_id} 之 train_end_date ({window_spec.train_end_date})",
                    context={
                        "batch_id": batch.batch_id,
                        "window_id": window_spec.window_id,
                        "trained_cutoff_date": trained_cutoff.isoformat(),
                        "train_end_date": window_spec.train_end_date.isoformat(),
                    },
                )

        if trained_cutoff is not None:
            if trained_cutoff > d_date:
                raise LookaheadBiasError(
                    f"模型訓練集包含未來資料: 批次 {batch.batch_id} 之 trained_cutoff_date ({trained_cutoff}) "
                    f"晚於決策日 ({d_date})",
                    trade_date=d_date,
                    context={
                        "batch_id": batch.batch_id,
                        "trained_cutoff_date": trained_cutoff.isoformat(),
                        "decision_date": d_date.isoformat(),
                    },
                )
            active_embargo = window_spec.embargo_days if window_spec is not None else self.default_embargo_days
            if active_embargo > 0:
                if not self.calendar.is_trading_day(trained_cutoff):
                    raise BacktestCalendarError(
                        f"trained_cutoff_date ({trained_cutoff}) 非有效交易日，無法計算 Embargo 交易日距離",
                        context={"batch_id": batch.batch_id, "trained_cutoff_date": trained_cutoff.isoformat()},
                    )
                days_diff = self.calendar.trading_days_between(trained_cutoff, d_date)
                if days_diff < active_embargo:
                    raise EmbargoViolationError(
                        f"預測批次 {batch.batch_id} 違反 Embargo 隔離要求: trained_cutoff_date ({trained_cutoff}) "
                        f"至決策日 ({d_date}) 僅間隔 {days_diff} 個交易日，少於要求之 embargo_days={active_embargo}",
                        context={
                            "batch_id": batch.batch_id,
                            "trained_cutoff_date": trained_cutoff.isoformat(),
                            "decision_date": d_date.isoformat(),
                            "embargo_days": active_embargo,
                            "actual_trading_days": days_diff,
                        },
                    )

        entry = PredictionFeedEntry(
            batch=batch,
            decision_date=d_date,
            target_trade_date=target_trade_date,
            data_cutoff_time=cutoff_dt,
            generated_at=gen_dt,
            available_at=effective_avail_dt,
            provenance=entry_prov,
            window_spec=window_spec,
            warnings=tuple(warnings),
        )
        self._entries_by_decision_date[d_date] = entry
        self._registered_batch_ids.add(batch.batch_id)
        return entry

    def _validate_entry_at_cutoff(
        self,
        entry: PredictionFeedEntry,
        decision_date: dt.date,
        decision_cutoff_time: dt.datetime,
    ) -> dt.datetime:
        """驗證在 `decision_cutoff_time` 時點讀取 `entry` 是否完全滿足 PIT 與無前視規則。"""
        cutoff_dt = ensure_taipei_datetime(decision_cutoff_time, "decision_cutoff_time")
        if cutoff_dt.date() != decision_date:
            raise LookaheadBiasError(
                f"決策截止時點日期 ({cutoff_dt.date()}) 與請求之決策日 ({decision_date}) 不一致",
                trade_date=decision_date,
                cutoff_time=cutoff_dt,
            )

        if entry.data_cutoff_time > cutoff_dt:
            raise LookaheadBiasError(
                f"前視偏差攔截: 預測批次 {entry.batch_id} 之 data_cutoff_time ({entry.data_cutoff_time}) "
                f"晚於當前決策截止時點 ({cutoff_dt})",
                trade_date=decision_date,
                available_at=entry.data_cutoff_time,
                cutoff_time=cutoff_dt,
                context={"batch_id": entry.batch_id},
            )

        if entry.generated_at > cutoff_dt:
            raise LookaheadBiasError(
                f"前視偏差攔截: 預測批次 {entry.batch_id} 之 generated_at ({entry.generated_at}) "
                f"晚於當前決策截止時點 ({cutoff_dt})",
                trade_date=decision_date,
                available_at=entry.generated_at,
                cutoff_time=cutoff_dt,
                context={"batch_id": entry.batch_id},
            )

        if entry.available_at is None:
            raise PITViolationError(
                f"預測批次 {entry.batch_id} 缺少 available_at，禁止在回測中被消費",
                trade_date=decision_date,
                cutoff_time=cutoff_dt,
                context={"batch_id": entry.batch_id},
            )

        if entry.available_at > cutoff_dt:
            raise LookaheadBiasError(
                f"前視偏差攔截: 預測批次 {entry.batch_id} 之 available_at ({entry.available_at}) "
                f"晚於當前決策截止時點 ({cutoff_dt})，此時點尚未取得該預測",
                trade_date=decision_date,
                available_at=entry.available_at,
                cutoff_time=cutoff_dt,
                context={"batch_id": entry.batch_id},
            )

        return cutoff_dt

    def peek_entry(
        self,
        decision_date: dt.date,
        decision_cutoff_time: dt.datetime,
    ) -> Optional[PredictionFeedEntry]:
        """唯讀檢視指定決策日之預測項目（執行 PIT 檢查但不標記為已消費）。"""
        d_date = to_taipei_date(decision_date, "decision_date", allow_datetime=False)
        entry = self._entries_by_decision_date.get(d_date)
        if entry is None:
            return None
        self._validate_entry_at_cutoff(entry, d_date, decision_cutoff_time)
        return entry

    def consume_batch(
        self,
        decision_date: dt.date,
        decision_cutoff_time: dt.datetime,
        *,
        fail_on_missing: bool = False,
    ) -> Tuple[Optional[PredictionBatch], Optional[PredictionConsumptionRecord]]:
        """於指定決策日 (T 日) 與決策時點消費 `PredictionBatch`，強制保證單次消費與零前視偏差。"""
        d_date = to_taipei_date(decision_date, "decision_date", allow_datetime=False)
        cutoff_dt = ensure_taipei_datetime(decision_cutoff_time, "decision_cutoff_time")

        if d_date in self._consumed_decision_dates:
            raise DuplicatePredictionConsumptionError(
                f"決策日 {d_date} 之預測批次已被消費過，嚴禁重複消費同一決策日之預測",
                context={"decision_date": d_date.isoformat()},
            )

        entry = self._entries_by_decision_date.get(d_date)
        if entry is None:
            if fail_on_missing:
                raise PredictionBatchUnavailableError(
                    f"決策日 {d_date} 缺少可用之 PredictionBatch (fail_on_missing=True)",
                    trade_date=d_date,
                    missing_fields=("prediction_batch",),
                    context={"decision_date": d_date.isoformat(), "cutoff_time": cutoff_dt.isoformat()},
                )
            return None, None

        if entry.batch_id in self._consumed_batch_ids:
            raise DuplicatePredictionConsumptionError(
                f"預測批次 {entry.batch_id} 已被消費過，嚴禁重複消費",
                context={"batch_id": entry.batch_id, "decision_date": d_date.isoformat()},
            )

        validated_cutoff = self._validate_entry_at_cutoff(entry, d_date, cutoff_dt)
        assert entry.available_at is not None

        self._consumed_decision_dates.add(d_date)
        self._consumed_batch_ids.add(entry.batch_id)

        record = PredictionConsumptionRecord(
            batch_id=entry.batch_id,
            decision_date=entry.decision_date,
            target_trade_date=entry.target_trade_date,
            decision_cutoff_time=validated_cutoff,
            data_cutoff_time=entry.data_cutoff_time,
            generated_at=entry.generated_at,
            available_at=entry.available_at,
            model_name=entry.metadata.model_name,
            model_version=entry.model_version,
            feature_version=entry.feature_version,
            trained_cutoff_date=entry.trained_cutoff_date,
            is_mock=entry.metadata.is_mock,
            is_synthetic=entry.provenance.is_synthetic,
            window_id=entry.window_spec.window_id if entry.window_spec is not None else None,
            prediction_count=len(entry.batch.predictions),
            warnings=entry.warnings,
        )
        self._consumed_records.append(record)
        return entry.batch, record
