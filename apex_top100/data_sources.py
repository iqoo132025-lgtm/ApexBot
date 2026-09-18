#!/usr/bin/env python3
"""
طبقة البيانات لمحرك Top 100.

المصادر:
  • CoinGecko  → قائمة أكبر 100 عملة حسب Market Cap + الترتيب + الفوليوم + ATH.
  • Binance    → شموع يومية/أسبوعية للأزواج المدرجة (أسرع وأدق للتحليل الفني).
  • CoinGecko market_chart → بديل لأي عملة غير مدرجة على Binance.

كل الطلبات عبر urllib (بدون تبعيات خارجية) مع إعادة محاولة وكاش على القرص،
حتى لا نضرب حدود الـ rate limit في كل دورة.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Top100Config, STABLE_SYMBOLS, WRAPPED_SYMBOLS

CG_BASE = "https://api.coingecko.com/api/v3"
BINANCE_BASE = "https://api.binance.com"
USER_AGENT = "APEX-Ultimate-V2/Top100Engine"


# ══════════════════════════════════════════
#  نماذج البيانات
# ══════════════════════════════════════════
@dataclass
class CoinInfo:
    """
    صف واحد من قائمة Top 100.

    rank هو ترتيب CoinGecko الحقيقي (market_cap_rank) ولا يُعاد ترقيمه أبداً،
    حتى يبقى rank_history وحركة الترتيب وأحداث الدخول/الخروج صادقة.
    """
    coin_id: str
    symbol: str
    name: str
    rank: int
    price: float
    market_cap: float
    volume_24h: float
    pct_24h: Optional[float] = None
    pct_7d: Optional[float] = None
    pct_30d: Optional[float] = None
    ath: Optional[float] = None
    ath_change_pct: Optional[float] = None
    is_stablecoin: bool = False
    is_wrapped: bool = False

    @property
    def pair(self) -> str:
        return f"{self.symbol.upper()}USDT"

    def signalable(self, cfg: Optional[Top100Config] = None) -> bool:
        """هل تصلح لإطلاق إشارة تداول؟ (تبقى في الكون والتاريخ على أي حال)"""
        if cfg is None:
            return not (self.is_stablecoin or self.is_wrapped)
        if cfg.signal_exclude_stablecoins and self.is_stablecoin:
            return False
        if cfg.signal_exclude_wrapped and self.is_wrapped:
            return False
        return self.symbol.upper() not in {x.upper() for x in cfg.extra_signal_excluded}


@dataclass
class OHLCV:
    """شموع يومية (الأحدث آخراً)."""
    symbol: str
    opens: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    closes: List[float] = field(default_factory=list)
    volumes: List[float] = field(default_factory=list)
    times: List[int] = field(default_factory=list)   # زمن فتح كل شمعة (ثوانٍ)
    source: str = "binance"
    price_only: bool = False   # True = لا توجد High/Low حقيقية (سعر إغلاق فقط)

    def __len__(self) -> int:
        return len(self.closes)


# ══════════════════════════════════════════
#  HTTP + كاش
# ══════════════════════════════════════════
class HttpCache:
    def __init__(self, cache_dir: str, timeout: int = 15, retries: int = 3):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:180]
        return os.path.join(self.cache_dir, safe + ".json")

    def get_json(self, url: str, cache_key: Optional[str] = None, ttl: int = 0) -> Optional[dict]:
        if cache_key and ttl > 0:
            p = self._path(cache_key)
            try:
                if os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception:
                pass

        last_err = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if cache_key:
                    try:
                        with open(self._path(cache_key), "w", encoding="utf-8") as f:
                            json.dump(data, f)
                    except Exception:
                        pass
                return data
            except urllib.error.HTTPError as e:
                last_err = e
                # 429 = rate limit → انتظار تصاعدي
                time.sleep(2 ** attempt if e.code != 429 else 5 * (attempt + 1))
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)

        # فشل الشبكة: أعد آخر نسخة مخزّنة مهما كان عمرها بدل إسقاط الدورة كاملة
        if cache_key:
            p = self._path(cache_key)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
        raise RuntimeError(f"طلب فشل: {url} ({last_err})")


# ══════════════════════════════════════════
#  المزوّد الحي
# ══════════════════════════════════════════
class LiveDataProvider:
    """المزوّد الافتراضي: CoinGecko + Binance."""

    def __init__(self, cfg: Top100Config):
        self.cfg = cfg
        self.http = HttpCache(cfg.cache_dir, cfg.request_timeout, cfg.request_retries)
        self._binance_pairs: Optional[set] = None

    # ── قائمة Top 100 ──
    def fetch_top_markets(self, n: Optional[int] = None) -> List[CoinInfo]:
        """
        الكون = العملات التي ترتيبها الحقيقي في CoinGecko ضمن أول n.
        لا نحذف شيئاً من الكون ولا نعيد الترقيم: Stablecoins والمغلَّفة تبقى
        بترتيبها الصحيح، ويُكتفى بوسمها ليجري استبعادها من الإشارات لاحقاً.
        """
        n = n or self.cfg.universe_size
        per_page = min(250, max(n, 100))
        url = (f"{CG_BASE}/coins/markets?vs_currency=usd&order=market_cap_desc"
               f"&per_page={per_page}&page=1&sparkline=false"
               f"&price_change_percentage=24h,7d,30d")
        rows = self.http.get_json(url, cache_key="cg_markets_top", ttl=max(60, self.cfg.universe_refresh_sec // 2))
        coins: List[CoinInfo] = []
        for idx, row in enumerate(rows or [], start=1):
            sym = (row.get("symbol") or "").upper()
            if not row.get("market_cap"):
                continue
            rank = row.get("market_cap_rank") or idx   # الترتيب الحقيقي كما تعطيه CoinGecko
            if rank > n:
                continue
            coins.append(CoinInfo(
                coin_id=row.get("id", sym.lower()),
                symbol=sym,
                name=row.get("name", sym),
                rank=int(rank),
                price=float(row.get("current_price") or 0.0),
                market_cap=float(row.get("market_cap") or 0.0),
                volume_24h=float(row.get("total_volume") or 0.0),
                pct_24h=row.get("price_change_percentage_24h_in_currency"),
                pct_7d=row.get("price_change_percentage_7d_in_currency"),
                pct_30d=row.get("price_change_percentage_30d_in_currency"),
                ath=row.get("ath"),
                ath_change_pct=row.get("ath_change_percentage"),
                is_stablecoin=sym in STABLE_SYMBOLS,
                is_wrapped=sym in WRAPPED_SYMBOLS,
            ))
        coins.sort(key=lambda c: c.rank)
        return coins

    # ── الأزواج المتاحة على Binance ──
    def _binance_symbols(self) -> set:
        if self._binance_pairs is None:
            try:
                data = self.http.get_json(f"{BINANCE_BASE}/api/v3/exchangeInfo",
                                          cache_key="binance_exchange_info", ttl=86400)
                self._binance_pairs = {
                    s["symbol"] for s in data.get("symbols", [])
                    if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                }
            except Exception:
                self._binance_pairs = set()
        return self._binance_pairs

    # ── الشموع ──
    def fetch_ohlcv(self, coin: CoinInfo, days: Optional[int] = None) -> Optional[OHLCV]:
        days = days or self.cfg.history_days
        pair = coin.pair
        if pair in self._binance_symbols():
            try:
                limit = min(1000, days)
                url = f"{BINANCE_BASE}/api/v3/klines?symbol={pair}&interval=1d&limit={limit}"
                rows = self.http.get_json(url, cache_key=f"kl_{pair}_1d", ttl=3600)
                if rows:
                    return OHLCV(
                        symbol=coin.symbol,
                        opens=[float(r[1]) for r in rows],
                        highs=[float(r[2]) for r in rows],
                        lows=[float(r[3]) for r in rows],
                        closes=[float(r[4]) for r in rows],
                        volumes=[float(r[7]) for r in rows],   # quote volume = فوليوم بالدولار
                        times=[int(r[0]) // 1000 for r in rows],
                        source="binance",
                    )
            except Exception:
                pass
        # بديل 1: CoinGecko /ohlc — شموع حقيقية (High/Low فعلية) بلا فوليوم
        ohlc = self._coingecko_ohlc(coin, days)
        if ohlc:
            vols = self._coingecko_volumes(coin, days, len(ohlc.closes))
            if vols:
                ohlc.volumes = vols
            return ohlc

        # بديل 2: أسعار يومية فقط — تُوسم price_only فلا تُبنى عليها ATR ولا دعم/مقاومة
        try:
            url = (f"{CG_BASE}/coins/{urllib.parse.quote(coin.coin_id)}/market_chart"
                   f"?vs_currency=usd&days={min(days, 365)}&interval=daily")
            data = self.http.get_json(url, cache_key=f"cg_chart_{coin.coin_id}", ttl=3600)
            prices = [p[1] for p in (data or {}).get("prices", [])]
            vols = [v[1] for v in (data or {}).get("total_volumes", [])]
            if len(prices) < 30:
                return None
            return OHLCV(
                symbol=coin.symbol,
                opens=prices[:], highs=prices[:], lows=prices[:], closes=prices[:],
                volumes=vols or [0.0] * len(prices),
                times=[int(p[0]) // 1000 for p in (data or {}).get("prices", [])],
                source="coingecko_price_only",
                price_only=True,
            )
        except Exception:
            return None

    def _coingecko_ohlc(self, coin: CoinInfo, days: int) -> Optional[OHLCV]:
        """CoinGecko /coins/{id}/ohlc — يقبل 1/7/14/30/90/180/365 يوماً فقط."""
        allowed = [1, 7, 14, 30, 90, 180, 365]
        want = max([d for d in allowed if d <= days] or [30])
        try:
            url = (f"{CG_BASE}/coins/{urllib.parse.quote(coin.coin_id)}/ohlc"
                   f"?vs_currency=usd&days={want}")
            rows = self.http.get_json(url, cache_key=f"cg_ohlc_{coin.coin_id}_{want}", ttl=3600)
            if not rows or len(rows) < 30:
                return None
            return OHLCV(
                symbol=coin.symbol,
                opens=[float(r[1]) for r in rows],
                highs=[float(r[2]) for r in rows],
                lows=[float(r[3]) for r in rows],
                closes=[float(r[4]) for r in rows],
                volumes=[0.0] * len(rows),
                times=[int(r[0]) // 1000 for r in rows],
                source="coingecko_ohlc",
                price_only=False,
            )
        except Exception:
            return None

    def _coingecko_volumes(self, coin: CoinInfo, days: int, need: int) -> Optional[List[float]]:
        """فوليوم يومي من market_chart لإكمال شموع /ohlc."""
        try:
            url = (f"{CG_BASE}/coins/{urllib.parse.quote(coin.coin_id)}/market_chart"
                   f"?vs_currency=usd&days={min(days, 365)}&interval=daily")
            data = self.http.get_json(url, cache_key=f"cg_chart_{coin.coin_id}", ttl=3600)
            vols = [float(v[1]) for v in (data or {}).get("total_volumes", [])]
            if not vols:
                return None
            if len(vols) >= need:
                return vols[-need:]
            return [vols[0]] * (need - len(vols)) + vols
        except Exception:
            return None

    def fetch_global(self) -> Dict[str, float]:
        """إجمالي القيمة السوقية وهيمنة BTC — مدخلات لكاشف حالة السوق."""
        try:
            data = self.http.get_json(f"{CG_BASE}/global", cache_key="cg_global", ttl=1800)
            d = (data or {}).get("data", {})
            return {
                "total_mcap": float(d.get("total_market_cap", {}).get("usd", 0.0)),
                "total_volume": float(d.get("total_volume", {}).get("usd", 0.0)),
                "btc_dominance": float(d.get("market_cap_percentage", {}).get("btc", 0.0)),
                "eth_dominance": float(d.get("market_cap_percentage", {}).get("eth", 0.0)),
                "mcap_change_24h": float(d.get("market_cap_change_percentage_24h_usd", 0.0)),
            }
        except Exception:
            return {}
