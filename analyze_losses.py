import json, sys, time
sys.stdout.reconfigure(encoding="utf-8")
with open(r"C:\Users\JASSIM\Downloads\apex_ultimate_state.json","r",encoding="utf-8") as f:
    d=json.load(f)
trades=d.get("trades",[])
print(f"total_pnl: {d['total_pnl']:.2f}  daily_pnl: {d['daily_pnl']:.2f}")
print(f"wins:{d['wins']}  losses:{d['losses']}  WR:{d['wins']/(d['wins']+d['losses'])*100:.0f}%")
print()

# today trades only (daily_reset_day = today)
# show last 30 trades
recent = trades[-30:]
print("=== آخر 30 صفقة ===")
for t in recent:
    print(f"  {t['symbol']:<16} {t['pnl']:>+7.2f}$  held:{t['held']:<6}  {t['reason']}")
print()

from collections import Counter
print("=== اسباب الاغلاق (كل الصفقات) ===")
for r,c in Counter(t["reason"] for t in trades).most_common():
    ps = sum(t["pnl"] for t in trades if t["reason"]==r)
    print(f"  {r:<22} x{c:<3}  {ps:>+8.2f}$")
print()

print("=== مفتوحة الآن ===")
now=time.time()
for sym,pos in d["open_positions"].items():
    h=(now-pos["open_time"])/60
    unreal_pct = 0
    print(f"  {sym:<16} entry:{pos['entry']}  sl:{pos['sl']}  tp:{pos['tp']}  held:{h:.0f}m  atr%:{pos.get('atr_pct',0):.2f}%")
