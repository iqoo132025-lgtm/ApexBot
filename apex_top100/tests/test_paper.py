#!/usr/bin/env python3
"""
اختبارات Paper Trading — إدارة المركز الورقي وحساب الأداء.
شغّله: python3 -m apex_top100.tests.test_paper
"""
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
from apex_top100.paper import PaperBroker, PaperConfig
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
