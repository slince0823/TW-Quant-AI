"""訊號重複檢測器 (dedup.py).

防止同一 signal_id 被重複提交、重複審核或重複撮合。
純記憶體集合或具備容量淘汰機制的保護器。
"""

from __future__ import annotations

import collections
from typing import Optional


class DuplicateSignalError(ValueError):
    """訊號識別碼重複異常。"""


class SignalDeduplicator:
    """訊號識別碼去重與重複防護器。

    支援記憶體集合儲存，並可限制最大緩存數量以防止長週期記憶體洩漏。
    """

    def __init__(self, max_size: int = 100_000):
        if max_size <= 0:
            raise ValueError("max_size 必須為正整數")
        self.max_size = max_size
        self._seen_set: set[str] = set()
        self._order_queue: collections.deque[str] = collections.deque()

    def is_duplicate(self, signal_id: str) -> bool:
        """檢查 signal_id 是否已存在。"""
        if not signal_id or not isinstance(signal_id, str):
            raise ValueError("signal_id 必須為非空字串")
        return signal_id in self._seen_set

    def register(self, signal_id: str) -> None:
        """註冊新的 signal_id，若已存在則拋出 DuplicateSignalError。

        Raises:
            DuplicateSignalError: 當 signal_id 已經被記錄過時。
            ValueError: 當 signal_id 為空或非字串。
        """
        if not signal_id or not isinstance(signal_id, str):
            raise ValueError("signal_id 必須為非空字串")

        if signal_id in self._seen_set:
            raise DuplicateSignalError(f"偵測到重複訊號識別碼: {signal_id}")

        # 若超過最大容量，淘汰最舊的識別碼
        if len(self._seen_set) >= self.max_size:
            oldest = self._order_queue.popleft()
            self._seen_set.remove(oldest)

        self._seen_set.add(signal_id)
        self._order_queue.append(signal_id)

    def clear(self) -> None:
        """清空快取。"""
        self._seen_set.clear()
        self._order_queue.clear()

    def __len__(self) -> int:
        return len(self._seen_set)
