import time
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"


def _connect():
    return sqlite3.connect(DB_PATH, timeout=5)


def ensure_paper_account(chat_id: int):
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO paper_account (chat_id, balance, equity)
        VALUES (?, 10000, 10000)
        ON CONFLICT(chat_id) DO NOTHING
    """, (chat_id,))
    conn.commit()
    conn.close()


def get_account(chat_id: int) -> dict:
    ensure_paper_account(chat_id)

    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT balance, equity FROM paper_account WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()

    return {
        "balance": float(row[0]),
        "equity": float(row[1]),
    }


def open_position(
    chat_id: int,
    symbol: str,
    name: str,
    entry_price: float,
    quantity: float,
    entry_score: float = 0.0,
    entry_prob: float = 0.0,
    entry_signal: str = "",
) -> bool:
    ensure_paper_account(chat_id)

    symbol = symbol.upper().strip()

    conn = _connect()
    c = conn.cursor()

    c.execute("""
        SELECT 1 FROM paper_positions
        WHERE chat_id=? AND symbol=? AND status='open'
    """, (chat_id, symbol))
    exists = c.fetchone()

    if exists:
        conn.close()
        return False

    c.execute("""
        INSERT INTO paper_positions (
            chat_id, symbol, name, entry_price, quantity,
            entry_score, entry_prob, entry_signal, entry_ts, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
    """, (
        chat_id,
        symbol,
        name,
        float(entry_price),
        float(quantity),
        float(entry_score),
        float(entry_prob),
        str(entry_signal),
        int(time.time()),
    ))

    conn.commit()
    conn.close()
    return True


def get_open_positions(chat_id: int):
    conn = _connect()
    c = conn.cursor()

    c.execute("""
        SELECT id, symbol, name, entry_price, quantity, entry_score,
               entry_prob, entry_signal, entry_ts
        FROM paper_positions
        WHERE chat_id=? AND status='open'
        ORDER BY entry_ts DESC
    """, (chat_id,))
    rows = c.fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "symbol": r[1],
            "name": r[2],
            "entry_price": float(r[3]),
            "quantity": float(r[4]),
            "entry_score": float(r[5] or 0),
            "entry_prob": float(r[6] or 0),
            "entry_signal": r[7] or "",
            "entry_ts": int(r[8]),
        })
    return out


def close_position(chat_id: int, symbol: str, exit_price: float) -> bool:
    symbol = symbol.upper().strip()

    conn = _connect()
    c = conn.cursor()

    c.execute("""
        SELECT id, entry_price, quantity
        FROM paper_positions
        WHERE chat_id=? AND symbol=? AND status='open'
        ORDER BY entry_ts DESC
        LIMIT 1
    """, (chat_id, symbol))

    row = c.fetchone()
    if not row:
        conn.close()
        return False

    pos_id, entry_price, quantity = row
    entry_price = float(entry_price)
    quantity = float(quantity)
    exit_price = float(exit_price)

    pnl = (exit_price - entry_price) * quantity
    return_pct = ((exit_price - entry_price) / entry_price) if entry_price else 0.0

    c.execute("""
        UPDATE paper_positions
        SET status='closed',
            exit_price=?,
            exit_ts=?,
            pnl=?,
            return_pct=?
        WHERE id=?
    """, (
        exit_price,
        int(time.time()),
        pnl,
        return_pct,
        pos_id,
    ))

    c.execute("""
        UPDATE paper_account
        SET balance = balance + ?
        WHERE chat_id=?
    """, (pnl, chat_id))

    conn.commit()
    conn.close()
    return True


def get_closed_stats(chat_id: int) -> dict:
    ensure_paper_account(chat_id)

    conn = _connect()
    c = conn.cursor()

    c.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
               AVG(return_pct),
               SUM(pnl)
        FROM paper_positions
        WHERE chat_id=? AND status='closed'
    """, (chat_id,))
    row = c.fetchone()

    c.execute("""
        SELECT balance, equity
        FROM paper_account
        WHERE chat_id=?
    """, (chat_id,))
    acc = c.fetchone()

    conn.close()

    total = int(row[0] or 0)
    wins = int(row[1] or 0)
    avg_return = float(row[2] or 0)
    total_pnl = float(row[3] or 0)

    return {
        "total_trades": total,
        "wins": wins,
        "winrate": (wins / total) if total else 0.0,
        "avg_return": avg_return,
        "total_pnl": total_pnl,
        "balance": float(acc[0]),
        "equity": float(acc[1]),
    }