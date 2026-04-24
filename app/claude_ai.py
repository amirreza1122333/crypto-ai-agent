"""
Claude AI integration — three surfaces:
  1. analyze_coin()        → per-coin explanation replacing rule-based ai_explainer
  2. analyze_news()        → LLM sentiment from raw headlines
  3. chat()                → free-form Telegram chat with scan context
"""
import os
import time

import anthropic

_client: anthropic.Anthropic | None = None

def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if key:
            _client = anthropic.Anthropic(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# 1. Coin explanation (replaces rule-based ai_explainer)
# ---------------------------------------------------------------------------
# System prompt is stable → cached across all per-coin calls.
_COIN_SYSTEM = (
    "You are a concise crypto market analyst. Given a coin's metrics, write "
    "ONE sentence (max 25 words) summarizing the key signal. Be specific — "
    "mention actual numbers. No emoji. No hedging. No preamble."
)

def analyze_coin(coin: dict) -> str:
    """
    Return a 1-sentence Claude-generated market signal for a coin.
    Falls back to empty string on any error so callers can use the
    rule-based fallback.
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
            f"Bucket:     {coin.get('bucket', '?')}\n"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            system=[{
                "type": "text",
                "text": _COIN_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        return next((b.text.strip() for b in resp.content if b.type == "text"), "")
    except Exception as e:
        print(f"[CLAUDE] analyze_coin error: {e}")
        return ""


# ---------------------------------------------------------------------------
# 2. News sentiment from headlines
# ---------------------------------------------------------------------------
_NEWS_SYSTEM = (
    "You are a crypto news sentiment classifier. "
    "Given headlines about a coin, respond with exactly ONE word: "
    "bullish, bearish, or neutral. Nothing else."
)

def analyze_news(symbol: str, headlines: list[str]) -> str:
    """
    Returns 'bullish' | 'bearish' | 'neutral'.
    Falls back to empty string on error (caller keeps vote-based sentiment).
    """
    client = _get_client()
    if client is None or not headlines:
        return ""

    try:
        numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines[:5]))
        prompt = f"Headlines for {symbol.upper()}:\n{numbered}"

        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=5,
            system=[{
                "type": "text",
                "text": _NEWS_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        word = next((b.text.strip().lower() for b in resp.content if b.type == "text"), "")
        if word in ("bullish", "bearish", "neutral"):
            return word
        return ""
    except Exception as e:
        print(f"[CLAUDE] analyze_news error: {e}")
        return ""


# ---------------------------------------------------------------------------
# 3. Free-form Telegram chat
# ---------------------------------------------------------------------------
_CHAT_SYSTEM = """\
You are CryptoAI — an expert crypto analyst assistant inside a Telegram bot.

You have access to real-time scan data (passed as context). When answering:
- Be concise and direct. Telegram has character limits.
- Reference actual numbers from the context when relevant.
- If asked about a specific coin, look it up in the coin list.
- If a question is outside crypto/finance, politely stay on topic.
- Max 3 short paragraphs. No markdown headers. Use plain text.
"""

# Simple per-chat conversation history (in-memory, resets on restart)
_chat_histories: dict[int, list[dict]] = {}
_MAX_HISTORY = 8  # keep last N turns to stay within context


def chat(chat_id: int, user_message: str, context: dict | None = None) -> str:
    """
    Multi-turn conversational AI for Telegram.
    context can contain: top_coins list, fear_greed value, etc.
    Returns the assistant reply text.
    """
    client = _get_client()
    if client is None:
        return "AI chat is not configured. Set ANTHROPIC_API_KEY in .env to enable it."

    # Build context block
    ctx_lines = []
    if context:
        coins = context.get("coins", [])
        if coins:
            ctx_lines.append("== Live Scan (top coins right now) ==")
            for c in coins[:10]:
                ctx_lines.append(
                    f"{c.get('symbol','?').upper()} | "
                    f"Rank #{c.get('market_cap_rank','?')} | "
                    f"24h {c.get('price_change_percentage_24h',0):+.1f}% | "
                    f"7d {c.get('price_change_percentage_7d_in_currency',0):+.1f}% | "
                    f"Score {c.get('final_score',0):.2f} | "
                    f"{c.get('risk_level','?')} risk | {c.get('bucket','?')}"
                )
        if context.get("fear_greed") is not None:
            ctx_lines.append(f"\nFear & Greed Index: {context['fear_greed']}/100")

    history = _chat_histories.setdefault(chat_id, [])

    # If context changed since last turn, prepend it as a system reminder
    user_content = user_message
    if ctx_lines:
        user_content = f"[Context]\n{chr(10).join(ctx_lines)}\n\n[Question]\n{user_message}"

    history.append({"role": "user", "content": user_content})

    # Trim to last _MAX_HISTORY messages
    if len(history) > _MAX_HISTORY:
        history[:] = history[-_MAX_HISTORY:]

    try:
        resp = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=512,
            thinking={"type": "adaptive"},
            system=[{
                "type": "text",
                "text": _CHAT_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=history,
        )
        reply = next((b.text.strip() for b in resp.content if b.type == "text"), "")
        if not reply:
            reply = "Sorry, I couldn't generate a response. Please try again."

        history.append({"role": "assistant", "content": reply})
        return reply

    except anthropic.RateLimitError:
        return "Rate limit reached. Please wait a moment and try again."
    except Exception as e:
        print(f"[CLAUDE] chat error: {e}")
        return f"AI error: {type(e).__name__}. Please try again."


def reset_chat(chat_id: int) -> None:
    """Clear conversation history for a chat."""
    _chat_histories.pop(chat_id, None)
