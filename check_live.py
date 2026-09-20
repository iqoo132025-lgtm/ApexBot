#!/usr/bin/env python3
"""
فحص أول تشغيل حي لمحرك Top 100.

يشغّل دورة واحدة كما يفعل `run_top100.py --once`، ثم يطبع ما يحتاجه المراجع:
من أين جاءت كل بيانة، وكم عملة سقطت إلى price_only، وهل ضربنا 429/403/timeout.

ضعه داخل مجلد APEX بجانب run_top100.py وشغّله:  python check_live.py
لا يغيّر شيئاً في المستودع ولا في قاعدة البيانات أكثر مما تفعله دورة عادية.
"""
import collections
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── عدّاد أخطاء الشبكة: HttpCache يبتلع الأخطاء ويعيد المحاولة ثم يسقط على
#    نسخة الكاش القديمة بصمت، فنعدّها هنا قبل أن تُبتلع.
_net_errors: collections.Counter = collections.Counter()
_orig_urlopen = urllib.request.urlopen


def _counting_urlopen(*args, **kwargs):
    try:
        return _orig_urlopen(*args, **kwargs)
    except urllib.error.HTTPError as e:
        _net_errors[f"HTTP {e.code}"] += 1
        raise
    except Exception as e:
        _net_errors[type(e).__name__] += 1
        raise


urllib.request.urlopen = _counting_urlopen

from apex_top100.config import Top100Config          # noqa: E402
from apex_top100.integration import ApexV2Bridge, build_engine   # noqa: E402

cfg = Top100Config(db_path="apex_top100.db")
eng = build_engine(cfg, bridge=ApexV2Bridge())
out = eng.run_once()

print("\n" + "=" * 56)
print("فحص التشغيل الحي")
print("=" * 56)

uni = eng.universe
if uni:
    print(f"الكون من CoinGecko: {len(uni)} عملة، الترتيب الحقيقي من "
          f"#{min(c.rank for c in uni)} إلى #{max(c.rank for c in uni)}")
    print(f"  مستبعدة من الإشارات فقط (stable/wrapped): "
          f"{sum(1 for c in uni if not c.signalable(cfg))} — تبقى في الكون وفي اللقطات")
else:
    print("الكون فارغ — لم تصل قائمة CoinGecko")

src = collections.Counter()
price_only = []
for o in eng.ohlcv_cache.values():
    if getattr(o, "price_only", False):
        src["price_only"] += 1
        price_only.append(o.symbol)
    else:
        src[o.source] += 1
print(f"\nمصادر الشموع ({len(eng.ohlcv_cache)} عملة): {dict(src)}")
if price_only:
    print(f"  price_only ({len(price_only)}): {', '.join(sorted(price_only))}")
    print("  ← بلا High/Low حقيقية: لا إشارة تعتمد على ATR ولا إدارة ورقية")

print(f"\nأخطاء الشبكة أثناء الدورة: "
      f"{dict(_net_errors) if _net_errors else 'لا شيء'}")
if _net_errors:
    print("  ← أي 429 أو 403 هنا يعني أن جزءاً من البيانات جاء من الكاش القديم")

reg = out["regime"]
if reg:
    print(f"\nحالة السوق: {reg.regime}  ({reg.score:.0f}/100)")
    for k, v in (reg.metrics or {}).items():
        print(f"  {k}: {v}")

sigs = out["signals"]
print(f"\nتحليلات: {len(sigs)} — قابلة للتداول: {sum(1 for s in sigs if s.tradable)} "
      f"— أُرسلت: {len(out['sent'])} (العتبة {eng.min_score_now():.0f})")
for s in sorted(sigs, key=lambda x: -x.score)[:15]:
    print(f"  #{s.rank:<4} {s.symbol:<6} score={s.score:5.1f} "
          f"{'tradable' if s.tradable else 'blocked ':<9} {s.data_quality}")

print("\n" + (eng.paper.report() if eng.paper else "Paper Trading معطّل"))
