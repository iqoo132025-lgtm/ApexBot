#!/usr/bin/env python3
"""
طبقة البيانات لمحرك Top 100.

المصادر وترتيبها:
  • CoinGecko `/coins/markets`  → قائمة أكبر 100 عملة + الترتيب الحقيقي + الفوليوم + ATH.
  • Binance klines             → **المصدر الأساسي للشموع** لكل زوج USDT مدرج.
  • CoinGecko `/coins/{id}/ohlc` → بديل فقط للعملات غير المدرجة على Binance.
  • إغلاق يومي فقط             → آخر ملاذ، يُوسم `price_only` ولا تُبنى عليه إشارة.

لماذا هذا الترتيب صارم: الطبقة المجانية من CoinGecko ترد `429 Too Many Requests`
بعد عشرات الطلبات في الدقيقة، وسحب الشموع لمئة عملة منها يضرب الحد حتماً. لذلك:

  1. حصة CoinGecko في الدورة الواحدة **مقنَّنة** (`cg_max_calls_per_cycle`)
     ومُباعَدة زمنياً (`cg_min_interval_sec`)، وعند تكرار 429 يُفتح قاطع دائرة
     لبقية الدورة بدل الاستمرار في الطرق على الباب.
  2. **لا تُستخدم نسخة كاش قديمة بصمت.** كل جلب يعيد مصدره:
     `fresh` (من الشبكة)، `cache` (نسخة حديثة ضمن TTL)، `stale` (نسخة قديمة بعد فشل).
     البيانات `stale` تصل إلى التحليل موسومة، ولا تُطلق إشارة ولا تُدار ورقياً.
  3. كل دورة تُخرج تقريراً (`CycleReport`) بعدد fresh/cache/stale/price_only و429
     وأي عملة سقطت خارج الميزانية، حتى لا تبدو الدورة سليمة وهي تخدم بيانات قديمة.

كل الطلبات عبر urllib بلا تبعيات خارجية.
"""
import collections
import json
import os
import threading
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
    provenance: str = "fresh"  # fresh | cache | stale
    age_sec: float = 0.0       # عمر البيانة إن جاءت من الكاش

    @property
    def stale(self) -> bool:
        return self.provenance == "stale"

    def __len__(self) -> int:
        return len(self.closes)


@dataclass
class FetchResult:
    """ناتج طلب واحد مع مصدره — المصدر جزء من البيانة لا تفصيل داخلي."""
    data: object
    provenance: str            # fresh | cache | stale
    age_sec: float = 0.0


class RateLimitedError(RuntimeError):
    """الحد الأقصى للمصدر أُوقف عنده الطلب (429 متكرر أو نفاد ميزانية الدورة)."""


# ══════════════════════════════════════════
#  تقرير الدورة
# ══════════════════════════════════════════
@dataclass
class CycleReport:
    """
    ماذا جرى لبيانات هذه الدورة. يُقرأ قبل الوثوق بأي نتيجة تحليل.
    """
    binance_available: bool = True
    sources: collections.Counter = field(default_factory=collections.Counter)
    provenance: collections.Counter = field(default_factory=collections.Counter)
    http_errors: collections.Counter = field(default_factory=collections.Counter)
    cg_calls: int = 0
    cg_budget: int = 0
    cg_circuit_open: bool = False
    stale_symbols: List[str] = field(default_factory=list)
    price_only_symbols: List[str] = field(default_factory=list)
    skipped_symbols: List[str] = field(default_factory=list)   # نفدت الميزانية أو فشل المصدر

    def as_dict(self) -> dict:
        return {
            "binance_available": self.binance_available,
            "sources": dict(self.sources),
            "provenance": dict(self.provenance),
            "http_errors": dict(self.http_errors),
            "cg_calls": self.cg_calls,
            "cg_budget": self.cg_budget,
            "cg_circuit_open": self.cg_circuit_open,
            "stale": sorted(self.stale_symbols),
            "price_only": sorted(self.price_only_symbols),
            "skipped": sorted(self.skipped_symbols),
        }

    def summary(self) -> str:
        p = self.provenance
        lines = [
            f"مصادر الشموع: {dict(self.sources) or '—'}",
            f"أصل البيانة: fresh={p.get('fresh', 0)} cache={p.get('cache', 0)} "
            f"stale={p.get('stale', 0)}",
            f"CoinGecko: {self.cg_calls}/{self.cg_budget} طلب"
            + ("  ← القاطع مفتوح، أُوقفت الطلبات لبقية الدورة" if self.cg_circuit_open else ""),
            f"أخطاء HTTP: {dict(self.http_errors) or 'لا شيء'}",
        ]
        if not self.binance_available:
            lines.append("تحذير: Binance غير متاح — كل الشموع تحاول المرور عبر CoinGecko")
        if self.stale_symbols:
            lines.append(f"بيانات قديمة ({len(self.stale_symbols)}): "
                         f"{', '.join(sorted(self.stale_symbols))} ← لا إشارة منها")
        if self.price_only_symbols:
            lines.append(f"price_only ({len(self.price_only_symbols)}): "
                         f"{', '.join(sorted(self.price_only_symbols))}")
        if self.skipped_symbols:
            lines.append(f"بلا بيانات هذه الدورة ({len(self.skipped_symbols)}): "
                         f"{', '.join(sorted(self.skipped_symbols))}")
        return "\n".join(lines)

    @property
    def healthy(self) -> bool:
        """دورة يُعتمد عليها: لا 429، ولا قاطع مفتوح، ولا بيانات قديمة."""
        return (not self.cg_circuit_open
                and not self.stale_symbols
                and self.http_errors.get("429", 0) == 0)


# ══════════════════════════════════════════
#  مباعدة الطلبات
# ══════════════════════════════════════════
class RateLimiter:
    """يضمن مرور فاصل زمني أدنى بين طلبين لنفس المضيف."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            gap = time.time() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.time()


# ══════════════════════════════════════════
#  HTTP + كاش
# ══════════════════════════════════════════
class HttpCache:
    """
    كاش على القرص + مباعدة لكل مضيف + قاطع دائرة عند 429 متكرر.

    `fetch()` يعيد `FetchResult` يحمل مصدر البيانة. النسخة القديمة (`stale`)
    لا تُعاد إلا بطلب صريح `allow_stale=True`، فلا يحدث سقوط صامت عليها.
    """

    def __init__(self, cache_dir: str, timeout: int = 15, retries: int = 3,
                 limits: Optional[Dict[str, float]] = None,
                 max_consecutive_429: int = 2):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.max_consecutive_429 = max_consecutive_429
        self.limiters: Dict[str, RateLimiter] = {
            host: RateLimiter(interval) for host, interval in (limits or {}).items()
        }
        self.errors: collections.Counter = collections.Counter()
        self._consecutive_429: collections.Counter = collections.Counter()
        self.open_circuits: set = set()
        os.makedirs(cache_dir, exist_ok=True)

    # ── إدارة الدورة ──
    def begin_cycle(self) -> None:
        """تُستدعى في بداية كل دورة: تصفّر العدادات وتغلق القواطع المفتوحة."""
        self.errors.clear()
        self._consecutive_429.clear()
        self.open_circuits.clear()

    def host_of(self, url: str) -> str:
        return urllib.parse.urlparse(url).netloc or "?"

    def circuit_open(self, url: str) -> bool:
        return self.host_of(url) in self.open_circuits

    # ── الكاش ──
    def _path(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:180]
        return os.path.join(self.cache_dir, safe + ".json")

    def _read_cache(self, cache_key: str) -> Optional[FetchResult]:
        p = self._path(cache_key)
        if not os.path.exists(p):
            return None
        try:
            age = time.time() - os.path.getmtime(p)
            with open(p, "r", encoding="utf-8") as f:
                return FetchResult(json.load(f), "cache", age)
        except Exception:
            return None

    def _write_cache(self, cache_key: str, data) -> None:
        try:
            with open(self._path(cache_key), "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    # ── الطلب ──
    def fetch(self, url: str, cache_key: Optional[str] = None, ttl: int = 0,
              allow_stale: bool = False, max_stale_age: Optional[float] = None) -> FetchResult:
        if cache_key and ttl > 0:
            hit = self._read_cache(cache_key)
            if hit and hit.age_sec < ttl:
                return hit

        host = self.host_of(url)
        if host in self.open_circuits:
            self.errors["circuit_open"] += 1
            return self._stale_or_raise(cache_key, allow_stale, max_stale_age,
                                        RateLimitedError(f"القاطع مفتوح على {host}"))

        limiter = self.limiters.get(host)
        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            if limiter:
                limiter.wait()
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._consecutive_429[host] = 0
                if cache_key:
                    self._write_cache(cache_key, data)
                return FetchResult(data, "fresh", 0.0)
            except urllib.error.HTTPError as e:
                last_err = e
                self.errors[str(e.code)] += 1
                if e.code == 429:
                    self._consecutive_429[host] += 1
                    if self._consecutive_429[host] >= self.max_consecutive_429:
                        # لا فائدة من الاستمرار: المصدر يرفض، ويُترك لبقية الدورة
                        self.open_circuits.add(host)
                        break
                    time.sleep(self._retry_after(e, attempt))
                else:
                    time.sleep(min(2 ** attempt, 8))
            except Exception as e:
                last_err = e
                self.errors[type(e).__name__] += 1
                time.sleep(min(2 ** attempt, 8))

        return self._stale_or_raise(cache_key, allow_stale, max_stale_age,
                                    RuntimeError(f"طلب فشل: {url} ({last_err})"))

    @staticmethod
    def _retry_after(err: urllib.error.HTTPError, attempt: int) -> float:
        try:
            v = float(err.headers.get("Retry-After", ""))
            return min(max(v, 1.0), 30.0)
        except Exception:
            return min(5.0 * (attempt + 1), 30.0)

    def _stale_or_raise(self, cache_key, allow_stale, max_stale_age, err) -> FetchResult:
        if allow_stale and cache_key:
            hit = self._read_cache(cache_key)
            if hit and (max_stale_age is None or hit.age_sec <= max_stale_age):
                hit.provenance = "stale"
                return hit
        raise err

    def get_json(self, url: str, cache_key: Optional[str] = None, ttl: int = 0,
                 allow_stale: bool = False, max_stale_age: Optional[float] = None):
        """للنداءات التي لا تحتاج معرفة المصدر."""
        return self.fetch(url, cache_key, ttl, allow_stale, max_stale_age).data


# ══════════════════════════════════════════
#  المزوّد الحي
# ══════════════════════════════════════════
class LiveDataProvider:
    """المزوّد الافتراضي: Binance للشموع أولاً، وCoinGecko للترتيب وكبديل مقنَّن."""

    def __init__(self, cfg: Top100Config):
        self.cfg = cfg
        self.http = HttpCache(
            cfg.cache_dir, cfg.request_timeout, cfg.request_retries,
            limits={"api.binance.com": cfg.binance_min_interval_sec,
                    "api.coingecko.com": cfg.cg_min_interval_sec},
            max_consecutive_429=cfg.cg_max_consecutive_429,
        )
        self._binance_pairs: Optional[set] = None
        self.report = CycleReport(cg_budget=cfg.cg_max_calls_per_cycle)

    # ── حدود الدورة ──
    def begin_cycle(self) -> CycleReport:
        self.http.begin_cycle()
        self.report = CycleReport(cg_budget=self.cfg.cg_max_calls_per_cycle)
        return self.report

    def _cg_budget_left(self) -> int:
        return max(0, self.cfg.cg_max_calls_per_cycle - self.report.cg_calls)

    def _cg_fetch(self, url: str, cache_key: str, ttl: int,
                  allow_stale: bool = False) -> Optional[FetchResult]:
        """طلب CoinGecko محكوم بالميزانية والقاطع. يعيد None عند عدم السماح."""
        if self.http.circuit_open(url):
            self.report.cg_circuit_open = True
            return None
        if self._cg_budget_left() <= 0:
            return None
        self.report.cg_calls += 1
        try:
            return self.http.fetch(url, cache_key=cache_key, ttl=ttl,
                                   allow_stale=allow_stale,
                                   max_stale_age=self.cfg.stale_max_age_sec)
        except RateLimitedError:
            self.report.cg_circuit_open = True
            return None
        except Exception:
            return None
        finally:
            self.report.cg_circuit_open = (self.report.cg_circuit_open
                                           or self.http.circuit_open(url))
            self.report.http_errors = collections.Counter(self.http.errors)

    # ── قائمة Top 100 ──
    def fetch_top_markets(self, n: Optional[int] = None) -> List[CoinInfo]:
        """
        الكون = العملات التي ترتيبها الحقيقي في CoinGecko ضمن أول n.
        لا نحذف شيئاً من الكون ولا نعيد الترقيم: Stablecoins والمغلَّفة تبقى
        بترتيبها الصحيح، ويُكتفى بوسمها ليجري استبعادها من الإشارات لاحقاً.

        هذه القائمة أساس كل شيء، فيُسمح لها بالسقوط على نسخة قديمة عند الفشل —
        ويُسجَّل ذلك في تقرير الدورة بدل أن يمر بصمت.
        """
        n = n or self.cfg.universe_size
        per_page = min(250, max(n, 100))
        url = (f"{CG_BASE}/coins/markets?vs_currency=usd&order=market_cap_desc"
               f"&per_page={per_page}&page=1&sparkline=false"
               f"&price_change_percentage=24h,7d,30d")
        res = self._cg_fetch(url, "cg_markets_top",
                             ttl=max(60, self.cfg.universe_refresh_sec // 2),
                             allow_stale=True)
        if res is None:
            self.report.provenance["universe_failed"] += 1
            return []
        self.report.provenance[f"universe_{res.provenance}"] += 1
        rows = res.data
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
                                          cache_key="binance_exchange_info", ttl=86400,
                                          allow_stale=True,
                                          max_stale_age=self.cfg.stale_max_age_sec * 10)
                self._binance_pairs = {
                    s["symbol"] for s in data.get("symbols", [])
                    if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                }
            except Exception:
                self._binance_pairs = set()
        # Binance محجوب أو ساقط ⇒ كل العبء سينتقل إلى CoinGecko، وهذا يجب أن يُقال
        self.report.binance_available = bool(self._binance_pairs)
        return self._binance_pairs

    # ── الشموع ──
    def fetch_ohlcv(self, coin: CoinInfo, days: Optional[int] = None) -> Optional[OHLCV]:
        days = days or self.cfg.history_days
        out = self._fetch_ohlcv_inner(coin, days)
        if out is None:
            self.report.skipped_symbols.append(coin.symbol)
            return None
        self.report.sources[out.source] += 1
        self.report.provenance[out.provenance] += 1
        if out.stale:
            self.report.stale_symbols.append(coin.symbol)
        if out.price_only:
            self.report.price_only_symbols.append(coin.symbol)
        return out

    def _fetch_ohlcv_inner(self, coin: CoinInfo, days: int) -> Optional[OHLCV]:
        # المصدر الأساسي: Binance. شموع حقيقية بفوليوم، وبلا ضغط على حصة CoinGecko.
        if coin.pair in self._binance_symbols():
            try:
                limit = min(1000, days)
                url = f"{BINANCE_BASE}/api/v3/klines?symbol={coin.pair}&interval=1d&limit={limit}"
                res = self.http.fetch(url, cache_key=f"kl_{coin.pair}_1d",
                                      ttl=self.cfg.ohlcv_cache_ttl_sec, allow_stale=True,
                                      max_stale_age=self.cfg.stale_max_age_sec)
                rows = res.data
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
                        provenance=res.provenance,
                        age_sec=res.age_sec,
                    )
            except Exception:
                self.report.http_errors = collections.Counter(self.http.errors)

        # بديل 1: CoinGecko /ohlc — شموع حقيقية (High/Low فعلية) بلا فوليوم
        ohlc = self._coingecko_ohlc(coin, days)
        if ohlc:
            if self.cfg.cg_fetch_volumes:
                vols = self._coingecko_volumes(coin, days, len(ohlc.closes))
                if vols:
                    ohlc.volumes = vols
            return ohlc

        # بديل 2: أسعار يومية فقط — تُوسم price_only فلا تُبنى عليها ATR ولا دعم/مقاومة
        url = (f"{CG_BASE}/coins/{urllib.parse.quote(coin.coin_id)}/market_chart"
               f"?vs_currency=usd&days={min(days, 365)}&interval=daily")
        res = self._cg_fetch(url, f"cg_chart_{coin.coin_id}",
                             ttl=self.cfg.ohlcv_cache_ttl_sec, allow_stale=True)
        if res is None:
            return None
        data = res.data or {}
        prices = [p[1] for p in data.get("prices", [])]
        vols = [v[1] for v in data.get("total_volumes", [])]
        if len(prices) < 30:
            return None
        return OHLCV(
            symbol=coin.symbol,
            opens=prices[:], highs=prices[:], lows=prices[:], closes=prices[:],
            volumes=vols or [0.0] * len(prices),
            times=[int(p[0]) // 1000 for p in data.get("prices", [])],
            source="coingecko_price_only",
            price_only=True,
            provenance=res.provenance,
            age_sec=res.age_sec,
        )

    def _coingecko_ohlc(self, coin: CoinInfo, days: int) -> Optional[OHLCV]:
        """CoinGecko /coins/{id}/ohlc — يقبل 1/7/14/30/90/180/365 يوماً فقط."""
        allowed = [1, 7, 14, 30, 90, 180, 365]
        want = max([d for d in allowed if d <= days] or [30])
        url = (f"{CG_BASE}/coins/{urllib.parse.quote(coin.coin_id)}/ohlc"
               f"?vs_currency=usd&days={want}")
        res = self._cg_fetch(url, f"cg_ohlc_{coin.coin_id}_{want}",
                             ttl=self.cfg.ohlcv_cache_ttl_sec, allow_stale=True)
        if res is None:
            return None
        rows = res.data
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
            provenance=res.provenance,
            age_sec=res.age_sec,
        )

    def _coingecko_volumes(self, coin: CoinInfo, days: int, need: int) -> Optional[List[float]]:
        """فوليوم يومي من market_chart لإكمال شموع /ohlc — طلب إضافي، مطفأ افتراضياً."""
        url = (f"{CG_BASE}/coins/{urllib.parse.quote(coin.coin_id)}/market_chart"
               f"?vs_currency=usd&days={min(days, 365)}&interval=daily")
        res = self._cg_fetch(url, f"cg_chart_{coin.coin_id}",
                             ttl=self.cfg.ohlcv_cache_ttl_sec, allow_stale=True)
        if res is None:
            return None
        vols = [float(v[1]) for v in (res.data or {}).get("total_volumes", [])]
        if not vols:
            return None
        if len(vols) >= need:
            return vols[-need:]
        return [vols[0]] * (need - len(vols)) + vols

    def fetch_global(self) -> Dict[str, float]:
        """إجمالي القيمة السوقية وهيمنة BTC — مدخلات لكاشف حالة السوق."""
        res = self._cg_fetch(f"{CG_BASE}/global", "cg_global", ttl=1800, allow_stale=True)
        if res is None:
            return {}
        d = (res.data or {}).get("data", {})
        return {
            "total_mcap": float(d.get("total_market_cap", {}).get("usd", 0.0)),
            "total_volume": float(d.get("total_volume", {}).get("usd", 0.0)),
            "btc_dominance": float(d.get("market_cap_percentage", {}).get("btc", 0.0)),
            "eth_dominance": float(d.get("market_cap_percentage", {}).get("eth", 0.0)),
            "mcap_change_24h": float(d.get("market_cap_change_percentage_24h_usd", 0.0)),
        }
