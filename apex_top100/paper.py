#!/usr/bin/env python3
"""
Paper Trading / Forward Testing لمسار Top 100.

كل إشارة تُفتح كمركز ورقي بأسعار السوق الحقيقية ثم تُدار كما ستُدار الصفقة الحقيقية:
  • دخول عند السوق إذا كان السعر داخل منطقة الدخول، وإلا أمر معلّق ينتهي بعد مدة
  • خروج على الإبطال (Invalidation) أو الأهداف الثلاثة أو الوقت الأقصى
  • خروج جزئي: 40% عند TP1، 30% عند TP2، 30% عند TP3، ونقل الوقف للتعادل بعد TP1
  • تتبّع أقصى ربح وأقصى تراجع داخل الصفقة (MFE / MAE)

**الإدارة تتم على شموع OHLC الحقيقية لكل فترة منذ آخر تحديث، لا على لقطة سعر واحدة**،
لأن اللقطة تُعمي المحاكاة عمّا حدث بين دورتين: سعر يهبط فيضرب الوقف ثم يرتد فوق الهدف
كان سيُحتسب ربحاً بينما الصفقة الحقيقية أُغلقت خاسرة.

سياسة ترتيب الأحداث داخل الشمعة الواحدة (مصرَّح بها ومختبَرة):
  1. الافتتاح أولاً: إن فتحت الشمعة تحت الوقف يُنفَّذ الخروج عند سعر الافتتاح (فجوة هابطة)،
     وإن فتحت فوق هدف يُنفَّذ الهدف عند سعر الافتتاح.
  2. عند الغموض — لمست الشمعة الوقف والهدف معاً — **يُفترض الوقف أولاً**. هذا افتراض محافظ
     يمنع منح الاستراتيجية أفضل نتيجة تلقائياً، لأن هدف الاختبار دليل يُعتمد عليه لا رقم جميل.
  3. MFE و MAE يُحسبان من High و Low لا من أسعار اللقطات.

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
    last_bar_ts   INTEGER DEFAULT 0,        -- زمن آخر شمعة عولجت (يمنع التكرار)
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


# ══════════════════════════════════════════
#  شمعة واحدة
# ══════════════════════════════════════════
@dataclass
class Bar:
    """شمعة OHLC واحدة. ts = زمن فتح الشمعة بالثواني."""
    ts: int
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def coerce(cls, b) -> "Bar":
        if isinstance(b, Bar):
            return b
        if isinstance(b, dict):
            return cls(int(b.get("ts") or b.get("time") or 0), float(b["open"]),
                       float(b["high"]), float(b["low"]), float(b["close"]))
        ts, o, h, l, c = b
        return cls(int(ts), float(o), float(h), float(l), float(c))


def bars_from_ohlcv(ohlcv) -> List[Bar]:
    """
    يحوّل كائن OHLCV إلى شموع.

    يرفض بيانات price_only لأنها بلا High/Low حقيقية: إدارة مركز على إغلاق
    مُكرَّر كـ O=H=L=C اختراعٌ للبيانات، وهذا ما نتجنّبه في الـ Forward Test.
    """
    if ohlcv is None or getattr(ohlcv, "price_only", False):
        return []
    n = len(ohlcv.closes)
    if not n or min(len(ohlcv.opens), len(ohlcv.highs), len(ohlcv.lows)) < n:
        return []
    times = ohlcv.times if len(ohlcv.times) == n else [0] * n
    return [Bar(int(times[i]), float(ohlcv.opens[i]), float(ohlcv.highs[i]),
                float(ohlcv.lows[i]), float(ohlcv.closes[i])) for i in range(n)]


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
        if not d.get("tradable", True) or d.get("data_quality") in ("price_only", "stale"):
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
             risk, data_quality, last_bar_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1.0,?,?,?,?,?,?)
        """, (d["coin_id"], d["symbol"], "OPEN" if immediate else "PENDING",
              _now() if immediate else None, _now(), entry_low, entry_high, entry,
              float(d["invalidation"]), float(d["invalidation"]),
              float(d["tp1"]), float(d["tp2"]), float(d["tp3"]), float(d["position_pct"]),
              float(d["score"]), d.get("regime"), d.get("rank"), d.get("risk"),
              d.get("data_quality", "ohlc"), _now()))
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
    #  الإدارة على شموع OHLC حقيقية
    # ══════════════════════════════════════
    def apply_candles(self, candles: Dict[str, object]) -> List[dict]:
        """
        candles: {coin_id: شموع الفترة} — قائمة Bar/dict/tuple أو كائن OHLCV.

        تُعالَج كل شمعة منذ آخر شمعة عولجت (ts >= last_bar_ts) بالترتيب الزمني،
        فلا تُفقد حركة وقعت بين دورتين. إعادة معالجة الشمعة الجارية آمنة:
        الأهداف محميّة بقائمة hits، والوقف يُغلق المركز مرة واحدة،
        و MFE/MAE دوال max/min.

        الشمعة التي وقعت فيها الإشارة لا تُعالَج (last_bar_ts يبدأ من زمن الإشارة)،
        حتى لا يُحسب هبوط حدث قبل الدخول وقفاً على صفقة لم تكن قائمة بعد.
        """
        events: List[dict] = []
        rows = self.conn.execute(
            "SELECT * FROM paper_positions WHERE status IN ('PENDING','OPEN')").fetchall()
        for row in rows:
            raw = candles.get(row["coin_id"])
            if raw is None:
                continue
            since = max(int(row["last_bar_ts"] or 0), int(row["signaled_at"] or 0))
            bars = self._bars_for(raw, since)
            if not bars:
                continue
            events += self._walk(row, bars)
        self.conn.commit()
        return events

    @staticmethod
    def _bars_for(raw, since_ts: int) -> List["Bar"]:
        bars = bars_from_ohlcv(raw) if hasattr(raw, "closes") else [Bar.coerce(b) for b in raw]
        bars = [b for b in bars if b.ts >= since_ts]
        bars.sort(key=lambda b: b.ts)
        return bars

    def _walk(self, row, bars: List["Bar"]) -> List[dict]:
        st = dict(row)
        st["realized_r"] = float(st["realized_r"] or 0.0)
        st["realized_pct"] = float(st["realized_pct"] or 0.0)
        st["remaining"] = float(st["remaining"] or 0.0)
        st["mfe_pct"] = float(st["mfe_pct"] or 0.0)
        st["mae_pct"] = float(st["mae_pct"] or 0.0)
        events: List[dict] = []
        for bar in bars:
            just_filled = False
            if st["status"] == "PENDING":
                evs = self._bar_pending(st, bar)
                events += evs
                just_filled = any(e["event"] == "FILLED" for e in evs)
            if st["status"] == "OPEN":
                events += self._bar_open(st, bar, gap_check=not just_filled)
            st["last_bar_ts"] = max(int(st["last_bar_ts"] or 0), bar.ts)
            if st["status"] in ("CLOSED", "EXPIRED"):
                break
        self._flush(st)
        return events

    # ── أمر معلّق ──────────────────────────
    def _bar_pending(self, st: dict, bar: "Bar") -> List[dict]:
        inval = float(st["invalidation"])
        entry_high = float(st["entry_high"])
        ts = bar.ts or _now()

        # فجوة تحت الإبطال: السعر لم يتداول داخل منطقة الدخول أصلاً → لا تنفيذ
        if bar.open < inval:
            st.update(status="EXPIRED", closed_at=ts, exit_reason="invalidated_before_entry")
            return [{"event": "EXPIRED", "symbol": st["symbol"],
                     "reason": "فجوة تحت الإبطال قبل الدخول"}]

        fill = None
        if bar.open <= entry_high:
            fill = bar.open              # فتحت داخل المنطقة أو تحتها
        elif bar.low <= entry_high:
            fill = entry_high            # نزلت إلى المنطقة خلال الشمعة (أمر محدَّد)

        if fill is not None:
            st.update(status="OPEN", opened_at=ts, entry=self._with_slippage(fill, buy=True))
            return [{"event": "FILLED", "symbol": st["symbol"], "price": st["entry"]}]

        if (ts - int(st["signaled_at"])) / 86400.0 >= self.cfg.entry_expiry_days:
            st.update(status="EXPIRED", closed_at=ts, exit_reason="entry_window_expired")
            return [{"event": "EXPIRED", "symbol": st["symbol"], "reason": "انتهت نافذة الدخول"}]
        return []

    # ── مركز مفتوح ─────────────────────────
    def _bar_open(self, st: dict, bar: "Bar", gap_check: bool = True) -> List[dict]:
        events: List[dict] = []
        entry = float(st["entry"] or st["entry_high"])
        risk = max(entry - float(st["invalidation"]), entry * 0.001)
        hits: List[str] = list(json.loads(st["hits"] or "[]"))
        ts = bar.ts or _now()
        mfe_ref = bar.open
        stopped = False

        st["mae_pct"] = min(st["mae_pct"], (bar.low / entry - 1) * 100.0)

        def book(index: int, key: str, at_price: float) -> None:
            frac = TP_FRACTIONS[index]
            ex = self._with_slippage(at_price, buy=False)
            st["realized_r"] += (ex - entry) / risk * frac
            st["realized_pct"] += (ex / entry - 1) * 100.0 * frac
            st["remaining"] = max(0.0, st["remaining"] - frac)
            hits.append(key)
            events.append({"event": key.upper(), "symbol": st["symbol"], "price": ex})
            if key == "tp1" and self.cfg.breakeven_after_tp1:
                st["stop"] = max(float(st["stop"]), entry)

        def close(at_price: float, reason: str, label: str) -> None:
            frac = st["remaining"]
            ex = self._with_slippage(at_price, buy=False)
            st["realized_r"] += (ex - entry) / risk * frac
            st["realized_pct"] += (ex / entry - 1) * 100.0 * frac
            st.update(status="CLOSED", closed_at=ts, exit_price=ex, exit_reason=reason, remaining=0.0)
            events.append({"event": "CLOSED", "symbol": st["symbol"], "reason": label,
                           "r": round(st["realized_r"], 2)})

        def finish(at_price: float, reason: str, label: str) -> None:
            st.update(status="CLOSED", closed_at=ts, exit_price=at_price, exit_reason=reason,
                      remaining=0.0)
            events.append({"event": "CLOSED", "symbol": st["symbol"], "reason": label,
                           "r": round(st["realized_r"], 2)})

        # (1) الافتتاح أولاً — الفجوة الهابطة تُنفَّذ عند سعر الافتتاح لا عند سعر الوقف النظري
        if gap_check:
            if bar.open <= float(st["stop"]):
                stopped = True
                at_be = float(st["stop"]) >= entry
                gapped = bar.open < float(st["stop"])
                label = "وقف عند التعادل" if at_be else "كسر الإبطال"
                close(bar.open, "stop_breakeven" if at_be else "invalidation",
                      ("فجوة هابطة عبر " + label) if gapped else label)
            else:
                # فتحت فوق هدف: الهدف تحقق يقيناً قبل أي هبوط لاحق في الشمعة.
                # يُحتسب عند سعر الهدف لا عند الافتتاح الأعلى — لا نمنح الاستراتيجية الفجوة.
                for i, key in enumerate(("tp1", "tp2", "tp3")):
                    tp = st[key]
                    if tp and key not in hits and bar.open >= float(tp):
                        book(i, key, float(tp))
                        mfe_ref = max(mfe_ref, float(tp))

        if st["status"] == "OPEN" and st["remaining"] <= 0.001:
            finish(self._with_slippage(float(st["tp3"] or bar.open), buy=False),
                   "targets_reached", "تحققت الأهداف")

        # (2) الحالة الغامضة — الشمعة لمست الوقف والهدف معاً: **يُفترض الوقف أولاً**
        #     افتراض محافظ يمنع منح الاستراتيجية أفضل نتيجة تلقائياً.
        if st["status"] == "OPEN" and bar.low <= float(st["stop"]):
            stopped = True
            at_breakeven = float(st["stop"]) >= entry
            close(float(st["stop"]),
                  "stop_breakeven" if at_breakeven else "invalidation",
                  "وقف عند التعادل" if at_breakeven else "كسر الإبطال")

        # (3) الأهداف داخل الشمعة (لم يُضرب الوقف)
        if st["status"] == "OPEN":
            for i, key in enumerate(("tp1", "tp2", "tp3")):
                tp = st[key]
                if tp and key not in hits and bar.high >= float(tp):
                    book(i, key, float(tp))
                    mfe_ref = max(mfe_ref, float(tp))
            if st["remaining"] <= 0.001:
                finish(self._with_slippage(float(st["tp3"] or bar.close), buy=False),
                       "targets_reached", "تحققت الأهداف")
            elif bar.low <= float(st["stop"]):
                # الوقف انتقل للتعادل بعد الهدف داخل هذه الشمعة، وقاع الشمعة تحته.
                # لا نعرف الترتيب، فنفترض أن القاع جاء بعد الهدف — وهو الافتراض المحافظ.
                stopped = True
                close(float(st["stop"]), "stop_breakeven", "وقف عند التعادل بعد الهدف")

        # (4) المدة القصوى
        if st["status"] == "OPEN" and st["opened_at"] and \
                (ts - int(st["opened_at"])) / 86400.0 >= self.cfg.max_hold_days:
            close(bar.close, "max_hold", "انتهت المدة القصوى")

        # (5) MFE/MAE من High/Low لا من لقطة سعر.
        #     إن ضُرب الوقف في هذه الشمعة لا يُحتسب High لأنه — بافتراضنا المحافظ — جاء بعد الخروج.
        if not stopped:
            mfe_ref = max(mfe_ref, bar.high)
        st["mfe_pct"] = max(st["mfe_pct"], (mfe_ref / entry - 1) * 100.0)
        st["hits"] = json.dumps(hits)
        return events

    def _flush(self, st: dict) -> None:
        self.conn.execute("""
            UPDATE paper_positions SET status=?, opened_at=?, closed_at=?, entry=?, stop=?,
                   remaining=?, realized_r=?, realized_pct=?, mfe_pct=?, mae_pct=?,
                   exit_reason=?, exit_price=?, hits=?, last_bar_ts=?
            WHERE id=?""",
            (st["status"], st["opened_at"], st["closed_at"], st["entry"], st["stop"],
             st["remaining"], st["realized_r"], st["realized_pct"], st["mfe_pct"], st["mae_pct"],
             st["exit_reason"], st["exit_price"],
             st["hits"] if isinstance(st["hits"], str) else json.dumps(st["hits"] or []),
             int(st["last_bar_ts"] or 0), st["id"]))

    def update(self, prices: Dict[str, float]) -> List[dict]:
        """
        بديل متدهور: لقطة سعر واحدة تُعامَل كشمعة O=H=L=C.

        محفوظ للتوافق فقط. لا يُستعمل في الـ Forward Test لأن اللقطة تُخفي
        ما حدث بين دورتين — الإدارة الحقيقية عبر apply_candles.
        """
        ts = _now()
        return self.apply_candles(
            {cid: [Bar(ts, float(p), float(p), float(p), float(p))]
             for cid, p in prices.items() if p})

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
