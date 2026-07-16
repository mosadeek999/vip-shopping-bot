
"""
VIP_Bot_Pro — Smart Telegram Price Comparison Bot
Production-ready bot that searches for products via the Serper Shopping API,
applies smart semantic filtering, and returns the top 10 results sorted
cheapest → most expensive.

Access model (priority order):
1. VIP_USERS — permanent free access, bypasses subscription entirely
2. SUBSCRIBERS — paid Telegram-Stars subscribers (100 ⭐ / 30 days)
3. Everyone else → told to subscribe

Sections:
1. Imports & Configuration
2. Persistence (subscribers JSON)
3. Auth helpers
4. Price parsing
5. Smart filter_results() ← edit blacklist / thresholds here
6. Serper API fetch
7. Markdown formatting
8. Command handlers (/start, /subscribe, /status, /add_vip)
9. Payment handlers (pre-checkout, successful payment)
10. Message handler (search)
11. Entry point
"""

──────────────────────────────────────────────────────────────────────────────
1. IMPORTS & CONFIGURATION
──────────────────────────────────────────────────────────────────────────────

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
logger = logging.getLogger(name)


── Secrets & env vars ────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SERPER_API_KEY: str = os.environ.get("SERPER_API_KEY", "")
OWNER_ID: int = int(os.environ.get("OWNER_ID", "0"))

if not TELEGRAM_BOT_TOKEN:
raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it in Replit Secrets.")
if not SERPER_API_KEY:
raise RuntimeError("SERPER_API_KEY is not set. Add it in Replit Secrets.")

── Search settings ───────────────────────────────────────────────────────────

SERPER_SHOPPING_URL = "https://google.serper.dev/shopping"
SERPER_FETCH_COUNT = 20 # how many raw results to fetch from Serper
MAX_RESULTS = 10 # how many filtered results to show the user
_NO_ACCESS_MSG = "🚫 Access denied. Please use /subscribe to get access."

── Subscription settings ─────────────────────────────────────────────────────

STARS_PRICE = 100 # Telegram Stars per subscription period
SUBSCRIPTION_DAYS = 30 # days granted per payment
SUBSCRIBERS_FILE = Path(file).parent / "subscribers.json"


──────────────────────────────────────────────────────────────────────────────
2. PERSISTENCE — subscribers.json
──────────────────────────────────────────────────────────────────────────────
Format: { "<user_id>": "<ISO-8601 expiry datetime UTC>" }
The file is read on startup and updated on every new subscription.

def _load_subscribers() -> dict[int, datetime]:
"""Load the subscriber map from disk. Missing / corrupt file → empty dict."""
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
"""Persist the subscriber map to disk."""
try:
raw = {str(uid): expiry.isoformat() for uid, expiry in subs.items()}
SUBSCRIBERS_FILE.write_text(json.dumps(raw, indent=2))
except Exception as exc:
logger.error("Failed to save subscribers.json: %s", exc)


In-memory VIP list (permanent free access — seeded with owner)
VIP_USERS: set[int] = set()
if OWNER_ID:
VIP_USERS.add(OWNER_ID)

In-memory subscriber map {user_id: expiry_utc}
SUBSCRIBERS: dict[int, datetime] = _load_subscribers()


──────────────────────────────────────────────────────────────────────────────
3. AUTH HELPERS
──────────────────────────────────────────────────────────────────────────────

def is_vip(user_id: int) -> bool:
"""Permanent free access — no subscription check needed."""
return user_id in VIP_USERS


def is_subscribed(user_id: int) -> bool:
"""Return True if the user has an active (non-expired) subscription."""
expiry = SUBSCRIBERS.get(user_id)
if expiry is None:
return False
return datetime.now(timezone.utc) < expiry


def has_access(user_id: int) -> bool:
"""Full access gate: VIP list OR active subscription."""
return is_vip(user_id) or is_subscribed(user_id)


def grant_subscription(user_id: int) -> datetime:
"""
Grant (or extend) a subscription by SUBSCRIPTION_DAYS days.
If already subscribed, extends from the current expiry; otherwise from now.
Returns the new expiry datetime.
"""
now = datetime.now(timezone.utc)
current_expiry = SUBSCRIBERS.get(user_id)
base = current_expiry if (current_expiry and current_expiry > now) else now
new_expiry = base + timedelta(days=SUBSCRIPTION_DAYS)
SUBSCRIBERS[user_id] = new_expiry
_save_subscribers(SUBSCRIBERS)
logger.info("Subscription granted: user_id=%d, expires=%s", user_id, new_expiry.isoformat())
return new_expiry


def subscription_expiry_str(user_id: int) -> str:
"""Human-readable expiry date for the given subscriber, or 'N/A'."""
expiry = SUBSCRIBERS.get(user_id)
if expiry is None:
return "N/A"
return expiry.strftime("%Y-%m-%d %H:%M UTC")


──────────────────────────────────────────────────────────────────────────────
4. PRICE PARSING
──────────────────────────────────────────────────────────────────────────────

def clean_price(raw_price: str) -> float:
"""
Parse a price string to a comparable float value.

Handles:
- Currency symbols and codes ($`, €, £, ¥, USD, EUR, …)
- Thousands separators with dots (1.299 → 1299) or commas (1,299 → 1299)
- Decimal separators with commas (1,99 → 1.99) or dots (1.99 → 1.99)
- Mixed formats (1.299,99 → 1299.99 and 1,299.99 → 1299.99)

Returns float('inf') when price cannot be parsed so the item sorts last.
"""
if not raw_price:
return float("inf")

cleaned = re.sub(r"[^\d.,]", "", raw_price.strip())
if not cleaned:
return float("inf")

dot_pos = cleaned.rfind(".")
comma_pos = cleaned.rfind(",")

if dot_pos != -1 and comma_pos != -1:
# Both separators — the rightmost one is the decimal separator
if comma_pos > dot_pos:
cleaned = cleaned.replace(".", "").replace(",", ".") # 1.299,99
else:
cleaned = cleaned.replace(",", "") # 1,299.99

elif comma_pos != -1:
parts = cleaned.split(",")
if len(parts) == 2 and len(parts[1]) <= 2:
cleaned = cleaned.replace(",", ".") # decimal comma: 19,99
else:
cleaned = cleaned.replace(",", "") # thousands comma: 1,299

elif dot_pos != -1:
parts = cleaned.split(".")
# Multiple dots or exactly 3 fractional digits → thousands separator
if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
cleaned = cleaned.replace(".", "") # 1.299 → 1299
# else: decimal dot — leave as-is: 19.99

try:
return float(cleaned)
except ValueError:
return float("inf")


──────────────────────────────────────────────────────────────────────────────
5. SMART FILTER — filter_results()
──────────────────────────────────────────────────────────────────────────────
╔══════════════════════════════════════════════════════════════════════════╗
║ HOW TO CUSTOMISE THIS FUNCTION ║
║ ║
║ A) KEYWORD BLACKLIST ║
║ Add or remove words from ACCESSORY_BLACKLIST below. ║
║ Any product whose title contains one of these words (case- ║
║ insensitive, whole-word match) is dropped before sorting. ║
║ Example: add "Bag" to exclude product bags from results. ║
║ ║
║ B) PRICE OUTLIER THRESHOLD ║
║ PRICE_OUTLIER_RATIO (default 0.50) controls what counts as a ║
║ suspiciously cheap price. ║
║ Formula: drop item if item_price < avg_price * PRICE_OUTLIER_RATIO ║
║ Set to 0.0 to disable price filtering entirely. ║
║ Set to 0.7 to be more aggressive (drop anything < 70 % of avg). ║
╚══════════════════════════════════════════════════════════════════════════╝

── Keyword blacklist ─────────────────────────────────────────────────────────
Words that indicate accessories / non-primary products.
Whole-word, case-insensitive matching is applied automatically.
ACCESSORY_BLACKLIST: list[str] = [
"Case",
"Cover",
"Protector",
"Screen",
"Sticker",
"Cable",
"Holder",
"Strap",
"Skin",
"Pouch",
"Sleeve",
"Stand",
"Mount",
"Charger",
"Adapter",
"Dock",
"Stylus",
"Folio",
"Bumper",
"Shell",
"Film",
"Glass",
"Tempered",
"Wallet",
"Clip",
"Replica",
"Fake",
"Copy",
"Knockoff",
]

Pre-compile the blacklist into a single regex for speed
_BLACKLIST_RE = re.compile(
r"\b(" + "|".join(re.escape(w) for w in ACCESSORY_BLACKLIST) + r")\b",
re.IGNORECASE,
)

── Price outlier threshold ───────────────────────────────────────────────────
PRICE_OUTLIER_RATIO: float = 0.50 # drop items cheaper than 50 % of average


def filter_results(items: list[dict]) -> list[dict]:
"""
Apply two-stage smart filtering and return items sorted cheapest → most expensive.

Stage 1 — Keyword filter:
Drop any item whose title matches a word in ACCESSORY_BLACKLIST.
(Whole-word, case-insensitive match via _BLACKLIST_RE.)

Stage 2 — Price outlier filter:
1. Collect numeric prices of the surviving items.
2. Compute the average of those prices.
3. Drop any item whose price is below (average × PRICE_OUTLIER_RATIO).
This removes suspiciously cheap / misleading listings
(e.g. a `$0.99 "accessory bundle" slipping through the keyword filter).
Items with unparseable prices (inf) are also dropped here.

Items are sorted ascending by numeric price after both filters.
"""
# ── Stage 1: keyword filter ───────────────────────────────────────────────
keyword_passed = [
item for item in items
if not _BLACKLIST_RE.search(item["title"])
]
dropped_kw = len(items) - len(keyword_passed)
if dropped_kw:
logger.debug("Keyword filter dropped %d items", dropped_kw)

# ── Stage 2: price outlier filter ────────────────────────────────────────
# Exclude items with unparseable prices first
priced = [i for i in keyword_passed if i["numeric_price"] != float("inf")]

if not priced:
return [] # nothing survived

if PRICE_OUTLIER_RATIO > 0 and len(priced) > 1:
avg_price = sum(i["numeric_price"] for i in priced) / len(priced)
threshold = avg_price * PRICE_OUTLIER_RATIO
price_passed = [i for i in priced if i["numeric_price"] >= threshold]
dropped_price = len(priced) - len(price_passed)
if dropped_price:
logger.debug(
"Price outlier filter dropped %d items (avg=%.2f, threshold=%.2f)",
dropped_price, avg_price, threshold,
)
else:
price_passed = priced

# ── Sort cheapest → most expensive ────────────────────────────────────────
price_passed.sort(key=lambda x: x["numeric_price"])

return price_passed


──────────────────────────────────────────────────────────────────────────────
6. SERPER API FETCH
──────────────────────────────────────────────────────────────────────────────



async def fetch_shopping_results(query: str) -> list[dict]:
headers = {
"X-API-KEY": SERPER_API_KEY,
"Content-Type": "application/json",
}
payload = {
"q": query,
"gl": "us",
"hl": "en",
"num": 40
}
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



──────────────────────────────────────────────────────────────────────────────
7. MARKDOWN FORMATTING
──────────────────────────────────────────────────────────────────────────────

_MD_SPECIAL = re.compile(r"([_*[]()~`>#+=|{}.!\-])")


def md_escape(text: str) -> str:
"""Escape a plain string for safe use in MarkdownV2 messages."""
return _MD_SPECIAL.sub(r"\\1", text)


def format_results(query: str, items: list[dict]) -> str:
"""
Build a MarkdownV2 result message.

Format per item:
N. Product Title
💰 $X.XX
"""
header = f"🔍 {md_escape(query)} — Top {len(items)} Results\n"
lines = [header]
for i, item in enumerate(items, start=1):
title = md_escape(item["title"])
link = item["link"] or "https://google.com"
price = md_escape(item["price_str"]) if item["price_str"] else "N/A"
lines.append(f"{i}\. {title}\n 💰 {price}\n")
return "\n".join(lines)


──────────────────────────────────────────────────────────────────────────────
8. COMMAND HANDLERS
──────────────────────────────────────────────────────────────────────────────

── /start ─────────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user_id = update.effective_user.id

if is_vip(user_id):
tag = "👑 VIP member"
elif is_subscribed(user_id):
tag = f"✅ Subscriber \(expires {md_escape(subscription_expiry_str(user_id))}\)"
else:
tag = "🔒 Not subscribed"

await update.message.reply_text(
"👋 Welcome to VIP Price Comparison Bot\!\n\n"
f"Your status: {tag}\n\n"
"📦 Send me any product name and I'll search the web for the best prices\.\n"
"🔢 Results are filtered \(no accessories\) and sorted cheapest → most expensive\.\n\n"
"Commands:\n"
"• /subscribe — get 30 days of access for 100 ⭐\n"
"• /status — check your subscription\n",
parse_mode=ParseMode.MARKDOWN_V2,
)


── /status ────────────────────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user_id = update.effective_user.id

if is_vip(user_id):
text = "👑 You have permanent VIP access \— no subscription needed\."
elif is_subscribed(user_id):
expiry = md_escape(subscription_expiry_str(user_id))
text = f"✅ Your subscription is active\.\nExpires: {expiry}"
else:
text = (
"❌ You don't have an active subscription\.\n\n"
"Use /subscribe to get 30 days of access for 100 ⭐\."
)

await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


── /subscribe ─────────────────────────────────────────────────────────────────

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user_id = update.effective_user.id

if is_vip(user_id):
await update.message.reply_text(
"👑 You have permanent VIP access — no payment needed\!",
parse_mode=ParseMode.MARKDOWN_V2,
)
return

if is_subscribed(user_id):
expiry = md_escape(subscription_expiry_str(user_id))
await update.message.reply_text(
f"✅ You already have an active subscription\.\n"
f"Expires: {expiry}\n\n"
"Paying again will extend your subscription by 30 days\.",
parse_mode=ParseMode.MARKDOWN_V2,
)
# Fall through — let them pay again to extend

await context.bot.send_invoice(
chat_id=update.effective_chat.id,
title="VIP Price Bot — 30-Day Subscription",
description=(
"30 days of unlimited access to smart product price comparison. "
"Results are filtered and sorted from cheapest to most expensive."
),
payload="vip_subscription_30d", # verified in pre-checkout handler
currency="XTR", # Telegram Stars
prices=[LabeledPrice("30-Day Access", STARS_PRICE)],
# No provider_token needed for Stars payments
)
logger.info("Invoice sent to user_id=%d", user_id)


── /add_vip ───────────────────────────────────────────────────────────────────

async def add_vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
"""Owner-only: add a user_id to the permanent VIP list."""
requester_id = update.effective_user.id

if requester_id != OWNER_ID:
await update.message.reply_text(
"🚫 Access Denied", parse_mode=ParseMode.MARKDOWN_V2
)
logger.warning("Unauthorised /add_vip: user_id=%d", requester_id)
return

args = context.args
if not args or not args[0].lstrip("-").isdigit():
await update.message.reply_text(
"⚠️ Usage: /add_vip &lt;user_id>",
parse_mode=ParseMode.MARKDOWN_V2,
)
return

new_uid = int(args[0])
if new_uid in VIP_USERS:
await update.message.reply_text(
f"ℹ️ User {new_uid} is already VIP\.",
parse_mode=ParseMode.MARKDOWN_V2,
)
return

VIP_USERS.add(new_uid)
logger.info("Owner granted VIP to user_id=%d", new_uid)
await update.message.reply_text(
f"✅ User {new_uid} added to the VIP list\.",
parse_mode=ParseMode.MARKDOWN_V2,
)


──────────────────────────────────────────────────────────────────────────────
9. PAYMENT HANDLERS (Telegram Stars)
──────────────────────────────────────────────────────────────────────────────

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
"""
Telegram calls this before charging the user.
We MUST respond within 10 seconds or the payment is cancelled.
Validate the payload and approve (answer_pre_checkout_query ok=True).
"""
query = update.pre_checkout_query

if query.invoice_payload != "vip_subscription_30d":
# Unknown payload — reject
await query.answer(ok=False, error_message="Invalid subscription payload.")
logger.warning(
"Rejected pre-checkout with unexpected payload: %s (user_id=%d)",
query.invoice_payload, query.from_user.id,
)
return

# All good — approve the payment
await query.answer(ok=True)
logger.info("Pre-checkout approved for user_id=%d", query.from_user.id)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
"""
Called after a successful Stars payment.
Grants the subscription and confirms to the user.
"""
user_id = update.effective_user.id
payment = update.message.successful_payment

logger.info(
"Successful payment: user_id=%d, stars=%d, charge_id=%s",
user_id,
payment.total_amount,
payment.telegram_payment_charge_id,
)

new_expiry = grant_subscription(user_id)
expiry_str = md_escape(new_expiry.strftime("%Y-%m-%d %H:%M UTC"))

await update.message.reply_text(
"🎉 Payment received — subscription activated\!\n\n"
f"⭐ Stars paid: {payment.total_amount}\n"
f"📅 Your access expires: {expiry_str}\n\n"
"You can now search for any product\. Just send me the product name\!",
parse_mode=ParseMode.MARKDOWN_V2,
)


──────────────────────────────────────────────────────────────────────────────
10. MESSAGE HANDLER — product search
──────────────────────────────────────────────────────────────────────────────




async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
user_id = update.effective_user.id
query = (update.message.text or "").strip()

if not has_access(user_id):
await update.message.reply_text(_NO_ACCESS_MSG, parse_mode=ParseMode.MARKDOWN_V2)
return

if not query:
return

try:
loop = asyncio.get_event_loop()

# تصنيف الرسالة باستخدام run_in_executor
classification = await loop.run_in_executor(None, lambda: GROQ_CLIENT.chat.completions.create(
messages=[{"role": "system", "content": "Is the user message a request to buy or search for a product? Answer only YES or NO."},
{"role": "user", "content": query}],
model="llama-3.3-70b-versatile"
).choices[0].message.content.strip().upper())

if "YES" in classification:
items = await fetch_shopping_results(query)
if items:
reply = format_results(query, items[:10])
await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
else:
await update.message.reply_text("No results found.")
else:
# الدردشة العادية باستخدام run_in_executor
chat_completion = await loop.run_in_executor(None, lambda: GROQ_CLIENT.chat.completions.create(
messages=[{"role": "user", "content": query}],
model="llama-3.3-70b-versatile"
))
await update.message.reply_text(chat_completion.choices[0].message.content)

except Exception as e:
logger.error(f"Error in handle_message: {e}")
await update.message.reply_text("An error occurred. Please try again later.")



──────────────────────────────────────────────────────────────────────────────
11. ENTRY POINT
──────────────────────────────────────────────────────────────────────────────

def main() -> None:
logger.info("Starting VIP_Bot_Pro …")
logger.info("Owner ID : %s", OWNER_ID or "(not set — /add_vip disabled)")
logger.info("VIP users : %s", VIP_USERS)
logger.info("Subscribers : %d loaded from disk", len(SUBSCRIBERS))

app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

# Commands
app.add_handler(CommandHandler("start", start_command))
app.add_handler(CommandHandler("status", status_command))
app.add_handler(CommandHandler("subscribe", subscribe_command))
app.add_handler(CommandHandler("add_vip", add_vip_command))

# Payments (Stars)
app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

# Search (any non-command text)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

logger.info("Bot is running — polling mode active.")
app.run_polling(
allowed_updates=Update.ALL_TYPES,
drop_pending_updates=True,
)


if name == "main":
main()
