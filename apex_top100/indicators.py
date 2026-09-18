#!/usr/bin/env python3
"""مؤشرات تحليلية خفيفة (stdlib فقط) — مشتركة بين محرك Top 100 وكاشف حالة السوق."""
import math
from typing import List, Optional, Sequence, Tuple


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if not values or len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def ema(values: Sequence[float], n: int) -> Optional[float]:
    if not values or len(values) < n or n <= 0:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: Sequence[float], n: int = 14) -> Optional[float]:
    if len(values) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def roc(values: Sequence[float], n: int) -> Optional[float]:
    """نسبة التغير % عبر n شمعة."""
    if len(values) < n + 1 or values[-n - 1] == 0:
        return None
    return (values[-1] / values[-n - 1] - 1.0) * 100.0


def atr_pct(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Optional[float]:
    """ATR كنسبة مئوية من السعر."""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    a = sum(trs[-n:]) / n
    price = closes[-1]
    return (a / price * 100.0) if price else None


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def returns(values: Sequence[float]) -> List[float]:
    out = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        out.append((values[i] / prev - 1.0) if prev else 0.0)
    return out


def correlation(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """معامل ارتباط بيرسون بين سلسلتَي عوائد."""
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = list(a[-n:]), list(b[-n:])
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def to_weekly(closes: Sequence[float], highs: Sequence[float] = None,
              lows: Sequence[float] = None) -> Tuple[List[float], List[float], List[float]]:
    """تجميع شموع يومية إلى أسبوعية (7 شموع لكل أسبوع، الأحدث آخراً)."""
    highs = list(highs) if highs is not None else list(closes)
    lows = list(lows) if lows is not None else list(closes)
    wc, wh, wl = [], [], []
    n = len(closes)
    start = n % 7
    i = start if start else 0
    if start:
        wc.append(closes[start - 1]); wh.append(max(highs[:start])); wl.append(min(lows[:start]))
    while i + 7 <= n:
        chunk_c, chunk_h, chunk_l = closes[i:i + 7], highs[i:i + 7], lows[i:i + 7]
        wc.append(chunk_c[-1]); wh.append(max(chunk_h)); wl.append(min(chunk_l))
        i += 7
    return wc, wh, wl


def to_monthly(closes: Sequence[float], highs: Sequence[float] = None,
               lows: Sequence[float] = None) -> Tuple[List[float], List[float], List[float]]:
    """تجميع يومي إلى شهري تقريبي (30 يوماً)."""
    highs = list(highs) if highs is not None else list(closes)
    lows = list(lows) if lows is not None else list(closes)
    mc, mh, ml = [], [], []
    n = len(closes)
    i = n % 30
    if i:
        mc.append(closes[i - 1]); mh.append(max(highs[:i])); ml.append(min(lows[:i]))
    while i + 30 <= n:
        mc.append(closes[i + 29]); mh.append(max(highs[i:i + 30])); ml.append(min(lows[i:i + 30]))
        i += 30
    return mc, mh, ml


def swing_levels(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                 lookback: int = 90, lb: int = 3) -> Tuple[List[float], List[float]]:
    """مستويات دعم/مقاومة من قمم وقيعان متأرجحة داخل نافذة."""
    H, L, C = list(highs[-lookback:]), list(lows[-lookback:]), list(closes[-lookback:])
    price = C[-1] if C else 0.0
    sup, res = [], []
    for i in range(lb, len(H) - lb):
        window_h = H[i - lb:i + lb + 1]
        window_l = L[i - lb:i + lb + 1]
        if H[i] == max(window_h):
            (res if H[i] > price else sup).append(H[i])
        if L[i] == min(window_l):
            (sup if L[i] < price else res).append(L[i])
    sup = sorted({round(x, 10) for x in sup}, reverse=True)
    res = sorted({round(x, 10) for x in res})
    return sup, res


def max_drawdown(closes: Sequence[float]) -> float:
    """أقصى تراجع % داخل السلسلة."""
    peak, mdd = float("-inf"), 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, (c / peak - 1.0) * 100.0)
    return mdd


def pct_from_high(closes: Sequence[float], window: int = 365) -> Optional[float]:
    w = list(closes[-window:])
    if not w:
        return None
    hi = max(w)
    return (w[-1] / hi - 1.0) * 100.0 if hi else None


def linreg_slope_pct(values: Sequence[float]) -> Optional[float]:
    """ميل خط الانحدار كنسبة % من متوسط القيم (اتجاه عام مستقر ضد الضوضاء)."""
    n = len(values)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(values) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0 or my == 0:
        return None
    slope = sum((xs[i] - mx) * (values[i] - my) for i in range(n)) / den
    return slope / my * 100.0 * n   # التغير الكلي % عبر النافذة
