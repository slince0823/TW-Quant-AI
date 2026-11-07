"""台股模擬委託與狀態機管理 (order.py).

核心原則：
1. 嚴格實作訂單生命週期狀態機，防止已終結訂單再次成交。
2. 完整追蹤每筆委託、部分成交紀錄、預留資金與預留持倉。
3. 採用 Decimal 精確計量金額。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from ..contracts.signal import SignalAction, SignalOrderType


class PaperOrderStatus(str, Enum):
    """委託單狀態枚舉。"""
    NEW = "NEW"                          # 新建立，尚未通過預留或驗證
    ACCEPTED = "ACCEPTED"                # 已通過驗證並預留款券，掛單等待撮合
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交，剩餘數量仍在等待中
    FILLED = "FILLED"                    # 全部成交，終態
    CANCELLED = "CANCELLED"              # 委託取消，終態
    REJECTED = "REJECTED"                # 委託被拒絕，終態
    EXPIRED = "EXPIRED"                  # 委託已逾期作廢，終態


# 終態集合：進入後不得再有任何狀態轉移或撮合成交
TERMINAL_STATUSES = {
    PaperOrderStatus.FILLED,
    PaperOrderStatus.CANCELLED,
    PaperOrderStatus.REJECTED,
    PaperOrderStatus.EXPIRED,
}


@dataclass(frozen=True)
class FillRecord:
    """單次撮合成交紀錄 (不可變 dataclass)."""
    fill_id: str
    order_id: str
    signal_id: str
    symbol: str
    fill_quantity: int
    fill_price: Decimal
    notional: Decimal
    commission: Decimal
    tax: Decimal
    slippage: Decimal
    timestamp: dt.datetime


@dataclass
class PaperOrder:
    """台股模擬委託單資料結構。"""

    order_id: str
    signal_id: str
    symbol: str
    action: SignalAction
    order_type: SignalOrderType
    order_quantity: int
    limit_price: Optional[Decimal]
    created_at: dt.datetime

    # 動態變更屬性
    filled_quantity: int = 0
    remaining_quantity: int = field(init=False)
    status: PaperOrderStatus = PaperOrderStatus.NEW
    rejection_reason: str = ""
    reserved_cash: Decimal = Decimal("0.00")
    reserved_shares: int = 0
    fills: List[FillRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.remaining_quantity = self.order_quantity
        if self.order_quantity <= 0:
            raise ValueError(f"order_quantity 必須是大於 0 的整數，收到 {self.order_quantity}")
        if self.limit_price is not None and (not isinstance(self.limit_price, Decimal) or self.limit_price <= Decimal("0")):
            raise ValueError(f"limit_price 必須為大於 0 的 Decimal，收到 {self.limit_price!r}")

    @property
    def is_active(self) -> bool:
        """是否處於可撮合或可取消之有效狀態。"""
        return self.status in {PaperOrderStatus.ACCEPTED, PaperOrderStatus.PARTIALLY_FILLED}

    @property
    def is_terminal(self) -> bool:
        """是否已進入終態。"""
        return self.status in TERMINAL_STATUSES

    def accept(self, reserved_cash: Decimal = Decimal("0.00"), reserved_shares: int = 0) -> None:
        """委託進入市場掛單 (NEW -> ACCEPTED)."""
        if self.status != PaperOrderStatus.NEW:
            raise ValueError(f"無法將狀態為 {self.status.value} 的訂單轉為 ACCEPTED")
        self.status = PaperOrderStatus.ACCEPTED
        self.reserved_cash = reserved_cash
        self.reserved_shares = reserved_shares

    def add_fill(self, fill: FillRecord) -> None:
        """添加一筆成交紀錄並推進狀態機。

        Raises:
            ValueError: 若訂單已處於終態或成交數量超過剩餘數量。
        """
        if self.is_terminal:
            raise ValueError(f"訂單 {self.order_id} 處於終態 {self.status.value}，嚴禁再次撮合成交")

        if fill.fill_quantity <= 0:
            raise ValueError(f"成交數量必須大於 0，收到 {fill.fill_quantity}")

        if fill.fill_quantity > self.remaining_quantity:
            raise ValueError(
                f"成交數量 ({fill.fill_quantity}) 超過剩餘委託量 ({self.remaining_quantity})"
            )

        self.fills.append(fill)
        self.filled_quantity += fill.fill_quantity
        self.remaining_quantity -= fill.fill_quantity

        if self.remaining_quantity == 0:
            self.status = PaperOrderStatus.FILLED
        else:
            self.status = PaperOrderStatus.PARTIALLY_FILLED

    def cancel(self, reason: str = "") -> None:
        """撤銷剩餘未成交委託。"""
        if self.is_terminal:
            raise ValueError(f"訂單 {self.order_id} 處於終態 {self.status.value}，無法撤銷")
        self.status = PaperOrderStatus.CANCELLED
        self.rejection_reason = reason or "委託手動或自動撤銷"

    def reject(self, reason: str) -> None:
        """拒絕委託。"""
        if self.is_terminal:
            raise ValueError(f"訂單 {self.order_id} 處於終態 {self.status.value}，無法重複拒絕")
        self.status = PaperOrderStatus.REJECTED
        self.rejection_reason = reason

    def expire(self, reason: str = "") -> None:
        """委託逾期或收盤作廢。"""
        if self.is_terminal:
            raise ValueError(f"訂單 {self.order_id} 處於終態 {self.status.value}，無法過期作廢")
        self.status = PaperOrderStatus.EXPIRED
        self.rejection_reason = reason or "委託已過期或收盤失效"


class OrderManager:
    """台股委託訂單生命週期管理器。"""

    def __init__(self) -> None:
        self._orders: Dict[str, PaperOrder] = {}
        self._signal_to_order: Dict[str, str] = {}
        self._order_seq: int = 0

    def generate_order_id(self) -> str:
        """生成唯一訂單識別碼。"""
        self._order_seq += 1
        return f"TW-ORD-{self._order_seq:08d}"

    def register_order(self, order: PaperOrder) -> None:
        """註冊新訂單。"""
        if order.order_id in self._orders:
            raise ValueError(f"訂單識別碼重複: {order.order_id}")
        self._orders[order.order_id] = order
        self._signal_to_order[order.signal_id] = order.order_id

    def get_order(self, order_id: str) -> Optional[PaperOrder]:
        """根據 order_id 查詢訂單。"""
        return self._orders.get(order_id)

    def get_order_by_signal(self, signal_id: str) -> Optional[PaperOrder]:
        """根據 signal_id 查詢對應訂單。"""
        oid = self._signal_to_order.get(signal_id)
        return self._orders.get(oid) if oid else None

    def get_open_orders(self) -> List[PaperOrder]:
        """取得所有目前仍為有效且待撮合的訂單。"""
        return [o for o in self._orders.values() if o.is_active]

    def get_all_orders(self) -> List[PaperOrder]:
        """取得所有歷史與當前訂單。"""
        return list(self._orders.values())
