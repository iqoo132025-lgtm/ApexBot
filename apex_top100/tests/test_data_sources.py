#!/usr/bin/env python3
"""
اختبارات جلب البيانات الحية — الحصص والمصادر ومنع السقوط الصامت على كاش قديم.
شغّله: python3 -m apex_top100.tests.test_data_sources
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_top100 import data_sources as ds
from apex_top100.analysis import analyze_coin
from apex_top100.config import Top100Config
from apex_top100.data_sources import (CoinInfo, CycleReport, HttpCache, LiveDataProvider,
                                      OHLCV, RateLimiter)
from apex_top100.paper import PaperBroker, PaperConfig


class FakeResponse:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("http://x", code, "err", hdrs, io.BytesIO(b""))


class PatchedNet:
    """يستبدل urlopen داخل data_sources بسلسلة ردود، ويلغي النوم."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        self._urlopen = ds.urllib.request.urlopen
        self._sleep = ds.time.sleep
        ds.time.sleep = lambda *_: None

        def fake(req, timeout=None):
            self.calls.append(req.full_url if hasattr(req, "full_url") else str(req))
            r = self.responses.pop(0) if self.responses else self.responses
            if isinstance(r, Exception):
                raise r
            return FakeResponse(r)

        ds.urllib.request.urlopen = fake
        return self

    def __exit__(self, *a):
        ds.urllib.request.urlopen = self._urlopen
        ds.time.sleep = self._sleep
        return False


def cache_dir():
    return tempfile.mkdtemp()


class TestRateLimiter(unittest.TestCase):
    def test_spaces_requests_apart(self):
        rl = RateLimiter(0.05)
        t0 = time.time()
        rl.wait(); rl.wait(); rl.wait()
        self.assertGreaterEqual(time.time() - t0, 0.09)

    def test_zero_interval_never_sleeps(self):
        rl = RateLimiter(0)
        t0 = time.time()
        for _ in range(50):
            rl.wait()
        self.assertLess(time.time() - t0, 0.05)


class TestHttpCacheProvenance(unittest.TestCase):
    def test_fresh_then_cache(self):
        c = HttpCache(cache_dir(), retries=1)
        with PatchedNet([{"v": 1}]):
            first = c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=300)
        self.assertEqual(first.provenance, "fresh")
        second = c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=300)
        self.assertEqual(second.provenance, "cache")
        self.assertEqual(second.data, {"v": 1})

    def test_failure_does_not_fall_back_to_stale_silently(self):
        c = HttpCache(cache_dir(), retries=1)
        with PatchedNet([{"v": 1}]):
            c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=300)
        # الكاش موجود لكن انتهى عمره، والطلب فشل: بلا allow_stale يجب أن يرتفع الخطأ
        with PatchedNet([http_error(500)]):
            with self.assertRaises(RuntimeError):
                c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=0)

    def test_stale_is_returned_only_when_asked_and_is_labelled(self):
        c = HttpCache(cache_dir(), retries=1)
        with PatchedNet([{"v": 1}]):
            c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=300)
        with PatchedNet([http_error(500)]):
            res = c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=0, allow_stale=True)
        self.assertEqual(res.provenance, "stale")
        self.assertEqual(res.data, {"v": 1})

    def test_stale_older_than_limit_is_refused(self):
        c = HttpCache(cache_dir(), retries=1)
        with PatchedNet([{"v": 1}]):
            c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=300)
        old = time.time() - 10_000
        os.utime(c._path("k"), (old, old))
        with PatchedNet([http_error(500)]):
            with self.assertRaises(RuntimeError):
                c.fetch("https://api.coingecko.com/x", cache_key="k", ttl=0,
                        allow_stale=True, max_stale_age=3600)

    def test_repeated_429_opens_the_circuit_and_stops_calling(self):
        c = HttpCache(cache_dir(), retries=5, max_consecutive_429=2)
        with PatchedNet([http_error(429), http_error(429), {"v": 1}]) as net:
            with self.assertRaises(RuntimeError):
                c.fetch("https://api.coingecko.com/a", cache_key="a", ttl=0)
            self.assertEqual(len(net.calls), 2, "يتوقف عند فتح القاطع لا يستنفد المحاولات")
        self.assertTrue(c.circuit_open("https://api.coingecko.com/b"))
        with PatchedNet([{"v": 2}]) as net:
            with self.assertRaises(RuntimeError):
                c.fetch("https://api.coingecko.com/b", cache_key="b", ttl=0)
            self.assertEqual(net.calls, [], "القاطع مفتوح: لا يُرسل أي طلب")
        self.assertEqual(c.errors["429"], 2)

    def test_circuit_is_per_host(self):
        c = HttpCache(cache_dir(), retries=3, max_consecutive_429=1)
        with PatchedNet([http_error(429)]):
            with self.assertRaises(RuntimeError):
                c.fetch("https://api.coingecko.com/a", cache_key="a", ttl=0)
        self.assertTrue(c.circuit_open("https://api.coingecko.com/z"))
        self.assertFalse(c.circuit_open("https://api.binance.com/z"))

    def test_begin_cycle_resets_circuit_and_errors(self):
        c = HttpCache(cache_dir(), retries=3, max_consecutive_429=1)
        with PatchedNet([http_error(429)]):
            with self.assertRaises(RuntimeError):
                c.fetch("https://api.coingecko.com/a", cache_key="a", ttl=0)
        c.begin_cycle()
        self.assertFalse(c.circuit_open("https://api.coingecko.com/a"))
        self.assertEqual(dict(c.errors), {})


def coin(symbol="SOL", coin_id="solana", rank=4):
    return CoinInfo(coin_id=coin_id, symbol=symbol, name=symbol, rank=rank,
                    price=100.0, market_cap=1e10, volume_24h=1e9)


def klines(n=120):
    return [[(1700000000 + i * 86400) * 1000, "100", "105", "95", "102", "1",
             0, "1000", 0, 0, 0, 0] for i in range(n)]


class TestCoinGeckoBudgetDefaults(unittest.TestCase):
    """
    القيم التي تحمي الطبقة المجانية. 2.5 ثانية (~24 طلباً/دقيقة) ضربت 429
    بعد 14 طلباً في أول تشغيل حي، فالمباعدة الآن 6 ثوانٍ (~10 طلبات/دقيقة).
    """

    def test_spacing_and_budget(self):
        cfg = Top100Config()
        self.assertEqual(cfg.cg_min_interval_sec, 6.0)
        self.assertLessEqual(60.0 / cfg.cg_min_interval_sec, 10.0,
                             "أكثر من 10 طلبات/دقيقة يعيدنا إلى 429")
        # الميزانية والقاطع لم يُمسّا: التشديد على المباعدة وحدها
        self.assertEqual(cfg.cg_max_calls_per_cycle, 25)
        self.assertEqual(cfg.cg_max_consecutive_429, 2)
        self.assertFalse(cfg.cg_fetch_volumes)

    def test_binance_is_not_slowed_by_the_coingecko_spacing(self):
        """التأخير يقع على عملات fallback وحدها."""
        cfg = Top100Config()
        self.assertLess(cfg.binance_min_interval_sec, 1.0)
        self.assertGreater(cfg.cg_min_interval_sec, cfg.binance_min_interval_sec * 10)


class TestProviderSourceOrder(unittest.TestCase):
    def provider(self, **kw):
        cfg = Top100Config(cache_dir=cache_dir(), request_retries=1, **kw)
        return LiveDataProvider(cfg)

    def test_binance_is_primary_and_coingecko_is_not_touched(self):
        p = self.provider()
        p._binance_pairs = {"SOLUSDT"}
        p.begin_cycle()
        with PatchedNet([klines()]) as net:
            o = p.fetch_ohlcv(coin(), days=120)
        self.assertEqual(o.source, "binance")
        self.assertEqual(len(net.calls), 1)
        self.assertNotIn("coingecko", net.calls[0])
        self.assertEqual(p.report.sources["binance"], 1)
        self.assertEqual(p.report.cg_calls, 0, "لا يُستهلك شيء من حصة CoinGecko")

    def test_binance_unavailable_is_reported(self):
        p = self.provider()
        p.begin_cycle()
        with PatchedNet([http_error(451)]):
            p._binance_symbols()
        self.assertFalse(p.report.binance_available)

    def test_coingecko_budget_stops_further_calls(self):
        p = self.provider(cg_max_calls_per_cycle=2)
        p._binance_pairs = set()          # لا زوج على Binance ⇒ كل شيء على CoinGecko
        p.begin_cycle()
        rows = [[(1700000000 + i * 86400) * 1000, 100, 105, 95, 102] for i in range(60)]
        with PatchedNet([rows, rows, rows, rows]) as net:
            a = p.fetch_ohlcv(coin("AAA", "aaa"), days=90)
            b = p.fetch_ohlcv(coin("BBB", "bbb"), days=90)
            c = p.fetch_ohlcv(coin("CCC", "ccc"), days=90)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertIsNone(c, "الميزانية نفدت ⇒ لا طلب ثالث")
        self.assertEqual(len(net.calls), 2)
        self.assertEqual(p.report.cg_calls, 2)
        self.assertIn("CCC", p.report.skipped_symbols)

    def test_volumes_call_is_off_by_default(self):
        p = self.provider()
        p._binance_pairs = set()
        p.begin_cycle()
        rows = [[(1700000000 + i * 86400) * 1000, 100, 105, 95, 102] for i in range(60)]
        with PatchedNet([rows]) as net:
            o = p.fetch_ohlcv(coin(), days=90)
        self.assertEqual(o.source, "coingecko_ohlc")
        self.assertEqual(len(net.calls), 1, "طلب واحد لكل عملة على CoinGecko لا طلبان")

    def test_stale_candles_are_labelled_in_the_report(self):
        p = self.provider()
        p._binance_pairs = {"SOLUSDT"}
        p.begin_cycle()
        with PatchedNet([klines()]):
            p.fetch_ohlcv(coin(), days=120)          # يملأ الكاش
        p.cfg.ohlcv_cache_ttl_sec = 0                 # أجبر إعادة الطلب
        p.begin_cycle()
        with PatchedNet([http_error(500)]):
            o = p.fetch_ohlcv(coin(), days=120)
        self.assertTrue(o.stale)
        self.assertEqual(o.provenance, "stale")
        self.assertIn("SOL", p.report.stale_symbols)
        self.assertFalse(p.report.healthy)


class TestStaleNeverTrades(unittest.TestCase):
    @staticmethod
    def _series(n=200):
        return [100 + i * 0.5 for i in range(n)]

    def _ohlcv(self, **kw):
        c = self._series()
        return OHLCV(symbol="SOL", opens=c[:], highs=[x * 1.02 for x in c],
                     lows=[x * 0.98 for x in c], closes=c[:], volumes=[1e6] * len(c),
                     times=[1700000000 + i * 86400 for i in range(len(c))], **kw)

    def test_stale_signal_is_not_tradable(self):
        cfg = Top100Config()
        btc = self._series()
        sig = analyze_coin(coin(), self._ohlcv(provenance="stale", age_sec=7200),
                           btc_closes=btc, eth_closes=btc, cfg=cfg, regime=None)
        self.assertEqual(sig.data_quality, "stale")
        self.assertFalse(sig.tradable)
        self.assertTrue(any("كاش قديم" in f for f in sig.flags))

    def test_fresh_equivalent_is_tradable(self):
        cfg = Top100Config()
        btc = self._series()
        sig = analyze_coin(coin(), self._ohlcv(), btc_closes=btc, eth_closes=btc,
                           cfg=cfg, regime=None)
        self.assertEqual(sig.data_quality, "ohlc")
        self.assertTrue(sig.tradable, "الضبط المقابل: نفس البيانات وهي طازجة تُتداول")

    def test_paper_refuses_a_stale_signal(self):
        import sqlite3
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        b = PaperBroker(conn, PaperConfig(slippage_pct=0.0))
        from apex_top100.analysis import Top100Signal
        sig = Top100Signal(symbol="SOL", coin_id="solana", name="SOL", rank=4, price=100.0,
                           score=90.0, regime="BULL", trend_emoji="🟢", trend_label="Up",
                           vs_btc_pct=1.0, vs_eth_pct=1.0, volume_state="Increasing",
                           momentum_label="Strong", risk="Low", entry_low=98.0,
                           entry_high=101.0, entry_note="", invalidation=90.0,
                           tp1=110.0, tp2=125.0, tp3=140.0, position_pct=3.0,
                           data_quality="stale")
        self.assertIsNone(b.open_from_signal(sig))
        self.assertEqual(b.open_positions(), [])


class TestCycleReport(unittest.TestCase):
    def test_healthy_only_when_nothing_degraded(self):
        r = CycleReport(cg_budget=10)
        self.assertTrue(r.healthy)
        r.stale_symbols.append("SOL")
        self.assertFalse(r.healthy)

    def test_429_makes_the_cycle_unhealthy(self):
        r = CycleReport(cg_budget=10)
        r.http_errors["429"] = 1
        self.assertFalse(r.healthy)

    def test_summary_names_the_degradations(self):
        r = CycleReport(cg_budget=10)
        r.binance_available = False
        r.cg_circuit_open = True
        r.stale_symbols.append("SOL")
        r.price_only_symbols.append("DOT")
        text = r.summary()
        for expected in ("Binance غير متاح", "القاطع مفتوح", "SOL", "DOT"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
