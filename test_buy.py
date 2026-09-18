"""
اختبار شامل لمعرفة سبب عدم الشراء في بوت الفيوتشر
"""
import hashlib, hmac, time, json, math
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import os  # المفاتيح تُقرأ من متغيرات البيئة ولا تُكتب داخل الكود

API_KEY = os.getenv("BINANCE_API_KEY", "")
SECRET  = os.getenv("BINANCE_API_SECRET", "")
BASE    = "https://fapi.binance.com"

# ── مزامنة الوقت ──
with urlopen(Request(BASE + "/fapi/v1/time"), timeout=5) as r:
    srv = json.loads(r.read())["serverTime"]
offset = srv - int(time.time()*1000)

def now(): return int(time.time()*1000) + offset
def sign(q): return hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

def api(path, params=None, signed=False):
    params = params or {}
    if signed:
        params["timestamp"] = now()
        q = "&".join(f"{k}={v}" for k,v in params.items())
        params["signature"] = sign(q)
    from urllib.parse import urlencode
    query = urlencode(params)
    url = f"{BASE}{path}?{query}" if query else f"{BASE}{path}"
    req = Request(url, headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urlopen(req, timeout=10) as r: return json.loads(r.read())
    except HTTPError as e:
        return {"ERROR": json.loads(e.read())}

def ema(closes, n):
    if len(closes)<n: return closes[-1]
    k=2/(n+1); e=sum(closes[:n])/n
    for c in closes[n:]: e=c*k+e*(1-k)
    return e

def rsi(closes, n=14):
    if len(closes)<n+1: return 50
    gains, losses = [], []
    for i in range(1, n+1):
        d = closes[-i] - closes[-i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    if al==0: return 100
    return 100-(100/(1+ag/al))

def macd(closes):
    if len(closes)<35: return 0,0,0,0
    mv=[]
    for i in range(26,len(closes)+1):
        mv.append(ema(closes[:i],12)-ema(closes[:i],26))
    if len(mv)<9: return 0,0,0,0
    sig=ema(mv,9); hist=mv[-1]-sig
    mom=hist-(mv[-2]-ema(mv[:-1],9)) if len(mv)>9 else 0
    return mv[-1],sig,hist,mom

def atr(H,L,C,n=14):
    if len(C)<n+2: return 0
    trs=[]
    for i in range(1,len(C)):
        tr=max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1]))
        trs.append(tr)
    return sum(trs[-n:])/n

def bollinger(closes,n=20):
    if len(closes)<n: return 50
    sl=closes[-n:]; mid=sum(sl)/n
    std=math.sqrt(sum((v-mid)**2 for v in sl)/n)
    up=mid+2*std; lo=mid-2*std
    return ((closes[-1]-lo)/(up-lo)*100) if up!=lo else 50

def adx(H,L,C,n=14):
    if len(C)<n+2: return 0
    pdm,mdm,trl=[],[],[]
    for i in range(1,len(C)):
        hd=H[i]-H[i-1]; ld=L[i-1]-L[i]
        pdm.append(hd if hd>ld and hd>0 else 0)
        mdm.append(ld if ld>hd and ld>0 else 0)
        trl.append(max(H[i]-L[i],abs(H[i]-C[i-1]),abs(L[i]-C[i-1])))
    def sm(lst,n):
        s=sum(lst[:n]); out=[s]
        for v in lst[n:]: s=s-s/n+v; out.append(s)
        return out
    a14=sm(trl,n); p14=sm(pdm,n); m14=sm(mdm,n)
    di=[]
    for i in range(len(a14)):
        if a14[i]==0: di.append(0); continue
        pdi=100*p14[i]/a14[i]; mdi=100*m14[i]/a14[i]
        dx=100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi)>0 else 0
        di.append(dx)
    return sum(di[-n:])/n if len(di)>=n else 0

def vwap(H,L,C,V):
    if not V or sum(V)==0: return C[-1]
    typ=[(H[i]+L[i]+C[i])/3 for i in range(len(C))]
    return sum(t*v for t,v in zip(typ,V))/sum(V)

# ════════════════════════════════════════
print("="*55)
print("  اختبار شامل — APEX FUTURES")
print("="*55)

# 1. الرصيد
print("\n[1] رصيد الفيوتشر:")
bal_data = api("/fapi/v2/balance", signed=True)
if "ERROR" in bal_data:
    print("   ❌ خطأ:", bal_data["ERROR"])
else:
    for b in bal_data:
        if b["asset"] == "USDT":
            bal = float(b["availableBalance"])
            print(f"   USDT Available: ${bal:.2f}")
            print(f"   USDT Balance:   ${float(b['balance']):.2f}")

# 2. أذونات الـ API
print("\n[2] أذونات API:")
acc = api("/fapi/v2/account", signed=True)
if "ERROR" in acc:
    print("   ❌ خطأ:", acc["ERROR"])
else:
    print(f"   canTrade: {acc.get('canTrade')}")
    print(f"   canDeposit: {acc.get('canDeposit')}")
    print(f"   totalWalletBalance: ${float(acc.get('totalWalletBalance',0)):.2f}")

# 3. اختبار تحليل 5 عملات وتشخيص سبب الرفض
print("\n[3] تحليل العملات (سبب الرفض لكل عملة):")
SYMS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT"]
MIN_SCORE = 4
ADX_MIN   = 15
MIN_ATR   = 0.2

passed = []
for sym in SYMS:
    k = api("/fapi/v1/klines", {"symbol":sym,"interval":"1m","limit":100})
    if "ERROR" in k or not k:
        print(f"   {sym}: ❌ فشل جلب البيانات")
        continue
    C=[float(x[4]) for x in k]
    H=[float(x[2]) for x in k]
    L=[float(x[3]) for x in k]
    V=[float(x[5]) for x in k]
    price=C[-1]

    a=atr(H,L,C); atr_pct=(a/price*100) if price>0 else 0
    if atr_pct < MIN_ATR:
        print(f"   {sym}: ❌ ATR منخفض {atr_pct:.3f}% < {MIN_ATR}%")
        continue

    adx_val=adx(H,L,C)
    if adx_val < ADX_MIN:
        print(f"   {sym}: ❌ ADX منخفض {adx_val:.1f} < {ADX_MIN}")
        continue

    r=rsi(C)
    _,_,hist,mom=macd(C)
    bb=bollinger(C)
    e9=ema(C,9); e21=ema(C,21)
    avg_v=sum(V[-20:])/20; vr=V[-1]/avg_v if avg_v>0 else 1
    chg=((C[-1]-C[-2])/C[-2])*100 if C[-2]>0 else 0
    vwap_val=vwap(H,L,C,V)

    score=0
    if r<40: score+=1
    elif r>60: score-=1
    if r<35: score+=1
    elif r>65: score-=1
    if hist>0 and mom>0: score+=2
    elif hist>0: score+=1
    elif hist<0 and mom<0: score-=2
    else: score-=1
    if bb<15: score+=1
    elif bb>85: score-=1
    if e9>e21: score+=1
    else: score-=1
    if vr>2 and chg>0: score+=1
    elif vr>2 and chg<0: score-=1
    if price>vwap_val*1.001: score+=1
    elif price<vwap_val*0.999: score-=1

    # trend 5m
    k5=api("/fapi/v1/klines",{"symbol":sym,"interval":"5m","limit":30})
    trend="NEUTRAL"
    if k5 and "ERROR" not in k5:
        C5=[float(x[4]) for x in k5]
        d=(ema(C5,9)-ema(C5,21))/ema(C5,21)*100
        if d>0.15: trend="UP"
        elif d<-0.15: trend="DOWN"
    if trend=="UP": score+=1
    elif trend=="DOWN": score-=1

    side="BUY" if score>0 else "SELL"
    trend_block = (side=="BUY" and trend=="DOWN") or (side=="SELL" and trend=="UP")

    status = "✅ مؤهل" if abs(score)>=MIN_SCORE and not trend_block else "❌ مرفوض"
    reason = ""
    if abs(score)<MIN_SCORE: reason = f"نقاط {score:+d} < {MIN_SCORE}"
    if trend_block: reason += f" ترند يعاكس ({trend})"

    print(f"   {sym}: {status} | score:{score:+d}/8 | ADX:{adx_val:.0f} | ATR:{atr_pct:.2f}% | RSI:{r:.0f} | MACD_hist:{hist:.6f} | ترند:{trend} {reason}")
    if abs(score)>=MIN_SCORE and not trend_block:
        passed.append(sym)

print(f"\n   → عملات مؤهلة للدخول: {len(passed)} من {len(SYMS)}")
if passed: print(f"   → {passed}")

# 4. اختبار إذن الفيوتشر
print("\n[4] اختبار إذن وضع الرافعة (BTCUSDT):")
lev_res = api("/fapi/v1/leverage", {"symbol":"BTCUSDT","leverage":5}, signed=False)
# Actually leverage is POST, skip direct test
print("   (يحتاج POST — يُختبر عند أول صفقة)")

print("\n" + "="*55)
print("  خلاصة التشخيص:")
print("="*55)
if bal <= 0:
    print("  🔴 السبب الرئيسي: رصيد الفيوتشر = $0")
    print("  → حوّل رصيداً من Spot إلى Futures في Binance")
else:
    print(f"  ✅ الرصيد: ${bal:.2f}")
    if not passed:
        print("  🔴 السبب: لا توجد عملات تستوفي الشروط الآن")
        print("  → قد يكون السوق هادئاً أو الفلاتر صارمة جداً")
    else:
        print(f"  ✅ يوجد {len(passed)} عملة مؤهلة — البوت يجب أن يشتري")
