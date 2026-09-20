#!/usr/bin/env python3
"""
Top100MarketEngine — المسار الثاني داخل APEX Ultimate V2.

المسار الأول (Micro/New Tokens): Pump.fun + Dexscreener + RugCheck + Fake Volume + Developer Intelligence.
المسار الثاني (هذا الملف): أكبر 100 عملة حسب Market Cap — استثمار وسوينغ واتجاه عام.

الدورة الواحدة:
  1) تحديث قائمة Top 100 وتسجيل الداخل/الخارج منها
  2) لقطة يومية في قاعدة البيانات (تاريخ الترتيب لا يُحذف أبداً)
  3) تصنيف حالة السوق من البيانات (Market Regime Detector)
  4) تحليل العملات وإصدار إشارات APEX Score
  5) تسليم الإشارة إلى Telegram و إلى APEX (رأس المال / إدارة المراكز / التنفيذ)
"""
import time
import traceback
from typing import Callable, Dict, List, Optional

from .analysis import Top100Signal, analyze_coin
from .paper import PaperBroker, PaperConfig
from .config import Top100Config
from .data_sources import CoinInfo, LiveDataProvider, OHLCV
from .regime import RegimeResult, detect_regime
from .snapshots import ListEvent, SnapshotStore, _today
from .telegram_signals import TelegramSender, format_list_event, format_regime_change


class ApexHooks:
    """
    نقاط الربط مع بقية APEX Ultimate V2.
    ورّث هذا الصنف ومرّره للمحرك لتوصيله بإدارة رأس المال والمراكز والتنفيذ.
    """

    def should_trade(self, sig: Top100Signal) -> bool:
        """اعتراض إدارة رأس المال: هل نسمح بالإشارة؟ (حدود يومية، مراكز مفتوحة، تعرّض للقطاع...)"""
        return True

    def on_signal(self, sig: Top100Signal) -> None:
        """تسليم الإشارة لمدير المراكز / Execution Adapter (BonkBot أو غيره)."""

    def on_list_event(self, event: ListEvent, regime: Optional[RegimeResult]) -> None:
        """دخول/خروج عملة من Top 100."""

    def on_regime_change(self, result: RegimeResult) -> None:
        """تغيّر حالة السوق — لتعديل الأوزان في بقية النظام."""

    def on_pattern(self, name: str, rows: List[dict]) -> None:
        """أنماط مكتشفة من تاريخ اللقطات (Pattern Database)."""

    def log(self, msg: str, level: str = "info") -> None:
        """
        سجل نصي. level من {"info", "warn", "error", "success"} — كل تطبيق للخطاف
        يجب أن يقبل الوسيط الثاني حتى لا ينكسر مسار تحذير.
        """
        prefix = "[TOP100]" if level == "info" else f"[TOP100][{level}]"
        print(f"{prefix} {msg}", flush=True)


class Top100MarketEngine:
    def __init__(self, cfg: Optional[Top100Config] = None, provider=None,
                 store: Optional[SnapshotStore] = None, hooks: Optional[ApexHooks] = None,
                 telegram: Optional[TelegramSender] = None):
        self.cfg = cfg or Top100Config()
        self.provider = provider or LiveDataProvider(self.cfg)
        self.store = store or SnapshotStore(self.cfg.db_path)
        self.hooks = hooks or ApexHooks()
        self.telegram = telegram or TelegramSender(self.cfg.telegram_token, self.cfg.telegram_chat_id)

        self.paper: Optional[PaperBroker] = None
        if self.cfg.paper_trading:
            self.paper = PaperBroker(self.store.conn, PaperConfig(
                start_equity=self.cfg.paper_start_equity,
                entry_expiry_days=self.cfg.paper_entry_expiry_days,
                max_hold_days=self.cfg.paper_max_hold_days,
                slippage_pct=self.cfg.paper_slippage_pct,
            ))

        self.universe: List[CoinInfo] = []
        self.ohlcv_cache: Dict[str, OHLCV] = {}
        self.regime: Optional[RegimeResult] = None
        self.last_universe_refresh: float = 0.0
        self.new_entries: List[str] = []       # coin_ids دخلت Top 100 حديثاً (أولوية تتبع)

    # ══════════════════════════════════════
    #  1) الكون
    # ══════════════════════════════════════
    def refresh_universe(self, force: bool = False) -> List[CoinInfo]:
        if not force and self.universe and \
           time.time() - self.last_universe_refresh < self.cfg.universe_refresh_sec:
            return self.universe
        self.universe = self.provider.fetch_top_markets(self.cfg.universe_size)
        self.last_universe_refresh = time.time()
        self.hooks.log(f"تم تحديث الكون: {len(self.universe)} عملة")
        return self.universe

    def _coin(self, symbol: str) -> Optional[CoinInfo]:
        return next((c for c in self.universe if c.symbol.upper() == symbol.upper()), None)

    # ══════════════════════════════════════
    #  2) اللقطة اليومية + أحداث القائمة
    # ══════════════════════════════════════
    def ensure_daily_snapshot(self, force: bool = False, date: Optional[str] = None) -> List[ListEvent]:
        date = date or _today()
        if not force and self.store.has_snapshot(date):
            return []
        btc = self._coin("BTC")
        eth = self._coin("ETH")
        _, events = self.store.take_snapshot(
            self.universe, btc_price=btc.price if btc else 0.0,
            eth_price=eth.price if eth else 0.0, date=date)
        for e in events:
            if e.event == "ENTER":
                self.new_entries.append(e.coin_id)
            self.hooks.on_list_event(e, self.regime)
            if self.cfg.send_list_events and self.telegram.enabled:
                self.telegram.send(format_list_event(e, self.regime.regime if self.regime else None))
        if events:
            self.hooks.log(f"أحداث Top 100: {sum(1 for e in events if e.event=='ENTER')} دخول، "
                           f"{sum(1 for e in events if e.event=='EXIT')} خروج")
        return events

    # ══════════════════════════════════════
    #  3) بيانات الشموع
    # ══════════════════════════════════════
    def signalable_universe(self) -> List[CoinInfo]:
        """
        الكون كله يدخل التاريخ واللقطات بترتيبه الحقيقي.
        الإشارات وحساب اتساع السوق يستبعدان Stablecoins والعملات المغلَّفة.
        """
        return [c for c in self.universe if c.signalable(self.cfg)]

    def _priority_order(self) -> List[CoinInfo]:
        """أولوية السحب: الداخل الجديد، ثم صاعدو الترتيب، ثم الأعلى ترتيباً."""
        movers = {m["coin_id"] for m in self.store.top_rank_movers(days=30, limit=15)}
        def key(c: CoinInfo):
            return (0 if c.coin_id in self.new_entries else 1 if c.coin_id in movers else 2, c.rank)
        return sorted(self.signalable_universe(), key=key)

    def load_ohlcv(self, coins: List[CoinInfo]) -> Dict[str, OHLCV]:
        out: Dict[str, OHLCV] = {}
        for c in coins:
            try:
                data = self.provider.fetch_ohlcv(c, self.cfg.history_days)
            except Exception as e:
                self.hooks.log(f"تعذّر سحب شموع {c.symbol}: {e}")
                data = None
            if data and len(data) >= 60:
                out[c.coin_id] = data
        self.ohlcv_cache.update(out)
        return out

    # ══════════════════════════════════════
    #  4) حالة السوق
    # ══════════════════════════════════════
    def update_regime(self) -> Optional[RegimeResult]:
        btc = self._coin("BTC")
        eth = self._coin("ETH")
        btc_ohlcv = self.ohlcv_cache.get(btc.coin_id) if btc else None
        if btc and not btc_ohlcv:
            btc_ohlcv = self.provider.fetch_ohlcv(btc, self.cfg.history_days)
            if btc_ohlcv:
                self.ohlcv_cache[btc.coin_id] = btc_ohlcv
        if not btc_ohlcv:
            self.hooks.log("لا توجد بيانات BTC — تخطّي تصنيف حالة السوق")
            return self.regime
        eth_ohlcv = self.ohlcv_cache.get(eth.coin_id) if eth else None

        eligible = {c.coin_id for c in self.signalable_universe()}
        coin_closes = {cid: o.closes for cid, o in self.ohlcv_cache.items()
                       if cid in eligible and not getattr(o, "price_only", False)
                       and not getattr(o, "stale", False)}
        mcap_change = self._total_mcap_change_30d()
        previous = self.store.last_regime()
        result = detect_regime(btc_ohlcv.closes, coin_closes,
                               eth_ohlcv.closes if eth_ohlcv else None,
                               total_mcap_change_30d=mcap_change, previous=previous)
        prev_regime = previous.get("regime") if previous else None
        self.store.save_regime(result.regime, result.score, result.metrics)
        self.regime = result
        if prev_regime and prev_regime != result.regime:
            result.changed_from = prev_regime
            self.hooks.on_regime_change(result)
            if self.telegram.enabled:
                self.telegram.send(format_regime_change(result))
            self.hooks.log(f"تغيّرت حالة السوق: {prev_regime} → {result.regime}")
        else:
            self.hooks.log(f"حالة السوق: {result.regime} ({result.score:.0f}/100)")
        return result

    def _total_mcap_change_30d(self) -> Optional[float]:
        dates = self.store.snapshot_dates(31)
        if len(dates) < 5:
            return None
        new = sum(r["market_cap"] or 0 for r in self.store.snapshot(dates[0]).values())
        old = sum(r["market_cap"] or 0 for r in self.store.snapshot(dates[-1]).values())
        return ((new / old - 1) * 100.0) if old else None

    # ══════════════════════════════════════
    #  5) المسح وإصدار الإشارات
    # ══════════════════════════════════════
    def min_score_now(self) -> float:
        base = self.cfg.min_score
        if self.regime and self.regime.regime == "BEAR":
            base = max(base, self.cfg.min_score_bear)
        add = self.regime.policy["score_add"] if self.regime else 0.0
        return min(98.0, base + (add if self.regime and self.regime.regime != "BEAR" else 0.0))

    def analyze_all(self) -> List[Top100Signal]:
        btc = self._coin("BTC")
        eth = self._coin("ETH")
        btc_closes = self.ohlcv_cache[btc.coin_id].closes if btc and btc.coin_id in self.ohlcv_cache else []
        eth_closes = self.ohlcv_cache[eth.coin_id].closes if eth and eth.coin_id in self.ohlcv_cache else []
        sigs: List[Top100Signal] = []
        for c in self.signalable_universe():
            o = self.ohlcv_cache.get(c.coin_id)
            if not o:
                continue
            try:
                sig = analyze_coin(c, o, btc_closes, eth_closes, self.cfg, self.regime,
                                   rank_change_30=self.store.rank_change(c.coin_id, 30),
                                   rank_velocity_14=self.store.rank_velocity(c.coin_id, 14))
            except Exception:
                self.hooks.log(f"خطأ في تحليل {c.symbol}: {traceback.format_exc(limit=2)}")
                continue
            if sig:
                sigs.append(sig)
        sigs.sort(key=lambda s: s.score, reverse=True)
        return sigs

    def _is_duplicate(self, sig: Top100Signal) -> bool:
        last = self.store.last_signal(sig.coin_id)
        if not last:
            return False
        age_h = (time.time() - last["ts"]) / 3600.0
        if age_h >= self.cfg.signal_cooldown_hours:
            return False
        if (last["regime"] or "") != sig.regime:
            return False
        return sig.score < (last["score"] or 0) + self.cfg.rescore_improvement

    def cycle_blocked(self) -> Optional[str]:
        """
        سبب حجب نتائج الدورة، أو None إذا كانت جودة البيانات مقبولة.

        قاعدة fail-closed: دورة بها 429 أو قاطع مفتوح أو بيانات كاش قديمة لم
        تستوفِ شروط الاعتماد، فلا تُرسَل منها إشارة ولا يُفتح منها مركز ورقي جديد.
        التحليل والتصنيف واللقطات والتشخيص تستمر — لكن كنتيجة تشخيصية فقط.
        """
        if not self.cfg.require_healthy_cycle:
            return None
        report = self.data_report()
        if report is None or report.healthy:
            return None
        reasons = []
        if report.cg_circuit_open:
            reasons.append("قاطع CoinGecko مفتوح")
        if report.http_errors.get("429", 0):
            reasons.append(f"{report.http_errors['429']}× خطأ 429")
        if report.stale_symbols:
            reasons.append(f"{len(report.stale_symbols)} عملة ببيانات قديمة")
        return "، ".join(reasons) or "جودة البيانات غير مستوفاة"

    def emit(self, sigs: List[Top100Signal]) -> List[Top100Signal]:
        threshold = self.min_score_now()
        blocked = self.cycle_blocked()
        if blocked is not None:
            held = sum(1 for s in sigs if s.tradable and s.score >= threshold)
            self.hooks.log(f"دورة غير سليمة ({blocked}) — حُجبت {held} إشارة مرشحة "
                           f"ولم يُفتح أي مركز ورقي جديد؛ النتائج تشخيصية فقط", "warn")
            return []
        sent: List[Top100Signal] = []
        for sig in sigs:
            if not sig.tradable or sig.score < threshold:
                continue
            if self._is_duplicate(sig):
                continue
            if not self.hooks.should_trade(sig):
                continue
            self.store.record_signal({**sig.to_dict(), "regime": sig.regime})
            self.hooks.on_signal(sig)
            if self.paper:
                self.paper.open_from_signal(sig)
            if self.telegram.enabled:
                self.telegram.send_signal(sig)
            sent.append(sig)
        if sent:
            self.hooks.log(f"أُرسلت {len(sent)} إشارة (العتبة {threshold:.0f})")
        return sent

    # ══════════════════════════════════════
    #  الأنماط من تاريخ اللقطات
    # ══════════════════════════════════════
    def scan_patterns(self) -> Dict[str, List[dict]]:
        patterns = {
            "rank_movers": self.store.top_rank_movers(days=30),
            "outperformers_vs_btc": self.store.outperformers_vs_btc(days=30),
            "volume_leading_price": self.store.volume_leading_price(days=14),
            "new_entries": self.store.newly_entered(days=30),
        }
        for name, rows in patterns.items():
            if rows:
                self.hooks.on_pattern(name, rows)
        return patterns

    # ══════════════════════════════════════
    #  الدورة الكاملة
    # ══════════════════════════════════════
    def run_once(self) -> Dict[str, object]:
        self.begin_cycle()
        self.refresh_universe()
        events = self.ensure_daily_snapshot()
        coins = self._priority_order()[:self.cfg.max_ohlcv_per_cycle]
        for sym in ("BTC", "ETH"):
            c = self._coin(sym)
            if c and c not in coins:
                coins.append(c)
        self.load_ohlcv(coins)
        self.update_regime()
        sigs = self.analyze_all()
        sent = self.emit(sigs)
        paper_events = self.update_paper()
        patterns = self.scan_patterns()
        self.new_entries = []
        report = self.data_report()
        if report is not None:
            self.hooks.log("تقرير بيانات الدورة:\n" + report.summary())
            if not report.binance_available:
                self.hooks.log("Binance غير متاح — الشموع تعتمد على CoinGecko وحدها", "warn")
            if report.cg_circuit_open:
                self.hooks.log("CoinGecko ردّ 429 — أُوقفت طلباته لبقية الدورة", "warn")
            if report.stale_symbols:
                self.hooks.log(f"{len(report.stale_symbols)} عملة ببيانات كاش قديمة — "
                               f"محجوبة عن الإشارات", "warn")
        return {"regime": self.regime, "signals": sigs, "sent": sent,
                "events": events, "patterns": patterns,
                "paper_events": paper_events,
                "paper_stats": self.paper.stats() if self.paper else None,
                "data_report": report, "blocked": self.cycle_blocked()}

    def begin_cycle(self) -> None:
        """تصفير عدّادات المزوّد وميزانيته قبل كل دورة (المزوّد الصناعي لا يملكها)."""
        fn = getattr(self.provider, "begin_cycle", None)
        if callable(fn):
            fn()

    def data_report(self):
        """تقرير مصادر بيانات الدورة، أو None لمزوّد لا يصدر تقريراً."""
        return getattr(self.provider, "report", None)

    def update_paper(self) -> List[dict]:
        """
        يدير المراكز الورقية على شموع OHLC الحقيقية لكل فترة منذ آخر تحديث.

        لا تُستخدم لقطة السعر الحالية: بين دورتين قد يهبط السعر فيضرب الوقف
        ثم يرتد فوق الهدف، واللقطة ترى الارتداد فقط فتحتسب ربحاً لصفقة أُغلقت خاسرة.
        العملات ذات البيانات price_only (بلا High/Low حقيقية) تُستبعد من الإدارة،
        وهي أصلاً ممنوعة من فتح مركز ورقي.
        """
        if not self.paper:
            return []
        candles = {cid: o for cid, o in self.ohlcv_cache.items()
                   if o and not getattr(o, "price_only", False)
                   and not getattr(o, "stale", False)}
        events = self.paper.apply_candles(candles)
        self.paper.record_equity()
        for e in events:
            self.hooks.log(f"Paper {e['event']} — {e['symbol']}"
                           + (f" ({e.get('reason')})" if e.get("reason") else ""))
        self.hooks.on_pattern("paper_events", events) if events else None
        return events

    def run_forever(self, stop: Optional[Callable[[], bool]] = None) -> None:
        while not (stop and stop()):
            try:
                self.run_once()
            except Exception:
                self.hooks.log(f"خطأ في الدورة: {traceback.format_exc(limit=3)}")
            time.sleep(self.cfg.scan_interval_sec)
