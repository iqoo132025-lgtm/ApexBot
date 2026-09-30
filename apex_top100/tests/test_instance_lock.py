#!/usr/bin/env python3
"""
قفل النسخة الواحدة لحلقة الإنتاج: حلقتان على نفس قاعدة البيانات لا تعملان معاً،
والقفل يتحرر حين تموت العملية المالكة ولو قُتلت.
كل الأقفال وقواعد البيانات هنا في مجلدات مؤقتة — لا قفل الإنتاج ولا apex_top100.db.
بدون إنترنت. شغّله: python3 -m apex_top100.tests.test_instance_lock
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_top100.instance_lock import ALREADY_RUNNING_EXIT, InstanceLock, lock_path_for
from apex_top100.tests.tmpdb import TmpDbTestCase

PROD_DB = os.path.join(ROOT, "apex_top100.db")

# عملية منفصلة تمسك القفل حتى تُقتل — مثل حلقة إنتاج حقيقية
_HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from apex_top100.instance_lock import InstanceLock
lock = InstanceLock({path!r})
print("locked" if lock.acquire() else "busy", flush=True)
time.sleep(120)
"""


def _prod_lock_state():
    """وجود قفل الإنتاج ومعلومات مالكه. البايت 0 قد يكون مقفولاً بحلقة حية، فنقرأ ما بعده.
    (لا نقارن تجزئة قاعدة الإنتاج: الحلقة الحية تكتب فيها أثناء الاختبار.)"""
    path = lock_path_for(PROD_DB)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        f.seek(1)
        return f.read()


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class TestInstanceLock(TmpDbTestCase):
    @classmethod
    def setUpClass(cls):
        cls._prod_lock = _prod_lock_state()
        cls._prod_db_existed = os.path.exists(PROD_DB)

    @classmethod
    def tearDownClass(cls):
        assert _prod_lock_state() == cls._prod_lock, "الاختبارات لمست قفل الإنتاج"
        assert os.path.exists(PROD_DB) == cls._prod_db_existed, "الاختبارات أنشأت قاعدة الإنتاج"

    def setUp(self):
        self.tmp = self.tmp_dir()
        self.db = os.path.join(self.tmp, "t.db")
        self.path = lock_path_for(self.db)

    def _holder(self) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", _HOLDER.format(root=ROOT, path=self.path)],
                             stdout=subprocess.PIPE, text=True, env=_env())
        # القتل والانتظار قبل حذف المجلد: addCleanup يعمل بترتيب عكسي
        self.addCleanup(p.wait, 30)
        self.addCleanup(p.kill)
        self.addCleanup(p.stdout.close)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        return p

    def _lock(self) -> InstanceLock:
        lock = InstanceLock(self.path)
        self.addCleanup(lock.release)
        return lock

    def test_lock_paths_are_temporary(self):
        self.assertTrue(self.path.startswith(os.path.abspath(self.tmp)))
        self.assertNotEqual(os.path.normcase(self.path), os.path.normcase(lock_path_for(PROD_DB)))

    def test_first_loop_acquires_the_lock(self):
        lock = self._lock()
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.held)
        with open(self.path, "rb") as f:
            f.seek(1)                       # البايت 0 مقفول على ويندوز؛ معلومات المالك بعده
            self.assertIn(f"pid={os.getpid()}".encode(), f.read())

    def test_second_loop_cannot_acquire_the_same_lock(self):
        self._holder()
        self.assertFalse(self._lock().acquire(), "عمليتان تملكان نفس القفل")

    def test_lock_is_free_after_the_owner_is_killed(self):
        """قتل المالك (انهيار) لا يترك قفلاً ميتاً يمنع الاستعادة."""
        holder = self._holder()
        lock = self._lock()
        self.assertFalse(lock.acquire())
        holder.kill()
        holder.wait(30)
        self.assertTrue(os.path.exists(self.path), "الملف يبقى — القفل نفسه هو ما يتحرر")
        self.assertTrue(lock.acquire())

    def test_release_lets_the_next_owner_in(self):
        first = self._lock()
        self.assertTrue(first.acquire())
        first.release()
        self.assertTrue(self._lock().acquire())

    def _run_top100(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, os.path.join(ROOT, "run_top100.py"), *args,
                               "--db", self.db],
                              cwd=self.tmp, env=_env(), capture_output=True, timeout=120)

    def test_second_production_loop_exits_without_touching_the_db(self):
        self._holder()
        r = self._run_top100("--loop")
        self.assertEqual(r.returncode, ALREADY_RUNNING_EXIT, r.stderr.decode("utf-8", "replace"))
        self.assertIn("لن تبدأ نسخة ثانية", r.stderr.decode("utf-8"))
        self.assertFalse(os.path.exists(self.db), "الحلقة الثانية لا تفتح قاعدة البيانات")

    def test_paper_report_is_not_blocked_by_a_running_loop(self):
        self._holder()
        r = self._run_top100("--paper-report")
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
