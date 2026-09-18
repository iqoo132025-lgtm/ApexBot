#!/usr/bin/env python3
"""
سجل Top-100 التاريخي (SQLite).

يحفظ لقطة يومية لترتيب أكبر 100 عملة، ويشتق منها أنماطاً:
  • سرعة تحسّن الترتيب (مثلاً #80 → #40 خلال أسبوعين)
  • العملات التي تتفوق على BTC
  • العملات التي يدخلها Volume قبل ارتفاع السعر
  • أحداث الدخول/الخروج من Top 100 (مع الاحتفاظ بتاريخ الخارجة أيضاً)

لا يُحذف أي صف: الخارج من القائمة يبقى في التاريخ لأن دخوله/خروجه بحد ذاته إشارة.
"""
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS rank_history (
    date        TEXT NOT NULL,
    coin_id     TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    market_cap  REAL,
    price       REAL,
    volume_24h  REAL,
    btc_price   REAL,
    eth_price   REAL,
    taken_at    INTEGER,
    PRIMARY KEY (date, coin_id)
);
CREATE INDEX IF NOT EXISTS idx_rank_coin ON rank_history(coin_id, date);

CREATE TABLE IF NOT EXISTS list_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    coin_id   TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    event     TEXT NOT NULL,           -- ENTER | EXIT
    rank      INTEGER,
    prev_rank INTEGER,
    market_cap REAL,
    UNIQUE(date, coin_id, event)
);

CREATE TABLE IF NOT EXISTS regime_history (
    date      TEXT PRIMARY KEY,
    ts        INTEGER NOT NULL,
    regime    TEXT NOT NULL,
    score     REAL,
    metrics   TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    date     TEXT NOT NULL,
    coin_id  TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    rank     INTEGER,
    score    REAL,
    regime   TEXT,
    payload  TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_coin ON signals(coin_id, ts);

CREATE TABLE IF NOT EXISTS tracking (
    coin_id      TEXT PRIMARY KEY,
    symbol       TEXT,
    first_seen   TEXT,
    last_seen    TEXT,
    best_rank    INTEGER,
    worst_rank   INTEGER,
    in_top100    INTEGER DEFAULT 1,
    entered_at   TEXT,
    exited_at    TEXT
);
"""


def _today(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ListEvent:
    date: str
    coin_id: str
    symbol: str
    event: str          # ENTER | EXIT
    rank: Optional[int]
    prev_rank: Optional[int]
    market_cap: Optional[float]


class SnapshotStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── كتابة اللقطة ──
    def take_snapshot(self, coins: Iterable, btc_price: float = 0.0, eth_price: float = 0.0,
                      date: Optional[str] = None) -> Tuple[str, List[ListEvent]]:
        """يسجل لقطة اليوم ويعيد أحداث الدخول/الخروج مقارنةً بآخر لقطة سابقة."""
        coins = list(coins)
        date = date or _today()
        prev_date = self.last_snapshot_date(before=date)
        prev = self.snapshot(prev_date) if prev_date else {}

        now = int(time.time())
        rows = [(date, c.coin_id, c.symbol, c.rank, c.market_cap, c.price,
                 c.volume_24h, btc_price, eth_price, now) for c in coins]
        self.conn.executemany(
            "INSERT OR REPLACE INTO rank_history "
            "(date, coin_id, symbol, rank, market_cap, price, volume_24h, btc_price, eth_price, taken_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

        events: List[ListEvent] = []
        current_ids = {c.coin_id: c for c in coins}
        for cid, c in current_ids.items():
            if prev and cid not in prev:
                events.append(ListEvent(date, cid, c.symbol, "ENTER", c.rank, None, c.market_cap))
        for cid, row in (prev or {}).items():
            if cid not in current_ids:
                events.append(ListEvent(date, cid, row["symbol"], "EXIT", None, row["rank"], row["market_cap"]))

        for e in events:
            self.conn.execute(
                "INSERT OR IGNORE INTO list_events (date, ts, coin_id, symbol, event, rank, prev_rank, market_cap) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (e.date, now, e.coin_id, e.symbol, e.event, e.rank, e.prev_rank, e.market_cap))

        # جدول التتبع: يحتفظ بكل عملة رأيناها يوماً ما
        for c in coins:
            self.conn.execute("""
                INSERT INTO tracking (coin_id, symbol, first_seen, last_seen, best_rank, worst_rank, in_top100, entered_at)
                VALUES (?,?,?,?,?,?,1,?)
                ON CONFLICT(coin_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    last_seen=excluded.last_seen,
                    best_rank=MIN(best_rank, excluded.best_rank),
                    worst_rank=MAX(worst_rank, excluded.worst_rank),
                    in_top100=1,
                    entered_at=CASE WHEN tracking.in_top100=0 THEN excluded.last_seen ELSE tracking.entered_at END
            """, (c.coin_id, c.symbol, date, date, c.rank, c.rank, date))
        for e in events:
            if e.event == "EXIT":
                self.conn.execute("UPDATE tracking SET in_top100=0, exited_at=? WHERE coin_id=?", (date, e.coin_id))

        self.conn.commit()
        return date, events

    # ── قراءة ──
    def last_snapshot_date(self, before: Optional[str] = None) -> Optional[str]:
        q = "SELECT MAX(date) d FROM rank_history"
        args: tuple = ()
        if before:
            q += " WHERE date < ?"
            args = (before,)
        r = self.conn.execute(q, args).fetchone()
        return r["d"] if r and r["d"] else None

    def snapshot(self, date: str) -> Dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM rank_history WHERE date=?", (date,)).fetchall()
        return {r["coin_id"]: r for r in rows}

    def snapshot_dates(self, limit: int = 60) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT date FROM rank_history ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
        return [r["date"] for r in rows]

    def has_snapshot(self, date: Optional[str] = None) -> bool:
        date = date or _today()
        r = self.conn.execute("SELECT 1 FROM rank_history WHERE date=? LIMIT 1", (date,)).fetchone()
        return r is not None

    def rank_series(self, coin_id: str, days: int = 90) -> List[Tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT date, rank FROM rank_history WHERE coin_id=? ORDER BY date DESC LIMIT ?",
            (coin_id, days)).fetchall()
        return [(r["date"], r["rank"]) for r in reversed(rows)]

    def rank_change(self, coin_id: str, days: int = 30) -> Optional[int]:
        """موجب = تحسّن في الترتيب (مثلاً 80 → 40 يعطي +40)."""
        s = self.rank_series(coin_id, days + 1)
        if len(s) < 2:
            return None
        return s[0][1] - s[-1][1]

    def rank_velocity(self, coin_id: str, days: int = 14) -> Optional[float]:
        """مراتب/يوم — سرعة صعود الترتيب."""
        s = self.rank_series(coin_id, days + 1)
        if len(s) < 3:
            return None
        span = max(1, len(s) - 1)
        return (s[0][1] - s[-1][1]) / span

    def top_rank_movers(self, days: int = 30, limit: int = 20) -> List[dict]:
        """أسرع العملات صعوداً في ترتيب Market Cap."""
        dates = self.snapshot_dates(days + 1)
        if len(dates) < 2:
            return []
        new_d, old_d = dates[0], dates[-1]
        new, old = self.snapshot(new_d), self.snapshot(old_d)
        out = []
        for cid, r in new.items():
            if cid in old:
                delta = old[cid]["rank"] - r["rank"]
                if delta > 0:
                    out.append({"coin_id": cid, "symbol": r["symbol"], "rank": r["rank"],
                                "prev_rank": old[cid]["rank"], "improved": delta,
                                "from_date": old_d, "to_date": new_d})
        out.sort(key=lambda x: x["improved"], reverse=True)
        return out[:limit]

    def outperformers_vs_btc(self, days: int = 30, limit: int = 20) -> List[dict]:
        """العملات التي تفوّق أداؤها على BTC خلال المدة."""
        dates = self.snapshot_dates(days + 1)
        if len(dates) < 2:
            return []
        new_d, old_d = dates[0], dates[-1]
        new, old = self.snapshot(new_d), self.snapshot(old_d)
        btc_new = next((r["btc_price"] for r in new.values() if r["btc_price"]), 0)
        btc_old = next((r["btc_price"] for r in old.values() if r["btc_price"]), 0)
        if not btc_new or not btc_old:
            return []
        btc_ret = (btc_new / btc_old - 1) * 100
        out = []
        for cid, r in new.items():
            o = old.get(cid)
            if not o or not o["price"]:
                continue
            ret = (r["price"] / o["price"] - 1) * 100
            out.append({"coin_id": cid, "symbol": r["symbol"], "return_pct": ret,
                        "btc_return_pct": btc_ret, "excess_pct": ret - btc_ret, "days": days})
        out.sort(key=lambda x: x["excess_pct"], reverse=True)
        return [o for o in out if o["excess_pct"] > 0][:limit]

    def volume_leading_price(self, days: int = 14, vol_growth: float = 50.0,
                             max_price_move: float = 10.0, limit: int = 20) -> List[dict]:
        """نمط: الفوليوم يرتفع بوضوح بينما السعر ما زال هادئاً (تجميع محتمل)."""
        dates = self.snapshot_dates(days + 1)
        if len(dates) < 2:
            return []
        new_d, old_d = dates[0], dates[-1]
        new, old = self.snapshot(new_d), self.snapshot(old_d)
        out = []
        for cid, r in new.items():
            o = old.get(cid)
            if not o or not o["volume_24h"] or not o["price"]:
                continue
            dv = (r["volume_24h"] / o["volume_24h"] - 1) * 100
            dp = (r["price"] / o["price"] - 1) * 100
            if dv >= vol_growth and abs(dp) <= max_price_move:
                out.append({"coin_id": cid, "symbol": r["symbol"], "volume_growth_pct": dv,
                            "price_move_pct": dp, "days": days})
        out.sort(key=lambda x: x["volume_growth_pct"], reverse=True)
        return out[:limit]

    def recent_events(self, days: int = 30, event: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM list_events"
        args: list = []
        if event:
            q += " WHERE event=?"
            args.append(event)
        q += " ORDER BY ts DESC LIMIT 500"
        rows = self.conn.execute(q, args).fetchall()
        cutoff = time.time() - days * 86400
        return [dict(r) for r in rows if r["ts"] >= cutoff]

    def newly_entered(self, days: int = 30) -> List[dict]:
        return self.recent_events(days, "ENTER")

    def exited_history(self, days: int = 365) -> List[dict]:
        return self.recent_events(days, "EXIT")

    # ── حالة السوق ──
    def save_regime(self, regime: str, score: float, metrics: dict, date: Optional[str] = None) -> None:
        date = date or _today()
        self.conn.execute(
            "INSERT OR REPLACE INTO regime_history (date, ts, regime, score, metrics) VALUES (?,?,?,?,?)",
            (date, int(time.time()), regime, score, json.dumps(metrics, ensure_ascii=False)))
        self.conn.commit()

    def regime_series(self, days: int = 30) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM regime_history ORDER BY date DESC LIMIT ?", (days,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_regime(self) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM regime_history ORDER BY date DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ── الإشارات (قاعدة الأنماط) ──
    def record_signal(self, sig: dict) -> None:
        self.conn.execute(
            "INSERT INTO signals (ts, date, coin_id, symbol, rank, score, regime, payload) VALUES (?,?,?,?,?,?,?,?)",
            (int(time.time()), _today(), sig.get("coin_id", ""), sig.get("symbol", ""),
             sig.get("rank"), sig.get("score"), sig.get("regime"),
             json.dumps(sig, ensure_ascii=False, default=str)))
        self.conn.commit()

    def last_signal(self, coin_id: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT * FROM signals WHERE coin_id=? ORDER BY ts DESC LIMIT 1", (coin_id,)).fetchone()
        return dict(r) if r else None

    def close(self) -> None:
        self.conn.close()
