"""統一交易訊號規格介面 (UnifiedTradeSignal).

核心合約：
作為 AI 選股模型、獨立風控引擎及台股 Paper Trading 之間的標準通訊契約。
遵循純標準函式庫設計原則，具備嚴格的型別校驗、時區完整性檢查、單位隔離與前視偏差防禦。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from enum import Enum
import math
import re
from typing import Any, Optional, Set

from .conversion import is_finite_positive_number, lots_to_shares


class SignalAction(str, Enum):
    """交易動作。"""
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"


class QuantityUnit(str, Enum):
    """委託數量單位。"""
    SHARES = "SHARES"  # 股 (系統內部唯一運算基準)
    LOTS = "LOTS"      # 張 (台股 1 張 = 1000 股)


class SignalOrderType(str, Enum):
    """委託類型。"""
    MARKET = "MARKET"  # 市價委託
    LIMIT = "LIMIT"    # 限價委託


class SignalMode(str, Enum):
    """交易模式 (第二階段嚴格限制僅允許 PAPER 模擬模式)."""
    PAPER = "PAPER"


# 台股代號正規表示式：4~6 碼數字或英文字母，可選加上 .TW 或 .TWO 後綴
_TW_SYMBOL_REGEX = re.compile(r"^[0-9A-Z]{4,6}(\.(TW|TWO))?$", re.IGNORECASE)

# 支援的合約欄位集合 (用於嚴格防範未知欄位注入)
_ALLOWED_FIELDS: Set[str] = {
    "signal_id",
    "symbol",
    "action",
    "quantity",
    "quantity_unit",
    "order_type",
    "limit_price",
    "created_at",
    "expires_at",
    "strategy_id",
    "model_version",
    "reason",
    "mode",
    "schema_version",
}

# 必要欄位集合
_REQUIRED_FIELDS: Set[str] = {
    "signal_id",
    "symbol",
    "action",
    "quantity",
    "quantity_unit",
    "order_type",
    "limit_price",
    "created_at",
    "expires_at",
    "strategy_id",
    "model_version",
    "reason",
}


@dataclass(frozen=True)
class UnifiedTradeSignal:
    """統一交易訊號資料結構 (不可變 dataclass)."""

    signal_id: str
    symbol: str
    action: SignalAction
    quantity: int | float
    quantity_unit: QuantityUnit
    order_type: SignalOrderType
    limit_price: Optional[float]
    created_at: dt.datetime
    expires_at: dt.datetime
    strategy_id: str
    model_version: str
    reason: str
    mode: SignalMode = SignalMode.PAPER
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        """建立時自動執行完整驗證。"""
        self.validate()

    def validate(self) -> None:
        """強制防禦性檢查所有欄位有效性。

        Raises:
            TypeError: 當欄位型別不符或包含 bool。
            ValueError: 當欄位數值、格式、時間邏輯不符規範。
        """
        # 1. signal_id 驗證
        if not isinstance(self.signal_id, str) or not self.signal_id.strip():
            raise ValueError("signal_id 必須是非空字串")

        # 2. symbol 股票代碼驗證
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol 必須是非空字串")
        clean_symbol = self.symbol.strip()
        if not _TW_SYMBOL_REGEX.match(clean_symbol):
            raise ValueError(
                f"無效的股票代碼格式: {self.symbol!r}。必須符合台股 4~6 碼規格 (如 '2330', '0050')"
            )

        # 3. action 交易動作驗證
        if not isinstance(self.action, SignalAction):
            raise TypeError(f"action 必須是 SignalAction 列舉值，收到 {self.action!r}")

        # 4. quantity 委託數量驗證 (嚴格排除 bool)
        if isinstance(self.quantity, bool):
            raise TypeError("quantity 不能是布林值 (bool)")
        if not is_finite_positive_number(self.quantity):
            raise ValueError(f"quantity 必須是有限正數，收到 {self.quantity!r}")

        # 5. quantity_unit 數量單位驗證
        if not isinstance(self.quantity_unit, QuantityUnit):
            raise TypeError(
                f"quantity_unit 必須是 QuantityUnit 列舉值，收到 {self.quantity_unit!r}"
            )

        # 6. order_type 委託類型驗證
        if not isinstance(self.order_type, SignalOrderType):
            raise TypeError(
                f"order_type 必須是 SignalOrderType 列舉值，收到 {self.order_type!r}"
            )

        # 7. limit_price 價格驗證
        if self.order_type == SignalOrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("限價單 (LIMIT) 必須提供 limit_price")
            if isinstance(self.limit_price, bool):
                raise TypeError("limit_price 不能是布林值 (bool)")
            if not is_finite_positive_number(self.limit_price):
                raise ValueError(
                    f"限價單價格必須是有限正數 (不得為 NaN/Inf/<=0)，收到 {self.limit_price!r}"
                )
        elif self.order_type == SignalOrderType.MARKET:
            if self.limit_price is not None:
                raise ValueError(
                    f"市價單 (MARKET) 的 limit_price 必須為 None，避免價格混淆，收到 {self.limit_price!r}"
                )

        # 8. created_at 與 expires_at 時間戳記驗證 (強制 timezone-aware)
        if not isinstance(self.created_at, dt.datetime):
            raise TypeError(f"created_at 必須是 datetime 物件，收到 {type(self.created_at).__name__}")
        if self.created_at.tzinfo is None or self.created_at.tzinfo.utcoffset(self.created_at) is None:
            raise ValueError("created_at 必須具有明確時區資訊 (timezone-aware)，禁止使用 naive datetime")

        if not isinstance(self.expires_at, dt.datetime):
            raise TypeError(f"expires_at 必須是 datetime 物件，收到 {type(self.expires_at).__name__}")
        if self.expires_at.tzinfo is None or self.expires_at.tzinfo.utcoffset(self.expires_at) is None:
            raise ValueError("expires_at 必須具有明確時區資訊 (timezone-aware)，禁止使用 naive datetime")

        # 時間先後邏輯 (先轉 UTC 等效基準比較)
        created_utc = self.created_at.astimezone(dt.timezone.utc)
        expires_utc = self.expires_at.astimezone(dt.timezone.utc)
        if expires_utc <= created_utc:
            raise ValueError(
                f"expires_at ({self.expires_at}) 必須嚴格晚於 created_at ({self.created_at})"
            )

        # 9. strategy_id, model_version, reason 字串檢查
        for field_name, value in [
            ("strategy_id", self.strategy_id),
            ("model_version", self.model_version),
            ("reason", self.reason),
        ]:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必須是非空字串")

        # 10. mode 模式檢查 (固定限制為 PAPER)
        if self.mode != SignalMode.PAPER:
            raise ValueError(f"mode 目前嚴格限制為 SignalMode.PAPER，收到 {self.mode!r}")

        # 11. schema_version 檢查
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version 必須是非空字串")

    def is_expired(self, as_of: Optional[dt.datetime] = None) -> bool:
        """判定訊號在指定時間點是否已經過期。

        Args:
            as_of: 比較的時間點，必須具備時區；若為 None 則預設為當前 UTC 時間。

        Returns:
            True 表示訊號已逾期作廢，False 表示仍在有效期內。
        """
        if as_of is None:
            as_of = dt.datetime.now(dt.timezone.utc)
        else:
            if not isinstance(as_of, dt.datetime):
                raise TypeError(f"as_of 必須是 datetime 物件，收到 {type(as_of).__name__}")
            if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
                raise ValueError("as_of 必須具有明確時區資訊 (timezone-aware)")

        as_of_utc = as_of.astimezone(dt.timezone.utc)
        expires_utc = self.expires_at.astimezone(dt.timezone.utc)
        return as_of_utc >= expires_utc

    def to_shares(self) -> UnifiedTradeSignal:
        """將訊號數量明確標準化為系統內部標準「股 (SHARES)」.

        若目前單位為 LOTS，呼叫 lots_to_shares 精準轉換為股數；
        若目前單位已是 SHARES，確保數量為整數。

        Returns:
            全新的 UnifiedTradeSignal 物件，其 quantity_unit 固定為 SHARES。
        """
        if self.quantity_unit == QuantityUnit.SHARES:
            shares = int(self.quantity)
        else:
            shares = lots_to_shares(self.quantity)

        # 透過 dataclass 複製並更新
        return UnifiedTradeSignal(
            signal_id=self.signal_id,
            symbol=self.symbol,
            action=self.action,
            quantity=shares,
            quantity_unit=QuantityUnit.SHARES,
            order_type=self.order_type,
            limit_price=self.limit_price,
            created_at=self.created_at,
            expires_at=self.expires_at,
            strategy_id=self.strategy_id,
            model_version=self.model_version,
            reason=self.reason,
            mode=self.mode,
            schema_version=self.schema_version,
        )

    def validate_execution_timing(
        self,
        execution_time: dt.datetime,
        market_close_time: Optional[dt.time] = dt.time(13, 30),
    ) -> None:
        """驗證成交撮合時間與訊號產生時間之因果關係，嚴格防禦 Lookahead Bias。

        核心規則：
        1. 成交時間不得早於訊號產生時間。
        2. 成交時間不得晚於訊號到期時間。
        3. 若訊號產生於 T 日盤後 (時間 >= market_close_time)，絕不允許以 T 日盤中或收盤時間成交。

        Raises:
            ValueError: 當時間存在因果矛盾或前視偏差時。
        """
        if not isinstance(execution_time, dt.datetime):
            raise TypeError("execution_time 必須是 datetime 物件")
        if execution_time.tzinfo is None or execution_time.tzinfo.utcoffset(execution_time) is None:
            raise ValueError("execution_time 必須具有明確時區資訊")

        exec_utc = execution_time.astimezone(dt.timezone.utc)
        created_utc = self.created_at.astimezone(dt.timezone.utc)
        expires_utc = self.expires_at.astimezone(dt.timezone.utc)

        # 先檢查 T 日盤後訊號是否嘗試撮合 T 日當日價格 (防範最典型的 Lookahead Bias 前視偏差)
        taipei_tz = dt.timezone(dt.timedelta(hours=8))
        created_loc = self.created_at.astimezone(taipei_tz)
        exec_loc = execution_time.astimezone(taipei_tz)

        if market_close_time is not None:
            # 判斷是否為盤後訊號 (如 13:30 之後產生)
            is_post_market = created_loc.time() >= market_close_time
            if is_post_market:
                # 若訊號是 T 日盤後產生，若嘗試以 T 日當日收盤或盤中時間成交，直接判定為 Lookahead Bias
                if exec_loc.date() == created_loc.date() and exec_loc.time() <= market_close_time:
                    raise ValueError(
                        f"不得將 T 日盤後產生的訊號 (產生於 {created_loc}) 直接以 T 日當日或收盤價成交 "
                        f"(撮合時間 {exec_loc})，此為典型 Lookahead Bias"
                    )

        if exec_utc < created_utc:
            raise ValueError(
                f"撮合成交時間 ({execution_time}) 早於訊號產生時間 ({self.created_at})，違反時序因果律"
            )

        if exec_utc >= expires_utc:
            raise ValueError(
                f"撮合成交時間 ({execution_time}) 已超過訊號到期時間 ({self.expires_at})，訊號已過期失效"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnifiedTradeSignal:
        """從字典反序列化建立 UnifiedTradeSignal，嚴格校驗未知欄位與型別。

        Raises:
            ValueError: 缺少必要欄位、包含未知欄位或欄位數值無效。
            TypeError: 欄位型別不符。
        """
        if not isinstance(data, dict):
            raise TypeError(f"輸入資料必須為字典 dict，收到 {type(data).__name__}")

        input_keys = set(data.keys())

        # 1. 檢查未知欄位
        unknown = input_keys - _ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"輸入資料包含未定義之未知欄位: {sorted(unknown)}")

        # 2. 檢查缺少欄位
        missing = _REQUIRED_FIELDS - input_keys
        if missing:
            raise ValueError(f"輸入資料缺少必要欄位: {sorted(missing)}")

        # 3. 解析 action 列舉
        action_val = data["action"]
        if isinstance(action_val, str):
            try:
                action = SignalAction(action_val.upper())
            except ValueError:
                raise ValueError(f"無效的 action 值: {action_val!r}")
        elif isinstance(action_val, SignalAction):
            action = action_val
        else:
            raise TypeError("action 必須是字串或 SignalAction 列舉")

        # 4. 解析 quantity_unit 列舉
        unit_val = data["quantity_unit"]
        if isinstance(unit_val, str):
            try:
                quantity_unit = QuantityUnit(unit_val.upper())
            except ValueError:
                raise ValueError(f"無效的 quantity_unit 值: {unit_val!r}")
        elif isinstance(unit_val, QuantityUnit):
            quantity_unit = unit_val
        else:
            raise TypeError("quantity_unit 必須是字串或 QuantityUnit 列舉")

        # 5. 解析 order_type 列舉
        type_val = data["order_type"]
        if isinstance(type_val, str):
            try:
                order_type = SignalOrderType(type_val.upper())
            except ValueError:
                raise ValueError(f"無效的 order_type 值: {type_val!r}")
        elif isinstance(type_val, SignalOrderType):
            order_type = type_val
        else:
            raise TypeError("order_type 必須是字串或 SignalOrderType 列舉")

        # 6. 解析 mode 列舉
        mode_val = data.get("mode", SignalMode.PAPER)
        if isinstance(mode_val, str):
            try:
                mode = SignalMode(mode_val.upper())
            except ValueError:
                raise ValueError(f"無效的 mode 值: {mode_val!r}，僅支援 PAPER")
        elif isinstance(mode_val, SignalMode):
            mode = mode_val
        else:
            raise TypeError("mode 必須是字串或 SignalMode 列舉")

        # 7. 解析 created_at
        created_val = data["created_at"]
        if isinstance(created_val, str):
            try:
                created_at = dt.datetime.fromisoformat(created_val)
            except Exception as e:
                raise ValueError(f"無法解析 created_at ISO 時間字串: {created_val!r}") from e
        elif isinstance(created_val, dt.datetime):
            created_at = created_val
        else:
            raise TypeError("created_at 必須是 ISO 格式字串或 datetime 物件")

        # 8. 解析 expires_at
        expires_val = data["expires_at"]
        if isinstance(expires_val, str):
            try:
                expires_at = dt.datetime.fromisoformat(expires_val)
            except Exception as e:
                raise ValueError(f"無法解析 expires_at ISO 時間字串: {expires_val!r}") from e
        elif isinstance(expires_val, dt.datetime):
            expires_at = expires_val
        else:
            raise TypeError("expires_at 必須是 ISO 格式字串或 datetime 物件")

        return cls(
            signal_id=str(data["signal_id"]),
            symbol=str(data["symbol"]),
            action=action,
            quantity=data["quantity"],
            quantity_unit=quantity_unit,
            order_type=order_type,
            limit_price=data.get("limit_price"),
            created_at=created_at,
            expires_at=expires_at,
            strategy_id=str(data["strategy_id"]),
            model_version=str(data["model_version"]),
            reason=str(data["reason"]),
            mode=mode,
            schema_version=str(data.get("schema_version", "1.0.0")),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化為乾淨的字典 (時間輸出為標準 ISO 8601 字串)."""
        d = asdict(self)
        d["action"] = self.action.value
        d["quantity_unit"] = self.quantity_unit.value
        d["order_type"] = self.order_type.value
        d["mode"] = self.mode.value
        d["created_at"] = self.created_at.isoformat()
        d["expires_at"] = self.expires_at.isoformat()
        return d
