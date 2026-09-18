#!/usr/bin/env python3
"""
ربط Top 100 Market Engine داخل APEX_ULTIMATE.py مباشرة.

سطر واحد داخل البوت:

    from apex_top100.apex_ultimate_hook import install_top100
    install_top100(CONFIG, state, log)

يعمل المحرك في خيط مستقل بجانب حلقة التداول، ولا يفتح أي صفقة بنفسه:
الإشارات تُخزَّن في state["top100"] وتُعرض في اللوحة وتُرسل إلى Telegram إن ضُبط التوكن.
"""
import os
import threading
import time
from typing import Optional

from .config import Top100Config
from .engine import Top100MarketEngine
from .integration import ApexV2Bridge
from .snapshots import SnapshotStore
from .telegram_signals import TelegramSender

MAX_KEPT_SIGNALS = 30
MAX_KEPT_EVENTS = 40


class _ApexUltimateCapital:
    """
    مدير رأس مال بسيط مبني على state الموجود في APEX_ULTIMATE:
    يوقف إشارات Top 100 عند تفعيل حماية الخسارة اليومية أو امتلاء المراكز.
    """

    def __init__(self, cfg, state, log):
        self.cfg, self.state, self.log = cfg, state, log
        self.regime = None
        self.policy = {}

    def approve_position(self, symbol, pct, meta) -> bool:
        if self.state.get("protected"):
            self.log(f"[TOP100] تجاهل {symbol}: حماية الخسارة اليومية مفعّلة", "warn")
            return False
        open_n = len(self.state.get("open_positions", {}))
        if open_n >= self.cfg.get("MAX_OPEN_POSITIONS", 2):
            self.log(f"[TOP100] تجاهل {symbol}: المراكز المفتوحة ممتلئة ({open_n})", "warn")
            return False
        if symbol.upper() + "USDT" in set(self.cfg.get("BANNED_SYMBOLS", [])):
            return False
        return True

    def set_market_regime(self, regime, policy) -> bool:
        self.regime, self.policy = regime, policy
        return True


class _ApexUltimatePositions:
    """يسجّل إشارات Top 100 في state لعرضها في اللوحة — بلا تنفيذ تلقائي."""

    def __init__(self, state):
        self.state = state

    def register_signal(self, symbol, meta) -> None:
        bucket = self.state.setdefault("top100", {"regime": None, "signals": [], "events": []})
        bucket["signals"].insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "symbol": symbol, "rank": meta.get("rank"), "score": meta.get("score"),
            "regime": meta.get("regime"), "trend": meta.get("trend_emoji"),
            "vs_btc": meta.get("vs_btc_pct"), "volume": meta.get("volume_state"),
            "momentum": meta.get("momentum_label"), "risk": meta.get("risk"),
            "entry_low": meta.get("entry_low"), "entry_high": meta.get("entry_high"),
            "invalidation": meta.get("invalidation"),
            "tp1": meta.get("tp1"), "tp2": meta.get("tp2"), "tp3": meta.get("tp3"),
            "position_pct": meta.get("position_pct"), "flags": meta.get("flags", []),
        })
        del bucket["signals"][MAX_KEPT_SIGNALS:]

    def start_tracking(self, symbol) -> None:
        bucket = self.state.setdefault("top100", {"regime": None, "signals": [], "events": []})
        bucket["events"].insert(0, {"time": time.strftime("%Y-%m-%d %H:%M"),
                                    "symbol": symbol, "event": "ENTER"})
        del bucket["events"][MAX_KEPT_EVENTS:]


class _ApexUltimateBridge(ApexV2Bridge):
    def __init__(self, state, log, **kw):
        super().__init__(**kw)
        self._state = state
        self._log = log

    def on_regime_change(self, result) -> None:
        super().on_regime_change(result)
        bucket = self._state.setdefault("top100", {"regime": None, "signals": [], "events": []})
        bucket["regime"] = {"regime": result.regime, "score": round(result.score, 1),
                            "changed_from": result.changed_from}

    def on_list_event(self, event, regime) -> None:
        super().on_list_event(event, regime)
        bucket = self._state.setdefault("top100", {"regime": None, "signals": [], "events": []})
        bucket["events"].insert(0, {"time": time.strftime("%Y-%m-%d %H:%M"),
                                    "symbol": event.symbol, "event": event.event,
                                    "rank": event.rank, "prev_rank": event.prev_rank})
        del bucket["events"][MAX_KEPT_EVENTS:]

    def log(self, msg: str) -> None:
        try:
            self._log(f"[TOP100] {msg}", "info")
        except Exception:
            print(f"[TOP100] {msg}", flush=True)


def install_top100(apex_config: dict, state: dict, log, start: bool = True,
                   provider=None) -> Optional[Top100MarketEngine]:
    """
    يبني المحرك ويشغّله في خيط مستقل.
    يقرأ إعداداته من CONFIG الخاص بـ APEX_ULTIMATE إن وُجدت، وإلا من القيم الافتراضية.
    توكن Telegram من متغيرات البيئة: TELEGRAM_TOKEN و TELEGRAM_CHAT_ID.
    """
    state.setdefault("top100", {"regime": None, "signals": [], "events": []})

    base_dir = os.path.dirname(os.path.abspath(__file__))
    cfg = Top100Config(
        universe_size=int(apex_config.get("TOP100_UNIVERSE", 100)),
        min_score=int(apex_config.get("TOP100_MIN_SCORE", 70)),
        scan_interval_sec=int(apex_config.get("TOP100_SCAN_MIN", 15)) * 60,
        db_path=os.path.join(os.path.dirname(base_dir), "apex_top100.db"),
        cache_dir=os.path.join(os.path.dirname(base_dir), ".apex_cache"),
        telegram_token=os.getenv("TELEGRAM_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )

    bridge = _ApexUltimateBridge(
        state, log,
        capital_manager=_ApexUltimateCapital(apex_config, state, log),
        position_manager=_ApexUltimatePositions(state),
        auto_execute=False,           # إشارات فقط — لا تنفيذ تلقائي على Top 100
    )

    engine = Top100MarketEngine(
        cfg=cfg, provider=provider, store=SnapshotStore(cfg.db_path), hooks=bridge,
        telegram=TelegramSender(cfg.telegram_token, cfg.telegram_chat_id),
    )

    if start:
        threading.Thread(target=engine.run_forever, name="top100-engine", daemon=True).start()
        try:
            log("🌐 Top 100 Market Engine يعمل بجانب مسار التداول", "success")
        except Exception:
            print("[TOP100] engine started", flush=True)
    return engine
