#!/usr/bin/env python3
"""
يبدأ Forward Test نظيفاً بلا فقدان سجلّ ما حدث.

كل ما في `paper_positions` و`paper_equity` و`signals` اليوم جاء من دورات سبقت
بوابة `require_healthy_cycle` — أي من بيانات اعتُبرت تشخيصية. هذه الأداة تنقلها
إلى جداول `*_pre_fix_<التاريخ>` داخل نفس الملف بدل حذفها، ثم تُنشئ جداول فارغة.

لماذا `signals` أيضاً: منع التكرار يقرأ آخر إشارة لكل عملة ويصمتها 24 ساعة
(`signal_cooldown_hours`). لو تُركت إشارات ما قبل الإصلاح فإن العملات الثماني —
وهي بالضبط ما نريد قياسه — ستبقى مكتومة حتى الغد.

ما لا يُمَس: `rank_history` و`list_events` و`regime_history` و`tracking`.

    python reset_paper.py              # يعرض ما سيفعله ولا يغيّر شيئاً
    python reset_paper.py --yes        # ينفّذ
"""
import os
import shutil
import sqlite3
import sys
import time

ARCHIVED = ("paper_positions", "paper_equity", "signals")
KEPT = ("rank_history", "list_events", "regime_history", "tracking")


def main() -> int:
    db = next((a for a in sys.argv[1:] if not a.startswith("-")), "apex_top100.db")
    apply = "--yes" in sys.argv

    if not os.path.exists(db):
        print(f"لا يوجد ملف: {db}")
        return 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    have = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    plan = []
    for t in ARCHIVED:
        if t not in have:
            continue
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        plan.append((t, f"{t}_pre_fix_{stamp}", n))

    if not plan:
        print("لا يوجد ما يُؤرشف.")
        return 0

    print(f"قاعدة البيانات: {os.path.abspath(db)}\n")
    print("سيُنقل إلى الأرشيف:")
    for src, dst, n in plan:
        print(f"  {src:<18} {n:>5} صف  ←  {dst}")
    print("\nيبقى كما هو: " + "، ".join(t for t in KEPT if t in have))

    if not apply:
        print("\nعرض فقط. أضف --yes للتنفيذ.")
        return 0

    backup = f"{db}.bak-{stamp}"
    conn.close()
    shutil.copy2(db, backup)
    print(f"\nنسخة احتياطية كاملة: {backup}")

    conn = sqlite3.connect(db)
    for src, dst, _ in plan:
        conn.execute(f"ALTER TABLE {src} RENAME TO {dst}")
    # الفهارس تتبع جداولها بعد إعادة التسمية، فنُسقطها لتُبنى نظيفة
    for idx in ("idx_paper_status", "idx_signals_coin"):
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    conn.commit()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from apex_top100.paper import PAPER_SCHEMA
    from apex_top100.snapshots import SCHEMA
    conn.executescript(SCHEMA)
    conn.executescript(PAPER_SCHEMA)
    conn.commit()

    fresh = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()[0]
             for t in ARCHIVED}
    conn.close()
    print("الجداول الجديدة: " + "، ".join(f"{t}={n}" for t, n in fresh.items()))
    print("رصيد Forward Test يبدأ من paper_start_equity في الإعدادات ($1000).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
