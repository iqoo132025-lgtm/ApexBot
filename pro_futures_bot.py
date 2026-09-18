"""
╔══════════════════════════════════════════════════════════════════╗
║           PROFESSIONAL FUTURES TRADING BOT - FULL SYSTEM        ║
║           Binance USDT-M Futures | Python 3.10+                 ║
╚══════════════════════════════════════════════════════════════════╝

ARCHITECTURE:
  DataCollector → StrategyEngine → RiskManager → ExecutionEngine → Monitor

DISCLAIMER:
  هذا الكود للأغراض التعليمية فقط.
  التداول بالرافعة المالية ينطوي على مخاطر عالية جداً.
  لا تستخدم أموالاً لا تستطيع تحمل خسارتها.

REQUIREMENTS:
  pip install python-binance pandas numpy ta requests python-telegram-bot

USAGE:
  1. أضف API Keys في ملف config.json
  2. شغّل: python pro_futures_bot.py --mode paper   (ورقي)
  3. شغّل: python pro_futures_bot.py --mode live    (حقيقي — بحذر)
"""

import os
import json
import time
import logging
import argparse
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import pandas as pd
import numpy as np

# ─── External Libraries (install via pip) ────────────────────────────────────
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
    import ta  # pip install ta
    import requests
    BINANCE_AVAILABLE = True
except ImportError:
    BINANCE_AVAILABLE = False
    print("[WARNING] python-binance or ta not installed. Running in SIMULATION mode.")

# ═══════════════════════════════════════════════════════════════════
# 0. CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

CONFIG = {
    # Binance API (استخدم مفاتيح Testnet للاختبار الآمن)
    "api_key": os.getenv("BINANCE_API_KEY", ""),
    "api_secret": os.getenv("BINANCE_API_SECRET", ""),
    "testnet": False,                 # True = Testnet (آمن) | False = Real Money

    # Symbol & Timeframe
    "symbol": "BTCUSDT",
    "interval": "15m",               # 1m, 5m, 15m, 1h, 4h, 1d
    "leverage": 5,                   # الرافعة المالية (1-20 موصى به)

    # Risk Management — أهم قسم في البوت
    "capital_usdt": 1000.0,          # رأس المال الكلي بـ USDT
    "risk_per_trade_pct": 1.0,       # نسبة المخاطرة لكل صفقة (%)
    "max_open_positions": 1,         # أقصى عدد صفقات مفتوحة في نفس الوقت
    "max_daily_loss_pct": 3.0,       # إيقاف البوت إذا خسر هذه النسبة يومياً

    # Stop Loss / Take Profit (% من سعر الدخول)
    "stop_loss_pct": 1.5,            # وقف الخسارة
    "take_profit_pct": 3.0,          # هدف الربح
    "trailing_stop_pct": 0.8,        # Trailing Stop (None لإيقافه)

    # Strategy Parameters — EMA Crossover + RSI Filter
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,

    # Telegram Alerts (اختياري)
    "telegram_token": None,          # "YOUR_BOT_TOKEN"
    "telegram_chat_id": None,        # "YOUR_CHAT_ID"

    # Logging
    "log_file": "bot_trades.log",
    "log_level": "INFO",
}


# ═══════════════════════════════════════════════════════════════════
# 1. LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════

def setup_logger(config: dict) -> logging.Logger:
    logger = logging.getLogger("FuturesBot")
    logger.setLevel(getattr(logging, config["log_level"]))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler
    fh = logging.FileHandler(config["log_file"])
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


# ═══════════════════════════════════════════════════════════════════
# 2. DATA MODELS
# ═══════════════════════════════════════════════════════════════════

class Side(Enum):
    LONG  = "BUY"
    SHORT = "SELL"


class PositionStatus(Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class Signal:
    """نتيجة قرار الاستراتيجية"""
    side: Optional[Side] = None          # LONG / SHORT / None (لا تداول)
    confidence: float = 0.0             # 0.0 → 1.0
    reason: str = ""

    def has_signal(self) -> bool:
        return self.side is not None


@dataclass
class Position:
    """صفقة مفتوحة"""
    symbol: str
    side: Side
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    trailing_stop_pct: Optional[float]
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    highest_price: float = 0.0          # لحساب Trailing Stop
    status: PositionStatus = PositionStatus.OPEN
    pnl_usdt: float = 0.0
    order_id: Optional[str] = None


@dataclass
class TradeRecord:
    """سجل صفقة مغلقة"""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    pnl_pct: float
    duration_seconds: float
    closed_at: datetime
    reason: str                         # "SL" | "TP" | "TRAILING" | "MANUAL"


# ═══════════════════════════════════════════════════════════════════
# 3. DATA COLLECTOR
# ═══════════════════════════════════════════════════════════════════

class DataCollector:
    """
    يجمع بيانات OHLCV من Binance Futures
    ويعيدها كـ DataFrame جاهزة للتحليل
    """

    def __init__(self, client, config: dict, logger: logging.Logger):
        self.client  = client
        self.symbol  = config["symbol"]
        self.interval = config["interval"]
        self.logger  = logger

    def get_klines(self, limit: int = 200) -> Optional[pd.DataFrame]:
        """جلب الشموع اليابانية"""
        try:
            if not BINANCE_AVAILABLE or self.client is None:
                return self._simulate_klines(limit)

            raw = self.client.futures_klines(
                symbol=self.symbol,
                interval=self.interval,
                limit=limit
            )
            df = pd.DataFrame(raw, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore"
            ])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            return df

        except Exception as e:
            self.logger.error(f"DataCollector error: {e}")
            return None

    def get_current_price(self) -> Optional[float]:
        """السعر الحالي"""
        try:
            if not BINANCE_AVAILABLE or self.client is None:
                return self._simulate_price()
            ticker = self.client.futures_symbol_ticker(symbol=self.symbol)
            return float(ticker["price"])
        except Exception as e:
            self.logger.error(f"Price fetch error: {e}")
            return None

    # ── Simulation (for testing without API) ──────────────────────

    def _simulate_klines(self, limit: int) -> pd.DataFrame:
        """بيانات محاكاة للاختبار بدون API"""
        np.random.seed(42)
        dates = pd.date_range(end=datetime.now(), periods=limit, freq="15min")
        price = 43000 + np.cumsum(np.random.randn(limit) * 200)
        df = pd.DataFrame({
            "open":   price,
            "high":   price + np.random.uniform(50, 300, limit),
            "low":    price - np.random.uniform(50, 300, limit),
            "close":  price + np.random.randn(limit) * 100,
            "volume": np.random.uniform(100, 1000, limit),
        }, index=dates)
        return df

    def _simulate_price(self) -> float:
        return 43000 + np.random.randn() * 100


# ═══════════════════════════════════════════════════════════════════
# 4. STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════════════

class StrategyEngine:
    """
    استراتيجية: EMA Crossover + RSI Filter
    ─────────────────────────────────────
    LONG  : EMA20 يقطع فوق EMA50 + RSI < 70
    SHORT : EMA20 يقطع تحت EMA50 + RSI > 30

    يمكنك استبدال هذا القسم بأي استراتيجية أخرى
    طالما تُرجع Signal object
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.ema_fast = config["ema_fast"]
        self.ema_slow = config["ema_slow"]
        self.rsi_period = config["rsi_period"]
        self.rsi_oversold = config["rsi_oversold"]
        self.rsi_overbought = config["rsi_overbought"]
        self.logger = logger

    def analyze(self, df: pd.DataFrame) -> Signal:
        if df is None or len(df) < self.ema_slow + 10:
            return Signal(reason="Insufficient data")

        df = df.copy()

        # ── حساب المؤشرات ────────────────────────────────────────
        df["ema_fast"] = df["close"].ewm(span=self.ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.ema_slow, adjust=False).mean()

        # RSI
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).ewm(span=self.rsi_period, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=self.rsi_period, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

        # ── قراءة آخر شمعتين ─────────────────────────────────────
        prev = df.iloc[-2]
        curr = df.iloc[-1]

        rsi_now     = curr["rsi"]
        cross_up    = (prev["ema_fast"] <= prev["ema_slow"]) and (curr["ema_fast"] > curr["ema_slow"])
        cross_down  = (prev["ema_fast"] >= prev["ema_slow"]) and (curr["ema_fast"] < curr["ema_slow"])

        self.logger.debug(
            f"EMA({self.ema_fast})={curr['ema_fast']:.2f} | "
            f"EMA({self.ema_slow})={curr['ema_slow']:.2f} | "
            f"RSI={rsi_now:.1f}"
        )

        # ── قرار الدخول ─────────────────────────────────────────
        if cross_up and rsi_now < self.rsi_overbought:
            return Signal(
                side=Side.LONG,
                confidence=min(1.0, (self.rsi_overbought - rsi_now) / 40),
                reason=f"EMA cross UP | RSI={rsi_now:.1f}"
            )

        if cross_down and rsi_now > self.rsi_oversold:
            return Signal(
                side=Side.SHORT,
                confidence=min(1.0, (rsi_now - self.rsi_oversold) / 40),
                reason=f"EMA cross DOWN | RSI={rsi_now:.1f}"
            )

        return Signal(reason=f"No signal | RSI={rsi_now:.1f}")


# ═══════════════════════════════════════════════════════════════════
# 5. RISK MANAGER
# ═══════════════════════════════════════════════════════════════════

class RiskManager:
    """
    يتحكم في:
    - حجم الصفقة (Position Sizing)
    - وقف الخسارة والهدف
    - الحد اليومي للخسارة
    - الحد الأقصى للصفقات المفتوحة
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.capital         = config["capital_usdt"]
        self.risk_pct        = config["risk_per_trade_pct"] / 100
        self.sl_pct          = config["stop_loss_pct"] / 100
        self.tp_pct          = config["take_profit_pct"] / 100
        self.trailing_pct    = config.get("trailing_stop_pct")
        self.max_positions   = config["max_open_positions"]
        self.max_daily_loss  = config["max_daily_loss_pct"] / 100
        self.leverage        = config["leverage"]
        self.logger          = logger

        self.daily_loss_usdt    = 0.0
        self.daily_loss_reset   = datetime.now(timezone.utc).date()

    def can_open_position(self, open_positions: list) -> tuple[bool, str]:
        """هل يُسمح بفتح صفقة جديدة؟"""
        self._check_day_reset()

        if len(open_positions) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"

        daily_loss_pct = self.daily_loss_usdt / self.capital
        if daily_loss_pct >= self.max_daily_loss:
            return False, f"Daily loss limit hit ({daily_loss_pct*100:.1f}%)"

        return True, "OK"

    def calculate_position(self, entry_price: float, side: Side) -> dict:
        """حساب حجم الصفقة ومستويات SL/TP"""
        risk_usdt   = self.capital * self.risk_pct
        sl_distance = entry_price * self.sl_pct

        # Quantity: كم عملة نشتري
        quantity    = risk_usdt / sl_distance
        notional    = quantity * entry_price

        if side == Side.LONG:
            stop_loss   = entry_price * (1 - self.sl_pct)
            take_profit = entry_price * (1 + self.tp_pct)
        else:
            stop_loss   = entry_price * (1 + self.sl_pct)
            take_profit = entry_price * (1 - self.tp_pct)

        self.logger.info(
            f"Position Sizing | Entry={entry_price:.2f} | "
            f"Qty={quantity:.6f} | Notional={notional:.2f} USDT | "
            f"Risk={risk_usdt:.2f} USDT | SL={stop_loss:.2f} | TP={take_profit:.2f}"
        )

        return {
            "quantity":    round(quantity, 6),
            "notional":    notional,
            "risk_usdt":   risk_usdt,
            "stop_loss":   round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "trailing_stop_pct": self.trailing_pct,
        }

    def check_exit(self, position: Position, current_price: float) -> Optional[str]:
        """هل يجب إغلاق الصفقة؟ يُرجع السبب أو None"""
        if position.side == Side.LONG:
            # Trailing Stop
            if self.trailing_pct and current_price > position.highest_price:
                position.highest_price = current_price

            if self.trailing_pct and position.highest_price > 0:
                trailing_sl = position.highest_price * (1 - self.trailing_pct / 100)
                if current_price <= trailing_sl:
                    return "TRAILING"

            if current_price <= position.stop_loss:
                return "SL"
            if current_price >= position.take_profit:
                return "TP"

        elif position.side == Side.SHORT:
            if self.trailing_pct and current_price < position.highest_price:
                position.highest_price = current_price

            if self.trailing_pct and position.highest_price > 0:
                trailing_sl = position.highest_price * (1 + self.trailing_pct / 100)
                if current_price >= trailing_sl:
                    return "TRAILING"

            if current_price >= position.stop_loss:
                return "SL"
            if current_price <= position.take_profit:
                return "TP"

        return None

    def record_loss(self, loss_usdt: float):
        self._check_day_reset()
        if loss_usdt < 0:
            self.daily_loss_usdt += abs(loss_usdt)

    def _check_day_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self.daily_loss_reset:
            self.daily_loss_usdt = 0.0
            self.daily_loss_reset = today


# ═══════════════════════════════════════════════════════════════════
# 6. EXECUTION ENGINE
# ═══════════════════════════════════════════════════════════════════

class ExecutionEngine:
    """
    ينفذ الأوامر على Binance Futures API
    أو يحاكيها في وضع Paper Trading
    """

    def __init__(self, client, config: dict, logger: logging.Logger):
        self.client   = client
        self.symbol   = config["symbol"]
        self.leverage = config["leverage"]
        self.testnet  = config["testnet"]
        self.logger   = logger

        # Paper trading state
        self.paper_balance = config["capital_usdt"]
        self.paper_trades  = 0

    def open_position(self, side: Side, quantity: float,
                      stop_loss: float, take_profit: float,
                      paper_mode: bool = True) -> Optional[str]:
        """فتح صفقة — يُرجع order_id"""
        if paper_mode:
            self.paper_trades += 1
            order_id = f"PAPER_{self.paper_trades}_{int(time.time())}"
            self.logger.info(
                f"[PAPER] OPEN {side.value} {quantity} {self.symbol} | "
                f"SL={stop_loss} | TP={take_profit} | ID={order_id}"
            )
            return order_id

        # Live mode
        try:
            # ضبط الرافعة المالية
            self.client.futures_change_leverage(
                symbol=self.symbol, leverage=self.leverage
            )

            # أمر السوق
            order = self.client.futures_create_order(
                symbol=self.symbol,
                side=side.value,
                type="MARKET",
                quantity=quantity
            )
            order_id = str(order["orderId"])
            self.logger.info(f"[LIVE] OPEN {side.value} | ID={order_id}")

            # أمر وقف الخسارة
            sl_side = "SELL" if side == Side.LONG else "BUY"
            self.client.futures_create_order(
                symbol=self.symbol,
                side=sl_side,
                type="STOP_MARKET",
                stopPrice=stop_loss,
                closePosition=True
            )

            # أمر هدف الربح
            self.client.futures_create_order(
                symbol=self.symbol,
                side=sl_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=take_profit,
                closePosition=True
            )

            return order_id

        except BinanceAPIException as e:
            self.logger.error(f"Open position error: {e}")
            return None

    def close_position(self, position: Position, current_price: float,
                       reason: str, paper_mode: bool = True) -> float:
        """إغلاق صفقة — يُرجع PnL بالـ USDT"""
        if position.side == Side.LONG:
            pnl = (current_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - current_price) * position.quantity

        if paper_mode:
            self.paper_balance += pnl
            self.logger.info(
                f"[PAPER] CLOSE {position.side.value} | "
                f"PnL={pnl:+.2f} USDT | Reason={reason} | "
                f"Balance={self.paper_balance:.2f}"
            )
            return pnl

        try:
            close_side = "SELL" if position.side == Side.LONG else "BUY"
            self.client.futures_create_order(
                symbol=self.symbol,
                side=close_side,
                type="MARKET",
                quantity=position.quantity,
                reduceOnly=True
            )
            self.logger.info(f"[LIVE] CLOSE | PnL={pnl:+.2f} USDT | Reason={reason}")
            return pnl

        except BinanceAPIException as e:
            self.logger.error(f"Close position error: {e}")
            return 0.0


# ═══════════════════════════════════════════════════════════════════
# 7. MONITORING & ALERTS
# ═══════════════════════════════════════════════════════════════════

class Monitor:
    """إرسال تنبيهات Telegram + عرض إحصائيات"""

    def __init__(self, config: dict, logger: logging.Logger):
        self.token   = config.get("telegram_token")
        self.chat_id = config.get("telegram_chat_id")
        self.logger  = logger
        self.trade_log: list[TradeRecord] = []

    def send_telegram(self, message: str):
        if not self.token or not self.chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=5)
        except Exception as e:
            self.logger.warning(f"Telegram error: {e}")

    def log_trade(self, record: TradeRecord):
        self.trade_log.append(record)
        emoji = "✅" if record.pnl_usdt > 0 else "❌"
        msg = (
            f"{emoji} <b>Trade Closed</b>\n"
            f"Symbol: {record.symbol}\n"
            f"Side: {record.side}\n"
            f"Entry: {record.entry_price:.2f}\n"
            f"Exit: {record.exit_price:.2f}\n"
            f"PnL: {record.pnl_usdt:+.2f} USDT ({record.pnl_pct:+.2f}%)\n"
            f"Reason: {record.reason}"
        )
        self.logger.info(f"Trade closed: {record.side} PnL={record.pnl_usdt:+.2f}")
        self.send_telegram(msg)

    def print_stats(self):
        """طباعة إحصائيات الأداء في الـ Terminal"""
        if not self.trade_log:
            print("\n📊 No trades yet.\n")
            return

        df = pd.DataFrame([vars(t) for t in self.trade_log])
        total     = len(df)
        wins      = len(df[df["pnl_usdt"] > 0])
        losses    = len(df[df["pnl_usdt"] <= 0])
        win_rate  = wins / total * 100 if total > 0 else 0
        total_pnl = df["pnl_usdt"].sum()
        avg_win   = df[df["pnl_usdt"] > 0]["pnl_usdt"].mean() if wins > 0 else 0
        avg_loss  = df[df["pnl_usdt"] <= 0]["pnl_usdt"].mean() if losses > 0 else 0
        max_dd    = df["pnl_usdt"].cumsum().min()

        # Sharpe (تقريبي)
        returns = df["pnl_pct"] / 100
        sharpe  = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

        print("\n" + "═" * 50)
        print("         📊 PERFORMANCE STATISTICS")
        print("═" * 50)
        print(f"  Total Trades   : {total}")
        print(f"  Win Rate       : {win_rate:.1f}%  ({wins}W / {losses}L)")
        print(f"  Total PnL      : {total_pnl:+.2f} USDT")
        print(f"  Avg Win        : {avg_win:+.2f} USDT")
        print(f"  Avg Loss       : {avg_loss:+.2f} USDT")
        print(f"  Max Drawdown   : {max_dd:.2f} USDT")
        print(f"  Sharpe Ratio   : {sharpe:.2f}")
        print("═" * 50 + "\n")


# ═══════════════════════════════════════════════════════════════════
# 8. MAIN BOT ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════

class FuturesBot:
    """
    ينسّق بين جميع الطبقات:
    DataCollector → StrategyEngine → RiskManager → ExecutionEngine → Monitor
    """

    def __init__(self, config: dict, paper_mode: bool = True):
        self.config     = config
        self.paper_mode = paper_mode
        self.running    = False

        # Setup logger
        self.logger = setup_logger(config)

        # Init Binance client
        self.client = self._init_client()

        # Init layers
        self.data_collector  = DataCollector(self.client, config, self.logger)
        self.strategy_engine = StrategyEngine(config, self.logger)
        self.risk_manager    = RiskManager(config, self.logger)
        self.execution       = ExecutionEngine(self.client, config, self.logger)
        self.monitor         = Monitor(config, self.logger)

        # State
        self.open_positions: list[Position] = []

        mode_str = "📄 PAPER TRADING" if paper_mode else "💰 LIVE TRADING ⚠️"
        self.logger.info(f"Bot initialized | Mode: {mode_str} | {config['symbol']} {config['interval']}")
        self.monitor.send_telegram(f"🤖 Bot started | {mode_str} | {config['symbol']}")

    def _init_client(self):
        if not BINANCE_AVAILABLE:
            return None
        try:
            client = Client(
                self.config["api_key"],
                self.config["api_secret"],
                testnet=self.config["testnet"]
            )
            client.ping()
            self.logger.info("Binance connection OK")
            return client
        except Exception as e:
            self.logger.warning(f"Binance connection failed: {e} — using simulation")
            return None

    # ─── Main Loop ──────────────────────────────────────────────

    def run(self, interval_seconds: int = 60):
        """الحلقة الرئيسية للبوت"""
        self.running = True
        self.logger.info(f"Starting main loop (interval={interval_seconds}s)")

        try:
            while self.running:
                self._tick()
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user")
        finally:
            self.monitor.print_stats()
            self.monitor.send_telegram("🛑 Bot stopped")

    def _tick(self):
        """دورة واحدة: تحليل → قرار → تنفيذ"""

        # 1. جمع البيانات
        df = self.data_collector.get_klines()
        if df is None:
            self.logger.warning("Failed to get klines, skipping tick")
            return

        current_price = self.data_collector.get_current_price()
        if current_price is None:
            return

        self.logger.debug(f"Price: {current_price:.2f}")

        # 2. فحص الصفقات المفتوحة (SL/TP/Trailing)
        self._check_open_positions(current_price)

        # 3. تحليل الاستراتيجية
        signal = self.strategy_engine.analyze(df)

        if signal.has_signal():
            self.logger.info(f"Signal: {signal.side.value} | {signal.reason} | Confidence={signal.confidence:.2f}")

            # 4. فحص إدارة المخاطر
            can_open, reason = self.risk_manager.can_open_position(self.open_positions)

            if can_open:
                pos_params = self.risk_manager.calculate_position(current_price, signal.side)
                self._open_position(signal, current_price, pos_params)
            else:
                self.logger.info(f"Position blocked: {reason}")
        else:
            self.logger.debug(f"No signal: {signal.reason}")

    def _open_position(self, signal: Signal, entry_price: float, params: dict):
        """فتح صفقة جديدة"""
        order_id = self.execution.open_position(
            side=signal.side,
            quantity=params["quantity"],
            stop_loss=params["stop_loss"],
            take_profit=params["take_profit"],
            paper_mode=self.paper_mode
        )

        if order_id:
            position = Position(
                symbol=self.config["symbol"],
                side=signal.side,
                entry_price=entry_price,
                quantity=params["quantity"],
                stop_loss=params["stop_loss"],
                take_profit=params["take_profit"],
                trailing_stop_pct=params["trailing_stop_pct"],
                highest_price=entry_price,
                order_id=order_id
            )
            self.open_positions.append(position)

            self.monitor.send_telegram(
                f"📈 Position Opened\n"
                f"{signal.side.value} {self.config['symbol']}\n"
                f"Entry: {entry_price:.2f}\n"
                f"SL: {params['stop_loss']:.2f} | TP: {params['take_profit']:.2f}\n"
                f"Risk: {params['risk_usdt']:.2f} USDT"
            )

    def _check_open_positions(self, current_price: float):
        """فحص كل صفقة مفتوحة"""
        positions_to_remove = []

        for position in self.open_positions:
            exit_reason = self.risk_manager.check_exit(position, current_price)

            if exit_reason:
                pnl = self.execution.close_position(
                    position, current_price, exit_reason, self.paper_mode
                )

                pnl_pct = (pnl / (position.entry_price * position.quantity)) * 100
                duration = (datetime.now(timezone.utc) - position.opened_at).total_seconds()

                record = TradeRecord(
                    symbol=position.symbol,
                    side=position.side.value,
                    entry_price=position.entry_price,
                    exit_price=current_price,
                    quantity=position.quantity,
                    pnl_usdt=pnl,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    closed_at=datetime.now(timezone.utc),
                    reason=exit_reason
                )
                self.monitor.log_trade(record)
                self.risk_manager.record_loss(pnl)
                positions_to_remove.append(position)

        for p in positions_to_remove:
            self.open_positions.remove(p)

    def stop(self):
        self.running = False


# ═══════════════════════════════════════════════════════════════════
# 9. ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Professional Futures Trading Bot")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="paper = تداول ورقي آمن | live = حقيقي (بحذر شديد)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="فترة التحديث بالثواني (default: 60)"
    )
    args = parser.parse_args()

    if args.mode == "live":
        print("\n⚠️  تحذير: أنت على وشك تشغيل البوت بأموال حقيقية!")
        print("    تأكد من:")
        print("    ✓ اختبرت الاستراتيجية بوضع Paper Trading لأسابيع")
        print("    ✓ api_key و api_secret صحيحان")
        print("    ✓ testnet = False في CONFIG")
        confirm = input("\n    اكتب 'YES' للمتابعة: ")
        if confirm.strip() != "YES":
            print("    إلغاء.")
            return

    paper_mode = (args.mode == "paper")
    bot = FuturesBot(CONFIG, paper_mode=paper_mode)
    bot.run(interval_seconds=args.interval)


if __name__ == "__main__":
    main()
