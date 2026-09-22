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
from apex_top100.tests.test_paper import make_signal


class QuietHooks(ApexHooks):
    def __init__(self):
        self.signals, self.events, self.regimes, self.patterns = [], [], [], []
        self.logs = []

    def on_signal(self, sig): self.signals.append(sig)
    def on_list_event(self, e, r): self.events.append(e)
    def on_regime_change(self, r): self.regimes.append(r)
    def on_pattern(self, name, rows): self.patterns.append((name, rows))
    def log(self, msg, level="info"): self.logs.append((level, msg))


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


class TestUniverseIntegrity(unittest.TestCase):
    """ترتيب CoinGecko الحقيقي محفوظ، والاستبعاد يقع على الإشارات لا على الكون."""

    def _provider_with_rows(self, rows, provenance="fresh", age_sec=0.0):
        from apex_top100.data_sources import FetchResult, LiveDataProvider
        cfg = Top100Config(universe_size=5, cache_dir=tempfile.mkdtemp())
        p = LiveDataProvider(cfg)
        p.http.fetch = lambda *a, **k: FetchResult(rows, provenance, age_sec)
        p.http.get_json = lambda *a, **k: rows
        return p, cfg

    def test_real_rank_is_preserved(self):
        rows = [
            {"id": "bitcoin",  "symbol": "btc",  "name": "Bitcoin",  "market_cap_rank": 1, "market_cap": 1e12, "current_price": 60000, "total_volume": 3e10},
            {"id": "ethereum", "symbol": "eth",  "name": "Ethereum", "market_cap_rank": 2, "market_cap": 4e11, "current_price": 3000,  "total_volume": 1e10},
            {"id": "tether",   "symbol": "usdt", "name": "Tether",   "market_cap_rank": 3, "market_cap": 1e11, "current_price": 1.0,   "total_volume": 5e10},
            {"id": "solana",   "symbol": "sol",  "name": "Solana",   "market_cap_rank": 4, "market_cap": 9e10, "current_price": 150,   "total_volume": 4e9},
            {"id": "wbtc",     "symbol": "wbtc", "name": "Wrapped Bitcoin", "market_cap_rank": 5, "market_cap": 8e10, "current_price": 60000, "total_volume": 3e8},
            {"id": "faraway",  "symbol": "far",  "name": "Faraway",  "market_cap_rank": 105, "market_cap": 1e8, "current_price": 2, "total_volume": 1e6},
        ]
        provider, cfg = self._provider_with_rows(rows)
        coins = provider.fetch_top_markets(5)
        by_symbol = {c.symbol: c for c in coins}

        # الترتيب كما تعطيه CoinGecko، بلا إعادة ترقيم
        self.assertEqual([c.rank for c in coins], [1, 2, 3, 4, 5])
        self.assertEqual(by_symbol["SOL"].rank, 4, "SOL يجب أن يبقى #4 لا أن يصعد بعد حذف USDT")
        # العملات خارج أول n لا تدخل الكون
        self.assertNotIn("FAR", by_symbol)
        # Stablecoins والمغلَّفة داخل الكون (فيدخلان rank_history) لكن خارج الإشارات
        self.assertIn("USDT", by_symbol)
        self.assertTrue(by_symbol["USDT"].is_stablecoin)
        self.assertTrue(by_symbol["WBTC"].is_wrapped)
        self.assertFalse(by_symbol["USDT"].signalable(cfg))
        self.assertFalse(by_symbol["WBTC"].signalable(cfg))
        self.assertTrue(by_symbol["SOL"].signalable(cfg))

    def test_snapshot_keeps_stablecoins_but_signals_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            eng.run_once()
            snap = eng.store.snapshot(eng.store.last_snapshot_date())
            self.assertIn("usdt", snap, "Stablecoin يجب أن تبقى في تاريخ الترتيب")
            self.assertNotIn("USDT", [s.symbol for s in eng.analyze_all()],
                             "Stablecoin لا تدخل التحليل ولا الإشارات")


class TestDataQuality(unittest.TestCase):
    """بيانات بلا High/Low حقيقية لا تُنتج إشارة."""

    def test_price_only_coin_is_not_tradable(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            eng.cfg.min_score = 1
            out = eng.run_once()
            price_only = [s for s in out["signals"] if s.data_quality == "price_only"]
            self.assertTrue(price_only, "المزوّد الصناعي يوفّر عملة price_only للاختبار")
            for s in price_only:
                self.assertFalse(s.tradable, f"{s.symbol}: إشارة مبنية على High/Low غير حقيقية")
                self.assertIsNone(s.metrics.get("atr_pct"))
            self.assertTrue(all(s.data_quality == "ohlc" for s in out["sent"]),
                            "لا تُرسل إشارة إلا على شموع حقيقية")


class _FakeReport:
    """بديل مبسّط لـ CycleReport لاختبار بوابة جودة الدورة."""

    def __init__(self, healthy=True, cg_circuit_open=False, http_errors=None,
                 stale_symbols=None):
        self._healthy = healthy
        self.cg_circuit_open = cg_circuit_open
        self.http_errors = http_errors or {}
        self.stale_symbols = stale_symbols or []

    @property
    def healthy(self):
        return self._healthy


class TestMockProviderDeterminism(unittest.TestCase):
    """
    المزوّد الصناعي يأخذ seed ليولّد بيانات ثابتة، فيجب ألا يعتمد على hash()
    المُعشَّى لكل عملية. كان يفعل، فسقط test_policy_never_buys_alone على main
    في نحو 1 من 400 تشغيل بسلسلة "down" خرجت باتجاه أسبوعي داعم.
    """

    def test_symbol_hash_is_stable_across_processes(self):
        from apex_top100.providers_mock import _stable_hash

        self.assertEqual(_stable_hash("AVAX"), 3859748292)
        self.assertEqual(_stable_hash("BTC"), 3176990918)

    def test_series_are_reproducible(self):
        from apex_top100.providers_mock import MockDataProvider

        a, b = MockDataProvider(n=10), MockDataProvider(n=10)
        self.assertEqual(a._series, b._series)
        self.assertAlmostEqual(a._series["AVAX"][-1], 1.852859, places=6,
                               msg="تغيّرت بيانات المزوّد الصناعي — الاختبارات المبنية عليها لم تعد ثابتة")

    def test_the_weak_coin_stays_untradable(self):
        """الحالة التي سقطت على main: العملة ذات الملف down لا تصبح tradable."""
        from apex_top100.providers_mock import MockDataProvider

        up = [100 * (1.003 ** i) for i in range(400)]
        coins = {f"c{i}": [50 * (1.003 ** j) for j in range(300)] for i in range(15)}
        reg = detect_regime(up, coins)
        provider = MockDataProvider(n=10)
        weak = next(c for c in provider.fetch_top_markets()
                    if provider.profiles[c.symbol] == "down")
        sig = analyze_coin(weak, provider.fetch_ohlcv(weak), up, up, Top100Config(), reg)
        self.assertFalse(sig.tradable)
        self.assertIn("الاتجاه الأسبوعي غير داعم", sig.flags)


class TestHeldPositionsKeepTheirCandles(unittest.TestCase):
    """
    المركز القائم يُدار من شموع الدورة وحدها: `update_paper` يقرأ من
    `ohlcv_cache`، فعملة لم تُسحب شموعها هذه الدورة لا تتحرك لها MFE/MAE،
    ولا يُفحص وقفها ولا أهدافها، ولا تنتهي نافذة دخولها لأن فحص الانتهاء
    داخل معالجة الشمعة. ميزانية `max_ohlcv_per_cycle` أصغر من الكون، فبلا
    أولوية للمركز القائم تتجمّد الصفقة بصمت وتُقرأ كأنها معلومة عن السوق.
    """

    def _engine(self, tmp, n=14, budget=3):
        cfg = Top100Config(universe_size=n, db_path=os.path.join(tmp, "t.db"),
                           cache_dir=os.path.join(tmp, "cache"),
                           max_ohlcv_per_cycle=budget, min_score=60)
        eng = Top100MarketEngine(cfg=cfg, provider=MockDataProvider(n=n),
                                 store=SnapshotStore(cfg.db_path), hooks=QuietHooks())
        eng.refresh_universe(force=True)
        return eng

    @staticmethod
    def _worst_ranked(eng):
        """أسوأ عملة ترتيباً — خارج الميزانية بلا شك."""
        return sorted(eng.signalable_universe(), key=lambda c: c.rank)[-1]

    @staticmethod
    def _hold(eng, coin):
        """مركز ورقي مفتوح على هذه العملة بأهداف بعيدة لا تُضرب في شمعة واحدة."""
        price = coin.price
        sig = make_signal(coin.symbol, price=price, rank=coin.rank,
                          entry_low=price * 0.98, entry_high=price * 1.02,
                          invalidation=price * 0.90,
                          tp1=price * 1.2, tp2=price * 1.4, tp3=price * 1.6)
        return eng.paper.open_from_signal(sig)

    def test_a_held_coin_outside_the_budget_is_pulled_into_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            coin = self._worst_ranked(eng)
            self.assertNotIn(coin.coin_id,
                             [c.coin_id for c in eng._priority_order()[:eng.cfg.max_ohlcv_per_cycle]],
                             "العملة يجب أن تكون خارج الميزانية قبل فتح المركز")
            self._hold(eng, coin)
            self.assertIn(coin.coin_id,
                          [c.coin_id for c in eng._priority_order()[:eng.cfg.max_ohlcv_per_cycle]])

    def test_holding_moves_the_coin_first_and_disturbs_nothing_else(self):
        """الترتيب النسبي لبقية الكون لا يتغير — الإصلاح يضيف طبقة ولا يعيد ترتيب شيء."""
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            before = [c.coin_id for c in eng._priority_order()]
            coin = self._worst_ranked(eng)
            self._hold(eng, coin)
            after = [c.coin_id for c in eng._priority_order()]
            self.assertEqual(after[0], coin.coin_id)
            self.assertEqual(after[1:], [cid for cid in before if cid != coin.coin_id])

    def test_a_pending_order_counts_as_held(self):
        """الأمر المعلّق يحتاج الشموع أكثر: بلا شمعة لا يدخل ولا تنتهي نافذته."""
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            coin = self._worst_ranked(eng)
            price = coin.price
            sig = make_signal(coin.symbol, price=price * 1.30, rank=coin.rank,
                              entry_low=price * 0.98, entry_high=price * 1.02,
                              invalidation=price * 0.90,
                              tp1=price * 1.2, tp2=price * 1.4, tp3=price * 1.6)
            eng.paper.open_from_signal(sig)
            self.assertEqual(eng.paper.open_positions()[0]["status"], "PENDING")
            self.assertIn(coin.coin_id, eng.held_coin_ids())
            self.assertIn(coin.coin_id,
                          [c.coin_id for c in eng._priority_order()[:eng.cfg.max_ohlcv_per_cycle]])

    def test_a_low_ranked_position_is_actually_managed_by_a_full_cycle(self):
        """الاختبار الذي يهم: دورة كاملة تحرّك MFE/MAE لمركز خارج الميزانية."""
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            coin = self._worst_ranked(eng)
            self._hold(eng, coin)
            pos = eng.paper.open_positions()[0]
            self.assertEqual(pos["status"], "OPEN")
            self.assertEqual(pos["mfe_pct"], 0.0)
            eng.provider.advance(1)
            eng.run_once()
            after = [p for p in eng.paper.open_positions() if p["coin_id"] == coin.coin_id][0]
            self.assertIn(coin.coin_id, eng.ohlcv_cache, "شموع المركز تُسحب في الدورة")
            self.assertGreater(after["mfe_pct"], 0.0, "المركز تُدار حركته فعلاً")
            self.assertLess(after["mae_pct"], 0.0)

    def test_order_is_unchanged_when_nothing_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            self.assertEqual(eng.held_coin_ids(), set())
            ranks = [c.rank for c in eng._priority_order()]
            self.assertEqual(ranks, sorted(ranks), "بلا مراكز، الترتيب يبقى بالترتيب السوقي")

    def test_priority_order_survives_a_disabled_paper_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp)
            eng.paper = None
            self.assertEqual(eng.held_coin_ids(), set())
            self.assertTrue(eng._priority_order())

    def test_every_held_position_is_fetched_even_beyond_the_budget(self):
        """
        الميزانية سقف على الاكتشاف لا على الإدارة: ثلاثة مراكز وميزانية اثنين
        تعني ثلاث عمليات سحب، لا اثنتين ومركزاً متروكاً. إسقاط مركز توفيراً
        لطلب يحوّله إلى صف ميت: لا وقف ولا هدف ولا انتهاء نافذة.
        """
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp, budget=2)
            held = sorted(eng.signalable_universe(), key=lambda c: c.rank)[-3:]
            for coin in held:
                self._hold(eng, coin)
            picked = [c.coin_id for c in eng.cycle_coins()]
            for coin in held:
                self.assertIn(coin.coin_id, picked)
            self.assertEqual(len(picked), 3, "لا اكتشاف يُضاف حين تستوعب المراكز الميزانية")

    def test_all_held_positions_are_managed_by_a_cycle_beyond_the_budget(self):
        """نفس الحالة عبر دورة كاملة: الثلاثة تتحرك لهم MFE/MAE فعلاً."""
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp, budget=2)
            held = sorted(eng.signalable_universe(), key=lambda c: c.rank)[-3:]
            for coin in held:
                self._hold(eng, coin)
            eng.provider.advance(1)
            eng.run_once()
            rows = {p["coin_id"]: p for p in eng.paper.open_positions()}
            for coin in held:
                self.assertIn(coin.coin_id, eng.ohlcv_cache, f"{coin.symbol} بلا شموع")
                self.assertGreater(rows[coin.coin_id]["mfe_pct"], 0.0,
                                   f"{coin.symbol} مركز قائم لم يُدَر")

    def test_discovery_takes_only_what_the_positions_leave(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp, budget=5)
            held = sorted(eng.signalable_universe(), key=lambda c: c.rank)[-2:]
            for coin in held:
                self._hold(eng, coin)
            picked = eng.cycle_coins()
            self.assertEqual(len(picked), 5, "المجموع يبقى عند الميزانية ما دام فيها متسع")
            ids = [c.coin_id for c in picked]
            for coin in held:
                self.assertIn(coin.coin_id, ids)

    def test_a_cycle_warns_when_positions_eat_the_whole_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp, budget=2)
            for coin in sorted(eng.signalable_universe(), key=lambda c: c.rank)[-3:]:
                self._hold(eng, coin)
            eng.run_once()
            warns = [m for lvl, m in eng.hooks.logs if lvl == "warn" and "ميزانية الشموع" in m]
            self.assertTrue(warns, "توقّف الاكتشاف يجب أن يُقال صراحة لا أن يُكتشف لاحقاً")

    def test_no_warning_while_discovery_still_has_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(tmp, budget=5)
            self._hold(eng, self._worst_ranked(eng))
            eng.run_once()
            warns = [m for lvl, m in eng.hooks.logs if lvl == "warn" and "ميزانية الشموع" in m]
            self.assertFalse(warns)


class TestLoggingSignature(unittest.TestCase):
    """
    regression لـ TypeError: ApexV2Bridge.log() takes 2 positional arguments.
    كل تطبيق لـ ApexHooks.log يجب أن يقبل مستوى التسجيل، لا واحد فقط.
    """

    def _implementations(self):
        import importlib
        import inspect
        import pkgutil

        import apex_top100

        for mod in pkgutil.iter_modules(apex_top100.__path__):
            if mod.name.startswith("tests"):
                continue
            importlib.import_module(f"apex_top100.{mod.name}")
        found = [ApexHooks]
        stack = [ApexHooks]
        while stack:
            for sub in stack.pop().__subclasses__():
                if sub not in found:
                    found.append(sub)
                    stack.append(sub)
        return [(c, c.__dict__["log"]) for c in found if "log" in c.__dict__]

    def test_every_hook_log_accepts_a_level(self):
        import inspect

        impls = self._implementations()
        self.assertGreaterEqual(len(impls), 3, "يجب فحص كل تطبيقات log لا واحد")
        for cls, fn in impls:
            with self.subTest(cls=cls.__name__):
                sig = inspect.signature(fn)
                try:
                    sig.bind(object(), "msg", "warn")
                except TypeError as e:
                    self.fail(f"{cls.__module__}.{cls.__name__}.log لا يقبل مستوى: {e}")

    def test_bridge_log_survives_a_single_argument_logger(self):
        """logger خارجي قديم يقبل رسالة واحدة — التحذير يُسجَّل ولا يرفع استثناء."""
        from apex_top100.integration import ApexV2Bridge

        seen = []

        class OldLogger:
            def log(self, msg):
                seen.append(msg)

        bridge = ApexV2Bridge(logger=OldLogger())
        bridge.log("تحذير", "warn")
        self.assertEqual(len(seen), 1, "يجب السقوط إلى توقيع الوسيط الواحد")

    def test_every_warn_call_site_runs(self):
        """المسارات الثلاثة التي تستدعي log(..., 'warn') داخل run_once لا تنكسر."""
        from apex_top100.integration import ApexV2Bridge

        with tempfile.TemporaryDirectory() as tmp:
            eng, _ = build_engine(tmp)
            eng.hooks = ApexV2Bridge()          # الجسر الذي انكسر في التشغيل الحي
            eng.provider.report = _FakeReport(
                healthy=False, cg_circuit_open=True,
                http_errors={"429": 2}, stale_symbols=["ada"])
            eng.provider.report.binance_available = False
            eng.provider.report.summary = lambda: "تقرير"
            eng.run_once()                       # كان يرفع TypeError عند engine.py:297


class TestFailClosedOnDegradedCycle(unittest.TestCase):
    """
    دورة بجودة بيانات منقوصة لا تُنتج إشارات ولا مراكز ورقية جديدة.
    الإشارات الثمانية في التشغيل الحي خرجت قبل تقييم report.healthy.
    """

    def _degrade(self, eng):
        report = _FakeReport(healthy=False, cg_circuit_open=True, http_errors={"429": 2})
        report.binance_available = True
        report.summary = lambda: "تقرير"
        eng.provider.report = report
        return report

    def test_no_signals_and_no_paper_entries_when_cycle_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            eng.cfg.min_score = 1               # كل شيء فوق العتبة لولا البوابة
            self._degrade(eng)
            out = eng.run_once()

            self.assertEqual(out["sent"], [], "لا إشارة من دورة غير سليمة")
            self.assertEqual(hooks.signals, [], "on_signal لا يُستدعى")
            self.assertEqual(eng.paper.open_positions(), [], "لا مركز ورقي جديد")
            self.assertEqual(eng.store.conn.execute(
                "SELECT COUNT(*) FROM signals").fetchone()[0], 0,
                "لا تُسجَّل إشارة من دورة محجوبة")
            self.assertIsNotNone(out["blocked"], "سبب الحجب يظهر في نتيجة الدورة")
            self.assertTrue(any(lvl == "warn" and "دورة غير سليمة" in msg
                                for lvl, msg in hooks.logs),
                            f"يجب تسجيل سبب الحجب: {hooks.logs}")

    def test_diagnostics_still_run_on_a_blocked_cycle(self):
        """الحجب يمنع الإشارات فقط — التحليل واللقطة والتصنيف تستمر للتشخيص."""
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            eng.cfg.min_score = 1
            self._degrade(eng)
            out = eng.run_once()

            self.assertTrue(out["signals"], "التحليل يستمر ونتائجه تشخيصية")
            self.assertIsNotNone(out["regime"], "تصنيف حالة السوق يستمر")
            self.assertIsNotNone(eng.store.last_snapshot_date(), "اللقطة اليومية تُحفظ")

    def test_healthy_cycle_still_emits(self):
        """ضبط مقابل: نفس المحرك بدورة سليمة يُرسل إشاراته كالمعتاد."""
        with tempfile.TemporaryDirectory() as tmp:
            eng, hooks = build_engine(tmp)
            eng.cfg.min_score = 1
            report = _FakeReport(healthy=True)
            report.binance_available = True
            report.summary = lambda: "تقرير"
            eng.provider.report = report
            out = eng.run_once()

            self.assertTrue(out["sent"], "دورة سليمة يجب أن تُرسل إشارات")
            self.assertIsNone(out["blocked"])
            self.assertTrue(eng.paper.open_positions(), "المراكز الورقية تُفتح كالمعتاد")

    def test_provider_without_a_report_is_not_blocked(self):
        """مزوّد لا يصدر تقريراً (المزوّد الصناعي) لا يُحجب."""
        with tempfile.TemporaryDirectory() as tmp:
            eng, _ = build_engine(tmp)
            eng.cfg.min_score = 1
            self.assertFalse(hasattr(eng.provider, "report"))
            self.assertIsNone(eng.cycle_blocked())
            self.assertTrue(eng.run_once()["sent"])

    def test_flag_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng, _ = build_engine(tmp)
            eng.cfg.min_score = 1
            eng.cfg.require_healthy_cycle = False
            self._degrade(eng)
            self.assertIsNone(eng.cycle_blocked())
            self.assertTrue(eng.run_once()["sent"], "إيقاف البوابة يعيد السلوك القديم")


if __name__ == "__main__":
    unittest.main(verbosity=2)
