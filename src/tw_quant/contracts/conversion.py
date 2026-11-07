"""台股交易單位明確轉換工具 (conversion.py).

核心原則：
1. 全系統內部計算一律以「股 (SHARES)」為唯一基準。
2. 外部輸入若為「張 (LOTS)」，必須經過明確的本轉換函式，嚴禁模糊或自動推導。
3. 嚴格拒絕 bool、NaN、Inf 及非有限正數。
"""

from __future__ import annotations

import math
from typing import Union


def is_finite_positive_number(val: object) -> bool:
    """檢查數值是否為有限正數，且排除布林值 (bool 為 int 子類別)."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    if not math.isfinite(val):
        return False
    return val > 0


def lots_to_shares(lots: Union[int, float]) -> int:
    """將台股「張 (LOTS)」明確轉換為「股 (SHARES)」.

    台股 1 張 = 1,000 股。
    
    Args:
        lots: 張數，必須為正整數或有限正小數（換算後必須為整數股）。

    Returns:
        整數股數。

    Raises:
        TypeError: 若輸入為布林值或非數值。
        ValueError: 若輸入小於等於 0、非有限數或換算後不是整數股。
    """
    if isinstance(lots, bool):
        raise TypeError("lots 不能是布林值 (bool)")
    if not isinstance(lots, (int, float)):
        raise TypeError(f"lots 必須是整數或浮點數，收到 {type(lots).__name__}")
    if not math.isfinite(lots) or lots <= 0:
        raise ValueError(f"lots 必須是有限正數，收到 {lots}")

    shares_float = lots * 1000.0
    shares_int = round(shares_float)
    if abs(shares_float - shares_int) > 1e-6:
        raise ValueError(f"lots ({lots} 張) 換算後不是整數股數: {shares_float}")

    return int(shares_int)


def shares_to_lots(shares: int) -> float:
    """將台股「股 (SHARES)」換算為「張 (LOTS)」.

    Args:
        shares: 股數，必須為正整數。

    Returns:
        換算後張數（浮點數）。

    Raises:
        TypeError: 若輸入為布林值或非整數。
        ValueError: 若股數小於等於 0。
    """
    if isinstance(shares, bool):
        raise TypeError("shares 不能是布林值 (bool)")
    if not isinstance(shares, int):
        raise TypeError(f"shares 必須是整數，收到 {type(shares).__name__}")
    if shares <= 0:
        raise ValueError(f"shares 必須是大於 0 的正整數，收到 {shares}")

    return shares / 1000.0
