# APEX Ultimate V2

بوت تداول بمسارين داخل نظام واحد:

| المسار | النطاق | المحرك |
|---|---|---|
| **Micro / New Tokens** | Pump.fun + Dexscreener | RugCheck + Bundled Supply + Fake Volume + Developer Intelligence |
| **Top 100** | أكبر 100 عملة حسب Market Cap | `apex_top100/` — اتجاه أسبوعي/شهري، Volume، القوة النسبية، Momentum، Drawdown، دعم/مقاومة، تغيّر الترتيب |

يجمعهما: **Market Regime Detector**، إدارة رأس المال، إدارة المراكز، Telegram، Pattern Database، ومحوّلات التنفيذ.

## البنية

```
APEX_ULTIMATE.py        بوت Binance Spot + لوحة تحكم على :8080 (مسار Top 100 مدمج بداخله)
APEX_FUTURES.py         بوت العقود الآجلة
pro_futures_bot.py      بوت عقود مستقل (يحتاج requirements.txt)
apex_top100/            محرك Top 100 — انظر TOP100_README.md
run_top100.py           تشغيل محرك Top 100 وحده: --once / --loop / --demo
setup_windows.ps1       إعداد ويندوز: المفاتيح + الاختبارات
.env.example            أسماء متغيرات البيئة المطلوبة
```

## التشغيل السريع

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1   # مرة واحدة
python APEX_ULTIMATE.py                                         # البوت + Top 100 معاً
```

ثم افتح `http://localhost:8080`. إشارات Top 100 متاحة أيضاً على `http://localhost:8080/api/top100`.

لتشغيل محرك Top 100 وحده:

```powershell
python run_top100.py --demo           # محاكاة بلا إنترنت
python run_top100.py --once           # دورة حقيقية
python run_top100.py --paper-report   # تقرير Forward Test
```

## المفاتيح

لا يوجد أي مفتاح داخل الكود. كل شيء من متغيرات البيئة:

```powershell
setx BINANCE_API_KEY "..."
setx BINANCE_API_SECRET "..."
setx TELEGRAM_TOKEN "..."       # اختياري لإشارات Top 100
setx TELEGRAM_CHAT_ID "..."     # اختياري
```

`setx` لا يؤثر على النافذة المفتوحة — افتح PowerShell جديدة بعده.

> المفاتيح التي كانت مكتوبة نصاً في الكود سابقاً تُعتبر مكشوفة ويجب إلغاؤها من لوحة Binance.

## الاختبارات

يشغّلها GitHub Actions على كل push و PR (بايثون 3.10 و3.11 و3.12)، ومعها فحص يفشل إن عاد أي مفتاح مكتوباً نصاً داخل الكود.

```powershell
python -m apex_top100.tests.test_engine      # المحرك: 11 اختباراً
python -m apex_top100.tests.test_paper       # Paper Trading: 25 اختباراً
python -m apex_top100.tests.test_data_sources # البيانات الحية: 20 اختباراً
python -m apex_top100.tests.test_apex_hook   # الدمج داخل البوت: 4 اختبارات
```

## إعداد Git

```powershell
git init
git add .
git commit -m "APEX Ultimate V2 + Top 100 Market Engine"
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

`.gitignore` يستبعد قاعدة البيانات وملف الحالة والكاش و`.env` تلقائياً.
