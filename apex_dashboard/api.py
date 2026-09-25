#!/usr/bin/env python3
"""
قارئ بيانات لوحة APEX — للقراءة فقط.

القاعدة: هذه الطبقة لا تغيّر شيئاً يخص الـForward Test.
  • الاتصال بـSQLite عبر `file:...?mode=ro` — أي محاولة كتابة تفشل من SQLite نفسه
    لا من انضباطنا فقط.
  • لا يُنشأ `PaperBroker` بطريقته العادية لأن `__init__` ينفّذ `CREATE TABLE`.
    نعيد استعمال دوال الأداء نفسها (`stats`, `max_drawdown`, ...) على اتصال
    للقراءة فقط، فأرقام اللوحة هي أرقام `--paper-report` حرفياً لا نسخة موازية.
  • الشموع تُقرأ من كاش المحرك (`kl_<PAIR>_1d.json`) ولا تُطلب من الشبكة هنا.
    الأسعار الحية تأتي من Binance WebSocket داخل المتصفح، للعرض فقط.

ما يُشتق ولا يُخزَّن يُعلَّم صراحةً: أزمنة ضرب الأهداف تُستنتج من الشموع
(`source="inferred"`) لأن الجدول يحفظ أيّ الأهداف تحققت لا متى.
"""
import inspect
import json
import os
import re
import sqlite3
import time
import urllib.parse
from typing import Dict, List, Optional

from apex_top100.config import Top100Config
from apex_top100.integration import ApexV2Bridge
from apex_top100.paper import PaperBroker, PaperConfig

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,20}$")
STATUSES = ("OPEN", "PENDING", "CLOSED", "EXPIRED")

# فترات المتوسطات المعروضة على الشارت اليومي
SMA_PERIODS = (20, 50)
EMA_PERIODS = (21,)
RSI_PERIOD = 14


class DashboardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ══════════════════════════════════════════
#  سلاسل المؤشرات (نفس معادلات apex_top100.indicators، لكن لكل شمعة)
# ══════════════════════════════════════════
def sma_series(values: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema_series(values: List[float], n: int) -> List[Optional[float]]:
    """تبدأ بمتوسط أول n قيمة كما في `indicators.ema`، فآخر قيمة تطابقها."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < n or n <= 0:
        return out
    k = 2.0 / (n + 1.0)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_series(values: List[float], n: int = 14) -> List[Optional[float]]:
    """RSI بتنعيم Wilder كما في `indicators.rsi`، فآخر قيمة تطابقها."""
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < n + 1:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n

    def value(ag_: float, al_: float) -> float:
        return 100.0 if al_ == 0 else 100.0 - 100.0 / (1.0 + ag_ / al_)

    out[n] = value(ag, al)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = value(ag, al)
    return out


def _bar_time_for(times: List[int], ts: Optional[int]) -> Optional[int]:
    """زمن الشمعة اليومية التي يقع فيها الحدث (أكبر فتح ≤ ts)."""
    if ts is None or not times:
        return None
    best = None
    for t in times:
        if t <= ts:
            best = t
        else:
            break
    return best


def _auto_execute_default() -> bool:
    """القيمة الافتراضية في الكود، لا ثابت منسوخ: إن تغيّرت في الجسر تظهر هنا."""
    return bool(inspect.signature(ApexV2Bridge.__init__).parameters["auto_execute"].default)


# ══════════════════════════════════════════
#  القارئ
# ══════════════════════════════════════════
class DashboardReader:
    def __init__(self, db_path: str, cache_dir: str,
                 cfg: Optional[Top100Config] = None):
        self.db_path = os.path.abspath(db_path)
        self.cache_dir = os.path.abspath(cache_dir)
        self.cfg = cfg or Top100Config()
        self.paper_cfg = PaperConfig(
            start_equity=self.cfg.paper_start_equity,
            entry_expiry_days=self.cfg.paper_entry_expiry_days,
            max_hold_days=self.cfg.paper_max_hold_days,
            slippage_pct=self.cfg.paper_slippage_pct)

    # ── الاتصال ──
    def connect(self) -> sqlite3.Connection:
        if not os.path.exists(self.db_path):
            raise DashboardError(f"قاعدة البيانات غير موجودة: {self.db_path}", 503)
        path = self.db_path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path                        # مسار Windows: file:/C:/...
        uri = "file:" + urllib.parse.quote(path, safe="/:") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _broker(self, conn: sqlite3.Connection) -> PaperBroker:
        # بلا __init__: لا CREATE TABLE على قاعدة الاختبار الرسمي
        b = PaperBroker.__new__(PaperBroker)
        b.conn = conn
        b.cfg = self.paper_cfg
        return b

    @staticmethod
    def _has_table(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None

    # ── الشموع من كاش المحرك ──
    def _cache_path(self, symbol: str) -> str:
        return os.path.join(self.cache_dir, f"kl_{symbol}USDT_1d.json")

    def candles(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if not SYMBOL_RE.match(symbol):
            raise DashboardError("رمز غير صالح")
        path = self._cache_path(symbol)
        base = {"symbol": symbol, "pair": f"{symbol}USDT", "interval": "1d", "candles": [],
                "source": "apex_cache", "cache_age_sec": None}
        if not os.path.exists(path):
            base["note"] = ("لا شموع في كاش المحرك لهذا الزوج: إما بلا زوج USDT على Binance "
                            "أو لم يُسحب بعد")
            return base
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError):
            base["note"] = "ملف الكاش غير مقروء (ربما يُكتب الآن) — أعد المحاولة"
            return base
        out = []
        for r in rows or []:
            try:
                out.append({"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                            "low": float(r[3]), "close": float(r[4]),
                            # quote volume بالدولار — نفس الحقل الذي يستعمله المحرك
                            "volume": float(r[7]) if len(r) > 7 else float(r[5])})
            except (TypeError, ValueError, IndexError):
                continue
        out.sort(key=lambda c: c["time"])
        base["candles"] = out
        base["cache_age_sec"] = round(time.time() - os.path.getmtime(path), 1)
        return base

    def last_price(self, symbol: str) -> Optional[dict]:
        c = self.candles(symbol)
        if not c["candles"]:
            return None
        last = c["candles"][-1]
        return {"price": last["close"], "bar_time": last["time"], "cache_age_sec": c["cache_age_sec"]}

    # ── المراكز ──
    def _position_view(self, row: dict, price: Optional[dict]) -> dict:
        p = dict(row)
        try:
            p["hits"] = json.loads(p.get("hits") or "[]")
        except ValueError:
            p["hits"] = []
        p["pair"] = f"{p['symbol'].upper()}USDT"
        p["mark_price"] = price["price"] if price else None
        p["mark_source"] = "apex_cache_last_close" if price else None
        p["unrealized_pct"] = None
        if p["status"] == "OPEN" and price and p.get("entry"):
            move = (price["price"] / float(p["entry"]) - 1) * 100.0
            p["unrealized_pct"] = round(move * float(p.get("remaining") or 0), 3)
        # R الفعلي بوحدات المخاطرة من سعر الدخول
        if p.get("entry") and p.get("invalidation"):
            p["risk_per_unit"] = float(p["entry"]) - float(p["invalidation"])
        return p

    def positions(self, status: Optional[str] = None) -> List[dict]:
        if status and status.upper() not in STATUSES:
            raise DashboardError("status يجب أن يكون أحد: " + ", ".join(STATUSES))
        conn = self.connect()
        try:
            if not self._has_table(conn, "paper_positions"):
                return []
            q = "SELECT * FROM paper_positions"
            args: tuple = ()
            if status:
                q += " WHERE status=?"
                args = (status.upper(),)
            q += " ORDER BY signaled_at DESC, id DESC"
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()
        prices: Dict[str, Optional[dict]] = {}
        out = []
        for r in rows:
            sym = r["symbol"].upper()
            if sym not in prices:
                prices[sym] = self.last_price(sym) if SYMBOL_RE.match(sym) else None
            out.append(self._position_view(r, prices[sym]))
        return out

    def position(self, position_id: int) -> dict:
        conn = self.connect()
        try:
            if not self._has_table(conn, "paper_positions"):
                raise DashboardError("لا مراكز ورقية بعد", 404)
            r = conn.execute("SELECT * FROM paper_positions WHERE id=?", (position_id,)).fetchone()
        finally:
            conn.close()
        if r is None:
            raise DashboardError("المركز غير موجود", 404)
        sym = r["symbol"].upper()
        view = self._position_view(dict(r), self.last_price(sym) if SYMBOL_RE.match(sym) else None)
        bars = self.candles(sym)["candles"] if SYMBOL_RE.match(sym) else []
        view["timeline"] = self.timeline(view, bars)
        return view

    # ── Execution Timeline ──
    def timeline(self, pos: dict, bars: List[dict]) -> List[dict]:
        """
        SIGNAL → APPROVED → PENDING/FILLED → TP1 → SL→BE → TP2 → TP3/CLOSED

        كل مرحلة: done | active | waiting | skipped. `latency_ms` فارغ في وضع
        PAPER لأنه لا أمر يُرسل إلى منصة — يُملأ مع محرك التنفيذ المستقبلي.
        """
        status = pos["status"]
        signaled = pos.get("signaled_at")
        opened = pos.get("opened_at")
        closed = pos.get("closed_at")
        hits = list(pos.get("hits") or [])
        immediate = opened is not None and signaled is not None and abs(int(opened) - int(signaled)) <= 5
        stages: List[dict] = []

        def add(stage, state, ts=None, price=None, source="db", note=None):
            stages.append({"stage": stage, "state": state, "ts": ts, "price": price,
                           "source": source, "note": note, "latency_ms": None})

        add("SIGNAL", "done", signaled, None, note=f"score {round(float(pos['score']))}, {pos.get('regime') or '—'}"
            if pos.get("score") is not None else pos.get("regime"))
        add("APPROVED", "done", signaled, source="paper",
            note="PAPER / auto_execute=False — موافقة ورقية عبر بوابة الدورة السليمة، لا أمر حقيقي")

        if immediate:
            add("PENDING", "skipped", note="السعر كان داخل منطقة الدخول فنُفِّذ فوراً")
        else:
            add("PENDING", "active" if status == "PENDING" else "done", signaled,
                pos.get("entry_high"), note="أمر محدَّد: السعر أعلى من منطقة الدخول")

        if opened is not None:
            add("FILLED", "done", int(opened), pos.get("entry"))
        elif status == "EXPIRED":
            add("FILLED", "skipped", note="لم يعد السعر إلى منطقة الدخول")
        else:
            add("FILLED", "waiting")

        tp_times = self._infer_tp_times(pos, bars) if opened is not None else {}
        for key in ("tp1", "tp2", "tp3"):
            label = key.upper()
            if key in hits:
                add(label, "done", tp_times.get(key), pos.get(key), source="inferred",
                    note="الزمن مستنتج من الشموع — الجدول يحفظ تحقق الهدف لا زمنه")
            elif status in ("CLOSED", "EXPIRED"):
                add(label, "skipped", price=pos.get(key))
            else:
                add(label, "waiting", price=pos.get(key))
            if key == "tp1":
                if "tp1" in hits and self.paper_cfg.breakeven_after_tp1:
                    add("SL→BE", "done", tp_times.get("tp1"), pos.get("stop"), source="inferred",
                        note="الوقف نُقل إلى سعر الدخول بعد TP1")
                elif status in ("CLOSED", "EXPIRED"):
                    add("SL→BE", "skipped")
                else:
                    add("SL→BE", "waiting")

        if status in ("CLOSED", "EXPIRED"):
            add(status, "done", closed, pos.get("exit_price"), note=pos.get("exit_reason"))
        else:
            add("CLOSED", "waiting")
        return stages

    @staticmethod
    def _infer_tp_times(pos: dict, bars: List[dict]) -> Dict[str, int]:
        """
        أول شمعة بعد الدخول بلغ High فيها الهدف. صحيح لأن الشمعة التي لمست الهدف
        والوقف معاً تُغلق المركز بالوقف أولاً، فلا يدخل الهدف قائمة hits أصلاً.
        """
        start = max(int(pos.get("signaled_at") or 0), int(pos.get("opened_at") or 0))
        end = pos.get("closed_at")
        out: Dict[str, int] = {}
        for key in ("tp1", "tp2", "tp3"):
            tp = pos.get(key)
            if not tp or key not in (pos.get("hits") or []):
                continue
            for b in bars:
                if b["time"] < start - 5:
                    continue
                if end is not None and b["time"] > int(end):
                    break
                if b["high"] >= float(tp):
                    out[key] = b["time"]
                    break
        return out

    # ── الإشارات ──
    def signals(self, limit: int = 100) -> List[dict]:
        limit = max(1, min(int(limit), 1000))
        conn = self.connect()
        try:
            if not self._has_table(conn, "signals"):
                return []
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()]
        finally:
            conn.close()
        keep = ("price", "entry_low", "entry_high", "invalidation", "tp1", "tp2", "tp3",
                "position_pct", "risk", "trend_label", "momentum_label", "volume_state",
                "data_quality", "vs_btc_pct", "vs_eth_pct", "entry_note")
        for r in rows:
            try:
                payload = json.loads(r.pop("payload") or "{}")
            except ValueError:
                payload = {}
            r.update({k: payload.get(k) for k in keep})
        return rows

    # ── الحالة العامة ──
    def status(self) -> dict:
        conn = self.connect()
        try:
            b = self._broker(conn)
            has_paper = self._has_table(conn, "paper_positions")
            counts = {s: 0 for s in STATUSES}
            if has_paper:
                for r in conn.execute("SELECT status, COUNT(*) n FROM paper_positions GROUP BY status"):
                    counts[r["status"]] = r["n"]
            equity = b.equity() if has_paper else self.paper_cfg.start_equity
            dd = b.max_drawdown() if has_paper else {"max_dd_pct": 0.0}
            regime = None
            if self._has_table(conn, "regime_history"):
                r = conn.execute("SELECT * FROM regime_history ORDER BY date DESC LIMIT 1").fetchone()
                if r:
                    regime = {"regime": r["regime"], "score": r["score"], "date": r["date"], "ts": r["ts"]}
            # نبض الحلقة: كل دورة تكتب regime_history و paper_equity بزمن الكتابة.
            # last_bar_ts ليس نبضاً — هو زمن فتح شمعة يومية.
            activity = []
            for table, col in (("regime_history", "ts"), ("paper_equity", "ts"), ("signals", "ts")):
                if self._has_table(conn, table):
                    v = conn.execute(f"SELECT MAX({col}) m FROM {table}").fetchone()["m"]
                    if v:
                        activity.append(int(v))
        finally:
            conn.close()

        start = self.paper_cfg.start_equity
        open_rows = [p for p in self.positions("OPEN")] if has_paper else []
        unreal = 0.0
        marked = 0
        for p in open_rows:
            if p["mark_price"] is None or not p.get("entry"):
                continue
            marked += 1
            # الجزء المحقق من الأهداف + المتبقي على آخر إغلاق، بوزن حجم المركز
            pos_pct = float(p.get("realized_pct") or 0) + (p["unrealized_pct"] or 0)
            unreal += equity * float(p.get("size_pct") or 0) / 100.0 * pos_pct / 100.0
        db_mtime = os.path.getmtime(self.db_path)
        return {
            "mode": "PAPER",
            "auto_execute": _auto_execute_default(),
            "read_only": True,
            "regime": regime,
            "equity": equity,
            "start_equity": start,
            "pnl": round(equity - start, 2),
            "pnl_pct": round((equity / start - 1) * 100.0, 3) if start else 0.0,
            "max_dd_pct": dd["max_dd_pct"],
            "unrealized": {"usd": round(unreal, 2), "positions_marked": marked,
                           "positions_open": len(open_rows),
                           "note": "تقديري من آخر إغلاق في كاش المحرك — ليس جزءاً من رأس المال المحقق"},
            "counts": counts,
            "last_activity_ts": max(activity) if activity else None,
            "db_modified_ts": int(db_mtime),
            "scan_interval_sec": self.cfg.scan_interval_sec,
            "server_ts": int(time.time()),
        }

    # ── الأداء ──
    def performance(self) -> dict:
        conn = self.connect()
        try:
            if not self._has_table(conn, "paper_positions"):
                return {"stats": {"trades": 0}, "realized_curve": [], "daily_equity": [], "regimes": []}
            b = self._broker(conn)
            stats = b.stats()
            curve = b.realized_equity_curve()
            daily = [dict(r) for r in conn.execute(
                "SELECT * FROM paper_equity ORDER BY date ASC").fetchall()] \
                if self._has_table(conn, "paper_equity") else []
            regimes = [dict(r) for r in conn.execute(
                "SELECT date, ts, regime, score FROM regime_history ORDER BY date ASC").fetchall()] \
                if self._has_table(conn, "regime_history") else []
            expired = conn.execute(
                "SELECT exit_reason, COUNT(*) n FROM paper_positions WHERE status='EXPIRED' "
                "GROUP BY exit_reason").fetchall()
        finally:
            conn.close()
        stats["expired"] = {(r["exit_reason"] or "?"): r["n"] for r in expired}
        return {"stats": stats, "realized_curve": curve, "daily_equity": daily,
                "regimes": regimes, "start_equity": self.paper_cfg.start_equity}

    # ── الشارت ──
    def chart(self, symbol: str, position_id: Optional[int] = None) -> dict:
        data = self.candles(symbol)
        bars = data["candles"]
        closes = [c["close"] for c in bars]
        times = [c["time"] for c in bars]

        def series(vals):
            return [{"time": t, "value": round(v, 10)} for t, v in zip(times, vals) if v is not None]

        data["indicators"] = {
            **{f"sma{n}": series(sma_series(closes, n)) for n in SMA_PERIODS},
            **{f"ema{n}": series(ema_series(closes, n)) for n in EMA_PERIODS},
            f"rsi{RSI_PERIOD}": series(rsi_series(closes, RSI_PERIOD)),
        }

        pos = None
        if position_id is not None:
            pos = self.position(int(position_id))
            if pos["symbol"].upper() != data["symbol"]:
                raise DashboardError("المركز لا يخص هذا الرمز")
        else:
            rows = [p for p in self.positions() if p["symbol"].upper() == data["symbol"]]
            if rows:
                pos = self.position(rows[0]["id"])
        data["position"] = pos
        data["levels"] = self._levels(pos) if pos else []
        data["markers"] = self._markers(pos, times) if pos else []
        return data

    @staticmethod
    def _levels(pos: dict) -> List[dict]:
        out = []

        def lv(key, label, kind):
            v = pos.get(key)
            if v is not None:
                out.append({"key": key, "label": label, "price": float(v), "kind": kind})

        lv("entry_low", "Entry zone low", "zone")
        lv("entry_high", "Entry zone high", "zone")
        if pos.get("entry") is not None:
            lv("entry", "Entry (filled)", "entry")
        lv("tp1", "TP1", "tp")
        lv("tp2", "TP2", "tp")
        lv("tp3", "TP3", "tp")
        lv("invalidation", "Stop-Loss (invalidation)", "sl")
        if pos.get("stop") is not None and pos.get("invalidation") is not None \
                and float(pos["stop"]) != float(pos["invalidation"]):
            lv("stop", "Stop now (BE)", "sl_be")
        return out

    @staticmethod
    def _markers(pos: dict, times: List[int]) -> List[dict]:
        out = []
        for st in pos.get("timeline", []):
            if st["state"] != "done" or st["ts"] is None:
                continue
            if st["stage"] in ("APPROVED", "PENDING", "SL→BE"):
                continue
            t = _bar_time_for(times, int(st["ts"]))
            if t is None:
                continue
            out.append({"time": t, "stage": st["stage"], "price": st["price"],
                        "source": st["source"]})
        return out
