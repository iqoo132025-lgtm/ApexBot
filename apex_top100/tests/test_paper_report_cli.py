#!/usr/bin/env python3
"""
`run_top100.py --paper-report` للقراءة فقط: اتصال mode=ro، بلا CREATE TABLE ولا محرك ولا مزوّد.
قواعد بيانات مؤقتة فقط. شغّله: python3 -m apex_top100.tests.test_paper_report_cli
"""
import hashlib
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_top100.paper import PaperBroker, PaperConfig
from apex_top100.tests.test_paper import make_signal
from apex_top100.tests.tmpdb import TmpDbTestCase


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestPaperReportIsReadOnly(TmpDbTestCase):
    def setUp(self):
        self.tmp = self.tmp_dir()
        self.db = os.path.join(self.tmp, "ft.db")

    def _run(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run([sys.executable, os.path.join(ROOT, "run_top100.py"), "--paper-report",
                            "--db", self.db], cwd=self.tmp, env=env, capture_output=True, timeout=120)
        # stdout ويندوز يكتب CRLF؛ المحتوى هو المقارَن لا نهايات الأسطر
        norm = lambda b: b.decode("utf-8").replace(chr(13) + chr(10), chr(10))
        return r.returncode, norm(r.stdout), norm(r.stderr)

    def _forward_test(self):
        store = self.open_store(self.db)
        b = PaperBroker(store.conn, PaperConfig())
        b.open_from_signal(make_signal("SOL", price=100.0))
        b.open_from_signal(make_signal("ADA", price=100.0))
        b.update({"sol": 145.0, "ada": 90.0})
        b.open_from_signal(make_signal("DOT", price=130.0))       # PENDING
        expected = b.report()
        store.close()
        return expected

    def test_report_matches_the_broker_and_leaves_the_db_byte_identical(self):
        expected = self._forward_test()
        before = _sha(self.db)
        code, out, err = self._run()
        self.assertEqual(code, 0, err)
        self.assertIn(expected, out, "نفس report() حرفياً")
        self.assertIn("DOT", out, "المراكز القائمة تُعرض")
        self.assertEqual(_sha(self.db), before)
        self.assertEqual(os.listdir(self.tmp), ["ft.db"], "لا كاش ولا journal ولا ملفات جديدة")

    def test_missing_database_is_not_created(self):
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("لا قاعدة بيانات", err)
        self.assertFalse(os.path.exists(self.db))

    def test_database_without_paper_tables_is_not_migrated(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE other (a)")
        conn.commit()
        conn.close()
        before = _sha(self.db)
        code, out, err = self._run()
        self.assertEqual(code, 0, err)
        self.assertIn("لا مراكز ورقية بعد", out)
        self.assertEqual(_sha(self.db), before, "لا CREATE TABLE على قاعدة قائمة")


if __name__ == "__main__":
    unittest.main(verbosity=2)
