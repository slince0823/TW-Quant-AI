"""離線預測工作流程與模型介面 (pipeline.py).

核心原則：
1. 嚴格時序資料隔離：推論僅能使用截至 data_cutoff_time 為止已產生之特徵。
2. 缺失資料誠實回報：若某標的特徵資料不全，嚴禁靜默補零，必須記錄於 failed_symbols 與 missing_features。
3. 嚴格隔離交易核心：本模組僅產出 PredictionBatch，絕不包含下單、資金預留或券商連線代碼。
4. 正式與 Mock 模型強制分離：所有測試用模型必須顯式宣告為 MOCK_FOR_TESTING，不得偽造正式模型產出。
"""

from __future__ import annotations

import datetime as dt
import math
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Protocol, Set

from .contract import PredictionBatch, PredictionMetadata, StockPrediction


class FeatureProvider(Protocol):
    """特徵資料提供者介面。"""

    def get_features(
        self,
        symbols: List[str],
        cutoff_time: dt.datetime,
        required_features: List[str],
    ) -> Dict[str, Dict[str, float]]:
        """取得截至 cutoff_time 為止各標的之可用特徵資料。

        Returns:
            Dict[symbol, Dict[feature_name, feature_value]]
            若某標的無任何資料，該 symbol 可不在此 dict 中，或其 value 為空。
        """
        ...


class BasePredictor(ABC):
    """AI 模型推論基底抽象類別。"""

    def __init__(self, metadata: PredictionMetadata) -> None:
        self.metadata = metadata
        self.metadata.validate()

    @property
    def is_mock(self) -> bool:
        return self.metadata.is_mock

    @abstractmethod
    def predict_batch(
        self,
        symbols: List[str],
        as_of_date: dt.date,
        data_cutoff_time: dt.datetime,
        feature_provider: FeatureProvider,
        top_k: Optional[int] = None,
        prediction_time: Optional[dt.datetime] = None,
    ) -> PredictionBatch:
        """執行批次截面預測。"""
        pass


class MockTestPredictor(BasePredictor):
    """供單元測試與離線架構驗證使用之 Mock 模型 (明確標記為測試用途)."""

    def __init__(
        self,
        model_name: str = "mock_alpha_momentum",
        model_version: str = "v0.1-mock",
        feature_version: str = "feat_v0.1",
        scoring_weights: Optional[Dict[str, float]] = None,
    ) -> None:
        metadata = PredictionMetadata(
            model_name=model_name,
            model_version=model_version,
            feature_version=feature_version,
            model_type="MOCK_FOR_TESTING",
            is_mock=True,
            description="Mock predictor for offline testing and pipeline verification only.",
        )
        super().__init__(metadata)
        # 預設權重：例如動能 (momentum) 0.6 + 價值 (value) 0.4
        self.scoring_weights = scoring_weights or {"momentum_5d": 0.6, "pe_ratio_inv": 0.4}
        self.required_features = list(self.scoring_weights.keys())

    def predict_batch(
        self,
        symbols: List[str],
        as_of_date: dt.date,
        data_cutoff_time: dt.datetime,
        feature_provider: FeatureProvider,
        top_k: Optional[int] = None,
        prediction_time: Optional[dt.datetime] = None,
    ) -> PredictionBatch:
        """執行 Mock 截面預測並落實嚴格資料檢驗。"""
        # 1. 時間戳時區檢驗
        if data_cutoff_time.tzinfo is None:
            raise ValueError("data_cutoff_time 必須是具備時區的 datetime (timezone-aware)")

        pred_time = prediction_time or dt.datetime.now(data_cutoff_time.tzinfo)
        if pred_time.tzinfo is None:
            raise ValueError("prediction_time 必須是具備時區的 datetime (timezone-aware)")

        # 2. 時序因果檢查
        if pred_time < data_cutoff_time:
            raise ValueError("推論時間不能早於資料截止時間")

        # 3. 取得截至 cutoff_time 的特徵資料
        raw_features = feature_provider.get_features(
            symbols=symbols,
            cutoff_time=data_cutoff_time,
            required_features=self.required_features,
        )

        valid_predictions: List[StockPrediction] = []
        failed_symbols: Dict[str, str] = {}
        missing_features_map: Dict[str, List[str]] = {}

        # 4. 逐一標的推論與防禦檢查
        scored_items: List[tuple[str, float, float]] = [] # (symbol, score, confidence)

        for sym in symbols:
            # 代碼合法性
            clean_sym = sym.split(".")[0].strip()
            if not clean_sym.isalnum():
                failed_symbols[sym] = f"股票代碼格式錯誤: {sym}"
                continue

            feats = raw_features.get(sym)
            if not feats:
                failed_symbols[sym] = "缺少特徵資料 (未收到特徵字典)"
                missing_features_map[sym] = list(self.required_features)
                continue

            # 檢查是否有缺失的必要因子 (嚴禁隱式填 0)
            missing = [f for f in self.required_features if f not in feats or feats[f] is None]
            if missing:
                failed_symbols[sym] = f"特徵不完整，缺少因子: {missing} (嚴禁靜默補零)"
                missing_features_map[sym] = missing
                continue

            # 檢查因子數值有效性 (NaN / Inf)
            invalid_vals = {
                f: feats[f]
                for f in self.required_features
                if math.isnan(feats[f]) or math.isinf(feats[f])
            }
            if invalid_vals:
                failed_symbols[sym] = f"因子數值異常包含 NaN 或 Inf: {invalid_vals}"
                continue

            # 線性加權計算 Score (模擬回歸預測)
            score = 0.0
            for f, weight in self.scoring_weights.items():
                score += feats[f] * weight

            confidence = 0.85 # Mock 信心水準
            scored_items.append((sym, score, confidence))

        # 5. 截面排序並指派連續 rank (1-based)，分數平手時以 symbol 字典序穩定排序
        scored_items.sort(key=lambda item: (-item[1], item[0]))

        # 若有指定 top_k 則只取前 K 名
        items_to_rank = scored_items[:top_k] if top_k is not None else scored_items

        for idx, (sym, score, conf) in enumerate(items_to_rank, start=1):
            pred = StockPrediction(
                symbol=sym,
                as_of_date=as_of_date,
                data_cutoff_time=data_cutoff_time,
                prediction_time=pred_time,
                score=score,
                rank=idx,
                model_version=self.metadata.model_version,
                feature_version=self.metadata.feature_version,
                confidence=conf,
                extra_info={"mock_model_type": self.metadata.model_type},
            )
            valid_predictions.append(pred)

        batch_id = (
            f"BATCH-{self.metadata.model_version}-{as_of_date.strftime('%Y%m%d')}-"
            f"{int(data_cutoff_time.timestamp())}"
        )

        batch = PredictionBatch(
            batch_id=batch_id,
            as_of_date=as_of_date,
            data_cutoff_time=data_cutoff_time,
            generated_at=pred_time,
            metadata=self.metadata,
            predictions=valid_predictions,
            failed_symbols=failed_symbols,
            missing_features=missing_features_map,
        )
        batch.validate()
        return batch


class InMemoryFeatureProvider:
    """純記憶體特徵提供者 (供離線測試與安全驗證使用)."""

    def __init__(self) -> None:
        # store: symbol -> list of (timestamp, feature_dict)
        self._store: Dict[str, List[tuple[dt.datetime, Dict[str, float]]]] = {}

    def add_features(
        self,
        symbol: str,
        timestamp: dt.datetime,
        features: Dict[str, float],
    ) -> None:
        """寫入某標的在某時間點可用的特徵資料。"""
        if timestamp.tzinfo is None:
            raise ValueError("timestamp 必須是具備時區的 datetime")
        if symbol not in self._store:
            self._store[symbol] = []
        self._store[symbol].append((timestamp, dict(features)))
        # 按時間排序
        self._store[symbol].sort(key=lambda x: x[0])

    def get_features(
        self,
        symbols: List[str],
        cutoff_time: dt.datetime,
        required_features: List[str],
    ) -> Dict[str, Dict[str, float]]:
        """取得截至 cutoff_time 為止各標的之最新可用特徵 (嚴格防止未來資料洩漏)."""
        if cutoff_time.tzinfo is None:
            raise ValueError("cutoff_time 必須是具備時區的 datetime")

        result: Dict[str, Dict[str, float]] = {}

        for sym in symbols:
            records = self._store.get(sym, [])
            # 僅篩選時間戳 <= cutoff_time 之紀錄 (時序因果屏障)
            valid_records = [r for r in records if r[0] <= cutoff_time]
            if not valid_records:
                continue

            # 取時間最接近 cutoff_time 的最新特徵快照
            latest_features = valid_records[-1][1]
            result[sym] = dict(latest_features)

        return result
