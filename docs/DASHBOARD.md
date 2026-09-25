# APEX Live Dashboard — طبقة مراقبة للقراءة فقط

لوحة داخل مشروع APEX فوق Forward Test الحالي. **لا تكتب في استراتيجية التداول ولا في قاعدة البيانات**:
لا تغيير على `entry` أو `score` أو TP/SL أو `entry_expiry_days` أو مخطط SQLite، ولا reset.

## التشغيل (Windows، بجانب الحلقة)

في نافذة PowerShell ثانية داخل `C:\Users\JASSIM\ApexBot` — الحلقة `--loop` تبقى تعمل كما هي:

```powershell
git fetch origin
git checkout feat/live-dashboard
py -u run_dashboard.py
```

ثم افتح `http://127.0.0.1:8765`. الخيارات: `--db` (افتراضياً `apex_top100.db`) و`--cache` (`.apex_cache`) و`--port`.
الخادم يستمع على `127.0.0.1` فقط ما لم يُمرَّر `--host` صراحةً.

> تحويل مجلد العمل إلى فرع آخر لا يمسّ العملية العاملة: الحلقة تحمل كودها في الذاكرة، وملفات الفرع
> لا تغيّر `apex_top100/`. لكن لا تُعِد تشغيل الحلقة من هذا الفرع قبل دمجه — عُد إلى `main` أولاً.

## ضمانات القراءة فقط

| ماذا | كيف |
|---|---|
| SQLite | يُفتح بـ`file:...?mode=ro` — أي `UPDATE`/`CREATE` يفشل من SQLite نفسه |
| أرقام الأداء | دوال `PaperBroker` نفسها (`stats`, `equity`, `max_drawdown`) على اتصال للقراءة، بلا `__init__` الذي ينفّذ `CREATE TABLE` |
| الشموع | تُقرأ من كاش المحرك `kl_<PAIR>_1d.json`، والخادم لا يطلب الشبكة |
| HTTP | GET/HEAD فقط؛ POST/PUT/PATCH/DELETE ترد 405 |
| الاختبار | `test_every_endpoint_leaves_the_database_byte_identical` يقارن SHA-256 للقاعدة والكاش قبل كل المسارات وبعدها |

## الـAPI

| المسار | المحتوى |
|---|---|
| `GET /api/status` | `mode=PAPER`، `auto_execute` (من القيمة الافتراضية في `ApexV2Bridge`)، Regime، Paper Equity، P&L المحقق، Max Drawdown، عدّادات OPEN/PENDING/CLOSED/EXPIRED، غير المحقق التقديري، نبض الحلقة |
| `GET /api/positions[?status=]` | كل المراكز الورقية مع آخر سعر من الكاش |
| `GET /api/positions/{id}` | مركز واحد مع Execution Timeline |
| `GET /api/signals[?limit=]` | الإشارات المسجّلة في جدول `signals` |
| `GET /api/chart/{SYMBOL}[?position_id=]` | شموع 1d + Volume + SMA20/SMA50/EMA21 + RSI14 + مستويات Entry/TP1-3/Stop + علامات SIGNAL/ENTRY/TP/EXIT |
| `GET /api/performance` | إحصاءات Forward Test، منحنى رأس المال المحقق، اللقطات اليومية، تفصيل EXPIRED |

## ما يجب معرفته عند القراءة

- **EXPIRED ظاهر**: في العدّادات والجدول والأداء (مع `exit_reason`)، بخلاف `report()` الحالي.
- **Execution Timeline**: `SIGNAL → APPROVED → PENDING/FILLED → TP1 → SL→BE → TP2 → TP3 → CLOSED/EXPIRED`.
  `APPROVED` في هذه المرحلة موافقة ورقية (بوابة الدورة السليمة) لا أمر حقيقي، و`latency_ms` فارغ حتى محرك التنفيذ.
- **أزمنة الأهداف مستنتجة**: الجدول يحفظ *أيّ* الأهداف تحققت لا *متى*. اللوحة تأخذ أول شمعة بعد الدخول بلغ High فيها
  الهدف وتعلّمها «مستنتج» (`source="inferred"` و`*` على الشارت). هذا صحيح لأن الشمعة التي تلمس الوقف والهدف معاً
  تُغلق بالوقف أولاً فلا يدخل الهدف `hits`.
- **غير المحقق تقديري**: من آخر إغلاق في الكاش، بوزن `size_pct` والجزء الباقي. لا يدخل Paper Equity ولا Max Drawdown
  (اللذين يبقيان على المحقق كما في `--paper-report`).
- **Binance WebSocket للعرض فقط**: المتصفح يشترك في `miniTicker` للمراكز OPEN/PENDING و`kline_1d` للشارت المفتوح.
  يحدّث السعر والشمعة الأخيرة فوراً، لكن إدارة المراكز الورقية تبقى في دورة المحرك على الشموع اليومية.
  المصدر الأساسي `wss://stream.binance.com:9443`؛ إن لم يُفتح الاتصال (رفض أو 8 ثوانٍ بلا فتح) تنتقل اللوحة تلقائياً إلى
  `wss://data-stream.binance.vision` على 443 (خادم Binance الرسمي لبيانات السوق فقط)، لأن بعض الشبكات تحجب المنفذ 9443.
  الشريط يعرض المصدر المستخدم `Binance WS • primary` أو `• fallback`، والانتقال يُسجَّل في Console. بعد فشل الاثنين
  تعود المحاولة إلى الأساسي كل 15 ثانية. الأسعار الحية تظهر للمراكز OPEN/PENDING فقط؛ CLOSED وEXPIRED تبقى `cache`.
- **نبض الحلقة**: من آخر `ts` في `regime_history`/`paper_equity`/`signals` (كل دورة تكتبها). أخضر حتى دورتين، أصفر حتى ست.
- مكتبة الشارت `lightweight-charts@4.2.3` من jsDelivr بتوقيع SRI. بلا اتصال بها يبقى الجدول والـTimeline والأداء.

## الاختبارات

```
python -m apex_dashboard.tests.test_dashboard
```
