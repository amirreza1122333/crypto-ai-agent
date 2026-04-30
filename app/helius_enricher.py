"""
Helius API enrichment for detected gems (Solana only).

Adds to every gem alert:
  - Creator wallet history (serial rugger detection)
  - Top holder concentration
  - Rug risk score + flags

Credit usage: ~20-30 credits per gem detected.
At 50 gems/day = ~1,500 credits/day = ~45,000/month
(well within 1M free monthly credits)
"""
import os
import time
import requests

from app.config import SSL_VERIFY
import urllib3
from pathlib import Path
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API     = "https://api.helius.xyz/v0"

CACHE_TTL = 600   # 10 min per token
_cache:    dict = {}
_cache_ts: dict = {}


# ──────────────────────────────────────────────────────────────────────────
# Low-level API helpers
# ──────────────────────────────────────────────────────────────────────────

def _rpc(method: str, params: list):
    if not HELIUS_API_KEY:
        return {}
    try:
        r = requests.post(
            HELIUS_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=15, verify=SSL_VERIFY,
        )
        if r.status_code == 200:
            return r.json().get("result", {})
    except Exception as e:
        print(f"[HELIUS] RPC {method}: {e}")
    return {}


def _api_get(path: str, params: dict = None):
    if not HELIUS_API_KEY:
        return {}
    try:
        r = requests.get(
            f"{HELIUS_API}/{path}",
            params={"api-key": HELIUS_API_KEY, **(params or {})},
            timeout=15, verify=SSL_VERIFY,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[HELIUS] GET {path}: {e}")
    return {}


def _api_post(path: str, body: dict):
    if not HELIUS_API_KEY:
        return {}
    try:
        r = requests.post(
            f"{HELIUS_API}/{path}",
            params={"api-key": HELIUS_API_KEY},
            json=body,
            timeout=15, verify=SSL_VERIFY,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[HELIUS] POST {path}: {e}")
    return {}


# ──────────────────────────────────────────────────────────────────────────
# Data fetchers
# ──────────────────────────────────────────────────────────────────────────

def _get_token_metadata(mint: str) -> dict:
    """~1 credit — get on-chain metadata including update authority (creator)."""
    data = _api_post("token-metadata", {
        "mintAccounts": [mint],
        "includeOffChain": False,
        "disableCache": False,
    })
    if isinstance(data, list) and data:
        return data[0]
    return {}


def _get_token_supply(mint: str) -> float:
    """~1 credit — total token supply."""
    result = _rpc("getTokenSupply", [mint])
    if isinstance(result, dict):
        return float(result.get("value", {}).get("amount", 0) or 0)
    return 0.0


def _get_top_holders(mint: str, limit: int = 10) -> list:
    """~1 credit — largest token accounts."""
    result = _rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    if isinstance(result, dict):
        return result.get("value", [])[:limit]
    return []


def _get_creator_txs(creator: str, limit: int = 25) -> list:
    """~limit credits — recent transactions for creator wallet."""
    data = _api_get(f"addresses/{creator}/transactions", {"limit": limit})
    return data if isinstance(data, list) else []


def get_token_transactions(mint: str, limit: int = 100) -> list:
    """
    ~limit credits — parsed Helius Enhanced Transactions involving this mint.

    Returns most-recent-first. Each entry has `slot`, `timestamp`,
    `tokenTransfers`, etc. Used by sniper_detector.check_block_clusters
    to look for coordinated buys in the same slot.

    Public API parallel of _get_creator_txs (same backend call, different
    semantic intent).
    """
    if not mint:
        return []
    data = _api_get(f"addresses/{mint}/transactions", {"limit": limit})
    return data if isinstance(data, list) else []


def _get_owner_token_balance(owner: str, mint: str) -> float:
    """
    ~1 credit — total amount of `mint` held by `owner`.

    Sums all of the owner's token accounts that hold this mint (a wallet can
    technically have multiple ATAs for the same mint, though it's rare).
    Returns the raw on-chain amount (not adjusted for decimals); pair with
    _get_token_supply for a ratio.

    Returns 0.0 on any failure or when Helius isn't configured.
    """
    if not owner or not mint:
        return 0.0
    result = _rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    if not isinstance(result, dict):
        return 0.0
    accounts = result.get("value") or []
    total = 0.0
    for acc in accounts:
        try:
            amount = (
                acc.get("account", {})
                   .get("data", {})
                   .get("parsed", {})
                   .get("info", {})
                   .get("tokenAmount", {})
                   .get("amount", "0")
            )
            total += float(amount)
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def get_creator_supply_pct(mint: str, creator: str) -> float:
    """
    Percentage of `mint`'s total supply held by the `creator` wallet.

    Returns:
        A float in [0.0, 100.0] on success.
        -1.0 if Helius isn't configured or any RPC call fails.
        (The negative sentinel lets callers distinguish "creator really
        holds 0% of the supply" from "we couldn't measure".)

    Cost: ~2 Helius credits (getTokenAccountsByOwner + getTokenSupply).
    Cheap enough to call at every $5K milestone, but DON'T call it on
    every newly-created token — pump.fun spawns 1000+ per hour.
    """
    if not HELIUS_API_KEY or not mint or not creator:
        return -1.0

    try:
        balance = _get_owner_token_balance(creator, mint)
        supply  = _get_token_supply(mint)
        if supply <= 0:
            return -1.0
        pct = balance / supply * 100.0
        if pct < 0:
            return -1.0
        return round(min(pct, 100.0), 2)
    except Exception as e:
        print(f"[HELIUS] creator_supply_pct {creator[:8]}/{mint[:8]}: {e}")
        return -1.0


# ──────────────────────────────────────────────────────────────────────────
# Analysis
# ──────────────────────────────────────────────────────────────────────────

def _analyze_creator(creator: str) -> dict:
    txs = _get_creator_txs(creator, limit=25)

    mints_seen = set()
    for tx in txs:
        for account in tx.get("accountData", []):
            for change in account.get("tokenBalanceChanges", []):
                mint = change.get("mint", "")
                # Exclude SOL and USDC
                if mint and mint not in (
                    "So11111111111111111111111111111111111111112",
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                ):
                    mints_seen.add(mint)

    total = len(mints_seen)
    return {
        "tokens_launched":   total,
        "is_serial_launcher": total >= 3,
        "tx_count":          len(txs),
    }


def _analyze_holders(mint: str) -> dict:
    holders      = _get_top_holders(mint, 10)
    total_supply = _get_token_supply(mint)

    if not holders or total_supply <= 0:
        return {"top1_pct": 0.0, "top10_pct": 0.0}

    amounts      = [float(h.get("amount", 0) or 0) for h in holders]
    top10_total  = sum(amounts)
    top1         = amounts[0] if amounts else 0

    return {
        "top1_pct":  round(top1 / total_supply * 100, 1),
        "top10_pct": round(top10_total / total_supply * 100, 1),
    }


# ──────────────────────────────────────────────────────────────────────────
# Main enrichment entry point
# ──────────────────────────────────────────────────────────────────────────

def enrich_gem(mint: str, chain: str = "solana") -> dict:
    """
    Full Helius enrichment for a detected gem.
    Only runs for Solana tokens (Helius is Solana-specific).
    Returns a dict with rug risk analysis.
    """
    if chain != "solana" or not HELIUS_API_KEY or not mint:
        return _empty()

    now = time.time()
    if mint in _cache and now - _cache_ts.get(mint, 0) < CACHE_TTL:
        return _cache[mint]

    result = _empty()

    try:
        # Step 1: Get token metadata to find creator
        meta    = _get_token_metadata(mint)         # ~1 credit
        creator = ""
        if meta:
            on_chain = meta.get("onChainMetadata", {})
            creator  = (on_chain.get("metadata") or {}).get("updateAuthority", "")
            result["name_verified"] = bool(on_chain)

        result["creator"]      = (creator[:8] + "...") if creator else "Unknown"
        result["creator_full"] = creator

        # Step 2: Analyze creator wallet
        if creator:
            cd = _analyze_creator(creator)          # ~25 credits
            result["tokens_launched"]   = cd["tokens_launched"]
            result["is_serial_launcher"] = cd["is_serial_launcher"]

        # Step 3: Holder concentration
        hd = _analyze_holders(mint)                 # ~2 credits
        result["top1_pct"]  = hd["top1_pct"]
        result["top10_pct"] = hd["top10_pct"]

        # Step 4: Build risk score + flags
        risk  = 0
        flags = []

        if result["is_serial_launcher"]:
            risk += 35
            flags.append(f"Serial launcher - {result['tokens_launched']} previous tokens")
        elif result["tokens_launched"] >= 2:
            risk += 15
            flags.append(f"Dev launched {result['tokens_launched']} tokens before")

        t1  = result["top1_pct"]
        t10 = result["top10_pct"]

        if t1 >= 50:
            risk += 40
            flags.append(f"Top holder owns {t1:.0f}% — extreme rug risk")
        elif t1 >= 25:
            risk += 25
            flags.append(f"Top holder owns {t1:.0f}% — high concentration")
        elif t1 >= 10:
            risk += 10
            flags.append(f"Top holder owns {t1:.0f}%")

        if t10 >= 85:
            risk += 15
            flags.append(f"Top 10 wallets hold {t10:.0f}% of supply")
        elif t10 >= 70:
            risk += 8

        result["rug_risk_score"] = min(risk, 100)
        result["rug_risk_label"] = (
            "HIGH"    if risk >= 55 else
            "MEDIUM"  if risk >= 30 else
            "LOW"
        )
        result["risk_flags"] = flags
        result["enriched"]   = True

    except Exception as e:
        print(f"[HELIUS] Enrichment failed for {mint}: {e}")

    _cache[mint]    = result
    _cache_ts[mint] = now
    return result


def format_enrichment(data: dict) -> str:
    """Return a compact risk block for Telegram messages."""
    if not data.get("enriched"):
        return ""

    label = data["rug_risk_label"]
    emoji = "🔴" if label == "HIGH" else "🟡" if label == "MEDIUM" else "🟢"
    lines = [f"Risk: {emoji} {label}"]

    if data["tokens_launched"] > 0:
        serial = " (SERIAL RUGGER)" if data["is_serial_launcher"] else ""
        lines.append(f"Dev: {data['creator']} | {data['tokens_launched']} prev tokens{serial}")

    t1  = data["top1_pct"]
    t10 = data["top10_pct"]
    if t1 > 0 or t10 > 0:
        lines.append(f"Holders: top1={t1:.0f}% | top10={t10:.0f}%")

    for flag in data["risk_flags"][:2]:
        lines.append(f"  ! {flag}")

    return "\n".join(lines)


def _empty() -> dict:
    return {
        "enriched":          False,
        "creator":           "Unknown",
        "creator_full":      "",
        "tokens_launched":   0,
        "is_serial_launcher": False,
        "top1_pct":          0.0,
        "top10_pct":         0.0,
        "rug_risk_score":    50,
        "rug_risk_label":    "UNKNOWN",
        "risk_flags":        [],
        "name_verified":     False,
    }
