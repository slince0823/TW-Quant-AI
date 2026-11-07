"""策略訊號派送 (dispatch.py).

只透過既有的 TradingGateway.submit_signal() 送出訊號，由 RiskEngine 裁決、TaiwanPaperBroker 建單。
本模組：
- 不直接呼叫 broker、不讀寫帳本、不修改風控狀態；
- 風控拒絕後不重試、不改寫訊號 (去重鍵與 signal_id 皆為確定性，重送也會被攔截)；
- 回報「風控是否核准 / 委託狀態」，核准不等於成交。
"""

from __future__ import annotations

from typing import Any, List, Optional

from ..contracts.dedup import DuplicateSignalError
from .config import CostModel
from .decision import DispatchRecord, StrategyResult
from .snapshot import PortfolioSnapshot

COST_MODEL_MISMATCH_NOTE = "COST_MODEL_DIFFERS_FROM_PAPER_CONFIG: 策略成本估算與模擬券商費率設定不一致，估算僅為近似"


def dispatch_signals(
    result: StrategyResult,
    gateway: Any,
    snapshot: PortfolioSnapshot,
    cost_model: Optional[CostModel] = None,
) -> List[DispatchRecord]:
    """依 result.signals 的既定順序 (先出場後進場) 逐筆送入 TradingGateway。

    cost_model 若提供，會與 gateway.broker.config 比對，不一致時在每筆紀錄 note 中揭露。
    """
    if result.batch_rejection is not None or not result.signals:
        return []

    note = ""
    broker_config = getattr(getattr(gateway, "broker", None), "config", None)
    if cost_model is not None and broker_config is not None and not cost_model.matches_paper_config(broker_config):
        note = COST_MODEL_MISMATCH_NOTE

    ref_prices = {d.symbol: d.target.reference_price for d in result.decisions if d.signal_id and d.target}
    prices = dict(snapshot.prices)

    records: List[DispatchRecord] = []
    for signal in result.signals:
        try:
            _ok, risk_decision, order = gateway.submit_signal(
                signal,
                ref_price=ref_prices.get(signal.symbol),
                market_prices=prices,
            )
        except DuplicateSignalError as exc:
            records.append(DispatchRecord(
                signal_id=signal.signal_id, symbol=signal.symbol, action=signal.action.value,
                risk_allowed=False, reason_codes=("DUPLICATE_SIGNAL",), order_id=None, order_status=None,
                note=f"下游拒絕重複訊號: {exc}",
            ))
            continue
        records.append(DispatchRecord(
            signal_id=signal.signal_id, symbol=signal.symbol, action=signal.action.value,
            risk_allowed=bool(risk_decision.allowed),
            reason_codes=tuple(risk_decision.reason_codes),
            order_id=None if order is None else order.order_id,
            order_status=None if order is None else order.status.value,
            note=note,
        ))
    return records
