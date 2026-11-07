"""台股歷史回測 FIFO 持倉批次帳本模組 (lot_ledger.py).

核心原則：
1. 解決第三階段 B 獨立審查問題 A-1：為回測提供精確之分批進場追蹤 (`PositionLot`) 與已平倉配對紀錄 (`ClosedLotTrade`)，而不修改既有 `PortfolioLedger` 核心實作。
2. 嚴格遵守「僅實際成交紀錄 (`FillRecord`) 可建立或扣減持倉」原則；未成交、已取消、已拒絕或過期之委託單絕不建立批次。
3. 同一標的支援多個進場批次，部分賣出或平倉時強制依 FIFO（先進先出）順序扣減最早批次。
4. 嚴格禁止負持倉、負批次股數、超賣（Oversell，不自動建立空頭部位）與重複 `fill_id` 入帳。
5. 交易成本（買進手續費、賣出手續費、賣出證交稅）採精確 `Decimal` 分攤與尾差守恆機制：
   - 買進手續費計入該批次之 `initial_total_cost` (`notional + commission`)，部分賣出時按股數比例扣減成本基準，最後一筆平倉吸收尾差，確保整批平倉總成本 100% 等於原始買進總支出。
   - 賣出手續費與證交稅於賣出時自 `exit_gross_proceeds` 扣除，若單筆賣單跨越設定多個 FIFO 批次，按平倉股數比例分攤並由最後一批吸收尾差，絕不重複扣費亦不漏扣。
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Deque, Dict, List, Mapping, Optional, Tuple

from ..broker.ledger import PortfolioLedger
from ..broker.order import FillRecord, PaperOrder
from ..contracts.signal import SignalAction
from .calendar import TAIPEI_TZ, TradingCalendar, ensure_taipei_datetime, to_taipei_date
from .exceptions import (
    BacktestAccountingError,
    BacktestCalendarError,
    DuplicateFillError,
    InvalidFillRecordError,
    OversellPositionError,
)

_PLAIN_SYMBOL_RE = re.compile(r"^[0-9A-Z]{4,6}$")
_CENT = Decimal("0.01")
_FOUR_PLACES = Decimal("0.0001")
_ZERO = Decimal("0.00")


def _normalize_plain_symbol(symbol: object) -> str:
    """驗證並標準化台股代號為不含 .TW/.TWO 後綴之 4~6 碼代號（與 PortfolioSnapshot 相容）。"""
    if not isinstance(symbol, str) or not symbol.strip():
        raise InvalidFillRecordError("symbol 必須為非空字串", context={"symbol": symbol})
    clean = symbol.strip().split(".")[0].strip()
    if not _PLAIN_SYMBOL_RE.match(clean):
        raise InvalidFillRecordError(
            f"無效的台股代號格式: {symbol!r}，必須為 4~6 碼英數字代號",
            context={"symbol": symbol},
        )
    return clean


def _require_non_negative_decimal(value: object, field_name: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidFillRecordError(
            f"{field_name} 必須為有限之 Decimal，收到 {value!r}",
            context={"field_name": field_name, "value": repr(value)},
        )
    if allow_zero and value < _ZERO:
        raise InvalidFillRecordError(
            f"{field_name} 不得為負數，收到 {value}",
            context={"field_name": field_name, "value": str(value)},
        )
    if not allow_zero and value <= _ZERO:
        raise InvalidFillRecordError(
            f"{field_name} 必須嚴格大於 0，收到 {value}",
            context={"field_name": field_name, "value": str(value)},
        )
    return value


@dataclass(frozen=True)
class LotFillEvent:
    """相容於既有 `FillRecord` 之最小擴充封裝介面。

    說明：既有 `src/tw_quant/broker/order.py` 的 `FillRecord` 包含
    `fill_id`, `order_id`, `signal_id`, `symbol`, `fill_quantity`, `fill_price`,
    `notional`, `commission`, `tax`, `slippage`, `timestamp`，但未內嵌買賣方向 `action` 與 `strategy_id`。
    本結構提供明確綁定，呼叫端亦可直接呼叫 `BacktestLotLedger.record_buy_fill`、`record_sell_fill`
    或 `record_order_fill(fill, order)`。
    """

    fill: FillRecord
    action: SignalAction
    strategy_id: str = "default"


@dataclass(frozen=True)
class PositionLot:
    """單一買進成交批次紀錄（不可變快照）。"""

    lot_id: str
    symbol: str
    entry_date: dt.date
    entry_timestamp: dt.datetime
    fill_id: str
    order_id: str
    signal_id: str
    strategy_id: str
    initial_quantity: int
    remaining_quantity: int
    entry_price: Decimal
    initial_notional: Decimal
    entry_commission: Decimal
    initial_total_cost: Decimal
    remaining_cost_basis: Decimal
    remaining_entry_commission: Decimal

    def __post_init__(self) -> None:
        clean_sym = _normalize_plain_symbol(self.symbol)
        object.__setattr__(self, "symbol", clean_sym)

        if not isinstance(self.lot_id, str) or not self.lot_id.strip():
            raise InvalidFillRecordError("lot_id 必須為非空字串")
        if not isinstance(self.fill_id, str) or not self.fill_id.strip():
            raise InvalidFillRecordError("fill_id 必須為非空字串")
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise InvalidFillRecordError("order_id 必須為非空字串")
        if not isinstance(self.signal_id, str) or not self.signal_id.strip():
            raise InvalidFillRecordError("signal_id 必須為非空字串")
        if not isinstance(self.strategy_id, str) or not self.strategy_id.strip():
            raise InvalidFillRecordError("strategy_id 必須為非空字串")

        e_date = to_taipei_date(self.entry_date, "entry_date", allow_datetime=False)
        e_ts = ensure_taipei_datetime(self.entry_timestamp, "entry_timestamp")
        if e_ts.date() != e_date:
            raise InvalidFillRecordError(
                f"PositionLot 之 entry_date ({e_date}) 與 entry_timestamp 日期 ({e_ts.date()}) 不一致",
                context={"entry_date": e_date.isoformat(), "entry_timestamp": e_ts.isoformat()},
            )
        object.__setattr__(self, "entry_date", e_date)
        object.__setattr__(self, "entry_timestamp", e_ts)

        if isinstance(self.initial_quantity, bool) or not isinstance(self.initial_quantity, int) or self.initial_quantity <= 0:
            raise InvalidFillRecordError(
                f"initial_quantity 必須為 > 0 之整數，收到 {self.initial_quantity!r}"
            )
        if (
            isinstance(self.remaining_quantity, bool)
            or not isinstance(self.remaining_quantity, int)
            or self.remaining_quantity < 0
            or self.remaining_quantity > self.initial_quantity
        ):
            raise InvalidFillRecordError(
                f"remaining_quantity ({self.remaining_quantity}) 必須介於 0 與 initial_quantity ({self.initial_quantity}) 之間"
            )

        _require_non_negative_decimal(self.entry_price, "entry_price", allow_zero=False)
        _require_non_negative_decimal(self.initial_notional, "initial_notional", allow_zero=False)
        _require_non_negative_decimal(self.entry_commission, "entry_commission", allow_zero=True)
        _require_non_negative_decimal(self.initial_total_cost, "initial_total_cost", allow_zero=False)
        _require_non_negative_decimal(self.remaining_cost_basis, "remaining_cost_basis", allow_zero=True)
        _require_non_negative_decimal(self.remaining_entry_commission, "remaining_entry_commission", allow_zero=True)

        if self.initial_total_cost != self.initial_notional + self.entry_commission:
            raise InvalidFillRecordError(
                f"initial_total_cost ({self.initial_total_cost}) 必須等於 initial_notional ({self.initial_notional}) + entry_commission ({self.entry_commission})"
            )
        if self.remaining_quantity == 0 and self.remaining_cost_basis != _ZERO:
            raise InvalidFillRecordError("當 remaining_quantity 為 0 時，remaining_cost_basis 必須為 0.00")

    @property
    def is_open(self) -> bool:
        """該批次是否仍有未平倉股數。"""
        return self.remaining_quantity > 0

    @property
    def unit_cost_with_fee(self) -> Decimal:
        """目前剩餘股數之含手續費單位成本（四捨五入至小數點後 4 位）。"""
        if self.remaining_quantity == 0:
            return Decimal("0.0000")
        return (self.remaining_cost_basis / Decimal(self.remaining_quantity)).quantize(
            _FOUR_PLACES, rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class ClosedLotTrade:
    """FIFO 批次平倉（含部分平倉）之配對損益紀錄。"""

    trade_id: str
    lot_id: str
    symbol: str
    strategy_id: str
    entry_fill_id: str
    exit_fill_id: str
    entry_order_id: str
    exit_order_id: str
    entry_date: dt.date
    exit_date: dt.date
    entry_timestamp: dt.datetime
    exit_timestamp: dt.datetime
    holding_calendar_days: int
    holding_trading_days: Optional[int]
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    entry_cost_basis: Decimal            # 分攤之買進總成本（含買進手續費）
    allocated_entry_commission: Decimal  # 其中包含之買進手續費分攤額
    exit_gross_proceeds: Decimal         # 分攤之賣出總價金（未扣費稅）
    allocated_exit_commission: Decimal   # 分攤之賣出手續費
    allocated_exit_tax: Decimal          # 分攤之賣出證交稅
    exit_net_proceeds: Decimal           # 賣出淨回收金額 = exit_gross_proceeds - exit_commission - exit_tax
    realized_pnl: Decimal                # 已實現淨損益 = exit_net_proceeds - entry_cost_basis


class BacktestLotLedger:
    """台股歷史回測 FIFO 持倉批次追蹤帳本。

    與既有 `PortfolioLedger` 平行運作，不修改 `PortfolioLedger` 原始碼，
    並提供 `get_oldest_entry_dates()` 直接供 `PortfolioSnapshot.from_broker()` 使用。
    """

    def __init__(self, calendar: Optional[TradingCalendar] = None) -> None:
        if calendar is not None and not isinstance(calendar, TradingCalendar):
            raise BacktestCalendarError(f"calendar 必須為 TradingCalendar 實例，收到 {type(calendar).__name__}")
        self.calendar: Optional[TradingCalendar] = calendar

        # symbol -> FIFO 佇列 (僅保存 remaining_quantity > 0 之活躍批次)
        self._open_lots: Dict[str, Deque[PositionLot]] = {}
        # 所有曾建立之批次最新狀態 (lot_id -> PositionLot)
        self._all_lots_by_id: Dict[str, PositionLot] = {}
        # 已處理過之 fill_id 集合 (防重複入帳)
        self._processed_fill_ids: set[str] = set()
        # 各標的最後一筆成交時間戳 (防時間倒流亂序入帳)
        self._last_fill_timestamp_by_symbol: Dict[str, dt.datetime] = {}
        # 所有已平倉配對紀錄
        self._closed_trades: List[ClosedLotTrade] = []
        self._trade_seq: int = 0

        # 累計統計
        self._cumulative_realized_pnl: Decimal = _ZERO
        self._total_buy_commission: Decimal = _ZERO
        self._total_sell_commission: Decimal = _ZERO
        self._total_sell_tax: Decimal = _ZERO

    # ── 內部驗證輔助 ───────────────────────────────────────────

    def _validate_fill_common(self, fill: FillRecord, action: SignalAction) -> Tuple[str, dt.datetime, dt.date]:
        if not isinstance(fill, FillRecord):
            raise InvalidFillRecordError(
                f"僅允許實際成交之 FillRecord 入帳，收到 {type(fill).__name__}",
                context={"received_type": type(fill).__name__},
            )
        for id_name in ("fill_id", "order_id", "signal_id"):
            val = getattr(fill, id_name, None)
            if not isinstance(val, str) or not val.strip():
                raise InvalidFillRecordError(f"FillRecord.{id_name} 必須為非空字串")

        if fill.fill_id in self._processed_fill_ids:
            raise DuplicateFillError(
                f"重複的成交識別碼 fill_id={fill.fill_id!r}，嚴禁重複入帳",
                context={"fill_id": fill.fill_id, "symbol": fill.symbol},
            )

        clean_sym = _normalize_plain_symbol(fill.symbol)

        if isinstance(fill.fill_quantity, bool) or not isinstance(fill.fill_quantity, int) or fill.fill_quantity <= 0:
            raise InvalidFillRecordError(
                f"成交股數 fill_quantity 必須為 > 0 之整數，收到 {fill.fill_quantity!r}",
                context={"fill_id": fill.fill_id, "fill_quantity": fill.fill_quantity},
            )

        _require_non_negative_decimal(fill.fill_price, "fill_price", allow_zero=False)
        _require_non_negative_decimal(fill.notional, "notional", allow_zero=False)
        _require_non_negative_decimal(fill.commission, "commission", allow_zero=True)
        _require_non_negative_decimal(fill.tax, "tax", allow_zero=True)
        _require_non_negative_decimal(fill.slippage, "slippage", allow_zero=True)

        expected_notional = (fill.fill_price * Decimal(fill.fill_quantity)).quantize(_CENT, rounding=ROUND_HALF_UP)
        if fill.notional != expected_notional:
            raise InvalidFillRecordError(
                f"成交金額 notional ({fill.notional}) 與 fill_price ({fill.fill_price}) * fill_quantity ({fill.fill_quantity}) = {expected_notional} 不一致",
                context={
                    "fill_id": fill.fill_id,
                    "notional": str(fill.notional),
                    "expected_notional": str(expected_notional),
                },
            )

        if action == SignalAction.BUY and fill.tax != _ZERO:
            raise InvalidFillRecordError(
                f"買進成交 (BUY) 之證交稅 tax 必須為 0.00，收到 {fill.tax}",
                context={"fill_id": fill.fill_id, "tax": str(fill.tax)},
            )

        try:
            ts_taipei = ensure_taipei_datetime(fill.timestamp, "fill.timestamp")
        except BacktestCalendarError as exc:
            raise InvalidFillRecordError(
                f"成交時間戳 fill.timestamp 無效或缺少時區: {exc.message}",
                context={"fill_id": fill.fill_id},
            ) from exc

        fill_date = ts_taipei.date()
        if self.calendar is not None:
            if not self.calendar.is_trading_day(fill_date):
                raise BacktestCalendarError(
                    f"成交日期 {fill_date} 在交易日曆中屬於非交易日 (NON_TRADING_DAY)，拒絕入帳",
                    context={"fill_id": fill.fill_id, "symbol": clean_sym, "fill_date": fill_date.isoformat()},
                )

        last_ts = self._last_fill_timestamp_by_symbol.get(clean_sym)
        if last_ts is not None and ts_taipei < last_ts:
            raise InvalidFillRecordError(
                f"標的 {clean_sym} 之成交時間戳 ({ts_taipei}) 早於前一筆已入帳成交時間 ({last_ts})，違反時序單調性",
                context={
                    "fill_id": fill.fill_id,
                    "symbol": clean_sym,
                    "timestamp": ts_taipei.isoformat(),
                    "last_timestamp": last_ts.isoformat(),
                },
            )

        return clean_sym, ts_taipei, fill_date

    # ── 成交入帳 API ───────────────────────────────────────────

    def record_buy_fill(
        self,
        fill: FillRecord,
        *,
        strategy_id: str = "default",
    ) -> PositionLot:
        """記錄一筆買進成交 (`BUY`)，建立新的 `PositionLot` 並排入該標的之 FIFO 佇列尾端。"""
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise InvalidFillRecordError("strategy_id 必須為非空字串")

        clean_sym, ts_taipei, fill_date = self._validate_fill_common(fill, SignalAction.BUY)
        total_cost = fill.notional + fill.commission
        lot_id = f"LOT-{fill.fill_id}"

        lot = PositionLot(
            lot_id=lot_id,
            symbol=clean_sym,
            entry_date=fill_date,
            entry_timestamp=ts_taipei,
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            signal_id=fill.signal_id,
            strategy_id=strategy_id.strip(),
            initial_quantity=fill.fill_quantity,
            remaining_quantity=fill.fill_quantity,
            entry_price=fill.fill_price,
            initial_notional=fill.notional,
            entry_commission=fill.commission,
            initial_total_cost=total_cost,
            remaining_cost_basis=total_cost,
            remaining_entry_commission=fill.commission,
        )

        queue = self._open_lots.setdefault(clean_sym, deque())
        queue.append(lot)
        self._all_lots_by_id[lot_id] = lot
        self._processed_fill_ids.add(fill.fill_id)
        self._last_fill_timestamp_by_symbol[clean_sym] = ts_taipei
        self._total_buy_commission += fill.commission
        return lot

    def record_sell_fill(
        self,
        fill: FillRecord,
        *,
        strategy_id: str = "default",
    ) -> Tuple[ClosedLotTrade, ...]:
        """記錄一筆賣出或平倉成交 (`SELL` / `CLOSE`)，依 FIFO 順序扣減最早之未平倉批次。

        若賣出股數超過目前持有之未平倉總股數，立即拋出 `OversellPositionError`，
        且保證原有批次狀態完全不變（原子性檢查，絕不產生負持倉或空頭部位）。
        """
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise InvalidFillRecordError("strategy_id 必須為非空字串")

        clean_sym, ts_taipei, fill_date = self._validate_fill_common(fill, SignalAction.SELL)
        current_qty = self.get_position_quantity(clean_sym)
        if fill.fill_quantity > current_qty:
            raise OversellPositionError(
                f"標的 {clean_sym} 超賣拒絕: 嘗試賣出 {fill.fill_quantity} 股，但目前未平倉總股數僅 {current_qty} 股（嚴禁自動建立空頭持倉）",
                context={
                    "fill_id": fill.fill_id,
                    "symbol": clean_sym,
                    "sell_quantity": fill.fill_quantity,
                    "available_quantity": current_qty,
                },
            )

        queue = self._open_lots.get(clean_sym)
        assert queue is not None and len(queue) > 0

        # 預先決定將被匹配之批次與股數清單，確認無任何時序違規後再執行狀態更新
        matched_plan: List[Tuple[PositionLot, int]] = []
        remaining_to_sell = fill.fill_quantity
        for lot in queue:
            if remaining_to_sell <= 0:
                break
            if ts_taipei < lot.entry_timestamp or fill_date < lot.entry_date:
                raise InvalidFillRecordError(
                    f"賣出成交時間 ({ts_taipei}) 早於批次 {lot.lot_id} 進場時間 ({lot.entry_timestamp})",
                    context={"fill_id": fill.fill_id, "lot_id": lot.lot_id},
                )
            take_qty = min(lot.remaining_quantity, remaining_to_sell)
            matched_plan.append((lot, take_qty))
            remaining_to_sell -= take_qty

        assert remaining_to_sell == 0

        # 執行 FIFO 扣減與費用分攤
        unallocated_notional = fill.notional
        unallocated_exit_comm = fill.commission
        unallocated_exit_tax = fill.tax
        total_sell_qty = Decimal(fill.fill_quantity)
        num_matched = len(matched_plan)
        closed_records: List[ClosedLotTrade] = []

        for idx, (lot, take_qty) in enumerate(matched_plan, start=1):
            is_last_match = idx == num_matched

            # 1. 計算該批次被扣減之進場成本基準與進場手續費
            if take_qty == lot.remaining_quantity:
                cost_basis_reduced = lot.remaining_cost_basis
                entry_comm_allocated = lot.remaining_entry_commission
            else:
                lot_ratio = Decimal(take_qty) / Decimal(lot.remaining_quantity)
                cost_basis_reduced = (lot.remaining_cost_basis * lot_ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
                entry_comm_allocated = (lot.remaining_entry_commission * lot_ratio).quantize(
                    _CENT, rounding=ROUND_HALF_UP
                )

            # 2. 計算該筆配對分攤之賣出價金、賣出手續費與證交稅（最後一筆吸收分攤尾差）
            if is_last_match:
                gross_proceeds = unallocated_notional
                exit_comm = unallocated_exit_comm
                exit_tax = unallocated_exit_tax
            else:
                sell_ratio = Decimal(take_qty) / total_sell_qty
                gross_proceeds = (fill.notional * sell_ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
                exit_comm = (fill.commission * sell_ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
                exit_tax = (fill.tax * sell_ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
                unallocated_notional -= gross_proceeds
                unallocated_exit_comm -= exit_comm
                unallocated_exit_tax -= exit_tax

            net_proceeds = gross_proceeds - exit_comm - exit_tax
            realized_pnl = net_proceeds - cost_basis_reduced

            # 3. 計算持有天數
            cal_days = (fill_date - lot.entry_date).days
            trad_days: Optional[int] = None
            if self.calendar is not None:
                trad_days = self.calendar.trading_days_between(lot.entry_date, fill_date)

            self._trade_seq += 1
            trade_record = ClosedLotTrade(
                trade_id=f"CLS-{self._trade_seq:08d}",
                lot_id=lot.lot_id,
                symbol=clean_sym,
                strategy_id=lot.strategy_id,
                entry_fill_id=lot.fill_id,
                exit_fill_id=fill.fill_id,
                entry_order_id=lot.order_id,
                exit_order_id=fill.order_id,
                entry_date=lot.entry_date,
                exit_date=fill_date,
                entry_timestamp=lot.entry_timestamp,
                exit_timestamp=ts_taipei,
                holding_calendar_days=cal_days,
                holding_trading_days=trad_days,
                quantity=take_qty,
                entry_price=lot.entry_price,
                exit_price=fill.fill_price,
                entry_cost_basis=cost_basis_reduced,
                allocated_entry_commission=entry_comm_allocated,
                exit_gross_proceeds=gross_proceeds,
                allocated_exit_commission=exit_comm,
                allocated_exit_tax=exit_tax,
                exit_net_proceeds=net_proceeds,
                realized_pnl=realized_pnl,
            )
            closed_records.append(trade_record)
            self._closed_trades.append(trade_record)
            self._cumulative_realized_pnl += realized_pnl

            # 4. 更新批次剩餘狀態
            new_rem_qty = lot.remaining_quantity - take_qty
            new_rem_cost = lot.remaining_cost_basis - cost_basis_reduced
            new_rem_comm = lot.remaining_entry_commission - entry_comm_allocated

            updated_lot = PositionLot(
                lot_id=lot.lot_id,
                symbol=lot.symbol,
                entry_date=lot.entry_date,
                entry_timestamp=lot.entry_timestamp,
                fill_id=lot.fill_id,
                order_id=lot.order_id,
                signal_id=lot.signal_id,
                strategy_id=lot.strategy_id,
                initial_quantity=lot.initial_quantity,
                remaining_quantity=new_rem_qty,
                entry_price=lot.entry_price,
                initial_notional=lot.initial_notional,
                entry_commission=lot.entry_commission,
                initial_total_cost=lot.initial_total_cost,
                remaining_cost_basis=new_rem_cost,
                remaining_entry_commission=new_rem_comm,
            )
            self._all_lots_by_id[lot.lot_id] = updated_lot

            # 更新佇列頭部
            queue.popleft()
            if new_rem_qty > 0:
                queue.appendleft(updated_lot)

        if not queue:
            del self._open_lots[clean_sym]

        self._processed_fill_ids.add(fill.fill_id)
        self._last_fill_timestamp_by_symbol[clean_sym] = ts_taipei
        self._total_sell_commission += fill.commission
        self._total_sell_tax += fill.tax
        return tuple(closed_records)

    def record_fill(
        self,
        fill: FillRecord,
        action: SignalAction | str,
        *,
        strategy_id: str = "default",
    ) -> PositionLot | Tuple[ClosedLotTrade, ...]:
        """依明確指定之買賣方向 (`BUY` / `SELL` / `CLOSE`) 記錄單筆 `FillRecord`。"""
        if isinstance(action, str):
            try:
                parsed_action = SignalAction(action.upper())
            except ValueError as exc:
                raise InvalidFillRecordError(f"不支援的交易動作 action={action!r}") from exc
        elif isinstance(action, SignalAction):
            parsed_action = action
        else:
            raise InvalidFillRecordError(f"action 必須為 SignalAction 或字串，收到 {type(action).__name__}")

        if parsed_action == SignalAction.BUY:
            return self.record_buy_fill(fill, strategy_id=strategy_id)
        if parsed_action in {SignalAction.SELL, SignalAction.CLOSE}:
            return self.record_sell_fill(fill, strategy_id=strategy_id)
        raise InvalidFillRecordError(f"未知的 SignalAction: {parsed_action!r}")

    def record_order_fill(
        self,
        fill: FillRecord,
        order: PaperOrder,
        *,
        strategy_id: str = "default",
    ) -> PositionLot | Tuple[ClosedLotTrade, ...]:
        """從 `PaperOrder` 與其產出之 `FillRecord` 同步入帳。

        強制驗證 `fill.order_id == order.order_id` 且該 `fill` 確實屬於已成交紀錄。
        """
        if not isinstance(order, PaperOrder):
            raise InvalidFillRecordError(
                f"order 必須為 PaperOrder 實例，收到 {type(order).__name__}"
            )
        if not isinstance(fill, FillRecord):
            raise InvalidFillRecordError(
                f"fill 必須為 FillRecord 實例，收到 {type(fill).__name__}"
            )
        if fill.order_id != order.order_id:
            raise InvalidFillRecordError(
                f"fill.order_id ({fill.order_id}) 與 order.order_id ({order.order_id}) 不匹配"
            )
        if _normalize_plain_symbol(fill.symbol) != _normalize_plain_symbol(order.symbol):
            raise InvalidFillRecordError(
                f"fill.symbol ({fill.symbol}) 與 order.symbol ({order.symbol}) 不匹配"
            )
        return self.record_fill(fill, order.action, strategy_id=strategy_id)

    # ── 持倉、批次、進場日與損益查詢 API ────────────────────────

    def get_position_quantity(self, symbol: str) -> int:
        """查詢指定股票目前之未平倉總股數。"""
        clean_sym = _normalize_plain_symbol(symbol)
        queue = self._open_lots.get(clean_sym)
        if not queue:
            return 0
        return sum(lot.remaining_quantity for lot in queue)

    def get_all_position_quantities(self) -> Dict[str, int]:
        """回傳所有目前持有股數 > 0 之 `{symbol: quantity}` 字典。"""
        return {
            sym: sum(lot.remaining_quantity for lot in queue)
            for sym, queue in self._open_lots.items()
            if queue
        }

    def get_position_cost_basis(self, symbol: str) -> Decimal:
        """查詢指定股票目前所有未平倉批次之剩餘總成本基準（含買進手續費）。"""
        clean_sym = _normalize_plain_symbol(symbol)
        queue = self._open_lots.get(clean_sym)
        if not queue:
            return _ZERO
        return sum((lot.remaining_cost_basis for lot in queue), _ZERO)

    def get_open_lots(self, symbol: Optional[str] = None) -> Tuple[PositionLot, ...]:
        """查詢目前未平倉之 `PositionLot` 序列（按 FIFO 先進先出順序排列）。"""
        if symbol is not None:
            clean_sym = _normalize_plain_symbol(symbol)
            queue = self._open_lots.get(clean_sym)
            return tuple(queue) if queue else ()

        all_open: List[PositionLot] = []
        for sym in sorted(self._open_lots.keys()):
            all_open.extend(self._open_lots[sym])
        return tuple(all_open)

    def get_oldest_entry_date(self, symbol: str) -> Optional[dt.date]:
        """取得指定股票目前最早一筆未平倉批次之進場日期（FIFO 佇列首筆）；若無持倉則回傳 None。"""
        clean_sym = _normalize_plain_symbol(symbol)
        queue = self._open_lots.get(clean_sym)
        if not queue:
            return None
        return queue[0].entry_date

    def get_oldest_entry_dates(self) -> Dict[str, dt.date]:
        """匯出所有目前有持倉標的之最早未平倉進場日期 `{symbol: oldest_entry_date}`。

        此字典可直接傳入 `PortfolioSnapshot.from_broker(..., entry_dates=lot_ledger.get_oldest_entry_dates())`。
        """
        return {
            sym: queue[0].entry_date
            for sym, queue in self._open_lots.items()
            if queue
        }

    def get_holding_days(
        self,
        symbol: str,
        as_of_date: dt.date,
        *,
        calendar: Optional[TradingCalendar] = None,
    ) -> Optional[int]:
        """查詢指定股票截至 `as_of_date` 的持有天數（以最早未平倉批次起算）。

        - 若提供 `calendar` 或帳本已綁定 `self.calendar`，回傳台股實際交易日數 `trading_days_between(entry_date, as_of_date)`。
        - 若未綁定交易日曆，則回傳日曆天數 `(as_of_date - entry_date).days`。
        - 若目前無該股票持倉，回傳 `None`。
        """
        oldest_entry = self.get_oldest_entry_date(symbol)
        if oldest_entry is None:
            return None
        target_date = to_taipei_date(as_of_date, "as_of_date", allow_datetime=False)
        if target_date < oldest_entry:
            raise BacktestAccountingError(
                f"查詢持有天數之基準日 as_of_date ({target_date}) 不得早於最早進場日 ({oldest_entry})",
                context={"symbol": symbol, "entry_date": oldest_entry.isoformat(), "as_of_date": target_date.isoformat()},
            )
        active_cal = calendar or self.calendar
        if active_cal is not None:
            return active_cal.trading_days_between(oldest_entry, target_date)
        return (target_date - oldest_entry).days

    def get_eligible_shares_for_ex_date(self, symbol: str, ex_date: dt.date) -> int:
        """計算在除息交易日 `ex_date` 開盤前享有配息權利之合格股數。

        規則：僅 `entry_date < ex_date`（即除息交易日前一日收盤已持有）之未平倉批次股數享有配息權利；
        於 `ex_date` 當日或之後新買進之批次不含息。
        """
        clean_sym = _normalize_plain_symbol(symbol)
        target_ex = to_taipei_date(ex_date, "ex_date", allow_datetime=False)
        queue = self._open_lots.get(clean_sym)
        if not queue:
            return 0
        return sum(lot.remaining_quantity for lot in queue if lot.entry_date < target_ex)

    def get_closed_trades(self, symbol: Optional[str] = None) -> Tuple[ClosedLotTrade, ...]:
        """取得所有已平倉（含部分平倉）之 `ClosedLotTrade` 歷史紀錄。"""
        if symbol is None:
            return tuple(self._closed_trades)
        clean_sym = _normalize_plain_symbol(symbol)
        return tuple(t for t in self._closed_trades if t.symbol == clean_sym)

    def get_realized_pnl(self, symbol: Optional[str] = None) -> Decimal:
        """取得累計已實現淨損益（已扣除買進手續費、賣出手續費與賣出證交稅）。"""
        if symbol is None:
            return self._cumulative_realized_pnl
        clean_sym = _normalize_plain_symbol(symbol)
        return sum((t.realized_pnl for t in self._closed_trades if t.symbol == clean_sym), _ZERO)

    def get_cost_summary(self) -> Dict[str, Decimal]:
        """取得累計交易成本統計（買進手續費、賣出手續費、賣出證交稅與總成本）。"""
        total_comm = self._total_buy_commission + self._total_sell_commission
        return {
            "buy_commission": self._total_buy_commission,
            "sell_commission": self._total_sell_commission,
            "total_commission": total_comm,
            "sell_tax": self._total_sell_tax,
            "total_transaction_cost": total_comm + self._total_sell_tax,
        }

    def assert_reconciled_with_portfolio_ledger(
        self,
        portfolio_ledger: PortfolioLedger,
        *,
        check_realized_pnl_when_flat: bool = True,
    ) -> None:
        """驗證 `BacktestLotLedger` 與 `PortfolioLedger` 之持倉股數與全平倉損益守恆。"""
        if not isinstance(portfolio_ledger, PortfolioLedger):
            raise BacktestAccountingError("portfolio_ledger 必須為 PortfolioLedger 實例")

        lot_positions = self.get_all_position_quantities()
        ledger_positions = {
            _normalize_plain_symbol(sym): pos.quantity
            for sym, pos in portfolio_ledger.positions.items()
            if pos.quantity > 0
        }

        if lot_positions != ledger_positions:
            raise BacktestAccountingError(
                f"持倉股數不一致: BacktestLotLedger={lot_positions} vs PortfolioLedger={ledger_positions}",
                context={"lot_positions": lot_positions, "ledger_positions": ledger_positions},
            )

        # 當所有部位均已平倉 (無未平倉批次) 時，FIFO 已實現總損益與加權平均法已實現總損益必然 100% 相等
        if check_realized_pnl_when_flat and not lot_positions:
            if self._cumulative_realized_pnl != portfolio_ledger.cumulative_realized_pnl:
                raise BacktestAccountingError(
                    f"全平倉後累計已實現損益不守恆: BacktestLotLedger ({self._cumulative_realized_pnl}) != "
                    f"PortfolioLedger ({portfolio_ledger.cumulative_realized_pnl})",
                    context={
                        "lot_realized_pnl": str(self._cumulative_realized_pnl),
                        "ledger_realized_pnl": str(portfolio_ledger.cumulative_realized_pnl),
                    },
                )
