#!/usr/bin/env python3
"""
Market Regime Detector — يصنّف السوق من البيانات وحدها.

BEAR / RECOVERY / EARLY BULL / BULL / OVERHEATED

مبدأ ثابت في APEX: التصنيف **ليس سبب شراء**.
هو يعدّل الأوزان وحجم المركز وعتبة الإشارة فقط — ولا يفتح صفقة بذاته.
يستخدم كذلك مرشّح ثبات (hysteresis) حتى لا يتذبذب التصنيف يوماً بيوم.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import indicators as I

REGIMES = ["BEAR", "RECOVERY", "EARLY BULL", "BULL", "OVERHEATED"]

# عتبات السكور المركّب (0-100)
THRESHOLDS = [
    (25.0, "BEAR"),
    (45.0, "RECOVERY"),
    (65.0, "EARLY BULL"),
    (83.0, "BULL"),
    (101.0, "OVERHEATED"),
]

# تعديلات كل حالة: وزن القوة النسبية، مضاعف حجم المركز، وإضافة على عتبة السكور
REGIME_POLICY: Dict[str, Dict[str, float]] = {
    "BEAR":       {"strength_boost": 0.85, "size_mult": 0.40, "score_add": 12.0},
    "RECOVERY":   {"strength_boost": 0.95, "size_mult": 0.70, "score_add": 5.0},
    "EARLY BULL": {"strength_boost": 1.12, "size_mult": 1.00, "score_add": 0.0},
    "BULL":       {"strength_boost": 1.15, "size_mult": 1.00, "score_add": 0.0},
    "OVERHEATED": {"strength_boost": 0.90, "size_mult": 0.55, "score_add": 8.0},
}


@dataclass
class RegimeResult:
    regime: str
    raw_regime: str
    score: float
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    changed_from: Optional[str] = None

    @property
    def policy(self) -> Dict[str, float]:
        return REGIME_POLICY.get(self.regime, REGIME_POLICY["RECOVERY"])

    def as_dict(self) -> dict:
        return {"regime": self.regime, "raw_regime": self.raw_regime, "score": round(self.score, 1),
                "metrics": self.metrics, "reasons": self.reasons, "policy": self.policy,
                "changed_from": self.changed_from}


def _score_from(value: Optional[float], lo: float, hi: float) -> Optional[float]:
    """تحويل خطي إلى 0..100 مع تثبيت الطرفين."""
    if value is None:
        return None
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def compute_breadth(coin_closes: Dict[str, List[float]]) -> Dict[str, Optional[float]]:
    """اتساع السوق: نسبة عملات Top 100 فوق متوسطاتها وقرب قممها."""
    above50 = above200 = near_high = strong30 = total = 0
    for closes in coin_closes.values():
        if len(closes) < 60:
            continue
        total += 1
        s50 = I.sma(closes, 50)
        s200 = I.sma(closes, 200) if len(closes) >= 200 else None
        if s50 and closes[-1] > s50:
            above50 += 1
        if s200 and closes[-1] > s200:
            above200 += 1
        hi90 = max(closes[-90:]) if len(closes) >= 90 else max(closes)
        if hi90 and closes[-1] >= hi90 * 0.95:
            near_high += 1
        r30 = I.roc(closes, 30)
        if r30 is not None and r30 >= 100.0:
            strong30 += 1
    if not total:
        return {"breadth_50": None, "breadth_200": None, "near_high_pct": None,
                "parabolic_pct": None, "sample": 0}
    return {
        "breadth_50": above50 / total * 100.0,
        "breadth_200": above200 / total * 100.0,
        "near_high_pct": near_high / total * 100.0,
        "parabolic_pct": strong30 / total * 100.0,
        "sample": total,
    }


def detect_regime(btc_closes: List[float],
                  coin_closes: Dict[str, List[float]],
                  eth_closes: Optional[List[float]] = None,
                  total_mcap_change_30d: Optional[float] = None,
                  previous: Optional[dict] = None,
                  confirm_days: int = 2) -> RegimeResult:
    """
    previous: آخر صف من regime_history (لتطبيق الثبات)؛
    confirm_days: كم قراءة متتالية نحتاج قبل تغيير التصنيف.
    """
    m: Dict[str, Optional[float]] = {}
    reasons: List[str] = []

    price = btc_closes[-1] if btc_closes else 0.0
    s50 = I.sma(btc_closes, 50)
    s200 = I.sma(btc_closes, 200) if len(btc_closes) >= 200 else None
    m["btc_price"] = price
    m["btc_vs_sma50"] = ((price / s50 - 1) * 100.0) if s50 else None
    m["btc_vs_sma200"] = ((price / s200 - 1) * 100.0) if s200 else None
    m["btc_sma50_vs_200"] = ((s50 / s200 - 1) * 100.0) if (s50 and s200) else None
    m["btc_ret_30d"] = I.roc(btc_closes, 30)
    m["btc_ret_90d"] = I.roc(btc_closes, 90)
    m["btc_rsi_14"] = I.rsi(btc_closes, 14)
    m["btc_from_365d_high"] = I.pct_from_high(btc_closes, 365)
    m["eth_ret_30d"] = I.roc(eth_closes, 30) if eth_closes else None
    m["total_mcap_change_30d"] = total_mcap_change_30d

    m.update(compute_breadth(coin_closes))

    # ── سكور مركّب من مكوّنات مستقلة ──
    parts: List[tuple] = []   # (وزن, قيمة 0..100)
    def add(weight: float, value: Optional[float], label: str = ""):
        if value is not None:
            parts.append((weight, value))
            if label:
                reasons.append(label)

    add(18, _score_from(m["btc_vs_sma200"], -35.0, 45.0))
    add(12, _score_from(m["btc_vs_sma50"], -18.0, 22.0))
    add(10, _score_from(m["btc_sma50_vs_200"], -15.0, 25.0))
    add(12, _score_from(m["btc_ret_90d"], -35.0, 60.0))
    add(8,  _score_from(m["btc_from_365d_high"], -70.0, -2.0))
    add(18, m["breadth_50"])
    add(12, m["breadth_200"])
    add(5,  _score_from(m["near_high_pct"], 0.0, 45.0))
    add(5,  _score_from(m["total_mcap_change_30d"], -25.0, 35.0))

    total_w = sum(w for w, _ in parts) or 1.0
    score = sum(w * v for w, v in parts) / total_w
    m["composite_score"] = round(score, 2)

    # ── تصنيف خام ──
    raw = "BEAR"
    for limit, name in THRESHOLDS:
        if score < limit:
            raw = name
            break

    # ── علامات الإفراط (Overheated) ──
    overheat = 0
    if (m["btc_rsi_14"] or 0) >= 75:
        overheat += 1; reasons.append("BTC RSI يومي فوق 75")
    if (m["btc_vs_sma200"] or 0) >= 55:
        overheat += 1; reasons.append("BTC بعيد >55% فوق SMA200")
    if (m["parabolic_pct"] or 0) >= 12:
        overheat += 1; reasons.append("أكثر من 12% من Top 100 ضاعفت خلال 30 يوم")
    if (m["near_high_pct"] or 0) >= 60:
        overheat += 1; reasons.append("أغلب Top 100 عند قمم 90 يوم")
    m["overheat_flags"] = overheat
    if overheat >= 2 and score >= 55:
        raw = "OVERHEATED"

    # ── قيود هبوطية صريحة ──
    if m["btc_vs_sma200"] is not None and m["btc_vs_sma200"] < 0 and (m["breadth_200"] or 0) < 25:
        if raw in ("EARLY BULL", "BULL", "OVERHEATED"):
            raw = "RECOVERY"
            reasons.append("BTC تحت SMA200 واتساع ضعيف — لا نرقّي التصنيف")

    # ── ثبات: لا نغيّر التصنيف إلا بتأكيد متتالٍ ──
    final = raw
    changed_from = None
    if previous:
        prev_regime = previous.get("regime")
        prev_metrics = previous.get("metrics") or {}
        if isinstance(prev_metrics, str):
            import json as _json
            try:
                prev_metrics = _json.loads(prev_metrics)
            except Exception:
                prev_metrics = {}
        prev_raw = prev_metrics.get("raw_regime", prev_regime)
        if prev_regime and raw != prev_regime:
            if prev_raw == raw and REGIMES.index(raw) is not None:
                confirms = int(prev_metrics.get("raw_streak", 1)) + 1
            else:
                confirms = 1
            m["raw_streak"] = confirms
            if confirms >= confirm_days:
                final = raw
                changed_from = prev_regime
                reasons.append(f"تأكيد الانتقال {prev_regime} → {raw} بعد {confirms} قراءات")
            else:
                final = prev_regime
                reasons.append(f"قراءة جديدة ({raw}) بانتظار تأكيد — نبقى على {prev_regime}")
        else:
            m["raw_streak"] = 1
    else:
        m["raw_streak"] = 1

    m["raw_regime"] = raw
    return RegimeResult(regime=final, raw_regime=raw, score=score, metrics=m,
                        reasons=reasons, changed_from=changed_from)
