"""
Portfolio tracker — users log their holdings, bot shows live P&L.
"""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"


def init_portfolio_table():
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("""
    CREATE TABLE IF NOT EXISTS portfolio (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER NOT NULL,
        symbol     TEXT    NOT NULL,
        quantity   REAL    NOT NULL,
        avg_price  REAL    NOT NULL,
        added_ts   INTEGER NOT NULL,
        UNIQUE(chat_id, symbol)
    )
    """)
    con.commit()
    con.close()


def add_holding(chat_id: int, symbol: str, quantity: float, avg_price: float) -> str:
    """Add or update a holding. Returns 'added' or 'updated'."""
    symbol = symbol.upper()
    con    = sqlite3.connect(DB_PATH, timeout=5)
    cur    = con.cursor()

    cur.execute("SELECT quantity, avg_price FROM portfolio WHERE chat_id=? AND symbol=?",
                (chat_id, symbol))
    row = cur.fetchone()

    if row:
        old_qty, old_price = row
        new_qty   = old_qty + quantity
        new_price = (old_qty * old_price + quantity * avg_price) / new_qty
        cur.execute(
            "UPDATE portfolio SET quantity=?, avg_price=?, added_ts=? WHERE chat_id=? AND symbol=?",
            (new_qty, new_price, int(time.time()), chat_id, symbol)
        )
        action = "updated"
    else:
        cur.execute(
            "INSERT INTO portfolio (chat_id, symbol, quantity, avg_price, added_ts) VALUES (?,?,?,?,?)",
            (chat_id, symbol, quantity, avg_price, int(time.time()))
        )
        action = "added"

    con.commit()
    con.close()
    return action


def remove_holding(chat_id: int, symbol: str) -> bool:
    symbol = symbol.upper()
    con    = sqlite3.connect(DB_PATH, timeout=5)
    cur    = con.cursor()
    cur.execute("DELETE FROM portfolio WHERE chat_id=? AND symbol=?", (chat_id, symbol))
    deleted = cur.rowcount > 0
    con.commit()
    con.close()
    return deleted


def get_holdings(chat_id: int) -> list:
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute(
        "SELECT symbol, quantity, avg_price FROM portfolio WHERE chat_id=? ORDER BY symbol",
        (chat_id,)
    )
    rows = cur.fetchall()
    con.close()
    return [{"symbol": r[0], "quantity": r[1], "avg_price": r[2]} for r in rows]


def format_portfolio(chat_id: int, live_prices: dict) -> str:
    """
    live_prices: dict mapping symbol -> current_price (from scan pipeline)
    """
    holdings = get_holdings(chat_id)
    if not holdings:
        return (
            "Your portfolio is empty.\n\n"
            "Add a holding:\n"
            "/port add BTC 0.5 95000\n"
            "(symbol, quantity, your avg buy price)"
        )

    total_cost  = 0.0
    total_value = 0.0
    lines       = ["Portfolio\n"]

    for h in holdings:
        sym   = h["symbol"]
        qty   = h["quantity"]
        entry = h["avg_price"]
        price = live_prices.get(sym, 0.0)

        cost  = qty * entry
        value = qty * price if price > 0 else cost
        pnl   = value - cost
        pct   = (pnl / cost * 100) if cost > 0 else 0

        total_cost  += cost
        total_value += value

        arrow = "+" if pnl >= 0 else ""
        price_str = f"${price:,.4f}" if price > 0 else "price N/A"
        lines.append(
            f"{sym}: {qty} @ ${entry:,.4f}\n"
            f"  Now: {price_str} | PnL: {arrow}{pct:.1f}% (${pnl:+,.2f})"
        )

    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    arrow = "+" if total_pnl >= 0 else ""

    lines.append(
        f"\nTotal Cost:  ${total_cost:,.2f}\n"
        f"Total Value: ${total_value:,.2f}\n"
        f"Overall PnL: {arrow}{total_pct:.1f}% (${total_pnl:+,.2f})"
    )
    lines.append("\n/port remove BTC - remove holding")
    return "\n".join(lines)
