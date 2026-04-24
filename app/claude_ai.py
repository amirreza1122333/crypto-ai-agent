"""
AI integration using Groq (free tier) — three surfaces:
  1. analyze_coin()  → per-coin explanation replacing rule-based ai_explainer
  2. analyze_news()  → LLM sentiment from raw headlines
  3. chat()          → free-form Telegram chat with scan context

Free tier: 14,400 requests/day, 30 RPM — no credit card needed.
Get key at: console.groq.com
"""
import os
from groq import Groq

_client: Groq | None = None
_configured = False

_MODEL = "llama-3.1-8b-instant"

_CHAT_SYSTEM = (
    "You are CryptoAI — an expert crypto analyst assistant inside a Telegram bot. "
    "Be concise and direct. Telegram has character limits. "
    "Reference actual numbers from the context when relevant. "
    "If asked about a specific coin, look it up in the coin list. "
    "If a question is outside crypto/finance, politely stay on topic. "
    "Max 3 short paragraphs. No markdown headers. Use plain text."
)

# Per-chat conversation history (in-memory, resets on restart)
_chat_histories: dict[int, list] = {}
_MAX_HISTORY = 8


def _get_client() -> Groq | None:
    global _client, _configured
    if _configured:
        return _client
    _configured = True
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key:
        _client = Groq(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# 1. Coin explanation (replaces rule-based ai_explainer)
# ---------------------------------------------------------------------------
def analyze_coin(coin: dict) -> str:
    """
    Return a 1-sentence AI-generated market signal for a coin.
    Returns empty string on error so callers use the rule-based fallback.
    """
    client = _get_client()
    if client is None:
        return ""
    try:
        prompt = (
            f"Coin: {coin.get('name', '?')} ({coin.get('symbol', '?').upper()})\n"
            f"24h change: {coin.get('price_change_percentage_24h', 0):.1f}%\n"
            f"7d change:  {coin.get('price_change_percentage_7d_in_currency', 0):.1f}%\n"
            f"MCap rank:  #{coin.get('market_cap_rank', '?')}\n"
            f"Vol/MCap:   {coin.get('volume_to_mcap', 0):.2f}\n"
            f"Score:      {coin.get('final_score', 0):.2f}\n"
            f"Risk level: {coin.get('risk_level', '?')}\n"
            f"Bucket:     {coin.get('bucket', '?')}\n\n"
            "Write ONE sentence (max 25 words) summarizing the key market signal. "
            "Be specific — mention actual numbers. No emoji. No preamble."
        )
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[GROQ] analyze_coin error: {e}")
        return ""


# ---------------------------------------------------------------------------
# 2. News sentiment from headlines
# ---------------------------------------------------------------------------
def analyze_news(symbol: str, headlines: list[str]) -> str:
    """
    Returns 'bullish' | 'bearish' | 'neutral'.
    Returns empty string on error (caller keeps vote-based sentiment).
    """
    client = _get_client()
    if client is None or not headlines:
        return ""
    try:
        numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines[:5]))
        prompt = (
            f"Headlines for {symbol.upper()}:\n{numbered}\n\n"
            "Reply with exactly ONE word: bullish, bearish, or neutral."
        )
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
        )
        word = resp.choices[0].message.content.strip().lower().rstrip(".")
        if word in ("bullish", "bearish", "neutral"):
            return word
        return ""
    except Exception as e:
        print(f"[GROQ] analyze_news error: {e}")
        return ""


# ---------------------------------------------------------------------------
# 3. Free-form Telegram chat
# ---------------------------------------------------------------------------
def chat(chat_id: int, user_message: str, context: dict | None = None) -> str:
    """
    Multi-turn conversational AI for Telegram using Groq.
    context can contain: coins list, fear_greed value.
    """
    client = _get_client()
    if client is None:
        return (
            "AI chat is not configured.\n"
            "Ask admin to set GROQ_API_KEY in .env\n"
            "Free key at: console.groq.com"
        )

    # Build context prefix
    ctx_lines = []
    if context:
        coins = context.get("coins", [])
        if coins:
            ctx_lines.append("== Live Scan (top coins right now) ==")
            for c in coins[:10]:
                ctx_lines.append(
                    f"{c.get('symbol','?').upper()} | "
                    f"Rank #{c.get('market_cap_rank','?')} | "
                    f"24h {c.get('price_change_percentage_24h', 0):+.1f}% | "
                    f"7d {c.get('price_change_percentage_7d_in_currency', 0):+.1f}% | "
                    f"Score {c.get('final_score', 0):.2f} | "
                    f"{c.get('risk_level','?')} risk | {c.get('bucket','?')}"
                )
        if context.get("fear_greed") is not None:
            ctx_lines.append(f"\nFear & Greed Index: {context['fear_greed']}/100")

    history = _chat_histories.setdefault(chat_id, [])

    user_content = user_message
    if ctx_lines:
        user_content = f"[Context]\n{chr(10).join(ctx_lines)}\n\n[Question]\n{user_message}"

    # Trim history
    if len(history) > _MAX_HISTORY:
        history[:] = history[-_MAX_HISTORY:]

    messages = [{"role": "system", "content": _CHAT_SYSTEM}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    try:
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=messages,
            max_tokens=300,
        )
        reply = resp.choices[0].message.content.strip()
        if not reply:
            reply = "Sorry, I couldn't generate a response. Please try again."

        # Update history
        history.append({"role": "user", "content": user_content})
        history.append({"role": "assistant", "content": reply})

        return reply
    except Exception as e:
        print(f"[GROQ] chat error: {e}")
        return f"AI error: {type(e).__name__}. Please try again."


def reset_chat(chat_id: int) -> None:
    """Clear conversation history for a chat."""
    _chat_histories.pop(chat_id, None)
