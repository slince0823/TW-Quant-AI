"""獨立風控引擎核心 (engine.py).

核心原則：
1. 嚴格貫徹 10 道風控安全檢驗防線。
2. 風控拒絕具備絕對否決權，AI 信心度不得覆寫。
3. 風控引擎絕不直接下單，絕不修改帳本，絕不連接券商。
4. 人工重設熔斷與 Kill Switch 必須具備審計日誌。
5. 所有金額與比例計算全程採用 Decimal 精確運算。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Set

from .config import RiskConfig
from .context import RiskContext
from .decision import RiskCheckItem, RiskDecision, RiskRejectCode
from ..contracts.conversion import lots_to_shares
from ..contracts.signal import QuantityUnit, SignalAction, SignalOrderType, UnifiedTradeSignal


class RiskEngine:
    """台股獨立交易風控引擎 (RiskEngine)."""

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig()

        # 系統級安全狀態
        self._kill_switch_active: bool = False
        self._circuit_breaker_active: bool = False

        # 訊號狀態追蹤 (持久集合或當前進程記憶體)
        self._approved_signals: Set[str] = set()
        self._rejected_signals: Set[str] = set()

        # 在途未入券商之已核准待辦資金/持倉鎖定池 (防止並發穿透)
        self._pending_committed_cash: Decimal = Decimal("0.00")
        self._pending_committed_shares: Dict[str, int] = {}

        # 審計與狀態變更日誌
        self.audit_log: List[Dict[str, Any]] = []

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active

    # ── 緊急控制與人工重設程序 ──────────────────────────────────

    def activate_kill_switch(self, authorized_by: str, reason: str) -> None:
        """啟動交易引擎緊急停止開關 (Kill Switch)."""
        if not authorized_by or not reason:
            raise ValueError("啟動 Kill Switch 必須提供授權人 (authorized_by) 與具體原因 (reason)")
        self._kill_switch_active = True
        record = {
            "action": "ACTIVATE_KILL_SWITCH",
            "authorized_by": authorized_by,
            "reason": reason,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.audit_log.append(record)

    def deactivate_kill_switch(self, authorized_by: str, reason: str) -> None:
        """人工解除緊急停止開關 (必須由人工審計確認，AI 模型禁止調用)."""
        if not authorized_by or not reason:
            raise ValueError("解除 Kill Switch 必須提供授權人 (authorized_by) 與具體原因 (reason)")
        self._kill_switch_active = False
        record = {
            "action": "DEACTIVATE_KILL_SWITCH",
            "authorized_by": authorized_by,
            "reason": reason,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.audit_log.append(record)

    def reset_circuit_breaker(self, authorized_by: str, reason: str) -> None:
        """人工重設最大回撤熔斷狀態 (必須由授權主管介入審查後始得解除)."""
        if not authorized_by or not reason:
            raise ValueError("重設熔斷必須提供授權人 (authorized_by) 與具體原因 (reason)")
        self._circuit_breaker_active = False
        record = {
            "action": "RESET_CIRCUIT_BREAKER",
            "authorized_by": authorized_by,
            "reason": reason,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.audit_log.append(record)

    # ── 風控前置審查核心 ────────────────────────────────────────

    def evaluate(
        self,
        signal: UnifiedTradeSignal,
        context: RiskContext,
    ) -> RiskDecision:
        """對單一交易訊號執行完整 10 道安全防線檢核。

        Returns:
            RiskDecision: 包含是否允許 (allowed)、拒絕代碼 (reason_codes) 與各項量化檢查結果。
        """
        checks: Dict[str, RiskCheckItem] = {}
        reason_codes: List[str] = []
        is_risk_reducing = False

        # 0. 單位轉換為「股 (SHARES)」
        if signal.quantity_unit == QuantityUnit.LOTS:
            try:
                shares = lots_to_shares(signal.quantity)
            except Exception as e:
                checks["signal_validity"] = RiskCheckItem(
                    name="signal_validity",
                    passed=False,
                    reason_code=RiskRejectCode.SIGNAL_INVALID,
                    message=f"單位轉換失敗: {e}",
                )
                return self._build_decision(signal.signal_id, False, [RiskRejectCode.SIGNAL_INVALID.value], checks, context.current_time)
        else:
            shares = int(signal.quantity)

        # ── 防線 1: 訊號合法性與時效 (Signal Validity Guard) ──
        try:
            signal.validate()
            if signal.is_expired(as_of=context.current_time):
                checks["signal_validity"] = RiskCheckItem(
                    name="signal_validity",
                    passed=False,
                    reason_code=RiskRejectCode.SIGNAL_EXPIRED,
                    message=f"訊號已逾期失效 (到期時間 {signal.expires_at}，目前時間 {context.current_time})",
                )
            else:
                checks["signal_validity"] = RiskCheckItem(name="signal_validity", passed=True)
        except Exception as e:
            checks["signal_validity"] = RiskCheckItem(
                name="signal_validity",
                passed=False,
                reason_code=RiskRejectCode.SIGNAL_INVALID,
                message=str(e),
            )

        if not checks["signal_validity"].passed:
            code = checks["signal_validity"].reason_code
            if code:
                reason_codes.append(code.value)

        # ── 防線 9: 重複訊號檢驗 (Duplicate Signal Guard) ──
        if signal.signal_id in self._approved_signals or signal.signal_id in self._rejected_signals:
            checks["duplicate_signal"] = RiskCheckItem(
                name="duplicate_signal",
                passed=False,
                reason_code=RiskRejectCode.DUPLICATE_SIGNAL,
                message=f"signal_id {signal.signal_id} 已經審核處理過，禁止重複送審",
            )
            reason_codes.append(RiskRejectCode.DUPLICATE_SIGNAL.value)
        else:
            checks["duplicate_signal"] = RiskCheckItem(name="duplicate_signal", passed=True)

        # ── 賣出型態與降低風險屬性判定 ──
        available_shares = context.get_available_shares(signal.symbol)
        if signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            if shares <= available_shares:
                is_risk_reducing = True  # 合法平倉或減碼，屬於降低現有曝險
            else:
                # 賣出數量超過可用持倉 -> 屬於裸放空！
                if not self.config.allow_short_selling:
                    checks["short_selling"] = RiskCheckItem(
                        name="short_selling",
                        passed=False,
                        reason_code=RiskRejectCode.SHORT_SELLING_DISALLOWED,
                        message=(
                            f"標的 {signal.symbol} 委託賣出 {shares} 股超過可用持倉 {available_shares} 股，"
                            "系統未開啟放空，禁止賣空"
                        ),
                    )
                    reason_codes.append(RiskRejectCode.SHORT_SELLING_DISALLOWED.value)
                else:
                    checks["short_selling"] = RiskCheckItem(name="short_selling", passed=True)

        # ── 防線 10: 緊急停止檢查 (Emergency Kill Switch Guard) ──
        if self._kill_switch_active:
            if not is_risk_reducing:
                checks["kill_switch"] = RiskCheckItem(
                    name="kill_switch",
                    passed=False,
                    reason_code=RiskRejectCode.KILL_SWITCH_ACTIVE,
                    message="系統處於緊急停止 (Kill Switch) 狀態，拒絕一切新開倉或增加曝險之委託",
                )
                reason_codes.append(RiskRejectCode.KILL_SWITCH_ACTIVE.value)
            else:
                checks["kill_switch"] = RiskCheckItem(
                    name="kill_switch",
                    passed=True,
                    message="緊急停止狀態下，放行降低風險之平倉賣出",
                )
        else:
            checks["kill_switch"] = RiskCheckItem(name="kill_switch", passed=True)

        # ── 防線 8: 最大回撤熔斷檢查 (Max Drawdown Circuit Breaker) ──
        current_nav = context.calculate_current_nav()
        if current_nav is not None and context.peak_nav > Decimal("0"):
            drawdown = ((context.peak_nav - current_nav) / context.peak_nav).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if drawdown >= self.config.max_drawdown_limit:
                self._circuit_breaker_active = True

        if self._circuit_breaker_active:
            if not is_risk_reducing:
                checks["circuit_breaker"] = RiskCheckItem(
                    name="circuit_breaker",
                    passed=False,
                    reason_code=RiskRejectCode.CIRCUIT_BREAKER_ACTIVE,
                    message="系統處於最大回撤熔斷狀態，拒絕一切增加風險之買單委託",
                )
                reason_codes.append(RiskRejectCode.CIRCUIT_BREAKER_ACTIVE.value)
            else:
                checks["circuit_breaker"] = RiskCheckItem(
                    name="circuit_breaker",
                    passed=True,
                    message="熔斷狀態下，放行降低風險之平倉賣出",
                )
        else:
            checks["circuit_breaker"] = RiskCheckItem(name="circuit_breaker", passed=True)

        # ── 防線 7: 每日虧損限制檢查 (Daily Loss Limit Guard) ──
        daily_loss_amount = context.get_daily_loss_amount()
        if context.baseline_nav > Decimal("0"):
            daily_loss_ratio = (daily_loss_amount / context.baseline_nav).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if daily_loss_ratio >= self.config.max_daily_loss_ratio:
                if not is_risk_reducing:
                    checks["daily_loss"] = RiskCheckItem(
                        name="daily_loss",
                        passed=False,
                        reason_code=RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED,
                        message=(
                            f"當日虧損達 {daily_loss_amount} 元 ({daily_loss_ratio:.2%})，"
                            f"已達到或超過每日最大虧損上限 {self.config.max_daily_loss_ratio:.2%}，禁止新開倉"
                        ),
                    )
                    reason_codes.append(RiskRejectCode.DAILY_LOSS_LIMIT_EXCEEDED.value)
                else:
                    checks["daily_loss"] = RiskCheckItem(
                        name="daily_loss",
                        passed=True,
                        message="每日虧損超標狀態下，放行降低風險之平倉賣出",
                    )
            else:
                checks["daily_loss"] = RiskCheckItem(name="daily_loss", passed=True)
        else:
            checks["daily_loss"] = RiskCheckItem(name="daily_loss", passed=True)

        # ── 防線 2: 股票交易狀態與行情完整性 (Trading Status Guard) ──
        if signal.symbol in context.halted_symbols:
            checks["symbol_status"] = RiskCheckItem(
                name="symbol_status",
                passed=False,
                reason_code=RiskRejectCode.SYMBOL_HALTED,
                message=f"標的 {signal.symbol} 處於停牌暫停交易狀態，拒絕委託",
            )
            reason_codes.append(RiskRejectCode.SYMBOL_HALTED.value)
        elif signal.symbol in self.config.restricted_symbols:
            checks["symbol_status"] = RiskCheckItem(
                name="symbol_status",
                passed=False,
                reason_code=RiskRejectCode.SYMBOL_RESTRICTED,
                message=f"標的 {signal.symbol} 位於禁止交易清單中",
            )
            reason_codes.append(RiskRejectCode.SYMBOL_RESTRICTED.value)
        else:
            checks["symbol_status"] = RiskCheckItem(name="symbol_status", passed=True)

        # 取得標的市價估值
        eval_price = (
            Decimal(str(signal.limit_price))
            if signal.order_type == SignalOrderType.LIMIT and signal.limit_price is not None
            else context.get_symbol_price(signal.symbol)
        )

        if eval_price is None or eval_price <= Decimal("0"):
            checks["market_data"] = RiskCheckItem(
                name="market_data",
                passed=False,
                reason_code=RiskRejectCode.MISSING_MARKET_DATA,
                message=f"標的 {signal.symbol} 缺少有效市場估值報價，採取保守拒絕策略",
            )
            reason_codes.append(RiskRejectCode.MISSING_MARKET_DATA.value)
        else:
            checks["market_data"] = RiskCheckItem(name="market_data", passed=True)

        # ── 防線 3、4、5、6 (買入委託專屬資金與曝險檢查) ──
        if signal.action == SignalAction.BUY:
            if eval_price is not None and eval_price > Decimal("0"):
                # 計算買入預估總支出 (含滑價與預估手續費)
                slipped_price = (eval_price * (Decimal("1") + self.config.est_slippage_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                order_notional = (slipped_price * Decimal(shares)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                est_fee = (
                    order_notional * self.config.est_commission_rate * self.config.est_commission_discount
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                est_fee = max(Decimal("20.00"), est_fee)
                total_required_cash = order_notional + est_fee

                # 3. 單筆委託上限檢查
                if total_required_cash > self.config.max_order_amount:
                    checks["single_order_limit"] = RiskCheckItem(
                        name="single_order_limit",
                        passed=False,
                        reason_code=RiskRejectCode.SINGLE_ORDER_LIMIT_EXCEEDED,
                        message=(
                            f"單筆委託預估金額 {total_required_cash} 元 超過上限 {self.config.max_order_amount} 元"
                        ),
                    )
                    reason_codes.append(RiskRejectCode.SINGLE_ORDER_LIMIT_EXCEEDED.value)
                else:
                    checks["single_order_limit"] = RiskCheckItem(name="single_order_limit", passed=True)

                # 6. 最低現金保留檢查 (考慮已預留現金與並發在途承諾現金)
                effective_available_cash = context.cash - self._pending_committed_cash
                remaining_cash_after_order = effective_available_cash - total_required_cash

                nav_for_check = current_nav or context.baseline_nav
                required_min_cash = (nav_for_check * self.config.min_cash_reserve_ratio).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

                if remaining_cash_after_order < required_min_cash or remaining_cash_after_order < Decimal("0"):
                    checks["cash_reserve"] = RiskCheckItem(
                        name="cash_reserve",
                        passed=False,
                        reason_code=RiskRejectCode.INSUFFICIENT_CASH_RESERVE,
                        message=(
                            f"交易後可用現金將剩餘 {remaining_cash_after_order} 元，低於最低保留需求 {required_min_cash} 元 "
                            f"(當前可用現金 {context.cash} 元，在途已承諾 {self._pending_committed_cash} 元)"
                        ),
                    )
                    reason_codes.append(RiskRejectCode.INSUFFICIENT_CASH_RESERVE.value)
                else:
                    checks["cash_reserve"] = RiskCheckItem(name="cash_reserve", passed=True)

                # 4. 單一股票持倉上限檢查
                current_sym_val = (eval_price * Decimal(context.positions.get(signal.symbol, 0))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                pending_sym_buy = context.pending_buy_notionals.get(signal.symbol, Decimal("0.00"))
                total_sym_post_buy = current_sym_val + pending_sym_buy + order_notional
                single_ratio = (total_sym_post_buy / nav_for_check).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )

                if single_ratio > self.config.max_single_position_ratio:
                    checks["single_position_limit"] = RiskCheckItem(
                        name="single_position_limit",
                        passed=False,
                        reason_code=RiskRejectCode.SINGLE_POSITION_LIMIT_EXCEEDED,
                        message=(
                            f"標的 {signal.symbol} 預計總曝險 {total_sym_post_buy} 元 佔 NAV 比例 {single_ratio:.2%}，"
                            f"超過單檔上限 {self.config.max_single_position_ratio:.2%}"
                        ),
                    )
                    reason_codes.append(RiskRejectCode.SINGLE_POSITION_LIMIT_EXCEEDED.value)
                else:
                    checks["single_position_limit"] = RiskCheckItem(name="single_position_limit", passed=True)

                # 5. 總投資金額上限檢查
                total_current_mkt = context.get_total_market_value() or Decimal("0.00")
                total_pending_buys = sum(context.pending_buy_notionals.values(), Decimal("0.00"))
                total_exposure_post_buy = total_current_mkt + total_pending_buys + order_notional
                total_ratio = (total_exposure_post_buy / nav_for_check).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )

                if total_ratio > self.config.max_total_exposure_ratio:
                    checks["total_exposure_limit"] = RiskCheckItem(
                        name="total_exposure_limit",
                        passed=False,
                        reason_code=RiskRejectCode.TOTAL_EXPOSURE_LIMIT_EXCEEDED,
                        message=(
                            f"全投資組合預計總股票曝險 {total_exposure_post_buy} 元 佔 NAV 比例 {total_ratio:.2%}，"
                            f"超過總持倉上限 {self.config.max_total_exposure_ratio:.2%}"
                        ),
                    )
                    reason_codes.append(RiskRejectCode.TOTAL_EXPOSURE_LIMIT_EXCEEDED.value)
                else:
                    checks["total_exposure_limit"] = RiskCheckItem(name="total_exposure_limit", passed=True)

        is_allowed = len(reason_codes) == 0

        # 記錄審核結果
        if is_allowed:
            self._approved_signals.add(signal.signal_id)
        else:
            self._rejected_signals.add(signal.signal_id)

        return self._build_decision(
            signal.signal_id,
            is_allowed,
            reason_codes,
            checks,
            context.current_time,
            is_risk_reducing=is_risk_reducing,
        )

    def commit_approval(
        self,
        signal: UnifiedTradeSignal,
        required_cash: Decimal,
        shares: int,
    ) -> None:
        """鎖定在途已核准資金或部位 (原子性並發防護)."""
        if signal.action == SignalAction.BUY:
            self._pending_committed_cash += required_cash
        elif signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            cur = self._pending_committed_shares.get(signal.symbol, 0)
            self._pending_committed_shares[signal.symbol] = cur + shares

    def release_committed(
        self,
        signal: UnifiedTradeSignal,
        required_cash: Decimal,
        shares: int,
    ) -> None:
        """當訂單進入券商完成預留或遭券商退單時，釋放在途已核准鎖定池。"""
        if signal.action == SignalAction.BUY:
            self._pending_committed_cash = max(Decimal("0.00"), self._pending_committed_cash - required_cash)
        elif signal.action in {SignalAction.SELL, SignalAction.CLOSE}:
            cur = self._pending_committed_shares.get(signal.symbol, 0)
            self._pending_committed_shares[signal.symbol] = max(0, cur - shares)

    def clear_committed(self) -> None:
        """清空在途已核准鎖定池。"""
        self._pending_committed_cash = Decimal("0.00")
        self._pending_committed_shares.clear()

    def _build_decision(
        self,
        signal_id: str,
        allowed: bool,
        reason_codes: List[str],
        checks: Dict[str, RiskCheckItem],
        evaluated_at: dt.datetime,
        is_risk_reducing: bool = False,
    ) -> RiskDecision:
        checks_dict = {
            name: {
                "passed": item.passed,
                "reason_code": item.reason_code.value if item.reason_code else None,
                "message": item.message,
                "metrics": item.metrics,
            }
            for name, item in checks.items()
        }
        return RiskDecision(
            allowed=allowed,
            signal_id=signal_id,
            reason_codes=reason_codes,
            checks=checks_dict,
            evaluated_at=evaluated_at,
            risk_config_version=self.config.version,
            is_risk_reducing=is_risk_reducing,
        )
