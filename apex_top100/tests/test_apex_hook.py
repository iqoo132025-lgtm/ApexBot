#!/usr/bin/env python3
"""
اختبار الدمج داخل APEX_ULTIMATE.py نفسه (بدون إنترنت وبدون تشغيل الخادم).
شغّله: python3 -m apex_top100.tests.test_apex_hook
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from apex_top100.apex_ultimate_hook import install_top100
from apex_top100.providers_mock import MockDataProvider


def _tmp_paths(tmp: str) -> dict:
    """
    يوجّه قاعدة البيانات والكاش إلى مجلد مؤقت.

    install_top100 يضعهما بجانب الحزمة افتراضياً — وهو الصحيح للبوت الحقيقي —
    فبدون هذا يقرأ الاختبار إشارات تشغيل سابق، ويحذفها منعُ التكرار، فيفشل
    عند التشغيل الثاني على نفس المجلد ويترك apex_top100.db في شجرة العمل.
    """
    return {"TOP100_DB_PATH": os.path.join(tmp, "t.db"),
            "TOP100_CACHE_DIR": os.path.join(tmp, "cache")}


class TestApexUltimateIntegration(unittest.TestCase):
    def test_bot_file_wiring(self):
        """الملف نفسه يحتوي نقاط الربط الخمس."""
        with open(os.path.join(ROOT, "APEX_ULTIMATE.py"), encoding="utf-8") as f:
            src = f.read()
        for needle in ['"TOP100_ENABLED"', '"top100": {"regime"', "/api/top100",
                       "install_top100", '"top100":      state.get("top100", {})']:
            self.assertIn(needle, src, f"نقطة الربط مفقودة: {needle}")
        self.assertNotIn("BINANCE_API_KEY\": \"", src)   # لا مفاتيح نصية

    def test_engine_fills_state_and_api_payload(self):
        logs = []
        state = {"open_positions": {}, "protected": False, "logs": []}
        with tempfile.TemporaryDirectory() as tmp:
            # قاعدة البيانات داخل tmp: install_top100 يضعها بجانب الحزمة افتراضياً،
            # فبدون هذا يقرأ الاختبار إشارات تشغيل سابق ويحذفها منع التكرار.
            cfg = {"MAX_OPEN_POSITIONS": 2, "BANNED_SYMBOLS": [], "TOP100_MIN_SCORE": 60,
                   "TOP100_UNIVERSE": 14, **_tmp_paths(tmp)}
            provider = MockDataProvider(n=14)
            engine = install_top100(cfg, state, lambda m, l="info": logs.append(m),
                                    start=False, provider=provider)
            engine.cfg.max_ohlcv_per_cycle = 14
            engine.cfg.min_score = 60
            out = engine.run_once()

            self.assertIsNotNone(out["regime"])
            self.assertTrue(state["top100"]["signals"], "الإشارات يجب أن تظهر في state")
            first = state["top100"]["signals"][0]
            for k in ("symbol", "rank", "score", "regime", "entry_low", "tp1", "position_pct"):
                self.assertIn(k, first)
            # نفس ما تعيده نقطة /api/top100
            payload = json.dumps(state["top100"], ensure_ascii=False, default=str)
            self.assertIn("signals", payload)

    def test_capital_guard_blocks_when_protected(self):
        state = {"open_positions": {}, "protected": True, "logs": []}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"MAX_OPEN_POSITIONS": 2, "BANNED_SYMBOLS": [], **_tmp_paths(tmp)}
            engine = install_top100(cfg, state, lambda m, l="info": None,
                                    start=False, provider=MockDataProvider(n=12))
            engine.cfg.max_ohlcv_per_cycle = 12
            engine.cfg.min_score = 50
            out = engine.run_once()
            self.assertEqual(out["sent"], [], "حماية الخسارة اليومية توقف إشارات Top 100")

    def test_max_positions_guard(self):
        state = {"open_positions": {"BTCUSDT": {}, "ETHUSDT": {}}, "protected": False, "logs": []}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"MAX_OPEN_POSITIONS": 2, "BANNED_SYMBOLS": [], **_tmp_paths(tmp)}
            engine = install_top100(cfg, state, lambda m, l="info": None,
                                    start=False, provider=MockDataProvider(n=12))
            engine.cfg.max_ohlcv_per_cycle = 12
            engine.cfg.min_score = 50
            self.assertEqual(engine.run_once()["sent"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
