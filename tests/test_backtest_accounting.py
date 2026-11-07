"""第三階段 C-2.2：FIFO 持倉批次與除權息會計單元與驗收測試 (test_backtest_accounting.py).

測試涵蓋範圍（對應任務要求 Section C 1~18 項及除息日 NAV 獨立核對）：
1. 單次買入及完整賣出。
2. 同一股票分批買入。
3. FIFO 部分賣出與最早進場日推進。
4. 多批次全部平倉。
5. 超賣拒絕（原子性保護，不自動建立空頭持倉）。
6. 重複成交識別碼 (fill_id) 拒絕。
7. 進場日與持有天數查詢（含供給 PortfolioSnapshot.from_broker 使用）。
8. 除息日建立應收股利（區分除息前持有與除息當日買進之股利權利）。
9. 發放日轉入現金。
10. 重複除息或重複發放事件不會重複入帳。
11. 缺少發放日的真實資料不能假裝成已知日期（嚴禁默認第 28 個交易日）。
12. 合成發放日假設與真實資料明確區分。
13. 股利應收款不得被當作可用現金。
14. 交易成本分攤與已實現損益計算一致（與 PortfolioLedger 對帳無誤差）。
15. 股數、金額與 NAV 帳務守恆，以及除息日市值變動與應收股利變動之獨立分解核對（含理論除息價與非等幅波動/填貼息情境，且絕不重複計入現金與應收股利）。
16. 不支援的公司行動（股票股利、股票分割、反分割、減資）明確拒絕，不得靜默略過。
17. 時區、日期順序、無效股數、無效金額與未成交委託之防禦檢查。
18. 兩個原始子專案保持不變验证。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
import subprocess
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tw_quant.backtest import (
    TAIPEI_TZ,
    AccountNAVBreakdown,
    BacktestAccountingError,
    BacktestCalendarError,
    BacktestLotLedger,
    ClosedLotTrade,
    CorporateActionError,
    CorporateActionEvent,
    CorporateActionOrderError,
    CorporateActionProcessor,
    CorporateActionType,
    DataProvenance,
    DataProvenanceError,
    DividendReceivable,
    DuplicateCorporateActionError,
    DuplicateFillError,
    HistoricalDataMissingError,
    InvalidFillRecordError,
    LookaheadBiasError,
    OversellPositionError,
    PITViolationError,
    PositionLot,
    ReceivableStatus,
    TradingCalendar,
    UnsupportedCorporateActionError,
)
from tw_quant.broker.broker import TaiwanPaperBroker
from tw_quant.broker.config import TaiwanPaperConfig
from tw_quant.broker.ledger import PortfolioLedger
from tw_quant.broker.order import FillRecord, PaperOrder
from tw_quant.contracts.signal import SignalAction, SignalOrderType
from tw_quant.strategy.snapshot import PortfolioSnapshot


def make_synth_provenance() -> DataProvenance:
    return DataProvenance(
        source="SYNTHETIC_ACCOUNTING_TEST",
        dataset_version="synth-acc-v1.0",
        retrieved_at=dt.datetime(2026, 1, 1, 8, 0, tzinfo=TAIPEI_TZ),
        is_synthetic=True,
        description="Synthetic fixture for Phase 3C-2.2 accounting tests",
    )


def make_real_provenance() -> DataProvenance:
    return DataProvenance(
        source="TWSE_OFFICIAL_DIVIDEND_TABLE",
        dataset_version="twse-2026Q1",
        retrieved_at=dt.datetime(2026, 1, 1, 8, 0, tzinfo=TAIPEI_TZ),
        is_synthetic=False,
        description="Official TWSE dividend schedule archive",
    )


def calc_fee(notional: Decimal) -> Decimal:
    raw = (notional * Decimal("0.001425")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(Decimal("20"), raw)


def calc_tax(notional: Decimal) -> Decimal:
    return (notional * Decimal("0.003")).quantize(Decimal("1"), rounding=ROUND_FLOOR)


def make_fill(
    fill_id: str,
    symbol: str,
    trade_date: dt.date,
    quantity: int,
    price: Decimal,
    *,
    is_sell: bool = False,
    order_id: str = "ORD-001",
    signal_id: str = "SIG-001",
    time_of_day: dt.time = dt.time(9, 5),
    custom_commission: Decimal | None = None,
    custom_tax: Decimal | None = None,
) -> FillRecord:
    notional = (price * Decimal(quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    comm = custom_commission if custom_commission is not None else calc_fee(notional)
    tax = (
        custom_tax
        if custom_tax is not None
        else (calc_tax(notional) if is_sell else Decimal("0.00"))
    )
    ts = dt.datetime.combine(trade_date, time_of_day, tzinfo=TAIPEI_TZ)
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        signal_id=signal_id,
        symbol=symbol,
        fill_quantity=quantity,
        fill_price=price,
        notional=notional,
        commission=comm,
        tax=tax,
        slippage=Decimal("0.00"),
        timestamp=ts,
    )


class TestBacktestAccounting(unittest.TestCase):
    """第三階段 C-2.2：FIFO 持倉批次與除權息會計完整測試套件。"""

    def setUp(self) -> None:
        # 建立 2026 年 1 月至 2 月合成交易日曆 (排除週末與 1/1 元旦)
        start = dt.date(2026, 1, 1)
        end = dt.date(2026, 2, 28)
        t_days = []
        n_days = []
        cur = start
        while cur <= end:
            if cur.weekday() >= 5 or cur == dt.date(2026, 1, 1):
                n_days.append(cur)
            else:
                t_days.append(cur)
            cur += dt.timedelta(days=1)

        self.calendar = TradingCalendar.create_synthetic_for_test(
            trading_days=t_days,
            non_trading_days=n_days,
            coverage_start=start,
            coverage_end=end,
            strict_explicit_schedule=True,
        )
        self.lot_ledger = BacktestLotLedger(calendar=self.calendar)
        self.corp_processor = CorporateActionProcessor(calendar=self.calendar)

    # ── 1. 單次買入及完整賣出 ───────────────────────────────────

    def test_01_single_buy_and_full_sell(self) -> None:
        """案例 1：單次買入及完整賣出之批次建立、平倉結轉與損益計算。"""
        buy_fill = make_fill("F-001", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"), is_sell=False)
        lot = self.lot_ledger.record_buy_fill(buy_fill, strategy_id="strat_v1")

        self.assertEqual(lot.symbol, "2330")
        self.assertEqual(lot.entry_date, dt.date(2026, 1, 2))
        self.assertEqual(lot.initial_quantity, 1000)
        self.assertEqual(lot.remaining_quantity, 1000)
        # 500 * 1000 = 500,000; 手續費 = round(500000 * 0.001425) = 713
        self.assertEqual(lot.entry_commission, Decimal("713"))
        self.assertEqual(lot.initial_total_cost, Decimal("500713.00"))
        self.assertEqual(self.lot_ledger.get_position_quantity("2330"), 1000)

        # 於 2026-01-06 完整賣出 1000 股 @ 520.00
        sell_fill = make_fill("F-002", "2330", dt.date(2026, 1, 6), 1000, Decimal("520.00"), is_sell=True)
        closed_trades = self.lot_ledger.record_sell_fill(sell_fill, strategy_id="strat_v1")

        self.assertEqual(len(closed_trades), 1)
        ct = closed_trades[0]
        self.assertEqual(ct.quantity, 1000)
        self.assertEqual(ct.entry_date, dt.date(2026, 1, 2))
        self.assertEqual(ct.exit_date, dt.date(2026, 1, 6))
        self.assertEqual(ct.holding_trading_days, 2)  # 1/5, 1/6 共 2 個交易日
        # 賣出價金 = 520,000; 賣出手續費 = 741; 證交稅 = 1,560; 淨回收 = 517,699
        self.assertEqual(ct.exit_gross_proceeds, Decimal("520000.00"))
        self.assertEqual(ct.allocated_exit_commission, Decimal("741"))
        self.assertEqual(ct.allocated_exit_tax, Decimal("1560"))
        self.assertEqual(ct.exit_net_proceeds, Decimal("517699.00"))
        self.assertEqual(ct.entry_cost_basis, Decimal("500713.00"))
        self.assertEqual(ct.realized_pnl, Decimal("16986.00"))

        self.assertEqual(self.lot_ledger.get_position_quantity("2330"), 0)
        self.assertEqual(len(self.lot_ledger.get_open_lots("2330")), 0)
        self.assertIsNone(self.lot_ledger.get_oldest_entry_date("2330"))
        self.assertEqual(self.lot_ledger.get_realized_pnl("2330"), Decimal("16986.00"))

    # ── 2. 同一股票分批買入 ─────────────────────────────────────

    def test_02_multiple_lot_buys_same_symbol(self) -> None:
        """案例 2：同一股票於不同交易日分批買入，各自保留獨立進場日期與成本。"""
        f1 = make_fill("F-101", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"))
        f2 = make_fill("F-102", "2330", dt.date(2026, 1, 5), 2000, Decimal("510.00"))
        f3 = make_fill("F-103", "2330", dt.date(2026, 1, 7), 1000, Decimal("525.00"))

        self.lot_ledger.record_buy_fill(f1)
        self.lot_ledger.record_buy_fill(f2)
        self.lot_ledger.record_buy_fill(f3)

        open_lots = self.lot_ledger.get_open_lots("2330")
        self.assertEqual(len(open_lots), 3)
        self.assertEqual(self.lot_ledger.get_position_quantity("2330"), 4000)
        self.assertEqual(open_lots[0].entry_date, dt.date(2026, 1, 2))
        self.assertEqual(open_lots[1].entry_date, dt.date(2026, 1, 5))
        self.assertEqual(open_lots[2].entry_date, dt.date(2026, 1, 7))
        self.assertEqual(self.lot_ledger.get_oldest_entry_date("2330"), dt.date(2026, 1, 2))

    # ── 3. FIFO 部分賣出 ────────────────────────────────────────

    def test_03_fifo_partial_sell_updates_oldest_entry_date(self) -> None:
        """案例 3：FIFO 部分賣出先扣減最早批次，第一批平倉後自動將 oldest_entry_date 推進至第二批。"""
        f1 = make_fill("F-201", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"))
        f2 = make_fill("F-202", "2330", dt.date(2026, 1, 6), 2000, Decimal("520.00"))
        self.lot_ledger.record_buy_fill(f1)
        self.lot_ledger.record_buy_fill(f2)

        # 於 2026-01-08 賣出 1,500 股（應全數平倉 F-201 的 1,000 股，並部分平倉 F-202 的 500 股）
        sell_f = make_fill("F-203", "2330", dt.date(2026, 1, 8), 1500, Decimal("530.00"), is_sell=True)
        closed = self.lot_ledger.record_sell_fill(sell_f)

        self.assertEqual(len(closed), 2)
        self.assertEqual(closed[0].lot_id, "LOT-F-201")
        self.assertEqual(closed[0].quantity, 1000)
        self.assertEqual(closed[0].entry_date, dt.date(2026, 1, 2))
        self.assertEqual(closed[1].lot_id, "LOT-F-202")
        self.assertEqual(closed[1].quantity, 500)
        self.assertEqual(closed[1].entry_date, dt.date(2026, 1, 6))

        # 檢查剩餘未平倉批次與最早進場日期推進
        open_lots = self.lot_ledger.get_open_lots("2330")
        self.assertEqual(len(open_lots), 1)
        self.assertEqual(open_lots[0].lot_id, "LOT-F-202")
        self.assertEqual(open_lots[0].remaining_quantity, 1500)
        self.assertEqual(self.lot_ledger.get_position_quantity("2330"), 1500)
        self.assertEqual(self.lot_ledger.get_oldest_entry_date("2330"), dt.date(2026, 1, 6))

    # ── 4. 多批次全部平倉 ───────────────────────────────────────

    def test_04_multi_lot_full_liquidation(self) -> None:
        """案例 4：單筆平倉單橫跨 3 個批次全部結清，持倉歸零且無殘留批次。"""
        self.lot_ledger.record_buy_fill(make_fill("F-301", "2317", dt.date(2026, 1, 2), 1000, Decimal("100.00")))
        self.lot_ledger.record_buy_fill(make_fill("F-302", "2317", dt.date(2026, 1, 5), 1000, Decimal("105.00")))
        self.lot_ledger.record_buy_fill(make_fill("F-303", "2317", dt.date(2026, 1, 6), 1000, Decimal("110.00")))

        close_fill = make_fill("F-304", "2317", dt.date(2026, 1, 9), 3000, Decimal("112.00"), is_sell=True)
        closed = self.lot_ledger.record_fill(close_fill, SignalAction.CLOSE)
        self.assertEqual(len(closed), 3)
        self.assertEqual(self.lot_ledger.get_position_quantity("2317"), 0)
        self.assertEqual(self.lot_ledger.get_all_position_quantities(), {})
        self.assertEqual(self.lot_ledger.get_oldest_entry_dates(), {})

    # ── 5. 超賣拒絕 ─────────────────────────────────────────────

    def test_05_oversell_rejected_without_mutating_lots(self) -> None:
        """案例 5：超賣必須明確拒絕 (OversellPositionError)，不得自動建立空頭持倉，且原有批次毫髮無傷。"""
        # 空手直接賣出 -> 拒絕
        with self.assertRaises(OversellPositionError):
            self.lot_ledger.record_sell_fill(
                make_fill("F-400", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"), is_sell=True)
            )

        # 持有 1000 股，嘗試賣出 2000 股 -> 拒絕且原 1000 股批次維持原狀
        self.lot_ledger.record_buy_fill(make_fill("F-401", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00")))
        with self.assertRaises(OversellPositionError):
            self.lot_ledger.record_sell_fill(
                make_fill("F-402", "2330", dt.date(2026, 1, 5), 2000, Decimal("510.00"), is_sell=True)
            )
        self.assertEqual(self.lot_ledger.get_position_quantity("2330"), 1000)
        self.assertEqual(len(self.lot_ledger.get_closed_trades("2330")), 0)

    # ── 6. 重複成交識別碼拒絕 ───────────────────────────────────

    def test_06_duplicate_fill_id_rejected(self) -> None:
        """案例 6：重複的 fill_id 入帳必須拋出 DuplicateFillError。"""
        f1 = make_fill("F-DUP-01", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"))
        self.lot_ledger.record_buy_fill(f1)
        with self.assertRaises(DuplicateFillError):
            self.lot_ledger.record_buy_fill(f1)

    # ── 7. 進場日與持有天數查詢 (含 PortfolioSnapshot 整合) ──────

    def test_07_entry_date_holding_days_and_portfolio_snapshot_integration(self) -> None:
        """案例 7：查詢進場日與持有交易日數，並驗證可無縫供給 PortfolioSnapshot.from_broker()。"""
        broker = TaiwanPaperBroker(TaiwanPaperConfig(initial_cash=Decimal("1000000.00")))
        buy_fill = make_fill("F-701", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"))
        broker.ledger.record_buy_fill(
            symbol="2330",
            fill_shares=1000,
            fill_price=Decimal("500.00"),
            notional=buy_fill.notional,
            fee=buy_fill.commission,
            reserved_cash_deduct=Decimal("0.00"),
        )
        self.lot_ledger.record_buy_fill(buy_fill)

        # 2026-01-02 (五) 進場，截至 2026-01-08 (四) 經過 1/5, 1/6, 1/7, 1/8 共 4 個交易日
        self.assertEqual(
            self.lot_ledger.get_holding_days("2330", dt.date(2026, 1, 8)),
            4,
        )

        # 建立 PortfolioSnapshot
        snap = PortfolioSnapshot.from_broker(
            broker=broker,
            as_of=dt.datetime(2026, 1, 8, 17, 30, tzinfo=TAIPEI_TZ),
            prices={"2330": Decimal("520.00")},
            prices_as_of=dt.datetime(2026, 1, 8, 13, 30, tzinfo=TAIPEI_TZ),
            entry_dates=self.lot_ledger.get_oldest_entry_dates(),
        )
        self.assertEqual(snap.positions["2330"].entry_date, dt.date(2026, 1, 2))
        self.assertEqual(snap.positions["2330"].quantity, 1000)

    # ── 8. 除息日建立應收股利 ───────────────────────────────────

    def test_08_ex_dividend_creates_receivable_without_increasing_cash(self) -> None:
        """案例 8：除息日依除息前持股建立應收股利（不增加可用現金，且除息當日新買進股數不含息）。"""
        ledger = PortfolioLedger(Decimal("1000000.00"))
        # 2026-01-05 (除息前) 買進 2000 股 @ 500
        f_before = make_fill("F-801", "2330", dt.date(2026, 1, 5), 2000, Decimal("500.00"))
        ledger.record_buy_fill("2330", 2000, Decimal("500.00"), f_before.notional, f_before.commission, Decimal("0"))
        self.lot_ledger.record_buy_fill(f_before)

        # 2026-01-08 為除息日，若在除息日當日 09:05 新買進 1000 股，該筆新買進股數不應享有本次配息
        f_on_ex = make_fill("F-802", "2330", dt.date(2026, 1, 8), 1000, Decimal("490.00"))
        self.lot_ledger.record_buy_fill(f_on_ex)
        self.assertEqual(self.lot_ledger.get_eligible_shares_for_ex_date("2330", dt.date(2026, 1, 8)), 2000)

        cash_before_ex = ledger.cash
        div_event = CorporateActionEvent(
            event_id="CA-2330-2026Q1",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=dt.date(2026, 1, 8),
            record_date=dt.date(2026, 1, 9),
            payable_date=dt.date(2026, 2, 5),
            cash_dividend_per_share=Decimal("4.50"),
            announced_at=dt.datetime(2025, 12, 15, 17, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 12, 15, 17, 30, tzinfo=TAIPEI_TZ),
            provenance=make_synth_provenance(),
            is_synthetic_payable_date=True,
            remittance_fee=Decimal("10.00"),
        )

        rec = self.corp_processor.process_ex_dividend_event(
            div_event,
            self.lot_ledger,
            as_of_time=dt.datetime(2026, 1, 8, 8, 0, tzinfo=TAIPEI_TZ),
        )
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.eligible_shares, 2000)
        self.assertEqual(rec.gross_dividend, Decimal("9000.00"))
        self.assertEqual(rec.fee_charged, Decimal("10.00"))
        self.assertEqual(rec.net_dividend, Decimal("8990.00"))
        self.assertEqual(rec.status, ReceivableStatus.PENDING)
        self.assertEqual(ledger.cash, cash_before_ex)
        self.assertEqual(self.corp_processor.get_total_dividends_receivable(), Decimal("8990.00"))

    # ── 9. 發放日轉入現金 ───────────────────────────────────────

    def test_09_payable_date_credits_cash_and_clears_receivable(self) -> None:
        """案例 9：發放日將對應之應收股利轉為可用現金，並將應收股利狀態更新為 PAID。"""
        ledger = PortfolioLedger(Decimal("1000000.00"))
        f_before = make_fill("F-901", "2330", dt.date(2026, 1, 5), 2000, Decimal("500.00"))
        ledger.record_buy_fill("2330", 2000, Decimal("500.00"), f_before.notional, f_before.commission, Decimal("0"))
        self.lot_ledger.record_buy_fill(f_before)

        cash_before_ex = ledger.cash
        div_event = CorporateActionEvent(
            event_id="CA-2330-PAY-09",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=dt.date(2026, 1, 8),
            record_date=dt.date(2026, 1, 9),
            payable_date=dt.date(2026, 2, 5),
            cash_dividend_per_share=Decimal("4.50"),
            announced_at=dt.datetime(2025, 12, 15, 17, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 12, 15, 17, 30, tzinfo=TAIPEI_TZ),
            provenance=make_synth_provenance(),
            is_synthetic_payable_date=True,
            remittance_fee=Decimal("10.00"),
        )
        self.corp_processor.process_ex_dividend_event(
            div_event,
            self.lot_ledger,
            as_of_time=dt.datetime(2026, 1, 8, 8, 0, tzinfo=TAIPEI_TZ),
        )

        # 發放日 (2026-02-05) 轉入可用現金
        paid_list = self.corp_processor.process_due_payables(
            ledger,
            as_of_time=dt.datetime(2026, 2, 5, 8, 0, tzinfo=TAIPEI_TZ),
        )
        self.assertEqual(len(paid_list), 1)
        self.assertEqual(paid_list[0].status, ReceivableStatus.PAID)
        self.assertEqual(ledger.cash, cash_before_ex + Decimal("8990.00"))
        self.assertEqual(self.corp_processor.get_total_dividends_receivable(), Decimal("0.00"))
        self.assertEqual(self.corp_processor.total_paid_dividends, Decimal("8990.00"))

    # ── 10. 重複發放事件不會重複入帳 ────────────────────────────

    def test_10_duplicate_ex_dividend_or_payable_settlement_rejected(self) -> None:
        """案例 10：重複註冊除息事件或重複執行發放日入帳均拋出 DuplicateCorporateActionError。"""
        ledger = PortfolioLedger(Decimal("1000000.00"))
        self.lot_ledger.record_buy_fill(make_fill("F-1001", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00")))

        ev = CorporateActionEvent(
            event_id="CA-DUP-1",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=dt.date(2026, 1, 8),
            payable_date=dt.date(2026, 1, 29),
            cash_dividend_per_share=Decimal("5.00"),
            announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
            provenance=make_synth_provenance(),
            is_synthetic_payable_date=True,
        )
        rec = self.corp_processor.process_ex_dividend_event(
            ev,
            self.lot_ledger,
            as_of_time=dt.datetime(2026, 1, 8, 8, 0, tzinfo=TAIPEI_TZ),
        )
        assert rec is not None

        # 重複處理同一除息事件 -> 拒絕
        with self.assertRaises(DuplicateCorporateActionError):
            self.corp_processor.process_ex_dividend_event(
                ev,
                self.lot_ledger,
                as_of_time=dt.datetime(2026, 1, 8, 8, 5, tzinfo=TAIPEI_TZ),
            )

        # 第一次發放成功
        self.corp_processor.settle_payable_dividend(
            rec.receivable_id,
            ledger,
            as_of_time=dt.datetime(2026, 1, 29, 8, 0, tzinfo=TAIPEI_TZ),
        )
        cash_after_first = ledger.cash

        # 第二次嘗試對同一筆應收股利再次發放 -> 拋出 DuplicateCorporateActionError 且現金不變
        with self.assertRaises(DuplicateCorporateActionError):
            self.corp_processor.settle_payable_dividend(
                rec.receivable_id,
                ledger,
                as_of_time=dt.datetime(2026, 1, 29, 9, 0, tzinfo=TAIPEI_TZ),
            )
        self.assertEqual(ledger.cash, cash_after_first)

    # ── 11. 缺少發放日的真實資料不能假裝成已知日期 ──────────────

    def test_11_real_data_missing_payable_date_never_defaults_to_28_days(self) -> None:
        """案例 11：缺少實際 payable_date 的真實歷史資料必須拋出 HistoricalDataMissingError，不得默認第 28 個交易日。"""
        with self.assertRaises(HistoricalDataMissingError) as ctx:
            CorporateActionEvent(
                event_id="CA-REAL-MISSING-PAY",
                symbol="2330",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=dt.date(2026, 1, 8),
                payable_date=None,
                cash_dividend_per_share=Decimal("5.00"),
                announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
                provenance=make_real_provenance(),
                is_synthetic_payable_date=False,
            )
        self.assertIn("payable_date", ctx.exception.missing_fields or ())

    # ── 12. 合成發放日假設與真實資料明確區分 ────────────────────

    def test_12_synthetic_payable_date_and_real_data_strictly_separated(self) -> None:
        """案例 12：真實資料嚴禁標記 is_synthetic_payable_date=True；合成模擬發放日必須明確標記。"""
        with self.assertRaises(DataProvenanceError):
            CorporateActionEvent(
                event_id="CA-SPOOF-PAY",
                symbol="2330",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=dt.date(2026, 1, 8),
                payable_date=dt.date(2026, 2, 10),
                cash_dividend_per_share=Decimal("5.00"),
                announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
                provenance=make_real_provenance(),
                is_synthetic_payable_date=True,  # 真實資料不可標記為合成發放日假設
            )

        # 合成資料缺少 payable_date 亦不得隱式猜測
        with self.assertRaises(HistoricalDataMissingError):
            CorporateActionEvent(
                event_id="CA-SYNTH-NONE-PAY",
                symbol="2330",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=dt.date(2026, 1, 8),
                payable_date=None,
                cash_dividend_per_share=Decimal("5.00"),
                announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
                provenance=make_synth_provenance(),
                is_synthetic_payable_date=True,
            )

    # ── 13. 股利應收款不得被當作可用現金 ────────────────────────

    def test_13_dividend_receivable_cannot_be_used_as_available_cash(self) -> None:
        """案例 13：在發放日前，應收股利不得被當作可用現金預留買股。"""
        ledger = PortfolioLedger(Decimal("501000.00"))
        buy_f = make_fill("F-1301", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"))
        # 買進花費 500,713 元，剩餘可用現金僅 287 元
        ledger.record_buy_fill("2330", 1000, Decimal("500.00"), buy_f.notional, buy_f.commission, Decimal("0"))
        self.lot_ledger.record_buy_fill(buy_f)
        self.assertEqual(ledger.cash, Decimal("287.00"))

        # 2026-01-08 除息配發每股 20 元 -> 應收股利 20,000 元
        ev = CorporateActionEvent(
            event_id="CA-1301",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=dt.date(2026, 1, 8),
            payable_date=dt.date(2026, 2, 10),
            cash_dividend_per_share=Decimal("20.00"),
            announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
            provenance=make_synth_provenance(),
            is_synthetic_payable_date=True,
        )
        self.corp_processor.process_ex_dividend_event(
            ev,
            self.lot_ledger,
            as_of_time=dt.datetime(2026, 1, 8, 8, 0, tzinfo=TAIPEI_TZ),
        )

        # 雖然應收股利有 20,000 元，但可用現金仍只有 287 元；嘗試預留 10,000 元買股必須失敗
        self.assertEqual(ledger.cash, Decimal("287.00"))
        with self.assertRaises(ValueError):
            ledger.reserve_buy_funds(Decimal("10000.00"))

    # ── 14. 交易成本與已實現損益計算一致 ────────────────────────

    def test_14_transaction_costs_and_realized_pnl_reconciliation(self) -> None:
        """案例 14：分批買入與分批賣出後，交易成本分攤無重複扣費且總已實現損益與 PortfolioLedger 100% 守恆。"""
        ledger = PortfolioLedger(Decimal("2000000.00"))

        # 分兩批買進 2330：第一批 3000 股 @ 333.33，第二批 2000 股 @ 345.67
        b1 = make_fill("F-1401", "2330", dt.date(2026, 1, 2), 3000, Decimal("333.33"))
        b2 = make_fill("F-1402", "2330", dt.date(2026, 1, 5), 2000, Decimal("345.67"))
        for bf in (b1, b2):
            ledger.record_buy_fill(bf.symbol, bf.fill_quantity, bf.fill_price, bf.notional, bf.commission, Decimal("0"))
            self.lot_ledger.record_buy_fill(bf)

        # 分三批賣出：1000 股 @ 350.00、2500 股 @ 355.00（跨第一批與第二批）、1500 股 @ 360.00（平倉）
        s1 = make_fill("F-1403", "2330", dt.date(2026, 1, 6), 1000, Decimal("350.00"), is_sell=True)
        s2 = make_fill("F-1404", "2330", dt.date(2026, 1, 7), 2500, Decimal("355.00"), is_sell=True)
        s3 = make_fill("F-1405", "2330", dt.date(2026, 1, 8), 1500, Decimal("360.00"), is_sell=True)
        for sf in (s1, s2, s3):
            ledger.record_sell_fill(sf.symbol, sf.fill_quantity, sf.fill_price, sf.notional, sf.commission, sf.tax, 0)
            self.lot_ledger.record_sell_fill(sf)

        # 對帳驗證：持倉均為 0，且累計已實現損益精確到 0.01 完全相等
        self.lot_ledger.assert_reconciled_with_portfolio_ledger(ledger)
        self.assertEqual(self.lot_ledger.get_realized_pnl(), ledger.cumulative_realized_pnl)

        # 驗證各筆 ClosedLotTrade 之分攤買進手續費、賣出手續費與證交稅總和與原始成交紀錄完全相等（無重複扣費）
        all_closed = self.lot_ledger.get_closed_trades("2330")
        self.assertEqual(
            sum(t.allocated_entry_commission for t in all_closed),
            b1.commission + b2.commission,
        )
        self.assertEqual(
            sum(t.allocated_exit_commission for t in all_closed),
            s1.commission + s2.commission + s3.commission,
        )
        self.assertEqual(
            sum(t.allocated_exit_tax for t in all_closed),
            s1.tax + s2.tax + s3.tax,
        )

    # ── 15. 股數、金額與 NAV 守恆及除息日 NAV 變化分解核對 ───────

    def test_15_ex_dividend_nav_decomposition_and_no_double_counting(self) -> None:
        """案例 15：除息日市值減少金額與應收股利變動分開核對（含理論除息價與真實非等幅價格波動），且發放日不重複計入 NAV。"""
        ledger = PortfolioLedger(Decimal("1000000.00"))
        # 2026-01-02 買進 1000 股 @ 500.00（花費 500,713 元，現金餘 499,287 元）
        bf = make_fill("F-1501", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00"))
        ledger.record_buy_fill("2330", 1000, Decimal("500.00"), bf.notional, bf.commission, Decimal("0"))
        self.lot_ledger.record_buy_fill(bf)

        # 除息前一日 (2026-01-07) 收盤價 500.00
        nav_pre_ex = self.corp_processor.compute_nav_breakdown(
            ledger,
            close_prices={"2330": Decimal("500.00")},
            as_of_date=dt.date(2026, 1, 7),
        )
        self.assertEqual(nav_pre_ex.available_cash, Decimal("499287.00"))
        self.assertEqual(nav_pre_ex.market_value, Decimal("500000.00"))
        self.assertEqual(nav_pre_ex.dividends_receivable, Decimal("0.00"))
        self.assertEqual(nav_pre_ex.total_nav, Decimal("999287.00"))

        # 2026-01-08 除息：每股配發 20.00 元現金（0 匯費、0 稅）
        ev = CorporateActionEvent(
            event_id="CA-1501",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=dt.date(2026, 1, 8),
            payable_date=dt.date(2026, 2, 5),
            cash_dividend_per_share=Decimal("20.00"),
            announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
            provenance=make_synth_provenance(),
            is_synthetic_payable_date=True,
        )
        self.corp_processor.process_ex_dividend_event(
            ev,
            self.lot_ledger,
            as_of_time=dt.datetime(2026, 1, 8, 8, 0, tzinfo=TAIPEI_TZ),
        )

        # 情境 A：當除息日收盤價恰等於理論除息參考價 480.00 時，將市值減少與應收股利變動分開核對
        nav_ex_ref = self.corp_processor.compute_nav_breakdown(
            ledger,
            close_prices={"2330": Decimal("480.00")},
            as_of_date=dt.date(2026, 1, 8),
        )
        mv_change_ref = nav_ex_ref.market_value - nav_pre_ex.market_value
        receivable_change = nav_ex_ref.dividends_receivable - nav_pre_ex.dividends_receivable
        self.assertEqual(mv_change_ref, Decimal("-20000.00"))      # 市值減少 20,000 元
        self.assertEqual(receivable_change, Decimal("20000.00"))   # 應收股利增加 20,000 元
        self.assertEqual(nav_ex_ref.available_cash, Decimal("499287.00"))  # 可用現金不變
        self.assertEqual(nav_ex_ref.total_nav, nav_pre_ex.total_nav)       # 總 NAV 守恆為 999,287.00

        # 情境 B：真實市場價格不一定只下跌股利金額（例如部分填息收在 485.00，或貼息收在 472.00）
        nav_ex_partial_fill = self.corp_processor.compute_nav_breakdown(
            ledger,
            close_prices={"2330": Decimal("485.00")},
            as_of_date=dt.date(2026, 1, 8),
        )
        self.assertEqual(nav_ex_partial_fill.market_value - nav_pre_ex.market_value, Decimal("-15000.00"))
        self.assertEqual(nav_ex_partial_fill.dividends_receivable, Decimal("20000.00"))
        self.assertEqual(nav_ex_partial_fill.total_nav - nav_pre_ex.total_nav, Decimal("5000.00"))

        # 情境 C：發放日 (2026-02-05) 應收股利轉為可用現金後，應收股利歸零，絕不與現金重複計入 NAV
        self.corp_processor.process_due_payables(
            ledger,
            as_of_time=dt.datetime(2026, 2, 5, 8, 0, tzinfo=TAIPEI_TZ),
        )
        nav_on_pay = self.corp_processor.compute_nav_breakdown(
            ledger,
            close_prices={"2330": Decimal("485.00")},
            as_of_date=dt.date(2026, 2, 5),
        )
        self.assertEqual(nav_on_pay.available_cash, Decimal("519287.00"))   # 499,287 + 20,000
        self.assertEqual(nav_on_pay.dividends_receivable, Decimal("0.00"))  # 應收股利歸零
        self.assertEqual(nav_on_pay.market_value, Decimal("485000.00"))
        self.assertEqual(nav_on_pay.total_nav, nav_ex_partial_fill.total_nav)  # 與發放前一日總 NAV 完全相等

    # ── 16. 不支援的公司行動不得靜默略過 ────────────────────────

    def test_16_unsupported_corporate_actions_explicitly_rejected(self) -> None:
        """案例 16：股票股利、股票分割、反分割與減資必須明確拋出 UnsupportedCorporateActionError，嚴禁靜默略過。"""
        for unsupported_type in (
            CorporateActionType.STOCK_DIVIDEND,
            CorporateActionType.STOCK_SPLIT,
            CorporateActionType.REVERSE_SPLIT,
            CorporateActionType.CAPITAL_REDUCTION,
        ):
            with self.subTest(action_type=unsupported_type):
                with self.assertRaises(UnsupportedCorporateActionError):
                    CorporateActionEvent(
                        event_id=f"CA-UNSUP-{unsupported_type.value}",
                        symbol="2330",
                        action_type=unsupported_type,
                        ex_date=dt.date(2026, 1, 8),
                        payable_date=dt.date(2026, 2, 5),
                        cash_dividend_per_share=Decimal("1.00"),
                        announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
                        available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
                        provenance=make_synth_provenance(),
                        is_synthetic_payable_date=True,
                    )

    # ── 17. 時區、日期順序、無效股數與無效金額 ──────────────────

    def test_17_timezone_date_order_invalid_quantities_and_amounts(self) -> None:
        """案例 17：時區缺漏、日期順序矛盾、未成交委託、無效股數與無效金額之完整攔截檢查。"""
        # A. 無時區成交時間戳 -> 拒絕
        bad_ts_fill = FillRecord(
            fill_id="F-BAD-TZ",
            order_id="O-1",
            signal_id="S-1",
            symbol="2330",
            fill_quantity=1000,
            fill_price=Decimal("500.00"),
            notional=Decimal("500000.00"),
            commission=Decimal("713"),
            tax=Decimal("0"),
            slippage=Decimal("0"),
            timestamp=dt.datetime(2026, 1, 2, 9, 0),  # naive
        )
        with self.assertRaises(InvalidFillRecordError):
            self.lot_ledger.record_buy_fill(bad_ts_fill)

        # B. 成交金額 notional 與 price * quantity 不符 -> 拒絕
        bad_notional_fill = FillRecord(
            fill_id="F-BAD-NOTIONAL",
            order_id="O-1",
            signal_id="S-1",
            symbol="2330",
            fill_quantity=1000,
            fill_price=Decimal("500.00"),
            notional=Decimal("400000.00"),  # 錯誤金額
            commission=Decimal("713"),
            tax=Decimal("0"),
            slippage=Decimal("0"),
            timestamp=dt.datetime(2026, 1, 2, 9, 0, tzinfo=TAIPEI_TZ),
        )
        with self.assertRaises(InvalidFillRecordError):
            self.lot_ledger.record_buy_fill(bad_notional_fill)

        # C. 非法股數 (0 或負數) 或未成交委託單直接傳入 -> 拒絕
        for bad_qty in (0, -500, True):
            with self.subTest(bad_qty=bad_qty):
                f_bad = FillRecord(
                    fill_id=f"F-BAD-QTY-{bad_qty}",
                    order_id="O-1",
                    signal_id="S-1",
                    symbol="2330",
                    fill_quantity=bad_qty,  # type: ignore[arg-type]
                    fill_price=Decimal("500.00"),
                    notional=Decimal("500000.00"),
                    commission=Decimal("713"),
                    tax=Decimal("0"),
                    slippage=Decimal("0"),
                    timestamp=dt.datetime(2026, 1, 2, 9, 0, tzinfo=TAIPEI_TZ),
                )
                with self.assertRaises(InvalidFillRecordError):
                    self.lot_ledger.record_buy_fill(f_bad)

        unfilled_order = PaperOrder(
            order_id="ORD-UNFILLED",
            signal_id="SIG-U",
            symbol="2330",
            action=SignalAction.BUY,
            order_type=SignalOrderType.LIMIT,
            order_quantity=1000,
            limit_price=Decimal("500.00"),
            created_at=dt.datetime(2026, 1, 2, 8, 30, tzinfo=TAIPEI_TZ),
        )
        with self.assertRaises(InvalidFillRecordError):
            self.lot_ledger.record_buy_fill(unfilled_order)  # type: ignore[arg-type]

        # D. 公司行動日期順序錯誤：payable_date < ex_date 或 record_date < ex_date
        with self.assertRaises(CorporateActionOrderError):
            CorporateActionEvent(
                event_id="CA-BAD-ORDER-1",
                symbol="2330",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=dt.date(2026, 1, 15),
                payable_date=dt.date(2026, 1, 10),  # 早於除息日
                cash_dividend_per_share=Decimal("5.00"),
                announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
                provenance=make_synth_provenance(),
                is_synthetic_payable_date=True,
            )

        with self.assertRaises(CorporateActionOrderError):
            CorporateActionEvent(
                event_id="CA-BAD-ORDER-2",
                symbol="2330",
                action_type=CorporateActionType.CASH_DIVIDEND,
                ex_date=dt.date(2026, 1, 15),
                record_date=dt.date(2026, 1, 12),  # 早於除息日
                payable_date=dt.date(2026, 2, 10),
                cash_dividend_per_share=Decimal("5.00"),
                announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
                available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
                provenance=make_synth_provenance(),
                is_synthetic_payable_date=True,
            )

        # E. 未達發放日嘗試提前結算入帳 -> 拋出 CorporateActionOrderError
        self.lot_ledger.record_buy_fill(make_fill("F-1701", "2330", dt.date(2026, 1, 2), 1000, Decimal("500.00")))
        ev_valid = CorporateActionEvent(
            event_id="CA-1701",
            symbol="2330",
            action_type=CorporateActionType.CASH_DIVIDEND,
            ex_date=dt.date(2026, 1, 8),
            payable_date=dt.date(2026, 2, 5),
            cash_dividend_per_share=Decimal("5.00"),
            announced_at=dt.datetime(2025, 12, 20, 16, 0, tzinfo=TAIPEI_TZ),
            available_at=dt.datetime(2025, 12, 20, 16, 30, tzinfo=TAIPEI_TZ),
            provenance=make_synth_provenance(),
            is_synthetic_payable_date=True,
        )
        rec = self.corp_processor.process_ex_dividend_event(
            ev_valid,
            self.lot_ledger,
            as_of_time=dt.datetime(2026, 1, 8, 8, 0, tzinfo=TAIPEI_TZ),
        )
        assert rec is not None
        with self.assertRaises(CorporateActionOrderError):
            self.corp_processor.settle_payable_dividend(
                rec.receivable_id,
                as_of_time=dt.datetime(2026, 1, 20, 8, 0, tzinfo=TAIPEI_TZ),  # 早於 2026-02-05
            )

    # ── 18. 原始子專案保持不變驗證 ──────────────────────────────

    def test_18_subprojects_unchanged(self) -> None:
        """案例 18：確認 anti-gambling-trader-tw 與 qlib-tw-trader 之 Commit 與工作目錄完全未變。"""
        expected_commits = {
            "anti-gambling-trader-tw": "8609cef73718a056fddb74201fc304f174fa1553",
            "qlib-tw-trader": "774b3fb60d3b20b5c9ee86d25052ad2ec08e2011",
        }
        for repo_name, expected_sha in expected_commits.items():
            repo_dir = ROOT_DIR / repo_name
            head_proc = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(head_proc.stdout.strip(), expected_sha)

            status_proc = subprocess.run(
                ["git", "-C", str(repo_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(status_proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
