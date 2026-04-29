"""
Price alert system — users set target prices, bot notifies when hit.
"""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"


def init_price_alerts_table():
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("""
    CREATE TABLE IF NOT EXISTS price_alerts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id      INTEGER NOT NULL,
        symbol       TEXT    NOT NULL,
        target_price REAL    NOT NULL,
        direction    TEXT    NOT NULL,
        created_ts   INTEGER NOT NULL,
        triggered    INTEGER NOT NULL DEFAULT 0
    )
    """)
    con.commit()
    con.close()


def add_price_alert(chat_id: int, symbol: str, target_price: float, direction: str) -> int:
    """Returns the new alert ID, or -1 on failure."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        cur = con.execute("""
            INSERT INTO price_alerts (chat_id, symbol, target_price, direction, created_ts)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, symbol.upper(), target_price, direction, int(time.time())))
        con.commit()
        return cur.lastrowid
    except Exception:
        return -1
    finally:
        con.close()


def get_user_price_alerts(chat_id: int) -> list:
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute("""
        SELECT id, symbol, target_price, direction, created_ts
        FROM price_alerts
        WHERE chat_id=? AND triggered=0
        ORDER BY created_ts DESC
    """, (chat_id,))
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "symbol": r[1], "target": r[2], "direction": r[3], "ts": r[4]}
        for r in rows
    ]


def remove_price_alert(chat_id: int, alert_id: int) -> bool:
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute(
        "DELETE FROM price_alerts WHERE id=? AND chat_id=?",
        (alert_id, chat_id)
    )
    deleted = cur.rowcount > 0
    con.commit()
    con.close()
    return deleted


def get_all_active_alerts() -> list:
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute("""
        SELECT id, chat_id, symbol, target_price, direction
        FROM price_alerts
        WHERE triggered=0
    """)
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "chat_id": r[1], "symbol": r[2], "target": r[3], "direction": r[4]}
        for r in rows
    ]


def mark_alert_triggered(alert_id: int):
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("UPDATE price_alerts SET triggered=1 WHERE id=?", (alert_id,))
    con.commit()
    con.close()


def format_user_alerts(chat_id: int) -> str:
    alerts = get_user_price_alerts(chat_id)
    if not alerts:
        return (
            "No active price alerts.\n\n"
            "Set one with:\n"
            "/setalert BTC 100000\n"
            "/setalert ETH 2000"
        )
    lines = ["Your Price Alerts\n"]
    for a in alerts:
        dir_label = "rises above" if a["direction"] == "above" else "drops below"
        lines.append(f"[{a['id']}] {a['symbol']} {dir_label} ${a['target']:,.2f}")
    lines.append("\nRemove with: /delalert <ID>")
    return "\n".join(lines)
