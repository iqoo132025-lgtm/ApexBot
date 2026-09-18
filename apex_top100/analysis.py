#!/usr/bin/env python3
"""
محرك تحليل Top 100 — منفصل عن محرك عملات الميم الجديدة.

لا يُطبَّق هنا RugCheck ولا Bundled Supply ولا Developer Intelligence:
تلك فلاتر مسار Micro/New Tokens. عملات Top 100 تُقاس بمقاييس الاستثمار والسوينغ:

  الاتجاه الأسبوعي/الشهري · Volume · Market Cap · BTC correlation
  القوة النسبية مقابل BTC و ETH · Momentum · Drawdown
  مستويات الدعم/المقاومة · تغيّر ترتيب Market Cap

المخرجات: APEX Score من 100 + منطقة دخول + إبطال + ثلاثة أهداف + حجم مركز.
حالة السوق (Regime) تعدّل الأوزان والحجم فقط — ولا تُنتج إشارة شراء بذاتها.
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from . import indicators as I
from .config import Top100Config


@dataclass
class Top100Signal:
    symbol: str
    coin_id: str
    name: str
    rank: int
    price: float
    score: float
    regime: str
    trend_emoji: str
    trend_label: str
    vs_btc_pct: Optional[float]
    vs_eth_pct: Optional[float]
    volume_state: str
    momentum_label: str
    risk: str
    entry_low: float
    entry_high: float
    entry_note: str
    invalidation: float
    tp1: float
    tp2: float
    tp3: float
    position_pct: float
    components: Dict[str, float] = field(default_factory=dict)
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    tradable: bool = True
    data_quality: str = "ohlc"     # ohlc = شموع حقيقية | price_only = إغلاق فقط بلا High/Low

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _scale(value: Optional[float], lo: float, hi: float, default: float = 50.0) -> float:
    if value is None:
        return default
    if hi == lo:
        return default
    return _clamp((value - lo) / (hi - lo) * 100.0)


def _ratio_series(a: List[float], b: List[float]) -> List[float]:
    n = min(len(a), len(b))
    if n < 2:
        return []
    a, b = a[-n:], b[-n:]
    return [a[i] / b[i] for i in range(n) if b[i]]


# ══════════════════════════════════════════
#  مكوّنات السكور
# ══════════════════════════════════════════
def score_trend(closes: List[float], highs: List[float], lows: List[float]) -> (float, Dict[str, Optional[float]]):
    wc, wh, wl = I.to_weekly(closes, highs, lows)
    mc, _, _ = I.to_monthly(closes, highs, lows)
    price = closes[-1]
    w_sma10 = I.sma(wc, 10)
    w_sma30 = I.sma(wc, 30)
    d_sma50 = I.sma(closes, 50)
    d_sma200 = I.sma(closes, 200) if len(closes) >= 200 else None
    m_slope = I.linreg_slope_pct(mc[-6:]) if len(mc) >= 3 else None
    w_slope = I.linreg_slope_pct(wc[-12:]) if len(wc) >= 6 else None

    pts, wts = [], []
    def add(w, v):
        pts.append(v); wts.append(w)

    add(1.2, 100.0 if (w_sma10 and price > w_sma10) else 0.0)
    add(1.5, 100.0 if (w_sma30 and price > w_sma30) else 0.0)
    add(1.0, 100.0 if (d_sma50 and price > d_sma50) else 0.0)
    add(1.0, 100.0 if (d_sma200 and price > d_sma200) else (50.0 if d_sma200 is None else 0.0))
    add(1.3, _scale(w_slope, -25.0, 45.0))
    add(1.0, _scale(m_slope, -30.0, 60.0))
    if w_sma10 and w_sma30:
        add(1.0, 100.0 if w_sma10 > w_sma30 else 0.0)

    score = sum(p * w for p, w in zip(pts, wts)) / sum(wts)
    metrics = {
        "weekly_sma10": w_sma10, "weekly_sma30": w_sma30,
        "daily_sma50": d_sma50, "daily_sma200": d_sma200,
        "weekly_slope_pct": w_slope, "monthly_slope_pct": m_slope,
    }
    return score, metrics


def score_relative_strength(coin_closes: List[float], base_closes: List[float]) -> (float, Optional[float]):
    """القوة النسبية: أداء العملة ناقص أداء الأصل المرجعي خلال 30 يوم."""
    if not base_closes:
        return 50.0, None
    n = min(len(coin_closes), len(base_closes))
    if n < 31:
        return 50.0, None
    c, b = coin_closes[-n:], base_closes[-n:]
    c30 = (c[-1] / c[-31] - 1) * 100 if c[-31] else None
    b30 = (b[-1] / b[-31] - 1) * 100 if b[-31] else None
    if c30 is None or b30 is None:
        return 50.0, None
    excess = c30 - b30
    ratio = _ratio_series(c, b)
    ratio_slope = I.linreg_slope_pct(ratio[-60:]) if len(ratio) >= 20 else None
    s = 0.7 * _scale(excess, -30.0, 40.0) + 0.3 * _scale(ratio_slope, -25.0, 35.0)
    return s, excess


def score_momentum(closes: List[float]) -> (float, Dict[str, Optional[float]], str):
    r30 = I.roc(closes, 30)
    r90 = I.roc(closes, 90) if len(closes) > 90 else None
    r7 = I.roc(closes, 7)
    rsi14 = I.rsi(closes, 14)
    # RSI المثالي 55-70: قوة بلا إفراط
    if rsi14 is None:
        rsi_score = 50.0
    elif rsi14 < 40:
        rsi_score = _scale(rsi14, 20.0, 40.0) * 0.5
    elif rsi14 <= 70:
        rsi_score = 60.0 + (min(rsi14, 65) - 40) / 25 * 40.0
    else:
        rsi_score = max(30.0, 100.0 - (rsi14 - 70) * 4.0)   # إفراط شراء = خصم
    s = 0.35 * _scale(r30, -25.0, 45.0) + 0.25 * _scale(r90, -40.0, 90.0) + \
        0.15 * _scale(r7, -12.0, 15.0) + 0.25 * rsi_score

    if (r30 or 0) >= 25 and (rsi14 or 0) >= 55:
        label = "Strong"
    elif (r30 or 0) >= 8:
        label = "Building"
    elif (r30 or 0) >= -5:
        label = "Neutral"
    else:
        label = "Weak"
    return s, {"roc_7d": r7, "roc_30d": r30, "roc_90d": r90, "rsi_14": rsi14}, label


def score_volume(volumes: List[float], closes: List[float]) -> (float, Dict[str, Optional[float]], str):
    if len(volumes) < 40 or sum(volumes[-30:]) == 0:
        return 50.0, {"vol_7_30": None, "vol_30_90": None}, "Unknown"
    v7 = sum(volumes[-7:]) / 7
    v30 = sum(volumes[-30:]) / 30
    v90 = sum(volumes[-90:]) / min(90, len(volumes)) if len(volumes) >= 45 else v30
    r_7_30 = (v7 / v30) if v30 else None
    r_30_90 = (v30 / v90) if v90 else None

    # فوليوم على الشموع الصاعدة مقابل الهابطة (تجميع/تصريف) خلال 30 يوم
    up = down = 0.0
    for i in range(max(1, len(closes) - 30), len(closes)):
        if i < len(volumes):
            if closes[i] >= closes[i - 1]:
                up += volumes[i]
            else:
                down += volumes[i]
    acc_ratio = (up / down) if down else (2.0 if up else 1.0)

    s = 0.45 * _scale(r_7_30, 0.6, 2.0) + 0.30 * _scale(r_30_90, 0.7, 1.8) + \
        0.25 * _scale(acc_ratio, 0.7, 2.0)

    if r_7_30 is None:
        state = "Unknown"
    elif r_7_30 >= 1.25:
        state = "Increasing"
    elif r_7_30 >= 0.85:
        state = "Stable"
    else:
        state = "Decreasing"
    return s, {"vol_7_30": r_7_30, "vol_30_90": r_30_90, "accumulation_ratio": acc_ratio}, state


def score_rank(rank: int, rank_change_30: Optional[int], rank_velocity_14: Optional[float]) -> (float, Dict):
    base = _scale(-rank, -100.0, -1.0) * 0.35          # الترتيب الأعلى = جودة/سيولة أفضل
    change = _scale(rank_change_30, -15.0, 25.0) * 0.45
    vel = _scale(rank_velocity_14, -1.0, 1.5) * 0.20
    return base + change + vel, {"rank_change_30d": rank_change_30, "rank_velocity_14d": rank_velocity_14}


def score_structure(closes: List[float], highs: List[float], lows: List[float]) -> (float, Dict):
    price = closes[-1]
    w = closes[-90:] if len(closes) >= 90 else closes
    lo, hi = min(w), max(w)
    pos = ((price - lo) / (hi - lo) * 100.0) if hi > lo else 50.0
    sup, res = I.swing_levels(highs, lows, closes, lookback=120)
    nearest_sup = sup[0] if sup else lo
    nearest_res = res[0] if res else hi
    dist_sup = ((price / nearest_sup - 1) * 100.0) if nearest_sup else None
    dist_res = ((nearest_res / price - 1) * 100.0) if price else None
    # أفضل وضع: فوق الدعم بمسافة معقولة ومساحة كافية حتى المقاومة
    s = 0.45 * _scale(pos, 25.0, 85.0) + 0.30 * _scale(dist_res, 2.0, 25.0) + \
        0.25 * (100.0 - _scale(dist_sup, 2.0, 30.0))
    return s, {"range_position_pct": pos, "nearest_support": nearest_sup,
               "nearest_resistance": nearest_res, "supports": sup[:4], "resistances": res[:4],
               "range_low_90d": lo, "range_high_90d": hi}


def score_drawdown(closes: List[float], highs: List[float], lows: List[float],
                   ath_change_pct: Optional[float]) -> (float, Dict):
    from_high = I.pct_from_high(closes, 365)
    atrp = I.atr_pct(highs, lows, closes, 14)
    mdd90 = I.max_drawdown(closes[-90:]) if len(closes) >= 30 else None
    # تراجع معتدل أفضل من انهيار، والتذبذب المفرط يُخصم
    s = 0.45 * _scale(from_high, -75.0, -5.0) + 0.25 * _scale(ath_change_pct, -92.0, -15.0) + \
        0.30 * (100.0 - _scale(atrp, 3.0, 12.0))
    if mdd90 is not None and mdd90 < -45:
        s *= 0.85
    return s, {"from_365d_high_pct": from_high, "atr_pct": atrp,
               "max_drawdown_90d": mdd90, "from_ath_pct": ath_change_pct}


# ══════════════════════════════════════════
#  التحليل الكامل
# ══════════════════════════════════════════
def _risk_level(atrp: Optional[float], from_high: Optional[float], corr_btc: Optional[float], rank: int) -> str:
    pts = 0
    if atrp is not None:
        pts += 2 if atrp > 9 else (1 if atrp > 5.5 else 0)
    if from_high is not None:
        pts += 1 if from_high < -60 else 0
    if corr_btc is not None and corr_btc > 0.85:
        pts += 1
    pts += 1 if rank > 60 else 0
    return "Low" if pts <= 1 else ("Medium" if pts <= 3 else "High")


def analyze_coin(coin, ohlcv, btc_closes: List[float], eth_closes: List[float],
                 cfg: Top100Config, regime, rank_change_30: Optional[int] = None,
                 rank_velocity_14: Optional[float] = None) -> Optional[Top100Signal]:
    closes, highs, lows, vols = ohlcv.closes, ohlcv.highs, ohlcv.lows, ohlcv.volumes
    if len(closes) < 60:
        return None
    price = closes[-1]
    flags: List[str] = []

    t_s, t_m = score_trend(closes, highs, lows)
    rsb_s, vs_btc = score_relative_strength(closes, btc_closes)
    rse_s, vs_eth = score_relative_strength(closes, eth_closes)
    mom_s, mom_m, mom_label = score_momentum(closes)
    vol_s, vol_m, vol_state = score_volume(vols, closes)
    rank_s, rank_m = score_rank(coin.rank, rank_change_30, rank_velocity_14)
    str_s, str_m = score_structure(closes, highs, lows)
    dd_s, dd_m = score_drawdown(closes, highs, lows, getattr(coin, "ath_change_pct", None))

    comps = {"trend": t_s, "rs_btc": rsb_s, "rs_eth": rse_s, "momentum": mom_s,
             "volume": vol_s, "rank": rank_s, "structure": str_s, "drawdown": dd_s}

    w = dict(cfg.weights)
    # ── تعديل الأوزان بحسب حالة السوق: نرفع وزن القوة النسبية والزخم في الصعود ──
    boost = regime.policy["strength_boost"] if regime else 1.0
    for k in ("rs_btc", "rs_eth", "momentum"):
        w[k] = w[k] * boost
    if regime and regime.regime in ("BEAR", "OVERHEATED"):
        w["drawdown"] *= 1.25       # الحماية أهم في الهبوط/الإفراط
        w["structure"] *= 1.15
    total_w = sum(w.values())
    score = sum(comps[k] * w[k] for k in comps) / total_w

    corr = I.correlation(I.returns(closes[-90:]), I.returns(btc_closes[-90:])) if btc_closes else None
    atrp = dd_m["atr_pct"]
    if getattr(ohlcv, "price_only", False):
        atrp = None
        dd_m["atr_pct"] = None
        for k in ("nearest_support", "nearest_resistance", "supports", "resistances"):
            str_m[k] = None if not isinstance(str_m.get(k), list) else []
    risk = _risk_level(atrp, dd_m["from_365d_high_pct"], corr, coin.rank)

    # ── بوابة بنيوية: لا إشارة بمجرد حالة السوق ──
    tradable = True
    price_only = bool(getattr(ohlcv, "price_only", False))
    if price_only:
        # High/Low غير حقيقية: ATR والدعم/المقاومة ومنطقة الدخول والإبطال والأهداف
        # ستكون مضلّلة، فنمنع الإشارة ونكتفي بالتتبع والسكور.
        tradable = False
        flags.append("بيانات إغلاق فقط بلا High/Low حقيقية — لا إشارة تعتمد على ATR")
    if t_s < 45:
        tradable = False; flags.append("الاتجاه الأسبوعي غير داعم")
    if vol_state == "Decreasing" and (vol_m.get("vol_7_30") or 1) < 0.7:
        tradable = False; flags.append("الفوليوم ينكمش")
    if (mom_m.get("roc_30d") or 0) > 150:
        flags.append("ارتفاع مكافئ — خطر مطاردة السعر")
        score -= 6
    if regime and regime.regime == "OVERHEATED":
        flags.append("السوق في حالة إفراط — تصغير الحجم وتشديد الدخول")
    if price and t_m.get("weekly_sma30") and price < t_m["weekly_sma30"]:
        flags.append("تحت SMA30 الأسبوعي")

    # ── منطقة الدخول ──
    # في وضع price_only نستخدم تذبذب الإغلاقات كبديل تقريبي، والإشارة ممنوعة أصلاً.
    if atrp is None:
        rets = I.returns(closes[-30:])
        atr_proxy_pct = (I.stdev(rets) * 100.0) if rets else 3.0
        atr_abs = max(atr_proxy_pct, 1.0) / 100.0 * price
    else:
        atr_abs = atrp / 100.0 * price
    sma20 = I.sma(closes, 20) or price
    nearest_sup = str_m.get("nearest_support") or (min(closes[-60:]) if len(closes) >= 30 else price * 0.9)
    entry_high = min(price, max(sma20, nearest_sup * 1.02) + 0.25 * atr_abs)
    entry_low = max(nearest_sup, entry_high - 1.2 * atr_abs)
    if entry_low >= entry_high:
        entry_low = entry_high - 0.8 * atr_abs
    entry_note = "عند السوق" if price <= entry_high * 1.01 else "انتظار تصحيح للمنطقة"

    # ── الإبطال ──
    invalidation = min(nearest_sup * 0.985, entry_low - 1.0 * atr_abs)
    weekly_sma30 = t_m.get("weekly_sma30")
    if weekly_sma30 and weekly_sma30 < entry_low:
        invalidation = max(invalidation, weekly_sma30 * 0.985)
    risk_per_unit = max(entry_high - invalidation, price * 0.01)

    # ── الأهداف: مقاومات حقيقية أولاً ثم مضاعفات المخاطرة ──
    res_levels = [r for r in (str_m.get("resistances") or []) if r > entry_high * 1.01]
    targets: List[float] = []
    for r in res_levels[:3]:
        targets.append(r)
    r_mults = [1.8, 3.2, 5.0]
    while len(targets) < 3:
        targets.append(entry_high + r_mults[len(targets)] * risk_per_unit)
    targets = sorted(targets)[:3]

    # ── حجم المركز ──
    size = cfg.base_position_pct
    size *= 1.0 + max(0.0, (score - 70.0)) / 100.0          # سكور أعلى = حجم أكبر قليلاً
    size *= {"Low": 1.15, "Medium": 1.0, "High": 0.75}[risk]
    size *= regime.policy["size_mult"] if regime else 1.0
    size = max(cfg.min_position_pct, min(cfg.max_position_pct, size))
    size = round(size * 2) / 2.0

    trend_emoji = "🟢" if t_s >= 65 else ("🟡" if t_s >= 45 else "🔴")
    trend_label = "Uptrend" if t_s >= 65 else ("Neutral" if t_s >= 45 else "Downtrend")

    metrics = {"correlation_btc_90d": corr, **t_m, **mom_m, **vol_m, **rank_m, **str_m, **dd_m}

    return Top100Signal(
        symbol=coin.symbol, coin_id=coin.coin_id, name=getattr(coin, "name", coin.symbol),
        rank=coin.rank, price=price, score=round(_clamp(score), 1),
        regime=regime.regime if regime else "UNKNOWN",
        trend_emoji=trend_emoji, trend_label=trend_label,
        vs_btc_pct=vs_btc, vs_eth_pct=vs_eth,
        volume_state=vol_state, momentum_label=mom_label, risk=risk,
        entry_low=entry_low, entry_high=entry_high, entry_note=entry_note,
        invalidation=invalidation, tp1=targets[0], tp2=targets[1], tp3=targets[2],
        position_pct=size, components={k: round(v, 1) for k, v in comps.items()},
        metrics=metrics, flags=flags, tradable=tradable,
        data_quality="price_only" if price_only else "ohlc",
    )
