import os
import re
import logging
from datetime import datetime, timezone
from collections import defaultdict

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
NOTION_TOKEN    = os.environ["NOTION_TOKEN"]
DATABASE_ID     = os.environ["DATABASE_ID"]
ANTHROPIC_TOKEN = os.environ["ANTHROPIC_TOKEN"]
ALLOWED_USERS = set(u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip())

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CATEGORIES = [
    "🍕 Їжа", "🚌 Транспорт", "🏠 Житло", "💊 Здоровʼя",
    "👗 Одяг", "💄 Краса", "🐾 Тварини", "📚 Навчання",
    "🎉 Розваги", "✈️ Подорожі", "💸 Інше",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def clean_category(cat: str) -> str:
    """Strip emoji prefix from category button label."""
    return re.sub(r"^[\U00010000-\U0010ffff\u2600-\u26FF\u2700-\u27BF\s]+", "", cat).strip()

CATEGORY_NAMES = [
    "Їжа", "Транспорт", "Житло", "Здоровʼя",
    "Одяг", "Краса", "Тварини", "Навчання",
    "Розваги", "Подорожі", "Інше",
]

async def ai_categorize(description: str) -> str | None:
    """Ask Claude to pick a category for the expense description."""
    prompt = (
        f"Визнач категорію витрати за описом: «{description}»\n\n"
        f"Доступні категорії: {', '.join(CATEGORY_NAMES)}\n\n"
        f"Відповідай ТІЛЬКИ назвою категорії, без пояснень."
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_TOKEN,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            result = r.json()["content"][0]["text"].strip()
            if result in CATEGORY_NAMES:
                return result
            for cat in CATEGORY_NAMES:
                if cat.lower() in result.lower():
                    return cat
            return None
    except Exception as e:
        logger.error(f"AI categorization error: {e}")
        return None

async def add_to_notion(amount: float, category: str, note: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Назва": {"title": [{"text": {"content": note or category}}]},
            "Сума": {"number": amount},
            "Категорія": {"select": {"name": category}},
            "Дата": {"date": {"start": now}},
            "Нотатка": {"rich_text": [{"text": {"content": note}}]},
        },
    }
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.notion.com/v1/pages", json=payload, headers=NOTION_HEADERS)
        r.raise_for_status()

async def query_notion(start_date: str = None, end_date: str = None) -> list[dict]:
    filters_list = []
    if start_date:
        filters_list.append({"property": "Дата", "date": {"on_or_after": start_date}})
    if end_date:
        filters_list.append({"property": "Дата", "date": {"on_or_before": end_date}})

    body = {"page_size": 100}
    if filters_list:
        body["filter"] = {"and": filters_list} if len(filters_list) > 1 else filters_list[0]

    rows = []
    async with httpx.AsyncClient() as client:
        while True:
            r = await client.post(
                f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
                json=body, headers=NOTION_HEADERS,
            )
            r.raise_for_status()
            data = r.json()
            rows.extend(data["results"])
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
    return rows

def parse_rows(rows: list[dict]):
    entries = []
    for row in rows:
        props = row["properties"]
        try:
            amount   = props["Сума"]["number"] or 0
            category = props["Категорія"]["select"]["name"] if props["Категорія"]["select"] else "Інше"
            entries.append({"amount": amount, "category": category})
        except Exception:
            pass
    return entries

def build_stats(entries: list[dict]) -> str:
    if not entries:
        return "📭 За цей період витрат немає."
    total = sum(e["amount"] for e in entries)
    by_cat = defaultdict(float)
    for e in entries:
        by_cat[e["category"]] += e["amount"]
    sorted_cats = sorted(by_cat.items(), key=lambda x: -x[1])

    lines = [f"💰 *Загалом: {total:,.0f} zł*\n"]
    for cat, amt in sorted_cats:
        pct = amt / total * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        lines.append(f"`{bar}` {cat}\n  {amt:,.0f} zł — {pct:.1f}%")
    return "\n".join(lines)

# ─── CONVERSATION STATE ───────────────────────────────────────────────────────
pending: dict[int, dict] = {}   # user_id → {amount, note}

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
def category_keyboard():
    rows = [CATEGORIES[i:i+2] for i in range(0, len(CATEGORIES), 2)]
    return ReplyKeyboardMarkup([[KeyboardButton(c) for c in row] for row in rows],
                               resize_keyboard=True, one_time_keyboard=True)

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📊 Статистика місяця"), KeyboardButton("📅 Минулий місяць")],
         [KeyboardButton("📆 Весь час"), KeyboardButton("➕ Додати витрату")]],
        resize_keyboard=True,
    )

# ─── GUARDS ───────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return update.effective_user.username in ALLOWED_USERS

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Привіт! Я твій фінансовий трекер.\n\n"
        "Щоб додати витрату, просто напиши суму і (необовʼязково) нотатку:\n"
        "`500` або `500 кава`\n\n"
        "Або скористайся кнопками нижче 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id

    # ── Stat buttons ──
    if text == "📊 Статистика місяця":
        await stats_this_month(update, ctx); return
    if text == "📅 Минулий місяць":
        await stats_last_month(update, ctx); return
    if text == "📆 Весь час":
        await stats_all(update, ctx); return
    if text == "➕ Додати витрату":
        await update.message.reply_text(
            "Введи суму (і нотатку, якщо потрібно):\n`500` або `500 кава`",
            parse_mode="Markdown"
        ); return

    # ── Category selection ──
    if user_id in pending and text in CATEGORIES:
        data = pending.pop(user_id)
        category = clean_category(text)
        try:
            await add_to_notion(data["amount"], category, data.get("note", ""))
            await update.message.reply_text(
                f"✅ Записано!\n\n"
                f"💸 *{data['amount']:,.0f} zł* — {text}\n"
                f"{'📝 ' + data['note'] if data.get('note') else ''}",
                parse_mode="Markdown",
                reply_markup=main_keyboard(),
            )
        except Exception as e:
            logger.error(e)
            await update.message.reply_text("❌ Помилка запису в Notion. Спробуй ще раз.")
        return

    # ── Amount input ──
    match = re.match(r"^(\d+(?:[.,]\d+)?)\s*(.*)?$", text)
    if match:
        amount = float(match.group(1).replace(",", "."))
        note   = (match.group(2) or "").strip()

        # якщо є опис — пробуємо автоматично визначити категорію через Claude
        if note:
            await update.message.reply_text("🤔 Визначаю категорію...", parse_mode="Markdown")
            category = await ai_categorize(note)
            if category:
                try:
                    await add_to_notion(amount, category, note)
                    await update.message.reply_text(
                        f"✅ Записано!\n\n"
                        f"💸 *{amount:,.0f} zł* — {note}\n"
                        f"🏷 Категорія: *{category}*",
                        parse_mode="Markdown",
                        reply_markup=main_keyboard(),
                    )
                except Exception as e:
                    logger.error(e)
                    await update.message.reply_text("❌ Помилка запису в Notion. Спробуй ще раз.")
                return

        # якщо немає опису або AI не впорався — показуємо кнопки
        pending[user_id] = {"amount": amount, "note": note}
        await update.message.reply_text(
            f"Сума: *{amount:,.0f} zł*{' | ' + note if note else ''}\nОбери категорію:",
            parse_mode="Markdown",
            reply_markup=category_keyboard(),
        )
        return

    await update.message.reply_text(
        "Не розумію 🤔 Введи суму, наприклад `500` або `500 кава`",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )

async def stats_this_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = await query_notion(start_date=start)
    entries = parse_rows(rows)
    month_name = now.strftime("%B %Y")
    text = f"📊 *{month_name}*\n\n" + build_stats(entries)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

async def stats_last_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc)
    first_this = now.replace(day=1)
    import calendar
    last_month_end = first_this.replace(hour=23, minute=59, second=59) - __import__("datetime").timedelta(days=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0)
    rows = await query_notion(start_date=last_month_start.isoformat(), end_date=last_month_end.isoformat())
    entries = parse_rows(rows)
    month_name = last_month_end.strftime("%B %Y")
    text = f"📅 *{month_name}*\n\n" + build_stats(entries)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

async def stats_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await query_notion()
    entries = parse_rows(rows)
    text = "📆 *Усі витрати*\n\n" + build_stats(entries)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
