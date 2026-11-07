"""獨立風控引擎配置模組 (config.py).

核心原則：
1. 嚴格採用 Decimal 計量所有金額與風險比例門檻。
2. 參數全面可自訂，不得將門檻數值硬編碼於規則引擎中。
3. 嚴格禁止未經授權的放空與無限制擴大曝險。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Set


@dataclass(frozen=True)
class RiskConfig:
    """獨立交易風控配置參數 (不可變 dataclass)."""

    version: str = "1.0.0"

    # 1. 單筆與單檔持倉限制
    max_order_amount: Decimal = Decimal("500000.00")         # 單筆委託上限 (台幣 50 萬元)
    max_single_position_ratio: Decimal = Decimal("0.10")    # 單一股票持倉佔總 NAV 上限 (10%)
    max_total_exposure_ratio: Decimal = Decimal("0.80")     # 股票總曝險佔總 NAV 上限 (80%)

    # 2. 現金保留與流動性限制
    min_cash_reserve_ratio: Decimal = Decimal("0.20")       # 交易後最低可用現金保留比例 (20%)

    # 3. 虧損控制與熔斷限制
    max_daily_loss_ratio: Decimal = Decimal("0.02")         # 每日最大虧損比例 (2.0% 觸發當日停機)
    max_drawdown_limit: Decimal = Decimal("0.15")           # 歷史淨值最大回撤熔斷門檻 (15%)

    # 4. 費用預估參數 (計算買入所需款項時使用)
    est_slippage_rate: Decimal = Decimal("0.0005")          # 預估滑價 (0.05%)
    est_commission_rate: Decimal = Decimal("0.001425")      # 預估手續費率 (0.1425%)
    est_commission_discount: Decimal = Decimal("0.60")      # 預估券商折讓 (6 折)

    # 5. 放空與特殊限制
    allow_short_selling: bool = False                       # 是否允許放空 (目前系統嚴格限制為 False)
    restricted_symbols: Set[str] = field(default_factory=set)  # 禁止交易標的黑名單

    def __post_init__(self) -> None:
        """防禦性校驗所有配置參數有效性。"""
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version 必須是非空字串")

        if not isinstance(self.max_order_amount, Decimal) or self.max_order_amount <= Decimal("0"):
            raise ValueError(f"max_order_amount 必須為大於 0 的 Decimal，收到 {self.max_order_amount!r}")

        for name, ratio in [
            ("max_single_position_ratio", self.max_single_position_ratio),
            ("max_total_exposure_ratio", self.max_total_exposure_ratio),
            ("min_cash_reserve_ratio", self.min_cash_reserve_ratio),
            ("max_daily_loss_ratio", self.max_daily_loss_ratio),
            ("max_drawdown_limit", self.max_drawdown_limit),
            ("est_slippage_rate", self.est_slippage_rate),
            ("est_commission_rate", self.est_commission_rate),
            ("est_commission_discount", self.est_commission_discount),
        ]:
            if not isinstance(ratio, Decimal) or not (Decimal("0") <= ratio <= Decimal("1")):
                raise ValueError(f"{name} 必須介於 0 與 1 之間的 Decimal，收到 {ratio!r}")

        if not isinstance(self.allow_short_selling, bool):
            raise TypeError("allow_short_selling 必須是布林值")
