#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║       APEX FUTURES — بوت السكالبينج للعقود          ║
║   فيوتشر Binance | رافعة 5x | Isolated Margin       ║
║   افتح المتصفح على: http://localhost:8081            ║
╚══════════════════════════════════════════════════════╝
"""
import hashlib, hmac, time, json, math, threading, sys, queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tradingview_ta import TA_Handler, Interval as TVInterval
import os  # المفاتيح تُقرأ من متغيرات البيئة ولا تُكتب داخل الكود

TV_INTERVAL = {
    "1m":  TVInterval.INTERVAL_1_MINUTE,
    "5m":  TVInterval.INTERVAL_5_MINUTES,
    "15m": TVInterval.INTERVAL_15_MINUTES,
    "30m": TVInterval.INTERVAL_30_MINUTES,
    "1h":  TVInterval.INTERVAL_1_HOUR,
}
_tv_cache = {}      # {sym_interval: (timestamp, analysis)}
_TV_TTL   = 60      # ثانية — لا نستدعي TradingView أكثر من مرة كل دقيقة لكل عملة

def get_tv(sym, interval_key):
    """جلب تحليل TradingView مع كاش 60 ثانية"""
    key = f"{sym}_{interval_key}"
    cached = _tv_cache.get(key)
    if cached and time.time() - cached[0] < _TV_TTL:
        return cached[1]
    try:
        h = TA_Handler(
            symbol=sym, screener="crypto", exchange="BINANCE",
            interval=TV_INTERVAL.get(interval_key, TVInterval.INTERVAL_1_MINUTE)
        )
        a = h.get_analysis()
        _tv_cache[key] = (time.time(), a)
        return a
    except Exception as e:
        if cached:
            return cached[1]   # أعد القديم إذا فشل الجديد
        return None

# ══════════════════════════════════════════
#  CONFIG — عدّل هنا فقط
# ══════════════════════════════════════════
CONFIG = {
    "API_KEY":    os.getenv("BINANCE_API_KEY", ""),
    "SECRET_KEY": os.getenv("BINANCE_API_SECRET", ""),
    "TESTNET":    False,
    # ── رافعة وهامش ──
    "LEVERAGE":            3,        # 3x — المحترفون: 3-5x للسكالبينج
    "MARGIN_TYPE":         "ISOLATED",
    # ── اختيار العملات ──
    "TOP_N":               5,        # BTC,ETH,SOL,BNB,XRP فقط
    # ── إدارة رأس المال (المعيار الاحترافي: 0.5%) ──
    "RISK_PER_TRADE_PCT":  0.5,      # كان 1% — نصفه لحماية الرصيد
    "MAX_OPEN_POSITIONS":  3,        # المحترفون: 3 كحد أقصى
    # ── SL/TP — نسبة 2:1 كمعيار احترافي ──
    "ATR_SL_MULT":         1.0,      # SL = 1.0 × ATR (أضيق)
    "ATR_TP_MULT":         2.0,      # TP = 2.0 × ATR → نسبة RR = 2:1
    "BREAKEVEN_TRIGGER":   0.5,      # بريك إيفن بعد 50% من TP
    "TRAILING_ATR_MULT":   0.8,      # تريلينج
    # ── فلاتر الجودة (معايير احترافية مشددة) ──
    "MIN_SIGNAL_SCORE":    5,        # 8 مؤشرات — نريد 5 صافي على الأقل
    "MIN_ATR_PCT":         0.03,
    "ADX_MIN":             15,
    "MIN_VOLUME_RATIO":    1.2,
    # ── حماية الرصيد ──
    "DAILY_LOSS_LIMIT_PCT": 2.0,
    "MAX_HOLD_MINUTES":    20,       # وقت كافٍ للوصول لـ TP بنسبة 2:1
    "SL_BLACKLIST_MIN":    30,       # حظر 30 دقيقة بعد SL
    # ── تقني ──
    "KLINE_INTERVAL":      "1m",
    "KLINE_LIMIT":         100,
    "TREND_INTERVAL":      "5m",
    "LOOP_INTERVAL":       5,
    "COOLDOWN":            60,       # كان 45 — انتظر أطول بين الصفقات
}

BASE_URL = "https://testnet.binancefuture.com" if CONFIG["TESTNET"] else "https://fapi.binance.com"

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
    "protected": False,
    "funding_paid": 0.0,
}
sse_clients = []
sse_lock = threading.Lock()
_time_offset = 0
last_trade_time = {}
sl_blacklist = {}
daily_reset_day = -1
leverage_set = set()   # العملات التي تم ضبط الرافعة عليها

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
#  BINANCE FUTURES API
# ══════════════════════════════════════════
def _sign(q): return hmac.new(CONFIG["SECRET_KEY"].encode(), q.encode(), hashlib.sha256).hexdigest()

def _sync_time():
    global _time_offset
    try:
        local = int(time.time()*1000)
        with urlopen(Request(f"{BASE_URL}/fapi/v1/time"), timeout=5) as r:
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
    headers = {"X-MBX-APIKEY": CONFIG["API_KEY"]}
    body = None
    if method == "POST":
        body = query.encode()
        url = f"{BASE_URL}{path}"
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif method == "DELETE":
        url = f"{BASE_URL}{path}?{query}"
        query = ""
    req = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=10) as r: return json.loads(r.read())
    except HTTPError as e:
        body_err = json.loads(e.read())
        raise Exception(f"Binance {e.code}: {body_err.get('msg', str(body_err))}")

def get_balance():
    data = api("GET", "/fapi/v2/balance", signed=True)
    for b in data:
        if b["asset"] == "USDT": return float(b["availableBalance"])
    return 0.0

def get_klines(sym, interval, limit):
    return api("GET", "/fapi/v1/klines", {"symbol":sym,"interval":interval,"limit":limit})

def place_order(sym, side, otype, **kw):
    params = {"symbol":sym,"side":side,"type":otype,**kw}
    return api("POST", "/fapi/v1/order", params, signed=True)

def cancel_all_orders(sym):
    try: api("DELETE", "/fapi/v1/allOpenOrders", {"symbol":sym}, signed=True)
    except: pass

def get_futures_filters(sym):
    info = api("GET", "/fapi/v1/exchangeInfo")
    for s in info["symbols"]:
        if s["symbol"] == sym:
            f = {x["filterType"]:x for x in s["filters"]}
            return {
                "step": float(f.get("LOT_SIZE",{}).get("stepSize","0.001")),
                "minQ": float(f.get("LOT_SIZE",{}).get("minQty","0.001")),
                "minN": float(f.get("MIN_NOTIONAL",{}).get("notional","5")),
                "tick": float(f.get("PRICE_FILTER",{}).get("tickSize","0.01")),
            }
    return {"step":0.001,"minQ":0.001,"minN":5,"tick":0.01}

def set_leverage_and_margin(sym):
    if sym in leverage_set: return
    try:
        api("POST", "/fapi/v1/marginType", {"symbol":sym,"marginType":CONFIG["MARGIN_TYPE"]}, signed=True)
    except Exception as e:
        if "No need to change" not in str(e):
            log(f"marginType {sym}: {e}", "warn")
    try:
        api("POST", "/fapi/v1/leverage", {"symbol":sym,"leverage":CONFIG["LEVERAGE"]}, signed=True)
        leverage_set.add(sym)
    except Exception as e:
        log(f"leverage {sym}: {e}", "warn")

def round_step(qty, step):
    p = max(0, round(-math.log10(step)))
    return round(math.floor(qty/step)*step, p)

# ══════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════
def rsi(closes, n=14):
    if len(closes)<n+1: return 50
    gains, losses = [], []
    for i in range(1, n+1):
        d = closes[-i] - closes[-i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains)/n; al = sum(losses)/n
    if al==0: return 100
    return 100-(100/(1+ag/al))

def ema(closes, n):
    if len(closes)<n: return closes[-1]
    k=2/(n+1); e=sum(closes[:n])/n
    for c in closes[n:]: e=c*k+e*(1-k)
    return e

def macd(closes):
    if len(closes)<35: return 0,0,0,0
    macd_vals=[]
    for i in range(26, len(closes)+1):
        macd_vals.append(ema(closes[:i],12)-ema(closes[:i],26))
    if len(macd_vals)<9: return 0,0,0,0
    signal=ema(macd_vals,9)
    hist=macd_vals[-1]-signal
    momentum = hist - (macd_vals[-2] - ema(macd_vals[:-1],9)) if len(macd_vals)>9 else 0
    return macd_vals[-1], signal, hist, momentum

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
        s = sum(lst[:n]); out = [s]
        for v in lst[n:]: s = s - s/n + v; out.append(s)
        return out
    atr14=smooth(tr_list,n); pdm14=smooth(plus_dm,n); mdm14=smooth(minus_dm,n)
    di_list=[]
    for i in range(len(atr14)):
        if atr14[i]==0: di_list.append(0); continue
        pdi=100*pdm14[i]/atr14[i]; mdi=100*mdm14[i]/atr14[i]
        dx=100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi)>0 else 0
        di_list.append(dx)
    return sum(di_list[-n:])/n if len(di_list)>=n else 0

def vwap(highs, lows, closes, volumes):
    if not volumes or sum(volumes)==0: return closes[-1]
    typ=[(highs[i]+lows[i]+closes[i])/3 for i in range(len(closes))]
    return sum(t*v for t,v in zip(typ,volumes))/sum(volumes)

def get_trend(sym):
    try:
        k=get_klines(sym, CONFIG["TREND_INTERVAL"], 30)
        C=[float(x[4]) for x in k]
        e9=ema(C,9); e21=ema(C,21)
        diff_pct=(e9-e21)/e21*100
        if diff_pct>0.15: return "UP"
        if diff_pct<-0.15: return "DOWN"
        return "NEUTRAL"
    except: return "NEUTRAL"

def analyze(sym):
    """تحليل بمؤشرات محسوبة من Binance — 8 مؤشرات احترافية"""
    try:
        k = get_klines(sym, CONFIG["KLINE_INTERVAL"], CONFIG["KLINE_LIMIT"])
    except: return None
    C=[float(x[4]) for x in k]; H=[float(x[2]) for x in k]
    L=[float(x[3]) for x in k]; V=[float(x[5]) for x in k]
    price=C[-1]; score=0; sigs={}

    # ATR
    a = atr(H,L,C)
    atr_pct = (a/price*100) if price>0 else 0
    if atr_pct < CONFIG["MIN_ATR_PCT"]: return None

    # ADX — فلتر قوة الترند
    adx_val = adx(H,L,C)
    if adx_val < CONFIG["ADX_MIN"]: return None
    sigs["ADX"] = f"{adx_val:.0f} {'💪' if adx_val>25 else ''}"

    # RSI
    r = rsi(C)
    if   r < 35:              score+=2; sigs["RSI"]=f"تشبع بيع {r:.0f} 🟢🟢"
    elif r < 45:              score+=1; sigs["RSI"]=f"منطقة شراء {r:.0f} 🟢"
    elif r > 65:              score-=2; sigs["RSI"]=f"تشبع شراء {r:.0f} 🔴🔴"
    elif r > 55:              score-=1; sigs["RSI"]=f"منطقة بيع {r:.0f} 🔴"
    else:                               sigs["RSI"]=f"محايد {r:.0f} ⚪"

    # MACD
    _,_,m_hist,m_mom = macd(C)
    if   m_hist>0 and m_mom>0: score+=2; sigs["MACD"]="صاعد+زخم 🟢🟢"
    elif m_hist>0:             score+=1; sigs["MACD"]="صاعد 🟢"
    elif m_hist<0 and m_mom<0: score-=2; sigs["MACD"]="هابط+زخم 🔴🔴"
    else:                      score-=1; sigs["MACD"]="هابط 🔴"

    # Bollinger Bands
    bb = bollinger(C)
    if   bb < 15: score+=1; sigs["BB"]=f"تحت الدعم {bb:.0f}% 🟢"
    elif bb > 85: score-=1; sigs["BB"]=f"فوق المقاومة {bb:.0f}% 🔴"
    else:                   sigs["BB"]=f"منتصف {bb:.0f}% ⚪"

    # EMA 20/50
    e20=ema(C,20); e50=ema(C,50)
    if   e20 > e50: score+=1; sigs["EMA"]="EMA20>50 🟢"
    else:           score-=1; sigs["EMA"]="EMA20<50 🔴"

    # VWAP
    vwap_val = vwap(H,L,C,V)
    if   price > vwap_val*1.001: score+=1; sigs["VWAP"]="فوق VWAP 🟢"
    elif price < vwap_val*0.999: score-=1; sigs["VWAP"]="تحت VWAP 🔴"
    else:                                  sigs["VWAP"]="عند VWAP ⚪"

    # Volume — آخر شمعة مكتملة vs متوسط 20
    av = sum(V[-21:-1])/20
    v_ratio = V[-2]/av if av>0 else 1.0
    if   v_ratio>2.0 and C[-1]>C[-2]: score+=1; sigs["VOL"]=f"ضغط شراء {v_ratio:.1f}x 🟢"
    elif v_ratio>2.0 and C[-1]<C[-2]: score-=1; sigs["VOL"]=f"ضغط بيع {v_ratio:.1f}x 🔴"
    else:                                        sigs["VOL"]=f"{v_ratio:.1f}x ⚪"

    # Trend 5m
    trend = get_trend(sym)
    if   trend=="UP":   score+=1; sigs["TREND"]="ترند صاعد 5m 🟢"
    elif trend=="DOWN": score-=1; sigs["TREND"]="ترند هابط 5m 🔴"
    else:                         sigs["TREND"]="محايد 5m ⚪"

    return {
        "symbol":sym, "price":price, "score":score, "signals":sigs,
        "rsi":r, "support":min(L[-20:]), "resistance":max(H[-20:]),
        "atr":a, "atr_pct":atr_pct, "trend":trend,
        "adx":adx_val, "vwap":vwap_val, "v_ratio":v_ratio,
    }

# ══ العملات المسموح بها — أكبر 5 بحجم تداول وسيولة ══
# المحترفون: BTC,ETH,SOL فقط — أضفنا BNB,XRP لزيادة الفرص
SAFE_SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT"]

def fetch_top_symbols(n=5):
    """يتحقق أن العملات الخمس متاحة وبحجم كافٍ"""
    try:
        tickers = api("GET", "/fapi/v1/ticker/24hr")
        tick_map = {t["symbol"]: t for t in tickers}
        available = []
        for sym in SAFE_SYMBOLS:
            t = tick_map.get(sym)
            if not t: continue
            vol = float(t.get("quoteVolume", 0))
            if vol < 200_000_000: continue   # 200M$ كحد أدنى للسيولة
            available.append(sym)
        if not available:
            available = SAFE_SYMBOLS
        log(f"📡 عملات اليوم: {' | '.join(available)}", "success")
        return available
    except Exception as e:
        log(f"فشل جلب العملات: {e}", "warn")
        return SAFE_SYMBOLS

# ══════════════════════════════════════════
#  TRADE ENGINE
# ══════════════════════════════════════════
def open_trade(result):
    sym   = result["symbol"]
    price = result["price"]
    score = result["score"]
    now   = time.time()

    if state.get("protected"): return
    if sym in state["open_positions"]: return
    if now - last_trade_time.get(sym,0) < CONFIG["COOLDOWN"]: return
    if sym in sl_blacklist and now - sl_blacklist[sym] < CONFIG["SL_BLACKLIST_MIN"]*60: return
    if len(state["open_positions"]) >= CONFIG["MAX_OPEN_POSITIONS"]: return

    trend    = result.get("trend","NEUTRAL")
    v_ratio  = result.get("v_ratio", 1.0)
    side     = "BUY" if score > 0 else "SELL"

    # فلاتر احترافية للدخول
    if trend == "NEUTRAL":
        log(f"⏭ {sym} — ترند محايد، لا دخول","info"); return
    if side=="BUY"  and trend=="DOWN":
        log(f"⏭ {sym} — إشارة شراء عكس الترند","info"); return
    if side=="SELL" and trend=="UP":
        log(f"⏭ {sym} — إشارة بيع عكس الترند","info"); return
    if v_ratio < CONFIG["MIN_VOLUME_RATIO"]:
        log(f"⏭ {sym} — حجم منخفض {v_ratio:.1f}x < {CONFIG['MIN_VOLUME_RATIO']}x","info"); return

    a = result.get("atr", price*0.006)
    if a <= 0: return

    log(f"{'─'*45}", "info")
    log(f"إشارة {side} | {sym} | ${price:,.4f} | نقاط:{score:+d}/8 | ADX:{result.get('adx',0):.0f} | ATR:{result.get('atr_pct',0):.2f}%", "signal")

    try:
        # ضبط الرافعة والهامش
        set_leverage_and_margin(sym)

        bal = get_balance()
        if bal < 10: log(f"رصيد غير كافٍ: ${bal:.2f}","warn"); return

        flt = get_futures_filters(sym)
        tk  = flt["tick"]
        pr  = max(0, round(-math.log10(tk)))

        sl_raw = price - a*CONFIG["ATR_SL_MULT"] if side=="BUY" else price + a*CONFIG["ATR_SL_MULT"]
        tp_raw = price + a*CONFIG["ATR_TP_MULT"] if side=="BUY" else price - a*CONFIG["ATR_TP_MULT"]

        sl_dist = abs(price - sl_raw)
        if sl_dist <= 0: return

        # إدارة رأس المال الصحيحة للفيوتشر:
        # loss = qty × sl_dist  →  qty = risk_usdt / sl_dist  (الرافعة لا تدخل هنا)
        # margin_required = qty × price / leverage  →  يجب أن لا تتجاوز 20% من الرصيد
        risk_usdt = bal * (CONFIG["RISK_PER_TRADE_PCT"] / 100)
        qty_by_risk   = risk_usdt / sl_dist
        qty_by_margin = (bal * 0.20 * CONFIG["LEVERAGE"]) / price  # حد أقصى 20% من الرصيد كـ margin
        qty = round_step(min(qty_by_risk, qty_by_margin), flt["step"])
        notional = qty * price
        margin_req = notional / CONFIG["LEVERAGE"]

        if qty < flt["minQ"] or notional < flt["minN"]:
            log(f"كمية صغيرة جداً: {qty} (${notional:.2f})","warn"); return
        if margin_req > bal * 0.95:
            log(f"هامش مطلوب ${margin_req:.2f} > رصيد ${bal:.2f}","warn"); return

        order = place_order(sym, side, "MARKET", quantity=qty)
        fills = order.get("fills", [])
        fp = (sum(float(f["price"])*float(f["qty"]) for f in fills)
              / sum(float(f["qty"]) for f in fills)) if fills else price
        fq = float(order.get("executedQty", qty))
        if fp <= 0: fp = price

        sl_final = round(fp - a*CONFIG["ATR_SL_MULT"] if side=="BUY" else fp + a*CONFIG["ATR_SL_MULT"], pr)
        tp_final = round(fp + a*CONFIG["ATR_TP_MULT"] if side=="BUY" else fp - a*CONFIG["ATR_TP_MULT"], pr)
        be_price = round(fp + a*CONFIG["ATR_TP_MULT"]*CONFIG["BREAKEVEN_TRIGGER"] if side=="BUY"
                         else fp - a*CONFIG["ATR_TP_MULT"]*CONFIG["BREAKEVEN_TRIGGER"], pr)

        sl_pct = abs(fp-sl_final)/fp*100
        tp_pct = abs(fp-tp_final)/fp*100
        lev = CONFIG["LEVERAGE"]
        log(f"✅ {side} {sym} | ${fp:,.4f} | qty:{fq} | رافعة:{lev}x | خطر:${risk_usdt:.2f} | هامش:${margin_req:.2f}","success")
        log(f"🛡️ SL:${sl_final} ({sl_pct:.2f}%) → TP:${tp_final} ({tp_pct:.2f}%) | RR:{tp_pct/sl_pct:.1f}:1","info")

        close_side = "SELL" if side=="BUY" else "BUY"

        # ══ SL إلزامي — 3 محاولات ══
        sl_placed = False
        for attempt in range(3):
            try:
                place_order(sym, close_side, "STOP_MARKET",
                            stopPrice=sl_final, closePosition="true")
                sl_placed = True
                break
            except Exception as e:
                log(f"محاولة SL {attempt+1}/3 فشلت: {e}", "warn")
                time.sleep(0.5)

        if not sl_placed:
            # SL فشل تماماً — أغلق الصفقة فوراً ولا تتركها مكشوفة
            log(f"🚨 SL فشل 3 مرات — إغلاق {sym} فوراً لحماية الرصيد","error")
            try:
                place_order(sym, close_side, "MARKET", quantity=fq, reduceOnly="true")
                log(f"✅ {sym} أُغلقت طارئاً بعد فشل SL","warn")
            except Exception as e2:
                log(f"🚨 فشل الإغلاق الطارئ أيضاً: {e2} — أغلق يدوياً على Binance!","error")
            last_trade_time[sym] = now
            return

        # SL مُوضع — الآن أضف الصفقة للمراقبة
        state["open_positions"][sym] = {
            "side":side, "entry":fp, "qty":fq,
            "sl":sl_final, "tp":tp_final,
            "be_price":be_price, "be_done":False,
            "trail_sl":None,
            "atr":a, "atr_pct":result.get("atr_pct",0),
            "order_id":order.get("orderId"),
            "open_time":now,
            "leverage":lev,
        }
        last_trade_time[sym] = now
        log(f"🛡️ SL مُؤكد على Binance @ ${sl_final}","success")

        # TP اختياري — إذا فشل يراقبه البوت بنفسه
        try:
            place_order(sym, close_side, "TAKE_PROFIT_MARKET",
                        stopPrice=tp_final, closePosition="true")
            log("✅ SL+TP مُفعّلان على Binance Futures","success")
        except Exception as e:
            log(f"تحذير TP: {e} — البوت سيراقب TP بنفسه","warn")

    except Exception as e:
        log(f"فشل الصفقة: {e}","error")

def ensure_sl_exists(sym, pos):
    """تحقق أن STOP_MARKET موجود على Binance لهذه الصفقة — وإلا ضعه فوراً"""
    try:
        orders = api("GET", "/fapi/v1/openOrders", {"symbol": sym}, signed=True)
        sl_exists = any(o.get("type") == "STOP_MARKET" for o in orders)
        if not sl_exists:
            side = pos["side"]
            close_side = "SELL" if side == "BUY" else "BUY"
            sl_price = pos["sl"]
            place_order(sym, close_side, "STOP_MARKET",
                        stopPrice=sl_price, closePosition="true")
            log(f"⚠️ SL مفقود على Binance — أُعيد وضعه لـ {sym} @ ${sl_price}","warn")
    except Exception as e:
        log(f"فشل فحص SL {sym}: {e}","warn")

def check_positions():
    now = time.time()
    for sym in list(state["open_positions"].keys()):
        pos = state["open_positions"].get(sym)
        if not pos: continue
        try:
            k   = get_klines(sym,"1m",3)
            cur = float(k[-1][4])
            cur_high = float(k[-1][2])
            cur_low  = float(k[-1][3])
        except: continue

        side  = pos["side"]
        entry = pos["entry"]
        a     = pos.get("atr", entry*0.006)
        pr    = max(0, round(-math.log10(max(a/1000, 0.00000001))))

        # 0. تأكد أن SL موجود على Binance
        ensure_sl_exists(sym, pos)

        # 1. خروج بالوقت
        held_min = (now - pos["open_time"]) / 60
        if held_min >= CONFIG["MAX_HOLD_MINUTES"]:
            close_position(sym, cur, f"وقت ⏰ {held_min:.0f}د")
            continue

        # 2. بريك إيفن
        if not pos.get("be_done"):
            be_hit = (side=="BUY" and cur>=pos["be_price"]) or (side=="SELL" and cur<=pos["be_price"])
            if be_hit:
                new_sl = round(entry*(1.0008) if side=="BUY" else entry*(0.9992), pr)
                pos["sl"] = new_sl
                pos["be_done"] = True
                log(f"🔒 بريك إيفن {sym} | SL → ${new_sl}","info")

        # 3. تريلينج ستوب
        if pos.get("be_done"):
            trail_sl = pos.get("trail_sl") or pos["sl"]
            if side=="BUY":
                new_trail = round(cur_high - a*CONFIG["TRAILING_ATR_MULT"], pr)
                if new_trail > trail_sl:
                    pos["trail_sl"] = new_trail
                    pos["sl"] = new_trail
            else:
                new_trail = round(cur_low + a*CONFIG["TRAILING_ATR_MULT"], pr)
                if new_trail < trail_sl:
                    pos["trail_sl"] = new_trail
                    pos["sl"] = new_trail

        # 4. فحص TP/SL
        hit_tp = (side=="BUY" and cur>=pos["tp"]) or (side=="SELL" and cur<=pos["tp"])
        hit_sl = (side=="BUY" and cur<=pos["sl"]) or (side=="SELL" and cur>=pos["sl"])

        if hit_tp:   close_position(sym, cur, "TP ✅")
        elif hit_sl: close_position(sym, cur, "SL ❌")

def close_position(sym, cur, reason):
    pos = state["open_positions"].pop(sym, None)
    if not pos: return
    side=pos["side"]; entry=pos["entry"]; qty=pos["qty"]
    held_min = (time.time() - pos["open_time"]) / 60
    lev = pos.get("leverage", CONFIG["LEVERAGE"])

    # PnL مع الرافعة وخصم العمولة
    pnl_pct_raw = ((cur-entry)/entry*100) if side=="BUY" else ((entry-cur)/entry*100)
    fee_pct = 0.1   # 0.05% دخول + 0.05% خروج (taker futures)
    pnl_pct_net = pnl_pct_raw - fee_pct
    pnl = qty * entry * pnl_pct_net / 100  # الربح الفعلي بالـ USDT مع الرافعة

    state["total_pnl"] += pnl
    state["daily_pnl"] += pnl
    if pnl >= 0: state["wins"]   += 1
    else:        state["losses"] += 1

    emoji = "✅" if pnl>=0 else "❌"
    log(f"{emoji} {sym} | {reason} | {held_min:.0f}د | PnL:{pnl:+.4f}$ ({pnl_pct_net:+.2f}%) [{lev}x]","success" if pnl>=0 else "error")

    if "SL" in reason:
        sl_blacklist[sym] = time.time()
        log(f"🚫 {sym} محظور {CONFIG['SL_BLACKLIST_MIN']}د بعد SL","warn")

    try:
        cancel_all_orders(sym)
        cs = "SELL" if side=="BUY" else "BUY"
        place_order(sym, cs, "MARKET", quantity=qty, reduceOnly="true")
    except Exception as e:
        log(f"تحذير إغلاق: {e}","warn")

    state["trades"].append({
        "symbol":sym, "side":side,
        "entry":round(entry,6), "exit":round(cur,6),
        "pnl":round(pnl,4), "pnl_pct":round(pnl_pct_net,2),
        "reason":reason, "held":f"{held_min:.0f}د",
        "leverage":lev,
        "time":datetime.now().strftime("%H:%M:%S")
    })
    if len(state["trades"]) > 200: state["trades"].pop(0)

    # فحص حد الخسارة اليومية
    try:
        daily_loss_pct = abs(state["daily_pnl"]) / max(state["daily_start_balance"],1) * 100
        if state["daily_pnl"] < 0 and daily_loss_pct >= CONFIG["DAILY_LOSS_LIMIT_PCT"]:
            state["protected"] = True
            log(f"🛑 حد الخسارة اليومية! -{daily_loss_pct:.1f}% — البوت متوقف حتى الغد","error")
    except: pass

def sync_open_positions(startup=False):
    """مزامنة الصفقات المفتوحة مع Binance — تُستدعى كل دورة"""
    try:
        data = api("GET", "/fapi/v2/positionRisk", signed=True)
        binance_syms = set()

        for p in data:
            amt = float(p.get("positionAmt", 0))
            if amt == 0: continue
            sym   = p["symbol"]
            entry = float(p["entryPrice"])
            side  = "BUY" if amt > 0 else "SELL"
            qty   = abs(amt)
            cur   = float(p.get("markPrice", entry))
            binance_syms.add(sym)

            if sym not in state["open_positions"]:
                # صفقة على Binance غير موجودة في البوت — أضفها
                a_est  = cur * 0.004
                sl_est = round(cur - a_est*CONFIG["ATR_SL_MULT"]*2 if side=="BUY" else cur + a_est*CONFIG["ATR_SL_MULT"]*2, 4)
                tp_est = round(cur + a_est*CONFIG["ATR_TP_MULT"] if side=="BUY" else cur - a_est*CONFIG["ATR_TP_MULT"], 4)
                state["open_positions"][sym] = {
                    "side": side, "entry": entry, "qty": qty,
                    "sl": sl_est, "tp": tp_est,
                    "be_price": tp_est, "be_done": False,
                    "trail_sl": None, "atr": a_est, "atr_pct": a_est/cur*100,
                    "order_id": None, "open_time": time.time() - 60,
                    "leverage": CONFIG["LEVERAGE"],
                }
                log(f"⚠️ صفقة مجهولة على Binance — تمت إضافتها: {sym} {side} qty:{qty}", "warn")
                # أرسل SL طارئ فوراً
                try:
                    cs = "SELL" if side=="BUY" else "BUY"
                    place_order(sym, cs, "STOP_MARKET", stopPrice=sl_est, closePosition="true")
                    log(f"🛡️ SL طارئ وُضع على {sym} @ ${sl_est}", "warn")
                except Exception as e:
                    log(f"فشل SL طارئ {sym}: {e}", "error")

        # أزل من البوت أي صفقة أغلقها Binance
        for sym in list(state["open_positions"].keys()):
            if sym not in binance_syms:
                pos = state["open_positions"].pop(sym)
                log(f"🔄 {sym} أُغلقت على Binance (SL/TP نُفّذ)", "info")
                pnl_est = 0
                try:
                    cur_p = float(api("GET","/fapi/v1/ticker/price",{"symbol":sym})["price"])
                    side  = pos["side"]; entry = pos["entry"]; qty = pos["qty"]
                    pnl_est = qty*entry*((cur_p-entry)/entry if side=="BUY" else (entry-cur_p)/entry)
                    state["total_pnl"] += pnl_est
                    state["daily_pnl"] += pnl_est
                    if pnl_est >= 0: state["wins"] += 1
                    else:            state["losses"] += 1
                    log(f"{'✅' if pnl_est>=0 else '❌'} {sym} | Binance أغلقها | PnL:{pnl_est:+.4f}$","success" if pnl_est>=0 else "error")
                except: pass

        if startup:
            log(f"✅ مزامنة Binance: {len(binance_syms)} صفقة مفتوحة", "info")
    except Exception as e:
        log(f"تحذير مزامنة الصفقات: {e}", "warn")

def bot_loop():
    global daily_reset_day
    log(f"🚀 APEX FUTURES بدأ | رافعة:{CONFIG['LEVERAGE']}x | {CONFIG['MARGIN_TYPE']}","success")
    _sync_time()
    try:
        bal = get_balance()
        state["balance"] = bal
        state["daily_start_balance"] = bal
        log(f"💼 رصيد: ${bal:.2f} USDT | قوة شراء: ${bal*CONFIG['LEVERAGE']:.2f}","success")
    except Exception as e:
        log(f"فشل الرصيد: {e}","error")

    sync_open_positions(startup=True)
    CONFIG["SYMBOLS"] = fetch_top_symbols(CONFIG["TOP_N"])
    last_refresh = time.time()
    cycle = 0

    while state["running"]:
        cycle += 1

        today = datetime.now().day
        if today != daily_reset_day:
            daily_reset_day = today
            state["daily_pnl"] = 0.0
            state["protected"] = False
            leverage_set.clear()
            try: state["daily_start_balance"] = get_balance()
            except: pass
            log(f"🌅 يوم جديد | رصيد:${state['daily_start_balance']:.2f}","info")

        if state.get("protected"):
            if state["open_positions"]: check_positions()
            time.sleep(CONFIG["LOOP_INTERVAL"]); continue

        if time.time() - last_refresh > 900:
            CONFIG["SYMBOLS"] = fetch_top_symbols(CONFIG["TOP_N"])
            last_refresh = time.time()

        try:
            # مزامنة مع Binance أولاً — تكتشف صفقات مفقودة أو مُغلقة
            sync_open_positions()

            if state["open_positions"]: check_positions()

            candidates = []
            syms = list(CONFIG["SYMBOLS"])
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures_map = {ex.submit(analyze, sym): sym for sym in syms}
                for fut in as_completed(futures_map):
                    r = fut.result()
                    if not r: continue
                    state["market"][r["symbol"]] = {
                        "price":r["price"],"score":r["score"],
                        "signals":r["signals"],"rsi":r["rsi"]
                    }
                    if abs(r["score"]) >= CONFIG["MIN_SIGNAL_SCORE"]:
                        candidates.append(r)

            candidates.sort(key=lambda x: abs(x["score"]), reverse=True)
            for r in candidates:
                if len(state["open_positions"]) >= CONFIG["MAX_OPEN_POSITIONS"]: break
                d = "شراء 🟢" if r["score"]>0 else "بيع 🔴"
                log(f"فرصة: {r['symbol']} {d} نقاط:{r['score']:+d}/8 ADX:{r.get('adx',0):.0f}","signal")
                open_trade(r)

            if not candidates:
                log(f"لا إشارات (score≥{CONFIG['MIN_SIGNAL_SCORE']}) | {len(syms)} عملة","info")

            try: state["balance"] = get_balance()
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
        "running":      state["running"],
        "protected":    state.get("protected",False),
        "balance":      round(state["balance"],2),
        "buying_power": round(state["balance"]*CONFIG["LEVERAGE"],2),
        "total_pnl":    round(state["total_pnl"],4),
        "daily_pnl":    round(state.get("daily_pnl",0),4),
        "daily_pnl_pct":round(state.get("daily_pnl",0)/db*100,2),
        "wins":  state["wins"], "losses": state["losses"],
        "win_rate": round(state["wins"]/t*100,1) if t else 0,
        "blacklisted": list(sl_blacklist.keys()),
        "leverage": CONFIG["LEVERAGE"],
        "margin_type": CONFIG["MARGIN_TYPE"],
        "open_positions":[
            {"symbol":k,"side":v["side"],
             "entry":round(v["entry"],6),"qty":round(v["qty"],6),
             "sl":round(v["sl"],6),"tp":round(v["tp"],6),
             "be_done":v.get("be_done",False),
             "trail_sl":v.get("trail_sl"),
             "atr_pct":round(v.get("atr_pct",0),3),
             "leverage":v.get("leverage",CONFIG["LEVERAGE"]),
             "held_min":round((time.time()-v["open_time"])/60,1)}
            for k,v in state["open_positions"].items()
        ],
        "trades": list(reversed(state["trades"][-20:])),
        "market": state["market"],
        "net": "TESTNET 🧪" if CONFIG["TESTNET"] else "LIVE FUTURES 🔴",
        "config": {
            "symbols":    CONFIG["SYMBOLS"],
            "leverage":   CONFIG["LEVERAGE"],
            "margin":     CONFIG["MARGIN_TYPE"],
            "risk_pct":   CONFIG["RISK_PER_TRADE_PCT"],
            "sl_mult":    CONFIG["ATR_SL_MULT"],
            "tp_mult":    CONFIG["ATR_TP_MULT"],
            "interval":   CONFIG["KLINE_INTERVAL"],
            "max_pos":    CONFIG["MAX_OPEN_POSITIONS"],
            "min_score":  CONFIG["MIN_SIGNAL_SCORE"],
            "adx_min":    CONFIG["ADX_MIN"],
            "daily_limit":CONFIG["DAILY_LOSS_LIMIT_PCT"],
        }
    }

# ══════════════════════════════════════════
#  WEB DASHBOARD
# ══════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX FUTURES</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Tajawal:wght@400;700&display=swap');
:root{--bg:#050810;--s:#0b101b;--c:#101825;--b:#1a2840;--a:#00d4ff;--g:#00ff88;--r:#ff3b6b;--y:#ffd166;--p:#a78bfa;--o:#ff9500;--t:#e2e8f0;--m:#4a6080}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Tajawal',sans-serif;min-height:100vh}
body::before{content:'';position:fixed;top:-20vh;left:50%;transform:translateX(-50%);width:70vw;height:50vh;background:radial-gradient(ellipse,rgba(255,149,0,.04) 0%,transparent 70%);pointer-events:none}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 26px;border-bottom:1px solid var(--b);background:rgba(11,16,27,.95);position:sticky;top:0;z-index:99;backdrop-filter:blur(10px)}
.logo{font-family:'IBM Plex Mono',monospace;font-size:1.1rem;font-weight:600;color:var(--o);letter-spacing:.12em}
.logo small{color:var(--t);opacity:.3;font-size:.75rem}
.hright{display:flex;align-items:center;gap:10px}
.net-badge{font-family:'IBM Plex Mono',monospace;font-size:.68rem;padding:4px 10px;border-radius:4px;background:rgba(255,149,0,.1);border:1px solid rgba(255,149,0,.3);color:var(--o)}
.lev-badge{font-family:'IBM Plex Mono',monospace;font-size:.75rem;padding:5px 12px;border-radius:6px;background:rgba(255,149,0,.15);border:1px solid rgba(255,149,0,.4);color:var(--o);font-weight:600}
.pill{display:flex;align-items:center;gap:7px;padding:5px 14px;border-radius:50px;font-family:'IBM Plex Mono',monospace;font-size:.7rem;border:1px solid rgba(0,255,136,.2);color:var(--g)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--g);animation:p 1.5s infinite}
@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(0,255,136,.4)}50%{box-shadow:0 0 0 5px rgba(0,255,136,0)}}
.wrap{padding:18px 22px;display:flex;flex-direction:column;gap:16px}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.stat{background:var(--c);border:1px solid var(--b);border-radius:10px;padding:15px;border-top:2px solid var(--ac,var(--a))}
.sl{font-size:.68rem;color:var(--m);margin-bottom:4px}
.sv{font-family:'IBM Plex Mono',monospace;font-size:1.2rem;font-weight:600}
.ss{font-size:.65rem;color:var(--m);margin-top:3px}
.ga{color:var(--g)}.ra{color:var(--r)}.aa{color:var(--a)}.ya{color:var(--y)}.pa{color:var(--p)}.oa{color:var(--o)}
.mgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.mc{background:var(--c);border:1px solid var(--b);border-radius:8px;padding:11px;transition:border .2s}
.mc.hot{border-color:rgba(0,255,136,.35)}.mc.sell{border-color:rgba(255,59,107,.25)}
.mcs{font-family:'IBM Plex Mono',monospace;font-size:.8rem;font-weight:600;margin-bottom:2px}
.mcp{font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--m)}
.mcsc{font-size:.72rem;margin-top:5px;font-family:'IBM Plex Mono',monospace}
.bar{height:3px;background:var(--b);border-radius:2px;overflow:hidden;margin-top:5px}
.bf{height:100%;border-radius:2px;transition:width .5s}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.card{background:var(--c);border:1px solid var(--b);border-radius:10px;overflow:hidden}
.ch{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--b);font-size:.82rem;font-weight:500}
table{width:100%;border-collapse:collapse}
th{padding:8px 14px;text-align:right;font-size:.65rem;color:var(--m);font-family:'IBM Plex Mono',monospace;border-bottom:1px solid var(--b);font-weight:400;letter-spacing:.06em}
td{padding:9px 14px;font-size:.76rem;border-bottom:1px solid rgba(26,40,64,.4)}
tr:last-child td{border:none}
tr:hover td{background:rgba(255,255,255,.015)}
.badge{font-size:.62rem;padding:2px 7px;border-radius:3px;font-family:'IBM Plex Mono',monospace}
.buy{background:rgba(0,255,136,.1);color:var(--g)}.sell2{background:rgba(255,59,107,.1);color:var(--r)}
.logbox{background:var(--bg);border:1px solid var(--b);border-radius:8px;padding:11px;height:190px;overflow-y:auto;font-family:'IBM Plex Mono',monospace;font-size:.68rem;line-height:1.9}
.le{display:flex;gap:9px}
.lt{color:var(--m);flex-shrink:0}
.li{color:var(--a)}.ls{color:var(--g)}.lw{color:var(--y)}.lr{color:var(--r)}.ltd{color:var(--p)}.lsi{color:#f9a8d4}
.btn{padding:9px 20px;border-radius:7px;border:none;font-weight:700;font-size:.85rem;cursor:pointer;transition:all .2s;font-family:'Tajawal',sans-serif}
.bstart{background:linear-gradient(135deg,#ff9500,#ff5500);color:#000;box-shadow:0 0 18px rgba(255,149,0,.3)}
.bstop{background:linear-gradient(135deg,#ff3b6b,#ff6b00);color:#fff}
.btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--b);border-radius:2px}
.sec{font-size:.78rem;font-weight:500;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.sec::before{content:'';width:3px;height:13px;background:var(--o);border-radius:2px}
</style>
</head>
<body>
<header>
  <div class="logo">APEX <small>FUTURES</small></div>
  <div class="hright">
    <span class="net-badge" id="netBadge">🔴 FUTURES</span>
    <span class="lev-badge" id="levBadge">5x ISOLATED</span>
    <span style="font-family:'IBM Plex Mono',monospace;font-size:.65rem;color:var(--m)" id="cfgBadge"></span>
    <button class="btn bstart" id="mainBtn" onclick="toggleBot()">🚀 تشغيل</button>
    <div class="pill"><div class="dot"></div><span id="stxt">متصل</span></div>
  </div>
</header>

<div class="wrap">
  <div class="stats">
    <div class="stat" style="--ac:var(--a)"><div class="sl">رصيد USDT</div><div class="sv aa" id="bal">$—</div><div class="ss oa" id="bp">قوة شراء: $—</div></div>
    <div class="stat" style="--ac:var(--o)"><div class="sl">رافعة مالية</div><div class="sv oa" id="lev">5x</div><div class="ss" id="mtype">ISOLATED</div></div>
    <div class="stat" style="--ac:var(--g)"><div class="sl">إجمالي PnL</div><div class="sv" id="pnl">$0.00</div><div class="ss" id="wr">Win: 0%</div></div>
    <div class="stat" style="--ac:var(--y)"><div class="sl">PnL اليوم</div><div class="sv" id="dpnl">$0.00</div><div class="ss" id="dpnlpct">0.00%</div></div>
    <div class="stat" style="--ac:var(--p)"><div class="sl">مفتوحة</div><div class="sv pa" id="oc">0</div></div>
    <div class="stat" style="--ac:var(--g)"><div class="sl">Win / Loss</div><div class="sv"><span class="ga" id="wins">0</span> / <span class="ra" id="losses">0</span></div><div class="ss" id="wrt">0 صفقة</div></div>
  </div>

  <div>
    <div class="sec">ماسح السوق — فيوتشر</div>
    <div class="mgrid" id="mgrid"><div class="mc"><div class="mcs" style="color:var(--m)">جاري التحميل...</div></div></div>
  </div>

  <div class="g2">
    <div class="card">
      <div class="ch"><span>الصفقات المفتوحة</span><span class="pa" style="font-family:'IBM Plex Mono',monospace;font-size:.7rem" id="ocb">0</span></div>
      <table><thead><tr><th>الزوج</th><th>اتجاه</th><th>دخول</th><th>SL</th><th>TP</th><th>رافعة</th><th>مدة</th><th>حالة</th></tr></thead>
      <tbody id="opb"><tr><td colspan="8" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="ch"><span>سجل الصفقات</span><span style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--m)" id="tc">0</span></div>
      <table><thead><tr><th>الزوج</th><th>نوع</th><th>دخول</th><th>خروج</th><th>PnL</th><th>رافعة</th><th>سبب</th><th>وقت</th></tr></thead>
      <tbody id="trb"><tr><td colspan="8" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات بعد</td></tr></tbody></table>
    </div>
  </div>

  <div>
    <div class="sec">سجل النشاط</div>
    <div class="logbox" id="logbox"></div>
  </div>
</div>

<script>
let running=false;
const es=new EventSource('/events');
es.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='log')addLog(d);else if(d.type==='snapshot')render(d)};
es.onerror=()=>{document.getElementById('stxt').textContent='إعادة الاتصال...'};

function render(d){
  running=d.running;
  const btn=document.getElementById('mainBtn');
  btn.textContent=running?'⏹ إيقاف':'🚀 تشغيل';
  btn.className='btn '+(running?'bstop':'bstart');
  document.getElementById('stxt').textContent=running?'يعمل ⚡':'متوقف';
  document.getElementById('netBadge').textContent=d.net||'FUTURES 🔴';
  if(d.leverage) document.getElementById('levBadge').textContent=d.leverage+'x '+(d.margin_type||'ISOLATED');
  if(d.config){
    document.getElementById('cfgBadge').textContent=
      `${d.config.interval} | Risk${d.config.risk_pct}% | SL×${d.config.sl_mult} TP×${d.config.tp_mult} | Score≥${d.config.min_score}/8`;
    document.getElementById('lev').textContent=d.config.leverage+'x';
    document.getElementById('mtype').textContent=d.config.margin;
  }
  document.getElementById('bal').textContent='$'+d.balance.toFixed(2);
  if(d.buying_power) document.getElementById('bp').textContent='قوة شراء: $'+d.buying_power.toFixed(2);
  const pe=document.getElementById('pnl');
  pe.textContent=(d.total_pnl>=0?'+':'')+'$'+Math.abs(d.total_pnl).toFixed(4);
  pe.className='sv '+(d.total_pnl>=0?'ga':'ra');
  document.getElementById('wr').textContent='Win Rate: '+d.win_rate+'%';
  const dpnl=document.getElementById('dpnl');
  dpnl.textContent=(d.daily_pnl>=0?'+':'')+'$'+Math.abs(d.daily_pnl).toFixed(4);
  dpnl.className='sv '+(d.daily_pnl>=0?'ga':'ra');
  document.getElementById('dpnlpct').textContent=(d.daily_pnl_pct>=0?'+':'')+d.daily_pnl_pct.toFixed(2)+'% اليوم';
  document.getElementById('oc').textContent=d.open_positions.length;
  document.getElementById('ocb').textContent=d.open_positions.length;
  document.getElementById('wins').textContent=d.wins;
  document.getElementById('losses').textContent=d.losses;
  document.getElementById('wrt').textContent=(d.wins+d.losses)+' صفقة';
  document.getElementById('tc').textContent=d.trades.length+' صفقة';
  if(d.protected) document.getElementById('stxt').textContent='🛑 محمي';

  const mg=document.getElementById('mgrid');
  if(d.market&&Object.keys(d.market).length){
    mg.innerHTML=Object.entries(d.market).map(([s,m])=>{
      const c=m.score>0?'var(--g)':m.score<0?'var(--r)':'var(--y)';
      const pct=Math.min(100,Math.abs(m.score)/8*100);
      const sigHtml=Object.entries(m.signals||{}).map(([k,v])=>`<span style="font-size:.6rem;padding:2px 5px;border-radius:3px;border:1px solid var(--b);background:var(--s);font-family:'IBM Plex Mono',monospace">${k}:${v.replace(/[🟢🔴]/g,'').trim()}</span>`).join('');
      return `<div class="mc ${m.score>=4?'hot':''} ${m.score<=-4?'sell':''}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div class="mcs">${s.replace('USDT','/USDT')}</div>
          <div class="mcsc" style="color:${c}">${m.score>0?'+':''}${m.score}/8</div>
        </div>
        <div class="mcp">$${m.price.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
        <div class="bar"><div class="bf" style="width:${pct}%;background:${c}"></div></div>
        <div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:5px">${sigHtml}</div>
      </div>`;
    }).join('');
  }

  document.getElementById('opb').innerHTML=d.open_positions.length
    ?d.open_positions.map(p=>{
      const status=p.be_done?(p.trail_sl?'تريلينج 📈':'بريك إيفن 🔒'):'مفتوح';
      return `<tr>
      <td style="font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:.73rem">${p.symbol}</td>
      <td><span class="badge ${p.side==='BUY'?'buy':'sell2'}">${p.side==='BUY'?'LONG':'SHORT'}</span></td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">$${p.entry}</td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--r)">$${p.sl}</td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem;color:var(--g)">$${p.tp}</td>
      <td style="font-size:.68rem;color:var(--o);font-family:'IBM Plex Mono',monospace">${p.leverage}x</td>
      <td style="font-size:.68rem;color:var(--m)">${p.held_min}د</td>
      <td style="font-size:.65rem;color:var(--a)">${status}</td>
    </tr>`}).join('')
    :'<tr><td colspan="8" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات</td></tr>';

  document.getElementById('trb').innerHTML=d.trades.length
    ?d.trades.map(t=>`<tr>
      <td style="font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:.73rem">${t.symbol}</td>
      <td><span class="badge ${t.side==='BUY'?'buy':'sell2'}">${t.side==='BUY'?'LONG':'SHORT'}</span></td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">$${t.entry}</td>
      <td style="font-family:'IBM Plex Mono',monospace;font-size:.68rem">$${t.exit}</td>
      <td style="font-family:'IBM Plex Mono',monospace;font-weight:600;color:${t.pnl>=0?'var(--g)':'var(--r)'}">${t.pnl>=0?'+':''}$${Math.abs(t.pnl).toFixed(4)}</td>
      <td style="font-size:.68rem;color:var(--o)">${t.leverage||5}x</td>
      <td style="font-size:.63rem;color:${t.reason&&t.reason.includes('TP')?'var(--g)':t.reason&&t.reason.includes('SL')?'var(--r)':'var(--y)'}">${t.reason||''}</td>
      <td style="font-size:.63rem;color:var(--m)">${t.time}</td>
    </tr>`).join('')
    :'<tr><td colspan="8" style="text-align:center;color:var(--m);padding:18px;font-size:.75rem">لا توجد صفقات بعد</td></tr>';
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

fetch('/snapshot').then(r=>r.json()).then(render).catch(()=>{});
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        p=self.path.split('?')[0]
        if p=='/': self._s(200,'text/html; charset=utf-8',HTML.encode())
        elif p=='/snapshot': self._s(200,'application/json',json.dumps(snapshot(),ensure_ascii=False).encode())
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

    def _s(self,code,ct,body):
        self.send_response(code)
        self.send_header('Content-Type',ct)
        self.send_header('Content-Length',len(body))
        self.end_headers(); self.wfile.write(body)

if __name__=='__main__':
    PORT=8081
    print(f"\033[93m╔══════════════════════════════════════════╗\n║     APEX FUTURES — جاهز للتداول         ║\n║  رافعة {CONFIG['LEVERAGE']}x | {CONFIG['MARGIN_TYPE']} | Risk {CONFIG['RISK_PER_TRADE_PCT']}%/صفقة    ║\n╚══════════════════════════════════════════╝\033[0m")
    print(f"\033[97m  ▸ افتح:\033[0m \033[92mhttp://localhost:{PORT}\033[0m")
    print(f"\033[91m  ▸ تحذير: الفيوتشر بالرافعة يضاعف الأرباح والخسائر\033[0m\n")
    try:
        ThreadingHTTPServer(('localhost',PORT),Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n\033[93m▸ تم الإيقاف\033[0m")
