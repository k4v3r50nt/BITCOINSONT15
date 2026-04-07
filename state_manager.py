"""
BITCOINSONT15 — State Manager

Persists bankroll and session stats across process restarts.

Priority for bankroll on startup:
  1. CURRENT_BANKROLL env var  (set manually in Railway after important sessions)
  2. state.json file           (survives container restarts within same deploy)
  3. INITIAL_BANKROLL env var  (fresh start default)

state.json location:
  /data/state.json   if /data/ directory exists (Railway persistent volume mount)
  ./state.json       otherwise (ephemeral, survives restarts but not redeploys)
"""

import json
import logging
import os
import time
from typing import Dict, Any

logger = logging.getLogger(__name__)

# ── File location ─────────────────────────────────────────────────────────────

def _state_path() -> str:
    """Return the best available path for state.json."""
    if os.path.isdir("/data"):
        return "/data/state.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

STATE_FILE = _state_path()

# ── Load ──────────────────────────────────────────────────────────────────────

def load_bankroll(initial_bankroll: float) -> float:
    """
    Return the bankroll to start with, using the priority chain:
      1. CURRENT_BANKROLL env var
      2. state.json bankroll field
      3. initial_bankroll argument
    """
    # 1. Railway env var (manually updated after important sessions)
    env_val = os.environ.get("CURRENT_BANKROLL")
    if env_val:
        try:
            br = float(env_val)
            logger.info(f"[State] Bankroll from CURRENT_BANKROLL env: ${br:.2f}")
            return br
        except ValueError:
            logger.warning(f"[State] Invalid CURRENT_BANKROLL env value: {env_val!r}")

    # 2. state.json (survives restarts within same deploy)
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                data = json.load(f)
            br = float(data.get("bankroll", initial_bankroll))
            updated = data.get("updated_at", 0)
            age_min = (time.time() - updated) / 60
            logger.info(
                f"[State] Bankroll from state.json: ${br:.2f} "
                f"(saved {age_min:.0f}min ago at {STATE_FILE})"
            )
            return br
    except Exception as e:
        logger.warning(f"[State] Could not read state.json: {e}")

    # 3. Default
    logger.info(f"[State] Using initial bankroll: ${initial_bankroll:.2f}")
    return initial_bankroll


def load_state() -> Dict[str, Any]:
    """
    Load full state dict from state.json.
    Returns empty dict if file doesn't exist or is unreadable.
    """
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[State] Could not load state.json: {e}")
    return {}

# ── Save ──────────────────────────────────────────────────────────────────────

def save_state(
    bankroll:     float,
    total_trades: int   = 0,
    wins:         int   = 0,
    losses:       int   = 0,
) -> None:
    """
    Write current session state to state.json.
    Called after every trade execute and resolve.
    Non-blocking: failures are logged but never raise.
    """
    data = {
        "bankroll":     round(bankroll, 4),
        "total_trades": total_trades,
        "wins":         wins,
        "losses":       losses,
        "updated_at":   time.time(),
        "updated_human": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    try:
        # Write atomically via temp file to avoid corruption on crash
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
        logger.debug(
            f"[State] Saved state.json — bankroll=${bankroll:.2f} "
            f"trades={total_trades} W={wins} L={losses}"
        )
    except Exception as e:
        logger.warning(f"[State] Could not write state.json: {e}")


def print_startup_banner(bankroll: float, source: str) -> None:
    """Print a clear startup message showing where the bankroll came from."""
    print(f"[STATE] ═══════════════════════════════════════")
    print(f"[STATE]  Bankroll cargado: ${bankroll:.2f}")
    print(f"[STATE]  Fuente: {source}")
    print(f"[STATE]  state.json: {STATE_FILE}")
    print(f"[STATE] ═══════════════════════════════════════")
