#!/usr/bin/env python3
"""
اختبارات Paper Trading — إدارة المركز الورقي وحساب الأداء.
شغّله: python3 -m apex_top100.tests.test_paper
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_top100.analysis import Top100Signal
from apex_top100.config import Top100Config
from apex_top100.engine import Top100MarketEngine
from apex_top100.data_sources import OHLCV
from apex_top100.paper import Bar, PaperBroker, PaperConfig, bars_from_ohlcv
from apex_top100.providers_mock import MockDataProvider
from apex_top100.snapshots import SnapshotStore


def make_signal(symbol="SOL", price=100.0, **kw) -> Top100Signal:
    base = dict(
        symbol=symbol, coin_id=symbol.lower(), name=symbol, rank=5, price=price,
        score=80.0, regime="EARLY BULL", trend_emoji="🟢", trend_label="Uptrend",
        vs_btc_pct=10.0, vs_eth_pct=5.0, volume_state="Increasing", momentum_label="Strong",
        risk="Medium", entry_low=98.0, entry_high=101.0, entry_note="عند السوق",
        invalidation=90.0, tp1=110.0, tp2=125.0, tp3=140.0, position_pct=3.0,
    )
    base.update(kw)
    return Top100Signal(**base)


def broker(**cfg) -> PaperBroker:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return PaperBroker(conn, PaperConfig(slippage_pct=0.0, **cfg))


class TestPaperLifecycle(unittest.TestCase):
    def test_opens_at_market_inside_entry_zone(self):
        b = broker()
        self.assertIsNotNone(b.open_from_signal(make_signal(price=100.0)))
        pos = b.open_positions()[0]
        self.assertEqual(pos["status"], "OPEN")
        self.assertAlmostEqual(pos["entry"], 100.0, places=4)

    def test_pending_when_price_above_zone(self):
        b = broker()
        b.open_from_signal(make_signal(price=120.0))
        self.assertEqual(b.open_positions()[0]["status"], "PENDING")
        b.update({"sol": 100.0})                      # رجع إلى المنطقة
        self.assertEqual(b.open_positions()[0]["status"], "OPEN")

    def test_pending_expires_when_invalidation_breaks_first(self):
        b = broker()
        b.open_from_signal(make_signal(price=120.0))
        b.update({"sol": 85.0})                       # كسر الإبطال قبل الدخول
        self.assertEqual(b.open_positions(), [])
        row = b.conn.execute("SELECT * FROM paper_positions").fetchone()
        self.assertEqual(row["status"], "EXPIRED")

    def test_stop_loss_is_minus_one_r(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))  # مخاطرة 10 نقاط
        b.update({"sol": 90.0})
        closed = b.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "invalidation")
        self.assertAlmostEqual(closed["realized_r"], -1.0, places=2)

    def test_partial_exits_and_breakeven_stop(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.update({"sol": 111.0})                      # TP1
        pos = b.open_positions()[0]
        self.assertAlmostEqual(pos["remaining"], 0.60, places=2)
        self.assertGreaterEqual(pos["stop"], pos["entry"], "الوقف ينتقل للتعادل بعد TP1")
        b.update({"sol": 100.0})                      # رجوع إلى التعادل
        closed = b.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "stop_breakeven")
        # 40% × 1R من TP1 فقط
        self.assertAlmostEqual(closed["realized_r"], 0.4, places=2)

    def test_all_targets_close_position(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.update({"sol": 145.0})                      # الأهداف الثلاثة دفعة واحدة
        closed = b.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "targets_reached")
        expected = 0.40 * 1.0 + 0.30 * 2.5 + 0.30 * 4.0   # (110-100)/10, (125-100)/10, (140-100)/10
        self.assertAlmostEqual(closed["realized_r"], expected, places=2)
        self.assertEqual(b.open_positions(), [])

    def test_max_hold_exit(self):
        b = broker(max_hold_days=10)
        b.open_from_signal(make_signal(price=100.0))
        b.conn.execute("UPDATE paper_positions SET opened_at=?", (int(time.time()) - 11 * 86400,))
        b.update({"sol": 104.0})
        self.assertEqual(b.closed_positions()[0]["exit_reason"], "max_hold")

    def test_mfe_and_mae_tracked(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.update({"sol": 108.0})
        b.update({"sol": 94.0})
        pos = b.open_positions()[0]
        self.assertAlmostEqual(pos["mfe_pct"], 8.0, places=1)
        self.assertAlmostEqual(pos["mae_pct"], -6.0, places=1)

    def test_price_only_signal_never_enters(self):
        b = broker()
        self.assertIsNone(b.open_from_signal(make_signal(data_quality="price_only")))
        self.assertIsNone(b.open_from_signal(make_signal(tradable=False)))
        self.assertEqual(b.open_positions(), [])

    def test_no_duplicate_position_per_coin(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.open_from_signal(make_signal(price=100.0))
        self.assertEqual(len(b.open_positions()), 1)


class TestCandleExecution(unittest.TestCase):
    """
    واقعية التنفيذ: الإدارة على شموع OHLC لكل فترة منذ آخر تحديث،
    وسياسة صريحة عندما تلمس الشمعة الهدف والوقف معاً.
    """

    @staticmethod
    def bars(*rows):
        """rows: (يوم، افتتاح، أعلى، أدنى، إغلاق) — الأزمنة بعد زمن الإشارة."""
        t0 = int(time.time())
        return [Bar(t0 + d * 86400, o, h, l, c) for d, o, h, l, c in rows]

    def test_stop_hit_between_cycles_is_a_loss(self):
        """
        الحالة التي كشفها المراجع: SOL عند 100، بين دورتين هبط إلى 89 فضرب الوقف 90
        ثم ارتد إلى 111. لقطة السعر ترى 111 فتحتسب TP1؛ الشمعة ترى الوقف.
        """
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        events = b.apply_candles({"sol": self.bars((1, 100.0, 111.0, 89.0, 111.0))})
        closed = b.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "invalidation")
        self.assertAlmostEqual(closed["realized_r"], -1.0, places=2)
        self.assertEqual([e["event"] for e in events], ["CLOSED"])
        self.assertNotIn("tp1", closed["hits"])

        # للمقارنة: اللقطة وحدها كانت ستعطي نتيجة معاكسة
        b2 = broker()
        b2.open_from_signal(make_signal(price=100.0))
        b2.update({"sol": 111.0})
        self.assertIn("tp1", b2.open_positions()[0]["hits"])

    def test_tp_and_stop_in_same_candle_assumes_stop_first(self):
        """الغموض: الشمعة لمست الأهداف الثلاثة والوقف. الافتراض المحافظ = الوقف أولاً."""
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars((1, 100.0, 145.0, 89.0, 120.0))})
        closed = b.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "invalidation")
        self.assertAlmostEqual(closed["realized_r"], -1.0, places=2)
        self.assertEqual(closed["hits"], "[]", "لا هدف يُحتسب عندما يُفترض الوقف أولاً")

    def test_target_alone_in_candle_is_booked(self):
        """ضبط مقابل الاختبار السابق: بلا لمس الوقف تُحتسب الأهداف من High."""
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars((1, 101.0, 126.0, 100.5, 120.0))})
        pos = b.open_positions()[0]
        self.assertIn("tp1", pos["hits"])
        self.assertIn("tp2", pos["hits"])
        self.assertAlmostEqual(pos["remaining"], 0.30, places=2)

    def test_gap_down_through_stop_exits_at_open_not_at_stop(self):
        """فجوة تفتح تحت الوقف: الخروج عند الافتتاح — خسارة أسوأ من 1R، وهذا هو الواقع."""
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars((1, 80.0, 95.0, 78.0, 94.0))})
        closed = b.closed_positions()[0]
        self.assertAlmostEqual(closed["exit_price"], 80.0, places=2)
        self.assertAlmostEqual(closed["realized_r"], -2.0, places=2)

    def test_gap_up_books_at_target_price_then_breakeven_stop(self):
        """فتحت فوق TP1: يُحتسب عند سعر الهدف لا عند الافتتاح الأعلى، ثم الوقف للتعادل."""
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars((1, 115.0, 115.0, 89.0, 92.0))})
        closed = b.closed_positions()[0]
        self.assertIn("tp1", closed["hits"])
        self.assertEqual(closed["exit_reason"], "stop_breakeven")
        self.assertAlmostEqual(closed["realized_r"], 0.4, places=2)

    def test_mfe_and_mae_come_from_high_and_low(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars((1, 100.0, 108.0, 94.0, 100.0))})
        pos = b.open_positions()[0]
        self.assertAlmostEqual(pos["mfe_pct"], 8.0, places=1)
        self.assertAlmostEqual(pos["mae_pct"], -6.0, places=1)

    def test_stopped_candle_does_not_credit_its_high_as_mfe(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars((1, 100.0, 140.0, 89.0, 100.0))})
        closed = b.closed_positions()[0]
        self.assertAlmostEqual(closed["mfe_pct"], 0.0, places=1)
        self.assertAlmostEqual(closed["mae_pct"], -11.0, places=1)

    def test_pending_fills_intrabar_then_stops_in_same_candle(self):
        """الشمعة نزلت من 120 إلى 89: الدخول عند حد المنطقة ثم الوقف — لا خروج بلا خسارة."""
        b = broker()
        b.open_from_signal(make_signal(price=120.0))
        events = b.apply_candles({"sol": self.bars((1, 120.0, 121.0, 89.0, 95.0))})
        self.assertEqual([e["event"] for e in events], ["FILLED", "CLOSED"])
        closed = b.closed_positions()[0]
        self.assertAlmostEqual(closed["entry"], 101.0, places=2)
        self.assertAlmostEqual(closed["realized_r"], -1.0, places=2)

    def test_bars_are_not_replayed_but_forming_bar_updates(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        forming = self.bars((1, 100.0, 105.0, 98.0, 104.0))
        self.assertEqual(b.apply_candles({"sol": forming}), [])
        self.assertEqual(b.apply_candles({"sol": forming}), [], "لا تُعاد معالجة نفس الشمعة")
        grown = [Bar(forming[0].ts, 100.0, 112.0, 98.0, 111.0)]   # نفس الشمعة وقد اتسعت
        events = b.apply_candles({"sol": grown})
        # القاع 98 تحت وقف التعادل الذي نشأ من TP1 في نفس الشمعة → يُفترض الوقف بعده
        self.assertEqual([e["event"] for e in events], ["TP1", "CLOSED"])
        self.assertEqual(b.apply_candles({"sol": grown}), [], "لا تتكرر أحداث نفس الشمعة")
        self.assertAlmostEqual(b.closed_positions()[0]["realized_r"], 0.4, places=2)

    def test_candles_before_the_signal_are_ignored(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        t0 = int(time.time())
        past = [Bar(t0 - 3 * 86400, 100.0, 105.0, 80.0, 82.0)]    # هبوط سابق للإشارة
        self.assertEqual(b.apply_candles({"sol": past}), [])
        self.assertEqual(b.open_positions()[0]["status"], "OPEN")

    def test_multi_bar_sequence_stops_at_the_right_bar(self):
        b = broker()
        b.open_from_signal(make_signal(price=100.0))
        b.apply_candles({"sol": self.bars(
            (1, 100.0, 104.0, 99.0, 103.0),
            (2, 103.0, 112.0, 102.0, 111.0),     # TP1 → الوقف للتعادل
            (3, 111.0, 113.0, 95.0, 96.0),       # رجوع إلى التعادل
            (4, 96.0, 150.0, 95.0, 149.0),       # لا يُحتسب: الصفقة أُغلقت
        )})
        closed = b.closed_positions()[0]
        self.assertEqual(closed["exit_reason"], "stop_breakeven")
        self.assertEqual(json.loads(closed["hits"]), ["tp1"])
        self.assertAlmostEqual(closed["realized_r"], 0.4, places=2)

    def test_price_only_ohlcv_yields_no_bars(self):
        ohlcv = OHLCV(symbol="DOT", opens=[1, 2], highs=[1, 2], lows=[1, 2],
                      closes=[1, 2], times=[1, 2], price_only=True)
        self.assertEqual(bars_from_ohlcv(ohlcv), [])

    def test_engine_manages_paper_from_candles(self):
        cfg = Top100Config(db_path=os.path.join(tempfile.mkdtemp(), "p.db"),
                           cache_dir=tempfile.mkdtemp(), universe_size=12, min_score=55)
        eng = Top100MarketEngine(cfg, provider=MockDataProvider(), hooks=None)
        eng.run_once()
        self.assertTrue(eng.paper.open_positions())
        pos = eng.paper.open_positions()[0]
        self.assertGreater(pos["last_bar_ts"], 0, "الإدارة تبدأ من زمن الإشارة لا من تاريخ العملة")
        # شمعة جديدة بعد الإشارة تُدار فعلاً
        nxt = int(time.time()) + 86400
        ev = eng.paper.apply_candles({
            pos["coin_id"]: [Bar(nxt, pos["entry"], pos["entry"],
                                 pos["invalidation"] * 0.9, pos["invalidation"] * 0.9)]})
        self.assertEqual([e["event"] for e in ev], ["CLOSED"])


class TestPaperStats(unittest.TestCase):
    def test_stats_and_report(self):
        b = broker()
        b.open_from_signal(make_signal("SOL", price=100.0))
        b.update({"sol": 145.0})                      # رابحة
        b.open_from_signal(make_signal("ADA", price=100.0))
        b.update({"ada": 90.0})                       # خاسرة
        s = b.stats()
        self.assertEqual(s["trades"], 2)
        self.assertEqual(s["win_rate"], 50.0)
        self.assertGreater(s["total_r"], 0)
        self.assertIn("EARLY BULL", s["by_regime"])
        self.assertIn("FORWARD TEST", b.report())
        self.assertNotEqual(b.equity(), 1000.0)


class TestEngineIntegration(unittest.TestCase):
    def test_engine_opens_and_manages_paper_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Top100Config(universe_size=14, db_path=os.path.join(tmp, "t.db"),
                               cache_dir=os.path.join(tmp, "c"), max_ohlcv_per_cycle=14,
                               min_score=55)
            eng = Top100MarketEngine(cfg=cfg, provider=MockDataProvider(n=14),
                                     store=SnapshotStore(cfg.db_path))
            out = eng.run_once()
            self.assertIsNotNone(eng.paper)
            self.assertTrue(out["sent"], "نحتاج إشارات لاختبار المسار الورقي")
            self.assertTrue(eng.paper.open_positions(), "كل إشارة تفتح مركزاً ورقياً")
            self.assertIsNotNone(out["paper_stats"])
            self.assertTrue(eng.paper.equity_curve(), "منحنى رأس المال يُسجَّل يومياً")
            for p in eng.paper.open_positions():
                self.assertNotEqual(p["data_quality"], "price_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
