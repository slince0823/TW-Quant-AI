"""台股模擬交易引擎配置模組 (config.py).

核心原則：
1. 所有金額、費率與比例一律採用 Decimal，杜絕浮點誤差。
2. 費率與法規規則完全可配置，不寫死於交易引擎。
3. 提供明確的四捨五入與取整模式配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP


@dataclass(frozen=True)
class TaiwanPaperConfig:
    """台股模擬交易引擎配置參數 (不可變 dataclass)."""

    # 1. 資金帳戶設定
    initial_cash: Decimal = Decimal("1000000.00")

    # 2. 券商手續費設定
    commission_rate: Decimal = Decimal("0.001425")       # 法定手續費率 0.1425%
    commission_discount: Decimal = Decimal("0.60")       # 券商電子下單折讓 (例如 6 折，亦可設為 0.28, 0.50 等)
    min_commission: Decimal = Decimal("20.00")           # 每筆委託最低手續費 (台幣 20 元)
    commission_rounding: str = ROUND_HALF_UP             # 手續費取整方式：四捨五入至整數位

    # 3. 證券交易稅設定 (賣出時由賣方負擔)
    stock_tax_rate: Decimal = Decimal("0.0030")          # 一般公司股票證交稅 0.3%
    etf_tax_rate: Decimal = Decimal("0.0010")            # ETF 指數股票型基金證交稅 0.1%
    day_trading_tax_rate: Decimal = Decimal("0.0015")   # 現股當沖優惠降稅 0.15%
    tax_rounding: str = ROUND_FLOOR                     # 證交稅取整方式：法規慣例採無條件捨去取整

    # 4. 價格與滑價設定
    slippage_rate: Decimal = Decimal("0.0005")           # 模擬市價單或突破撮合之滑價比例 (0.05%)
    price_limit_rate: Decimal = Decimal("0.10")          # 台股漲跌停限制比例 (10%)

    # 5. 流動性與撮合限制
    max_volume_participation: Decimal = Decimal("0.10")  # 單次撮合成交量上限 (佔當下市場成交量比例，例如 10%，超額部分成交)
    allow_odd_lots: bool = True                          # 是否允許零股交易 (未滿 1000 股)
    strict_cash_check: bool = True                       # 是否嚴格禁止超額融資 (現金不足立即拒絕)

    def __post_init__(self) -> None:
        """防禦性校驗所有配置參數之有效性。"""
        if not isinstance(self.initial_cash, Decimal) or self.initial_cash <= Decimal("0"):
            raise ValueError(f"initial_cash 必須為大於 0 的 Decimal，收到 {self.initial_cash!r}")

        if not isinstance(self.commission_rate, Decimal) or self.commission_rate < Decimal("0"):
            raise ValueError(f"commission_rate 必須為非負 Decimal，收到 {self.commission_rate!r}")

        if not isinstance(self.commission_discount, Decimal) or not (Decimal("0") <= self.commission_discount <= Decimal("1")):
            raise ValueError(f"commission_discount 必須介於 0 與 1 之間，收到 {self.commission_discount!r}")

        if not isinstance(self.min_commission, Decimal) or self.min_commission < Decimal("0"):
            raise ValueError(f"min_commission 必須為非負 Decimal，收到 {self.min_commission!r}")

        for name, rate in [
            ("stock_tax_rate", self.stock_tax_rate),
            ("etf_tax_rate", self.etf_tax_rate),
            ("day_trading_tax_rate", self.day_trading_tax_rate),
            ("slippage_rate", self.slippage_rate),
            ("price_limit_rate", self.price_limit_rate),
            ("max_volume_participation", self.max_volume_participation),
        ]:
            if not isinstance(rate, Decimal) or not (Decimal("0") <= rate <= Decimal("1")):
                raise ValueError(f"{name} 必須介於 0 與 1 之間的 Decimal，收到 {rate!r}")

        if not isinstance(self.allow_odd_lots, bool):
            raise TypeError("allow_odd_lots 必須為布林值")
