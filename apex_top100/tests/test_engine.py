#!/usr/bin/env python3
"""
اختبار محرك Top 100 بالكامل بدون إنترنت (مزوّد بيانات صناعي).
شغّله: python3 -m apex_top100.tests.test_engine
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from apex_top100.analysis import analyze_coin
from apex_top100.config import Top100Config
from apex_top100.engine import ApexHooks, Top100MarketEngine
from apex_top100.providers_mock import MockDataProvider
from apex_top100.regime import detect_regime
from apex_top100.snapshots import SnapshotStore
from apex_top100.telegram_signals import format_top100_signal, signal_buttons


class QuietHooks(ApexHooks):
    def __init__(self):
        self.signals, self.events, self.regimes, self.patterns = [], [], [], []

    def on_signal(self, sig): self.signals.append(sig)
    def on_list_event(self, e, r): self.events.append(e)
    def on_regime_change(self, r): self.regimes.append(r)
    def on_pattern(self, name, rows): self.patterns.append((name, rows))
    def log(self, msg): pass


def build_engine(tmpdir, n=14, days=400):
    cfg = Top100Config(universe_size=n, db_path=os.path.join(tmpdir, "t.db"),
                       cache_dir=os.path.join(tmpdir, "cache"), max_ohlcv_per_cycle=n,
                       min_score=60)
    hooks = QuietHooks()
    eng = Top100MarketEngine(cfg=cfg, provider=MockDataProvider(n=n, days=days),
                             store=SnapshotStore(cfg.db_path), hooks=hooks)
    return eng, hooks


class TestRegime(unittest.TestCase):
    def test_bear_and_bull(self):
        up = [100 * (1.003 ** i) for i in range(400)]
        down = [100 * (0.996 ** i) for i in range(400)]
        coins_up = {f"c{i}": [50 * (1.003 ** j) for j in range(300)] for i in range(15)}
        coins_dn = {f"c{i}": [50 * (0.996 ** j) for j in range(300)] for i in range(15)}
        self.assertIn(detect_regime(up, coins_up).raw_regime, ("BULL", "EARLY BULL", "OVERHEATED"))
        self.assertEqual(detect_regime(down, coins_dn).raw_regime, "BEAR")

    def test_hysteresis_requires_confirmation(self):
        up = [100 * (1.003 ** i) for i in range(400)]
        coins = {f"c{i}": [50 * (1.003 ** j) for j in range(300)] for i in range(15)}
        prev = {"regime": "BEAR", "metrics": {"raw_regime": "BEAR", "raw_streak": 1}}
        first = detect_regime(up, coins, previous=prev)
        self.assertEqual(first.regime, "BEAR")                 # قراءة واحدة لا تكفي
        prev2 = {"regime": "BEAR", "metrics": first.metrics}
        second = detect_regime(up, coins, previous=prev2)
        self.assertNotEqual(second.regime, "BEAR")             # القراءة الثانية تؤكد الانتقال
        self.assertEqual(second.changed_from, "BEAR")

    def test_policy_never_buys_alone(self):
        """التصنيف يغيّر الأوزان والحجم فقط — البوابة البنيوية تبقى شرطاً."""
        up = [100 * (1.003 ** i) for i in range(400)]
        coins = {f"c{i}": [50 * (1.003 ** j) for j in range(300)] for i in range(15)}
        reg = detect_regime(up, coins)
        cfg = Top100Config()
        provider = MockDataProvider(n=10)
        universe = provider.fetch_top_markets()
        weak = next(c for c in universe if provider.profiles[c.symbol] == "down")
        sig = analyze_coin(weak, provider.fetch_ohlcv(weak), up, up, cfg, reg)
        self.assertFalse(sig.tradable)          # سوق صاعد + عملة ضعيفة = لا إشارة


class TestSnapshots(unittest.TestCase):
    def test_entries_exits_and_rank_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(os.path.join(tmp, "s.db"))
            provider = MockDataProvider(n=12, days=380)
            base = date(2026, 1, 1)
            for d in range(20):
                provider.advance(1)
                coins = provider.fetch_top_markets()
                store.take_snapshot(coins, btc_price=next(c.price for c in coins if c.symbol == "BTC"),
                                    date=(base + timedelta(days=d)).isoformat())
            self.assertEqual(len(store.snapshot_dates(50)), 20)
            entries = store.newly_entered(days=3650)
            self.assertTrue(entries, "يجب تسجيل دخول العملة المتأخرة إلى Top 100")
            sym = entries[0]["symbol"]
            self.assertTrue(store.rank_series(entries[0]["coin_id"], 30))
            # تاريخ الخارجين محفوظ ولا يُحذف
            rows = store.conn.execute("SELECT COUNT(*) c FROM tracking").fetchone()["c"]
            self.assertGreaterEqual(rows, 12)
            movers = store.top_rank_movers(days=20)
            self.assertIsInstance(movers, list)
            self.assertIsInstance(store.outperformers_vs_btc(days=20), list)
            self.assertIsInstance(store.volume_leading_price(days=10), list)


class TestSignalAndEngine(unittest.TestCase):
    def test_full_cycle_and_telegram_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            out = eng.run_once()
            self.assertTrue(out["signals"], "يجب إنتاج تحليلات")
            self.assertIsNotNone(out["regime"])
            sig = out["signals"][0]
            self.assertGreaterEqual(sig.score, 0)
            self.assertLess(sig.invalidation, sig.entry_low)
            self.assertLess(sig.entry_low, sig.entry_high)
            self.assertLess(sig.tp1, sig.tp2)
            self.assertLess(sig.tp2, sig.tp3)
            self.assertGreaterEqual(sig.position_pct, eng.cfg.min_position_pct)
            self.assertLessEqual(sig.position_pct, eng.cfg.max_position_pct)

            text = format_top100_signal(sig)
            for needle in ["🔥 APEX TOP-100 SIGNAL", "Rank #", "Market Regime:", "APEX Score:",
                           "Trend:", "vs BTC:", "Volume:", "Momentum:", "Risk:",
                           "Entry Zone:", "Invalidation:", "TP1 / TP2 / TP3:", "Position Size:"]:
                self.assertIn(needle, text)
            btns = signal_buttons(sig)["inline_keyboard"][0]
            self.assertEqual([b["text"] for b in btns], ["CHART", "ANALYSIS", "BUY"])

    def test_dedup_and_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            eng.run_once()
            first = len(hooks.signals)
            eng.run_once()                        # نفس الدورة مباشرة
            self.assertEqual(len(hooks.signals), first, "لا تتكرر نفس الإشارة داخل فترة التهدئة")

    def test_capital_manager_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            class Veto(QuietHooks):
                def should_trade(self, sig): return False
            eng.hooks = Veto()
            out = eng.run_once()
            self.assertEqual(out["sent"], [], "اعتراض إدارة رأس المال يمنع الإرسال")

    def test_multiday_simulation(self):
        """محاكاة 25 يوماً: لقطات يومية + أنماط ترتيب."""
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            base = date(2026, 2, 1)
            for d in range(25):
                eng.provider.advance(1)
                eng.refresh_universe(force=True)
                eng.ensure_daily_snapshot(date=(base + timedelta(days=d)).isoformat())
            eng.load_ohlcv(eng.universe)
            eng.update_regime()
            sigs = eng.analyze_all()
            self.assertTrue(sigs)
            self.assertEqual(len(eng.store.snapshot_dates(60)), 25)
            patterns = eng.scan_patterns()
            self.assertIn("rank_movers", patterns)
            with_rank = [s for s in sigs if s.metrics.get("rank_change_30d") is not None]
            self.assertTrue(with_rank, "التحليل يجب أن يستخدم تغيّر الترتيب من اللقطات")


if __name__ == "__main__":
    unittest.main(verbosity=2)
