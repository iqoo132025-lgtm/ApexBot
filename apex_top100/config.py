#!/usr/bin/env python3
"""
APEX Ultimate V2 — Top 100 Market Engine
إعدادات المحرك.

هذا المسار (Top 100) منفصل تماماً عن مسار Micro/New Tokens:
لا RugCheck ولا Bundled Supply هنا — تلك الفلاتر تخص عملات الميم الجديدة.
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class Top100Config:
    # ── الكون المتتبَّع ──
    # الكون = أكبر N عملة بترتيب CoinGecko الحقيقي (market_cap_rank) كما هو، بلا إعادة ترقيم.
    # Stablecoins والعملات المغلَّفة تبقى داخل الكون وداخل rank_history حفاظاً على صحة
    # الترتيب وأحداث الدخول/الخروج — وتُستبعد من *الإشارات* ومن حساب اتساع السوق فقط.
    universe_size: int = 100
    signal_exclude_stablecoins: bool = True
    signal_exclude_wrapped: bool = True    # WBTC/WETH/stETH... نسخ مكرّرة من الأصل
    extra_signal_excluded: List[str] = field(default_factory=list)

    # ── الدورات الزمنية (ثواني) ──
    universe_refresh_sec: int = 3600      # سحب قائمة Top 100 كل ساعة
    scan_interval_sec: int = 900          # مسح تحليلي كل 15 دقيقة
    snapshot_hour_utc: int = 0            # لقطة يومية عند 00:00 UTC

    # ── عتبات الإشارة ──
    min_score: int = 70                   # أقل APEX Score لإطلاق إشارة
    min_score_bear: int = 82              # عتبة أعلى في السوق الهابط
    signal_cooldown_hours: int = 24       # لا نكرر إشارة نفس العملة قبل هذه المدة
    rescore_improvement: int = 8          # إلا إذا تحسّن السكور بهذا القدر
    require_healthy_cycle: bool = True    # دورة منقوصة الجودة = لا إشارات ولا مراكز ورقية جديدة

    # ── إدارة حجم المركز (نسبة من رأس المال) ──
    base_position_pct: float = 3.0
    max_position_pct: float = 6.0
    min_position_pct: float = 1.0

    # ── أوزان مكوّنات التحليل (مجموعها 100) ──
    weights: Dict[str, float] = field(default_factory=lambda: {
        "trend":        22.0,   # الاتجاه الأسبوعي/الشهري
        "rs_btc":       15.0,   # القوة النسبية مقابل BTC
        "rs_eth":       8.0,    # القوة النسبية مقابل ETH
        "momentum":     15.0,   # Momentum / ROC / RSI
        "volume":       12.0,   # سلوك الفوليوم
        "rank":         10.0,   # تغيّر ترتيب Market Cap
        "structure":    10.0,   # الموقع بين الدعم والمقاومة
        "drawdown":     8.0,    # الـ Drawdown ومخاطر التذبذب
    })

    # ── Paper Trading / Forward Testing ──
    paper_trading: bool = True            # كل إشارة تُفتح كمركز ورقي ويُقاس أداؤها
    paper_start_equity: float = 1000.0
    paper_entry_expiry_days: int = 3
    paper_max_hold_days: int = 45
    paper_slippage_pct: float = 0.05

    # ── تخزين ──
    db_path: str = "apex_top100.db"
    cache_dir: str = ".apex_cache"
    history_days: int = 400               # كم يوم شمعة يومية نسحب

    # ── Telegram ──
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    send_list_events: bool = True         # إشعار عند دخول/خروج عملة من Top 100

    # ── حدود المصدر الخارجي ──
    # Binance هو المصدر الأساسي للشموع (بلا حد عملي على هذا الاستخدام)، وCoinGecko
    # بديل فقط: حصته مقنَّنة لأن الطبقة المجانية ترد 429 سريعاً عند 100 عملة.
    max_ohlcv_per_cycle: int = 40         # كم عملة نسحب لها شموع في الدورة الواحدة
    request_timeout: int = 15
    request_retries: int = 3
    binance_min_interval_sec: float = 0.12   # مباعدة بين طلبات Binance
    cg_min_interval_sec: float = 6.0         # مباعدة بين طلبات CoinGecko
    # 2.5 ثانية (أي ~24 طلباً/دقيقة) ضربت 429 بعد 14 طلباً في أول تشغيل حي.
    # 6 ثوانٍ = ~10 طلبات/دقيقة، والتأخير يقع على عملات fallback وحدها
    # لأن Binance هو المصدر الأساسي ولا يمر بهذه المباعدة.
    cg_max_calls_per_cycle: int = 25         # ميزانية CoinGecko للدورة الواحدة
    cg_max_consecutive_429: int = 2          # بعدها يُفتح قاطع الدائرة لبقية الدورة
    cg_fetch_volumes: bool = False           # طلب فوليوم إضافي من CoinGecko (يضاعف الاستهلاك)
    ohlcv_cache_ttl_sec: int = 3600          # عمر الشموع في الكاش قبل إعادة الطلب
    stale_max_age_sec: int = 259200          # أقصى عمر مقبول لنسخة قديمة (3 أيام)

    def to_dict(self) -> dict:
        return asdict(self)


# قائمة مساعدة لاستبعاد المغلَّفات والمرهونات (wrapped / liquid staking)
WRAPPED_PREFIXES = ("W", "ST", "WST", "RETH", "CB", "BB", "M")
WRAPPED_SYMBOLS = {
    "WBTC", "WETH", "WBETH", "WEETH", "STETH", "WSTETH", "RETH", "CBBTC", "CBETH",
    "SOLVBTC", "BSC-USD", "BTCB", "LBTC", "METH", "EZETH", "RSETH", "SUSDE", "SUSDS",
}
STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDD", "FRAX",
    "USDS", "USD1", "BUIDL", "EURC", "USDP", "GUSD", "LUSD", "CRVUSD",
}
