#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         APEX ULTIMATE — بوت التداول النهائي          ║
║     بسيط كـ APEX SCALPER + قوي كـ APEX PRO          ║
║   افتح المتصفح على: http://localhost:8080            ║
╚══════════════════════════════════════════════════════╝
"""
import hashlib, hmac, time, json, math, threading, sys, queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer

# ══════════════════════════════════════════
#  CONFIG — عدّل هنا فقط
# ══════════════════════════════════════════
CONFIG = {
    "API_KEY":    os.getenv("BINANCE_API_KEY", ""),
    "SECRET_KEY": os.getenv("BINANCE_API_SECRET", ""),
    "TESTNET":    False,
    "SYMBOLS":    ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    "PINNED_SYMBOLS": ["LABUSDT"],   # عملات مثبتة دائماً — تُضاف فوق الـ Top N
    "BANNED_SYMBOLS": ["FIDAUSDT", "RIFUSDT", "ONDOUSDT", "ZECUSDT"],  # محظورة دائماً — خسائر متكررة
    "TOP_N":               30,
    # ── إدارة رأس المال الاحترافية ──
    "RISK_PER_TRADE_PCT":  1.0,   # خاطر بـ 1% من رأس المال لكل صفقة
    "MAX_OPEN_POSITIONS":  2,     # Scalping: 2 صفقات فقط — تركيز وجودة
    "MAX_TRADES_PER_HOUR": 4,     # حد أقصى 4 صفقات جديدة في الساعة
    # ── SL/TP ديناميكي بـ ATR ──
    "ATR_SL_MULT":         2.0,   # SL واسع — يتجنب الإيقاف بالضوضاء
    "ATR_TP_MULT":         6.0,   # TP واسع — يلتقط كامل حركة الاختراق
    "BREAKEVEN_TRIGGER":   0.40,  # انقل SL للبريك إيفن بعد 40% من TP
    "TRAILING_ATR_MULT":   1.5,   # تريلينج للحفاظ على الربح
    # ── فلاتر الجودة ──
    "MIN_SIGNAL_SCORE":    7,     # ICT: يحتاج على الأقل إشارتين من الثلاثة
    "MIN_ATR_PCT":         0.5,   # نطلب تحركاً كافياً (ATR ≥ 0.5% من السعر)
    "ADX_MIN":             20,    # ADX أقوى للتأكد من الترند
    # ── حماية الرصيد ──
    "DAILY_LOSS_LIMIT_PCT": 3.0,  # أوقف عند خسارة 3% يومياً
    "MAX_HOLD_MINUTES":    180,   # Scalping: أقصى 3 ساعات لكل صفقة
    "SL_BLACKLIST_MIN":    60,    # Scalping: حظر ساعة واحدة بعد SL
    # ── تقني ──
    "KLINE_INTERVAL":      "15m", # Scalping: إشارات على فريم 15 دقيقة
    "KLINE_LIMIT":         100,
    "TREND_INTERVAL":      "1h",  # Scalping: ترند على الساعة بدل الأسبوعي
    "LOOP_INTERVAL":       60,    # Scalping: فحص كل دقيقة
    "COOLDOWN":            1800,  # Scalping: 30 دقيقة cooldown بين صفقات نفس العملة
    "WITHDRAW_THRESHOLD_PCT": 15,
    # ── مسار Top 100 (محرك الاستثمار والسوينغ — إشارات فقط بلا تنفيذ تلقائي) ──
    "TOP100_ENABLED":      True,
    "TOP100_UNIVERSE":     100,   # أكبر N عملة حسب Market Cap
    "TOP100_MIN_SCORE":    70,    # أقل APEX Score لإطلاق إشارة
    "TOP100_SCAN_MIN":     15,    # دورة مسح كل كم دقيقة
}

BASE_URL = "https://testnet.binance.vision" if CONFIG["TESTNET"] else "https://api.binance.com"

# ══════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════
state = {
    "running": False,
    "balance": 0.0,
    "total_pnl": 0.0,
    "daily_pnl": 0.0,
    "daily_start_balance": 0.0,
    "withdrawn": 0.0,
    "wins": 0, "losses": 0,
    "open_positions": {},
    "trades": [],
    "logs": [],
    "market": {},
    "protected": False,   # True = توقف بسبب حد الخسارة اليومية
    "top100": {"regime": None, "signals": [], "events": []},   # مسار Top 100
}
sse_clients = []
sse_lock = threading.Lock()
_time_offset = 0
last_trade_time = {}
sl_blacklist = {}        # {sym: timestamp} — عملات محظورة مؤقتاً بعد SL
daily_reset_day = -1     # رقم اليوم لإعادة تعيين daily_pnl
trade_timestamps = []    # قائمة timestamps الصفقات لتطبيق حد الساعة
consecutive_losses = 0  # عداد الخسائر المتتالية
last_loss_time     = 0  # وقت آخر خسارة

import os as _os
STATE_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "apex_ultimate_state.json")

def save_state():
    try:
        data = {
            "total_pnl":          state["total_pnl"],
            "daily_pnl":          state["daily_pnl"],
            "daily_start_balance":state["daily_start_balance"],
            "withdrawn":          state["withdrawn"],
            "wins":               state["wins"],
            "losses":             state["losses"],
            "trades":             state["trades"],
            "open_positions":     state["open_positions"],
            "protected":          state["protected"],
            "daily_reset_day":    daily_reset_day,
            "market":             state["market"],
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        pass

def load_state():
    global daily_reset_day
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state["total_pnl"]           = data.get("total_pnl", 0.0)
        state["daily_pnl"]           = data.get("daily_pnl", 0.0)
        state["daily_start_balance"] = data.get("daily_start_balance", 0.0)
        state["withdrawn"]           = data.get("withdrawn", 0.0)
        state["wins"]                = data.get("wins", 0)
        state["losses"]              = data.get("losses", 0)
        state["trades"]              = data.get("trades", [])
        loaded_pos = data.get("open_positions", {})
        for p in loaded_pos.values():
            p.setdefault("be_done", False)
            p.setdefault("partial_done", False)
            p.setdefault("trail_sl", None)
            p.setdefault("max_hold", CONFIG["MAX_HOLD_MINUTES"])  # صفقات قديمة تأخذ الإعداد الحالي
        state["open_positions"]      = loaded_pos
        state["protected"]           = data.get("protected", False)
        daily_reset_day              = data.get("daily_reset_day", -1)
        state["market"]              = data.get("market", {})
    except FileNotFoundError:
        pass
    except Exception as e:
        pass

def sync_spot_positions():
    """مزامنة المحفظة: أي عملة بقيمة >$5 تُضاف كصفقة مفتوحة إذا لم تكن مسجلة"""
    try:
        account = api("GET", "/api/v3/account", signed=True)
        balances = account.get("balances", [])
        tickers = {t["symbol"]: float(t["price"])
                   for t in api("GET", "/api/v3/ticker/price")
                   if t["symbol"].endswith("USDT")}
        added = 0
        for b in balances:
            asset = b["asset"]
            sym = asset + "USDT"
            free = float(b.get("free", 0))
            locked = float(b.get("locked", 0))
            qty = free + locked
            if qty <= 0: continue
            if sym not in tickers: continue
            price = tickers[sym]
            value = qty * price
            if value < 5: continue                       # أقل من $5 تجاهل
            if sym in state["open_positions"]: continue  # مسجلة مسبقاً

            # ATR يومي — المزامنة تحتاج SL واسعاً مناسباً للـ Swing
            try:
                k = get_klines(sym, "1d", 20)
                highs  = [float(x[2]) for x in k]
                lows   = [float(x[3]) for x in k]
                closes = [float(x[4]) for x in k]
                trs = [max(highs[i]-lows[i],
                           abs(highs[i]-closes[i-1]),
                           abs(lows[i]-closes[i-1])) for i in range(1, len(k))]
                a = sum(trs[-14:]) / 14
            except:
                a = price * 0.02  # افتراضي: 2% من السعر

            atr_pct = a / price * 100
            # تخطي المزامنة إذا كان ATR أكبر من 5% — خطر مفرط
            if atr_pct > 5.0:
                log(f"⛔ تخطي مزامنة {sym} — ATR {atr_pct:.1f}% كبير جداً","warn")
                continue

            pr = max(0, round(-math.log10(max(a/1000, 1e-9))))
            sl = round(price - a * CONFIG["ATR_SL_MULT"], pr)
            tp = round(price + a * CONFIG["ATR_TP_MULT"], pr)

            state["open_positions"][sym] = {
                "side": "BUY", "entry": price, "qty": qty,
                "sl": sl, "tp": tp,
                "be_done": False, "partial_done": False, "trail_sl": None,
                "atr": a, "atr_pct": round(a/price*100, 3),
                "order_id": 0,
                "open_time": time.time(),
                "synced": True,   # علامة: هذه مزامنة وليست صفقة جديدة
            }
            log(f"🔄 مزامنة {sym} | qty:{qty:.4f} | ${value:.2f} | SL:{sl} TP:{tp}", "warn")
            added += 1

        if added:
            save_state()
            log(f"✅ تمت مزامنة {added} صفقة من المحفظة", "success")
        else:
            log("✅ لا توجد صفقات غير مسجلة في المحفظة", "info")
    except Exception as e:
        log(f"فشل المزامنة: {e}", "error")

def push(data):
    msg = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try: q.put_nowait(msg)
            except: dead.append(q)
        for q in dead: sse_clients.remove(q)

def log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"info":"🔵","success":"🟢","warn":"🟡","error":"🔴","trade":"💜","signal":"🔍"}
    print(f"[{ts}] {icons.get(level,'▸')} {msg}")
    sys.stdout.flush()
    entry = {"time": ts, "msg": msg, "level": level}
    state["logs"].insert(0, entry)
    if len(state["logs"]) > 300: state["logs"].pop()
    push({"type": "log", **entry})

# ══════════════════════════════════════════
#  BINANCE API
# ══════════════════════════════════════════
def _sign(q): return hmac.new(CONFIG["SECRET_KEY"].encode(), q.encode(), hashlib.sha256).hexdigest()

def _sync_time():
    global _time_offset
    try:
        local = int(time.time()*1000)
        with urlopen(Request(f"{BASE_URL}/api/v3/time"), timeout=5) as r:
            srv = json.loads(r.read())["serverTime"]
        _time_offset = srv - local
        log(f"مزامنة التوقيت: {_time_offset:+d}ms", "info")
    except Exception as e:
        log(f"تحذير مزامنة: {e}", "warn")
        _time_offset = -1000

def _now(): return int(time.time()*1000) + _time_offset - 100

def api(method, path, params=None, signed=False):
    params = params or {}
    if signed:
        params["timestamp"] = _now()
        q = urlencode(params)
        params["signature"] = _sign(q)
    query = urlencode(params)
    url = f"{BASE_URL}{path}{'?'+query if query else ''}"
    headers = {"X-MBX-APIKEY": CONFIG["API_KEY"]} if CONFIG["API_KEY"] else {}
    req = Request(url, method=method, headers=headers)
    try:
        with urlopen(req, timeout=10) as r: return json.loads(r.read())
    except HTTPError as e:
        body = json.loads(e.read())
        raise Exception(f"Binance {e.code}: {body.get('msg', str(body))}")

def get_balance(asset="USDT"):
    acc = api("GET", "/api/v3/account", signed=True)
    for b in acc.get("balances", []):
        if b["asset"] == asset: return float(b["free"])
    return 0.0

def get_klines(sym, interval, limit):
    return api("GET", "/api/v3/klines", {"symbol":sym,"interval":interval,"limit":limit})

def place_order(sym, side, otype, **kw):
    return api("POST", "/api/v3/order", {"symbol":sym,"side":side,"type":otype,**kw}, signed=True)

def cancel_orders(sym):
    try: api("DELETE", "/api/v3/openOrders", {"symbol":sym}, signed=True)
    except: pass

def get_filters(sym):
    info = api("GET", "/api/v3/exchangeInfo", {"symbol":sym})
    s = info["symbols"][0]
    f = {x["filterType"]:x for x in s["filters"]}
    return {
        "step": float(f.get("LOT_SIZE",{}).get("stepSize",0.001)),
        "minQ": float(f.get("LOT_SIZE",{}).get("minQty",0.001)),
        "minN": float(f.get("MIN_NOTIONAL",{}).get("minNotional",10)),
        "tick": float(f.get("PRICE_FILTER",{}).get("tickSize",0.01)),
    }

def fetch_top_symbols(n=30):
    """
    يختار العملات الأكثر ترنداً بـ trend_score = |تغير24h| × √حجم_USDT
    - سيولة > 5M USDT ، عدد صفقات > 2000 ، سعر > 0.0001
    - يُخزِّن بيانات التيكر لإعادة الاستخدام في get_btc_dominance_trend
    """
    global _ticker_cache
    try:
        tickers = api("GET", "/api/v3/ticker/24hr")
        _ticker_cache = tickers   # حفظ للاستخدام لاحقاً

        EXCLUDE = ["UP","DOWN","BULL","BEAR","TUSD","BUSD","USDC","DAI","FDUSD",
                   "USDE","USD1","RLSD","RLUS","EUR","GBP","AUD","TRY","BRL",
                   "RUB","XUSD","SUSD","XPLUS","TLMUS","FFUS","RLUSD","WBTC",
                   "RLUSDT","USDT1","BFUSD","AEUR","BGBP","BIDR","BVND",
                   "PAXG","XAUT","EURT","JEUR","AGIX","ACH","UUSDT","EDEN"]

        usdt = [t for t in tickers
                if t["symbol"].endswith("USDT")
                and not any(x in t["symbol"] for x in EXCLUDE)
                and not (0.95 < float(t.get("lastPrice", 0)) < 1.05)
                and float(t.get("quoteVolume", 0)) > 5_000_000
                and float(t.get("count", 0)) > 2_000
                and float(t.get("lastPrice", 0)) > 0.0001
        ]

        # ترتيب بـ trend_score الحقيقي: حركة السعر × جذر الحجم
        usdt.sort(
            key=lambda t: abs(float(t.get("priceChangePercent", 0))) * (float(t.get("quoteVolume", 0)) ** 0.5),
            reverse=True
        )
        usdt = usdt[:n]
        symbols = [t["symbol"] for t in usdt]

        top = max(usdt, key=lambda t: abs(float(t.get("priceChangePercent", 0))), default=None)
        top_info = f"{top['symbol']} {float(top.get('priceChangePercent',0)):+.1f}%" if top else "-"

        # إزالة العملات المحظورة نهائياً
        banned = set(CONFIG.get("BANNED_SYMBOLS", []))
        symbols = [s for s in symbols if s not in banned]

        # دمج العملات المثبتة — تُضاف دائماً بغض النظر عن الترتيب
        pinned = [s for s in CONFIG.get("PINNED_SYMBOLS", []) if s not in symbols and s not in banned]
        if pinned:
            symbols = pinned + symbols
            log(f"📌 عملات مثبتة: {', '.join(pinned)}", "info")

        log(f"📡 {len(symbols)} عملة ترند | أعلى حركة: {top_info}", "success")
        return symbols

    except Exception as e:
        log(f"فشل جلب العملات: {e}", "warn")
        return CONFIG["SYMBOLS"]

def round_step(qty, step):
    p = max(0, round(-math.log10(step)))
    return round(math.floor(qty/step)*step, p)

# ══════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════
def rsi(closes, n=14):
    if len(closes) < n + 2: return 50
    # Wilder's Smoothing — متطابق مع TradingView
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    # متوسط أولي (SMA للـ n شمعة الأولى)
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    # تمهيد Wilder على بقية البيانات
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0: return 100
    return 100 - (100 / (1 + ag / al))

def ema(closes, n):
    if len(closes)<n: return closes[-1]
    k=2/(n+1); e=sum(closes[:n])/n
    for c in closes[n:]: e=c*k+e*(1-k)
    return e

def macd(closes):
    # MACD صحيح: EMA(12)-EMA(26) ثم EMA(9) للـ signal line
    if len(closes)<35: return 0,0,0,0
    macd_vals=[]
    for i in range(26, len(closes)+1):
        macd_vals.append(ema(closes[:i],12)-ema(closes[:i],26))
    if len(macd_vals)<9: return 0,0,0,0
    signal=ema(macd_vals,9)
    hist=macd_vals[-1]-signal
    # نقيس زخم: هل الهيستوغرام يتصاعد؟
    momentum = hist - (macd_vals[-2] - ema(macd_vals[:-1],9)) if len(macd_vals)>9 else 0
    return macd_vals[-1], signal, hist, momentum

def detect_divergence(closes, highs, lows, lookback=30):
    """
    كشف الدايفرجنس بين السعر ومؤشر RSI/MACD
    Returns: (rsi_div, macd_div)
      rsi_div  = +1 صاعد، -1 هابط،  0 لا يوجد
      macd_div = +1 صاعد، -1 هابط،  0 لا يوجد
    """
    if len(closes) < lookback + 14:
        return 0, 0

    window = closes[-lookback:]
    h_window = highs[-lookback:]
    l_window = lows[-lookback:]

    # حساب RSI لكل شمعة في النافذة — نستخدم كامل التاريخ حتى تلك الشمعة
    rsi_vals = []
    base_idx = len(closes) - lookback
    for i in range(len(window)):
        sub = closes[:base_idx + i + 1]
        rsi_vals.append(rsi(sub))

    # حساب هيستوغرام MACD لكل شمعة
    macd_hist = []
    for i in range(len(window)):
        sub = closes[:len(closes) - lookback + i + 1]
        if len(sub) >= 35:
            _, _, h, _ = macd(sub)
            macd_hist.append(h)
        else:
            macd_hist.append(None)

    # البحث عن قيعان محلية في آخر lookback شمعة (للدايفرجنس الصاعد)
    def find_troughs(values, n=5):
        troughs = []
        for i in range(n, len(values) - n):
            if all(values[i] <= values[i-j] for j in range(1, n+1)) and \
               all(values[i] <= values[i+j] for j in range(1, n+1)):
                troughs.append(i)
        return troughs

    def find_peaks(values, n=5):
        peaks = []
        for i in range(n, len(values) - n):
            if all(values[i] >= values[i-j] for j in range(1, n+1)) and \
               all(values[i] >= values[i+j] for j in range(1, n+1)):
                peaks.append(i)
        return peaks

    price_troughs = find_troughs(list(l_window))
    price_peaks   = find_peaks(list(h_window))

    rsi_div = 0
    # دايفرجنس RSI صاعد: السعر يصنع قاعاً أخفض بينما RSI يصنع قاعاً أعلى
    if len(price_troughs) >= 2:
        i1, i2 = price_troughs[-2], price_troughs[-1]
        if l_window[i2] < l_window[i1] and rsi_vals[i2] > rsi_vals[i1]:
            rsi_div = 1
        elif l_window[i2] > l_window[i1] and rsi_vals[i2] < rsi_vals[i1]:
            rsi_div = -1

    # دايفرجنس RSI هابط: السعر يصنع قمة أعلى بينما RSI يصنع قمة أخفض
    if len(price_peaks) >= 2:
        i1, i2 = price_peaks[-2], price_peaks[-1]
        if h_window[i2] > h_window[i1] and rsi_vals[i2] < rsi_vals[i1]:
            rsi_div = -1
        elif h_window[i2] < h_window[i1] and rsi_vals[i2] > rsi_vals[i1] and rsi_div == 0:
            rsi_div = 1

    macd_div = 0
    valid_hist = [(i, v) for i, v in enumerate(macd_hist) if v is not None]
    if len(valid_hist) >= 2 and len(price_troughs) >= 2:
        i1, i2 = price_troughs[-2], price_troughs[-1]
        h1 = macd_hist[i1]; h2 = macd_hist[i2]
        if h1 is not None and h2 is not None:
            if l_window[i2] < l_window[i1] and h2 > h1:
                macd_div = 1
            elif l_window[i2] > l_window[i1] and h2 < h1:
                macd_div = -1

    if len(valid_hist) >= 2 and len(price_peaks) >= 2:
        i1, i2 = price_peaks[-2], price_peaks[-1]
        h1 = macd_hist[i1]; h2 = macd_hist[i2]
        if h1 is not None and h2 is not None:
            if h_window[i2] > h_window[i1] and h2 < h1:
                macd_div = -1
            elif h_window[i2] < h_window[i1] and h2 > h1 and macd_div == 0:
                macd_div = 1

    return rsi_div, macd_div


def atr(highs, lows, closes, n=14):
    if len(closes)<n+2: return 0
    trs=[]
    for i in range(1,len(closes)):
        tr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs[-n:])/n

def bollinger(closes, n=20):
    if len(closes)<n: return 50
    sl=closes[-n:]; mid=sum(sl)/n
    std=math.sqrt(sum((v-mid)**2 for v in sl)/n)
    up=mid+2*std; lo=mid-2*std
    return ((closes[-1]-lo)/(up-lo)*100) if up!=lo else 50

def adx(highs, lows, closes, n=14):
    # ADX: قوة الترند — أعلى من 20 = سوق يتجه، أقل = سوق عرضي
    if len(closes) < n+2: return 0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        tr_list.append(tr)
    def smooth(lst, n):
        s = sum(lst[:n])
        out = [s]
        for v in lst[n:]: s = s - s/n + v; out.append(s)
        return out
    atr14 = smooth(tr_list, n); pdm14 = smooth(plus_dm, n); mdm14 = smooth(minus_dm, n)
    di_list = []
    for i in range(len(atr14)):
        if atr14[i] == 0: di_list.append(0); continue
        pdi = 100*pdm14[i]/atr14[i]; mdi = 100*mdm14[i]/atr14[i]
        dx = 100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi) > 0 else 0
        di_list.append(dx)
    return sum(di_list[-n:])/n if len(di_list) >= n else 0

def vwap(highs, lows, closes, volumes):
    # VWAP: متوسط السعر المرجح بالحجم — مرجع دخول المحترفين
    if not volumes or sum(volumes) == 0: return closes[-1]
    typ = [(highs[i]+lows[i]+closes[i])/3 for i in range(len(closes))]
    return sum(t*v for t,v in zip(typ,volumes)) / sum(volumes)

def get_btc_regime():
    """
    فلتر BTC الرئيسي — أهم قرار في التداول:
    90% من الألتكوين تتبع BTC. إذا BTC هابط → لا شراء على الإطلاق.
    يستخدم 3 تأكيدات: EMA50 اليومي + RSI + هيكل الشموع (HH/HL)
    """
    try:
        k = get_klines("BTCUSDT", "1d", 60)
        C = [float(x[4]) for x in k]
        H = [float(x[2]) for x in k]
        L = [float(x[3]) for x in k]
        price = C[-1]
        e50 = ema(C, 50); e20 = ema(C, 20)
        r = rsi(C, 14)
        # هيكل HH/HL: أعلى قمتين وقاعين في آخر 20 شمعة
        highs20 = H[-20:]; lows20 = L[-20:]
        hh = highs20[-1] > highs20[-10]   # قمة أعلى
        hl = lows20[-1]  > lows20[-10]    # قاع أعلى
        bull_structure = hh and hl
        # شروط الصعود: فوق EMA50 + RSI > 45 + هيكل صاعد
        if price > e50 and r > 45 and bull_structure:
            return "BULL"
        # شروط الهبوط: تحت EMA50 + RSI < 55
        if price < e50 and r < 55:
            return "BEAR"
        return "RANGING"
    except:
        return "RANGING"

# ══════════════════════════════════════════
#  ICT INDICATORS — iFVG + SMT + LIQUIDITY
# ══════════════════════════════════════════
def detect_ifvg(H, L, C, lookback=60):
    """
    Inverse Fair Value Gap — FVG تم ملؤه ويتحول لدعم/مقاومة
    Bullish iFVG: FVG هابط مُلئ → يصبح دعماً
    Bearish iFVG: FVG صاعد مُلئ → يصبح مقاومةً
    """
    n = len(C)
    score = 0; sigs = {}
    if n < 10: return score, sigs

    lb = min(n - 1, lookback)
    fvg_zones = []  # (top, bot, ftype, bar)

    for i in range(2, lb):
        idx = n - 1 - i
        if idx < 2: continue
        # Bullish FVG: فجوة صاعدة (قمة شمعة idx-2 < قاع شمعة idx)
        if H[idx - 2] < L[idx]:
            fvg_zones.append((L[idx], H[idx - 2], "bull", idx))
        # Bearish FVG: فجوة هابطة (قاع شمعة idx-2 > قمة شمعة idx)
        if L[idx - 2] > H[idx]:
            fvg_zones.append((L[idx - 2], H[idx], "bear", idx))

    price = C[-1]
    ifvg_bull = []  # مناطق دعم iFVG
    ifvg_bear = []  # مناطق مقاومة iFVG

    for top, bot, ftype, bar_idx in fvg_zones:
        mid = (top + bot) / 2
        # هل دخل السعر منطقة FVG في شمعة لاحقة؟
        for j in range(bar_idx + 1, n - 1):
            if L[j] <= top and H[j] >= bot:
                # FVG مُلئ → يتحول لـ iFVG
                if ftype == "bull":
                    ifvg_bear.append(mid)  # كان FVG صاعد → مُلئ → مقاومة iFVG
                else:
                    ifvg_bull.append(mid)  # كان FVG هابط → مُلئ → دعم iFVG
                break

    # هل السعر الحالي عند iFVG دعم؟ (في نطاق 1%)
    if ifvg_bull:
        nearest = min(ifvg_bull, key=lambda x: abs(x - price))
        dist = abs(price - nearest) / price * 100
        if dist < 1.0:
            score += 4
            sigs["IFVG"] = f"🟢🟢 iFVG دعم عند {nearest:.4f} ({dist:.2f}% بعيد) +4"
        elif dist < 2.5:
            score += 2
            sigs["IFVG"] = f"🟢 iFVG دعم قريب {nearest:.4f} ({dist:.2f}%) +2"
        else:
            sigs["IFVG"] = f"iFVG دعم بعيد {nearest:.4f} ({dist:.2f}%)"
    elif ifvg_bear:
        nearest = min(ifvg_bear, key=lambda x: abs(x - price))
        dist = abs(price - nearest) / price * 100
        if dist < 1.0:
            score -= 3
            sigs["IFVG"] = f"🔴 iFVG مقاومة عند {nearest:.4f} ({dist:.2f}%) -3"
        else:
            sigs["IFVG"] = f"iFVG مقاومة بعيدة {nearest:.4f}"
    else:
        sigs["IFVG"] = "لا iFVG قريب"

    return score, sigs


_btc_klines_cache = {"H": [], "L": [], "C": [], "ts": 0}

def get_btc_klines_cached(interval="15m", limit=60):
    """كاش BTC klines لتجنب 30 طلب API متزامن"""
    now = time.time()
    if now - _btc_klines_cache["ts"] < 300 and _btc_klines_cache["C"]:  # كاش 5 دقائق
        return _btc_klines_cache["H"], _btc_klines_cache["L"], _btc_klines_cache["C"]
    try:
        k = get_klines("BTCUSDT", interval, limit)
        H = [float(x[2]) for x in k]
        L = [float(x[3]) for x in k]
        C = [float(x[4]) for x in k]
        _btc_klines_cache.update({"H": H, "L": L, "C": C, "ts": now})
        return H, L, C
    except:
        return _btc_klines_cache["H"], _btc_klines_cache["L"], _btc_klines_cache["C"]


def detect_smt(H, L, C, lookback=20):
    """
    SMT Divergence (Smart Money Technique)
    تباين بين العملة وBTC — يكشف التلاعب المؤسسي
    Bullish SMT: العملة تصنع قاعاً أدنى لكن BTC لا → الانخفاض مصطنع
    Bearish SMT: العملة تصنع قمةً أعلى لكن BTC لا → الارتفاع مصطنع
    """
    score = 0; sigs = {}
    btc_H, btc_L, btc_C = get_btc_klines_cached()
    if not btc_C or len(btc_C) < lookback or len(C) < lookback:
        sigs["SMT"] = "بيانات غير كافية"
        return score, sigs

    lb = min(lookback, len(C) - 1, len(btc_C) - 1)
    half = lb // 2

    # قارن أدنى قاع في النصف الأخير مقابل النصف السابق
    coin_low_recent = min(L[-half:])
    coin_low_prev   = min(L[-lb:-half])
    btc_low_recent  = min(btc_L[-half:])
    btc_low_prev    = min(btc_L[-lb:-half])

    coin_high_recent = max(H[-half:])
    coin_high_prev   = max(H[-lb:-half])
    btc_high_recent  = max(btc_H[-half:])
    btc_high_prev    = max(btc_H[-lb:-half])

    tol = 0.003  # 0.3% tolerance

    # Bullish SMT: عملة → قاع أدنى، BTC → قاع أعلى أو مساوٍ
    if (coin_low_recent < coin_low_prev * (1 - tol) and
            btc_low_recent >= btc_low_prev * (1 - tol)):
        score += 4
        sigs["SMT"] = f"🟢🟢 SMT صاعد: عملة أدنى قاع، BTC لم ينخفض +4"

    # Bearish SMT: عملة → قمة أعلى، BTC → قمة أدنى أو مساوية
    elif (coin_high_recent > coin_high_prev * (1 + tol) and
              btc_high_recent <= btc_high_prev * (1 + tol)):
        score -= 3
        sigs["SMT"] = f"🔴 SMT هابط: عملة أعلى قمة، BTC لم يرتفع -3"

    # SMT تأكيد عكسي: BTC يرتفع والعملة لا → العملة ستلحق
    elif (btc_low_recent < btc_low_prev * (1 - tol) and
              coin_low_recent >= coin_low_prev * (1 - tol)):
        score += 2
        sigs["SMT"] = f"🟢 SMT: BTC أدنى قاع، العملة ثابتة → قوة نسبية +2"
    else:
        sigs["SMT"] = "لا SMT"

    return score, sigs


def detect_liquidity(H, L, C, tolerance=0.0015, lookback=50):
    """
    Liquidity Pools — EQH/EQL وكسح الوقفات (Stop Hunt)
    أقوى إشارة: كسح EQL ثم الإغلاق فوقه = Stop Hunt اكتمل → شراء
    """
    score = 0; sigs = {}
    n = len(C)
    if n < 10: return score, sigs

    lb = min(n - 1, lookback)
    H_w = H[-lb:]; L_w = L[-lb:]
    price = C[-1]

    # Equal Highs — بحث عن قممين متساويتين (سيولة فوق)
    eqh = []
    for i in range(len(H_w) - 1):
        for j in range(i + 3, len(H_w)):  # فاصل 3 شمعات على الأقل
            if abs(H_w[i] - H_w[j]) / H_w[i] < tolerance:
                eqh.append((H_w[i] + H_w[j]) / 2)

    # Equal Lows — بحث عن قاعين متساويين (سيولة تحت)
    eql = []
    for i in range(len(L_w) - 1):
        for j in range(i + 3, len(L_w)):
            if abs(L_w[i] - L_w[j]) / L_w[i] < tolerance:
                eql.append((L_w[i] + L_w[j]) / 2)

    # كسح EQL (Stop Hunt صاعد): آخر شمعة كسرت EQL ثم أغلقت فوقه
    swept_bull = [lvl for lvl in eql if L[-1] < lvl and C[-1] > lvl]
    # كسح EQH (Stop Hunt هابط): آخر شمعة كسرت EQH ثم أغلقت تحته
    swept_bear = [lvl for lvl in eqh if H[-1] > lvl and C[-1] < lvl]

    if swept_bull:
        score += 5
        sigs["LIQ"] = f"🟢🟢🟢 Stop Hunt صاعد! كسح EQL {len(swept_bull)} مستوى +5"
    elif swept_bear:
        score -= 5
        sigs["LIQ"] = f"🔴🔴🔴 Stop Hunt هابط! كسح EQH {len(swept_bear)} مستوى -5"
    else:
        # السعر يقترب من EQL (سيولة تحت — قد يُكسح قريباً)
        if eql:
            nearest_eql = min(eql, key=lambda x: abs(x - price))
            dist = (price - nearest_eql) / price * 100
            if 0 < dist <= 0.3:
                score += 2
                sigs["LIQ"] = f"🟡 سعر قرب EQL {nearest_eql:.5f} ({dist:.2f}% فوقه) +2"
            elif 0 < dist <= 1.0:
                score += 1
                sigs["LIQ"] = f"⚪ EQL عند {nearest_eql:.5f} ({dist:.2f}% أسفل) +1"
            else:
                sigs["LIQ"] = f"EQL عند {nearest_eql:.5f} ({dist:.2f}% أسفل)"
        elif eqh:
            nearest_eqh = min(eqh, key=lambda x: abs(x - price))
            dist = (nearest_eqh - price) / price * 100
            if 0 < dist <= 0.3:
                score -= 2
                sigs["LIQ"] = f"🟡 سعر قرب EQH {nearest_eqh:.5f} (مقاومة وشيكة) -2"
            else:
                sigs["LIQ"] = f"EQH عند {nearest_eqh:.5f} ({dist:.2f}% فوق)"
        else:
            sigs["LIQ"] = "لا سيولة Equal"

    return score, sigs


_btc_regime_cache = {"regime": "RANGING", "ts": 0, "dominance": "NEUTRAL"}
_ticker_cache: list = []       # يُخزَّن من fetch_top_symbols ويُعاد استخدامه

def get_btc_dominance_trend():
    """
    تقدير اتجاه استحواذ BTC (BTC.D):
    - يعيد استخدام _ticker_cache من fetch_top_symbols — لا استدعاء API إضافي
    - إذا BTC أضعف من الألتكوين → استحواذ هابط → موسم ألتكوين 🟢
    - إذا BTC أقوى → استحواذ صاعد → مال يتدفق لـ BTC 🔴
    """
    try:
        alts = ["ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT"]
        src = _ticker_cache if _ticker_cache else api("GET", "/api/v3/ticker/24hr")
        tickers = {t["symbol"]: float(t["priceChangePercent"])
                   for t in src if t["symbol"] in alts + ["BTCUSDT"]}
        btc_chg  = tickers.get("BTCUSDT", 0)
        alt_chgs = [tickers[s] for s in alts if s in tickers]
        if not alt_chgs:
            return "NEUTRAL"
        avg_alt = sum(alt_chgs) / len(alt_chgs)
        diff = avg_alt - btc_chg
        if diff > 1.5:  return "FALLING"
        if diff < -1.5: return "RISING"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def get_btc_regime_cached():
    """يُحدّث كل 15 دقيقة — لا يُثقل API في كل دورة"""
    if time.time() - _btc_regime_cache["ts"] > 900:
        _btc_regime_cache["regime"]    = get_btc_regime()
        _btc_regime_cache["dominance"] = get_btc_dominance_trend()
        _btc_regime_cache["ts"]        = time.time()
        dom = _btc_regime_cache["dominance"]
        dom_icon = {"FALLING":"📉 BTC.D هابط (موسم ألتكوين)","RISING":"📈 BTC.D صاعد","NEUTRAL":"➡️ BTC.D محايد"}.get(dom, dom)
        log(f"📊 BTC: {_btc_regime_cache['regime']} | {dom_icon}", "info")
    return _btc_regime_cache["regime"], _btc_regime_cache["dominance"]

def get_daily_trend(sym):
    """ترند العملة على الأسبوعي (EMA20/50 + RSI)"""
    try:
        k = get_klines(sym, CONFIG["TREND_INTERVAL"], 60)
        C = [float(x[4]) for x in k]
        e20 = ema(C, 20); e50 = ema(C, 50)
        r   = rsi(C, 14)
        diff_pct = (e20 - e50) / e50 * 100
        if diff_pct > 1.5 and r > 50: return "UP"
        if diff_pct < -1.5 and r < 50: return "DOWN"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def weekly_levels(sym):
    """تحليل يومي: فيبوناتشي + دعوم/مقاومات على آخر 90 يوم"""
    try:
        k = get_klines(sym, "1d", 90)
        if len(k) < 20: return None
        C=[float(x[4]) for x in k]; H=[float(x[2]) for x in k]; L=[float(x[3]) for x in k]
        price = C[-1]

        # ── Pivot Points اليومية ──
        pivots_h = [H[i] for i in range(2, len(H)-2) if H[i] == max(H[i-2:i+3])]
        pivots_l = [L[i] for i in range(2, len(L)-2) if L[i] == min(L[i-2:i+3])]
        support_levels = sorted(set(round(p,6) for p in pivots_l if p < price), reverse=True)[:3]
        resistance_levels = sorted(set(round(p,6) for p in pivots_h if p > price))[:3]

        # ── Fibonacci من آخر موجة يومية (آخر 45 يوم) ──
        swing_high = max(H[-45:]); swing_low = min(L[-45:])
        swing = swing_high - swing_low
        up_move = C[-1] >= C[-20] if len(C) >= 20 else True  # هل الاتجاه صاعد بالنظر للـ20 يوم الأخيرة؟

        if swing > 0:
            if up_move:
                # ارتداد تصحيحي من القمة نحو القاع (نشتري في التصحيح)
                fib = {
                    "0.236": swing_high - swing*0.236,
                    "0.382": swing_high - swing*0.382,
                    "0.5":   swing_high - swing*0.5,
                    "0.618": swing_high - swing*0.618,
                    "0.786": swing_high - swing*0.786,
                }
            else:
                fib = {
                    "0.236": swing_low + swing*0.236,
                    "0.382": swing_low + swing*0.382,
                    "0.5":   swing_low + swing*0.5,
                    "0.618": swing_low + swing*0.618,
                    "0.786": swing_low + swing*0.786,
                }
            golden_low  = min(fib["0.618"], fib["0.786"])
            golden_high = max(fib["0.618"], fib["0.786"])
            in_golden_zone = golden_low <= price <= golden_high
        else:
            fib = {}; in_golden_zone = False; golden_low = golden_high = 0

        entry_zone = (round(golden_low,6), round(golden_high,6)) if swing > 0 else None
        tp_zone = resistance_levels[0] if resistance_levels else (swing_high)

        return {
            "support": support_levels, "resistance": resistance_levels,
            "fib": {k_: round(v_,6) for k_,v_ in fib.items()},
            "swing_high": round(swing_high,6), "swing_low": round(swing_low,6),
            "in_golden_zone": in_golden_zone,
            "entry_zone": entry_zone, "tp_zone": round(tp_zone,6) if tp_zone else None,
            "up_move": up_move,
        }
    except Exception as e:
        return None

# ══════════════════════════════════════════
#  CLASSIC TECHNICAL ANALYSIS
# ══════════════════════════════════════════
def classic_analysis(H, L, C, price, O=None):
    """دعم/مقاومة + هيكل السوق (HH/HL) + أنماط الشموع"""
    if O is None: O = C  # fallback للتوافق مع backtest
    score = 0; sigs = {}

    # ── دعم ومقاومة (أقرب مستويات في آخر 50 شمعة) ──
    pivots_h = [H[i] for i in range(2, len(H)-2) if H[i] == max(H[i-2:i+3])]
    pivots_l = [L[i] for i in range(2, len(L)-2) if L[i] == min(L[i-2:i+3])]
    near_sup = max([p for p in pivots_l if p < price], default=None)
    near_res = min([p for p in pivots_h if p > price], default=None)
    if near_sup and near_res:
        rr_dist = (near_res - price) / (price - near_sup) if price > near_sup else 0
        bounce_zone = (price - near_sup) / near_sup * 100
        if bounce_zone < 1.0:  # السعر قريب جداً من الدعم
            score += 2; sigs["S/R"] = f"ارتداد من دعم ${near_sup:.4f} 🟢🟢"
        elif rr_dist >= 2:
            score += 1; sigs["S/R"] = f"RR={rr_dist:.1f} دعم:{near_sup:.4f} 🟢"
        else:
            sigs["S/R"] = f"دعم:{near_sup:.4f} مقاومة:{near_res:.4f}"
    elif near_sup:
        sigs["S/R"] = f"دعم:{near_sup:.4f}"
    else:
        sigs["S/R"] = "لا مستويات"

    # ── هيكل السوق: Higher Highs / Higher Lows ──
    if len(pivots_h) >= 2 and len(pivots_l) >= 2:
        hh = pivots_h[-1] > pivots_h[-2]  # قمة أعلى
        hl = pivots_l[-1] > pivots_l[-2]  # قاع أعلى
        lh = pivots_h[-1] < pivots_h[-2]  # قمة أدنى
        ll = pivots_l[-1] < pivots_l[-2]  # قاع أدنى
        if hh and hl:
            score += 2; sigs["STRUCT"] = "HH+HL ترند صاعد 🟢🟢"
        elif lh and ll:
            score -= 2; sigs["STRUCT"] = "LH+LL ترند هابط 🔴🔴"
        elif hh:
            score += 1; sigs["STRUCT"] = "قمة أعلى 🟢"
        elif ll:
            score -= 1; sigs["STRUCT"] = "قاع أدنى 🔴"
        else:
            sigs["STRUCT"] = "هيكل محايد ⚪"

    # ── أنماط الشموع (آخر 3 شموع) ──
    if len(C) >= 3:
        o1,h1,l1,c1 = O[-3],H[-3],L[-3],C[-3]
        o2,h2,l2,c2 = O[-2],H[-2],L[-2],C[-2]
        o3,h3,l3,c3 = O[-1],H[-1],L[-1],C[-1]
        body2 = abs(c2 - o2); rng2 = h2 - l2
        body3 = abs(c3 - o3); rng3 = h3 - l3

        # Hammer / Shooting Star
        upper_wick2 = h2 - max(o2, c2)
        lower_wick2 = min(o2, c2) - l2
        if rng2 > 0 and body2 / rng2 < 0.3 and lower_wick2 > body2 * 2 and c2 > l2:
            score += 1; sigs["CANDLE"] = "مطرقة 🟢"
        elif rng2 > 0 and body2 / rng2 < 0.3 and upper_wick2 > body2 * 2 and lower_wick2 < body2:
            score -= 1; sigs["CANDLE"] = "نجمة هابطة 🔴"
        # Engulfing
        elif c3 > o3 and c2 < o2 and c3 > o2 and o3 < c2:
            score += 2; sigs["CANDLE"] = "ابتلاع صاعد 🟢🟢"
        elif c3 < o3 and c2 > o2 and c3 < o2 and o3 > c2:
            score -= 2; sigs["CANDLE"] = "ابتلاع هابط 🔴🔴"
        else:
            sigs["CANDLE"] = "شمعة عادية"

    return score, sigs

# ══════════════════════════════════════════
#  HARMONIC PATTERNS
# ══════════════════════════════════════════
def harmonic_analysis(H, L, C, price):
    """كشف الأنماط الهارمونيكية: Gartley, Bat, Butterfly, Crab"""
    score = 0; sigs = {}
    try:
        # إيجاد swing points
        sp_h = [(i, H[i]) for i in range(2, len(H)-2) if H[i] == max(H[i-2:i+3])]
        sp_l = [(i, L[i]) for i in range(2, len(L)-2) if L[i] == min(L[i-2:i+3])]
        if len(sp_h) < 2 or len(sp_l) < 2:
            sigs["HARMONIC"] = "بيانات غير كافية"; return score, sigs

        # نمط XABCD — نحاول إيجاد نقطة D الحالية
        # بسيط: نحسب Fibonacci retracement من آخر موجة
        last_high = sp_h[-1][1]; last_low = sp_l[-1][1]
        swing = last_high - last_low
        if swing <= 0:
            sigs["HARMONIC"] = "لا نمط"; return score, sigs

        fib_618 = last_low + swing * 0.618
        fib_786 = last_low + swing * 0.786
        fib_382 = last_low + swing * 0.382
        fib_127 = last_low + swing * 1.272
        fib_161 = last_low + swing * 1.618

        tol = swing * 0.03  # تسامح 3%
        ref = C[-1]  # آخر كندل مغلق — ليس السعر الحي

        if abs(ref - fib_786) < tol:
            score += 2; sigs["HARMONIC"] = "Gartley D=0.786 🟢🟢"
        elif abs(ref - (last_low + swing * 0.886)) < tol:
            score += 2; sigs["HARMONIC"] = "Bat D=0.886 🟢🟢"
        elif abs(ref - fib_127) < tol:
            score += 2; sigs["HARMONIC"] = "Butterfly D=1.272 🟢🟢"
        elif abs(ref - fib_161) < tol:
            score += 2; sigs["HARMONIC"] = "Crab D=1.618 🟢🟢"
        elif abs(ref - fib_618) < tol:
            score += 1; sigs["HARMONIC"] = "Golden Zone 0.618 🟢"
        elif abs(ref - fib_382) < tol:
            score += 1; sigs["HARMONIC"] = "Retracement 0.382 🟢"
        else:
            sigs["HARMONIC"] = f"لا نمط | Fib618:{fib_618:.4f}"
    except:
        sigs["HARMONIC"] = "خطأ"
    return score, sigs

# ══════════════════════════════════════════
#  SMART MONEY CONCEPTS — QuantScript Pure SMC
# ══════════════════════════════════════════
def _swing_points(H, L, lb=3):
    """Swing Highs / Lows بـ lookback=lb كندل على كل جانب (مغلق فقط)."""
    sh, sl = [], []
    for i in range(lb, len(H) - lb):
        if all(H[i] > H[i-j] and H[i] > H[i+j] for j in range(1, lb+1)):
            sh.append((i, H[i]))
        if all(L[i] < L[i-j] and L[i] < L[i+j] for j in range(1, lb+1)):
            sl.append((i, L[i]))
    return sh, sl

def _market_structure(sh, sl):
    """
    يُعيد trend من Swing Highs / Lows:
      'BULL'  = HH + HL  (Higher Highs + Higher Lows)
      'BEAR'  = LH + LL  (Lower Highs + Lower Lows)
      'RANGE' = مختلط
    """
    if len(sh) < 2 or len(sl) < 2:
        return "RANGE"
    hh = sh[-1][1] > sh[-2][1]   # آخر Swing High أعلى من السابق
    hl = sl[-1][1] > sl[-2][1]   # آخر Swing Low أعلى من السابق
    lh = sh[-1][1] < sh[-2][1]
    ll = sl[-1][1] < sl[-2][1]
    if hh and hl: return "BULL"
    if lh and ll: return "BEAR"
    return "RANGE"

def _find_bos_choch(sh, sl, C):
    """
    BOS  = كسر swing في اتجاه الهيكل (تأكيد الاتجاه)
    CHoCH = كسر swing عكس الهيكل (تغيير الاتجاه / فرصة انعكاس)
    يقيس على الكندل المغلق C[-1].
    """
    result = {"type": None, "level": None, "dir": None}
    if not sh or not sl:
        return result
    last_c = C[-1]
    last_sh_lvl = sh[-1][1]
    last_sl_lvl = sl[-1][1]
    ms = _market_structure(sh, sl)

    if last_c > last_sh_lvl:
        result["level"] = last_sh_lvl
        result["type"]  = "BOS" if ms == "BULL" else "CHoCH"
        result["dir"]   = "UP"
    elif last_c < last_sl_lvl:
        result["level"] = last_sl_lvl
        result["type"]  = "BOS" if ms == "BEAR" else "CHoCH"
        result["dir"]   = "DOWN"
    return result

def _order_blocks(H, L, C, O, sh, sl, lb=3):
    """
    Bullish OB  = آخر شمعة هابطة (C<O) قبل BOS صاعد — الجسد الأخير قبل الصعود.
    Bearish OB  = آخر شمعة صاعدة (C>O) قبل BOS هابط.
    يتحقق من عدم كسر/تجاوز OB (Mitigation).
    """
    n = len(C)
    bull_obs, bear_obs = [], []

    # Bullish OB: ابحث قبل كل Swing High
    for idx, _ in sh:
        # الشمعة الهابطة الأخيرة قبل idx
        for i in range(idx - 1, max(0, idx - 6), -1):
            if C[i] < O[i]:  # شمعة هابطة (bearish candle = last down before up)
                ob_top = max(O[i], C[i])
                ob_bot = min(O[i], C[i])
                # غير مكسور: السعر الحالي لم يغلق تحت قاع OB
                if C[-1] >= ob_bot:
                    bull_obs.append({"top": ob_top, "bot": ob_bot, "idx": i})
                break

    # Bearish OB: ابحث قبل كل Swing Low
    for idx, _ in sl:
        for i in range(idx - 1, max(0, idx - 6), -1):
            if C[i] > O[i]:  # شمعة صاعدة
                ob_top = max(O[i], C[i])
                ob_bot = min(O[i], C[i])
                if C[-1] <= ob_top:
                    bear_obs.append({"top": ob_top, "bot": ob_bot, "idx": i})
                break

    return bull_obs, bear_obs

def _fvg(H, L, C, lookback=10):
    """
    Fair Value Gaps على آخر `lookback` كندل مغلق.
    Bullish FVG: L[i] > H[i-2]  (فجوة بين شمعة i-2 وشمعة i)
    Bearish FVG: H[i] < L[i-2]
    يُعيد قوائم FVGs غير مكتملة الإغلاق (unfilled).
    """
    n = len(C)
    bull_fvg, bear_fvg = [], []
    for i in range(2, min(lookback + 2, n)):
        ri = n - 1 - i + 2   # من الأحدث للأقدم
        if ri < 2 or ri >= n: continue
        # Bullish FVG
        if L[ri] > H[ri - 2]:
            top = L[ri]; bot = H[ri - 2]; mid = (top + bot) / 2
            # unfilled: السعر لم يغلق داخل الفجوة بعد
            if C[-1] > bot:
                bull_fvg.append({"top": top, "bot": bot, "mid": mid})
        # Bearish FVG
        elif H[ri] < L[ri - 2]:
            top = L[ri - 2]; bot = H[ri]; mid = (top + bot) / 2
            if C[-1] < top:
                bear_fvg.append({"top": top, "bot": bot, "mid": mid})
    return bull_fvg, bear_fvg

def _equal_hl(H, L, lookback=20, tol=0.002):
    """Equal Highs / Equal Lows = مناطق سيولة (Stop Hunts)."""
    rh = H[-lookback:]; rl = L[-lookback:]
    mh = max(rh); ml = min(rl)
    eqh = sum(1 for h in rh if abs(h - mh) / mh < tol)
    eql = sum(1 for l in rl if abs(l - ml) / ml < tol)
    return eqh, eql, mh, ml

def _premium_discount(sh, sl, C):
    """
    Premium Zone  = فوق 50% من آخر Swing (مُكلف — بيع)
    Discount Zone = تحت 50% (مُخفَّض — شراء)
    Equilibrium   = عند 50%
    """
    if not sh or not sl:
        return "غير محدد", 0
    hi = sh[-1][1]; lo = sl[-1][1]
    ref = C[-1]
    if ref > hi * 0.995: pct = 100
    elif ref < lo * 1.005: pct = 0
    else: pct = (ref - lo) / (hi - lo) * 100 if hi != lo else 50
    if pct > 55: zone = "Premium 🔴"
    elif pct < 45: zone = "Discount 🟢"
    else: zone = "Equilibrium ⚪"
    return zone, round(pct, 1)

def _displacement(H, L, C, O, lookback=5):
    """
    Displacement = شمعة بجسد كبير تُغلق بعيداً عن مستوى الفتح (>60% ATR)
    تُشير إلى إدخال مؤسسي قوي.
    """
    n = len(C)
    atr_vals = [H[i] - L[i] for i in range(max(0, n-lookback-14), n)]
    avg_rng = sum(atr_vals) / len(atr_vals) if atr_vals else 0
    if avg_rng == 0: return None, None
    last_body = abs(C[-1] - O[-1])
    if last_body > avg_rng * 0.6:
        return ("UP" if C[-1] > O[-1] else "DOWN"), round(last_body / avg_rng, 2)
    return None, None

def _inducement(sh, sl, C, H, L):
    """
    Inducement = كسح سيولة صغير (Stop Hunt) قبل BOS حقيقي.
    يحدث عندما تكسر الأسعار أعلى/أدنى Swing مؤقتاً ثم ترتد.
    """
    if len(sh) < 2 or len(sl) < 2: return None
    last_c = C[-1]
    prev_sh = sh[-2][1]; last_sh = sh[-1][1]
    prev_sl = sl[-2][1]; last_sl = sl[-1][1]
    # Bullish Inducement: كسرت أدنى Swing Low ثم أغلقت فوقه
    if last_sl < prev_sl and last_c > last_sl:
        return "Inducement صاعد (Stop Hunt على الـ Lows) 🟢"
    # Bearish Inducement: كسرت أعلى Swing High ثم أغلقت تحته
    if last_sh > prev_sh and last_c < last_sh:
        return "Inducement هابط (Stop Hunt على الـ Highs) 🔴"
    return None

def smc_analysis(H, L, C, price, O=None):
    """
    QuantScript Pure SMC:
    Swing Structure → BOS/CHoCH → Order Blocks → FVG → EQH/EQL
    → Premium/Discount → Displacement → Inducement
    يقيس كل شيء على الكندل المغلق C[-1].
    """
    score = 0; sigs = {}
    if O is None: O = C
    try:
        n = len(C)
        if n < 20: return score, sigs

        LB = 3  # swing lookback
        sh, sl = _swing_points(H, L, lb=LB)

        # ── 1. Market Structure ──
        ms = _market_structure(sh, sl)
        sigs["MS"] = {"BULL": "هيكل صاعد HH+HL 🟢", "BEAR": "هيكل هابط LH+LL 🔴"}.get(ms, "هيكل متذبذب ⚪")

        # ── 2. BOS / CHoCH ──
        bos = _find_bos_choch(sh, sl, C)
        if bos["type"] == "BOS" and bos["dir"] == "UP":
            score += 2; sigs["BOS"] = f"BOS صاعد >{bos['level']:.4f} 🟢🟢"
        elif bos["type"] == "BOS" and bos["dir"] == "DOWN":
            score -= 2; sigs["BOS"] = f"BOS هابط <{bos['level']:.4f} 🔴🔴"
        elif bos["type"] == "CHoCH" and bos["dir"] == "UP":
            score += 3; sigs["BOS"] = f"CHoCH صاعد (انعكاس) >{bos['level']:.4f} 🟢🟢🟢"
        elif bos["type"] == "CHoCH" and bos["dir"] == "DOWN":
            score -= 3; sigs["BOS"] = f"CHoCH هابط (انعكاس) <{bos['level']:.4f} 🔴🔴🔴"
        else:
            sigs["BOS"] = "لا كسر هيكل ⚪"

        # ── 3. Order Blocks ──
        bull_obs, bear_obs = _order_blocks(H, L, C, O, sh, sl, lb=LB)
        ref_c = C[-1]
        ob_scored = False
        # أقرب Bullish OB للسعر الحالي
        if bull_obs:
            best = min(bull_obs, key=lambda x: abs((x["top"] + x["bot"]) / 2 - ref_c))
            if best["bot"] <= ref_c <= best["top"]:
                score += 2; sigs["OB"] = f"داخل Bullish OB [{best['bot']:.4f}–{best['top']:.4f}] 🟢🟢"; ob_scored = True
            elif abs(ref_c - best["bot"]) / ref_c < 0.01:
                score += 1; sigs["OB"] = f"اقتراب Bullish OB عند {best['bot']:.4f} 🟢"; ob_scored = True
            else:
                sigs["OB"] = f"Bullish OB بعيد [{best['bot']:.4f}–{best['top']:.4f}] ⚪"; ob_scored = True
        if not ob_scored and bear_obs:
            best = min(bear_obs, key=lambda x: abs((x["top"] + x["bot"]) / 2 - ref_c))
            if best["bot"] <= ref_c <= best["top"]:
                score -= 2; sigs["OB"] = f"داخل Bearish OB [{best['bot']:.4f}–{best['top']:.4f}] 🔴🔴"
            else:
                sigs["OB"] = f"Bearish OB بعيد {best['top']:.4f} ⚪"
        if not ob_scored:
            sigs.setdefault("OB", "لا OB ⚪")

        # ── 4. Fair Value Gaps ──
        bull_fvg, bear_fvg = _fvg(H, L, C, lookback=10)
        fvg_scored = False
        if bull_fvg:
            closest = min(bull_fvg, key=lambda x: abs(x["mid"] - ref_c))
            if abs(ref_c - closest["mid"]) / ref_c < 0.015:
                score += 1; sigs["FVG"] = f"FVG صاعد [{closest['bot']:.4f}–{closest['top']:.4f}] 🟢"; fvg_scored = True
            else:
                sigs["FVG"] = f"FVG صاعد بعيد {closest['mid']:.4f}"
                fvg_scored = True
        if not fvg_scored and bear_fvg:
            closest = min(bear_fvg, key=lambda x: abs(x["mid"] - ref_c))
            if abs(ref_c - closest["mid"]) / ref_c < 0.015:
                score -= 1; sigs["FVG"] = f"FVG هابط [{closest['bot']:.4f}–{closest['top']:.4f}] 🔴"
            else:
                sigs["FVG"] = f"FVG هابط بعيد {closest['mid']:.4f}"
        if not fvg_scored and not bear_fvg:
            sigs.setdefault("FVG", "لا FVG ⚪")

        # ── 5. Equal Highs / Equal Lows (Liquidity) ──
        eqh_cnt, eql_cnt, eqh_lvl, eql_lvl = _equal_hl(H, L, lookback=20)
        if eqh_cnt >= 2 and ref_c > eqh_lvl * 0.998:
            score += 1; sigs["LIQ"] = f"كسح EQH سيولة فوق {eqh_lvl:.4f} 🟢"
        elif eql_cnt >= 2 and ref_c < eql_lvl * 1.002:
            score -= 1; sigs["LIQ"] = f"كسح EQL سيولة تحت {eql_lvl:.4f} 🔴"
        elif eqh_cnt >= 2:
            sigs["LIQ"] = f"EQH سيولة عند {eqh_lvl:.4f} (لم تُكسح) ⚪"
        elif eql_cnt >= 2:
            sigs["LIQ"] = f"EQL سيولة عند {eql_lvl:.4f} (لم تُكسح) ⚪"
        else:
            sigs["LIQ"] = "لا سيولة Equal ⚪"

        # ── 6. Premium / Discount Zone ──
        zone, pct = _premium_discount(sh, sl, C)
        sigs["ZONE"] = f"{zone} ({pct}%)"
        if "Discount" in zone:
            score += 1
        elif "Premium" in zone:
            score -= 1

        # ── 7. Displacement ──
        disp_dir, disp_ratio = _displacement(H, L, C, O)
        if disp_dir == "UP":
            score += 1; sigs["DISP"] = f"Displacement صاعد x{disp_ratio} ATR 🟢"
        elif disp_dir == "DOWN":
            score -= 1; sigs["DISP"] = f"Displacement هابط x{disp_ratio} ATR 🔴"
        else:
            sigs["DISP"] = "لا Displacement ⚪"

        # ── 8. Inducement ──
        ind = _inducement(sh, sl, C, H, L)
        if ind:
            if "صاعد" in ind:
                score += 1; sigs["IND"] = ind
            else:
                score -= 1; sigs["IND"] = ind
        else:
            sigs["IND"] = "لا Inducement ⚪"

    except Exception as e:
        sigs["SMC"] = f"خطأ: {e}"
    return score, sigs

# ══════════════════════════════════════════
#  SMART MONEY VOLUME — CVD + Absorption + Climax
# ══════════════════════════════════════════
def smart_volume_analysis(H, L, C, O, V):
    """
    Smart Money Volume:
    1. Volume Delta per candle = V × (2×(C-L)/(H-L) - 1)
       → موجب = ضغط شراء، سالب = ضغط بيع
    2. CVD (Cumulative Volume Delta): تراكم الضغط على آخر 20 شمعة
    3. Divergence CVD/السعر: كشف الشراء/البيع الخفي للمؤسسات
    4. Absorption: حجم × 2 + جسد صغير = امتصاص مؤسسي
    5. Volume Climax: حجم × 3 = ذروة اهتمام مؤسسي
    """
    score = 0; sigs = {}
    try:
        n = len(C)
        if n < 20: return score, sigs

        # ── 1. Volume Delta لكل شمعة ──
        deltas = []
        for i in range(n):
            rng = H[i] - L[i]
            d = V[i] * (2 * (C[i] - L[i]) / rng - 1) if rng > 0 else 0
            deltas.append(d)

        # ── 2. CVD تراكمي على النافذة الكاملة ──
        cvd = []
        cum = 0
        for d in deltas:
            cum += d
            cvd.append(cum)

        # ── 3. CVD vs السعر (آخر 10 شموع) ──
        lookback = min(10, n - 1)
        price_chg = C[-1] - C[-lookback]
        cvd_chg   = cvd[-1] - cvd[-lookback]
        avg_v     = sum(V[-20:]) / 20 if sum(V[-20:]) > 0 else 1

        if price_chg > 0 and cvd_chg > 0:
            score += 1; sigs["CVD"] = f"CVD↑ مع السعر (تدفق شراء مؤكد) 🟢"
        elif price_chg > 0 and cvd_chg < 0:
            score -= 2; sigs["CVD"] = f"دايفرجنس: سعر↑ CVD↓ — بيع مؤسسي خفي 🔴🔴"
        elif price_chg < 0 and cvd_chg > 0:
            score += 2; sigs["CVD"] = f"دايفرجنس: سعر↓ CVD↑ — شراء مؤسسي خفي 🟢🟢"
        elif price_chg < 0 and cvd_chg < 0:
            score -= 1; sigs["CVD"] = f"CVD↓ مع السعر (تدفق بيع مستمر) 🔴"
        else:
            sigs["CVD"] = "CVD محايد ⚪"

        # ── 4. Absorption — آخر 3 شموع ──
        abs_found = False
        for i in range(-3, 0):
            body = abs(C[i] - O[i])
            rng  = H[i] - L[i]
            v_ratio = V[i] / avg_v if avg_v > 0 else 1
            if v_ratio > 2.0 and rng > 0 and body / rng < 0.35:
                if C[i] >= O[i]:
                    score += 2; sigs["ABS"] = f"امتصاص صاعد {v_ratio:.1f}x حجم 🟢🟢"
                else:
                    score -= 2; sigs["ABS"] = f"امتصاص هابط {v_ratio:.1f}x حجم 🔴🔴"
                abs_found = True
                break
        if not abs_found:
            sigs["ABS"] = "لا امتصاص ⚪"

        # ── 5. Volume Climax — الشمعة الأخيرة ──
        last_ratio = V[-1] / avg_v if avg_v > 0 else 1
        if last_ratio > 3.0:
            if C[-1] >= O[-1]:
                score += 1; sigs["CLMX"] = f"ذروة شراء مؤسسي {last_ratio:.1f}x 🟢"
            else:
                score -= 1; sigs["CLMX"] = f"ذروة بيع مؤسسي {last_ratio:.1f}x 🔴"
        else:
            sigs["CLMX"] = f"حجم عادي {last_ratio:.1f}x ⚪"

    except Exception as e:
        sigs["SVOL"] = f"خطأ: {e}"
    return score, sigs

def analyze(sym):
    """
    قرار الدخول يعتمد فقط على 3 مؤشرات ICT:
      1. iFVG  — Inverse Fair Value Gap
      2. SMT   — Smart Money Technique Divergence
      3. LIQ   — Liquidity Sweep (Stop Hunt)
    باقي المؤشرات للعرض فقط — لا تؤثر على score
    """
    daily_trend = get_daily_trend(sym)
    try:
        k = get_klines(sym, CONFIG["KLINE_INTERVAL"], 100)
    except Exception as e:
        log(f"خطأ {sym}: {e}", "error"); return None

    O=[float(x[1]) for x in k]; C=[float(x[4]) for x in k]
    H=[float(x[2]) for x in k]; L=[float(x[3]) for x in k]
    V=[float(x[5]) for x in k]
    O_c=O[:-1]; C_c=C[:-1]; H_c=H[:-1]; L_c=L[:-1]; V_c=V[:-1]
    n = len(C_c)
    price=C[-1]; score=0; sigs={}

    # ── فلتر ATR: رفض الأسواق الراكدة ──
    a = atr(H_c, L_c, C_c)
    atr_pct = (a / price * 100) if price > 0 else 0
    if atr_pct < CONFIG["MIN_ATR_PCT"]: return None

    # ── فلتر الترند: رفض الهابط فقط ──
    if daily_trend == "DOWN":
        return None
    sigs["TREND"] = f"{'🟢 صاعد' if daily_trend=='UP' else '⚪ محايد'}"

    # ════════════════════════════════════════
    # SCORE 1 — iFVG (Inverse Fair Value Gap)  max: +4
    # ════════════════════════════════════════
    ifvg_score, ifvg_sigs = detect_ifvg(H_c, L_c, C_c)
    score += ifvg_score; sigs.update(ifvg_sigs)

    # ════════════════════════════════════════
    # SCORE 2 — SMT Divergence vs BTC         max: +4
    # ════════════════════════════════════════
    smt_score, smt_sigs = detect_smt(H_c, L_c, C_c)
    score += smt_score; sigs.update(smt_sigs)

    # ════════════════════════════════════════
    # SCORE 3 — Liquidity Sweep (Stop Hunt)   max: +5
    # ════════════════════════════════════════
    liq_score, liq_sigs = detect_liquidity(H_c, L_c, C_c)
    score += liq_score; sigs.update(liq_sigs)

    # ════════════════════════════════════════
    # مؤشرات إضافية — للعرض فقط (لا تؤثر على score)
    # ════════════════════════════════════════
    r = rsi(C_c)
    sigs["RSI"]  = f"RSI {r:.0f}"
    m_val, m_sig, m_hist, m_mom = macd(C_c)
    sigs["MACD"] = "صاعد 🟢" if m_hist > 0 else "هابط 🔴"
    adx_val = adx(H_c, L_c, C_c)
    sigs["ADX"]  = f"ADX {adx_val:.0f}"
    vwap_val = vwap(H_c, L_c, C_c, V_c)
    sigs["VWAP"] = f"{'فوق' if price > vwap_val else 'تحت'} VWAP"
    e20 = ema(C_c, 20); e50 = ema(C_c, 50)
    sigs["EMA"]  = "EMA20>50 🟢" if e20 > e50 else "EMA20<50 ⚪"
    avg_v = sum(V_c[-20:]) / 20 if sum(V_c[-20:]) > 0 else 1
    v_ratio = V_c[-1] / avg_v if avg_v > 0 else 1
    sigs["VOL"]  = f"حجم {v_ratio:.1f}x"
    is_breakout = C_c[-1] > (max(H_c[-17:-1]) if len(H_c) >= 17 else max(H_c[:-1]))

    sup = min(L_c[-20:]); res = max(H_c[-20:])
    return {
        "symbol": sym, "price": price, "score": score, "signals": sigs,
        "rsi": r, "support": sup, "resistance": res,
        "atr": a, "atr_pct": atr_pct, "trend": daily_trend,
        "adx": adx_val, "vwap": vwap_val, "weekly": None,
        "breakout": is_breakout,
    }

# ══════════════════════════════════════════
#  TRADE ENGINE
# ══════════════════════════════════════════
def open_trade(result):
    global trade_timestamps
    sym=result["symbol"]; price=result["price"]; score=result["score"]
    now=time.time()

    # ── حماية: حد الخسارة اليومية ──
    if state.get("protected"): return

    if sym in CONFIG.get("BANNED_SYMBOLS", []): return  # محظور دائماً
    if sym in state["open_positions"]: return
    if now - last_trade_time.get(sym,0) < CONFIG["COOLDOWN"]: return

    # ── حد أقصى للصفقات في الساعة — يمنع التداول المتهور ──
    trade_timestamps = [t for t in trade_timestamps if now - t < 3600]
    if len(trade_timestamps) >= CONFIG.get("MAX_TRADES_PER_HOUR", 4):
        log(f"⚠️ حد الساعة: {len(trade_timestamps)} صفقة في الساعة الأخيرة — انتظار","warn")
        return

    # ── قاطع الخسائر المتتالية: توقف 45 دقيقة بعد 3 خسائر متتالية ──
    COOLDOWN_AFTER_LOSSES = 45 * 60  # 45 دقيقة
    MAX_CONSECUTIVE = 3
    if consecutive_losses >= MAX_CONSECUTIVE:
        remaining = COOLDOWN_AFTER_LOSSES - (now - last_loss_time)
        if remaining > 0:
            log(f"🛑 قاطع: {consecutive_losses} خسائر متتالية — انتظار {remaining/60:.0f}د","warn")
            return
        else:
            # انتهى وقت الانتظار — أعد العداد
            pass

    # ── قائمة حظر SL ──
    if sym in sl_blacklist and now - sl_blacklist[sym] < CONFIG["SL_BLACKLIST_MIN"]*60:
        return

    if len(state["open_positions"]) >= CONFIG["MAX_OPEN_POSITIONS"]: return

    daily_trend = result.get("trend", "NEUTRAL")

    # سبوت: شراء فقط — لا بيع على المكشوف
    if score <= 0:
        return  # إشارة هابطة — لا نتداول في السبوت
    side = "BUY"

    # ── BTC Regime (مراقبة فقط — Scalping لا يُوقف على BEAR أو NEUTRAL) ──
    btc_regime, btc_dom = get_btc_regime_cached()
    # Scalping: نرفض فقط الترند الهابط الصريح على فريم التحليل
    if daily_trend == "DOWN":
        log(f"⏭ {sym} ترند 1h هابط — تخطي","info"); return

    # ── SL/TP من ATR نفس فريم التحليل (Scalping: 15m) ──
    a = result.get("atr", price * 0.003)  # ATR من analyze() مباشرة
    if a <= 0: a = price * 0.003
    if a <= 0: return

    # رفض الصفقة إذا كان ATR أقل من الحد الأدنى (سوق راكد)
    atr_pct_check = a / price * 100
    if atr_pct_check < CONFIG["MIN_ATR_PCT"]:
        log(f"⏭ {sym} ATR {atr_pct_check:.3f}% أقل من الحد {CONFIG['MIN_ATR_PCT']}% — سوق راكد","info")
        return
    # رفض إذا كان ATR أكبر من 8% (عملة متقلبة بلا سيطرة)
    if atr_pct_check > 8.0:
        log(f"⛔ {sym} ATR {atr_pct_check:.1f}% كبير جداً — تخطي","warn")
        return

    log(f"{'─'*45}","info")
    log(f"إشارة {side} | {sym} | ${price:,.4f} | نقاط:{score:+d} | يومي:{daily_trend} | ADX:{result.get('adx',0):.0f} | ATR:{result.get('atr_pct',0):.2f}%","signal")

    try:
        bal = get_balance("USDT")
        if bal < 15: log(f"رصيد غير كافٍ: ${bal:.2f}","warn"); return

        flt = get_filters(sym)
        tk  = flt["tick"]
        pr  = max(0, round(-math.log10(tk)))

        sl_raw = price-(a*CONFIG["ATR_SL_MULT"]) if side=="BUY" else price+(a*CONFIG["ATR_SL_MULT"])
        tp_raw = price+(a*CONFIG["ATR_TP_MULT"]) if side=="BUY" else price-(a*CONFIG["ATR_TP_MULT"])

        sl_dist = abs(price - sl_raw)
        if sl_dist <= 0: return

        # ── إدارة رأس المال: خاطر بمبلغ ثابت + حد أقصى 8% من الرصيد ──
        risk_usdt       = bal * (CONFIG["RISK_PER_TRADE_PCT"] / 100)
        qty_by_risk     = risk_usdt / sl_dist
        qty_by_notional = (bal * 0.08) / price   # رُفع من 5% → 8% لضمان أرباح معقولة
        qty = round_step(min(qty_by_risk, qty_by_notional), flt["step"])
        notional = qty * price

        # ── فلتر الربح الأدنى بعد العمولة ──
        # العمولة: 0.1% دخول + 0.1% خروج = 0.2% من الصفقة
        fee_cost    = notional * 0.002
        tp_pct      = (a * CONFIG["ATR_TP_MULT"]) / price
        expected_profit = notional * tp_pct - fee_cost
        MIN_PROFIT  = 2.0   # الحد الأدنى للربح المتوقع عند TP بعد العمولة ($)
        if expected_profit < MIN_PROFIT:
            log(f"⏭ {sym} ربح متوقع ${expected_profit:.2f} < ${MIN_PROFIT} — لا يستحق العمولة","info")
            return

        # تحقق من الحد الأدنى والحد الأقصى
        if qty < flt["minQ"] or notional < flt["minN"]:
            log(f"كمية صغيرة جداً: {qty} (notional ${notional:.2f})","warn"); return
        if notional > bal * 0.95:
            log(f"الصفقة تتجاوز الرصيد: ${notional:.2f} > ${bal:.2f}","warn"); return

        order = place_order(sym, side, "MARKET", quantity=qty)
        fills = order.get("fills", [])
        fp = (sum(float(f["price"])*float(f["qty"]) for f in fills)
              / sum(float(f["qty"]) for f in fills)) if fills else price
        fq = float(order.get("executedQty", qty))
        if fp <= 0: fp = price

        sl_final = round(fp-(a*CONFIG["ATR_SL_MULT"]) if side=="BUY" else fp+(a*CONFIG["ATR_SL_MULT"]), pr)
        tp_final = round(fp+(a*CONFIG["ATR_TP_MULT"]) if side=="BUY" else fp-(a*CONFIG["ATR_TP_MULT"]), pr)
        sl_pct = abs(fp-sl_final)/fp*100
        tp_pct = abs(fp-tp_final)/fp*100

        # رفض الصفقة إذا كان SL أكبر من 20% (عملة متقلبة جداً بلا تحكم)
        if sl_pct > 20:
            log(f"⛔ {sym} SL بعيد جداً {sl_pct:.1f}% — تخطي","warn"); return

        log(f"✅ {side} {sym} | ${fp:,.4f} | qty:{fq} | خطر:${risk_usdt:.2f}","success")
        log(f"🛡️ SL:${sl_final} ({sl_pct:.2f}%) → TP:${tp_final} ({tp_pct:.2f}%) | RR:{tp_pct/sl_pct:.1f}:1","info")

        state["open_positions"][sym] = {
            "side":side, "entry":fp, "qty":fq,
            "sl":sl_final, "tp":tp_final,
            "be_done":False,              # هل تم نقل SL للبريك إيفن؟
            "partial_done":False,         # هل تم جني جزئي؟
            "trail_sl":None,              # SL المتحرك الحالي
            "atr":a, "atr_pct":round(a/fp*100, 3),
            "order_id":order["orderId"],
            "open_time":now,
            "max_hold":CONFIG["MAX_HOLD_MINUTES"],  # يُحفظ عند الفتح — لا يتأثر بتغيير الإعدادات لاحقاً
        }
        last_trade_time[sym] = now
        trade_timestamps.append(now)  # تسجيل لحد الساعة
        save_state()

        # OCO لتأمين الصفقة على Binance
        try:
            cs = "SELL" if side=="BUY" else "BUY"
            api("POST","/api/v3/order/oco",{
                "symbol":sym,"side":cs,"quantity":fq,
                "price":tp_final,
                "stopPrice":sl_final,
                "stopLimitPrice":round(sl_final*(0.999 if side=="BUY" else 1.001),pr),
                "stopLimitTimeInForce":"GTC"
            }, signed=True)
            log("✅ OCO مُفعّل على Binance","info")
        except Exception as e:
            log(f"تحذير OCO: {e}","warn")

    except Exception as e:
        err = str(e)
        if "Market is closed" in err or "market is not open" in err.lower():
            sl_blacklist[sym] = time.time()
            log(f"⛔ {sym} سوقه مغلق — تم حذفه","warn")
            if sym in CONFIG.get("SYMBOLS", []):
                CONFIG["SYMBOLS"].remove(sym)
        elif "insufficient balance" in err.lower():
            log(f"رصيد غير كافٍ للصفقة — تخطي {sym}","warn")
        else:
            log(f"فشل الصفقة: {e}","error")

def check_positions():
    now = time.time()
    for sym in list(state["open_positions"].keys()):
        pos = state["open_positions"].get(sym)
        if not pos: continue
        try:
            # 15m بدل 1m — يتجنب الويك الكاذب الذي يضرب SL بدون إغلاق حقيقي
            k   = get_klines(sym,"15m",3)
            cur = float(k[-1][4])       # إغلاق الكندل الأخير
            cur_high = float(k[-1][2])
            cur_low  = float(k[-1][3])
        except: continue

        side  = pos["side"]
        entry = pos["entry"]
        a     = pos.get("atr", entry*0.008)
        pr    = max(0, round(-math.log10(max(a/1000, 0.00000001))))

        # ── 1. خروج بالوقت — يستخدم max_hold المحفوظ عند فتح الصفقة
        # هذا يمنع إغلاق صفقات Swing قسراً عند التحويل لـ Scalping
        held_min = (now - pos["open_time"]) / 60
        pos_max_hold = pos.get("max_hold", CONFIG["MAX_HOLD_MINUTES"])
        if held_min >= pos_max_hold:
            close_position(sym, cur, f"وقت ⏰ {held_min:.0f}د")
            continue

        tp_dist = pos["tp"] - entry

        # ── 2. بريك إيفن: بعد BREAKEVEN_TRIGGER من TP ──
        if not pos.get("be_done"):
            be_trigger = entry + tp_dist * CONFIG["BREAKEVEN_TRIGGER"]
            if cur_high >= be_trigger:
                new_sl = round(entry * 1.001, pr)
                pos["sl"] = new_sl
                pos["be_done"] = True
                log(f"🔒 بريك إيفن {sym} | SL → ${new_sl} ({CONFIG['BREAKEVEN_TRIGGER']*100:.0f}% TP)","info")
                save_state()

        # ── 3. جني أرباح جزئي عند 50% من TP (25% من الكمية) ──
        if pos.get("be_done") and not pos.get("partial_done"):
            partial_trigger = entry + tp_dist * 0.50
            if cur_high >= partial_trigger:
                partial_qty = round(pos["qty"] * 0.25, 8)
                try:
                    flt = get_filters(sym)
                    step = flt["step"]
                    partial_qty = math.floor(partial_qty / step) * step
                    if partial_qty > 0 and partial_qty * cur >= flt["minN"]:
                        place_order(sym, "SELL", "MARKET", quantity=partial_qty)
                        fee = partial_qty * cur * 0.001  # عمولة الخروج 0.1%
                        partial_pnl = partial_qty * (cur - entry) - fee
                        state["total_pnl"] += partial_pnl
                        state["daily_pnl"] += partial_pnl
                        pos["qty"] -= partial_qty
                        pos["partial_done"] = True
                        log(f"💰 جني جزئي {sym} | 25% عند ${cur:.4f} | ربح: ${partial_pnl:.2f}","success")
                        save_state()
                except Exception as pe:
                    log(f"تحذير جني جزئي {sym}: {pe}","warn")
                    pos["partial_done"] = True  # تجنب التكرار
                    save_state()  # حفظ إلزامي — تجنب إعادة المحاولة بعد restart

        # ── 4. تريلينج ستوب — يبدأ بعد ربح 1 ATR (لا ينتظر البريك إيفن) ──
        # المرحلة 1: دخول التريلينج بعد ربح بسيط (1× ATR)
        trail_entry_trigger = entry + a  # ربح 1 ATR يكفي للتفعيل
        if cur_high >= trail_entry_trigger:
            trail_sl = pos.get("trail_sl") or pos["sl"]
            # المرحلة 2: كلما ابتعد السعر عن الدخول، ضيّق التريلينج
            profit_atrs = (cur_high - entry) / a  # كم ATR ربحنا
            if profit_atrs >= 3.0:
                mult = CONFIG["TRAILING_ATR_MULT"] * 0.7   # ضيّق للحفاظ على الربح الكبير
            elif profit_atrs >= 2.0:
                mult = CONFIG["TRAILING_ATR_MULT"] * 0.85
            else:
                mult = CONFIG["TRAILING_ATR_MULT"]
            new_trail = round(cur_high - a * mult, pr)
            # SL لا يُرفع أبداً دون حد الدخول في المراحل الأولى
            floor_sl  = round(entry * 0.999, pr)  # لا يُنزل أسفل الدخول تقريباً
            new_trail = max(new_trail, floor_sl) if not pos.get("be_done") else new_trail
            if new_trail > trail_sl and new_trail < cur:
                pos["trail_sl"] = new_trail
                pos["sl"]       = new_trail
                log(f"📈 تريلينج {sym} | SL → ${new_trail} ({profit_atrs:.1f}× ATR ربح | mult:{mult:.2f}×)","info")
                save_state()

        # ── 5. فحص TP / SL (سبوت = شراء فقط) ──
        hit_tp = cur_high >= pos["tp"]  # high الكندل لاكتشاف ضرب TP الحقيقي
        hit_sl = cur_low  <= pos["sl"]  # low الكندل لاكتشاف ضرب SL الحقيقي

        if hit_tp:
            close_position(sym, pos["tp"], "TP ✅")
        elif hit_sl:
            close_position(sym, pos["sl"], "SL ❌")

def close_position(sym, cur, reason):
    global consecutive_losses, last_loss_time
    pos = state["open_positions"].pop(sym, None)
    if not pos: return
    side=pos["side"]; entry=pos["entry"]; qty=pos["qty"]
    held_min = (time.time() - pos["open_time"]) / 60

    # احتساب PnL — سبوت شراء فقط
    pnl_pct = (cur - entry) / entry * 100
    # إذا تم جني جزئي: عمولة الدخول دُفعت مرة واحدة على الكمية الكاملة
    # هنا ندفع فقط عمولة الخروج 0.1% على الكمية المتبقية
    exit_fee_pct = 0.1
    entry_fee_pct = 0.0 if pos.get("partial_done") else 0.1
    fee_pct = entry_fee_pct + exit_fee_pct
    pnl_pct_net = pnl_pct - fee_pct
    pnl = qty * entry * pnl_pct_net / 100

    state["total_pnl"]  += pnl
    state["daily_pnl"]  += pnl
    if pnl >= 0:
        state["wins"] += 1
        consecutive_losses = 0   # أعد العداد عند أي ربح
    else:
        state["losses"] += 1
        consecutive_losses += 1
        last_loss_time = time.time()
        if consecutive_losses >= 3:
            log(f"⚠️ {consecutive_losses} خسائر متتالية — سيتوقف البوت 45 دقيقة","warn")

    emoji = "✅" if pnl>=0 else "❌"
    log(f"{emoji} {sym} | {reason} | {held_min:.0f}د | PnL:{pnl:+.4f}$ ({pnl_pct_net:+.2f}%)","success" if pnl>=0 else "error")

    # حظر العملة مؤقتاً بعد SL
    if "SL" in reason:
        sl_blacklist[sym] = time.time()
        log(f"🚫 {sym} محظور {CONFIG['SL_BLACKLIST_MIN']}د بعد SL","warn")

    try:
        cancel_orders(sym)
        cs = "SELL" if side=="BUY" else "BUY"
        try:
            flt = get_filters(sym)
            step = flt["step"]
            # تقريب صحيح: floor إلى أقرب step
            qty_rounded = math.floor(qty / step) * step
            # تقريب عدد الأرقام العشرية حسب step
            decimals = max(0, round(-math.log10(step))) if step < 1 else 0
            qty_rounded = round(qty_rounded, decimals)
            if qty_rounded <= 0:
                log(f"كمية صفر بعد التقريب {sym} qty:{qty} step:{step} — تخطي البيع","warn")
                qty_rounded = None  # لا نبيع لكن نكمل تسجيل الصفقة
            if qty_rounded:
                log(f"إغلاق {sym}: qty_orig={qty} step={step} qty_final={qty_rounded}","info")
        except Exception as fe:
            log(f"فشل get_filters {sym}: {fe} — استخدام الكمية الأصلية","warn")
            qty_rounded = qty
        if qty_rounded:
            place_order(sym, cs, "MARKET", quantity=qty_rounded)
    except Exception as e:
        log(f"تحذير إغلاق: {e}","warn")

    state["trades"].append({
        "symbol":sym, "side":side,
        "entry":round(entry,6), "exit":round(cur,6),
        "pnl":round(pnl,4), "pnl_pct":round(pnl_pct_net,2),
        "reason":reason, "held":f"{held_min:.0f}د",
        "time":datetime.now().strftime("%H:%M:%S")
    })
    if len(state["trades"]) > 200: state["trades"].pop(0)
    save_state()

    # ── فحص حد الخسارة اليومية ──
    try:
        bal = get_balance("USDT")
        daily_loss_pct = abs(state["daily_pnl"]) / max(state["daily_start_balance"],1) * 100
        if state["daily_pnl"] < 0 and daily_loss_pct >= CONFIG["DAILY_LOSS_LIMIT_PCT"]:
            state["protected"] = True
            log(f"🛑 حد الخسارة اليومية! -{daily_loss_pct:.1f}% — البوت متوقف حتى الغد","error")
    except: pass

def bot_loop():
    global daily_reset_day
    load_state()
    log("🚀 APEX ULTIMATE PRO بدأ","success")
    _sync_time()
    try:
        bal = get_balance("USDT")
        state["balance"] = bal
        if state["daily_start_balance"] == 0.0:
            state["daily_start_balance"] = bal
        log(f"💼 رصيد: ${bal:.2f} USDT | PnL محفوظ: ${state['total_pnl']:+.4f}","success")
    except Exception as e:
        log(f"فشل الرصيد: {e}","error")

    CONFIG["SYMBOLS"] = fetch_top_symbols(CONFIG["TOP_N"])
    sync_spot_positions()
    last_refresh = time.time()

    cycle=0
    while state["running"]:
        cycle+=1

        # ── إعادة ضبط يومي تلقائي عند منتصف الليل ──
        today = datetime.now().day
        if today != daily_reset_day:
            daily_reset_day = today
            state["daily_pnl"] = 0.0
            state["protected"] = False
            try: state["daily_start_balance"] = get_balance("USDT")
            except: pass
            log(f"🌅 يوم جديد — daily_pnl صُفِّر | رصيد:${state['daily_start_balance']:.2f}","info")

        # ── إذا كان البوت محمياً توقف عن الدخول لكن راقب المفتوحة ──
        if state.get("protected"):
            if state["open_positions"]: check_positions()
            time.sleep(CONFIG["LOOP_INTERVAL"]); continue

        # تحديث قائمة الترند كل 15 دقيقة
        if time.time() - last_refresh > 900:
            CONFIG["SYMBOLS"] = fetch_top_symbols(CONFIG["TOP_N"])
            last_refresh = time.time()
        try:
            # ── BTC Regime: أهم قرار في كل دورة ──
            btc_reg, btc_dom = get_btc_regime_cached()
            regime_icons = {"BULL":"🟢 BULL","BEAR":"🔴 BEAR","RANGING":"🟡 RANGING"}
            dom_icons = {"FALLING":"📉 ALT.SEASON","RISING":"📈 BTC.D↑","NEUTRAL":"➡️ BTC.D محايد"}
            log(f"🔍 BTC Market: {regime_icons.get(btc_reg,btc_reg)} | {dom_icons.get(btc_dom,btc_dom)} | رصيد:${state['balance']:.2f} | مفتوحة:{len(state['open_positions'])} | إجمالي PnL:${state['total_pnl']:+.2f}","info")

            if state["open_positions"]: check_positions()

            # ── تحليل متوازٍ لكل العملات (أسرع بـ 10x) ──
            candidates = []
            syms = list(CONFIG["SYMBOLS"])
            active_set = set(syms)

            # تنظيف بيانات السوق القديمة — عملات خرجت من القائمة
            for old_sym in list(state["market"].keys()):
                if old_sym not in active_set:
                    state["market"].pop(old_sym, None)

            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(analyze, sym): sym for sym in syms}
                for fut in as_completed(futures):
                    r = fut.result()
                    if not r: continue
                    state["market"][r["symbol"]] = {
                        "price": r["price"], "score": r["score"],
                        "signals": r["signals"], "rsi": r["rsi"],
                        "weekly": r.get("weekly")
                    }
                    # تنبيه للإشارات الشرائية القوية فقط (score موجب ≥ الحد)
                    if r["score"] >= CONFIG["MIN_SIGNAL_SCORE"]:
                        candidates.append(r)
                        push({"type":"alert","symbol":r["symbol"],"score":r["score"],
                              "price":r["price"],"time":datetime.now().strftime("%H:%M:%S")})

            # سبوت: فرص الشراء فقط (score موجب) — مرتبة بالنقاط
            candidates = [r for r in candidates if r["score"] > 0]
            candidates.sort(key=lambda x: x["score"], reverse=True)
            opened = 0
            for r in candidates:
                if len(state["open_positions"]) >= CONFIG["MAX_OPEN_POSITIONS"]: break
                log(f"فرصة شراء: {r['symbol']} نقاط:{r['score']:+d} ADX:{r.get('adx',0):.0f} ATR:{r.get('atr_pct',0):.2f}%","signal")
                open_trade(r)
                opened += 1
                # فاصل 30 ثانية بين كل صفقة — يمنع فتح 30 صفقة دفعة واحدة
                if opened < len(candidates) and len(state["open_positions"]) < CONFIG["MAX_OPEN_POSITIONS"]:
                    time.sleep(30)
            if not candidates:
                log(f"لا توجد إشارات (score≥{CONFIG['MIN_SIGNAL_SCORE']}) | فحص {len(syms)} عملة","info")

            try: state["balance"]=get_balance("USDT")
            except: pass

            push({"type":"snapshot",**snapshot()})

        except Exception as e:
            log(f"خطأ: {e}","error")

        time.sleep(CONFIG["LOOP_INTERVAL"])

    log("⏹ البوت متوقف","warn")

def snapshot():
    t = state["wins"]+state["losses"]
    db = state.get("daily_start_balance",1) or 1
    return {
        "running":   state["running"],
        "protected": state.get("protected",False),
        "balance":   round(state["balance"],2),
        "total_pnl": round(state["total_pnl"],4),
        "daily_pnl": round(state.get("daily_pnl",0),4),
        "daily_pnl_pct": round(state.get("daily_pnl",0)/db*100,2),
        "withdrawn": round(state["withdrawn"],4),
        "wins":  state["wins"], "losses": state["losses"],
        "win_rate": round(state["wins"]/t*100,1) if t else 0,
        "blacklisted": list(sl_blacklist.keys()),
        "top100":      state.get("top100", {}),
        "btc_regime":  _btc_regime_cache["regime"],
        "btc_dom":     _btc_regime_cache.get("dominance","NEUTRAL"),
        "open_positions":[
            {"symbol":k,"side":v["side"],
             "entry":round(v["entry"],6),"qty":round(v["qty"],6),
             "sl":round(v["sl"],6),"tp":round(v["tp"],6),
             "be_done":v.get("be_done",False),
             "trail_sl":v.get("trail_sl"),
             "atr_pct":round(v.get("atr_pct",0),3),
             "held_min":round((time.time()-v.get("open_time",time.time()))/60,1),
             "cur_price":round(state["market"].get(k,{}).get("price", v["entry"]),6),
             "pnl_pct":round(((state["market"].get(k,{}).get("price",v["entry"])-v["entry"])/v["entry"]*100)
                             if v["side"]=="BUY" else
                             ((v["entry"]-state["market"].get(k,{}).get("price",v["entry"]))/v["entry"]*100), 2)}
            for k,v in state["open_positions"].items()
        ],
        "trades": list(reversed(state["trades"][-20:])),
        "market": state["market"],
        "net":    "TESTNET 🧪" if CONFIG["TESTNET"] else "LIVE 🔴",
        "config": {
            "symbols":   CONFIG["SYMBOLS"],
            "sl_mult":   CONFIG["ATR_SL_MULT"],
            "tp_mult":   CONFIG["ATR_TP_MULT"],
            "risk_pct":  CONFIG["RISK_PER_TRADE_PCT"],
            "interval":  CONFIG["KLINE_INTERVAL"],
            "max_open":  CONFIG["MAX_OPEN_POSITIONS"],
            "max_pos":   CONFIG["MAX_OPEN_POSITIONS"],
            "min_score": CONFIG["MIN_SIGNAL_SCORE"],
            "adx_min":   CONFIG["ADX_MIN"],
            "daily_loss_limit": CONFIG["DAILY_LOSS_LIMIT_PCT"],
            "daily_limit": CONFIG["DAILY_LOSS_LIMIT_PCT"],
            "max_hold_minutes": CONFIG["MAX_HOLD_MINUTES"],
        }
    }

# ══════════════════════════════════════════
#  BACKTEST ENGINE
# ══════════════════════════════════════════
def backtest(sym, days=180, start_balance=1000.0,
             sl_mult=None, tp_mult=None, min_score=None, interval=None):
    """
    محاكاة Walk-Forward على بيانات تاريخية:
    - تحميل كندلات تاريخية من Binance
    - تحليل كل كندل مغلق بنفس منطق analyze()
    - محاكاة SL/TP بناءً على كندلات المستقبل
    - إرجاع: balance, trades, win_rate, max_drawdown, profit_factor
    """
    sl_m   = sl_mult  if sl_mult  is not None else CONFIG["ATR_SL_MULT"]
    tp_m   = tp_mult  if tp_mult  is not None else CONFIG["ATR_TP_MULT"]
    min_sc = min_score if min_score is not None else CONFIG["MIN_SIGNAL_SCORE"]
    ivl    = interval  if interval  is not None else CONFIG["KLINE_INTERVAL"]

    # حساب عدد الكندلات المطلوبة
    bars_per_day = {"1h":24,"2h":12,"4h":6,"6h":4,"8h":3,"12h":2,"1d":1,"1w":0.143}.get(ivl,6)
    limit = min(1000, int(days * bars_per_day) + 100)

    try:
        raw = get_klines(sym, ivl, limit)
    except Exception as e:
        return {"error": str(e)}

    if len(raw) < 60:
        return {"error": f"بيانات غير كافية: {len(raw)} كندل فقط"}

    O = [float(x[1]) for x in raw]
    H = [float(x[2]) for x in raw]
    L = [float(x[3]) for x in raw]
    C = [float(x[4]) for x in raw]
    V = [float(x[5]) for x in raw]
    times = [int(x[0]) for x in raw]

    balance = start_balance
    peak_balance = start_balance
    max_drawdown = 0.0
    trades = []
    position = None   # {"entry","sl","tp","qty","bar","atr"}
    cooldown_bar = -999
    cooldown_bars = int(CONFIG["COOLDOWN"] / (3600 * (24/bars_per_day))) if bars_per_day > 0 else 6
    fee_pct = 0.001   # 0.1% دخول + 0.1% خروج

    MIN_BARS = 55  # نبدأ التحليل بعد 55 كندل لضمان ATR/RSI/MACD كافيين

    for i in range(MIN_BARS, len(C) - 1):
        # ─ محاكاة الإغلاق الحالي للمركز ─
        if position:
            fut_h = H[i]  # أعلى الكندل الحالي
            fut_l = L[i]  # أدنى الكندل الحالي
            fut_c = C[i]  # إغلاق الكندل

            # TP يُضرب أولاً (الأفضل للبائع)
            exit_price = None
            reason = ""
            if fut_h >= position["tp"]:
                exit_price = position["tp"]; reason = "TP ✅"
            elif fut_l <= position["sl"]:
                exit_price = position["sl"]; reason = "SL ❌"
            elif (i - position["bar"]) >= int(CONFIG["MAX_HOLD_MINUTES"] * bars_per_day / 1440):
                exit_price = fut_c; reason = "وقت ⏰"

            if exit_price:
                pnl_pct = (exit_price - position["entry"]) / position["entry"]
                pnl = position["qty"] * pnl_pct - (position["qty"] * fee_pct * 2)
                balance += pnl
                if balance > peak_balance: peak_balance = balance
                dd = (peak_balance - balance) / peak_balance * 100
                if dd > max_drawdown: max_drawdown = dd
                trades.append({
                    "bar": i,
                    "time": datetime.fromtimestamp(times[i]//1000).strftime("%Y-%m-%d"),
                    "entry": round(position["entry"], 6),
                    "exit":  round(exit_price, 6),
                    "pnl":   round(pnl, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "reason": reason,
                    "held":  i - position["bar"]
                })
                cooldown_bar = i
                position = None

        # ─ بحث عن إشارة دخول ─
        if position: continue
        if (i - cooldown_bar) < cooldown_bars: continue

        # نفس منطق analyze() — على الكندل المغلق
        c = C[:i]; h = H[:i]; l = L[:i]; v = V[:i]

        a4 = atr(h, l, c)
        atr_p = a4 / c[-1] * 100 if c[-1] > 0 else 0
        if atr_p < CONFIG["MIN_ATR_PCT"]: continue

        adx_v = adx(h, l, c)
        if adx_v < CONFIG["ADX_MIN"]: continue

        # ATR يومي لـ SL/TP
        a_sl = a4
        if ivl != "1d":
            # تقريب: ATR اليومي ≈ ATR_4h × جذر(6)
            a_sl = a4 * (bars_per_day ** 0.5)

        r_val = rsi(c)
        m_val, _, m_hist, m_mom = macd(c)
        e20 = ema(c, 20); e50 = ema(c, 50)
        bb_v = bollinger(c)

        sc = 0
        # RSI
        if r_val < 40:   sc += 1
        elif r_val > 65: sc -= 1
        # MACD
        if m_hist > 0 and m_mom > 0:   sc += 2
        elif m_hist > 0:                sc += 1
        elif m_hist < 0 and m_mom < 0: sc -= 2
        elif m_hist < 0:                sc -= 1
        # BB
        if bb_v < 15:   sc += 1
        elif bb_v > 85: sc -= 1
        # EMA
        if e20 > e50: sc += 1
        else:         sc -= 1
        # ADX bonus
        if adx_v > 30: sc += 1
        # Divergence
        rdiv, mdiv = detect_divergence(c, h, l, lookback=min(30, len(c)//3))
        sc += rdiv * 2 + mdiv * 2

        if sc < min_sc: continue

        price = C[i]  # دخول عند إغلاق الكندل التالي (محاكاة واقعية)
        sl = round(price - a_sl * sl_m, 8)
        tp = round(price + a_sl * tp_m, 8)
        sl_pct = (price - sl) / price * 100
        if sl_pct > 20: continue  # SL واسع جداً

        risk_usdt = balance * (CONFIG["RISK_PER_TRADE_PCT"] / 100)
        sl_dist = price - sl
        if sl_dist <= 0: continue
        qty_r = risk_usdt / sl_dist
        qty_n = (balance * 0.05) / price
        qty   = min(qty_r, qty_n)
        notional = qty * price
        if notional < 10: continue

        entry_fee = notional * fee_pct
        balance -= entry_fee
        position = {"entry": price, "sl": sl, "tp": tp,
                    "qty": notional, "bar": i, "atr": a_sl}

    # أغلق أي صفقة مفتوحة في نهاية البيانات
    if position:
        exit_price = C[-1]
        pnl_pct = (exit_price - position["entry"]) / position["entry"]
        pnl = position["qty"] * pnl_pct - (position["qty"] * fee_pct)
        balance += pnl
        trades.append({"bar": len(C)-1,
                        "time": datetime.fromtimestamp(times[-1]//1000).strftime("%Y-%m-%d"),
                        "entry": round(position["entry"],6), "exit": round(exit_price,6),
                        "pnl": round(pnl,4), "pnl_pct": round(pnl_pct*100,2),
                        "reason": "نهاية البيانات", "held": len(C)-1-position["bar"]})

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999

    return {
        "symbol":        sym,
        "interval":      ivl,
        "days":          days,
        "bars":          len(C),
        "start_balance": start_balance,
        "end_balance":   round(balance, 2),
        "net_pnl":       round(balance - start_balance, 2),
        "net_pnl_pct":   round((balance - start_balance) / start_balance * 100, 2),
        "total_trades":  len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins)/len(trades)*100, 1) if trades else 0,
        "max_drawdown":  round(max_drawdown, 2),
        "profit_factor": profit_factor,
        "avg_win":       round(sum(t["pnl_pct"] for t in wins)/len(wins), 2)    if wins   else 0,
        "avg_loss":      round(sum(t["pnl_pct"] for t in losses)/len(losses), 2) if losses else 0,
        "trades":        trades[-50:]  # آخر 50 صفقة للعرض
    }


# ══════════════════════════════════════════
#  WEB DASHBOARD
# ══════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX ULTIMATE</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Tajawal:wght@400;700&display=swap');
:root{--bg:#050810;--s:#0b101b;--c:#101825;--b:#1a2840;--a:#00d4ff;--g:#00ff88;--r:#ff3b6b;--y:#ffd166;--p:#a78bfa;--t:#e2e8f0;--m:#4a6080}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Tajawal',sans-serif;min-height:100vh}
body::before{content:'';position:fixed;top:-20vh;left:50%;transform:translateX(-50%);width:70vw;height:50vh;background:radial-gradient(ellipse,rgba(0,212,255,.05) 0%,transparent 70%);pointer-events:none}

/* Header */
header{display:flex;align-items:center;justify-content:space-between;padding:14px 26px;border-bottom:1px solid var(--b);background:rgba(11,16,27,.95);position:sticky;top:0;z-index:99;backdrop-filter:blur(10px)}
.logo{font-family:'IBM Plex Mono',monospace;font-size:1.1rem;font-weight:600;color:var(--a);letter-spacing:.12em}
.logo small{color:var(--t);opacity:.3;font-size:.75rem}
.hright{display:flex;align-items:center;gap:10px}
.net-badge{font-family:'IBM Plex Mono',monospace;font-size:.68rem;padding:4px 10px;border-radius:4px;background:rgba(255,59,107,.1);border:1px solid rgba(255,59,107,.3);color:var(--r)}
.pill{display:flex;align-items:center;gap:7px;padding:5px 14px;border-radius:50px;font-family:'IBM Plex Mono',monospace;font-size:.7rem;border:1px solid rgba(0,255,136,.2);color:var(--g)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--g);animation:p 1.5s infinite}
@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(0,255,136,.4)}50%{box-shadow:0 0 0 5px rgba(0,255,136,0)}}

/* Layout */
.wrap{padding:18px 22px;display:flex;flex-direction:column;gap:16px}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.stat{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:15px;border-top:2px solid var(--ac,var(--a))}
.sl{font-size:.68rem;color:var(--m);margin-bottom:4px}
.sv{font-family:'IBM Plex Mono',monospace;font-size:1.25rem;font-weight:600}
.ss{font-size:.65rem;color:var(--m);margin-top:3px}
.ga{color:var(--g)}.ra{color:var(--r)}.aa{color:var(--a)}.ya{color:var(--y)}.pa{color:var(--p)}

/* Market Grid */
.mgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.mc{background:var(--c);border:1px solid var(--b);border-radius:8px;padding:11px;transition:border .2s;cursor:default}
.mc.hot{border-color:rgba(0,255,136,.35)}
.mc.sell{border-color:rgba(255,59,107,.25)}
.mcs{font-family:'IBM Plex Mono',monospace;font-size:.8rem;font-weight:600;margin-bottom:2px}
.mcp{font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--m)}
.mcsc{font-size:.72rem;margin-top:5px;font-family:'IBM Plex Mono',monospace}
.bar{height:3px;background:var(--b);border-radius:2px;overflow:hidden;margin-top:5px}
.bf{height:100%;border-radius:2px;transition:width .5s}

/* Two column */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px}

/* Card */
.card{background:var(--c);border:1px solid var(--b);border-radius:10px;overflow:hidden}
.ch{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--b);font-size:.82rem;font-weight:500}
table{width:100%;border-collapse:collapse}
th{padding:8px 14px;text-align:right;font-size:.65rem;color:var(--m);font-family:'IBM Plex Mono',monospace;border-bottom:1px solid var(--b);font-weight:400;letter-spacing:.06em}
td{padding:9px 14px;font-size:.76rem;border-bottom:1px solid rgba(26,40,64,.4)}
tr:last-child td{border:none}
tr:hover td{background:rgba(255,255,255,.015)}
.badge{font-size:.62rem;padding:2px 7px;border-radius:3px;font-family:'IBM Plex Mono',monospace}
.buy{background:rgba(0,255,136,.1);color:var(--g)}.sell2{background:rgba(255,59,107,.1);color:var(--r)}

/* Log */
.logbox{background:var(--bg);border:1px solid var(--b);border-radius:8px;padding:11px;height:190px;overflow-y:auto;font-family:'IBM Plex Mono',monospace;font-size:.68rem;line-height:1.9}
.le{display:flex;gap:9px}
.lt{color:var(--m);flex-shrink:0}
.li{color:var(--a)}.ls{color:var(--g)}.lw{color:var(--y)}.lr{color:var(--r)}.ltd{color:var(--p)}.lsi{color:#f9a8d4}

/* Buttons */
.btn{padding:9px 20px;border-radius:7px;border:none;font-weight:700;font-size:.85rem;cursor:pointer;transition:all .2s;font-family:'Tajawal',sans-serif;letter-spacing:.03em}
.bstart{background:linear-gradient(135deg,#00d4ff,#0055ff);color:#000;box-shadow:0 0 18px rgba(0,212,255,.3)}
.bstop{background:linear-gradient(135deg,#ff3b6b,#ff6b00);color:#fff;box-shadow:0 0 18px rgba(255,59,107,.3)}
.btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.bsm{padding:5px 12px;font-size:.72rem;border-radius:5px;border:1px solid var(--b);background:var(--c);color:var(--m);cursor:pointer;font-family:'Tajawal',sans-serif;transition:all .2s}
.bsm:hover{border-color:var(--a);color:var(--a)}
.bsm.active{border-color:var(--a);color:var(--a);background:rgba(0,212,255,.08)}

/* Signals panel */
.sigs{display:flex;flex-wrap:wrap;gap:5px;padding:10px 14px}
.sig-item{font-size:.65rem;padding:3px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;border:1px solid var(--b);background:var(--s)}

/* Scrollbar */
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--b);border-radius:2px}

.sec{font-size:.78rem;font-weight:500;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.sec::before{content:'';width:3px;height:13px;background:var(--a);border-radius:2px}
.lvl-tag{display:flex;align-items:center;gap:5px;font-family:'IBM Plex Mono',monospace}
.lvl-dot{width:8px;height:8px;border-radius:50%}
.mc{cursor:pointer;transition:transform .15s}
.mc:hover{transform:translateY(-2px);border-color:var(--a)}
#toastBox{position:fixed;bottom:20px;left:20px;z-index:999;display:flex;flex-direction:column;gap:8px}
.toast{background:var(--c);border:1px solid var(--g);border-radius:8px;padding:12px 16px;
  font-family:'IBM Plex Mono',monospace;font-size:.8rem;box-shadow:0 4px 20px rgba(0,255,136,.2);
  animation:toastIn .3s ease;min-width:220px}
@keyframes toastIn{from{opacity:0;transform:translateX(-20px)}to{opacity:1;transform:translateX(0)}}
.settings-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:14px 16px}
.setf{display:flex;flex-direction:column;gap:4px}
.setf label{font-size:.7rem;color:var(--m)}
.setf input{background:var(--c);border:1px solid var(--b);color:var(--t);border-radius:6px;
  padding:6px 10px;font-family:'IBM Plex Mono',monospace;font-size:.78rem}
.btn-save{background:var(--a);color:#000;border:none;border-radius:6px;padding:8px 20px;
  font-weight:600;cursor:pointer;font-size:.78rem;margin:0 16px 14px}
</style>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
</head>
<body>
<header>
  <div class="logo">APEX <small>ULTIMATE</small></div>
  <div class="hright">
    <span id="btcRegimeBadge" style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;padding:4px 10px;border-radius:4px;background:rgba(255,209,102,.1);border:1px solid rgba(255,209,102,.3);color:var(--y)">BTC ...</span>
    <span class="net-badge" id="netBadge">🔴 LIVE</span>
    <span style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--m)" id="cfgBadge"></span>
    <button class="btn bstart" id="mainBtn" onclick="toggleBot()">🚀 تشغيل</button>
    <div class="pill"><div class="dot"></div><span id="stxt">متصل</span></div>
  </div>
</header>
<div id="toastBox"></div>

<div class="wrap">
  <!-- Stats Row -->
  <div class="stats">
    <div class="stat" style="--ac:var(--a)"><div class="sl">رصيد USDT</div><div class="sv aa" id="bal">$—</div></div>
    <div class="stat" style="--ac:var(--g)"><div class="sl">إجمالي PnL</div><div class="sv" id="pnl">$0.00</div><div class="ss" id="wr">—</div></div>
    <div class="stat" style="--ac:var(--y)"><div class="sl">PnL اليوم</div><div class="sv" id="dpnl">$0.00</div><div class="ss" id="dpnlpct">0.00%</div></div>
    <div class="stat" style="--ac:var(--p)"><div class="sl">صفقات مفتوحة</div><div class="sv pa" id="oc">0</div></div>
    <div class="stat" style="--ac:var(--g)"><div class="sl">Win / Loss</div><div class="sv"><span class="ga" id="wins">0</span> / <span class="ra" id="losses">0</span></div><div class="ss" id="wrt">—</div></div>
  </div>

  <!-- Market Scanner -->
  <div>
    <div class="sec">ماسح السوق المباشر</div>
    <div class="mgrid" id="mgrid"><div class="mc"><div class="mcs" style="color:var(--m)">جاري التحميل...</div></div></div>
  </div>

  <!-- Weekly Chart + Fibonacci -->
  <div class="card" style="margin-top:18px">
    <div class="ch">
      <span>تحليل أسبوعي: كلاسيك + فيبوناتشي</span>
      <select id="chartSym" style="background:var(--c);color:var(--t);border:1px solid var(--b);border-radius:6px;padding:4px 8px;font-family:'IBM Plex Mono',monospace;font-size:.75rem"></select>
    </div>
    <div id="chartContainer" style="height:420px;width:100%"></div>
    <div id="levelsInfo" style="padding:12px 16px;font-size:.78rem;color:var(--m);display:flex;flex-wrap:wrap;gap:14px"></div>
  </div>

  <!-- Settings Panel -->
  <div class="card" style="margin-top:18px">
    <div class="ch"><span>⚙️ إعدادات البوت</span><span style="font-size:.68rem;color:var(--m)">تُطبَّق فوراً دون إعادة تشغيل</span></div>
    <div class="settings-grid">
      <div class="setf"><label>نسبة المخاطرة %</label><input id="s_risk" type="number" step="0.1" min="0.1" max="5"></div>
      <div class="setf"><label>أقصى صفقات مفتوحة</label><input id="s_maxpos" type="number" step="1" min="1" max="20"></div>
      <div class="setf"><label>الحد الأدنى ADX</label><input id="s_adx" type="number" step="1" min="10" max="50"></div>
      <div class="setf"><label>الحد الأدنى للنقاط</label><input id="s_score" type="number" step="1" min="1"></div>
      <div class="setf"><label>أقصى مدة صفقة (دقيقة)</label><input id="s_hold" type="number" step="60" min="60"></div>
      <div class="setf"><label>حد الخسارة اليومية %</label><input id="s_dloss" type="number" step="0.5" min="0.5" max="20"></div>
      <div class="setf"><label>مضاعف SL (ATR)</label><input id="s_sl" type="number" step="0.1" min="0.5" max="10"></div>
      <div class="setf"><label>مضاعف TP (ATR)</label><input id="s_tp" type="number" step="0.1" min="1" max="20"></div>
    </div>
    <button class="btn-save" onclick="saveSettings()">💾 حفظ الإعدادات</button>
    <span id="settingsFeedback" style="font-family:'IBM Plex Mono',monospace;font-size:.72rem;color:var(--g);margin-left:10px"></span>
  </div>

  <!-- Positions + History -->
  <div class="g2">
    <div class="card">
      <div class="ch"><span>الصفقات المفتوحة</span><span class="pa" style="font-family:'IBM Plex Mono',monospace;font-size:.7rem" id="ocb">0</span></div>
      <table><thead><tr><th>الزوج</th><th>اتجاه</th><th>دخول</th><th>سعر حالي</th><th>SL</th><th>TP</th><th>PnL%</th><th>مدة</th></tr></thead>
      <tbody id="opb"><tr><td colspan="8" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات مفتوحة</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="ch"><span>سجل الصفقات</span><span style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--m)" id="tc">0</span></div>
      <table><thead><tr><th>الزوج</th><th>نوع</th><th>دخول</th><th>خروج</th><th>PnL</th><th>سبب</th><th>وقت</th></tr></thead>
      <tbody id="trb"><tr><td colspan="7" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات بعد</td></tr></tbody></table>
    </div>
  </div>

  <!-- Backtest -->
  <div class="card" id="backtestCard">
    <div class="ch">
      <span>🧪 اختبار الاستراتيجية (Backtest)</span>
      <span style="font-size:.68rem;color:var(--m)">محاكاة على بيانات تاريخية حقيقية من Binance</span>
    </div>
    <div style="padding:16px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end">
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">العملة</div>
        <input id="bt_sym" value="BTCUSDT" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem;width:130px">
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">أيام</div>
        <input id="bt_days" value="180" type="number" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem;width:80px">
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">رصيد ابتدائي $</div>
        <input id="bt_bal" value="1000" type="number" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem;width:100px">
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">فريم</div>
        <select id="bt_ivl" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem">
          <option value="4h" selected>4h</option>
          <option value="1d">1d</option>
          <option value="1h">1h</option>
          <option value="2h">2h</option>
        </select>
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">SL Mult</div>
        <input id="bt_sl" value="2.5" type="number" step="0.1" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem;width:80px">
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">TP Mult</div>
        <input id="bt_tp" value="6.0" type="number" step="0.1" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem;width:80px">
      </div>
      <div>
        <div style="font-size:.65rem;color:var(--m);margin-bottom:4px">Min Score</div>
        <input id="bt_score" value="7" type="number" style="background:var(--bg);border:1px solid var(--b);color:var(--t);padding:7px 10px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:.8rem;width:80px">
      </div>
      <button class="btn bstart" onclick="runBacktest()" style="padding:8px 22px;font-size:.82rem">▶ تشغيل</button>
    </div>

    <!-- نتائج الـ Backtest -->
    <div id="bt_results" style="display:none;padding:0 16px 16px">
      <div id="bt_summary" style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px"></div>
      <div style="font-size:.72rem;color:var(--m);margin-bottom:6px">آخر 50 صفقة في الاختبار</div>
      <div style="max-height:220px;overflow-y:auto">
        <table><thead><tr>
          <th>التاريخ</th><th>دخول</th><th>خروج</th><th>PnL%</th><th>سبب</th><th>مدة(بار)</th>
        </tr></thead>
        <tbody id="bt_trades"></tbody></table>
      </div>
    </div>
    <div id="bt_loading" style="display:none;padding:20px;text-align:center;color:var(--a);font-family:'IBM Plex Mono',monospace;font-size:.8rem">
      ⏳ جارٍ التحليل... قد يستغرق 10-30 ثانية
    </div>
    <div id="bt_error" style="display:none;padding:14px 16px;color:var(--r);font-size:.78rem;font-family:'IBM Plex Mono',monospace"></div>
  </div>

  <!-- Log -->
  <div>
    <div class="sec">سجل النشاط المباشر</div>
    <div class="logbox" id="logbox"></div>
  </div>
</div>

<script>
let running=false;
const es=new EventSource('/events');
es.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='log')addLog(d);else if(d.type==='snapshot')render(d);else if(d.type==='alert')showAlert(d)};
es.onerror=()=>{document.getElementById('stxt').textContent='إعادة الاتصال...'};

function render(d){
  running=d.running;
  const btn=document.getElementById('mainBtn');
  btn.textContent=running?'⏹ إيقاف':'🚀 تشغيل';
  btn.className='btn '+(running?'bstop':'bstart');
  document.getElementById('stxt').textContent=running?'يعمل ⚡':'متوقف';
  document.getElementById('netBadge').textContent=d.net;
  const rb=document.getElementById('btcRegimeBadge');
  if(rb){
    const regime=d.btc_regime||'RANGING';
    const dom=d.btc_dom||'NEUTRAL';
    const rmap={'BULL':['🟢 BTC BULL','rgba(0,255,136,.1)','rgba(0,255,136,.3)','var(--g)'],
                'BEAR':['🔴 BTC BEAR','rgba(255,59,107,.1)','rgba(255,59,107,.3)','var(--r)'],
                'RANGING':['🟡 BTC RANGING','rgba(255,209,102,.1)','rgba(255,209,102,.3)','var(--y)']};
    const dmap={'FALLING':' 📉ALT.S','RISING':' 📈BTC.D↑','NEUTRAL':''};
    const [txt,bg,bdr,clr]=rmap[regime]||rmap['RANGING'];
    rb.textContent=txt+(dmap[dom]||''); rb.style.background=bg; rb.style.borderColor=bdr; rb.style.color=clr;
  }
  if(d.config) document.getElementById('cfgBadge').textContent=`${d.config.interval} | Risk${d.config.risk_pct}% | SL×${d.config.sl_mult} TP×${d.config.tp_mult} | ADX>${d.config.adx_min} | Score≥${d.config.min_score}`;
  if(d.config && d.config.symbols && typeof updateSymbolDropdown==='function') updateSymbolDropdown(d.config.symbols);
  // حد الخسارة اليومية
  const dpnl=document.getElementById('dpnl'); const dpnlpct=document.getElementById('dpnlpct');
  if(dpnl){ dpnl.textContent=(d.daily_pnl>=0?'+':'')+'$'+Math.abs(d.daily_pnl).toFixed(4); dpnl.className='sv '+(d.daily_pnl>=0?'ga':'ra'); }
  if(dpnlpct){ dpnlpct.textContent=(d.daily_pnl_pct>=0?'+':'')+d.daily_pnl_pct.toFixed(2)+'% اليوم'; dpnlpct.style.color=d.daily_pnl>=0?'var(--g)':'var(--r)'; }
  if(d.protected){ document.getElementById('stxt').textContent='🛑 محمي — حد اليوم'; }

  document.getElementById('bal').textContent='$'+d.balance.toFixed(2);
  const pe=document.getElementById('pnl');
  pe.textContent=(d.total_pnl>=0?'+':'')+'$'+Math.abs(d.total_pnl).toFixed(4);
  pe.className='sv '+(d.total_pnl>=0?'ga':'ra');
  document.getElementById('wr').textContent=`Win Rate: ${d.win_rate}%`;
  const _s=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
  _s('oc', d.open_positions.length);
  _s('ocb', d.open_positions.length);
  _s('wd', '$'+d.withdrawn.toFixed(4));
  _s('wins', d.wins);
  _s('losses', d.losses);
  _s('wrt', `${d.wins+d.losses} صفقة`);
  _s('tc', d.trades.length+' صفقة');

  // Market
  try {
    const mg=document.getElementById('mgrid');
    if(d.market&&Object.keys(d.market).length){
      mg.innerHTML=Object.entries(d.market).map(([s,m])=>{
        try {
          const c=m.score>0?'var(--g)':m.score<0?'var(--r)':'var(--y)';
          const hot=m.score>=2;
          const pct=Math.min(100,Math.abs(m.score)/8*100);
          const sigHtml=Object.entries(m.signals||{}).map(([k,v])=>`<span class="sig-item">${k}: ${String(v).replace(/[🟢🔴⚪]/g,'')}</span>`).join('');
          return `<div class="mc ${hot?'hot':''}" onclick="loadChart('${s}');document.getElementById('chartSym').value='${s}';document.getElementById('chartContainer').scrollIntoView({behavior:'smooth'})">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <div class="mcs">${s.replace('USDT','/USDT')}</div>
              <div class="mcsc" style="color:${c}">${m.score>0?'+':''}${m.score}</div>
            </div>
            <div class="mcp">$${(m.price||0).toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:6})}</div>
            <div class="bar"><div class="bf" style="width:${pct}%;background:${c}"></div></div>
            <div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:5px">${sigHtml}</div>
          </div>`;
        } catch(e2){ return ''; }
      }).join('');
    }
  } catch(e){ console.error('market render error:',e); }

  // Open positions
  try {
    const opEl = document.getElementById('opb');
    if (!d.open_positions || !d.open_positions.length) {
      opEl.innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات مفتوحة</td></tr>';
    } else {
      opEl.innerHTML = d.open_positions.map(p=>{
        const pnl = p.pnl_pct || 0;
        const pnlColor = pnl>=0 ? 'var(--g)' : 'var(--r)';
        const cur = p.cur_price || p.entry;
        return `<tr>
        <td style="font-family:'IBM Plex Mono',monospace;font-weight:600">${p.symbol.replace('USDT','')}</td>
        <td><span class="badge ${p.side==='BUY'?'buy':'sell2'}">${p.side==='BUY'?'▲ شراء':'▼ بيع'}</span></td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">$${p.entry}</td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;font-weight:600">$${cur}</td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--r)">$${p.sl}</td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--g)">$${p.tp}</td>
        <td style="font-weight:600;color:${pnlColor}">${pnl>=0?'+':''}${pnl.toFixed(2)}%</td>
        <td style="font-size:.68rem;color:var(--m)">${p.held_min}د</td>
        </tr>`;
      }).join('');
    }
  } catch(e) { console.error('positions render error:', e); }

  // Trades
  document.getElementById('trb').innerHTML=d.trades.length
    ?d.trades.map(t=>`<tr>
      <td style="font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:.73rem">${t.symbol}</td>
      <td><span class="badge ${t.side==='BUY'?'buy':'sell2'}">${t.side==='BUY'?'▲':'▼'}</span></td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">$${t.entry}</td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">$${t.exit}</td>
      <td style="font-family:'IBM Plex Mono',monospace;font-weight:600;color:${t.pnl>=0?'var(--g)':'var(--r)'}">${t.pnl>=0?'+':''}$${Math.abs(t.pnl).toFixed(4)}</td>
      <td style="font-size:.63rem;color:${t.reason&&t.reason.includes('TP')?'var(--g)':t.reason&&t.reason.includes('SL')?'var(--r)':'var(--y)'}">${t.reason||''}</td>
      <td style="font-size:.63rem;color:var(--m)">${t.time}</td>
    </tr>`).join('')
    :'<tr><td colspan="7" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات بعد</td></tr>';
}

function addLog(d){
  const box=document.getElementById('logbox');
  const div=document.createElement('div');
  div.className='le';
  div.innerHTML=`<span class="lt">${d.time}</span><span class="l${d.level[0]}">${d.msg}</span>`;
  box.prepend(div);
  if(box.children.length>200)box.lastChild.remove();
}

async function toggleBot(){
  await fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:running?'stop':'start'})});
}

// ── Weekly Chart + Fibonacci ──
let chart, candleSeries, priceLines=[];
let knownSymbols = new Set();

function initChart(){
  if(chart) return;
  const el = document.getElementById('chartContainer');
  chart = LightweightCharts.createChart(el, {
    layout:{ background:{color:'transparent'}, textColor:'#9aa8c0' },
    grid:{ vertLines:{color:'#1a2840'}, horzLines:{color:'#1a2840'} },
    timeScale:{ timeVisible:false, borderColor:'#1a2840' },
    rightPriceScale:{ borderColor:'#1a2840' },
    width: el.clientWidth, height: 420,
  });
  candleSeries = chart.addCandlestickSeries({
    upColor:'#00ff88', downColor:'#ff3b6b', borderVisible:false,
    wickUpColor:'#00ff88', wickDownColor:'#ff3b6b'
  });
  window.addEventListener('resize', ()=>chart.applyOptions({width: el.clientWidth}));
}

async function loadChart(sym){
  initChart();
  try {
    const r = await fetch('/chart?symbol='+sym);
    const d = await r.json();
    if(!d.candles || !d.candles.length) return;
    candleSeries.setData(d.candles);

    // إزالة الخطوط القديمة
    priceLines.forEach(l=>candleSeries.removePriceLine(l));
    priceLines = [];

    const addLine = (price, color, title) => {
      if(price==null || !isFinite(price)) return;
      priceLines.push(candleSeries.createPriceLine({
        price: price, color: color, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: title
      }));
    };

    const lv = d.levels;
    const infoEl = document.getElementById('levelsInfo');
    if(lv){
      (lv.resistance||[]).forEach((p,i)=>addLine(p, '#ff3b6b', 'R'+(i+1)));
      (lv.support||[]).forEach((p,i)=>addLine(p, '#00ff88', 'S'+(i+1)));
      if(lv.fib){
        addLine(lv.fib['0.618'], '#ffd166', 'Fib 0.618');
        addLine(lv.fib['0.786'], '#ffd166', 'Fib 0.786');
        addLine(lv.fib['0.5'], '#a78bfa', 'Fib 0.5');
      }
      let html = '';
      if(lv.entry_zone) html += `<span class="lvl-tag"><span class="lvl-dot" style="background:#ffd166"></span>منطقة دخول ذهبية: $${lv.entry_zone[0]} - $${lv.entry_zone[1]}</span>`;
      if(lv.tp_zone) html += `<span class="lvl-tag"><span class="lvl-dot" style="background:#00ff88"></span>هدف (مقاومة): $${lv.tp_zone}</span>`;
      html += `<span class="lvl-tag"><span class="lvl-dot" style="background:${lv.in_golden_zone?'#00ff88':'#4a6080'}"></span>${lv.in_golden_zone?'السعر داخل المنطقة الذهبية الآن 🟢':'السعر خارج المنطقة الذهبية'}</span>`;
      infoEl.innerHTML = html;
    } else {
      infoEl.innerHTML = '<span style="color:var(--m)">لا توجد بيانات مستويات</span>';
    }
  } catch(e){ console.error('chart load error:', e); }
}

function updateSymbolDropdown(symbols){
  const sel = document.getElementById('chartSym');
  let changed = false;
  symbols.forEach(s=>{ if(!knownSymbols.has(s)){ knownSymbols.add(s); changed=true; } });
  if(changed || sel.options.length===0){
    const cur = sel.value;
    sel.innerHTML = symbols.map(s=>`<option value="${s}">${s.replace('USDT','/USDT')}</option>`).join('');
    if(cur && symbols.includes(cur)) sel.value = cur;
  }
}

document.getElementById('chartSym').addEventListener('change', e=>loadChart(e.target.value));

function showAlert(d){
  const box=document.getElementById('toastBox');
  const t=document.createElement('div');
  t.className='toast';
  t.innerHTML=`<div style="color:var(--g);font-weight:700">🚀 إشارة قوية: ${d.symbol}</div><div style="margin-top:4px">النقاط: +${d.score} &nbsp;|&nbsp; السعر: $${d.price}</div><div style="margin-top:2px;color:var(--m)">${d.time}</div>`;
  box.appendChild(t);
  if('Notification' in window && Notification.permission==='granted'){
    new Notification('APEX SIGNAL: '+d.symbol, {body:`+${d.score} نقاط @ $${d.price}`, icon:''});
  }
  setTimeout(()=>t.remove(), 8000);
}

function fillSettingsFromConfig(cfg){
  if(!cfg) return;
  const map = {
    s_risk:'risk_pct', s_maxpos:'max_open', s_adx:'adx_min',
    s_score:'min_score', s_hold:'max_hold_minutes',
    s_dloss:'daily_loss_limit', s_sl:'sl_mult', s_tp:'tp_mult'
  };
  for(const [id, key] of Object.entries(map)){
    const el=document.getElementById(id);
    if(el && cfg[key]!=null) el.value=cfg[key];
  }
}

async function saveSettings(){
  const body={
    RISK_PER_TRADE_PCT: parseFloat(document.getElementById('s_risk').value)||undefined,
    MAX_OPEN_POSITIONS: parseInt(document.getElementById('s_maxpos').value)||undefined,
    ADX_MIN: parseFloat(document.getElementById('s_adx').value)||undefined,
    MIN_SIGNAL_SCORE: parseInt(document.getElementById('s_score').value)||undefined,
    MAX_HOLD_MINUTES: parseInt(document.getElementById('s_hold').value)||undefined,
    DAILY_LOSS_LIMIT_PCT: parseFloat(document.getElementById('s_dloss').value)||undefined,
    ATR_SL_MULT: parseFloat(document.getElementById('s_sl').value)||undefined,
    ATR_TP_MULT: parseFloat(document.getElementById('s_tp').value)||undefined,
  };
  Object.keys(body).forEach(k=>body[k]===undefined&&delete body[k]);
  try{
    const r=await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    const fb=document.getElementById('settingsFeedback');
    fb.textContent='✅ تم الحفظ';
    setTimeout(()=>fb.textContent='',3000);
  } catch(e){ document.getElementById('settingsFeedback').textContent='❌ خطأ'; }
}

async function runBacktest(){
  const sym   = document.getElementById('bt_sym').value.trim().toUpperCase();
  const days  = document.getElementById('bt_days').value;
  const bal   = document.getElementById('bt_bal').value;
  const ivl   = document.getElementById('bt_ivl').value;
  const sl    = document.getElementById('bt_sl').value;
  const tp    = document.getElementById('bt_tp').value;
  const score = document.getElementById('bt_score').value;

  document.getElementById('bt_results').style.display='none';
  document.getElementById('bt_error').style.display='none';
  document.getElementById('bt_loading').style.display='block';

  try {
    const url=`/backtest?symbol=${sym}&days=${days}&balance=${bal}&interval=${ivl}&sl_mult=${sl}&tp_mult=${tp}&min_score=${score}`;
    const r = await fetch(url);
    const d = await r.json();
    document.getElementById('bt_loading').style.display='none';

    if(d.error){
      const el=document.getElementById('bt_error');
      el.textContent='❌ '+d.error; el.style.display='block'; return;
    }

    // ملخص النتائج
    const pnlColor = d.net_pnl >= 0 ? 'var(--g)' : 'var(--r)';
    const ddColor  = d.max_drawdown > 20 ? 'var(--r)' : d.max_drawdown > 10 ? 'var(--y)' : 'var(--g)';
    const pfColor  = d.profit_factor >= 1.5 ? 'var(--g)' : d.profit_factor >= 1 ? 'var(--y)' : 'var(--r)';
    document.getElementById('bt_summary').innerHTML = `
      <div class="stat" style="--ac:${pnlColor}">
        <div class="sl">${sym} — ${days} يوم</div>
        <div class="sv" style="color:${pnlColor}">${d.net_pnl>=0?'+':''}${d.net_pnl}$</div>
        <div class="ss">${d.net_pnl_pct>=0?'+':''}${d.net_pnl_pct}% من ${d.start_balance}$</div>
      </div>
      <div class="stat" style="--ac:var(--a)">
        <div class="sl">صفقات / ربح / خسارة</div>
        <div class="sv aa">${d.total_trades}</div>
        <div class="ss"><span style="color:var(--g)">${d.wins}✅</span> / <span style="color:var(--r)">${d.losses}❌</span></div>
      </div>
      <div class="stat" style="--ac:var(--g)">
        <div class="sl">نسبة الربح</div>
        <div class="sv ga">${d.win_rate}%</div>
        <div class="ss">متوسط ربح ${d.avg_win}% / خسارة ${d.avg_loss}%</div>
      </div>
      <div class="stat" style="--ac:${ddColor}">
        <div class="sl">أقصى سحب (Drawdown)</div>
        <div class="sv" style="color:${ddColor}">-${d.max_drawdown}%</div>
        <div class="ss">رصيد نهائي $${d.end_balance}</div>
      </div>
      <div class="stat" style="--ac:${pfColor}">
        <div class="sl">معامل الربح (PF)</div>
        <div class="sv" style="color:${pfColor}">${d.profit_factor}</div>
        <div class="ss">>1.5 ممتاز | >1 مقبول</div>
      </div>`;

    // جدول الصفقات
    document.getElementById('bt_trades').innerHTML = (d.trades||[]).map(t=>{
      const c = t.pnl>=0?'var(--g)':'var(--r)';
      return `<tr>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">${t.time}</td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">${t.entry}</td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">${t.exit}</td>
        <td style="color:${c};font-family:'IBM Plex Mono',monospace;font-size:.7rem;font-weight:600">${t.pnl_pct>=0?'+':''}${t.pnl_pct}%</td>
        <td style="font-size:.68rem">${t.reason}</td>
        <td style="font-family:'IBM Plex Mono',monospace;font-size:.65rem;color:var(--m)">${t.held}</td>
      </tr>`;
    }).join('');

    document.getElementById('bt_results').style.display='block';
  } catch(e){
    document.getElementById('bt_loading').style.display='none';
    const el=document.getElementById('bt_error');
    el.textContent='❌ فشل الاتصال: '+e; el.style.display='block';
  }
}

if('Notification' in window && Notification.permission==='default'){
  Notification.requestPermission();
}

fetch('/snapshot').then(r=>r.json()).then(d=>{
  render(d);
  if(d.config){
    fillSettingsFromConfig(d.config);
    if(d.config.symbols && d.config.symbols.length){
      updateSymbolDropdown(d.config.symbols);
      loadChart(d.config.symbols[0]);
    }
  }
}).catch(()=>{});
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        p=self.path.split('?')[0]
        if p=='/': self._s(200,'text/html; charset=utf-8',HTML.encode())
        elif p=='/snapshot': self._s(200,'application/json',json.dumps(snapshot(),ensure_ascii=False).encode())
        elif p=='/api/top100':
            self._s(200,'application/json',
                    json.dumps(state.get("top100", {}), ensure_ascii=False, default=str).encode())
        elif p=='/chart':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sym = qs.get('symbol',['BTCUSDT'])[0]
            try:
                k = get_klines(sym, "1d", 90)
                candles = [{"time":int(x[0]/1000),"open":float(x[1]),"high":float(x[2]),
                            "low":float(x[3]),"close":float(x[4])} for x in k]
                levels = weekly_levels(sym)
                self._s(200,'application/json',json.dumps({"candles":candles,"levels":levels},ensure_ascii=False).encode())
            except Exception as e:
                self._s(200,'application/json',json.dumps({"candles":[],"levels":None,"error":str(e)}).encode())
        elif p=='/backtest':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sym     = qs.get('symbol',['BTCUSDT'])[0].upper()
            days    = int(qs.get('days',['180'])[0])
            bal     = float(qs.get('balance',['1000'])[0])
            sl_m    = float(qs.get('sl_mult',[str(CONFIG["ATR_SL_MULT"])])[0])
            tp_m    = float(qs.get('tp_mult',[str(CONFIG["ATR_TP_MULT"])])[0])
            min_sc  = int(qs.get('min_score',[str(CONFIG["MIN_SIGNAL_SCORE"])])[0])
            ivl     = qs.get('interval',[CONFIG["KLINE_INTERVAL"]])[0]
            result  = backtest(sym, days=days, start_balance=bal,
                               sl_mult=sl_m, tp_mult=tp_m, min_score=min_sc, interval=ivl)
            self._s(200,'application/json',json.dumps(result,ensure_ascii=False).encode())
        elif p=='/events':
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-cache')
            self.send_header('Access-Control-Allow-Origin','*')
            self.end_headers()
            q=queue.Queue(maxsize=200)
            with sse_lock: sse_clients.append(q)
            try:
                snap=json.dumps({"type":"snapshot",**snapshot()},ensure_ascii=False)
                self.wfile.write(f"data: {snap}\n\n".encode()); self.wfile.flush()
                # إرسال الـ logs الأخيرة
                for entry in list(reversed(state["logs"][:30])):
                    msg=json.dumps({"type":"log",**entry},ensure_ascii=False)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg=q.get(timeout=20)
                        self.wfile.write(msg.encode()); self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n"); self.wfile.flush()
            except:
                with sse_lock:
                    if q in sse_clients: sse_clients.remove(q)
        else: self._s(404,'text/plain',b'Not Found')

    def do_POST(self):
        if self.path=='/control':
            n=int(self.headers.get('Content-Length',0))
            body=json.loads(self.rfile.read(n))
            action=body.get('action')
            if action=='start' and not state['running']:
                state['running']=True
                threading.Thread(target=bot_loop,daemon=True).start()
            elif action=='stop':
                state['running']=False
                log("⏹ تم إيقاف البوت","warn")
            self._s(200,'application/json',b'{"ok":true}')
        elif self.path=='/settings':
            n=int(self.headers.get('Content-Length',0))
            body=json.loads(self.rfile.read(n))
            allowed = {
                "RISK_PER_TRADE_PCT": float, "MAX_OPEN_POSITIONS": int,
                "ADX_MIN": float, "MIN_SIGNAL_SCORE": int,
                "MAX_HOLD_MINUTES": int, "DAILY_LOSS_LIMIT_PCT": float,
                "ATR_SL_MULT": float, "ATR_TP_MULT": float,
            }
            changed = {}
            for k, caster in allowed.items():
                if k in body:
                    try:
                        CONFIG[k] = caster(body[k])
                        changed[k] = CONFIG[k]
                    except: pass
            if changed:
                log(f"⚙️ تحديث إعدادات: {changed}","info")
            self._s(200,'application/json',json.dumps({"ok":True,"changed":changed}).encode())
        else:
            self._s(404,'text/plain',b'Not Found')

    def _s(self,code,ct,body):
        self.send_response(code)
        self.send_header('Content-Type',ct)
        self.send_header('Content-Length',len(body))
        self.end_headers(); self.wfile.write(body)

if __name__=='__main__':
    PORT=8080
    print(f"\033[96m╔══════════════════════════════════════╗\n║    APEX ULTIMATE — جاهز             ║\n╚══════════════════════════════════════╝\033[0m")
    print(f"\033[97m  ▸ افتح:\033[0m \033[92mhttp://localhost:{PORT}\033[0m")
    print(f"\033[93m  ▸ تأكد من وضع API Key في CONFIG\033[0m\n")
    # Auto-start bot loop
    state['running'] = True
    threading.Thread(target=bot_loop, daemon=True).start()

    # ── مسار Top 100: يعمل بجانب حلقة التداول ولا يفتح صفقات بنفسه ──
    if CONFIG.get("TOP100_ENABLED"):
        try:
            sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            from apex_top100.apex_ultimate_hook import install_top100
            install_top100(CONFIG, state, log)
        except Exception as _e:
            log(f"تعذّر تشغيل مسار Top 100: {_e}", "warn")
    try:
        ThreadingHTTPServer(('localhost',PORT),Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n\033[93m▸ تم الإيقاف\033[0m")
