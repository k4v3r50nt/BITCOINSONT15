import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

MOMENTUM_DELTA_MIN  = 0.10   # % move from window open to trigger momentum
MOMENTUM_RSI_HIGH   = 75     # RSI above this = overbought, skip momentum UP
MOMENTUM_RSI_LOW    = 25     # RSI below this = oversold, skip momentum DOWN

MEAN_REV_MINUTES    = 7      # only active in the first N minutes of window
MEAN_REV_DELTA_MIN  = 0.50   # % move to trigger mean reversion

MACD_RSI_FALLBACK_UP   = 55  # when no MACD data, RSI > this → UP
MACD_RSI_FALLBACK_DOWN = 45  # when no MACD data, RSI < this → DOWN

MIN_CONFIDENCE      = 0.34   # 1 out of 3 strategies is enough


# ── Strategy 1 — Momentum ────────────────────────────────────────────────────

def strategy_momentum(snapshot: Dict[str, Any]) -> Optional[str]:
    """
    Bet continuation when price has already moved meaningfully and
    RSI is not at an extreme (which would suggest exhaustion).
    """
    delta = snapshot.get("delta_pct", 0.0)
    rsi   = snapshot.get("rsi")

    logger.debug(
        f"[Momentum] delta={delta:.3f}%  rsi={rsi!r}  "
        f"threshold=±{MOMENTUM_DELTA_MIN}%  rsi_range=[{MOMENTUM_RSI_LOW},{MOMENTUM_RSI_HIGH}]"
    )

    if rsi is None:
        logger.debug("[Momentum] SKIP — RSI not available (not enough candles yet)")
        return None

    if abs(delta) < MOMENTUM_DELTA_MIN:
        logger.debug(f"[Momentum] SKIP — |delta| {abs(delta):.3f}% < {MOMENTUM_DELTA_MIN}%")
        return None

    if delta > 0 and rsi > MOMENTUM_RSI_HIGH:
        logger.debug(f"[Momentum] SKIP — delta positive but RSI {rsi:.1f} overbought")
        return None

    if delta < 0 and rsi < MOMENTUM_RSI_LOW:
        logger.debug(f"[Momentum] SKIP — delta negative but RSI {rsi:.1f} oversold")
        return None

    direction = "UP" if delta > 0 else "DOWN"
    logger.debug(f"[Momentum] VOTE {direction}  (delta={delta:+.3f}%  rsi={rsi:.1f})")
    return direction


# ── Strategy 2 — Mean Reversion ──────────────────────────────────────────────

def strategy_mean_reversion(snapshot: Dict[str, Any], minutes_elapsed: float) -> Optional[str]:
    """
    When price has moved sharply in the first N minutes, bet on pullback.
    """
    delta = snapshot.get("delta_pct", 0.0)

    logger.debug(
        f"[MeanRev] delta={delta:.3f}%  minutes_elapsed={minutes_elapsed:.1f}  "
        f"window={MEAN_REV_MINUTES}min  threshold=±{MEAN_REV_DELTA_MIN}%"
    )

    if minutes_elapsed > MEAN_REV_MINUTES:
        logger.debug(f"[MeanRev] SKIP — past {MEAN_REV_MINUTES}-min window ({minutes_elapsed:.1f}min elapsed)")
        return None

    if abs(delta) < MEAN_REV_DELTA_MIN:
        logger.debug(f"[MeanRev] SKIP — |delta| {abs(delta):.3f}% < {MEAN_REV_DELTA_MIN}%")
        return None

    direction = "DOWN" if delta > 0 else "UP"
    logger.debug(f"[MeanRev] VOTE {direction}  (reversal of delta={delta:+.3f}%)")
    return direction


# ── Strategy 3 — MACD Cross ──────────────────────────────────────────────────

def strategy_macd_cross(snapshot: Dict[str, Any]) -> Optional[str]:
    """
    Detect when MACD line crosses the signal line between the previous and
    current candle.  Falls back to an RSI-momentum read when there isn't
    enough price history for MACD.
    """
    macd   = snapshot.get("macd")
    signal = snapshot.get("macd_signal")
    hist   = snapshot.get("macd_hist")
    rsi    = snapshot.get("rsi")

    logger.debug(
        f"[MACD] macd={macd!r}  signal={signal!r}  "
        f"hist_type={type(hist).__name__}  hist_len={len(hist) if isinstance(hist, list) else 'scalar'}  "
        f"rsi={rsi!r}"
    )

    # ── Primary: histogram cross detection ───────────────────────────────────
    if isinstance(hist, list) and len(hist) >= 2:
        prev, curr = hist[-2], hist[-1]
        logger.debug(f"[MACD] histogram[-2]={prev:.6f}  histogram[-1]={curr:.6f}")

        if curr > 0 and prev <= 0:
            logger.debug("[MACD] VOTE UP  (histogram crossed above zero)")
            return "UP"
        if curr < 0 and prev >= 0:
            logger.debug("[MACD] VOTE DOWN  (histogram crossed below zero)")
            return "DOWN"

        logger.debug("[MACD] SKIP — no cross in last 2 histogram bars")
        return None

    # ── Fallback: RSI bias when MACD unavailable ─────────────────────────────
    if rsi is None:
        logger.debug("[MACD] SKIP — no histogram list and no RSI")
        return None

    if rsi > MACD_RSI_FALLBACK_UP:
        logger.debug(f"[MACD] VOTE UP  (RSI fallback: {rsi:.1f} > {MACD_RSI_FALLBACK_UP})")
        return "UP"
    if rsi < MACD_RSI_FALLBACK_DOWN:
        logger.debug(f"[MACD] VOTE DOWN  (RSI fallback: {rsi:.1f} < {MACD_RSI_FALLBACK_DOWN})")
        return "DOWN"

    logger.debug(f"[MACD] SKIP — RSI fallback neutral ({rsi:.1f})")
    return None


# ── Signal fusion ─────────────────────────────────────────────────────────────

def fuse_signals(votes: List[Optional[str]]) -> Dict[str, Any]:
    valid      = [v for v in votes if v is not None]
    up_count   = sum(1 for v in valid if v == "UP")
    down_count = sum(1 for v in valid if v == "DOWN")

    logger.debug(f"[Fusion] votes={votes}  valid={valid}  up={up_count}  down={down_count}")

    if not valid:
        return {
            "direction": None, "confidence": 0.0,
            "strategies_voted": 0, "agreement": 0,
            "skip_reason": "no_signals",
        }

    if up_count > down_count:
        direction, agreement = "UP", up_count
    elif down_count > up_count:
        direction, agreement = "DOWN", down_count
    else:
        logger.debug("[Fusion] SKIP — tie")
        return {
            "direction": None, "confidence": 0.0,
            "strategies_voted": len(valid), "agreement": 0,
            "skip_reason": "tied_signals",
        }

    confidence = agreement / 3.0

    logger.debug(f"[Fusion] direction={direction}  confidence={confidence:.2f}  agreement={agreement}/3")
    return {
        "direction": direction,
        "confidence": confidence,
        "strategies_voted": len(valid),
        "agreement": agreement,
        "skip_reason": None,
    }


# ── SignalEngine ──────────────────────────────────────────────────────────────

class SignalEngine:
    def __init__(self, min_confidence: float = MIN_CONFIDENCE):
        self.min_confidence = min_confidence

    def evaluate(self, snapshot: Dict[str, Any], minutes_elapsed: float) -> Dict[str, Any]:
        price      = snapshot.get("price", 0)
        delta      = snapshot.get("delta_pct", 0.0)
        rsi        = snapshot.get("rsi")
        macd       = snapshot.get("macd")
        macd_sig   = snapshot.get("macd_signal")
        candles    = snapshot.get("candle_count", 0)

        logger.info(
            f"[Signal] T+{minutes_elapsed:.1f}min  "
            f"price=${price:,.2f}  delta={delta:+.3f}%  "
            f"rsi={rsi!r}  macd={macd!r}  sig={macd_sig!r}  candles={candles}"
        )

        s1 = strategy_momentum(snapshot)
        s2 = strategy_mean_reversion(snapshot, minutes_elapsed)
        s3 = strategy_macd_cross(snapshot)

        result = fuse_signals([s1, s2, s3])
        result["strategy_details"] = {
            "momentum":      s1,
            "mean_reversion": s2,
            "macd_cross":    s3,
        }

        if result["direction"] is not None:
            if result["confidence"] < self.min_confidence:
                reason = f"low_confidence_{result['confidence']:.2f}<{self.min_confidence:.2f}"
                logger.info(f"[Signal] SKIP — {reason}")
                result["skip_reason"] = reason
                result["direction"]   = None
            else:
                logger.info(
                    f"[Signal] FIRE {result['direction']}  "
                    f"conf={result['confidence']:.2f}  agreement={result['agreement']}/3"
                )
        else:
            logger.info(f"[Signal] SKIP — {result.get('skip_reason', 'unknown')}")

        return result
