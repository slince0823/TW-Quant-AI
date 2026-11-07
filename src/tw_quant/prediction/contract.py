"""統一 AI 股票預測輸出合約 (contract.py).

核心原則：
1. 嚴格區分「AI 預測分數 (Score)」與「交易訊號 (Signal)」：預測輸出僅代表模型截面期望排序，絕不直接視為買賣指令。
2. 強制時序因果保護：所有時間戳記必須 timezone-aware，且明確標註資料截止時間 (data_cutoff_time)。
3. 嚴格杜絕靜默填零：特徵或推論缺失之標的必須明確記錄於 failed_symbols，嚴禁隱蔽填補為 0。
4. 全面追溯性：每筆預測均可完整追溯至模型版本 (model_version)、特徵版本 (feature_version) 與資料截止時間。
5. 正式模型與測試 Mock 模型必須具備明確且不可混淆之型態標記。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set


@dataclass(frozen=True)
class PredictionMetadata:
    """預測模型與特徵元資料 (不可變)."""

    model_name: str
    model_version: str
    feature_version: str
    model_type: str                     # e.g. "MOCK_FOR_TESTING", "DOUBLE_ENSEMBLE_LGBM"
    is_mock: bool                       # 明確區分是否為測試 Mock 模型
    trained_cutoff_date: Optional[dt.date] = None
    description: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """校驗元資料完整性。"""
        if not self.model_name or not self.model_name.strip():
            raise ValueError("model_name 不能為空")
        if not self.model_version or not self.model_version.strip():
            raise ValueError("model_version 不能為空")
        if not self.feature_version or not self.feature_version.strip():
            raise ValueError("feature_version 不能為空")
        if not self.model_type or not self.model_type.strip():
            raise ValueError("model_type 不能為空")
        if self.is_mock and "MOCK" not in self.model_type.upper():
            raise ValueError("is_mock 為 True 時，model_type 必須包含 'MOCK' 標記")
        if not self.is_mock and "MOCK" in self.model_type.upper():
            raise ValueError("正式生產模型嚴禁標記為 MOCK 型態")


@dataclass(frozen=True)
class StockPrediction:
    """單一標的之 AI 預測結果 (不可變)."""

    symbol: str                         # 股票代碼 (純台股代號，如 2330)
    as_of_date: dt.date                 # 預測所屬交易基準日 (Target Trade Date)
    data_cutoff_time: dt.datetime       # 模型輸入特徵之資料截止時間戳 (Timezone-aware)
    prediction_time: dt.datetime        # 模型推論執行時間戳 (Timezone-aware)
    score: float                        # 模型預測原始截面評分 (數值型，代表相對期望值)
    rank: int                           # 截面排名 (1-based 整數，1 代表最高評分)
    model_version: str                  # 產出該預測之模型版本代碼
    feature_version: str                # 使用之特徵管線版本代碼
    confidence: Optional[float] = None  # 信心水準或預測不確定性估計 (0.0 ~ 1.0)
    extra_info: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """強制防禦性檢驗。"""
        # 1. 股票代碼格式
        if not self.symbol or not isinstance(self.symbol, str):
            raise ValueError("symbol 必須為非空字串")
        clean_sym = self.symbol.split(".")[0].strip()
        if not clean_sym.isalnum():
            raise ValueError(f"無效的股票代碼格式: {self.symbol}")

        # 2. 評分數值有效性 (嚴禁 NaN 或 Inf)
        if self.score is None or not isinstance(self.score, (int, float)):
            raise ValueError(f"標的 {self.symbol} 評分必須為數值，收到 {type(self.score).__name__}")
        if math.isnan(self.score):
            raise ValueError(f"標的 {self.symbol} 預測評分不得為 NaN")
        if math.isinf(self.score):
            raise ValueError(f"標的 {self.symbol} 預測評分不得為無窮大 (Inf)")

        # 3. 截面排名檢驗
        if not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError(f"標的 {self.symbol} 排名必須為大於等於 1 的正整數，收到 {self.rank}")

        # 4. 時區與時間有效性
        if not isinstance(self.data_cutoff_time, dt.datetime) or self.data_cutoff_time.tzinfo is None:
            raise ValueError("data_cutoff_time 必須是具備明確時區的 datetime (timezone-aware)")
        if not isinstance(self.prediction_time, dt.datetime) or self.prediction_time.tzinfo is None:
            raise ValueError("prediction_time 必須是具備明確時區的 datetime (timezone-aware)")

        # 5. 時序因果檢查 (推論時間不得早於資料截止時間)
        if self.prediction_time < self.data_cutoff_time:
            raise ValueError(
                f"時序因果違規: 推論時間 ({self.prediction_time}) 倒流早於資料截止時間 ({self.data_cutoff_time})"
            )

        # 6. 目標交易日不可早於特徵資料截止日期
        if self.as_of_date < self.data_cutoff_time.date():
            raise ValueError(
                f"未來資料洩漏違規: 目標交易日 ({self.as_of_date}) 早於資料截止日 ({self.data_cutoff_time.date()})"
            )

        # 7. 版本追溯必填
        if not self.model_version or not self.model_version.strip():
            raise ValueError(f"標的 {self.symbol} 缺少 model_version 版本標記")
        if not self.feature_version or not self.feature_version.strip():
            raise ValueError(f"標的 {self.symbol} 缺少 feature_version 版本標記")

        # 8. 信心水準範圍檢查
        if self.confidence is not None:
            if math.isnan(self.confidence) or math.isinf(self.confidence):
                raise ValueError(f"標的 {self.symbol} confidence 不能為 NaN 或 Inf")
            if not (0.0 <= self.confidence <= 1.0):
                raise ValueError(f"標的 {self.symbol} confidence 必須介於 0.0 與 1.0 之間，收到 {self.confidence}")

    def to_dict(self) -> Dict[str, Any]:
        """序列化為乾淨的字典。"""
        return {
            "symbol": self.symbol,
            "as_of_date": self.as_of_date.isoformat(),
            "data_cutoff_time": self.data_cutoff_time.isoformat(),
            "prediction_time": self.prediction_time.isoformat(),
            "score": self.score,
            "rank": self.rank,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "confidence": self.confidence,
            "extra_info": dict(self.extra_info),
        }


@dataclass
class PredictionBatch:
    """單一預測截面之整批預測輸出與審計紀錄。"""

    batch_id: str
    as_of_date: dt.date
    data_cutoff_time: dt.datetime
    generated_at: dt.datetime
    metadata: PredictionMetadata
    predictions: List[StockPrediction] = field(default_factory=list)
    failed_symbols: Dict[str, str] = field(default_factory=dict)         # symbol -> 失敗原因
    missing_features: Dict[str, List[str]] = field(default_factory=dict) # symbol -> 缺失特徵清單

    def validate(self) -> None:
        """整批資料一致性與時間防禦檢驗。"""
        if not self.batch_id or not self.batch_id.strip():
            raise ValueError("batch_id 不能為空")

        if not isinstance(self.data_cutoff_time, dt.datetime) or self.data_cutoff_time.tzinfo is None:
            raise ValueError("data_cutoff_time 必須具備明確時區 (timezone-aware)")
        if not isinstance(self.generated_at, dt.datetime) or self.generated_at.tzinfo is None:
            raise ValueError("generated_at 必須具備明確時區 (timezone-aware)")

        if self.generated_at < self.data_cutoff_time:
            raise ValueError("批次生成時間不得早於資料截止時間")

        self.metadata.validate()

        # 檢驗預測清單
        seen_symbols: Set[str] = set()
        seen_ranks: Set[int] = set()

        for pred in self.predictions:
            pred.validate()

            # 跨筆一致性檢查
            if pred.as_of_date != self.as_of_date:
                raise ValueError(
                    f"批次基準日不一致: 批次為 {self.as_of_date}，標的 {pred.symbol} 標示為 {pred.as_of_date}"
                )
            if pred.data_cutoff_time != self.data_cutoff_time:
                raise ValueError(
                    f"批次資料截止時間不一致: 批次為 {self.data_cutoff_time}，標的 {pred.symbol} 標示為 {pred.data_cutoff_time}"
                )
            if pred.model_version != self.metadata.model_version:
                raise ValueError(
                    f"模型版本不匹配: 批次元資料為 {self.metadata.model_version}，標的 {pred.symbol} 為 {pred.model_version}"
                )

            # 重複標的檢查
            if pred.symbol in seen_symbols:
                raise ValueError(f"批次中出現重複預測標的: {pred.symbol}")
            seen_symbols.add(pred.symbol)

            # 排名唯一性檢查
            if pred.rank in seen_ranks:
                raise ValueError(f"批次中出現重複截面排名 rank={pred.rank} (標的 {pred.symbol})")
            seen_ranks.add(pred.rank)

        # 排名連續性檢查 (若有預測資料)
        if self.predictions:
            max_rank = max(seen_ranks)
            if max_rank != len(self.predictions):
                raise ValueError(
                    f"截面排名不連續: 共 {len(self.predictions)} 筆預測，但最大排名為 {max_rank}"
                )

    def get_top_k(self, k: int) -> List[StockPrediction]:
        """安全取得排名前 K 名標的 (依 rank 升冪排序)."""
        if k <= 0:
            return []
        sorted_preds = sorted(self.predictions, key=lambda p: p.rank)
        return sorted_preds[:k]

    def to_dict(self) -> Dict[str, Any]:
        """結構化匯出。"""
        return {
            "batch_id": self.batch_id,
            "as_of_date": self.as_of_date.isoformat(),
            "data_cutoff_time": self.data_cutoff_time.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "metadata": {
                "model_name": self.metadata.model_name,
                "model_version": self.metadata.model_version,
                "feature_version": self.metadata.feature_version,
                "model_type": self.metadata.model_type,
                "is_mock": self.metadata.is_mock,
            },
            "predictions_count": len(self.predictions),
            "predictions": [p.to_dict() for p in self.predictions],
            "failed_symbols": dict(self.failed_symbols),
            "missing_features": {k: list(v) for k, v in self.missing_features.items()},
        }
