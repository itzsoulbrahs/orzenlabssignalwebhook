import os
import time
import json
import logging
import secrets
from typing import Optional, Any

import requests
from fastapi import FastAPI, Request, HTTPException, Query
from dotenv import load_dotenv

load_dotenv(override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
THREAD_ID = os.getenv("TELEGRAM_THREAD_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
AUTH_ENABLED = os.getenv("WEBHOOK_AUTH_ENABLED", "false").strip().lower() in ("true", "1", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("orzen-webhook")

app = FastAPI(title="Orzen Webhook Relay")

_tg: requests.Session = requests.Session()
_tg.headers.update({"Connection": "keep-alive"})


@app.get("/")
async def root():
    return {"status": "online", "service": "orzen-telegram-webhook"}


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "2.0"}


@app.post("/webhook")
async def webhook(
    request: Request,
    key: Optional[str] = Query(default=None, description="Shared secret for webhook auth"),
):
    t0 = time.time()

    # --- Webhook secret check (only if explicitly enabled) ---
    if AUTH_ENABLED and WEBHOOK_SECRET:
        if not key or not secrets.compare_digest(str(key), WEBHOOK_SECRET):
            src = request.client.host if request.client else "?"
            logger.error("Webhook rejected | reason=bad_secret | ip=%s", src)
            raise HTTPException(status_code=403, detail="Unauthorized")

    # --- Credential check ---
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Config error | missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        raise HTTPException(status_code=500, detail="Service not configured")

    # --- Read & parse body (JSON or plain text) ---
    raw = await request.body()
    payload: Any = None

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    if payload is None and raw:
        try:
            payload = {"text": raw.decode("utf-8").strip()}
        except UnicodeDecodeError:
            payload = {}

    if payload is None:
        payload = {}

    # --- Build Telegram message ---
    msg = _format_message(payload)
    src = request.client.host if request.client else "?"
    logger.info("Webhook received | ip=%s", src)

    # --- Send to Telegram ---
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    tg_payload: dict[str, Any] = {"chat_id": CHAT_ID, "text": msg}
    if THREAD_ID:
        try:
            tg_payload["message_thread_id"] = int(THREAD_ID)
        except ValueError:
            logger.warning("Invalid TELEGRAM_THREAD_ID value: %s — ignoring", THREAD_ID)

    t1 = time.time()
    try:
        resp = _tg.post(tg_url, json=tg_payload, timeout=10)
        result = resp.json()
    except requests.exceptions.Timeout:
        logger.error("Telegram timeout | elapsed=%.4fs", time.time() - t1)
        raise HTTPException(status_code=504, detail="Telegram API timeout")
    except requests.RequestException:
        logger.error("Telegram request failed | elapsed=%.4fs", time.time() - t1)
        raise HTTPException(status_code=502, detail="Telegram delivery failed")
    except ValueError:
        logger.error("Telegram returned invalid JSON")
        raise HTTPException(status_code=502, detail="Telegram delivery failed")

    if not result.get("ok"):
        err = result.get("description", "unknown")
        logger.error("Telegram API error | %s", err)
        raise HTTPException(status_code=502, detail="Telegram delivery failed")

    mid = result.get("result", {}).get("message_id")
    total = time.time() - t0
    logger.info(
        "Telegram sent | msg_id=%s | tg_latency=%.4fs | total=%.4fs",
        mid,
        time.time() - t1,
        total,
    )

    return {
        "status": "ok",
        "message_id": mid,
        "latency_s": round(total, 4),
    }


def _format_message(payload: Any) -> str:
    """Convert incoming payload to a Telegram-ready message string."""
    if isinstance(payload, str):
        return payload.strip() or "TradingView webhook received ✅"

    if isinstance(payload, dict):
        if "text" in payload:
            return str(payload["text"])

        if "message" in payload:
            return str(payload["message"])

        if payload:
            lines = [
                f"<b>{k}:</b> {v}"
                for k, v in payload.items()
                if v is not None
            ]
            return "\n".join(lines) if lines else "TradingView webhook test received ✅"

        return "TradingView webhook test received ✅"

    return str(payload)
