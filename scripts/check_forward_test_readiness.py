#!/usr/bin/env python3
"""
Forward Test readiness report: is the sample large and clean enough for a formal
strategy evaluation? Read-only; it judges data sufficiency, never the strategy.

    py scripts\\check_forward_test_readiness.py            text report (+ health check on Windows)
    py scripts\\check_forward_test_readiness.py --json     same data as JSON
    py scripts\\check_forward_test_readiness.py --no-health

Safety:
  - SQLite is opened with `file:...?mode=ro`, so SQLite itself refuses any write.
  - PaperBroker.__init__ (which runs CREATE TABLE) is never called; equity, P&L and
    drawdown come from PaperBroker's own read methods, exactly like --paper-report.
  - It does not start or touch the engine, the scheduled task or the instance lock.
  - It never says whether the strategy is good or bad and never suggests changes.

Overall status is READY FOR FORMAL REVIEW only when every gate passes; otherwise
DATA COLLECTION. Reaching READY does not stop the Forward Test.
Exit code: 0 for either status, 2 if the DB cannot be read.
"""
import argparse
import inspect
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from apex_top100.config import Top100Config  # noqa: E402
from apex_top100.integration import ApexV2Bridge  # noqa: E402
from apex_top100.paper import PaperBroker, PaperConfig  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "apex_top100.db")
HEALTH_SCRIPT = os.path.join(ROOT, "scripts", "check_apex_top100_health.ps1")

# The official Forward Test started on 2026-09-21 with $1000 paper equity (no reset since).
OFFICIAL_START = "2026-09-21"
# PR #11 (merged 2026-09-22 15:42 UTC) made held positions fetch their own candles.
# Positions opened before it may have incomplete MFE/MAE, so they don't count as "clean".
CLEAN_DATA_SINCE_TS = 1790091747

KNOWN_CAVEATS = [
    "Downtime after 2026-09-22 (loop not running); missed days were caught up from daily candles on restart.",
    "Downtime 2026-09-27 15:52 -> 2026-09-30 10:36 (machine powered off); caught up the same way.",
    "Positions opened before PR #11 (2026-09-22 15:42 UTC) may have incomplete MFE/MAE.",
    "Since 2026-09-30 the loop runs under the APEX-Top100-Production scheduled task.",
    "The Forward Test has never been reset since 2026-09-21.",
]

# Conservative gates. They decide only whether the sample is big enough to review.
GATES = {
    "min_days": 60,                # calendar days since the official start
    "min_clean_closed": 30,        # CLOSED trades opened after PR #11
    "min_entry_outcomes": 50,      # filled + expired signals (fill/expiry rate sample)
    "min_regimes": 2,              # regimes (at signal) with enough closed trades ...
    "min_closed_per_regime": 5,    # ... this many each
    "uptime_window_days": 30,      # trailing window for daily heartbeat coverage
    "min_uptime": 0.90,            # share of days in the window with an engine heartbeat
    "frozen_hours": 48,            # OPEN/PENDING with no processed candle for this long
    "heartbeat_max_minutes": 35,   # same threshold as the health check
}

STATUSES = ("OPEN", "PENDING", "CLOSED", "EXPIRED")


class ReadinessError(Exception):
    pass


def connect_ro(db_path: str) -> sqlite3.Connection:
    db_path = os.path.abspath(db_path)
    if not os.path.isfile(db_path):
        raise ReadinessError(f"database not found: {db_path}")
    path = db_path.replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path                      # Windows: file:/C:/...
    uri = "file:" + urllib.parse.quote(path, safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _broker(conn: sqlite3.Connection, cfg: Top100Config) -> PaperBroker:
    # No __init__: it would run CREATE TABLE. Same read methods as --paper-report.
    b = PaperBroker.__new__(PaperBroker)
    b.conn = conn
    b.cfg = PaperConfig(start_equity=cfg.paper_start_equity,
                        entry_expiry_days=cfg.paper_entry_expiry_days,
                        max_hold_days=cfg.paper_max_hold_days,
                        slippage_pct=cfg.paper_slippage_pct)
    return b


def auto_execute_default() -> bool:
    """The default run_top100.py uses (ApexV2Bridge()), read from the code, not copied."""
    return bool(inspect.signature(ApexV2Bridge.__init__).parameters["auto_execute"].default)


def _utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(x, n)


def _summary(values: List[float]) -> dict:
    if not values:
        return {"n": 0, "avg": None, "median": None, "min": None, "max": None}
    return {"n": len(values), "avg": _r(sum(values) / len(values)),
            "median": _r(statistics.median(values)), "min": _r(min(values)), "max": _r(max(values))}


def collect(conn: sqlite3.Connection, now: Optional[float] = None,
            cfg: Optional[Top100Config] = None, start: str = OFFICIAL_START,
            frozen_hours: float = GATES["frozen_hours"],
            uptime_window_days: int = GATES["uptime_window_days"]) -> dict:
    now = time.time() if now is None else now
    cfg = cfg or Top100Config()
    if not _has_table(conn, "paper_positions"):
        raise ReadinessError("no paper_positions table: the Forward Test has not started")

    P = [dict(r) for r in conn.execute("SELECT * FROM paper_positions ORDER BY signaled_at, id")]
    for p in P:
        try:
            p["hits"] = json.loads(p.get("hits") or "[]")
        except ValueError:
            p["hits"] = []
    counts = {s: 0 for s in STATUSES}
    for p in P:
        counts[p["status"]] = counts.get(p["status"], 0) + 1

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    today = _utc_date(now)
    days_elapsed = (datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc) - start_dt).days + 1
    first_signal = P[0]["signaled_at"] if P else None

    closed = [p for p in P if p["status"] == "CLOSED"]
    expired = [p for p in P if p["status"] == "EXPIRED"]
    filled = [p for p in P if p["opened_at"] is not None]
    outcomes = len(filled) + len(expired)       # PENDING has no outcome yet
    rs = [float(p["realized_r"] or 0.0) for p in closed]
    holds = [((p["closed_at"] or 0) - (p["opened_at"] or p["signaled_at"])) / 86400.0 for p in closed]
    clean_closed = [p for p in closed if (p["opened_at"] or 0) >= CLEAN_DATA_SINCE_TS]
    open_filled = [p for p in P if p["status"] == "OPEN"]

    b = _broker(conn, cfg)
    equity = b.equity()
    dd = b.max_drawdown()

    # same win/loss split as PaperBroker.stats(): R > 0 is a win
    wins = sum(1 for r in rs if r > 0)
    tp = {k: sum(1 for p in filled if k in p["hits"]) for k in ("tp1", "tp2", "tp3")}
    exits = Counter(p["exit_reason"] or "?" for p in closed + expired)

    regime_days: Dict[str, int] = {}
    hb_dates = set()
    heartbeat = None
    if _has_table(conn, "regime_history"):
        for r in conn.execute("SELECT date, ts, regime FROM regime_history WHERE date >= ? ORDER BY date",
                              (start,)):
            regime_days[r["regime"]] = regime_days.get(r["regime"], 0) + 1
            hb_dates.add(r["date"])
        heartbeat = conn.execute("SELECT MAX(ts) FROM regime_history").fetchone()[0]

    all_days = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_elapsed)]
    missing_days = [d for d in all_days if d not in hb_dates]
    window = all_days[-uptime_window_days:]
    covered = sum(1 for d in window if d in hb_dates)

    frozen = []
    for p in P:
        if p["status"] in ("OPEN", "PENDING"):
            age_h = (now - (p["last_bar_ts"] or p["signaled_at"])) / 3600.0
            if age_h > frozen_hours:
                frozen.append({"id": p["id"], "symbol": p["symbol"], "coin_id": p["coin_id"],
                               "status": p["status"], "hours_without_candle": round(age_h, 1)})

    signals = None
    if _has_table(conn, "signals"):
        signals = conn.execute("SELECT COUNT(*) FROM signals WHERE date >= ?", (start,)).fetchone()[0]

    by_regime: Dict[str, int] = Counter(p["regime"] or "?" for p in closed)

    return {
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "official_start": start,
        "first_signal_utc": _utc_date(first_signal) if first_signal else None,
        "days_elapsed": days_elapsed,
        "counts": counts,
        "positions": len(P),
        "signals": signals,
        "filled": len(filled),
        "expired": len(expired),
        "entry_outcomes": outcomes,
        "fill_rate_pct": _r(len(filled) / outcomes * 100, 1) if outcomes else None,
        "expiry_rate_pct": _r(len(expired) / outcomes * 100, 1) if outcomes else None,
        "start_equity": b.cfg.start_equity,
        "equity": equity,
        "realized_pnl": round(equity - b.cfg.start_equity, 2),
        "realized_pnl_pct": _r((equity / b.cfg.start_equity - 1) * 100, 2),
        "max_drawdown_pct": dd["max_dd_pct"],
        "closed_trades": len(closed),
        "clean_closed_trades": len(clean_closed),
        "wins": wins,
        "losses": len(rs) - wins,
        "win_rate_pct": _r(wins / len(rs) * 100, 1) if rs else None,
        "r": {**_summary(rs), "total": _r(sum(rs)) if rs else None},
        "avg_hold_days": _r(sum(holds) / len(holds), 1) if holds else None,
        "mfe_pct_closed": _summary([float(p["mfe_pct"] or 0) for p in closed]),
        "mae_pct_closed": _summary([float(p["mae_pct"] or 0) for p in closed]),
        "mfe_pct_open": _summary([float(p["mfe_pct"] or 0) for p in open_filled]),
        "mae_pct_open": _summary([float(p["mae_pct"] or 0) for p in open_filled]),
        "tp_hits": tp,
        "tp_hit_rate_pct": {k: (_r(v / len(filled) * 100, 1) if filled else None) for k, v in tp.items()},
        "exit_reasons": dict(exits),
        "regime_days": regime_days,
        "closed_by_regime": dict(by_regime),
        "heartbeat_utc": datetime.fromtimestamp(heartbeat, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        if heartbeat else None,
        "heartbeat_age_min": _r((now - heartbeat) / 60.0, 1) if heartbeat else None,
        "days_without_heartbeat": missing_days,
        "uptime_window": {"days": len(window), "with_heartbeat": covered,
                          "ratio": _r(covered / len(window), 3) if window else None},
        "frozen_positions": frozen,
        "auto_execute": auto_execute_default(),
    }


def run_health_check() -> dict:
    """The PowerShell health check (loop count, task state). Windows only."""
    if sys.platform != "win32" or not os.path.exists(HEALTH_SCRIPT):
        return {"ran": False, "reason": "health check is Windows-only"}
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            HEALTH_SCRIPT], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": False, "reason": f"could not run: {e}"}
    out = p.stdout.decode("utf-8", "replace")
    m = re.search(r"STATUS:\s*(\w+)", out)
    inst = re.search(r"independent instances\s+(\d+)", out)
    return {"ran": True, "exit_code": p.returncode, "status": m.group(1) if m else "?",
            "instances": int(inst.group(1)) if inst else None}


def evaluate(m: dict, health: Optional[dict] = None, gates: Optional[dict] = None) -> dict:
    g = {**GATES, **(gates or {})}
    out = []

    def gate(name, ok, value, target, note=""):
        out.append({"gate": name, "result": "PASS" if ok else "NOT YET", "value": value,
                    "target": target, "note": note})

    gate("duration", m["days_elapsed"] >= g["min_days"], f"{m['days_elapsed']} days",
         f">= {g['min_days']} days")
    gate("clean closed trades", m["clean_closed_trades"] >= g["min_clean_closed"],
         f"{m['clean_closed_trades']} (of {m['closed_trades']} closed)",
         f">= {g['min_clean_closed']} closed after PR #11",
         "this sample also has to support drawdown and the R distribution")
    gate("entry outcomes", m["entry_outcomes"] >= g["min_entry_outcomes"],
         f"{m['entry_outcomes']} ({m['filled']} filled, {m['expired']} expired)",
         f">= {g['min_entry_outcomes']} filled + expired")
    enough = {k: v for k, v in m["closed_by_regime"].items() if v >= g["min_closed_per_regime"]}
    gate("regime coverage", len(enough) >= g["min_regimes"],
         ", ".join(f"{k}:{v}" for k, v in m["closed_by_regime"].items()) or "no closed trades",
         f">= {g['min_regimes']} regimes with >= {g['min_closed_per_regime']} closed each")
    up = m["uptime_window"]
    gate("uptime", (up["ratio"] or 0) >= g["min_uptime"],
         f"{up['with_heartbeat']}/{up['days']} days with a heartbeat",
         f">= {int(g['min_uptime'] * 100)}% of the last {g['uptime_window_days']} days")
    gate("no frozen positions", not m["frozen_positions"],
         ", ".join(f"{f['symbol']} {f['status']} {f['hours_without_candle']}h"
                   for f in m["frozen_positions"]) or "none",
         f"no OPEN/PENDING without a candle for > {g['frozen_hours']}h")
    age = m["heartbeat_age_min"]
    gate("data ingestion", age is not None and age <= g["heartbeat_max_minutes"],
         f"heartbeat {age} min ago" if age is not None else "no heartbeat",
         f"<= {g['heartbeat_max_minutes']} min")
    gate("auto_execute off", m["auto_execute"] is False, f"auto_execute={m['auto_execute']}",
         "auto_execute=False")
    if health and health.get("ran"):
        gate("production health", health["status"] == "HEALTHY" and health.get("instances") == 1,
             f"{health['status']}, {health.get('instances')} loop(s)", "HEALTHY, exactly 1 loop")
    else:
        out.append({"gate": "production health", "result": "SKIPPED", "value": None,
                    "target": "HEALTHY, exactly 1 loop",
                    "note": (health or {}).get("reason", "not requested")})

    failed = [x["gate"] for x in out if x["result"] == "NOT YET"]
    return {"gates": out, "failed": failed,
            "overall": "READY FOR FORMAL REVIEW" if not failed else "DATA COLLECTION"}


def render(m: dict, ev: dict) -> str:
    c = m["counts"]
    r = m["r"]

    def s(d):
        return "n/a" if not d["n"] else f"avg {d['avg']}  median {d['median']}  min {d['min']}  max {d['max']}"

    lines = [
        "APEX FORWARD TEST - READINESS REPORT (read-only)",
        f"generated {m['generated_at']}",
        "",
        f"official start   {m['official_start']}  (first signal {m['first_signal_utc']} UTC)  day {m['days_elapsed']}",
        f"positions        {m['positions']}: OPEN {c['OPEN']}  PENDING {c['PENDING']}  CLOSED {c['CLOSED']}  EXPIRED {c['EXPIRED']}",
        f"signals          {m['signals']} recorded (a signal opens no position while that coin already has one)",
        f"fill / expiry    {m['fill_rate_pct']}% filled, {m['expiry_rate_pct']}% expired  (of {m['entry_outcomes']} outcomes; PENDING excluded)",
        f"realized equity  ${m['equity']}  (start ${m['start_equity']})  P&L ${m['realized_pnl']} ({m['realized_pnl_pct']}%)",
        f"max drawdown     {m['max_drawdown_pct']}%  (realized curve)",
        f"closed trades    {m['closed_trades']}  ({m['clean_closed_trades']} after PR #11)  wins {m['wins']}  losses {m['losses']}  win rate {m['win_rate_pct']}%",
        f"R                avg {r['avg']}  median {r['median']}  total {r['total']}  best {r['max']}  worst {r['min']}",
        f"avg hold         {m['avg_hold_days']} days",
        f"MFE % closed     {s(m['mfe_pct_closed'])}",
        f"MAE % closed     {s(m['mae_pct_closed'])}",
        f"MFE % open       {s(m['mfe_pct_open'])}",
        f"MAE % open       {s(m['mae_pct_open'])}",
        "TP hits          " + "  ".join(f"{k.upper()} {m['tp_hits'][k]} ({m['tp_hit_rate_pct'][k]}%)" for k in ("tp1", "tp2", "tp3"))
        + f"  of {m['filled']} filled",
        "exit reasons     " + (", ".join(f"{k} {v}" for k, v in sorted(m["exit_reasons"].items())) or "none"),
        "regime days      " + (", ".join(f"{k} {v}" for k, v in m["regime_days"].items()) or "none"),
        "closed by regime " + (", ".join(f"{k} {v}" for k, v in m["closed_by_regime"].items()) or "none"),
        f"heartbeat        {m['heartbeat_utc']} UTC ({m['heartbeat_age_min']} min ago)",
        f"no heartbeat     {len(m['days_without_heartbeat'])} day(s): {', '.join(m['days_without_heartbeat']) or '-'}",
        f"auto_execute     {m['auto_execute']}",
        "",
        "CAVEATS",
        *[f"  - {x}" for x in KNOWN_CAVEATS],
        *[f"  - FROZEN: {f['symbol']} ({f['coin_id']}) {f['status']} has had no processed candle for "
          f"{f['hours_without_candle']}h" for f in m["frozen_positions"]],
        "",
        "READINESS GATES",
        *[f"  [{g['result']:>7}] {g['gate']:<20} {g['value']}   target: {g['target']}" for g in ev["gates"]],
        "",
        f"OVERALL: {ev['overall']}",
        "This report measures whether the sample is large and clean enough to review."
        " It does not judge the strategy.",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="APEX Forward Test readiness report (read-only)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--no-health", action="store_true", help="skip the PowerShell health check")
    args = ap.parse_args(argv)
    try:
        conn = connect_ro(args.db)
    except ReadinessError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    try:
        m = collect(conn)
    except (ReadinessError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    health = {"ran": False, "reason": "--no-health"} if args.no_health else run_health_check()
    ev = evaluate(m, health)
    if args.json:
        print(json.dumps({"metrics": m, "health": health, **ev}, ensure_ascii=False, indent=2))
    else:
        print(render(m, ev))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
