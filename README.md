# TW Quant AI — Taiwan Stock Quantitative Trading Platform
台股量化選股研發、策略規則引擎、獨立風控與歷史回測平台

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-Phase%203%20Completed-brightgreen.svg)]()

## 專案簡介 (Overview)

**TW Quant AI** 是一套專為台灣股市設計的現代化量化交易與回測平台，整合了：
1. **AI 預測與選股模組**：基於 Qlib 框架之台股資料管線、特徵工程與模型預測介面。
2. **策略規則引擎 (Strategy Engine)**：狀態機驅動之交易策略信號產生器與決策分發。
3. **獨立風控引擎 (Risk Engine)**：涵蓋防賭裁決、部位限制、市場波動與極端行情報護機制。
4. **高真實度台股歷史回測引擎 (Backtest Engine)**：
   - 台股現股當沖與交易日曆 (`calendar.py`)
   - 逐筆批次持倉帳本 (FIFO Lot Ledger) 與除權息應收股利處理 (`corporate_actions.py`)
   - 歷史撮合模擬器 (`simulator.py`) 與資產估值器 (`valuator.py`)
   - Walk-Forward 樣本外滾動驗證與 Embargo 隔離機制 (`runner.py`)
   - 結構化績效報告與非對稱防偽資料血統機制 (`report.py`, `crypto.py`)

完整架構與詳細各階段審查報告請參閱：[`PROJECT_MAP.md`](PROJECT_MAP.md)。

---

## 專案結構 (Architecture)

```
TW-Quant-AI/
├── src/tw_quant/               # 核心量化套件
│   ├── backtest/               # 台股歷史回測引擎
│   ├── broker/                 # 台股模擬交易與券商介面
│   ├── prediction/             # AI 模型預測管線
│   ├── risk/                   # 獨立風控與裁決引擎
│   ├── signal/                 # 統一交易信號規格 (UnifiedTradeSignal)
│   ├── strategy/               # 策略狀態機與決策分發
│   └── gateway.py              # 統一執行閘道
├── tests/                      # 完整單元測試與安全整合測試 (330+ 測試全數通過)
├── anti-gambling-trader-tw/    # 子模組：交易統計與防賭風控模擬參考庫
├── qlib-tw-trader/             # 子模組：Qlib 台股資料管線與模型候選庫
├── PROJECT_MAP.md              # 專案全景導航與模組地圖
└── README.md                   # 專案說明文件
```

---

## 外部子模組 (Submodules)

本專案整合以下開源上游參考庫作為 Git 子模組：
- **anti-gambling-trader-tw**: [mars-tw/anti-gambling-trader-tw](https://github.com/mars-tw/anti-gambling-trader-tw)
- **qlib-tw-trader**: [Docat0209/qlib-tw-trader](https://github.com/Docat0209/qlib-tw-trader)

若要在 Clone 本專案時一併取得子模組：
```bash
git clone --recurse-submodules https://github.com/slince0823/TW-Quant-AI.git
```
或在現有 repository 中初始化子模組：
```bash
git submodule update --init --recursive
```

---

## 快速開始 (Quick Start)

### 執行單元與安全測試
```bash
pytest tests/
```

### 核心模組引用範例
```python
from tw_quant.backtest import BacktestRunner, BacktestConfig
from tw_quant.gateway import UnifiedGateway
from tw_quant.risk import RiskEngine
from tw_quant.strategy import StrategyEngine
```

---

## 開發階段與里程碑 (Milestones)

- [x] **第一階段**：券商連接器成熟度審查與差距分析 ([`BROKER_CAPABILITY_AUDIT.md`](BROKER_CAPABILITY_AUDIT.md))
- [x] **第二階段**：統一信號合約、模擬撮合引擎、獨立風控引擎與端到端整合 ([`PHASE2D_INTEGRATION_REPORT.md`](PHASE2D_INTEGRATION_REPORT.md))
- [x] **第三階段 A**：Qlib AI 模型預測合約與管線整合 ([`PHASE3A_IMPLEMENTATION_REPORT.md`](PHASE3A_IMPLEMENTATION_REPORT.md))
- [x] **第三階段 B**：策略規則引擎實作與決策分發驗收 ([`PHASE3B_STRATEGY_ENGINE_REPORT.md`](PHASE3B_STRATEGY_ENGINE_REPORT.md))
- [x] **第三階段 C**：台股歷史回測引擎、FIFO 持倉批次、除權息與防偽信任架構 ([`C2_5_2_2_1_REMEDIATION_REPORT.md`](C2_5_2_2_1_REMEDIATION_REPORT.md))
