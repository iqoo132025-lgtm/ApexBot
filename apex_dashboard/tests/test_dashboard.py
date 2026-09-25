#!/usr/bin/env python3
"""
اختبارات لوحة APEX — قراءة فقط، ونفس أرقام الوسيط الورقي.
شغّله: python3 -m apex_dashboard.tests.test_dashboard
"""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_dashboard import api as A
from apex_dashboard.api import DashboardError, DashboardReader
from apex_dashboard.server import make_server
from apex_top100 import indicators as I
from apex_top100.analysis import Top100Signal
from apex_top100.paper import Bar, PaperBroker, PaperConfig
from apex_top100.snapshots import SnapshotStore

DAY = 86400


def make_signal(symbol, price, **kw) -> Top100Signal:
    base = dict(
        symbol=symbol, coin_id=symbol.lower(), name=symbol, rank=5, price=price,
        score=80.0, regime="BULL", trend_emoji="🟢", trend_label="Uptrend",
        vs_btc_pct=10.0, vs_eth_pct=5.0, volume_state="Increasing", momentum_label="Strong",
        risk="Medium", entry_low=98.0, entry_high=101.0, entry_note="عند السوق",
        invalidation=90.0, tp1=110.0, tp2=125.0, tp3=140.0, position_pct=3.0,
    )
    base.update(kw)
    return Top100Signal(**base)


def kline(ts, o, h, l, c, qv=1000.0):
    """صف Binance الخام كما يخزنه المحرك في الكاش."""
    return [ts * 1000, str(o), str(h), str(l), str(c), "10", ts * 1000 + DAY * 1000 - 1, str(qv)]


def build_fixture(tmp: str):
    """
    قاعدة بمخطط المحرك الحقيقي وأربع حالات:
      AAA  OPEN فوراً ثم TP1 ونقل الوقف للتعادل
      BBB  PENDING ثم FILLED في شمعة لاحقة ثم كسر الإبطال → CLOSED
      CCC  PENDING ما زال معلّقاً
      DDD  EXPIRED بانتهاء نافذة الدخول
    """
    db = os.path.join(tmp, "apex_top100.db")
    cache = os.path.join(tmp, ".apex_cache")
    os.makedirs(cache)
    store = SnapshotStore(db)
    store.save_regime("BULL", 79.0, {"x": 1})
    b = PaperBroker(store.conn, PaperConfig())
    t0 = (1_790_000_000 // DAY) * DAY          # بداية يوم UTC

    import apex_top100.paper as P
    real_now = P._now
    try:
        P._now = lambda: t0 + 3600              # الإشارات داخل شمعة t0
        for sym, price, kw in (("AAA", 100.0, {}),
                               ("BBB", 105.0, {}),
                               ("CCC", 120.0, {}),
                               ("DDD", 130.0, {})):
            sig = make_signal(sym, price, **kw)
            b.open_from_signal(sig)
            store.record_signal(sig.to_dict())
    finally:
        P._now = real_now

    series = {
        # AAA: دخل فوراً عند 100، الشمعة التالية لمست 111 (TP1)
        "AAA": [(t0 - DAY, 95, 101, 94, 99), (t0, 99, 101, 98, 100),
                (t0 + DAY, 100, 111, 101, 108), (t0 + 2 * DAY, 108, 112, 104, 109)],
        # BBB: نزل إلى 101 في t0+1 فنُفّذ، ثم كسر 90 في t0+2
        "BBB": [(t0, 104, 106, 103, 105), (t0 + DAY, 104, 105, 100, 102),
                (t0 + 2 * DAY, 101, 102, 88, 89)],
        # CCC: بقي فوق المنطقة يومين (أقل من نافذة الثلاثة أيام)
        "CCC": [(t0, 119, 121, 118, 120), (t0 + DAY, 120, 122, 115, 118)],
        # DDD: بقي فوق المنطقة أربعة أيام → EXPIRED
        "DDD": [(t0 + i * DAY, 130, 132, 125, 131) for i in range(0, 5)],
    }
    b.apply_candles({s.lower(): [Bar(*r) for r in rows] for s, rows in series.items()})
    b.record_equity()
    for sym, rows in series.items():
        with open(os.path.join(cache, f"kl_{sym}USDT_1d.json"), "w") as f:
            json.dump([kline(*r) for r in rows], f)
    store.close()
    return db, cache, t0


def digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db, self.cache, self.t0 = build_fixture(self._tmp.name)
        self.reader = DashboardReader(self.db, self.cache)

    def tearDown(self):
        self._tmp.cleanup()

    def pos(self, symbol):
        return next(p for p in self.reader.positions() if p["symbol"] == symbol)


class TestReadOnly(Base):
    def test_every_endpoint_leaves_the_database_byte_identical(self):
        before = digest(self.db)
        mtime = os.path.getmtime(self.db)
        cache_before = {n: digest(os.path.join(self.cache, n)) for n in os.listdir(self.cache)}
        r = self.reader
        r.status(); r.positions(); r.signals(); r.performance()
        for p in r.positions():
            r.position(p["id"]); r.chart(p["symbol"], p["id"])
        r.chart("AAA"); r.candles("ZZZ")
        self.assertEqual(digest(self.db), before)
        self.assertEqual(os.path.getmtime(self.db), mtime)
        self.assertEqual({n: digest(os.path.join(self.cache, n)) for n in os.listdir(self.cache)},
                         cache_before)
        self.assertFalse(os.path.exists(self.db + "-journal"))

    def test_connection_refuses_writes_at_the_sqlite_level(self):
        conn = self.reader.connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE paper_positions SET status='CLOSED'")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE x (a INTEGER)")
        finally:
            conn.close()

    def test_missing_database_is_503_and_not_created(self):
        missing = os.path.join(self._tmp.name, "nope.db")
        with self.assertRaises(DashboardError) as cm:
            DashboardReader(missing, self.cache).status()
        self.assertEqual(cm.exception.status, 503)
        self.assertFalse(os.path.exists(missing))

    def test_path_with_spaces_opens(self):
        d = os.path.join(self._tmp.name, "dir with space#1")
        os.makedirs(d)
        import shutil
        shutil.copy(self.db, os.path.join(d, "a.db"))
        self.assertEqual(DashboardReader(os.path.join(d, "a.db"), self.cache).status()["mode"], "PAPER")


class TestNumbersMatchPaperBroker(Base):
    def test_status_and_performance_are_the_brokers_own_numbers(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        b = PaperBroker(conn, PaperConfig())
        stats, eq, dd = b.stats(), b.equity(), b.max_drawdown()
        conn.close()
        s = self.reader.status()
        self.assertEqual(s["equity"], eq)
        self.assertEqual(s["max_dd_pct"], dd["max_dd_pct"])
        self.assertAlmostEqual(s["pnl"], round(eq - 1000.0, 2))
        perf = self.reader.performance()["stats"]
        for k, v in stats.items():
            self.assertEqual(perf[k], v, k)

    def test_counts_include_expired(self):
        c = self.reader.status()["counts"]
        self.assertEqual(c, {"OPEN": 1, "PENDING": 1, "CLOSED": 1, "EXPIRED": 1})
        exp = self.reader.positions("EXPIRED")
        self.assertEqual([p["symbol"] for p in exp], ["DDD"])
        self.assertEqual(exp[0]["exit_reason"], "entry_window_expired")
        self.assertEqual(self.reader.performance()["stats"]["expired"], {"entry_window_expired": 1})

    def test_mode_is_paper_and_auto_execute_comes_from_the_bridge_default(self):
        s = self.reader.status()
        self.assertEqual(s["mode"], "PAPER")
        self.assertIs(s["auto_execute"], False)
        self.assertEqual(s["regime"]["regime"], "BULL")

    def test_unrealized_uses_last_cached_close_and_remaining_size(self):
        a = self.pos("AAA")
        # 60% باقٍ بعد TP1، آخر إغلاق 109
        expected = (109 / a["entry"] - 1) * 100 * 0.6
        self.assertAlmostEqual(a["unrealized_pct"], round(expected, 3), places=3)
        self.assertEqual(a["mark_source"], "apex_cache_last_close")
        self.assertIsNone(self.pos("CCC")["unrealized_pct"])

    def test_bad_status_filter(self):
        with self.assertRaises(DashboardError):
            self.reader.positions("OPENED")


class TestIndicators(unittest.TestCase):
    def test_series_end_on_the_engines_own_values(self):
        vals = [100 + (i * 7 % 13) - (i % 5) * 1.5 for i in range(80)]
        self.assertAlmostEqual(A.sma_series(vals, 20)[-1], I.sma(vals, 20))
        self.assertAlmostEqual(A.ema_series(vals, 21)[-1], I.ema(vals, 21))
        self.assertAlmostEqual(A.rsi_series(vals, 14)[-1], I.rsi(vals, 14))
        # كل نقطة وسطى أيضاً
        for i in (30, 55):
            self.assertAlmostEqual(A.rsi_series(vals, 14)[i], I.rsi(vals[:i + 1], 14))
            self.assertAlmostEqual(A.ema_series(vals, 21)[i], I.ema(vals[:i + 1], 21))

    def test_short_series_have_no_values(self):
        self.assertEqual(A.rsi_series([1, 2, 3], 14), [None] * 3)
        self.assertEqual(A.sma_series([1, 2], 3), [None, None])


class TestTimeline(Base):
    def stages(self, symbol):
        tl = self.reader.position(self.pos(symbol)["id"])["timeline"]
        return {s["stage"]: s for s in tl}, [s["stage"] for s in tl]

    def test_order_and_paper_latency(self):
        st, order = self.stages("AAA")
        self.assertEqual(order, ["SIGNAL", "APPROVED", "PENDING", "FILLED", "TP1", "SL→BE",
                                 "TP2", "TP3", "CLOSED"])
        self.assertTrue(all(s["latency_ms"] is None for s in st.values()))
        self.assertIn("auto_execute=False", st["APPROVED"]["note"])

    def test_immediate_fill_then_tp1_and_breakeven(self):
        st, _ = self.stages("AAA")
        self.assertEqual(st["PENDING"]["state"], "skipped")
        self.assertEqual(st["FILLED"]["state"], "done")
        self.assertEqual(st["TP1"]["state"], "done")
        self.assertEqual(st["TP1"]["source"], "inferred")
        self.assertEqual(st["TP1"]["ts"], self.t0 + DAY)      # أول شمعة High ≥ 110
        self.assertEqual(st["SL→BE"]["state"], "done")
        self.assertEqual(st["TP2"]["state"], "waiting")
        self.assertEqual(st["CLOSED"]["state"], "waiting")

    def test_pending_then_filled_then_stopped(self):
        st, _ = self.stages("BBB")
        self.assertEqual(st["PENDING"]["state"], "done")
        self.assertEqual(st["FILLED"]["ts"], self.t0 + DAY)
        self.assertEqual(st["CLOSED"]["state"], "done")
        self.assertEqual(st["CLOSED"]["note"], "invalidation")
        self.assertEqual(st["TP1"]["state"], "skipped")
        self.assertEqual(st["SL→BE"]["state"], "skipped")

    def test_still_pending(self):
        st, _ = self.stages("CCC")
        self.assertEqual(st["PENDING"]["state"], "active")
        self.assertEqual(st["FILLED"]["state"], "waiting")

    def test_expired(self):
        st, order = self.stages("DDD")
        self.assertEqual(order[-1], "EXPIRED")
        self.assertEqual(st["FILLED"]["state"], "skipped")
        self.assertEqual(st["EXPIRED"]["note"], "entry_window_expired")


class TestChart(Base):
    def test_chart_has_candles_indicators_levels_and_markers(self):
        a = self.pos("AAA")
        c = self.reader.chart("aaa", a["id"])
        self.assertEqual(c["pair"], "AAAUSDT")
        self.assertEqual(len(c["candles"]), 4)
        self.assertEqual(c["candles"][-1]["volume"], 1000.0)   # quote volume
        self.assertEqual(set(c["indicators"]), {"sma20", "sma50", "ema21", "rsi14"})
        kinds = {lv["key"] for lv in c["levels"]}
        self.assertTrue({"entry", "tp1", "tp2", "tp3", "invalidation", "stop"} <= kinds)
        times = {b["time"] for b in c["candles"]}
        marks = {m["stage"]: m for m in c["markers"]}
        self.assertEqual(set(marks), {"SIGNAL", "FILLED", "TP1"})
        self.assertEqual(marks["SIGNAL"]["time"], self.t0)       # الإشارة داخل شمعة t0
        self.assertTrue(all(m["time"] in times for m in c["markers"]))

    def test_chart_without_position_picks_latest_for_symbol(self):
        self.assertEqual(self.reader.chart("BBB")["position"]["symbol"], "BBB")

    def test_position_must_belong_to_symbol(self):
        with self.assertRaises(DashboardError):
            self.reader.chart("AAA", self.pos("BBB")["id"])

    def test_invalid_symbol_cannot_escape_cache_dir(self):
        for bad in ("../x", "A/B", "", "a" * 30):
            with self.assertRaises(DashboardError):
                self.reader.candles(bad)

    def test_missing_cache_explains_itself(self):
        c = self.reader.candles("ZZZ")
        self.assertEqual(c["candles"], [])
        self.assertIn("note", c)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.srv = make_server(self.reader, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, method="GET"):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_api_routes(self):
        pid = self.pos("AAA")["id"]
        for path in ("/api/status", "/api/positions", "/api/positions?status=EXPIRED",
                     f"/api/positions/{pid}", "/api/signals?limit=5", "/api/chart/AAA",
                     f"/api/chart/AAA?position_id={pid}", "/api/performance"):
            code, ctype, body = self.req(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(ctype.startswith("application/json"), path)
            json.loads(body)
        self.assertEqual(len(json.loads(self.req("/api/signals?limit=2")[2])["signals"]), 2)

    def test_static(self):
        code, ctype, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"APEX LIVE / PAPER", body)
        self.assertEqual(self.req("/static/app.js")[0], 200)
        self.assertEqual(self.req("/static/../api.py")[0], 404)
        self.assertEqual(self.req("/static/%2e%2e/api.py")[0], 404)

    def test_writes_are_refused(self):
        before = digest(self.db)
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.req("/api/positions", m)[0], 405, m)
        self.assertEqual(digest(self.db), before)

    def test_errors(self):
        self.assertEqual(self.req("/api/nope")[0], 404)
        self.assertEqual(self.req("/api/chart/..%2Fx")[0], 400)
        self.assertEqual(self.req("/api/positions/999999")[0], 404)
        self.assertEqual(self.req("/api/positions/abc")[0], 400)
        self.assertEqual(self.req("/api/positions?status=BAD")[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
