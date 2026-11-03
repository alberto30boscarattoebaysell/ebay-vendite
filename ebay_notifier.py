#!/usr/bin/env python3
"""
eBay Sales Notifier
Controlla periodicamente i tuoi ordini eBay (Fulfillment API) e invia
un messaggio Telegram per ogni nuova vendita.

Uso:
  python ebay_notifier.py                 # avvia il monitoraggio
  python ebay_notifier.py --find-chat-id  # mostra il tuo chat_id Telegram
  python ebay_notifier.py --test-telegram # invia un messaggio di prova
  python ebay_notifier.py --once          # un solo controllo e poi esce
"""

import base64
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------- config
EBAY_ENV = os.getenv("EBAY_ENV", "production").lower()
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
EBAY_REFRESH_TOKEN = os.getenv("EBAY_REFRESH_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))
STATE_FILE = Path(os.getenv("STATE_FILE", BASE_DIR / "seen_orders.json"))

API_HOST = "https://api.sandbox.ebay.com" if EBAY_ENV == "sandbox" else "https://api.ebay.com"
TOKEN_URL = f"{API_HOST}/identity/v1/oauth2/token"
ORDERS_URL = f"{API_HOST}/sell/fulfillment/v1/order"
SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ebay-notifier")


# ---------------------------------------------------------------- telegram
def telegram_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        log.error("Errore Telegram %s: %s", r.status_code, r.text)
    r.raise_for_status()


def find_chat_id() -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    data = requests.get(url, timeout=20).json()
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("username") or chat.get("title") or chat.get("first_name")
            chats[chat["id"]] = name
    if not chats:
        print("Nessun messaggio trovato. Scrivi qualcosa al tuo bot su Telegram e riprova.")
        return
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")


# ---------------------------------------------------------------- ebay auth
class EbayAuth:
    def __init__(self):
        self._token = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        basic = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
        r = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": EBAY_REFRESH_TOKEN,
                "scope": SCOPE,
            },
            timeout=30,
        )
        if r.status_code in (400, 401):
            raise PermissionError(f"Refresh token rifiutato da eBay: {r.text}")
        r.raise_for_status()
        payload = r.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 7200))
        log.info("Access token eBay rinnovato")
        return self._token


# ---------------------------------------------------------------- orders
def fetch_recent_orders(auth: EbayAuth) -> list:
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    params = {"filter": f"creationdate:[{since_str}..]", "limit": 50}
    url = ORDERS_URL
    orders = []
    while url:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {auth.token()}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        orders.extend(data.get("orders", []))
        url = data.get("next")  # URL completo della pagina successiva
        params = None
    return orders


def format_order(order: dict) -> str:
    total = order.get("pricingSummary", {}).get("total", {})
    buyer = order.get("buyer", {}).get("username", "?")
    lines = []
    for item in order.get("lineItems", []):
        title = html.escape(item.get("title", "Articolo"))
        qty = item.get("quantity", 1)
        lines.append(f"• <b>{title}</b> × {qty}")
    created = order.get("creationDate", "")
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone()
        created = dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        pass
    return (
        "🛒 <b>Nuova vendita su eBay!</b>\n\n"
        + "\n".join(lines)
        + f"\n\n💶 Totale: <b>{total.get('value', '?')} {total.get('currency', '')}</b>"
        + f"\n👤 Acquirente: {html.escape(buyer)}"
        + f"\n🕒 {created}"
        + f"\n🧾 Ordine: <code>{html.escape(order.get('orderId', ''))}</code>"
    )


# ---------------------------------------------------------------- state
def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("File di stato corrotto, lo ricreo")
        return None


def save_state(seen: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 30)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}  # pulizia vecchi ordini
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=1))
    tmp.replace(STATE_FILE)


# Piccolo file di servizio: errori consecutivi e data dell'ultimo avviso,
# così gli avvisi di problema arrivano al massimo una volta al giorno.
META_FILE = STATE_FILE.with_name("meta.json")


def load_meta() -> dict:
    try:
        return json.loads(META_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_meta(meta: dict) -> None:
    META_FILE.write_text(json.dumps(meta))


def alert_once_per_day(meta: dict, key: str, text: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    if meta.get(key) == today:
        return
    try:
        telegram_send(text)
        meta[key] = today
    except Exception as e:
        log.error("Invio avviso Telegram fallito: %s", e)


# ---------------------------------------------------------------- main loop
def check_once(auth: EbayAuth, seen: dict, notify: bool) -> int:
    new = 0
    orders = sorted(fetch_recent_orders(auth), key=lambda o: o.get("creationDate", ""))
    try:
        for order in orders:
            oid = order.get("orderId")
            if not oid or oid in seen:
                continue
            if notify:
                telegram_send(format_order(order))
                log.info("Notificata una nuova vendita")
            seen[oid] = datetime.now(timezone.utc).isoformat()
            new += 1
    finally:
        save_state(seen)  # salva anche se Telegram fallisce a metà
    return new


def run(once: bool = False) -> None:
    missing = [k for k, v in {
        "EBAY_CLIENT_ID": EBAY_CLIENT_ID,
        "EBAY_CLIENT_SECRET": EBAY_CLIENT_SECRET,
        "EBAY_REFRESH_TOKEN": EBAY_REFRESH_TOKEN,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing:
        sys.exit(f"Mancano queste impostazioni: {', '.join(missing)}")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    auth = EbayAuth()
    seen = load_state()
    first_run = seen is None
    if first_run:
        seen = {}
    meta = load_meta()

    while True:
        try:
            # Al primo avvio registra gli ordini esistenti senza notificarli
            n = check_once(auth, seen, notify=not first_run)
            if first_run:
                log.info("Primo avvio: %d ordini esistenti registrati (non notificati)", n)
                telegram_send("✅ Monitor eBay attivo! Ti scriverò qui a ogni nuova vendita.")
                first_run = False
            elif n:
                log.info("%d nuove vendite", n)
            meta["errors"] = 0
        except PermissionError as e:
            log.error(str(e))
            alert_once_per_day(
                meta, "token_alert",
                "⚠️ Il collegamento con eBay non è più valido. "
                "Ricollega l'account (vedi la guida, passo «Collega eBay»).",
            )
            save_meta(meta)
            sys.exit(1)
        except Exception as e:  # rete, eBay temporaneamente giù, ecc.
            meta["errors"] = meta.get("errors", 0) + 1
            log.warning("Errore durante il controllo (%d di fila): %s", meta["errors"], e)
            if meta["errors"] >= 10:
                alert_once_per_day(
                    meta, "error_alert",
                    f"⚠️ Monitor eBay: {meta['errors']} errori di fila. "
                    f"Ultimo: {html.escape(str(e))[:300]}",
                )
        save_meta(meta)
        if once:
            break
        errors = meta.get("errors", 0)
        time.sleep(POLL_SECONDS if errors == 0 else min(POLL_SECONDS * (errors + 1), 1800))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--find-chat-id":
        find_chat_id()
    elif arg == "--test-telegram":
        telegram_send("👋 Test riuscito: il bot può scriverti!")
        print("Messaggio inviato.")
    elif arg == "--once":
        run(once=True)
    else:
        run()
