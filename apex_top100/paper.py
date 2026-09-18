#!/usr/bin/env python3
"""
Paper Trading / Forward Testing لمسار Top 100.

كل إشارة تُفتح كمركز ورقي بأسعار السوق الحقيقية ثم تُدار كما ستُدار الصفقة الحقيقية:
  • دخول عند السوق إذا كان السعر داخل منطقة الدخول، وإلا أمر معلّق ينتهي بعد مدة
  • خروج على الإبطال (Invalidation) أو الأهداف الثلاثة أو الوقت الأقصى
  • خروج جزئي: 40% عند TP1، 30% عند TP2، 30% عند TP3، ونقل الوقف للتعادل بعد TP1
  • تتبّع أقصى ربح وأقصى تراجع داخل الصفقة (MFE / MAE)

الهدف إثبات الأداء ببيانات حقيقية قبل السماح بأي تنفيذ بأموال حقيقية.
لا يُرسل هذا الملف أي أمر إلى منصة — كل شيء ورقي داخل قاعدة البيانات.
"""
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id       TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    status        TEXT NOT NULL,            -- PENDING | OPEN | CLOSED | EXPIRED
    opened_at     INTEGER,
    signaled_at   INTEGER NOT NULL,
    closed_at     INTEGER,
    entry_low     REAL, entry_high REAL,
    entry         REAL,
    invalidation  REAL,
    stop          REAL,                     -- الوقف الحالي (ينتقل للتعادل بعد TP1)
    tp1 REAL, tp2 REAL, tp3 REAL,
    size_pct      REAL,
    remaining     REAL,                     -- نسبة المركز المتبقية (1.0 = كامل)
    realized_r    REAL DEFAULT 0.0,         -- الربح المحقق بوحدات المخاطرة
    realized_pct  REAL DEFAULT 0.0,         -- الربح المحقق % من قيمة المركز
    mfe_pct       REAL DEFAULT 0.0,
    mae_pct       REAL DEFAULT 0.0,
    score         REAL,
    regime        TEXT,
    rank          INTEGER,
    risk          TEXT,
    data_quality  TEXT,
    exit_reason   TEXT,
    exit_price    REAL,
    hits          TEXT DEFAULT '[]',        -- الأهداف التي تحققت
    UNIQUE(coin_id, signaled_at)
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions(status);

CREATE TABLE IF NOT EXISTS paper_equity (
    date      TEXT PRIMARY KEY,
    ts        INTEGER NOT NULL,
    equity    REAL NOT NULL,
    open_n    INTEGER,
    closed_n  INTEGER
);
"""

# نسب الخروج عند كل هدف
TP_FRACTIONS = (0.40, 0.30, 0.30)


def _now() -> int:
    return int(time.time())


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class PaperConfig:
    start_equity: float = 1000.0
    entry_expiry_days: int = 3        # الأمر المعلّق ينتهي بعد هذه المدة
    max_hold_days: int = 45           # خروج زمني إن لم يتحرك شيء
    breakeven_after_tp1: bool = True
    slippage_pct: float = 0.05        # انزلاق مفترض على الدخول والخروج


class PaperBroker:
    """وسيط ورقي يعمل على نفس قاعدة بيانات المحرك."""

    def __init__(self, conn: sqlite3.Connection, cfg: Optional[PaperConfig] = None):
        self.conn = conn
        self.cfg = cfg or PaperConfig()
        self.conn.executescript(PAPER_SCHEMA)
        self.conn.commit()

    # ══════════════════════════════════════
    #  فتح مركز من إشارة
    # ══════════════════════════════════════
    def open_from_signal(self, sig) -> Optional[int]:
        d = sig.to_dict() if hasattr(sig, "to_dict") else dict(sig)
        if not d.get("tradable", True) or d.get("data_quality") == "price_only":
            return None
        if self.has_active(d["coin_id"]):
            return None

        price = float(d["price"])
        entry_low, entry_high = float(d["entry_low"]), float(d["entry_high"])
        immediate = entry_low <= price <= entry_high * 1.005
        entry = self._with_slippage(price if immediate else entry_high, buy=True) if immediate else None

        cur = self.conn.execute("""
            INSERT OR IGNORE INTO paper_positions
            (coin_id, symbol, status, opened_at, signaled_at, entry_low, entry_high, entry,
             invalidation, stop, tp1, tp2, tp3, size_pct, remaining, score, regime, rank,
             risk, data_quality)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1.0,?,?,?,?,?)
        """, (d["coin_id"], d["symbol"], "OPEN" if immediate else "PENDING",
              _now() if immediate else None, _now(), entry_low, entry_high, entry,
              float(d["invalidation"]), float(d["invalidation"]),
              float(d["tp1"]), float(d["tp2"]), float(d["tp3"]), float(d["position_pct"]),
              float(d["score"]), d.get("regime"), d.get("rank"), d.get("risk"),
              d.get("data_quality", "ohlc")))
        self.conn.commit()
        return cur.lastrowid or None

    def has_active(self, coin_id: str) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM paper_positions WHERE coin_id=? AND status IN ('PENDING','OPEN') LIMIT 1",
            (coin_id,)).fetchone()
        return r is not None

    def _with_slippage(self, price: float, buy: bool) -> float:
        k = self.cfg.slippage_pct / 100.0
        return price * (1 + k) if buy else price * (1 - k)

    # ══════════════════════════════════════
    #  تحديث المراكز بأسعار السوق
    # ══════════════════════════════════════
    def update(self, prices: Dict[str, float]) -> List[dict]:
        """prices: {coin_id: السعر الحالي}. يعيد الأحداث التي وقعت."""
        events: List[dict] = []
        rows = self.conn.execute(
            "SELECT * FROM paper_positions WHERE status IN ('PENDING','OPEN')").fetchall()
        for row in rows:
            price = prices.get(row["coin_id"])
            if price is None:
                continue
            if row["status"] == "PENDING":
                events += self._handle_pending(row, price)
            else:
                events += self._handle_open(row, price)
        self.conn.commit()
        return events

    def _handle_pending(self, row, price: float) -> List[dict]:
        age_days = (_now() - row["signaled_at"]) / 86400.0
        if price <= row["entry_high"] and price >= row["invalidation"]:
            entry = self._with_slippage(price, buy=True)
            self.conn.execute(
                "UPDATE paper_positions SET status='OPEN', opened_at=?, entry=? WHERE id=?",
                (_now(), entry, row["id"]))
            return [{"event": "FILLED", "symbol": row["symbol"], "price": entry}]
        if price < row["invalidation"]:
            self.conn.execute(
                "UPDATE paper_positions SET status='EXPIRED', closed_at=?, exit_reason='invalidated_before_entry' WHERE id=?",
                (_now(), row["id"]))
            return [{"event": "EXPIRED", "symbol": row["symbol"], "reason": "كسر الإبطال قبل الدخول"}]
        if age_days >= self.cfg.entry_expiry_days:
            self.conn.execute(
                "UPDATE paper_positions SET status='EXPIRED', closed_at=?, exit_reason='entry_window_expired' WHERE id=?",
                (_now(), row["id"]))
            return [{"event": "EXPIRED", "symbol": row["symbol"], "reason": "انتهت نافذة الدخول"}]
        return []

    def _handle_open(self, row, price: float) -> List[dict]:
        events: List[dict] = []
        entry = row["entry"] or row["entry_high"]
        risk = max(entry - row["invalidation"], entry * 0.001)
        move_pct = (price / entry - 1) * 100.0
        mfe = max(row["mfe_pct"] or 0.0, move_pct)
        mae = min(row["mae_pct"] or 0.0, move_pct)
        self.conn.execute("UPDATE paper_positions SET mfe_pct=?, mae_pct=? WHERE id=?",
                          (mfe, mae, row["id"]))

        hits = json.loads(row["hits"] or "[]")
        remaining = row["remaining"]
        realized_r = row["realized_r"] or 0.0
        realized_pct = row["realized_pct"] or 0.0
        stop = row["stop"]

        # الأهداف
        for i, tp_key in enumerate(("tp1", "tp2", "tp3")):
            tp = row[tp_key]
            if tp and price >= tp and tp_key not in hits:
                frac = TP_FRACTIONS[i]
                exit_price = self._with_slippage(tp, buy=False)
                realized_r += (exit_price - entry) / risk * frac
                realized_pct += (exit_price / entry - 1) * 100.0 * frac
                remaining = max(0.0, remaining - frac)
                hits.append(tp_key)
                events.append({"event": tp_key.upper(), "symbol": row["symbol"], "price": exit_price})
                if tp_key == "tp1" and self.cfg.breakeven_after_tp1:
                    stop = max(stop, entry)

        if remaining <= 0.001:
            self._close(row["id"], price, "targets_reached", realized_r, realized_pct, hits, stop)
            events.append({"event": "CLOSED", "symbol": row["symbol"], "reason": "تحققت الأهداف",
                           "r": round(realized_r, 2)})
            return events

        # الوقف / الإبطال
        if price <= stop:
            exit_price = self._with_slippage(stop, buy=False)
            realized_r += (exit_price - entry) / risk * remaining
            realized_pct += (exit_price / entry - 1) * 100.0 * remaining
            reason = "stop_breakeven" if stop >= entry else "invalidation"
            self._close(row["id"], exit_price, reason, realized_r, realized_pct, hits, stop)
            events.append({"event": "CLOSED", "symbol": row["symbol"],
                           "reason": "وقف عند التعادل" if stop >= entry else "كسر الإبطال",
                           "r": round(realized_r, 2)})
            return events

        # الوقت الأقصى
        if row["opened_at"] and (_now() - row["opened_at"]) / 86400.0 >= self.cfg.max_hold_days:
            exit_price = self._with_slippage(price, buy=False)
            realized_r += (exit_price - entry) / risk * remaining
            realized_pct += (exit_price / entry - 1) * 100.0 * remaining
            self._close(row["id"], exit_price, "max_hold", realized_r, realized_pct, hits, stop)
            events.append({"event": "CLOSED", "symbol": row["symbol"], "reason": "انتهت المدة القصوى",
                           "r": round(realized_r, 2)})
            return events

        self.conn.execute(
            "UPDATE paper_positions SET remaining=?, realized_r=?, realized_pct=?, hits=?, stop=? WHERE id=?",
            (remaining, realized_r, realized_pct, json.dumps(hits), stop, row["id"]))
        return events

    def _close(self, pid: int, exit_price: float, reason: str, realized_r: float,
               realized_pct: float, hits: List[str], stop: float) -> None:
        self.conn.execute("""
            UPDATE paper_positions SET status='CLOSED', closed_at=?, exit_price=?, exit_reason=?,
                   realized_r=?, realized_pct=?, remaining=0.0, hits=?, stop=?
            WHERE id=?""",
            (_now(), exit_price, reason, realized_r, realized_pct, json.dumps(hits), stop, pid))

    # ══════════════════════════════════════
    #  الأداء
    # ══════════════════════════════════════
    def open_positions(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM paper_positions WHERE status IN ('PENDING','OPEN') ORDER BY signaled_at DESC").fetchall()
        return [dict(r) for r in rows]

    def closed_positions(self, limit: int = 200) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM paper_positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM paper_positions WHERE status='CLOSED'").fetchall()]
        n = len(rows)
        if not n:
            return {"trades": 0, "note": "لا صفقات مغلقة بعد — التجربة ما زالت جارية",
                    "open": len(self.open_positions()), "equity": self.equity()}
        wins = [r for r in rows if (r["realized_r"] or 0) > 0]
        losses = [r for r in rows if (r["realized_r"] or 0) <= 0]
        rs = [r["realized_r"] or 0.0 for r in rows]
        by_regime: Dict[str, List[float]] = {}
        for r in rows:
            by_regime.setdefault(r["regime"] or "?", []).append(r["realized_r"] or 0.0)
        holds = [((r["closed_at"] or 0) - (r["opened_at"] or r["signaled_at"])) / 86400.0 for r in rows]
        return {
            "trades": n,
            "open": len(self.open_positions()),
            "win_rate": round(len(wins) / n * 100, 1),
            "avg_r": round(sum(rs) / n, 2),
            "total_r": round(sum(rs), 2),
            "expectancy_r": round(sum(rs) / n, 2),
            "best_r": round(max(rs), 2),
            "worst_r": round(min(rs), 2),
            "avg_win_r": round(sum(r["realized_r"] for r in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss_r": round(sum(r["realized_r"] for r in losses) / len(losses), 2) if losses else 0.0,
            "avg_hold_days": round(sum(holds) / n, 1) if holds else 0.0,
            "by_regime": {k: {"trades": len(v), "avg_r": round(sum(v) / len(v), 2)}
                          for k, v in by_regime.items()},
            "equity": self.equity(),
        }

    def equity(self) -> float:
        """رأس المال الورقي: كل صفقة تخاطر بنسبة حجمها من رأس المال."""
        eq = self.cfg.start_equity
        for r in self.conn.execute(
                "SELECT size_pct, realized_pct FROM paper_positions WHERE status='CLOSED'"):
            eq *= 1 + (r["size_pct"] or 0) / 100.0 * (r["realized_pct"] or 0) / 100.0
        return round(eq, 2)

    def record_equity(self) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_equity (date, ts, equity, open_n, closed_n) VALUES (?,?,?,?,?)",
            (_today(), _now(), self.equity(), len(self.open_positions()),
             self.conn.execute("SELECT COUNT(*) c FROM paper_positions WHERE status='CLOSED'").fetchone()["c"]))
        self.conn.commit()

    def equity_curve(self, days: int = 180) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM paper_equity ORDER BY date DESC LIMIT ?", (days,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def report(self) -> str:
        s = self.stats()
        if not s.get("trades"):
            return (f"Forward Test — لا صفقات مغلقة بعد\n"
                    f"مراكز مفتوحة: {s['open']} | رأس المال الورقي: ${s['equity']}")
        lines = [
            "APEX TOP-100 — FORWARD TEST",
            "",
            f"الصفقات المغلقة: {s['trades']}   |   المفتوحة: {s['open']}",
            f"نسبة الربح: {s['win_rate']}%",
            f"المتوسط لكل صفقة: {s['avg_r']}R   |   الإجمالي: {s['total_r']}R",
            f"متوسط الرابحة: {s['avg_win_r']}R   |   متوسط الخاسرة: {s['avg_loss_r']}R",
            f"أفضل: {s['best_r']}R   |   أسوأ: {s['worst_r']}R",
            f"متوسط مدة الصفقة: {s['avg_hold_days']} يوم",
            f"رأس المال الورقي: ${s['equity']} (البداية ${self.cfg.start_equity})",
            "",
            "حسب حالة السوق:",
        ]
        for regime, v in s["by_regime"].items():
            lines.append(f"  {regime}: {v['trades']} صفقة، متوسط {v['avg_r']}R")
        return "\n".join(lines)
