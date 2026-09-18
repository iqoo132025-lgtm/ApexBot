#!/usr/bin/env python3
"""
صياغة وإرسال إشارات Top 100 إلى Telegram — بنفس الشكل المتفق عليه،
مع أزرار [CHART] [ANALYSIS] [BUY] كـ inline keyboard.

الأزرار:
  CHART    → رابط TradingView للزوج (أو CoinGecko لغير المدرج)
  ANALYSIS → callback_data = "apex:analysis:<coin_id>"  (يرد عليه البوت بالتفصيل)
  BUY      → callback_data = "apex:buy:<symbol>:<pct>"  (يمر عبر Execution Adapter)
"""
import json
import urllib.parse
import urllib.request
from typing import List, Optional

TELEGRAM_API = "https://api.telegram.org"


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "—"
    a = abs(p)
    if a >= 1000:
        return f"${p:,.0f}"
    if a >= 10:
        return f"${p:,.2f}"
    if a >= 0.1:
        return f"${p:.3f}"
    if a >= 0.001:
        return f"${p:.5f}"
    return f"${p:.8f}".rstrip("0")


def fmt_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def format_top100_signal(sig) -> str:
    """نص الإشارة كما هو متفق عليه في APEX Ultimate V2."""
    d = sig.to_dict() if hasattr(sig, "to_dict") else dict(sig)
    lines = [
        "🔥 APEX TOP-100 SIGNAL",
        "",
        f"{d['symbol']} — Rank #{d['rank']}",
        f"Market Regime: {d['regime']}",
        f"APEX Score: {int(round(d['score']))}/100",
        "",
        f"Trend: {d['trend_emoji']}",
        f"vs BTC: {fmt_pct(d.get('vs_btc_pct'))}",
        f"Volume: {d['volume_state']}",
        f"Momentum: {d['momentum_label']}",
        f"Risk: {d['risk']}",
        "",
        f"Entry Zone: {fmt_price(d['entry_low'])} – {fmt_price(d['entry_high'])} ({d['entry_note']})",
        f"Invalidation: {fmt_price(d['invalidation'])}",
        f"TP1 / TP2 / TP3: {fmt_price(d['tp1'])} / {fmt_price(d['tp2'])} / {fmt_price(d['tp3'])}",
        f"Position Size: {d['position_pct']:g}%",
    ]
    if d.get("flags"):
        lines += ["", "⚠️ " + " · ".join(d["flags"][:3])]
    return "\n".join(lines)


def signal_buttons(sig, chart_base: str = "https://www.tradingview.com/chart/?symbol=BINANCE:") -> dict:
    d = sig.to_dict() if hasattr(sig, "to_dict") else dict(sig)
    sym = d["symbol"]
    chart_url = f"{chart_base}{sym}USDT"
    return {"inline_keyboard": [[
        {"text": "CHART", "url": chart_url},
        {"text": "ANALYSIS", "callback_data": f"apex:analysis:{d['coin_id']}"},
        {"text": "BUY", "callback_data": f"apex:buy:{sym}:{d['position_pct']:g}"},
    ]]}


def format_list_event(event, regime: Optional[str] = None) -> str:
    """إشعار دخول/خروج عملة من Top 100."""
    if event.event == "ENTER":
        head = "🆕 دخلت Top 100"
        body = f"{event.symbol} — Rank #{event.rank}"
        tail = "بدأ APEX تتبّعها من الآن."
    else:
        head = "📤 خرجت من Top 100"
        body = f"{event.symbol} — كانت Rank #{event.prev_rank}"
        tail = "نحتفظ بتاريخها للدراسة."
    lines = [head, "", body]
    if regime:
        lines.append(f"Market Regime: {regime}")
    lines += ["", tail]
    return "\n".join(lines)


def format_regime_change(result) -> str:
    m = result.metrics
    return "\n".join([
        "📊 APEX MARKET REGIME",
        "",
        f"{result.changed_from} → {result.regime}",
        f"Regime Score: {result.score:.0f}/100",
        "",
        f"BTC vs SMA200: {fmt_pct(m.get('btc_vs_sma200'))}",
        f"Breadth >SMA50: {m.get('breadth_50') and round(m['breadth_50'])}%",
        f"Breadth >SMA200: {m.get('breadth_200') and round(m['breadth_200'])}%",
        "",
        "التصنيف يرفع أوزان العملات القوية فقط — ولا يُعد سبب شراء.",
    ])


class TelegramSender:
    def __init__(self, token: Optional[str], chat_id: Optional[str], timeout: int = 15):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
        if not self.enabled:
            return None
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"{TELEGRAM_API}/bot{self.token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def send_signal(self, sig) -> Optional[dict]:
        return self.send(format_top100_signal(sig), signal_buttons(sig))
