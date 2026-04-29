"""
Pre-launch Scout — monitors social media for upcoming token launches.

Sources:
  - Twitter/X  : keyword search via Twitter API v2 (needs TWITTER_BEARER_TOKEN)
  - Telegram   : public channel scraping via t.me/s/<channel> (no key needed)
  - Discord    : webhook/invite scraping (no key needed for public servers)

Detects posts containing:
  - "launching in X hours/minutes"
  - "fair launch", "stealth launch"
  - pump.fun URLs / Solana contract addresses
  - "going live at", "mint opens at"

Saves to scout_announcements SQLite table.
"""
import os
import re
import time
import sqlite3
import requests
import urllib3
from pathlib import Path
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TWITTER_BEARER = os.getenv("TWITTER_BEARER_TOKEN", "")
DB_PATH        = Path(__file__).resolve().parent.parent / "user_data.db"

# ── Patterns ───────────────────────────────────────────────────────────────

LAUNCH_KEYWORDS = [
    "launching in", "launch in", "launch at", "launching at",
    "going live", "fair launch", "stealth launch", "pump.fun",
    "new token", "new coin", "mint opens", "presale", "pre-sale",
    "dropping soon", "launching today", "launching tonight",
    "launch soon", "stealth drop", "new launch", "launching now",
    "token launch", "solana launch",
]

PUMPFUN_URL_RE   = re.compile(r'pump\.fun/([1-9A-HJ-NP-Za-km-z]{32,44})')
SOL_ADDRESS_RE   = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{40,44}\b')

# Public Telegram channels to monitor (no API key needed — web scrape)
TELEGRAM_CHANNELS = [
    "pumpfun_alpha",
    "solanagems",
    "solana_calls",
    "cryptolaunchpad",
    "newcoinlaunches",
    "solana_launches",
    "pumpfun_calls",
    "memecoin_calls",
]

# Twitter search query
TWITTER_QUERY = (
    '(pump.fun OR "fair launch" OR "stealth launch" OR "launching in" '
    'OR "launch at" OR "new token") '
    '(solana OR SOL OR memecoin) -is:retweet lang:en'
)


# ── DB ─────────────────────────────────────────────────────────────────────

def init_scout_tables():
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("""
    CREATE TABLE IF NOT EXISTS scout_announcements (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT,
        author      TEXT,
        text        TEXT,
        url         TEXT,
        mint        TEXT DEFAULT '',
        eta_min     INTEGER DEFAULT -1,
        detected_ts INTEGER,
        alerted     INTEGER DEFAULT 0,
        UNIQUE(source, url)
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS scout_users (
        chat_id    INTEGER PRIMARY KEY,
        subscribed INTEGER DEFAULT 1
    )
    """)
    con.commit()
    con.close()


def _save(source, author, text, url, mint, eta_min) -> bool:
    """Save announcement; returns True if it's new (not a duplicate)."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        cur = con.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO scout_announcements "
            "(source, author, text, url, mint, eta_min, detected_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (source, author, text[:1000], url, mint, eta_min, int(time.time()))
        )
        inserted = cur.rowcount > 0
        con.commit()
        con.close()
        return inserted
    except Exception:
        return False


def get_pending_alerts() -> list:
    """Announcements detected but not yet sent to Telegram."""
    con  = sqlite3.connect(DB_PATH, timeout=5)
    cur  = con.cursor()
    cutoff = int(time.time()) - 3600 * 6
    cur.execute("""
        SELECT id, source, author, text, url, mint, eta_min
        FROM scout_announcements
        WHERE alerted=0 AND detected_ts > ?
        ORDER BY detected_ts DESC
    """, (cutoff,))
    rows = cur.fetchall()
    con.close()
    return [
        {"id": r[0], "source": r[1], "author": r[2],
         "text": r[3], "url": r[4], "mint": r[5], "eta_min": r[6]}
        for r in rows
    ]


def mark_alerted(ann_id: int):
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("UPDATE scout_announcements SET alerted=1 WHERE id=?", (ann_id,))
    con.commit()
    con.close()


def get_recent_announcements(limit=10) -> list:
    con  = sqlite3.connect(DB_PATH, timeout=5)
    cur  = con.cursor()
    cutoff = int(time.time()) - 3600 * 6
    cur.execute("""
        SELECT source, author, text, url, mint, eta_min, detected_ts
        FROM scout_announcements
        WHERE detected_ts > ?
        ORDER BY detected_ts DESC LIMIT ?
    """, (cutoff, limit))
    rows = cur.fetchall()
    con.close()
    return [
        {"source": r[0], "author": r[1], "text": r[2],
         "url": r[3], "mint": r[4], "eta_min": r[5], "detected_ts": r[6]}
        for r in rows
    ]


# ── Helpers ────────────────────────────────────────────────────────────────

def _is_relevant(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in LAUNCH_KEYWORDS)


def _extract_mint(text: str) -> str:
    m = PUMPFUN_URL_RE.search(text)
    if m:
        return m.group(1)
    m = SOL_ADDRESS_RE.search(text)
    if m and len(m.group(0)) >= 40:
        return m.group(0)
    return ""


def _parse_eta(text: str) -> int:
    """Extract minutes until launch from text. Returns -1 if unknown."""
    t = text.lower()

    m = re.search(r'in\s+(\d+)\s*hour', t)
    if m:
        return int(m.group(1)) * 60

    m = re.search(r'in\s+(\d+)h', t)
    if m:
        return int(m.group(1)) * 60

    m = re.search(r'in\s+(\d+)\s*min', t)
    if m:
        return int(m.group(1))

    m = re.search(r'(\d+)h\s*(\d*)m?', t)
    if m:
        return int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)

    return -1


def _strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html)
    text = text.replace('&amp;', '&').replace('&#33;', '!') \
               .replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&quot;', '"').replace('&#39;', "'")
    return re.sub(r'\s+', ' ', text).strip()


# ── Twitter / X Scanner ────────────────────────────────────────────────────

def scan_twitter(max_results: int = 20) -> list:
    """
    Search recent tweets for launch announcements.
    Requires TWITTER_BEARER_TOKEN in .env
    Free tier: 500K reads/month.
    """
    if not TWITTER_BEARER:
        return []

    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
            params={
                "query":       TWITTER_QUERY,
                "max_results": max_results,
                "tweet.fields": "created_at,author_id,text",
                "expansions":  "author_id",
                "user.fields": "username",
            },
            timeout=12,
        )
        if r.status_code != 200:
            print(f"[SCOUT] Twitter {r.status_code}: {r.text[:200]}")
            return []

        data  = r.json()
        users = {u["id"]: u["username"]
                 for u in data.get("includes", {}).get("users", [])}
        found = []

        for tweet in data.get("data", []):
            text = tweet.get("text", "")
            if not _is_relevant(text):
                continue

            author  = users.get(tweet.get("author_id", ""), "unknown")
            url     = f"https://twitter.com/{author}/status/{tweet['id']}"
            mint    = _extract_mint(text)
            eta     = _parse_eta(text)

            if _save("twitter", f"@{author}", text, url, mint, eta):
                found.append({
                    "source": "Twitter/X", "author": f"@{author}",
                    "text": text, "url": url, "mint": mint, "eta_min": eta,
                })

        print(f"[SCOUT] Twitter: {len(found)} new")
        return found

    except Exception as e:
        print(f"[SCOUT] Twitter error: {e}")
        return []


# ── Telegram Public Channel Scraper ────────────────────────────────────────

def scan_telegram_channels(max_per_channel: int = 10) -> list:
    """
    Scrape public Telegram channels via t.me/s/<channel>.
    No API key required — uses the public web view.
    """
    found = []

    for channel in TELEGRAM_CHANNELS:
        try:
            r = requests.get(
                f"https://t.me/s/{channel}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=10,
            )
            if r.status_code != 200:
                continue

            # Extract message blocks from the page HTML
            blocks = re.findall(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                r.text, re.DOTALL
            )

            for raw in blocks[:max_per_channel]:
                text = _strip_html(raw)
                if len(text) < 20:
                    continue
                if not _is_relevant(text):
                    continue

                mint = _extract_mint(text)
                eta  = _parse_eta(text)
                url  = f"https://t.me/{channel}"

                if _save("telegram", f"@{channel}", text, url, mint, eta):
                    found.append({
                        "source": "Telegram", "author": f"@{channel}",
                        "text": text[:400], "url": url,
                        "mint": mint, "eta_min": eta,
                    })

            time.sleep(0.5)

        except Exception as e:
            print(f"[SCOUT] Telegram @{channel}: {e}")

    print(f"[SCOUT] Telegram: {len(found)} new")
    return found


# ── Discord Public Scraper ─────────────────────────────────────────────────
# Monitors public Discord servers via their invite page preview
# For full Discord bot support, add DISCORD_BOT_TOKEN to .env

DISCORD_INVITE_CHANNELS = [
    # Add public Discord invite codes of crypto announcement servers
    # e.g. "solana": "https://discord.gg/solana"
    # These are scraped for public announcement previews only
]

def scan_discord() -> list:
    """
    Placeholder — full Discord bot integration requires DISCORD_BOT_TOKEN.
    Add invite codes to DISCORD_INVITE_CHANNELS above.
    """
    return []


# ── Main entry point ───────────────────────────────────────────────────────

def scan_all_sources() -> list:
    """Run all scanners and return newly detected announcements."""
    results  = []
    results += scan_twitter()
    results += scan_telegram_channels()
    results += scan_discord()
    return results


# ── Formatting ─────────────────────────────────────────────────────────────

def format_announcement(ann: dict) -> str:
    src_icon = {"twitter": "𝕏", "telegram": "✈️", "discord": ""}.get(
        ann.get("source", "").lower(), "📢"
    )
    eta = ann.get("eta_min", -1)
    eta_str = f"\nLaunch ETA: ~{eta} min" if eta > 0 else ""

    lines = [
        f"PRE-LAUNCH ALERT  {src_icon} {ann.get('source','?').upper()}",
        f"From: {ann.get('author','')}",
        f"",
        ann.get("text", "")[:300],
    ]
    if eta_str:
        lines.append(eta_str)
    if ann.get("mint"):
        lines.append(f"\nToken: https://pump.fun/{ann['mint']}")
    if ann.get("url"):
        lines.append(f"Source: {ann['url']}")
    lines.append("\nDYOR — unverified announcement")
    return "\n".join(lines)


def format_upcoming_list() -> str:
    items = get_recent_announcements(limit=10)
    if not items:
        return (
            "Pre-Launch Announcements\n\n"
            "No launch announcements found in the last 6 hours.\n\n"
            "The scout monitors Twitter/X and Telegram channels every 5 minutes.\n"
            "Use /sources to see what's being watched."
        )

    lines = [f"Pre-Launch Announcements — Last 6h\n"]
    for ann in items:
        eta     = ann.get("eta_min", -1)
        eta_str = f" | ETA ~{eta}min" if eta > 0 else ""
        src     = ann.get("source", "?")
        author  = ann.get("author", "")
        preview = ann.get("text", "")[:120].replace("\n", " ")
        age_min = int((time.time() - ann.get("detected_ts", time.time())) / 60)

        lines.append(
            f"[{src}] {author}{eta_str} | {age_min}m ago\n"
            f"{preview}\n"
        )

    return "\n".join(lines)
