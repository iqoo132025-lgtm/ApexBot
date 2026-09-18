#!/usr/bin/env python3
"""
ربط Top 100 Market Engine ببقية APEX Ultimate V2.

المسارَان يشتركان في: حالة السوق، إدارة رأس المال، إدارة المراكز،
Telegram، Pattern Database، وطبقة التنفيذ — ويختلفان في محرك التحليل:

  Micro/New Tokens : Pump.fun + Dexscreener + RugCheck + Bundled Supply +
                     Fake Volume + Developer Intelligence
  Top 100          : الاتجاه الأسبوعي/الشهري + Volume + Market Cap + BTC correlation +
                     القوة النسبية + Momentum + Drawdown + الدعم/المقاومة + تغيّر الترتيب

استخدم ApexV2Bridge كجسر: مرّر له كائنات APEX الموجودة لديك (أياً كانت أسماؤها)
وسيستدعي الدوال المتاحة فقط، فلا ينكسر إن لم تكن كلها جاهزة.
"""
from typing import Any, Optional

from .config import Top100Config
from .engine import ApexHooks, Top100MarketEngine
from .snapshots import SnapshotStore
from .telegram_signals import TelegramSender, format_top100_signal, signal_buttons


def _call(obj: Any, name: str, *args, **kwargs):
    """استدعاء آمن: ينفّذ الدالة إن وُجدت، وإلا يتجاهل."""
    fn = getattr(obj, name, None) if obj is not None else None
    if callable(fn):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(f"[TOP100][bridge] {name} فشلت: {e}", flush=True)
    return None


class ApexV2Bridge(ApexHooks):
    """
    capital_manager : يُتوقع منه approve_position(symbol, pct, meta) أو can_open(...)
    position_manager: open_position(...) / track(...)
    execution       : محوّل التنفيذ (BonkBot / CEX / يدوي) → buy(symbol, pct, meta)
    pattern_db      : record(kind, rows)
    telegram        : أي كائن لديه send(text, markup) — أو اتركه للمرسل الداخلي
    logger          : أي كائن لديه log(msg) — مثل log() في APEX_ULTIMATE.py
    """

    def __init__(self, capital_manager=None, position_manager=None, execution=None,
                 pattern_db=None, telegram=None, logger=None, auto_execute: bool = False):
        self.capital_manager = capital_manager
        self.position_manager = position_manager
        self.execution = execution
        self.pattern_db = pattern_db
        self.telegram = telegram
        self.logger = logger
        self.auto_execute = auto_execute      # False = إشارات فقط (الوضع الافتراضي الآمن)

    # ── بوابة رأس المال ──
    def should_trade(self, sig) -> bool:
        meta = sig.to_dict()
        for name in ("approve_position", "can_open", "allow_trade"):
            res = _call(self.capital_manager, name, sig.symbol, sig.position_pct, meta)
            if res is not None:
                return bool(res)
        return True

    # ── تسليم الإشارة ──
    def on_signal(self, sig) -> None:
        meta = sig.to_dict()
        _call(self.position_manager, "register_signal", sig.symbol, meta)
        _call(self.pattern_db, "record", "top100_signal", [meta])
        if self.telegram is not None:
            _call(self.telegram, "send", format_top100_signal(sig), signal_buttons(sig))
        if self.auto_execute:
            _call(self.execution, "buy", sig.symbol, sig.position_pct, meta)
        self.log(f"إشارة {sig.symbol} #{sig.rank} — {sig.score:.0f}/100 ({sig.regime})")

    def on_list_event(self, event, regime) -> None:
        _call(self.pattern_db, "record", "top100_list_event", [event.__dict__])
        if event.event == "ENTER":
            _call(self.position_manager, "start_tracking", event.symbol)

    def on_regime_change(self, result) -> None:
        # حالة السوق تُبلَّغ لبقية النظام لتعديل الأوزان — لا لفتح صفقات
        for name in ("set_market_regime", "on_regime"):
            if _call(self.capital_manager, name, result.regime, result.policy) is not None:
                break
        _call(self.pattern_db, "record", "regime_change", [result.as_dict()])
        self.log(f"حالة السوق الآن: {result.regime} ({result.score:.0f}/100)")

    def on_pattern(self, name, rows) -> None:
        _call(self.pattern_db, "record", name, rows)

    def log(self, msg: str) -> None:
        if _call(self.logger, "log", f"[TOP100] {msg}") is None:
            print(f"[TOP100] {msg}", flush=True)


def build_engine(cfg: Optional[Top100Config] = None, bridge: Optional[ApexV2Bridge] = None,
                 provider=None) -> Top100MarketEngine:
    """تهيئة جاهزة للاستخدام داخل APEX Ultimate V2."""
    cfg = cfg or Top100Config()
    return Top100MarketEngine(
        cfg=cfg, provider=provider, store=SnapshotStore(cfg.db_path),
        hooks=bridge or ApexV2Bridge(),
        telegram=TelegramSender(cfg.telegram_token, cfg.telegram_chat_id),
    )


def start_in_thread(engine: Top100MarketEngine):
    """تشغيل المحرك بخيط مستقل بجانب حلقة المسار الأول (Micro/New Tokens)."""
    import threading
    t = threading.Thread(target=engine.run_forever, name="top100-engine", daemon=True)
    t.start()
    return t
