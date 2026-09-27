#!/usr/bin/env python3
"""
اختبار انحدار: سطر لوج عربي لا يُسقط حلقة run_top100 حين يكون الإخراج بترميز غير UTF-8
(ويندوز يستخدم cp1252 عند توجيه الإخراج إلى ملف أو أنبوب).
بدون إنترنت، بدون تيليجرام، وبدون لمس apex_top100.db.
شغّله: python3 -m apex_top100.tests.test_stdout_encoding
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARABIC = "تم تحديث الكون: 100 عملة"

# يسجّل عبر نفس مسار المحرك (ApexV2Bridge.log) بالمستويين، ثم يكتب مباشرة إلى stderr
_LOG_SNIPPET = f"""
import sys
sys.path.insert(0, {ROOT!r})
{{setup}}
from apex_top100.integration import ApexV2Bridge
b = ApexV2Bridge()
b.log({ARABIC!r})
b.log({ARABIC!r}, "warning")
print({ARABIC!r}, file=sys.stderr, flush=True)
"""


def _run(setup: str, cwd: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUTF8", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    env["PYTHONIOENCODING"] = "cp1252"   # نفس ترميز ويندوز حين يُوجَّه الإخراج
    env["PYTHONUTF8"] = "0"
    # stdout و stderr أنابيب، كما في: py run_top100.py --loop 2>&1 | Tee-Object
    return subprocess.run([sys.executable, "-c", _LOG_SNIPPET.format(setup=setup)],
                          cwd=cwd, env=env, capture_output=True, timeout=60)


class TestNonUtf8Stdout(unittest.TestCase):
    def setUp(self):
        # cwd مؤقت: أي ملف يُنشأ بالخطأ لا يصل إلى شجرة العمل ولا إلى apex_top100.db
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_precondition_cp1252_breaks_arabic(self):
        """بدون الإصلاح يفشل نفس الكود — يثبت أن الاختبار يعيد إنتاج الظرف الحقيقي."""
        r = _run("", self._tmp.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"UnicodeEncodeError", r.stderr)

    def test_run_top100_makes_arabic_logging_safe(self):
        """استيراد run_top100 يعيد ضبط stdout/stderr إلى UTF-8 قبل أي لوج."""
        r = _run("import run_top100", self._tmp.name)
        err = r.stderr.decode("utf-8")
        self.assertEqual(r.returncode, 0, err)
        self.assertNotIn("UnicodeEncodeError", err)
        out = r.stdout.decode("utf-8")
        self.assertIn(f"[TOP100] {ARABIC}", out)
        self.assertIn(f"[TOP100][warning] {ARABIC}", out)
        self.assertIn(ARABIC, err)
        self.assertEqual(os.listdir(self._tmp.name), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
