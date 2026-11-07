"""AI 股票預測合約與離線推論管線測試 (test_prediction.py).

完全離線執行，不依賴外部 API、網路或真實券商。
全面驗證 Prediction 合約的 14 項核心規範、時序因果、防資料洩漏與異常防禦。
"""

from __future__ import annotations

import datetime as dt
import math
import unittest

from src.tw_quant.prediction.contract import (
    PredictionBatch,
    PredictionMetadata,
    StockPrediction,
)
from src.tw_quant.prediction.pipeline import (
    InMemoryFeatureProvider,
    MockTestPredictor,
)


class TestPredictionContractAndPipeline(unittest.TestCase):
    """預測合約與離線推論管線單元測試。"""

    def setUp(self) -> None:
        self.tz_taipei = dt.timezone(dt.timedelta(hours=8))
        self.tz_utc = dt.timezone.utc
        self.as_of_date = dt.date(2026, 10, 21)
        # 資料截止時間：2026-10-20 13:30 (前一日收盤)
        self.cutoff_time = dt.datetime(2026, 10, 20, 13, 30, 0, tzinfo=self.tz_taipei)
        # 推論執行時間：2026-10-20 18:00 (前一日盤後執行)
        self.pred_time = dt.datetime(2026, 10, 20, 18, 0, 0, tzinfo=self.tz_taipei)

        self.mock_metadata = PredictionMetadata(
            model_name="mock_lgbm_model",
            model_version="v1.0.0-mock",
            feature_version="alpha_feat_v1",
            model_type="MOCK_FOR_TESTING",
            is_mock=True,
            trained_cutoff_date=dt.date(2026, 9, 30),
            description="Unit test mock model",
        )

        self.predictor = MockTestPredictor(
            model_name="mock_lgbm_model",
            model_version="v1.0.0-mock",
            feature_version="alpha_feat_v1",
        )

        self.feature_provider = InMemoryFeatureProvider()

    def _create_prediction(self, **overrides) -> StockPrediction:
        """建立合法預測物件輔助方法。"""
        params = {
            "symbol": "2330",
            "as_of_date": self.as_of_date,
            "data_cutoff_time": self.cutoff_time,
            "prediction_time": self.pred_time,
            "score": 0.85,
            "rank": 1,
            "model_version": "v1.0.0-mock",
            "feature_version": "alpha_feat_v1",
            "confidence": 0.90,
            "extra_info": {"notes": "test"},
        }
        params.update(overrides)
        return StockPrediction(**params)

    # 1. 正常預測輸出
    def test_01_normal_prediction_and_batch(self) -> None:
        pred = self._create_prediction()
        pred.validate()
        self.assertEqual(pred.symbol, "2330")
        self.assertEqual(pred.score, 0.85)
        self.assertEqual(pred.rank, 1)

        batch = PredictionBatch(
            batch_id="BATCH-01",
            as_of_date=self.as_of_date,
            data_cutoff_time=self.cutoff_time,
            generated_at=self.pred_time,
            metadata=self.mock_metadata,
            predictions=[pred],
        )
        batch.validate()
        self.assertEqual(len(batch.predictions), 1)
        self.assertEqual(batch.get_top_k(1)[0].symbol, "2330")

    # 2. 股票代碼格式錯誤
    def test_02_invalid_symbol_format(self) -> None:
        # 空字串
        with self.assertRaises(ValueError):
            self._create_prediction(symbol="")

        # 包含非法符號
        with self.assertRaises(ValueError):
            pred = self._create_prediction(symbol="2330*#@")
            pred.validate()

    # 3. 批次中出現重複股票紀錄
    def test_03_duplicate_symbol_in_batch(self) -> None:
        p1 = self._create_prediction(symbol="2330", rank=1)
        p2 = self._create_prediction(symbol="2330", rank=2)  # 同一股票重複出現

        batch = PredictionBatch(
            batch_id="BATCH-DUP",
            as_of_date=self.as_of_date,
            data_cutoff_time=self.cutoff_time,
            generated_at=self.pred_time,
            metadata=self.mock_metadata,
            predictions=[p1, p2],
        )
        with self.assertRaises(ValueError) as cm:
            batch.validate()
        self.assertIn("重複預測標的", str(cm.exception))

    # 4. 缺失預測值與特徵 (嚴禁隱式填 0)
    def test_04_missing_features_recorded_not_silently_zero(self) -> None:
        # 僅為 2330 提供完整特徵，2317 缺少 pe_ratio_inv 特徵，2454 完全無特徵
        self.feature_provider.add_features(
            "2330",
            self.cutoff_time,
            {"momentum_5d": 0.05, "pe_ratio_inv": 0.06},
        )
        self.feature_provider.add_features(
            "2317",
            self.cutoff_time,
            {"momentum_5d": 0.02},  # 缺少 pe_ratio_inv
        )

        batch = self.predictor.predict_batch(
            symbols=["2330", "2317", "2454"],
            as_of_date=self.as_of_date,
            data_cutoff_time=self.cutoff_time,
            feature_provider=self.feature_provider,
            prediction_time=self.pred_time,
        )

        # 只有 2330 成功產出預測
        self.assertEqual(len(batch.predictions), 1)
        self.assertEqual(batch.predictions[0].symbol, "2330")

        # 2317 與 2454 明確記錄於 failed_symbols，嚴禁隱式補 0！
        self.assertIn("2317", batch.failed_symbols)
        self.assertIn("2454", batch.failed_symbols)
        self.assertIn("pe_ratio_inv", batch.missing_features["2317"])

    # 5. NaN 與無限大分數
    def test_05_nan_and_inf_score_rejected(self) -> None:
        # 預測分數為 NaN
        with self.assertRaises(ValueError) as cm1:
            p_nan = self._create_prediction(score=float("nan"))
            p_nan.validate()
        self.assertIn("不得為 NaN", str(cm1.exception))

        # 預測分數為 Inf
        with self.assertRaises(ValueError) as cm2:
            p_inf = self._create_prediction(score=float("inf"))
            p_inf.validate()
        self.assertIn("不得為無窮大", str(cm2.exception))

        # 信心水準為 NaN
        with self.assertRaises(ValueError) as cm3:
            p_conf = self._create_prediction(confidence=float("nan"))
            p_conf.validate()
        self.assertIn("confidence 不能為 NaN", str(cm3.exception))

    # 6. 不同時區的時間處理
    def test_06_timezone_aware_validation(self) -> None:
        # naive datetime (無時區) 必須被拒絕
        naive_dt = dt.datetime(2026, 10, 20, 13, 30, 0)
        with self.assertRaises(ValueError) as cm:
            p_naive = self._create_prediction(data_cutoff_time=naive_dt)
            p_naive.validate()
        self.assertIn("timezone-aware", str(cm.exception))

        # 跨時區比較 (UTC 與 Taipei) 正確轉換比較
        utc_cutoff = dt.datetime(2026, 10, 20, 5, 30, 0, tzinfo=self.tz_utc) # 等於 Taipei 13:30
        taipei_pred = dt.datetime(2026, 10, 20, 14, 0, 0, tzinfo=self.tz_taipei) # 晚於 cutoff
        p_cross = self._create_prediction(data_cutoff_time=utc_cutoff, prediction_time=taipei_pred)
        p_cross.validate()
        self.assertTrue(p_cross.prediction_time > p_cross.data_cutoff_time)

    # 7. 資料截止時間錯誤 (時序因果倒流)
    def test_07_temporal_causality_error(self) -> None:
        # 推論時間早於資料截止時間 (時光倒流錯誤)
        early_pred = self.cutoff_time - dt.timedelta(minutes=30)
        with self.assertRaises(ValueError) as cm:
            p_time_travel = self._create_prediction(
                data_cutoff_time=self.cutoff_time,
                prediction_time=early_pred,
            )
            p_time_travel.validate()
        self.assertIn("時序因果違規", str(cm.exception))

        # 目標交易日早於資料截止日期 (未來資訊倒流)
        past_target_date = dt.date(2026, 10, 19)
        with self.assertRaises(ValueError) as cm2:
            p_leak = self._create_prediction(
                as_of_date=past_target_date,
                data_cutoff_time=self.cutoff_time,
            )
            p_leak.validate()
        self.assertIn("未來資料洩漏違規", str(cm2.exception))

    # 8. 模型版本缺失
    def test_08_missing_model_version(self) -> None:
        with self.assertRaises(ValueError):
            p = self._create_prediction(model_version="")
            p.validate()

    # 9. 特徵版本缺失
    def test_09_missing_feature_version(self) -> None:
        with self.assertRaises(ValueError):
            p = self._create_prediction(feature_version="")
            p.validate()

    # 10. 預測日期不一致
    def test_10_prediction_date_mismatch_in_batch(self) -> None:
        p1 = self._create_prediction(symbol="2330", as_of_date=self.as_of_date, rank=1)
        p2 = self._create_prediction(
            symbol="2317",
            as_of_date=self.as_of_date + dt.timedelta(days=1), # 日期不一致
            rank=2,
        )

        batch = PredictionBatch(
            batch_id="BATCH-MISMATCH",
            as_of_date=self.as_of_date,
            data_cutoff_time=self.cutoff_time,
            generated_at=self.pred_time,
            metadata=self.mock_metadata,
            predictions=[p1, p2],
        )
        with self.assertRaises(ValueError) as cm:
            batch.validate()
        self.assertIn("批次基準日不一致", str(cm.exception))

    # 11. 未來資料洩漏防護 (Feature Provider 阻擋未來資料)
    def test_11_future_data_leakage_blocked_by_provider(self) -> None:
        t_past = self.cutoff_time - dt.timedelta(hours=1)
        t_future = self.cutoff_time + dt.timedelta(hours=1) # 未來資料

        # 寫入過去特徵
        self.feature_provider.add_features(
            "2330",
            t_past,
            {"momentum_5d": 0.03, "pe_ratio_inv": 0.05},
        )
        # 寫入未來特徵
        self.feature_provider.add_features(
            "2330",
            t_future,
            {"momentum_5d": 0.99, "pe_ratio_inv": 0.99},
        )

        # 查詢截至 cutoff_time 的特徵
        feats = self.feature_provider.get_features(["2330"], self.cutoff_time, ["momentum_5d"])
        # 必須取得過去特徵 (0.03)，嚴禁取到未來特徵 (0.99)
        self.assertEqual(feats["2330"]["momentum_5d"], 0.03)

    # 12. 空資料集處理
    def test_12_empty_dataset_handling(self) -> None:
        batch = self.predictor.predict_batch(
            symbols=[],
            as_of_date=self.as_of_date,
            data_cutoff_time=self.cutoff_time,
            feature_provider=self.feature_provider,
            prediction_time=self.pred_time,
        )
        batch.validate()
        self.assertEqual(len(batch.predictions), 0)
        self.assertEqual(len(batch.failed_symbols), 0)

    # 13. 部分股票推論失敗，其餘正常排序連續
    def test_13_partial_stock_inference_failure(self) -> None:
        # 5 檔股票：2330, 2317, 2454, 2308, 9999
        # 提供其中 3 檔合法特徵，其餘 2 檔缺失
        self.feature_provider.add_features(
            "2330", self.cutoff_time, {"momentum_5d": 0.08, "pe_ratio_inv": 0.05}
        ) # score = 0.08*0.6 + 0.05*0.4 = 0.068
        self.feature_provider.add_features(
            "2317", self.cutoff_time, {"momentum_5d": 0.04, "pe_ratio_inv": 0.05}
        ) # score = 0.04*0.6 + 0.05*0.4 = 0.044
        self.feature_provider.add_features(
            "2454", self.cutoff_time, {"momentum_5d": 0.10, "pe_ratio_inv": 0.05}
        ) # score = 0.10*0.6 + 0.05*0.4 = 0.080

        batch = self.predictor.predict_batch(
            symbols=["2330", "2317", "2454", "2308", "INVALID@@"],
            as_of_date=self.as_of_date,
            data_cutoff_time=self.cutoff_time,
            feature_provider=self.feature_provider,
            prediction_time=self.pred_time,
        )

        # 3 檔成功，2 檔失敗
        self.assertEqual(len(batch.predictions), 3)
        self.assertEqual(len(batch.failed_symbols), 2)
        self.assertIn("2308", batch.failed_symbols)
        self.assertIn("INVALID@@", batch.failed_symbols)

        # 排名必須是連續的 1, 2, 3
        ranks = [p.rank for p in batch.predictions]
        self.assertEqual(ranks, [1, 2, 3])
        # 最高分是 2454 (0.080)，次高 2330 (0.068)，第三 2317 (0.044)
        self.assertEqual(batch.predictions[0].symbol, "2454")
        self.assertEqual(batch.predictions[1].symbol, "2330")
        self.assertEqual(batch.predictions[2].symbol, "2317")

    # 14. Mock 模型與正式模型的明確區分
    def test_14_mock_model_explicit_distinction(self) -> None:
        # Mock 模型必須明確標記
        self.assertTrue(self.predictor.is_mock)
        self.assertEqual(self.predictor.metadata.model_type, "MOCK_FOR_TESTING")

        # 若 is_mock=True 但 model_type 未包含 MOCK -> 報錯
        with self.assertRaises(ValueError):
            m_bad = PredictionMetadata(
                model_name="bad_mock",
                model_version="v1",
                feature_version="f1",
                model_type="PRODUCTION_LGBM",
                is_mock=True,
            )
            m_bad.validate()

        # 若 is_mock=False 但 model_type 包含 MOCK -> 報錯
        with self.assertRaises(ValueError):
            m_prod_bad = PredictionMetadata(
                model_name="bad_prod",
                model_version="v1",
                feature_version="f1",
                model_type="MOCK_LGBM",
                is_mock=False,
            )
            m_prod_bad.validate()


if __name__ == "__main__":
    unittest.main()
