import json, sys, time
sys.stdout.reconfigure(encoding="utf-8")
with open(r"C:\Users\JASSIM\Downloads\apex_ultimate_state.json","r",encoding="utf-8") as f:
    d=json.load(f)
trades=d.get("trades",[])

# daily_pnl = -26.30 -- show last 10 trades (today)
print("=== آخر 10 صفقات (اليوم) ===")
for t in trades[-10:]:
    print(f"  {t['symbol']:<16} {t['pnl']:>+7.2f}$  held:{t['held']:<6}  {t['reason']}")

print()
today = trades[-10:]
wins  = [t for t in today if t['pnl']>=0]
loss  = [t for t in today if t['pnl']<0]
print(f"ربح: {len(wins)} صفقة = {sum(t['pnl'] for t in wins):+.2f}$")
print(f"خسارة: {len(loss)} صفقة = {sum(t['pnl'] for t in loss):+.2f}$")
print()

# open positions unrealized PnL
print("=== مفتوحة الآن ===")
import urllib.request, urllib.error
now=time.time()
for sym,pos in d["open_positions"].items():
    h=(now-pos["open_time"])/60
    try:
        url=f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
        with urllib.request.urlopen(url,timeout=5) as r:
            cur=float(json.loads(r.read())["price"])
        unreal = pos["qty"]*(cur-pos["entry"])
        unreal_pct = (cur-pos["entry"])/pos["entry"]*100
        dist_sl = (cur-pos["sl"])/pos["sl"]*100
        print(f"  {sym:<16} دخول:{pos['entry']:.4f}  حالي:{cur:.4f}  PnL:{unreal:+.2f}$ ({unreal_pct:+.2f}%)  SL بُعد:{dist_sl:.1f}%  مدة:{h:.0f}م")
    except:
        print(f"  {sym:<16} entry:{pos['entry']}  held:{h:.0f}m")
