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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apex_top100.config import Top100Config
from apex_top100.integration import ApexV2Bridge, build_engine
from apex_top100.paper import format_position_line
from apex_top100.telegram_signals import format_top100_signal


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

    cfg = Top100Config(universe_size=args.size, min_score=args.min_score, db_path=args.db,
                       telegram_token=args.telegram_token, telegram_chat_id=args.telegram_chat)

    provider = None
    if args.demo:
        from apex_top100.providers_mock import MockDataProvider
        cfg.universe_size = min(args.size, 20)
        cfg.max_ohlcv_per_cycle = cfg.universe_size
        cfg.db_path = "apex_top100_demo.db"
        provider = MockDataProvider(n=cfg.universe_size)

    engine = build_engine(cfg, bridge=ApexV2Bridge(), provider=provider)

    if args.paper_report:
        if not engine.paper:
            print("Paper Trading معطّل في الإعدادات")
            return 1
        print(engine.paper.report())
        for p in engine.paper.open_positions()[:10]:
            print("  " + format_position_line(p))
        return 0

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
