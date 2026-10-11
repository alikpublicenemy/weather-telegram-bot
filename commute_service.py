"""Minimal Flask webhook service for Telegram inline-button callbacks."""
from __future__ import annotations

import hmac
import logging
import os

import httpx
from flask import Flask, jsonify, request

from vag_commute import build_commute_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
app = Flask(__name__)


def telegram_call(method: str, payload: dict) -> dict:
    if not TELEGRAM_API:
        raise RuntimeError("BOT_TOKEN is not configured")
    response = httpx.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=15.0)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API rejected {method}")
    return data


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.post("/telegram/webhook")
def telegram_webhook():
    if not WEBHOOK_SECRET or not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET
    ):
        return jsonify(error="forbidden"), 403

    update = request.get_json(silent=True) or {}
    callback = update.get("callback_query")
    if not callback or callback.get("data") != "commute_v1":
        return jsonify(status="ignored"), 200

    callback_id = callback.get("id")
    from_user = callback.get("from") or {}
    user_id = str(from_user.get("id", ""))
    if user_id != TELEGRAM_USER_ID:
        if callback_id:
            try:
                telegram_call("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "Эта кнопка доступна только владельцу бота.",
                    "show_alert": True,
                })
            except Exception:
                logger.warning("Could not answer unauthorized callback")
        return jsonify(status="unauthorized"), 200

    if callback_id:
        try:
            telegram_call("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:
            logger.exception("Could not acknowledge Telegram callback")

    message = build_commute_message()
    try:
        telegram_call("sendMessage", {
            "chat_id": TELEGRAM_USER_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })
    except Exception:
        logger.exception("Could not send commute calculation to Telegram")
        return jsonify(error="telegram_send_failed"), 502
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
