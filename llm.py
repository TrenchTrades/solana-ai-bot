"""Claude acts as a skeptical second opinion. It can only APPROVE or VETO a signal
the indicators already produced; it can never start a trade on its own."""
import json
import logging

import config as C

log = logging.getLogger("llm")

SYSTEM = """You review trade signals for an automated bot that trades Solana tokens against USDC.
A rule-based strategy (EMA crossover + RSI) has produced the signal below for one token. The bot's
other open positions are listed for context. Act as a skeptical second opinion. APPROVE only if the market data genuinely supports the signal. VETO if it looks
like sideways chop, a weak or likely-failed crossover, an overextended move, abnormal volatility,
or anything else that makes the trade poor. You cannot suggest other trades or sizes.

Respond with ONLY a JSON object, no markdown, no other text:
{"decision": "APPROVE" or "VETO", "confidence": <number 0 to 1>, "reason": "<one sentence>"}"""

_client = None


def _client_get():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=C.ANTHROPIC_API_KEY)
    return _client


def review_signal(signal, snapshot):
    """Returns {approve, decision, confidence, reason, error}. Fails closed (approve=False)."""
    try:
        msg = _client_get().messages.create(
            model=C.CLAUDE_MODEL,
            max_tokens=300,
            system=SYSTEM,
            messages=[{"role": "user", "content":
                       f"Signal: {signal}\nMarket snapshot:\n{json.dumps(snapshot, indent=1)}"}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        decision = str(data.get("decision", "")).upper()
        conf = float(data.get("confidence", 0))
        return {
            "approve": decision == "APPROVE" and conf >= C.MIN_LLM_CONFIDENCE,
            "decision": decision, "confidence": conf,
            "reason": str(data.get("reason", "")), "error": False,
        }
    except Exception as e:
        log.error("Claude review failed: %s", e)
        return {"approve": False, "decision": "ERROR", "confidence": 0.0,
                "reason": str(e), "error": True}
