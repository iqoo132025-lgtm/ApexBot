#!/usr/bin/env python3
"""
تقرير جاهزية Forward Test (scripts/check_forward_test_readiness.py):
للقراءة فقط، أرقامه أرقام PaperBroker نفسها، وبواباته تقيس حجم العيّنة ونظافتها لا الاستراتيجية.
قواعد بيانات مؤقتة فقط — لا قاعدة الإنتاج. شغّله: python3 -m apex_top100.tests.test_forward_test_readiness
"""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_top100.paper import PaperBroker, PaperConfig
from apex_top100.snapshots import SnapshotStore
from apex_top100.tests.tmpdb import TmpDbTestCase

SCRIPT = os.path.join(ROOT, "scripts", "check_forward_test_readiness.py")
_spec = importlib.util.spec_from_file_location("check_forward_test_readiness", SCRIPT)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

DAY = 86400
START = int(datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp())
CLEAN = R.CLEAN_DATA_SINCE_TS + 3600


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(TmpDbTestCase):
    def setUp(self):
        self.tmp = self.tmp_dir()
        self.db = os.path.join(self.tmp, "ft.db")
        store = SnapshotStore(self.db)          # نفس المخطط الحقيقي (CREATE هنا في الاختبار فقط)
        PaperBroker(store.conn, PaperConfig())
        self.conn = store.conn
        self.n = 0
        self.addCleanup(store.close)

    def pos(self, status, signaled, opened=None, closed=None, r=0.0, pct=0.0, size=5.0, hits=(),
            reason=None, regime="BULL", mfe=0.0, mae=0.0, last_bar=None, symbol=None):
        self.n += 1
        self.conn.execute(
            "INSERT INTO paper_positions (coin_id, symbol, status, opened_at, signaled_at, closed_at,"
            " entry_low, entry_high, entry, invalidation, stop, tp1, tp2, tp3, size_pct, remaining,"
            " realized_r, realized_pct, mfe_pct, mae_pct, score, regime, exit_reason, hits, last_bar_ts)"
            " VALUES (?,?,?,?,?,?,1,1,1,0.9,0.9,1.1,1.2,1.3,?,1,?,?,?,?,80,?,?,?,?)",
            (f"c{self.n}", symbol or f"S{self.n}", status, opened, signaled, closed, size, r, pct, mfe, mae,
             regime, reason, json.dumps(list(hits)), last_bar if last_bar is not None else signaled))

    def heartbeat(self, ts, regime="BULL"):
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute("INSERT OR REPLACE INTO regime_history (date, ts, regime, score, metrics)"
                          " VALUES (?,?,?,70,'{}')", (d, int(ts), regime))

    def small_sample(self, now):
        self.pos("CLOSED", START, START + 60, START + 2 * DAY, r=-1.0, pct=-7.5, reason="invalidation", mae=-10)
        self.pos("CLOSED", CLEAN, CLEAN + 60, CLEAN + DAY, r=0.7, pct=4.3, hits=["tp1"],
                 reason="stop_breakeven", mfe=11, mae=-2)
        self.pos("CLOSED", CLEAN, CLEAN + 60, CLEAN + DAY, r=0.3, pct=3.8, hits=["tp1"],
                 reason="stop_breakeven", mfe=9.5, mae=-0.5, regime="OVERHEATED")
        self.pos("EXPIRED", CLEAN, closed=CLEAN + 3 * DAY, reason="entry_window_expired")
        self.pos("OPEN", now - DAY, now - DAY, mfe=5, mae=-3, last_bar=now - 3600)
        self.pos("PENDING", now - DAY, last_bar=now - 3600)
        for d in (0, 1, 4, 6):
            self.heartbeat(START + d * DAY)
        self.heartbeat(now - 600)
        self.conn.commit()

    def collect(self, now, **kw):
        conn = R.connect_ro(self.db)
        self.addCleanup(conn.close)
        return R.collect(conn, now=now, **kw)


class TestReadOnly(Base):
    def test_report_leaves_the_database_byte_identical(self):
        now = START + 10 * DAY
        self.small_sample(now)
        before = _sha(self.db)
        m = self.collect(now)
        R.evaluate(m)
        R.render(m, R.evaluate(m))
        self.assertEqual(_sha(self.db), before)

    def test_connection_refuses_writes(self):
        conn = R.connect_ro(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x (a)")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("UPDATE paper_positions SET status='CLOSED'")

    def test_missing_database_is_an_error_and_is_not_created(self):
        path = os.path.join(self.tmp, "nope.db")
        with self.assertRaises(R.ReadinessError):
            R.connect_ro(path)
        self.assertFalse(os.path.exists(path))

    def test_cli_json_exit_zero_and_no_write(self):
        now_sample = START + 10 * DAY
        self.small_sample(now_sample)
        before = _sha(self.db)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run([sys.executable, SCRIPT, "--db", self.db, "--json", "--no-health"],
                           capture_output=True, timeout=120, env=env, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(out["metrics"]["counts"]["CLOSED"], 3)
        self.assertIn(out["overall"], ("DATA COLLECTION", "READY FOR FORMAL REVIEW"))
        self.assertEqual(_sha(self.db), before)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["ft.db"], "التقرير لا ينشئ ملفات")

    def test_cli_missing_db_exits_2(self):
        p = subprocess.run([sys.executable, SCRIPT, "--db", os.path.join(self.tmp, "x.db"), "--no-health"],
                           capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 2)


class TestMetrics(Base):
    def test_numbers_are_the_brokers_own(self):
        now = START + 10 * DAY
        self.small_sample(now)
        m = self.collect(now)
        b = PaperBroker.__new__(PaperBroker)
        b.conn, b.cfg = self.conn, PaperConfig()
        s = b.stats()
        self.assertEqual(m["equity"], b.equity())
        self.assertEqual(m["max_drawdown_pct"], b.max_drawdown()["max_dd_pct"])
        self.assertEqual(m["closed_trades"], s["trades"])
        self.assertEqual(m["win_rate_pct"], s["win_rate"])
        self.assertEqual(m["r"]["avg"], s["avg_r"])
        self.assertEqual(m["r"]["total"], s["total_r"])
        self.assertEqual(m["avg_hold_days"], s["avg_hold_days"])
        self.assertEqual(m["mfe_pct_closed"]["avg"], s["avg_mfe_pct"])
        self.assertEqual(m["mae_pct_closed"]["avg"], s["avg_mae_pct"])

    def test_counts_rates_hits_and_exits(self):
        now = START + 10 * DAY
        self.small_sample(now)
        m = self.collect(now)
        self.assertEqual(m["counts"], {"OPEN": 1, "PENDING": 1, "CLOSED": 3, "EXPIRED": 1})
        self.assertEqual((m["filled"], m["expired"], m["entry_outcomes"]), (4, 1, 5))
        self.assertEqual(m["fill_rate_pct"], 80.0)
        self.assertEqual(m["expiry_rate_pct"], 20.0)
        self.assertEqual(m["tp_hits"], {"tp1": 2, "tp2": 0, "tp3": 0})
        self.assertEqual(m["tp_hit_rate_pct"]["tp1"], 50.0)
        self.assertEqual(m["exit_reasons"], {"invalidation": 1, "stop_breakeven": 2,
                                             "entry_window_expired": 1})
        self.assertEqual(m["r"]["median"], 0.3)
        self.assertEqual((m["wins"], m["losses"]), (2, 1))
        self.assertEqual(m["clean_closed_trades"], 2, "المركز المفتوح قبل PR #11 لا يُحتسب نظيفاً")
        self.assertEqual(m["closed_by_regime"], {"BULL": 2, "OVERHEATED": 1})
        self.assertEqual(m["first_signal_utc"], "2026-09-21")
        self.assertEqual(m["days_elapsed"], 11)

    def test_days_without_heartbeat_and_uptime(self):
        now = START + 10 * DAY
        self.small_sample(now)
        m = self.collect(now)
        self.assertEqual(m["days_without_heartbeat"],
                         ["2026-09-23", "2026-09-24", "2026-09-26", "2026-09-28", "2026-09-29", "2026-09-30"])
        self.assertEqual((m["uptime_window"]["days"], m["uptime_window"]["with_heartbeat"]), (11, 5))

    def test_frozen_position_is_detected(self):
        now = START + 10 * DAY
        self.small_sample(now)
        self.pos("PENDING", now - 5 * DAY, last_bar=now - 5 * DAY, symbol="PYTH")
        self.conn.commit()
        m = self.collect(now)
        self.assertEqual([f["symbol"] for f in m["frozen_positions"]], ["PYTH"])
        gates = {g["gate"]: g["result"] for g in R.evaluate(m)["gates"]}
        self.assertEqual(gates["no frozen positions"], "NOT YET")

    def test_empty_forward_test(self):
        m = self.collect(START + DAY)
        self.assertEqual(m["closed_trades"], 0)
        self.assertIsNone(m["win_rate_pct"])
        self.assertIsNone(m["fill_rate_pct"])
        self.assertEqual(R.evaluate(m)["overall"], "DATA COLLECTION")


class TestGates(Base):
    def test_small_sample_is_data_collection_and_names_what_is_missing(self):
        now = START + 10 * DAY
        self.small_sample(now)
        ev = R.evaluate(self.collect(now))
        self.assertEqual(ev["overall"], "DATA COLLECTION")
        for g in ("duration", "clean closed trades", "entry outcomes", "regime coverage", "uptime"):
            self.assertIn(g, ev["failed"])
        self.assertNotIn("no frozen positions", ev["failed"])
        self.assertNotIn("auto_execute off", ev["failed"])

    def test_large_clean_sample_is_ready(self):
        now = START + 70 * DAY
        for i in range(40):
            t = CLEAN + i * DAY
            self.pos("CLOSED", t, t + 60, t + DAY, r=0.5 if i % 2 else -1.0, pct=3 if i % 2 else -7,
                     reason="stop_breakeven" if i % 2 else "invalidation",
                     regime="BULL" if i < 20 else "BEAR")
        for i in range(20):
            self.pos("EXPIRED", CLEAN + i * DAY, closed=CLEAN + i * DAY + 3 * DAY, reason="entry_window_expired")
        for d in range(71):
            self.heartbeat(START + d * DAY)
        self.heartbeat(now - 300)
        self.conn.commit()
        ev = R.evaluate(self.collect(now))
        self.assertEqual(ev["failed"], [])
        self.assertEqual(ev["overall"], "READY FOR FORMAL REVIEW")

    def test_unhealthy_production_or_duplicate_loops_block_readiness(self):
        now = START + 10 * DAY
        self.small_sample(now)
        m = self.collect(now)
        for health in ({"ran": True, "status": "CRITICAL", "instances": 2},
                       {"ran": True, "status": "UNHEALTHY", "instances": 0}):
            gates = {g["gate"]: g["result"] for g in R.evaluate(m, health)["gates"]}
            self.assertEqual(gates["production health"], "NOT YET")
        ok = {g["gate"]: g["result"] for g in
              R.evaluate(m, {"ran": True, "status": "HEALTHY", "instances": 1})["gates"]}
        self.assertEqual(ok["production health"], "PASS")

    def test_stale_heartbeat_fails_ingestion(self):
        now = START + 10 * DAY
        self.small_sample(now)
        gates = {g["gate"]: g["result"] for g in R.evaluate(self.collect(now + 3600))["gates"]}
        self.assertEqual(gates["data ingestion"], "NOT YET")

    def test_auto_execute_comes_from_the_bridge_default(self):
        now = START + 10 * DAY
        self.small_sample(now)
        m = self.collect(now)
        self.assertIs(m["auto_execute"], False)
        m["auto_execute"] = True
        self.assertIn("auto_execute off", R.evaluate(m)["failed"])

    def test_report_never_judges_the_strategy(self):
        now = START + 10 * DAY
        self.small_sample(now)
        m = self.collect(now)
        text = R.render(m, R.evaluate(m)).lower()
        for word in ("good strategy", "bad strategy", "profitable strategy", "recommend"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
