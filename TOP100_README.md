# Top 100 Market Engine — داخل APEX Ultimate V2

مسار ثانٍ داخل نفس البوت (وليس بوتاً منفصلاً). يعمل بجانب مسار Pump.fun/Dexscreener،
ويشاركه حالة السوق وإدارة رأس المال وإدارة المراكز و Telegram و Pattern Database وطبقة التنفيذ.

```
APEX Ultimate V2
├── مسار 1: Micro/New Tokens   → Pump.fun + Dexscreener + RugCheck + Bundled Supply
│                                + Fake Volume + Developer Intelligence
└── مسار 2: Top 100            → apex_top100/  (هذا المحرك)
     الاتجاه الأسبوعي/الشهري · Volume · Market Cap · BTC correlation
     القوة النسبية مقابل BTC و ETH · Momentum · Drawdown
     الدعم/المقاومة · تغيّر ترتيب Market Cap
```

شروط RugCheck و Bundled Supply **لا تُطبَّق** على Top 100 — هي فلاتر عملات الميم الجديدة فقط.

## الملفات

| الملف | الوظيفة |
|---|---|
| `apex_top100/config.py` | كل الإعدادات والأوزان في مكان واحد |
| `apex_top100/data_sources.py` | CoinGecko (الترتيب/القيمة السوقية) + Binance (الشموع) + كاش وإعادة محاولة |
| `apex_top100/indicators.py` | SMA/EMA/RSI/ROC/ATR/ارتباط/تجميع أسبوعي وشهري/قمم وقيعان |
| `apex_top100/regime.py` | **Market Regime Detector**: BEAR / RECOVERY / EARLY BULL / BULL / OVERHEATED |
| `apex_top100/analysis.py` | محرك تحليل Top 100 و APEX Score من 100 |
| `apex_top100/snapshots.py` | لقطات يومية (SQLite) + دخول/خروج القائمة + الأنماط |
| `apex_top100/telegram_signals.py` | صيغة الإشارة والأزرار [CHART] [ANALYSIS] [BUY] |
| `apex_top100/engine.py` | المنسّق: كون → لقطة → حالة سوق → تحليل → إشارة |
| `apex_top100/integration.py` | جسر الربط مع بقية APEX V2 |
| `apex_top100/providers_mock.py` | مزوّد بيانات صناعي للتجربة بدون إنترنت |
| `run_top100.py` | تشغيل: `--once` / `--loop` / `--demo` |

## التشغيل

```bash
python3 run_top100.py --demo            # محاكاة كاملة بدون إنترنت
python3 run_top100.py --once            # دورة واحدة على البيانات الحية
python3 run_top100.py --loop --telegram-token XXX --telegram-chat 123
python3 -m apex_top100.tests.test_engine   # 11 اختباراً
```

## Market Regime Detector

يُحسب سكور مركّب (0-100) من بيانات فقط:

- BTC مقابل SMA50 و SMA200، وتقاطع SMA50/SMA200
- عائد BTC 30/90 يوم، وبعده عن قمة 365 يوم، و RSI اليومي
- **اتساع السوق**: نسبة عملات Top 100 فوق SMA50 و SMA200، ونسبة القريبة من قمم 90 يوم
- تغيّر إجمالي القيمة السوقية لـ Top 100 خلال 30 يوم (من اللقطات نفسها)

| السكور | الحالة | مضاعف الحجم | وزن القوة النسبية | إضافة على عتبة الإشارة |
|---|---|---|---|---|
| < 25 | BEAR | 0.40 | 0.85 | +12 |
| 25-45 | RECOVERY | 0.70 | 0.95 | +5 |
| 45-65 | EARLY BULL | 1.00 | 1.12 | 0 |
| 65-83 | BULL | 1.00 | 1.15 | 0 |
| ≥ 83 أو علامتا إفراط | OVERHEATED | 0.55 | 0.90 | +8 |

قاعدتان مثبّتتان في الكود:

1. **التصنيف ليس سبب شراء.** يعدّل الأوزان وحجم المركز وعتبة السكور فقط. الإشارة تحتاج دائماً
   بوابة بنيوية: اتجاه أسبوعي داعم + فوليوم غير منكمش + سكور فوق العتبة
   (`analysis.py` → `tradable`، ويؤكده اختبار `test_policy_never_buys_alone`).
2. **مرشّح ثبات (hysteresis).** لا يتغيّر التصنيف إلا بعد قراءتين متتاليتين تؤكدان الحالة الجديدة،
   حتى لا يتذبذب بين BEAR و RECOVERY يوماً بيوم.

## APEX Score

| المكوّن | الوزن | ماذا يقيس |
|---|---|---|
| trend | 22 | السعر مقابل SMA10/SMA30 الأسبوعي و SMA50/200 اليومي + ميل أسبوعي/شهري |
| rs_btc | 15 | أداء 30 يوم ناقص أداء BTC + ميل نسبة العملة/BTC |
| rs_eth | 8 | نفس المقياس مقابل ETH |
| momentum | 15 | ROC 7/30/90 + RSI (الأفضل 55-70، وفوق 70 خصم) |
| volume | 12 | فوليوم 7ي/30ي و 30ي/90ي + نسبة التجميع (فوليوم الشموع الصاعدة/الهابطة) |
| rank | 10 | الترتيب الحالي + تحسّنه خلال 30 يوم + سرعته خلال 14 يوم |
| structure | 10 | الموقع داخل نطاق 90 يوم + المسافة للدعم والمقاومة |
| drawdown | 8 | البعد عن قمة 365 يوم و ATH + تذبذب ATR |

المخرجات: منطقة دخول (تصحيح إلى SMA20/أقرب دعم)، إبطال تحت الدعم بمقدار ATR،
ثلاثة أهداف من المقاومات الحقيقية (وتُكمَّل بمضاعفات مخاطرة 1.8R/3.2R/5R)،
وحجم مركز = الأساس × جودة السكور × مستوى المخاطرة × مضاعف حالة السوق (ضمن 1%-6%).

## تعريف الكون وجودة البيانات

قاعدتان تحكمان صحة الأرقام:

**الترتيب الحقيقي لا يُعاد ترقيمه.** الكون هو العملات التي ترتيبها في CoinGecko
(`market_cap_rank`) ضمن أول 100، ويُحفظ الترتيب كما هو. Stablecoins والعملات المغلَّفة
تبقى داخل الكون وداخل `rank_history` — فحذفها كان سيزيح بقية العملات ويشوّه حركة الترتيب
وأحداث الدخول/الخروج — وتُستبعد من **الإشارات** ومن حساب اتساع السوق فقط
(`signal_exclude_stablecoins` و`signal_exclude_wrapped`).

**لا إشارة على شموع غير حقيقية.** ترتيب مصادر الشموع: Binance أولاً، ثم `CoinGecko /ohlc`
وهي شموع حقيقية بـ High/Low فعلية مع فوليوم من `market_chart`. وإن لم يتوفر إلا سعر
الإغلاق اليومي، تُوسم البيانات `price_only` فيُلغى ATR ومستويات الدعم/المقاومة وتُمنع
الإشارة (`tradable=False`)، وتبقى العملة متتبَّعة بالسكور فقط. الحقل `data_quality` في
الإشارة يقول `ohlc` أو `price_only`.

## Snapshots وتتبع القائمة

SQLite في `apex_top100.db`، لقطة يومية واحدة، ولا يُحذف أي صف:

- `rank_history` — ترتيب وسعر وقيمة سوقية وفوليوم كل عملة كل يوم (مع سعر BTC و ETH لذلك اليوم)
- `list_events` — كل دخول/خروج من Top 100 (الخارج يبقى في التاريخ لأن دخوله لاحقاً إشارة بحد ذاتها)
- `tracking` — أول/آخر ظهور، أفضل وأسوأ ترتيب، وهل هي داخل القائمة الآن
- `regime_history` — حالة السوق يومياً مع مقاييسها
- `signals` — كل إشارة أُرسلت (نواة Pattern Database)

الأنماط الجاهزة من هذا التاريخ:

```python
store.top_rank_movers(days=30)          # من #80 إلى #40 مثلاً
store.outperformers_vs_btc(days=30)     # المتفوقة على BTC
store.volume_leading_price(days=14)     # فوليوم يرتفع والسعر ما زال هادئاً
store.newly_entered(days=30)            # الداخلة حديثاً (أولوية تتبع في المسح)
store.exited_history(days=365)          # تاريخ الخارجة
```

العملة الداخلة حديثاً تُعطى أولوية في سحب الشموع والتحليل في الدورة التالية مباشرة.

## الدمج داخل APEX_ULTIMATE.py (منفَّذ فعلاً)

المحرك موصول داخل البوت نفسه، لا كعملية منفصلة. ما أُضيف إلى `APEX_ULTIMATE.py`:

- إعدادات في `CONFIG`: `TOP100_ENABLED` و`TOP100_UNIVERSE` و`TOP100_MIN_SCORE` و`TOP100_SCAN_MIN`
- مفتاح `state["top100"]` يحمل حالة السوق وآخر الإشارات وأحداث دخول/خروج القائمة
- نقطة `GET /api/top100` في لوحة التحكم، و`top100` داخل `snapshot()` فيصل مع بث SSE
- تشغيل المحرك في خيط مستقل عند بدء البوت عبر `install_top100(CONFIG, state, log)`

حارس رأس المال مربوط بحالة البوت: لا تُرسل إشارة Top 100 عند تفعيل حماية الخسارة اليومية
(`state["protected"]`) أو عند امتلاء `MAX_OPEN_POSITIONS` أو إن كانت العملة ضمن `BANNED_SYMBOLS`.
و`auto_execute=False` أي أن Top 100 إشارات فقط ولا يفتح صفقة من نفسه.

الاختبار: `python -m apex_top100.tests.test_apex_hook` (4 اختبارات).

## الربط مع APEX Ultimate V2

```python
from apex_top100.config import Top100Config
from apex_top100.integration import ApexV2Bridge, build_engine, start_in_thread

bridge = ApexV2Bridge(
    capital_manager=apex.capital,       # approve_position(symbol, pct, meta) / set_market_regime(regime, policy)
    position_manager=apex.positions,    # register_signal(symbol, meta) / start_tracking(symbol)
    execution=apex.bonkbot,             # buy(symbol, pct, meta) — يُستدعى فقط عند auto_execute=True
    pattern_db=apex.patterns,           # record(kind, rows)
    telegram=apex.telegram,             # send(text, markup)
    logger=apex,                        # log(msg)
    auto_execute=False,                 # الافتراضي: إشارات فقط، والتنفيذ بقرارك
)

engine = build_engine(Top100Config(telegram_token=TOKEN, telegram_chat_id=CHAT), bridge)
start_in_thread(engine)                 # يعمل بجانب حلقة Pump.fun/Dexscreener
```

الجسر يستدعي فقط الدوال الموجودة فعلاً في كائناتك، فلا ينكسر إن لم تكن كلها جاهزة.
حالة السوق تُبلَّغ لإدارة رأس المال عبر `set_market_regime(regime, policy)` لتوحيد الأوزان بين المسارين.

## المفاتيح

كانت مفاتيح Binance مكتوبة نصاً في ستة ملفات (`APEX_ULTIMATE.py`، `APEX_FUTURES.py`،
`pro_futures_bot.py`، `check_spot.py`، `test_buy.py`، `test_futures.py`) — ونفس الزوج في كلها.
أصبحت الآن تُقرأ من متغيرات البيئة:

```powershell
setx BINANCE_API_KEY "المفتاح_الجديد"
setx BINANCE_API_SECRET "السر_الجديد"
```

المفاتيح القديمة تُعتبر مكشوفة ويجب إلغاؤها من لوحة Binance، لأن نقلها إلى متغيرات بيئة
لا يلغي انكشافها السابق.
