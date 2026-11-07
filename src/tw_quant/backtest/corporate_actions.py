"""台股歷史回測除權息與公司行動會計處理器模組 (corporate_actions.py).

核心原則：
1. 解決第三階段 B 獨立審查問題 A-2：將現金股利之「除息日 (ex_date)」與「發放日 (payable_date)」分離記帳。
2. 明確區分 `ex_date`、`record_date`（如適用）、`payable_date`、公告時間 (`announced_at`) 與策略可用時間 (`available_at`)。
3. 除息日 (`ex_date`) 依據除息前一日收盤已持有股數 (`entry_date < ex_date`) 計算股利權利，建立 `DividendReceivable`（應收股利），絕不直接增加可用現金 (`cash`)。
4. 發放日 (`payable_date`) 將對應之 `DividendReceivable` 轉入可用現金，且強制具備冪等單次入帳保護（嚴禁重複入帳）。
5. 嚴格防範發放日假設污染：
   - 缺少實際 `payable_date` 的真實歷史資料 (`is_synthetic=False`) 嚴禁默認第 28 個交易日為真實發放日，必須拋出 `HistoricalDataMissingError`。
   - 測試資料可指定模擬發放日，但必須明確標記 `is_synthetic_payable_date=True` 且 `provenance.is_synthetic=True`。
6. 股票股利 (`STOCK_DIVIDEND`)、股票分割 (`STOCK_SPLIT`)、反分割 (`REVERSE_SPLIT`) 與減資 (`CAPITAL_REDUCTION`) 於本階段明確定義為未支援行為，呼叫時立即拋出 `UnsupportedCorporateActionError`，嚴禁靜默略過或僅改股數而不調整成本與價格基準。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import re
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from ..broker.ledger import PortfolioLedger
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .data_provider import DataProvenance
from .exceptions import (
    BacktestCalendarError,
    CorporateActionError,
    CorporateActionOrderError,
    DataProvenanceError,
    DuplicateCorporateActionError,
    HistoricalDataMissingError,
    LookaheadBiasError,
    PITViolationError,
    UnsupportedCorporateActionError,
)
from .lot_ledger import BacktestLotLedger

_PLAIN_SYMBOL_RE = re.compile(r"^[0-9A-Z]{4,6}$")
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")
_ONE = Decimal("1")


class CorporateActionType(str, Enum):
    """公司行動類型列舉。"""

    CASH_DIVIDEND = "CASH_DIVIDEND"        # 現金股利（除息）- 本階段完整支援
    STOCK_DIVIDEND = "STOCK_DIVIDEND"      # 股票股利（除權配股）- 本階段明確攔截不靜默略過
    STOCK_SPLIT = "STOCK_SPLIT"            # 股票分割 - 本階段明確攔截不靜默略過
    REVERSE_SPLIT = "REVERSE_SPLIT"        # 股票反分割 - 本階段明確攔截不靜默略過
    CAPITAL_REDUCTION = "CAPITAL_REDUCTION"  # 減資換股 - 本階段明確攔截不靜默略過


_UNSUPPORTED_SHARE_ACTION_TYPES = {
    CorporateActionType.STOCK_DIVIDEND,
    CorporateActionType.STOCK_SPLIT,
    CorporateActionType.REVERSE_SPLIT,
    CorporateActionType.CAPITAL_REDUCTION,
}


class ReceivableStatus(str, Enum):
    """應收股利狀態列舉。"""

    PENDING = "PENDING"  # 已除息確認權利，等待發放日入帳（計入 NAV，不計入可用現金）
    PAID = "PAID"        # 已於發放日轉入可用現金（不再計入應收股利，避免重複計算）


def _normalize_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise CorporateActionError("symbol 必須為非空字串", context={"symbol": symbol})
    clean = symbol.strip().split(".")[0].strip()
    if not _PLAIN_SYMBOL_RE.match(clean):
        raise CorporateActionError(
            f"無效的台股代號格式: {symbol!r}",
            context={"symbol": symbol},
        )
    return clean


def _validate_decimal_field(
    value: object,
    field_name: str,
    *,
    allow_zero: bool = True,
    max_less_than_one: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise CorporateActionError(
            f"{field_name} 必須為有限之 Decimal，收到 {value!r}",
            context={"field_name": field_name, "value": repr(value)},
        )
    if allow_zero and value < _ZERO:
        raise CorporateActionError(
            f"{field_name} 不得小於 0，收到 {value}",
            context={"field_name": field_name, "value": str(value)},
        )
    if not allow_zero and value <= _ZERO:
        raise CorporateActionError(
            f"{field_name} 必須嚴格大於 0，收到 {value}",
            context={"field_name": field_name, "value": str(value)},
        )
    if max_less_than_one and value >= _ONE:
        raise CorporateActionError(
            f"{field_name} 必須小於 1，收到 {value}",
            context={"field_name": field_name, "value": str(value)},
        )
    return value


@dataclass(frozen=True)
class CorporateActionEvent:
    """公司行動事件定義（不可變）。

    時間與日期欄位：
    - `ex_date`: 除息／除權交易日。
    - `record_date`: 股利基準日／最後過戶基準日（選填，若提供必須 `>= ex_date`）。
    - `payable_date`: 現金股利實際發放日（真實歷史資料必填，不得為 None；嚴禁自動默認第 28 個交易日）。
    - `announced_at`: 公司行動公告時間（timezone-aware）。
    - `available_at`: 策略或回測系統可得知該公告之時間（timezone-aware，必須 `>= announced_at`）。
    - `is_synthetic_payable_date`: 標記 `payable_date` 是否為測試用之合成模擬假設日期（真實資料嚴禁設為 True）。
    """

    event_id: str
    symbol: str
    action_type: CorporateActionType
    ex_date: dt.date
    payable_date: Optional[dt.date]
    announced_at: dt.datetime
    available_at: dt.datetime
    provenance: DataProvenance
    cash_dividend_per_share: Optional[Decimal] = None
    record_date: Optional[dt.date] = None
    is_synthetic_payable_date: bool = False
    withholding_tax_rate: Decimal = Decimal("0.00")
    remittance_fee: Decimal = Decimal("0.00")
    currency_precision: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise CorporateActionError("event_id 必須為非空字串")

        clean_sym = _normalize_symbol(self.symbol)
        object.__setattr__(self, "symbol", clean_sym)

        if not isinstance(self.action_type, CorporateActionType):
            raise UnsupportedCorporateActionError(
                f"不支援或未知的公司行動類型: {self.action_type!r}",
                format_type=self.action_type,
                context={"event_id": self.event_id, "symbol": clean_sym},
            )

        # 針對股票股利、股票分割、反分割與減資：明確阻擋，拒絕靜默略過或僅改股數不調成本
        if self.action_type in _UNSUPPORTED_SHARE_ACTION_TYPES:
            raise UnsupportedCorporateActionError(
                f"公司行動類型 {self.action_type.value} 涉及股數變更、批次單位成本重算與歷史價格基準調整，"
                f"於第三階段 C-2.2 明確列為未支援行為，嚴禁靜默略過或僅修改股數",
                format_type=self.action_type.value,
                context={"event_id": self.event_id, "symbol": clean_sym, "action_type": self.action_type.value},
            )

        if self.provenance is None or not isinstance(self.provenance, DataProvenance):
            raise DataProvenanceError(
                f"公司行動事件 {self.event_id} 缺少有效之 DataProvenance 元資料"
            )

        if not isinstance(self.is_synthetic_payable_date, bool):
            raise CorporateActionError("is_synthetic_payable_date 必須為 bool")

        # 驗證 ex_date
        if self.ex_date is None:
            raise HistoricalDataMissingError(
                f"公司行動事件 {self.event_id} 缺少 ex_date",
                symbol=clean_sym,
                missing_fields=("ex_date",),
            )
        ex_d = to_taipei_date(self.ex_date, "ex_date", allow_datetime=False)
        object.__setattr__(self, "ex_date", ex_d)

        # 驗證 record_date (若有提供)
        rec_d: Optional[dt.date] = None
        if self.record_date is not None:
            rec_d = to_taipei_date(self.record_date, "record_date", allow_datetime=False)
            if rec_d < ex_d:
                raise CorporateActionOrderError(
                    f"公司行動事件 {self.event_id} 之基準日 record_date ({rec_d}) 不得早於除息日 ex_date ({ex_d})",
                    context={"event_id": self.event_id, "ex_date": ex_d.isoformat(), "record_date": rec_d.isoformat()},
                )
            object.__setattr__(self, "record_date", rec_d)

        # 驗證真實資料與合成發放日假設之隔離規則 (嚴禁將缺少發放日之真實資料默認為第 28 個交易日)
        if not self.provenance.is_synthetic and self.is_synthetic_payable_date:
            raise DataProvenanceError(
                f"真實歷史公司行動資料 (is_synthetic=False) 嚴禁標記使用合成模擬發放日假設 (is_synthetic_payable_date=True)",
                source=self.provenance.source,
                dataset_version=self.provenance.dataset_version,
                is_synthetic=False,
                context={"event_id": self.event_id, "symbol": clean_sym},
            )

        if self.payable_date is None:
            if not self.provenance.is_synthetic:
                raise HistoricalDataMissingError(
                    f"真實歷史除息資料 ({self.event_id}, 標的 {clean_sym}) 缺少實際發放日 payable_date；"
                    f"嚴禁自動默認第 28 個交易日或假裝為已知發放日",
                    symbol=clean_sym,
                    trade_date=ex_d,
                    missing_fields=("payable_date",),
                )
            raise HistoricalDataMissingError(
                f"合成除息測試資料 ({self.event_id}, 標的 {clean_sym}) 缺少 payable_date；"
                f"測試資料若需使用模擬發放日，必須明確傳入 payable_date 並標記 is_synthetic_payable_date=True",
                symbol=clean_sym,
                trade_date=ex_d,
                missing_fields=("payable_date",),
            )

        pay_d = to_taipei_date(self.payable_date, "payable_date", allow_datetime=False)
        if pay_d < ex_d:
            raise CorporateActionOrderError(
                f"公司行動事件 {self.event_id} 之發放日 payable_date ({pay_d}) 不得早於除息日 ex_date ({ex_d})",
                context={"event_id": self.event_id, "ex_date": ex_d.isoformat(), "payable_date": pay_d.isoformat()},
            )
        if rec_d is not None and pay_d < rec_d:
            raise CorporateActionOrderError(
                f"公司行動事件 {self.event_id} 之發放日 payable_date ({pay_d}) 不得早於基準日 record_date ({rec_d})",
                context={"event_id": self.event_id, "record_date": rec_d.isoformat(), "payable_date": pay_d.isoformat()},
            )
        object.__setattr__(self, "payable_date", pay_d)

        # 驗證公告時間與可用時間 (PIT 規則)
        if self.announced_at is None or self.available_at is None:
            raise PITViolationError(
                f"公司行動事件 {self.event_id} 必須提供明確之 announced_at 與 available_at 時間戳",
                symbol=clean_sym,
                trade_date=ex_d,
            )
        try:
            ann_dt = ensure_taipei_datetime(self.announced_at, "announced_at")
            avail_dt = ensure_taipei_datetime(self.available_at, "available_at")
        except BacktestCalendarError as exc:
            raise PITViolationError(
                f"公司行動事件 {self.event_id} 時間戳缺少時區或格式錯誤: {exc.message}",
                symbol=clean_sym,
                trade_date=ex_d,
            ) from exc

        if avail_dt < ann_dt:
            raise PITViolationError(
                f"公司行動事件 {self.event_id} 之 available_at ({avail_dt}) 不得早於 announced_at ({ann_dt})",
                symbol=clean_sym,
                trade_date=ex_d,
                published_at=ann_dt,
                available_at=avail_dt,
            )
        object.__setattr__(self, "announced_at", ann_dt)
        object.__setattr__(self, "available_at", avail_dt)

        # 驗證每股股利與稅費設定
        if self.cash_dividend_per_share is None:
            raise HistoricalDataMissingError(
                f"現金除息事件 {self.event_id} 缺少 cash_dividend_per_share",
                symbol=clean_sym,
                trade_date=ex_d,
                missing_fields=("cash_dividend_per_share",),
            )
        _validate_decimal_field(self.cash_dividend_per_share, "cash_dividend_per_share", allow_zero=False)
        _validate_decimal_field(
            self.withholding_tax_rate,
            "withholding_tax_rate",
            allow_zero=True,
            max_less_than_one=True,
        )
        _validate_decimal_field(self.remittance_fee, "remittance_fee", allow_zero=True)
        _validate_decimal_field(self.currency_precision, "currency_precision", allow_zero=False)
        if self.currency_precision not in {Decimal("1"), Decimal("0.01")}:
            raise CorporateActionError(
                f"currency_precision 僅支援 Decimal('1') 或 Decimal('0.01')，收到 {self.currency_precision!r}"
            )


@dataclass(frozen=True)
class DividendReceivable:
    """除息日產生之應收現金股利紀錄（不可變）。"""

    receivable_id: str
    event_id: str
    symbol: str
    ex_date: dt.date
    record_date: Optional[dt.date]
    payable_date: dt.date
    eligible_shares: int
    dividend_per_share: Decimal
    gross_dividend: Decimal
    tax_withheld: Decimal
    fee_charged: Decimal
    net_dividend: Decimal
    is_synthetic: bool
    is_synthetic_payable_date: bool
    status: ReceivableStatus
    created_at: dt.datetime
    paid_at: Optional[dt.datetime] = None

    def __post_init__(self) -> None:
        clean_sym = _normalize_symbol(self.symbol)
        object.__setattr__(self, "symbol", clean_sym)
        if isinstance(self.eligible_shares, bool) or not isinstance(self.eligible_shares, int) or self.eligible_shares <= 0:
            raise CorporateActionError(
                f"DividendReceivable.eligible_shares 必須為 > 0 之整數，收到 {self.eligible_shares!r}"
            )
        _validate_decimal_field(self.dividend_per_share, "dividend_per_share", allow_zero=False)
        _validate_decimal_field(self.gross_dividend, "gross_dividend", allow_zero=False)
        _validate_decimal_field(self.tax_withheld, "tax_withheld", allow_zero=True)
        _validate_decimal_field(self.fee_charged, "fee_charged", allow_zero=True)
        _validate_decimal_field(self.net_dividend, "net_dividend", allow_zero=True)
        if self.net_dividend != self.gross_dividend - self.tax_withheld - self.fee_charged:
            raise CorporateActionError(
                f"net_dividend ({self.net_dividend}) 必須等於 gross ({self.gross_dividend}) - tax ({self.tax_withheld}) - fee ({self.fee_charged})"
            )


@dataclass(frozen=True)
class AccountNAVBreakdown:
    """包含應收股利之帳戶資產淨值 (NAV) 結構化分解。

    核心守恆式：
    `total_nav = available_cash + reserved_cash + market_value + dividends_receivable`
    - 除息日 (`ex_date`)：產生 `PENDING` 之 `dividends_receivable`，`available_cash` 不變（應收股利不可當作可用現金下單）。
    - 發放日 (`payable_date`)：`dividends_receivable` 轉為 `PAID`（自應收股利歸零），同額加入 `available_cash`，絕不重複計入 `total_nav`。
    """

    as_of_date: dt.date
    available_cash: Decimal
    reserved_cash: Decimal
    market_value: Decimal
    dividends_receivable: Decimal
    total_nav: Decimal


class CorporateActionProcessor:
    """台股歷史回測除權息與公司行動處理器。"""

    def __init__(self, calendar: Optional[TradingCalendar] = None) -> None:
        if calendar is not None and not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須為 TradingCalendar 實例，收到 {type(calendar).__name__}")
        self.calendar: Optional[TradingCalendar] = calendar

        # 已註冊事件 (event_id -> CorporateActionEvent)
        self._events_by_id: Dict[str, CorporateActionEvent] = {}
        # 防止同一標的、同除息日、同類型重複註冊 ((symbol, action_type, ex_date) -> event_id)
        self._event_Semantic_keys: Dict[Tuple[str, CorporateActionType, dt.date], str] = {}
        # 已執行過除息處理之 event_id 集合
        self._processed_ex_event_ids: set[str] = set()
        # 應收股利帳本 (receivable_id -> DividendReceivable)
        self._receivables_by_id: Dict[str, DividendReceivable] = {}
        # event_id -> receivable_id 對應
        self._event_to_receivable_id: Dict[str, str] = {}
        # 單調處理日期水位線 (防止事件順序倒流)
        self._last_processed_date: Optional[dt.date] = None
        # 累計已發放現金股利總額
        self._total_paid_dividends: Decimal = _ZERO

    # ── 事件註冊與驗證 ─────────────────────────────────────────

    def register_event(self, event: CorporateActionEvent) -> None:
        """註冊單筆公司行動事件，阻擋重複事件與非交易日除息。"""
        if not isinstance(event, CorporateActionEvent):
            raise CorporateActionError(
                f"event 必須是 CorporateActionEvent 實例，收到 {type(event).__name__}"
            )
        if event.action_type in _UNSUPPORTED_SHARE_ACTION_TYPES:
            raise UnsupportedCorporateActionError(
                f"不支援的公司行動類型: {event.action_type.value}",
                format_type=event.action_type.value,
            )

        if event.event_id in self._events_by_id:
            raise DuplicateCorporateActionError(
                f"重複的公司行動 event_id={event.event_id!r}，拒絕重複註冊",
                context={"event_id": event.event_id, "symbol": event.symbol},
            )

        semantic_key = (event.symbol, event.action_type, event.ex_date)
        if semantic_key in self._event_Semantic_keys:
            existing_id = self._event_Semantic_keys[semantic_key]
            raise DuplicateCorporateActionError(
                f"標的 {event.symbol} 於除息日 {event.ex_date} 已存在相同類型之公司行動事件 ({existing_id})，拒絕重複輸入",
                context={
                    "event_id": event.event_id,
                    "existing_event_id": existing_id,
                    "symbol": event.symbol,
                    "ex_date": event.ex_date.isoformat(),
                },
            )

        if self.calendar is not None:
            if not self.calendar.is_trading_day(event.ex_date):
                raise BacktestCalendarError(
                    f"公司行動事件 {event.event_id} 之除息交易日 ex_date ({event.ex_date}) 非有效交易日",
                    context={"event_id": event.event_id, "ex_date": event.ex_date.isoformat()},
                )

        self._events_by_id[event.event_id] = event
        self._event_Semantic_keys[semantic_key] = event.event_id

    def register_events(self, events: Iterable[CorporateActionEvent]) -> None:
        """批次註冊公司行動事件。"""
        for ev in events:
            self.register_event(ev)

    # ── 除息日處理 (建立應收股利) ──────────────────────────────

    def process_ex_dividend_event(
        self,
        event_or_id: CorporateActionEvent | str,
        lot_ledger: BacktestLotLedger,
        *,
        as_of_time: dt.datetime,
    ) -> Optional[DividendReceivable]:
        """在除息交易日 (`ex_date`) 處理單一除息事件，依合格持股建立 `DividendReceivable`。

        規則：
        1. 若 `event` 尚未註冊，會先自動執行 `register_event`。
        2. 驗證 `as_of_time` 為台灣時區且日期等於 `event.ex_date`，並滿足 `event.available_at <= as_of_time`（PIT 防護）。
        3. 驗證事件處理順序不得早於 `_last_processed_date`。
        4. 從 `lot_ledger.get_eligible_shares_for_ex_date(symbol, ex_date)` 取得於 `ex_date` 前已買進持有之合格股數。
        5. 若合格股數 `> 0`，建立 `PENDING` 狀態之 `DividendReceivable`（不增加可用現金）；若合格股數 `== 0`，回傳 `None`。
        """
        if not isinstance(lot_ledger, BacktestLotLedger):
            raise CorporateActionError("lot_ledger 必須是 BacktestLotLedger 實例")

        ts_taipei = ensure_taipei_datetime(as_of_time, "as_of_time")
        current_date = ts_taipei.date()

        if isinstance(event_or_id, str):
            if event_or_id not in self._events_by_id:
                raise HistoricalDataMissingError(
                    f"找不到未註冊之公司行動事件 event_id={event_or_id!r}",
                    context={"event_id": event_or_id},
                )
            event = self._events_by_id[event_or_id]
        elif isinstance(event_or_id, CorporateActionEvent):
            if event_or_id.event_id not in self._events_by_id:
                self.register_event(event_or_id)
            event = self._events_by_id[event_or_id.event_id]
        else:
            raise CorporateActionError(
                f"event_or_id 必須為 CorporateActionEvent 或字串 ID，收到 {type(event_or_id).__name__}"
            )

        if event.event_id in self._processed_ex_event_ids:
            raise DuplicateCorporateActionError(
                f"除息事件 {event.event_id} 已處理過，嚴禁重複建立應收股利",
                context={"event_id": event.event_id, "symbol": event.symbol},
            )

        if self._last_processed_date is not None and current_date < self._last_processed_date:
            raise CorporateActionOrderError(
                f"公司行動處理日期倒流: 當前處理日期 ({current_date}) 早於已處理水位線 ({self._last_processed_date})",
                context={
                    "event_id": event.event_id,
                    "current_date": current_date.isoformat(),
                    "last_processed_date": self._last_processed_date.isoformat(),
                },
            )

        if current_date != event.ex_date:
            raise CorporateActionOrderError(
                f"除息事件 {event.event_id} 必須於 ex_date ({event.ex_date}) 當日處理，收到處理日期 {current_date}",
                context={"event_id": event.event_id, "ex_date": event.ex_date.isoformat(), "current_date": current_date.isoformat()},
            )

        if ts_taipei < event.available_at:
            raise LookaheadBiasError(
                f"除息事件 {event.event_id} 之資訊可用時間 ({event.available_at}) 晚於當前處理時間 ({ts_taipei})，拒絕提前使用",
                symbol=event.symbol,
                trade_date=event.ex_date,
                available_at=event.available_at,
                cutoff_time=ts_taipei,
            )

        eligible_shares = lot_ledger.get_eligible_shares_for_ex_date(event.symbol, event.ex_date)
        self._processed_ex_event_ids.add(event.event_id)
        self._last_processed_date = current_date

        if eligible_shares <= 0:
            return None

        assert event.cash_dividend_per_share is not None
        assert event.payable_date is not None

        prec = event.currency_precision
        gross = (Decimal(eligible_shares) * event.cash_dividend_per_share).quantize(prec, rounding=ROUND_HALF_UP)
        tax = (gross * event.withholding_tax_rate).quantize(prec, rounding=ROUND_HALF_UP)
        fee = event.remittance_fee.quantize(prec, rounding=ROUND_HALF_UP)
        net = gross - tax - fee
        if net < _ZERO:
            raise CorporateActionError(
                f"除息事件 {event.event_id} 計算之淨股利 net_dividend ({net}) 小於 0 "
                f"(gross={gross}, tax={tax}, fee={fee})",
                context={"event_id": event.event_id, "gross": str(gross), "tax": str(tax), "fee": str(fee)},
            )

        rec_id = f"DIV-REC-{event.event_id}"
        receivable = DividendReceivable(
            receivable_id=rec_id,
            event_id=event.event_id,
            symbol=event.symbol,
            ex_date=event.ex_date,
            record_date=event.record_date,
            payable_date=event.payable_date,
            eligible_shares=eligible_shares,
            dividend_per_share=event.cash_dividend_per_share,
            gross_dividend=gross,
            tax_withheld=tax,
            fee_charged=fee,
            net_dividend=net,
            is_synthetic=event.provenance.is_synthetic,
            is_synthetic_payable_date=event.is_synthetic_payable_date,
            status=ReceivableStatus.PENDING,
            created_at=ts_taipei,
            paid_at=None,
        )
        self._receivables_by_id[rec_id] = receivable
        self._event_to_receivable_id[event.event_id] = rec_id
        return receivable

    def process_due_ex_dividends(
        self,
        lot_ledger: BacktestLotLedger,
        *,
        as_of_time: dt.datetime,
    ) -> Tuple[DividendReceivable, ...]:
        """批次處理所有於 `as_of_time.date()` 當日除息 (`ex_date == current_date`) 且尚未處理之已註冊除息事件。"""
        ts_taipei = ensure_taipei_datetime(as_of_time, "as_of_time")
        current_date = ts_taipei.date()

        due_event_ids = [
            ev.event_id
            for ev in self._events_by_id.values()
            if ev.ex_date == current_date and ev.event_id not in self._processed_ex_event_ids
        ]
        created: List[DividendReceivable] = []
        for eid in sorted(due_event_ids):
            rec = self.process_ex_dividend_event(eid, lot_ledger, as_of_time=ts_taipei)
            if rec is not None:
                created.append(rec)
        return tuple(created)

    # ── 發放日處理 (應收股利轉入可用現金，單次入帳) ─────────────

    def settle_payable_dividend(
        self,
        receivable_or_event_id: str,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        *,
        as_of_time: dt.datetime,
    ) -> DividendReceivable:
        """於發放日 (`payable_date`) 將指定之應收股利結清並轉入 `portfolio_ledger.cash`。

        保護機制：
        - 若該筆應收股利狀態已為 `PAID`，立即拋出 `DuplicateCorporateActionError`，絕不重複入帳。
        - 若當前日期早於 `receivable.payable_date`，立即拋出 `CorporateActionOrderError`，禁止提前入帳。
        """
        ts_taipei = ensure_taipei_datetime(as_of_time, "as_of_time")
        current_date = ts_taipei.date()

        rec_id = self._event_to_receivable_id.get(receivable_or_event_id, receivable_or_event_id)
        if rec_id not in self._receivables_by_id:
            raise HistoricalDataMissingError(
                f"找不到對應之應收股利紀錄: {receivable_or_event_id!r}",
                context={"lookup_id": receivable_or_event_id},
            )

        rec = self._receivables_by_id[rec_id]
        if rec.status == ReceivableStatus.PAID:
            raise DuplicateCorporateActionError(
                f"應收股利 {rec.receivable_id} (事件 {rec.event_id}) 已於 {rec.paid_at} 發放入帳，嚴禁重複發放",
                context={"receivable_id": rec.receivable_id, "event_id": rec.event_id},
            )

        if self._last_processed_date is not None and current_date < self._last_processed_date:
            raise CorporateActionOrderError(
                f"發放日處理時間倒流: 當前日期 ({current_date}) 早於已處理水位線 ({self._last_processed_date})",
                context={"receivable_id": rec.receivable_id, "current_date": current_date.isoformat()},
            )

        if current_date < rec.payable_date:
            raise CorporateActionOrderError(
                f"應收股利 {rec.receivable_id} 尚未到達發放日 ({rec.payable_date})，不得於 {current_date} 提前轉入可用現金",
                context={
                    "receivable_id": rec.receivable_id,
                    "payable_date": rec.payable_date.isoformat(),
                    "current_date": current_date.isoformat(),
                },
            )

        if portfolio_ledger is not None:
            if not isinstance(portfolio_ledger, PortfolioLedger):
                raise CorporateActionError("portfolio_ledger 必須是 PortfolioLedger 實例")
            portfolio_ledger.cash += rec.net_dividend

        paid_rec = DividendReceivable(
            receivable_id=rec.receivable_id,
            event_id=rec.event_id,
            symbol=rec.symbol,
            ex_date=rec.ex_date,
            record_date=rec.record_date,
            payable_date=rec.payable_date,
            eligible_shares=rec.eligible_shares,
            dividend_per_share=rec.dividend_per_share,
            gross_dividend=rec.gross_dividend,
            tax_withheld=rec.tax_withheld,
            fee_charged=rec.fee_charged,
            net_dividend=rec.net_dividend,
            is_synthetic=rec.is_synthetic,
            is_synthetic_payable_date=rec.is_synthetic_payable_date,
            status=ReceivableStatus.PAID,
            created_at=rec.created_at,
            paid_at=ts_taipei,
        )
        self._receivables_by_id[rec_id] = paid_rec
        self._total_paid_dividends += paid_rec.net_dividend
        self._last_processed_date = current_date
        return paid_rec

    def process_due_payables(
        self,
        portfolio_ledger: Optional[PortfolioLedger] = None,
        *,
        as_of_time: dt.datetime,
    ) -> Tuple[DividendReceivable, ...]:
        """批次結算所有截至 `as_of_time.date()` 已到期 (`payable_date <= current_date`) 且仍為 `PENDING` 之應收股利。"""
        ts_taipei = ensure_taipei_datetime(as_of_time, "as_of_time")
        current_date = ts_taipei.date()

        if self._last_processed_date is not None and current_date < self._last_processed_date:
            raise CorporateActionOrderError(
                f"公司行動處理日期倒流: 當前日期 ({current_date}) 早於已處理水位線 ({self._last_processed_date})"
            )

        due_ids = [
            rec.receivable_id
            for rec in self._receivables_by_id.values()
            if rec.status == ReceivableStatus.PENDING and rec.payable_date <= current_date
        ]
        settled: List[DividendReceivable] = []
        for rid in sorted(due_ids):
            settled.append(
                self.settle_payable_dividend(rid, portfolio_ledger=portfolio_ledger, as_of_time=ts_taipei)
            )
        self._last_processed_date = current_date
        return tuple(settled)

    # ── 查詢與 NAV 守恆分解 API ─────────────────────────────────

    def get_pending_receivables(self, symbol: Optional[str] = None) -> Tuple[DividendReceivable, ...]:
        """取得所有目前尚未發放 (`PENDING`) 之應收股利紀錄。"""
        clean_sym = _normalize_symbol(symbol) if symbol is not None else None
        return tuple(
            rec
            for rec in self._receivables_by_id.values()
            if rec.status == ReceivableStatus.PENDING and (clean_sym is None or rec.symbol == clean_sym)
        )

    def get_paid_receivables(self, symbol: Optional[str] = None) -> Tuple[DividendReceivable, ...]:
        """取得所有已發放入帳 (`PAID`) 之股利紀錄。"""
        clean_sym = _normalize_symbol(symbol) if symbol is not None else None
        return tuple(
            rec
            for rec in self._receivables_by_id.values()
            if rec.status == ReceivableStatus.PAID and (clean_sym is None or rec.symbol == clean_sym)
        )

    def get_total_dividends_receivable(self, symbol: Optional[str] = None) -> Decimal:
        """計算目前尚未入帳 (`PENDING`) 之應收股利淨額總和。"""
        return sum((rec.net_dividend for rec in self.get_pending_receivables(symbol)), _ZERO)

    @property
    def total_paid_dividends(self) -> Decimal:
        """累計已轉入可用現金之股利淨額總和。"""
        return self._total_paid_dividends

    def compute_nav_breakdown(
        self,
        portfolio_ledger: PortfolioLedger,
        close_prices: Mapping[str, Decimal],
        as_of_date: dt.date,
    ) -> AccountNAVBreakdown:
        """計算包含應收股利之完整資產淨值分解 (`AccountNAVBreakdown`)。

        - `market_value` 完全依據傳入之當日實際 `close_prices` 與持倉股數計算（絕不假設股價必然僅下跌股利金額）。
        - `dividends_receivable` 僅包含狀態為 `PENDING` 之應收股利；一旦於發放日轉入 `portfolio_ledger.cash`，
          即不再計入 `dividends_receivable`，徹底杜絕應收股利與現金重複計入 NAV。
        """
        if not isinstance(portfolio_ledger, PortfolioLedger):
            raise CorporateActionError("portfolio_ledger 必須是 PortfolioLedger 實例")
        target_date = to_taipei_date(as_of_date, "as_of_date", allow_datetime=False)

        market_value = _ZERO
        for sym, pos in portfolio_ledger.positions.items():
            if pos.quantity > 0:
                clean_sym = _normalize_symbol(sym)
                px = close_prices.get(sym) if sym in close_prices else close_prices.get(clean_sym)
                if px is None:
                    raise HistoricalDataMissingError(
                        f"計算 {target_date} NAV 時缺少持倉標的 {clean_sym} 之收盤報價",
                        symbol=clean_sym,
                        trade_date=target_date,
                        missing_fields=("close_price",),
                    )
                _validate_decimal_field(px, f"close_prices[{clean_sym!r}]", allow_zero=False)
                market_value += (px * Decimal(pos.quantity)).quantize(_CENT, rounding=ROUND_HALF_UP)

        pending_div = self.get_total_dividends_receivable()
        total_nav = portfolio_ledger.cash + portfolio_ledger.reserved_cash + market_value + pending_div

        return AccountNAVBreakdown(
            as_of_date=target_date,
            available_cash=portfolio_ledger.cash,
            reserved_cash=portfolio_ledger.reserved_cash,
            market_value=market_value,
            dividends_receivable=pending_div,
            total_nav=total_nav,
        )
