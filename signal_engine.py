import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


def strategy_momentum(snapshot: Dict[str, Any]) -> Optional[str]:
    """
    Momentum: if |delta| > 0.3% and RSI not extreme, bet continuation.
    """
    delta = snapshot.get("delta_pct", 0.0)
    rsi = snapshot.get("rsi")

    if rsi is None:
        return None
    if abs(delta) < 0.3:
        return None
    if rsi > 75 or rsi < 25:
        return None

    return "UP" if delta > 0 else "DOWN"


def strategy_mean_reversion(snapshot: Dict[str, Any], minutes_elapsed: float) -> Optional[str]:
    """
    Mean reversion: if |delta in first 5 min| > 1.2%, bet reversal.
    """
    if minutes_elapsed > 5:
        return None

    delta = snapshot.get("delta_pct", 0.0)
    if abs(delta) < 1.2:
        return None

    # Bet reversal
    return "DOWN" if delta > 0 else "UP"


def strategy_macd_cross(snapshot: Dict[str, Any]) -> Optional[str]:
    """
    MACD cross: if MACD crossed signal line in last 3 candles, bet in direction of cross.
    """
    macd = snapshot.get("macd")
    signal = snapshot.get("macd_signal")
    hist = snapshot.get("macd_hist")

    if macd is None or signal is None or hist is None:
        return None

    # hist is a list; check last 3 for a cross
    if not isinstance(hist, list) or len(hist) < 3:
        if isinstance(hist, (int, float)):
            # Single value — can't detect cross
            return None
        return None

    recent = hist[-3:]
    # Cross up: last value positive, previous was negative
    if recent[-1] > 0 and recent[-2] <= 0:
        return "UP"
    # Cross down: last value negative, previous was positive
    if recent[-1] < 0 and recent[-2] >= 0:
        return "DOWN"

    return None


def fuse_signals(votes: List[Optional[str]]) -> Dict[str, Any]:
    """
    Voting fusion: count agreements, compute confidence.
    """
    valid = [v for v in votes if v is not None]
    if not valid:
        return {
            "direction": None,
            "confidence": 0.0,
            "strategies_voted": 0,
            "agreement": 0,
            "skip_reason": "no_signals",
        }

    up_count = sum(1 for v in valid if v == "UP")
    down_count = sum(1 for v in valid if v == "DOWN")

    if up_count > down_count:
        direction = "UP"
        agreement = up_count
    elif down_count > up_count:
        direction = "DOWN"
        agreement = down_count
    else:
        # Tie
        return {
            "direction": None,
            "confidence": 0.0,
            "strategies_voted": len(valid),
            "agreement": 0,
            "skip_reason": "tied_signals",
        }

    confidence = agreement / 3.0  # 3 total strategies

    return {
        "direction": direction,
        "confidence": confidence,
        "strategies_voted": len(valid),
        "agreement": agreement,
        "skip_reason": None,
    }


class SignalEngine:
    def __init__(self, min_confidence: float = 0.60):
        self.min_confidence = min_confidence

    def evaluate(
        self,
        snapshot: Dict[str, Any],
        minutes_elapsed: float,
    ) -> Dict[str, Any]:
        s1 = strategy_momentum(snapshot)
        s2 = strategy_mean_reversion(snapshot, minutes_elapsed)
        s3 = strategy_macd_cross(snapshot)

        result = fuse_signals([s1, s2, s3])

        strategy_details = {
            "momentum": s1,
            "mean_reversion": s2,
            "macd_cross": s3,
        }

        result["strategy_details"] = strategy_details

        # Check minimum confidence and minimum 2 agreements
        if result["direction"] is not None:
            if result["confidence"] < self.min_confidence:
                result["skip_reason"] = f"low_confidence_{result['confidence']:.2f}"
                result["direction"] = None
            elif result["agreement"] < 2:
                result["skip_reason"] = "less_than_2_agreements"
                result["direction"] = None

        return result
