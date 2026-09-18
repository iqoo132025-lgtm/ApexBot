#!/usr/bin/env python3
"""
مزوّد بيانات صناعي — للاختبار والتجربة بدون إنترنت.
يولّد كوناً من العملات بسلاسل أسعار قابلة للتحكم (صاعدة/هابطة/مسطّحة)
مع ترتيب Market Cap يتغيّر عبر الأيام، ودخول/خروج من Top 100.
"""
import math
import random
from typing import Dict, List, Optional

from .data_sources import CoinInfo, OHLCV


class MockDataProvider:
    def __init__(self, n: int = 30, days: int = 400, seed: int = 7, day_offset: int = 0):
        self.n = n
        self.days = days
        self.seed = seed
        self.day_offset = day_offset          # لمحاكاة مرور الأيام
        self.profiles: Dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        rnd = random.Random(self.seed)
        self.symbols = ["BTC", "ETH", "SOL", "SUI", "BNB", "XRP", "ADA", "AVAX", "LINK", "DOT"]
        while len(self.symbols) < self.n:
            self.symbols.append(f"T{len(self.symbols):02d}")
        kinds = ["up", "up_strong", "flat", "down", "choppy"]
        for i, s in enumerate(self.symbols):
            self.profiles[s] = "up" if s == "BTC" else ("up_strong" if s in ("SUI", "SOL") else kinds[i % len(kinds)])
        self._series: Dict[str, List[float]] = {s: self._make_series(s, rnd) for s in self.symbols}

    def _make_series(self, sym: str, rnd: random.Random) -> List[float]:
        kind = self.profiles[sym]
        base = {"BTC": 30000.0, "ETH": 2000.0}.get(sym, 5.0 + rnd.random() * 20)
        drift = {"up": 0.0022, "up_strong": 0.0045, "flat": 0.0002, "down": -0.0030, "choppy": 0.0005}[kind]
        noise = {"up": 0.012, "up_strong": 0.02, "flat": 0.008, "down": 0.015, "choppy": 0.03}[kind]
        r = random.Random(hash(sym) % 10000 + self.seed)
        out, p = [], base
        for i in range(self.days + self.day_offset):
            p *= (1 + drift + r.gauss(0, noise) + 0.004 * math.sin(i / 23.0))
            out.append(max(p, 0.0001))
        return out

    # ── واجهة المزوّد ──
    def fetch_top_markets(self, n: Optional[int] = None) -> List[CoinInfo]:
        n = n or self.n
        rows = []
        for s in self.symbols:
            series = self._series[s][:self.days + self.day_offset]
            price = series[-1]
            supply = {"BTC": 19_500_000, "ETH": 120_000_000}.get(s, 1_000_000_000 / (1 + self.symbols.index(s)))
            mcap = price * supply
            vol = mcap * (0.05 + 0.001 * self.symbols.index(s))
            # عملة تدخل Top 100 متأخرة: نخفي آخر عملة في الأيام الأولى
            if s == self.symbols[-1] and self.day_offset < 5:
                continue
            rows.append(CoinInfo(coin_id=s.lower(), symbol=s, name=s, rank=0, price=price,
                                 market_cap=mcap, volume_24h=vol,
                                 pct_30d=(price / series[-31] - 1) * 100 if len(series) > 31 else 0.0,
                                 ath=max(series), ath_change_pct=(price / max(series) - 1) * 100))
        rows.sort(key=lambda c: c.market_cap, reverse=True)
        rows = rows[:n]
        for i, c in enumerate(rows, 1):
            c.rank = i
        return rows

    def fetch_ohlcv(self, coin: CoinInfo, days: int = 400) -> Optional[OHLCV]:
        series = self._series.get(coin.symbol)
        if not series:
            return None
        closes = series[:self.days + self.day_offset][-days:]
        highs = [c * 1.015 for c in closes]
        lows = [c * 0.985 for c in closes]
        vols = []
        r = random.Random(hash(coin.symbol) % 997)
        for i, c in enumerate(closes):
            growth = 1.0 + (0.9 * i / max(1, len(closes)) if self.profiles[coin.symbol] in ("up", "up_strong") else 0.0)
            vols.append(c * 1_000_000 * growth * (0.8 + r.random() * 0.4))
        return OHLCV(symbol=coin.symbol, opens=closes[:], highs=highs, lows=lows,
                     closes=closes, volumes=vols, source="mock")

    def fetch_global(self) -> Dict[str, float]:
        return {"total_mcap": 2.4e12, "btc_dominance": 54.0, "eth_dominance": 12.0}

    def advance(self, days: int = 1) -> None:
        self.day_offset += days
