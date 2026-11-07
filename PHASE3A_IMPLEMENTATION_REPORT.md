# TW Quant AI — Taiwan Stock Quantitative Trading Platform
# 第三階段 A 實作報告：Qlib AI 模型整合、資料管線與離線預測介面 (PHASE3A_IMPLEMENTATION_REPORT.md)

完成日期：2026-10-21  
狀態：完成  
負責模組：`src/tw_quant/prediction/`、`tests/test_prediction.py`

---

## 一、開發目標與原則遵循

本階段的主要目標是在自建交易核心 `src/tw_quant/` 中建立嚴密、不可變且具備完整時序保護的統一 AI 預測介面，並完成深度原始碼審查與離線預測流程實作。

全實作嚴格遵循各項架構與安全性約束：
1. **區分「模型評分」與「交易訊號」**：AI 模型僅產出相對截面期望排序（`score` 與 `rank`），嚴禁直接輸出 `BUY` 或 `SELL` 訊號。
2. **時區與截止時間防護 (Data Cutoff Time Guard)**：所有預測物件必須具備明確時區（timezone-aware），並強制驗證 `prediction_time >= data_cutoff_time` 與 `as_of_date >= data_cutoff_time.date()`，防止時光倒流與未來資料洩漏。
3. **拒絕隱蔽補零**：特徵缺失或推論失敗之標的，必須誠實記錄於 `failed_symbols` 與 `missing_features`，嚴禁使用 `df.fillna(0)` 默默填零。
4. **正式模型與 Mock 模型強制分離**：測試用模型必須顯式宣告 `is_mock = True` 與 `model_type = "MOCK_FOR_TESTING"`，嚴禁偽造正式模型產出。
5. **與交易核心嚴格隔離**：預測介面不呼叫任何券商 API、不觸碰 `TaiwanPaperBroker` 或 `TradingGateway`。
6. **既有專案 0 改動**：`anti-gambling-trader-tw`（commit `8609cef`）與 `qlib-tw-trader`（commit `774b3fb`）維持 100% 原始乾淨狀態。

---

## 二、新增與修改檔案清單

| 檔案路徑 | 模組職責 | 關鍵類別 / 介面 |
| :--- | :--- | :--- |
| `src/tw_quant/prediction/contract.py` | 統一 AI 預測輸出合約 | `PredictionMetadata` (不可變元資料)<br>`StockPrediction` (單一標的評分與排名，自動驗證)<br>`PredictionBatch` (截面整批輸出與審計紀錄) |
| `src/tw_quant/prediction/pipeline.py` | 離線推論流程與抽象協定 | `FeatureProvider` (特徵提供者介面協定)<br>`BasePredictor` (推論抽象基底類別)<br>`MockTestPredictor` (顯式標記之測試用 Mock 模型)<br>`InMemoryFeatureProvider` (純記憶體防洩漏特徵庫) |
| `src/tw_quant/prediction/__init__.py` | 預測套件公開介面匯出 | 匯出預測合約、推論器與特徵提供者類別 |
| `src/tw_quant/__init__.py` | 套件根層級匯出更新 | 匯出 `prediction` 核心型別 |
| `tests/test_prediction.py` | 預測合約與推論流程單元測試 | 14 個純離線測試案例（涵蓋合約防禦、時區、NaN/Inf、時序因果、防洩漏等） |
| `AI_MODEL_INTEGRATION_AUDIT.md` | AI 模型與資料管線深度審查 | 針對 14 項關鍵問題之完整分析與相容性矩陣 |
| `PROJECT_MAP.md` | 專案全景導航與地圖更新 | 更新第三階段 A 完成狀態與架構圖 |

---

## 三、統一預測介面與資料結構規格

### 1. `StockPrediction` (單一股票預測)
```python
@dataclass(frozen=True)
class StockPrediction:
    symbol: str                         # 股票代碼 (純台股代號，如 2330)
    as_of_date: dt.date                 # 預測所屬交易基準日 (Target Trade Date)
    data_cutoff_time: dt.datetime       # 模型輸入特徵之資料截止時間戳 (Timezone-aware)
    prediction_time: dt.datetime        # 模型推論執行時間戳 (Timezone-aware)
    score: float                        # 模型預測原始截面評分 (數值型，代表相對期望值)
    rank: int                           # 截面排名 (1-based 整數，1 代表最高評分)
    model_version: str                  # 產出該預測之模型版本代碼
    feature_version: str                # 使用之特徵管線版本代碼
    confidence: Optional[float] = None  # 信心水準或預測不確定性估計 (0.0 ~ 1.0)
    extra_info: Dict[str, Any] = ...
```

### 2. `PredictionBatch` (整批截面預測與缺失追蹤)
```python
@dataclass
class PredictionBatch:
    batch_id: str
    as_of_date: dt.date
    data_cutoff_time: dt.datetime
    generated_at: dt.datetime
    metadata: PredictionMetadata
    predictions: List[StockPrediction]
    failed_symbols: Dict[str, str]          # 失敗標的 -> 失敗原因 (拒絕隱蔽填零)
    missing_features: Dict[str, List[str]]  # 缺失因子追蹤清單
```

### 3. 核心防禦性規則：
- `score` 與 `confidence` 嚴格禁止出現 `NaN` 或 `Inf`，違者於實例化時即刻拋出 `ValueError`。
- `data_cutoff_time` 與 `prediction_time` 強制必須為 timezone-aware。
- 時序因果防護：強制檢驗 `prediction_time >= data_cutoff_time`。
- 未來資料防護：強制檢驗 `as_of_date >= data_cutoff_time.date()`。
- 批次一致性：批次內所有預測標的之 `as_of_date`、`data_cutoff_time` 與 `model_version` 必須 100% 相同。
- 排名連續性：若預測集合有 $N$ 筆資料，其排名必須為 $1 \dots N$ 嚴格連續且無重複。

---

## 四、正式模型與 Mock 模型的明確區隔

為確保系統在任何開發與持續整合環境（無 GPU、無 MSVC C++ 編譯器）均能進行嚴格的離線測試，同時杜絕「以假亂真」之混淆風險：

1. **顯式標記**：
   - `PredictionMetadata.is_mock: bool`
   - `PredictionMetadata.model_type: str`
2. **雙向強制防偽校驗**：
   - 若 `is_mock == True`，`model_type` **必須**包含 `"MOCK"` 字樣（如 `"MOCK_FOR_TESTING"`），否則拋出 `ValueError`。
   - 若 `is_mock == False`（正式生產模型），`model_type` **嚴禁**包含 `"MOCK"` 字樣，否則拋出 `ValueError`。
3. **`MockTestPredictor` 職責限制**：
   - 僅供管線連通性、時間防護、異常輸入與排程測試。
   - 絕不用於宣稱真實策略獲利能力或產出回測報告。

---

## 五、測試套件執行結果

本階段新增之測試套件 [`tests/test_prediction.py`](file:///d:/OneDrive/桌面/TW%20Quant%20AI%20—%20Taiwan%20Stock%20Quantitative%20Trading%20Platform/tests/test_prediction.py) 涵蓋 14 個單元測試，全部通過：

```text
Ran 14 tests in 0.001s
OK
```

### 全平台測試統計（累計 104 項測試）：
- `tests/test_signal.py`: 18/18 通過
- `tests/test_paper_broker.py`: 26/26 通過
- `tests/test_risk_engine.py`: 22/22 通過
- `tests/test_integration.py`: 24/24 通過
- `tests/test_prediction.py`: 14/14 通過
- **總計：104 項單元與整合測試，通過率 100%，總耗時 0.009 秒。**

---

## 六、尚未實作功能與下一階段規劃

1. **StrategyEngine 訊號轉換器 (待實作 - 第三階段 B)**：
   - 目前 AI 僅輸出評分與排名，尚未包含根據排名、門檻或分位數生成 `UnifiedTradeSignal` 之策略規則引擎。
2. **Qlib 二進位格式轉接器 (Adapter)**：
   - 待實作將 Qlib 離線訓練產出的特徵與權重檔讀入自建 `FeatureProvider` 的輕量轉接器。
3. **正式模型推論容器化隔離**：
   - 鑑於 `pyqlib` 與 `lightgbm` 需要特定的 Python 3.10/C++ 環境，建議未來以獨立 Docker 容器或子行程隔離運行正式訓練與特徵生成，透過 `PredictionBatch` 結構化檔案進行跨進程通訊。

---

## 七、進入下一階段評估

| 檢驗項目 | 狀態 | 說明 |
| :--- | :---: | :--- |
| **統一預測合約 (Prediction Contract)** | **已通過驗證** | 資料型別、時序因果、防 NaN/Inf 與防重複完全驗證 |
| **離線預測管線 (Prediction Pipeline)** | **已通過驗證** | 特徵提供者防洩漏過濾、批次排序與缺失紀錄完全驗證 |
| **模型防偽與型態分離** | **已通過驗證** | 正式模型與 Mock 模型防偽雙向檢驗通過 |
| **與交易核心隔離性** | **已通過驗證** | 0 呼叫下單、0 呼叫真實券商 |
| **原始子模組狀態** | **已通過驗證** | `anti-gambling-trader-tw` 與 `qlib-tw-trader` 100% 原始乾淨狀態 (0 改動) |

### 結論
系統已具備乾淨、安全且標準化的 AI 預測資料合約，**完全具備安全進入下一階段（StrategyEngine 策略生成引擎開發）的條件**。
