"""Telegram alerts. Send-only: nobody can control the trading bot through Telegram.

  python notify.py --chat-id   find your chat ID (message your bot first)
  python notify.py             send a test message
"""
import logging
import sys
import time

import requests

import config as C

log = logging.getLogger("notify")
_last_sent = {}


def enabled():
    return bool(C.TELEGRAM_BOT_TOKEN and C.TELEGRAM_CHAT_ID)


def send(text, key=None, every=0):
    """Sends text. If key is given, the same key is sent at most once per `every` seconds."""
    if not enabled():
        return
    if key:
        now = time.time()
        if now - _last_sent.get(key, 0) < every:
            return
        _last_sent[key] = now
    try:
        r = requests.post(f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": C.TELEGRAM_CHAT_ID, "text": text,
                                "disable_web_page_preview": True}, timeout=10)
        if not r.ok:
            log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def tx_link(tx):
    return f"\nhttps://solscan.io/tx/{tx}" if tx and tx != "paper" else ""


def _find_chat_id():
    if not C.TELEGRAM_BOT_TOKEN:
        sys.exit("Add TELEGRAM_BOT_TOKEN to .env first.")
    r = requests.get(f"https://api.telegram.org/bot{C.TELEGRAM_BOT_TOKEN}/getUpdates", timeout=10)
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"Telegram rejected the token: {data.get('description')}")
    chats = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        sys.exit("No messages found. Open your bot in Telegram, press Start, send 'hi', then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


if __name__ == "__main__":
    if "--chat-id" in sys.argv:
        _find_chat_id()
    elif not enabled():
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
    else:
        send("✅ Test message from your Solana trading bot. Alerts are working!")
        print("Sent — check Telegram.")
