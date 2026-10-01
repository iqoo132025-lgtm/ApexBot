#!/usr/bin/env python3
"""
تشغيل لوحة APEX الحية — للقراءة فقط فوق Forward Test.

  py run_dashboard.py                       ثم افتح http://127.0.0.1:8765
  py run_dashboard.py --db apex_top100.db --cache .apex_cache --port 8765

عملية منفصلة عن المحرك: حلقة الإنتاج تديرها المهمة المجدولة APEX-Top100-Production وحدها،
واللوحة لا تشغّلها ولا توقفها ولا تمسّ قفلها. لا تشغّل `run_top100.py --loop` يدوياً.
SQLite يُفتح بوضع للقراءة فقط، والكاش يُقرأ ولا يُكتب.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apex_dashboard.api import DashboardReader
from apex_dashboard.server import make_server
from apex_top100.config import Top100Config


def main() -> int:
    cfg = Top100Config()
    ap = argparse.ArgumentParser(description="APEX Live Dashboard (read-only)")
    ap.add_argument("--db", default=cfg.db_path)
    ap.add_argument("--cache", default=cfg.cache_dir)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"تحذير: اللوحة ستكون مكشوفة على {args.host} بلا مصادقة.")

    reader = DashboardReader(args.db, args.cache, cfg)
    srv = make_server(reader, args.host, args.port)
    print(f"APEX Dashboard (PAPER, read-only): http://{args.host}:{args.port}")
    print(f"  db    = {reader.db_path}")
    print(f"  cache = {reader.cache_dir}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
