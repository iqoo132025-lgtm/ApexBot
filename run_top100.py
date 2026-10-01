#!/usr/bin/env python3
"""
تشغيل Top 100 Market Engine — مباشرةً أو للتجربة بدون إنترنت.

  python3 run_top100.py --once                 دورة واحدة على البيانات الحية
  python3 run_top100.py --loop                 تشغيل مستمر
  python3 run_top100.py --demo                 محاكاة كاملة بمزوّد بيانات صناعي
  python3 run_top100.py --once --telegram-token XXX --telegram-chat 123

متغيرات البيئة البديلة: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import os
import sqlite3
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force UTF-8 on Windows so redirected Arabic output cannot crash the engine loop.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from apex_top100.config import Top100Config
from apex_top100.integration import ApexV2Bridge, build_engine
from apex_top100.paper import PaperBroker, PaperConfig, format_position_line
from apex_top100.telegram_signals import format_top100_signal


def paper_report(cfg: Top100Config) -> int:
    """
    تقرير Forward Test للقراءة فقط: اتصال `mode=ro` فلا يكتب SQLite شيئاً، وبلا
    `PaperBroker.__init__` (ينفّذ CREATE TABLE) ولا محرك ولا مزوّد بيانات.
    قاعدة غير موجودة لا تُنشأ.
    """
    if not cfg.paper_trading:
        print("Paper Trading معطّل في الإعدادات")
        return 1
    db = os.path.abspath(cfg.db_path)
    if not os.path.isfile(db):
        print(f"لا قاعدة بيانات في {db} — لا تقرير", file=sys.stderr)
        return 1
    path = db.replace("\\", "/")
    uri = "file:" + urllib.parse.quote(path if path.startswith("/") else "/" + path, safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_positions'").fetchone():
            print("لا مراكز ورقية بعد — Forward Test لم يبدأ في هذه القاعدة")
            return 0
        b = PaperBroker.__new__(PaperBroker)
        b.conn = conn
        b.cfg = PaperConfig(start_equity=cfg.paper_start_equity,
                            entry_expiry_days=cfg.paper_entry_expiry_days,
                            max_hold_days=cfg.paper_max_hold_days,
                            slippage_pct=cfg.paper_slippage_pct)
        print(b.report())
        for p in b.open_positions()[:10]:
            print("  " + format_position_line(p))
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="APEX Ultimate V2 — Top 100 Market Engine")
    ap.add_argument("--once", action="store_true", help="دورة واحدة")
    ap.add_argument("--loop", action="store_true", help="تشغيل مستمر")
    ap.add_argument("--demo", action="store_true", help="محاكاة بدون إنترنت")
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--min-score", type=int, default=70)
    ap.add_argument("--db", default="apex_top100.db")
    ap.add_argument("--telegram-token", default=os.getenv("TELEGRAM_TOKEN"))
    ap.add_argument("--telegram-chat", default=os.getenv("TELEGRAM_CHAT_ID"))
    ap.add_argument("--days", type=int, default=20, help="عدد أيام المحاكاة في وضع --demo")
    ap.add_argument("--paper-report", action="store_true", help="تقرير Paper Trading / Forward Test")
    args = ap.parse_args()

    # حلقة إنتاج واحدة لكل قاعدة بيانات، قبل فتحها. --once و --demo و --paper-report لا تُقفل.
    # القفل يبقى ما بقيت العملية، والنظام يحرّره عند انتهائها ولو بانهيار.
    loop_lock = None
    if args.loop and not args.demo and not args.paper_report:
        from apex_top100.instance_lock import ALREADY_RUNNING_EXIT, InstanceLock, lock_path_for
        loop_lock = InstanceLock(lock_path_for(args.db))
        if not loop_lock.acquire():
            print(f"[TOP100] حلقة أخرى تعمل على {os.path.abspath(args.db)} — لن تبدأ نسخة ثانية",
                  file=sys.stderr, flush=True)
            return ALREADY_RUNNING_EXIT

    cfg = Top100Config(universe_size=args.size, min_score=args.min_score, db_path=args.db,
                       telegram_token=args.telegram_token, telegram_chat_id=args.telegram_chat)

    provider = None
    if args.demo:
        from apex_top100.providers_mock import MockDataProvider
        cfg.universe_size = min(args.size, 20)
        cfg.max_ohlcv_per_cycle = cfg.universe_size
        cfg.db_path = "apex_top100_demo.db"
        provider = MockDataProvider(n=cfg.universe_size)

    if args.paper_report:            # قبل build_engine: لا CREATE ولا مزوّد ولا كتابة
        return paper_report(cfg)

    engine = build_engine(cfg, bridge=ApexV2Bridge(), provider=provider)

    if args.demo:
        from datetime import date, timedelta
        base = date.today() - timedelta(days=args.days)
        for d in range(args.days):
            provider.advance(1)
            engine.refresh_universe(force=True)
            engine.ensure_daily_snapshot(date=(base + timedelta(days=d)).isoformat())
        engine.load_ohlcv(engine.universe)
        engine.update_regime()
        sigs = engine.analyze_all()
        sent = engine.emit(sigs)
        print(f"\nحالة السوق: {engine.regime.regime} ({engine.regime.score:.0f}/100)")
        print(f"العتبة الحالية: {engine.min_score_now():.0f} — إشارات مرسلة: {len(sent)}\n")
        for s in (sent or sigs[:2]):
            print(format_top100_signal(s))
            print("\n[CHART] [ANALYSIS] [BUY]\n" + "-" * 44)
        pats = engine.scan_patterns()
        print("أنماط من اللقطات:")
        for k, rows in pats.items():
            print(f"  {k}: {len(rows)}")
        if engine.paper:
            print("\n" + engine.paper.report())
        return 0

    if args.loop:
        engine.run_forever()
        return 0

    out = engine.run_once()
    reg = out["regime"]
    print(f"حالة السوق: {reg.regime if reg else '—'}")
    for s in out["sent"]:
        print(format_top100_signal(s))
    print(f"تحليلات: {len(out['signals'])} — إشارات: {len(out['sent'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
