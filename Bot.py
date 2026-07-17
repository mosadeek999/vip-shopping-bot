"""
VIP_Bot_Pro - Smart Telegram Price Comparison Bot
Production-ready bot that searches for products via the Serper Shopping API,
applies smart semantic filtering, and returns the top 10 results sorted
cheapest - most expensive.
"""

import os
import re
import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

import httpx
from groq import Groq
GROQ_CLIENT = Groq(api_key=os.environ.get("GROQ_API_KEY"))

from telegram import Update, LabeledPrice
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

keep_alive()

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SERPER_API_KEY: str = os.environ.get("SERPER_API_KEY", "")
OWNER_ID: int = int(os.environ.get("OWNER_ID", "0"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
if not SERPER_API_KEY:
    raise RuntimeError("SERPER_API_KEY is not set.")

SERPER_SHOPPING_URL = "https://google.serper.dev/shopping"
SERPER_FETCH_COUNT = 20
MAX_RESULTS = 10
_NO_ACCESS_MSG = "Access denied. Please use /subscribe to get access."

STARS_PRICE = 100
SUBSCRIPTION_DAYS = 30
SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "subscribers.json"

def _load_subscribers() -> dict[int, datetime]:
    if not SUBSCRIBERS_FILE.exists():
        return {}
    try:
        raw: dict[str, str] = json.loads(SUBSCRIBERS_FILE.read_text())
        return {
            int(uid): datetime.fromisoformat(expiry)
            for uid, expiry in raw.items()
        }
    except Exception as exc:
        logger.error("Failed to load subscribers.json: %s", exc)
        return {}

def _save_subscribers(subs: dict[int, datetime]) -> None:
    try:
        raw = {str(uid): expiry.isoformat() for uid, expiry in subs.items()}
        SUBSCRIBERS_FILE.write_text(json.dumps(raw, indent=2))
    except Exception as exc:
        logger.error("Failed to save subscribers.json: %s", exc)

VIP_USERS: set[int] = set()
if OWNER_ID:
    VIP_USERS.add(OWNER_ID)

SUBSCRIBERS: dict[int, datetime] = _load_subscribers()

def is_vip(user_id: int) -> bool:
    return user_id in VIP_USERS

def is_subscribed(user_id: int) -> bool:
    expiry = SUBSCRIBERS.get(user_id)
    if expiry is None:
        return False
    return datetime.now(timezone.utc) < expiry

def has_access(user_id: int) -> bool:
    return is_vip(user_id) or is_subscribed(user_id)

def grant_subscription(user_id: int) -> datetime:
    now = datetime.now(timezone.utc)
    current_expiry = SUBSCRIBERS.get(user_id)
    base = current_expiry if (current_expiry and current_expiry > now) else now
    new_expiry = base + timedelta(days=SUBSCRIPTION_DAYS)
    SUBSCRIBERS[user_id] = new_expiry
    _save_subscribers(SUBSCRIBERS)
    return new_expiry

def subscription_expiry_str(user_id: int) -> str:
    expiry = SUBSCRIBERS.get(user_id)
    if expiry is None:
        return "N/A"
    return expiry.strftime("%Y-%m-%d %H:%M UTC")

def clean_price(raw_price: str) -> float:
    if not raw_price:
        return float("inf")
    cleaned = re.sub(r"[^\d.,]", "", raw_price.strip())
    if not cleaned:
        return float("inf")
    dot_pos = cleaned.rfind(".")
    comma_pos = cleaned.rfind(",")
    if dot_pos != -1 and comma_pos != -1:
        if comma_pos > dot_pos:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif comma_pos != -1:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif dot_pos != -1:
        parts = cleaned.split(".")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
            cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return float("inf")

ACCESSORY_BLACKLIST: list[str] = [
    "Case", "Cover", "Protector", "Screen", "Sticker", "Cable", "Holder",
    "Strap", "Skin", "Pouch", "Sleeve", "Stand", "Mount", "Charger",
    "Adapter", "Dock", "Stylus", "Folio", "Bumper", "Shell", "Film",
    "Glass", "Tempered", "Wallet", "Clip", "Replica", "Fake", "Copy", "Knockoff",
]

_BLACKLIST_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in ACCESSORY_BLACKLIST) + r")\b",
    re.IGNORECASE,
)

PRICE_OUTLIER_RATIO: float = 0.50

def filter_results(items: list[dict]) -> list[dict]:
    keyword_passed = [item for item in items if not _BLACKLIST_RE.search(item["title"])]
    priced = [i for i in keyword_passed if i["numeric_price"] != float("inf")]
    if not priced:
        return []
    if PRICE_OUTLIER_RATIO > 0 and len(priced) > 1:
        avg_price = sum(i["numeric_price"] for i in priced) / len(priced)
        threshold = avg_price * PRICE_OUTLIER_RATIO
        price_passed = [i for i in priced if i["numeric_price"] >= threshold]
    else:
        price_passed = priced
    price_passed.sort(key=lambda x: x["numeric_price"])
    return price_passed

async def fetch_shopping_results(query: str) -> list[dict]:
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "gl": "us", "hl": "en", "num": 40}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(SERPER_SHOPPING_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    raw_items = data.get("shopping", [])
    processed_items = []
    for item in raw_items:
        price_str = item.get("price", "")
        if price_str:
            processed_items.append({
                "title": item.get("title", "Unknown"),
                "link": item.get("link") or item.get("productLink") or "",
                "price_str": price_str,
                "numeric_price": clean_price(price_str),
            })
    return filter_results(processed_items)[:MAX_RESULTS]

_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+=|{}.!\-])")

def md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)

def format_results(query: str, items: list[dict]) -> str:
    header = f"🔍 {md_escape(query)} - Top {len(items)} Results\n"
    lines = [header]
    for i, item in enumerate(items, start=1):
      title = md_escape(item["title"])
        price = md_escape(item["price_str"]) if item["price_str"] else "N/A"
lines.append(f"{i}\\. [{title}]({item['link'].replace(')', '\\)')})\n 💰 {price}")

    return "\n".join(lines)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_vip(user_id):
        tag = "VIP member"
    elif is_subscribed(user_id):
        tag = f"Subscriber (expires {md_escape(subscription_expiry_str(user_id))})"
    else:
        tag = "Not subscribed"
    await update.message.reply_text(f"Welcome! Your status: {tag}", parse_mode=ParseMode.MARKDOWN_V2)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_vip(user_id):
        text = "You have permanent VIP access."
    elif is_subscribed(user_id):
        text = f"Your subscription is active. Expires: {subscription_expiry_str(user_id)}"
    else:
        text = "You don't have an active subscription."
    await update.message.reply_text(text)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="VIP Subscription",
        description="30 days access.",
        payload="vip_subscription_30d",
        currency="XTR",
        prices=[LabeledPrice("30-Day Access", STARS_PRICE)],
    )

async def add_vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    args = context.args
    if args:
        new_uid = int(args[0])
        VIP_USERS.add(new_uid)
        await update.message.reply_text(f"User {new_uid} added to VIP.")

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    grant_subscription(user_id)
    await update.message.reply_text("Subscription activated!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    query = (update.message.text or "").strip()
    if not has_access(user_id):
        await update.message.reply_text(_NO_ACCESS_MSG)
        return
    if not query: return
    try:
        items = await fetch_shopping_results(query)
        if items:
            await update.message.reply_text(format_results(query, items), parse_mode=None)
        else:
            await update.message.reply_text("No results found.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("An error occurred.")

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("add_vip", add_vip_command))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
