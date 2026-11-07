"""TW Quant AI — Taiwan Stock Quantitative Trading Platform.

台股量化交易系統核心模組庫。
遵循純標準函式庫設計原則，高強健性、零外部相依。
"""

__version__ = "0.1.0"

from .gateway import TradingGateway
from .prediction import (
    BasePredictor,
    FeatureProvider,
    InMemoryFeatureProvider,
    MockTestPredictor,
    PredictionBatch,
    PredictionMetadata,
    StockPrediction,
)

__all__ = [
    "BasePredictor",
    "FeatureProvider",
    "InMemoryFeatureProvider",
    "MockTestPredictor",
    "PredictionBatch",
    "PredictionMetadata",
    "StockPrediction",
    "TradingGateway",
    "__version__",
]
