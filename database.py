import sqlite3
import time
from typing import Optional, List, Dict, Any

DB_PATH = "trades.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            window_ts INTEGER NOT NULL,
            direction TEXT NOT NULL,
            token_price REAL NOT NULL,
            shares REAL NOT NULL,
            cost_usd REAL NOT NULL,
            fee_usd REAL NOT NULL,
            win INTEGER,
            pnl REAL,
            bankroll_before REAL NOT NULL,
            bankroll_after REAL,
            confidence REAL NOT NULL,
            strategies TEXT NOT NULL,
            open_price REAL NOT NULL,
            close_price REAL,
            resolved INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def save_trade(
    window_ts: int,
    direction: str,
    token_price: float,
    shares: float,
    cost_usd: float,
    fee_usd: float,
    bankroll_before: float,
    confidence: float,
    strategies: str,
    open_price: float,
) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (timestamp, window_ts, direction, token_price, shares, cost_usd, fee_usd,
         bankroll_before, confidence, strategies, open_price, resolved)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        time.time(), window_ts, direction, token_price, shares, cost_usd, fee_usd,
        bankroll_before, confidence, strategies, open_price
    ))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def update_trade_result(
    trade_id: int,
    win: bool,
    pnl: float,
    bankroll_after: float,
    close_price: float,
):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE trades
        SET win = ?, pnl = ?, bankroll_after = ?, close_price = ?, resolved = 1
        WHERE id = ?
    """, (1 if win else 0, pnl, bankroll_after, close_price, trade_id))
    conn.commit()
    conn.close()


def get_stats() -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN win = 1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN win = 0 THEN 1 ELSE 0 END) as losses,
            SUM(pnl) as total_pnl,
            MAX(pnl) as best_trade,
            MIN(pnl) as worst_trade
        FROM trades WHERE resolved = 1
    """)
    row = c.fetchone()
    conn.close()
    if not row or row[0] == 0:
        return {
            "total": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "best_trade": 0.0,
            "worst_trade": 0.0, "win_rate": 0.0,
        }
    total, wins, losses, total_pnl, best_trade, worst_trade = row
    return {
        "total": total or 0,
        "wins": wins or 0,
        "losses": losses or 0,
        "total_pnl": total_pnl or 0.0,
        "best_trade": best_trade or 0.0,
        "worst_trade": worst_trade or 0.0,
        "win_rate": (wins / total * 100) if total else 0.0,
    }


def get_last_n_trades(n: int = 8) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, window_ts, direction, confidence, win, pnl, bankroll_after, cost_usd, resolved
        FROM trades
        ORDER BY id DESC
        LIMIT ?
    """, (n,))
    rows = c.fetchall()
    conn.close()
    result = []
    for row in rows:
        result.append({
            "id": row[0],
            "window_ts": row[1],
            "direction": row[2],
            "confidence": row[3],
            "win": row[4],
            "pnl": row[5],
            "bankroll_after": row[6],
            "cost_usd": row[7],
            "resolved": row[8],
        })
    return result


def get_consecutive_losses() -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT win FROM trades WHERE resolved = 1 ORDER BY id DESC LIMIT 10
    """)
    rows = c.fetchall()
    conn.close()
    count = 0
    for row in rows:
        if row[0] == 0:
            count += 1
        else:
            break
    return count
