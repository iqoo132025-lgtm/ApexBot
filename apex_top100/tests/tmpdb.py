"""
مجلد مؤقت لقواعد SQLite في الاختبارات يُغلق اتصالاته قبل حذفه.

ويندوز لا يحذف ملفاً مفتوحاً: `with TemporaryDirectory()` حول SnapshotStore
لم يُغلق يفشل عند الخروج بـ PermissionError [WinError 32]، ولينكس يسمح بذلك
فلا يظهر الخلل في CI. هنا المجلد والاتصال يُسجَّلان عبر addCleanup، والتنظيف
يعمل بترتيب عكسي (LIFO): كل اتصال فُتح بعد المجلد يُغلق قبله — حتى لو فشل الاختبار.
"""
import tempfile
import unittest

from apex_top100.snapshots import SnapshotStore


class TmpDbTestCase(unittest.TestCase):
    def tmp_dir(self) -> str:
        """مجلد مؤقت يُحذف بعد الاختبار، بعد إغلاق ما فُتح داخله."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def open_store(self, db_path: str) -> SnapshotStore:
        """SnapshotStore يُغلق تلقائياً قبل حذف مجلده."""
        store = SnapshotStore(db_path)
        self.addCleanup(store.close)
        return store

    def close_engine_store(self, engine):
        """للمحرك الذي يفتح قاعدته بنفسه (install_top100 أو store=None)."""
        self.addCleanup(engine.store.close)
        return engine
